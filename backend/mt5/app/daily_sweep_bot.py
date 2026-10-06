"""
Daily sweep ("turtle soup") auto-trader — CRT on the daily candle.

  C1 = yesterday's broker-day candle (D1)
  C2 = today — when today has swept exactly ONE side of yesterday's range and
       an M30 candle then closes back inside it, the breakout has failed → fade it.

  SELL: today's high > yesterday's high, M30 closes back below it
        — only when the daily trend is UP (a failed new high at the end of a run)
  BUY : today's low < yesterday's low, M30 closes back above it
        — only when the daily trend is DOWN

  Daily trend = yesterday's close vs the average of the last DSW_SMA_DAYS closes.
  Trading against the trend is the point: traders who bought the breakout are
  trapped and their exits push price back through the range. (DSW_TREND=with or
  off are available for comparison.)

  SL : beyond today's extreme so far (+ spread for SELL, + optional buffer)
  TP : the opposite side of yesterday's range (DSW_TP_FRAC = 1.0)
  BE : off by default (DSW_BE_TRIGGER = 0): targets are ~5× the risk, and an early
       break-even cuts the few big winners this style depends on.

  One trade per symbol per broker day. If today sweeps both sides, no trade.

Why this one: ranges are daily (stops ~15-40 pips), so spread + commission are a
small share of the risk, which is what sinks the M15/M30 CRT bots. In a backtest on 6
FX pairs (FXCM M1 bid/ask, 2023 → Sep 2026, same costs and rules as live) it came out
roughly break-even (+0.005R in-sample, +0.007R out-of-sample) with a ~17 % win rate
and deep drawdowns, against −0.25R for the 3-candle CRT. It is an experiment to
measure live, not a proven edge.

Shared rules (filters, one trade per pair, journaling to SQLite) live in bot_common.py.

Settings (env vars):
  DSW_ENABLED            true/false   start trading on boot (default false)
  DSW_SYMBOLS            comma list   (default: same as CRT_SYMBOLS)
  DSW_LOT                fixed lot    (default 0.01; only when BOTS_RISK_PCT=0)
  DSW_DEVIATION          max slippage in points (default 20)
  DSW_MAGIC_BASE         (default 790000; trades use 790030 = M30 confirmation)
  DSW_POLL_INTERVAL      seconds between checks (default 5)
  DSW_MAX_SIGNAL_AGE     ignore an M30 close older than N seconds (default 300)
  DSW_TREND              against / with / off (default against)
  DSW_SMA_DAYS           daily trend average length (default 20)
  DSW_TP_FRAC            TP as a fraction of the way to the opposite side (default 1.0)
  DSW_MIN_RR             minimum reward:risk (default 0, disabled)
  DSW_SL_BUFFER_SPREADS  extra SL room beyond today's extreme, × spread (default 0)
  DSW_MIN_SL_SPREADS     minimum SL distance, × spread (default 3, 0 disables)
  DSW_BE_TRIGGER         fraction of entry→TP that triggers break-even (default 0 = off)
"""

import os
from datetime import datetime, timezone

import MetaTrader5 as mt5

from bot_common import Bot, DEFAULT_SYMBOLS, sl_beyond_sweep

BOT = Bot(name="dsweep", label="DSW", prefix="DSW ", title="Daily sweep",
          description="Today sweeps one side of yesterday's range against the daily trend, "
                      "an M30 candle closes back inside; fade to the other side.")

settings = BOT.settings
events   = BOT.events

CONFIRM_TF = "M30"

# (symbol) -> open time of the last M30 candle already evaluated
_last_bar = {}


def _traded_today(symbol, day_open):
    """True if this bot already opened a trade on `symbol` during the broker day starting at day_open."""
    magic = BOT.magic(CONFIRM_TF)
    if any(p.magic == magic for p in (mt5.positions_get(symbol=symbol) or [])):
        return True
    # broker time can run ahead of UTC, so query a window around it
    deals = mt5.history_deals_get(datetime.fromtimestamp(day_open - 86400, timezone.utc),
                                  datetime.fromtimestamp(day_open + 2 * 86400, timezone.utc)) or []
    return any(d.symbol == symbol and d.magic == magic and d.entry == mt5.DEAL_ENTRY_IN
               and d.time >= day_open for d in deals)


def _trend(daily):
    """+1 / -1 / 0 from completed days: yesterday's close vs the average of the last N closes."""
    n = settings["sma_days"]
    closes = [float(r["close"]) for r in daily[-1 - n:-1]]   # completed days only
    if len(closes) < n:
        return 0
    sma = sum(closes) / n
    last = closes[-1]
    return 1 if last > sma else (-1 if last < sma else 0)


def _check(symbol, tf_name, running):
    # The bot loop offers every timeframe; this strategy only acts on its M30 slot.
    # With a trade open, scan_order() never offers M30 again → one trade per symbol.
    if tf_name != CONFIRM_TF:
        return False

    m30 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M30, 1, 1)
    if m30 is None or len(m30) < 1:
        return False
    bar_time = int(m30[0]["time"])
    if _last_bar.get(symbol) == bar_time:
        return False

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    _last_bar[symbol] = bar_time

    # Only right after the M30 candle closes
    if tick.time - (bar_time + 1800) > settings["max_signal_age"]:
        return False

    daily = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, settings["sma_days"] + 2)
    if daily is None or len(daily) < settings["sma_days"] + 2:
        return False
    today, yday = daily[-1], daily[-2]
    day_open = int(today["time"])
    if bar_time < day_open:
        return False                       # the M30 candle belongs to yesterday

    y_high, y_low = float(yday["high"]), float(yday["low"])
    close = float(m30[0]["close"])

    # Today's high/low up to the close of that M30 candle (not the live tick after it)
    today_bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M30, 1, 48)
    if today_bars is None or len(today_bars) == 0:
        return False
    today_bars = [r for r in today_bars if day_open <= int(r["time"]) <= bar_time]
    if not today_bars:
        return False
    t_high = max(float(r["high"]) for r in today_bars)
    t_low  = min(float(r["low"]) for r in today_bars)

    swept_high, swept_low = t_high > y_high, t_low < y_low
    if swept_high == swept_low:
        return False                       # nothing swept yet, or both sides (no clear story)
    if not (y_low < close < y_high):
        return False                       # not back inside yesterday's range

    side = "SELL" if swept_high else "BUY"

    mode = settings["trend"]
    if mode in ("against", "with"):
        trend = _trend(daily)
        if trend == 0:
            return False
        aligned = (trend > 0) == (side == "BUY")
        if (mode == "against" and aligned) or (mode == "with" and not aligned):
            return False

    if _traded_today(symbol, day_open):
        return False

    BOT.record(symbol, CONFIRM_TF, "signal", side=side,
               y_high=y_high, y_low=y_low, t_high=t_high, t_low=t_low, m30_close=close)

    spread = tick.ask - tick.bid
    extreme = t_high if side == "SELL" else t_low
    sl = sl_beyond_sweep(side, extreme, spread, settings["sl_buffer_spreads"])
    target = y_low if side == "SELL" else y_high
    entry = tick.ask if side == "BUY" else tick.bid
    tp = entry + (target - entry) * settings["tp_frac"]

    return BOT.place(symbol, CONFIRM_TF, side, sl, tp, tick,
                     comment=f"DSW {CONFIRM_TF} {day_open}", to_close=running,
                     signal_time=day_open, be_trigger=_be_price(entry, tp))


def _be_price(entry, tp):
    frac = settings["be_trigger"]
    return entry + (tp - entry) * frac if frac > 0 else None


def _be_trigger(pos):
    if pos.tp == 0:
        return None
    return _be_price(pos.price_open, pos.tp)


def start_daily_sweep_bot():
    default_symbols = os.environ.get("CRT_SYMBOLS", DEFAULT_SYMBOLS)
    settings.update({
        "symbols":           [s.strip() for s in
                              os.environ.get("DSW_SYMBOLS", default_symbols).split(",")
                              if s.strip()],
        "lot":               float(os.environ.get("DSW_LOT", "0.01")),
        "deviation":         int(os.environ.get("DSW_DEVIATION", "20")),
        "magic_base":        int(os.environ.get("DSW_MAGIC_BASE", "790000")),
        "poll_interval":     float(os.environ.get("DSW_POLL_INTERVAL", "5")),
        "max_signal_age":    int(os.environ.get("DSW_MAX_SIGNAL_AGE", "300")),
        "trend":             os.environ.get("DSW_TREND", "against").strip().lower(),
        "sma_days":          int(os.environ.get("DSW_SMA_DAYS", "20")),
        "tp_frac":           float(os.environ.get("DSW_TP_FRAC", "1.0")),
        "min_rr":            float(os.environ.get("DSW_MIN_RR", "0")),
        "sl_buffer_spreads": float(os.environ.get("DSW_SL_BUFFER_SPREADS", "0")),
        "min_sl_spreads":    float(os.environ.get("DSW_MIN_SL_SPREADS", "3")),
        "be_trigger":        float(os.environ.get("DSW_BE_TRIGGER", "0")),
    })
    BOT.start(_check, _be_trigger,
              enabled_by_default=os.environ.get("DSW_ENABLED", "false").lower() == "true")


def enable():
    return BOT.try_enable()


def disable():
    BOT.enabled.clear()


def is_enabled():
    return BOT.enabled.is_set()
