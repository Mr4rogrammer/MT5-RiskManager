"""
Strict 3-candle CRT — its own bot (crt3s), next to crt3, which is left unchanged.
Same C2 sweep / C3 entry as crt3, plus two filters on the candles and a break-even
at C1's midpoint, so the dashboard shows whether the stricter setup trades better.

  C1 = range candle         its wicks together ≤ C1_MAX_WICK (default 30 %) of its range
                            — a strong, full-bodied candle
  C2 = sweep candle         takes out one side of C1 and closes back inside, and the
                            WHOLE candle (wick included) stays within C2_MAX_RETRACE
                            (default 40 %) of C1's range from the swept side
  C3 = the candle now opening ← entry here, within MAX_SIGNAL_AGE seconds of its open

  Bearish (C1 high 100, low 0):  C2.high > 100, C2.close < 100, C2.low > 60  → SELL
  Bullish:                       C2.low  < 0,   C2.close > 0,   C2.high < 40 → BUY

  SL : beyond C2's sweep wick (+ spread for SELL), plus SL_BUFFER_SPREADS × spread
  TP : C1's opposite side (C1 low for SELL, C1 high for BUY)
  BE : when price reaches C1's BE_LEVEL (default 50 %) — the midpoint — PARTIAL_PCT
       (default 50 %) is closed and the SL moves to entry ± round-trip commission

A setup that is a crt3 setup but fails a filter is recorded as skipped ("C1 wicks …",
"C2 crossed C1 40% level"), so the dashboard's skip reasons show what the filters remove.

Settings (env vars). Every setting not listed defaults to crt3's CRT_<X> value:
  CRT3S_ENABLED          start trading on boot (default false)
  CRT3S_MAGIC_BASE       (default 860000)
  CRT3S_C1_MAX_WICK      C1's upper + lower wick ÷ C1 range, at most (default 0.30)
  CRT3S_C2_MAX_RETRACE   how far into C1 C2 may reach, ÷ C1 range (default 0.40)
  CRT3S_BE_LEVEL         break-even / partial at this fraction of C1's range from the
                         swept side (default 0.50 = midpoint)
  CRT3S_<X>              any crt3 setting, e.g. CRT3S_SYMBOLS, CRT3S_PARTIAL_PCT, CRT3S_MIN_RR
"""

import os
from datetime import datetime, timezone

import MetaTrader5 as mt5

from bot_common import Bot, TIMEFRAMES, DEFAULT_SYMBOLS, sl_beyond_sweep
from crt_bot import detect_signal

BOT = Bot(name="crt3s", label="CRT3S", prefix="SCRT3 ", title="3-candle CRT strict",
          description="Strong C1 (wicks ≤ 30% of range); C2 sweeps one side, closes back inside "
                      "and stays within 40% of C1; enter at C3 open. TP C1's other side; "
                      "half closed and SL to break-even at C1's 50%.")

settings = BOT.settings
events   = BOT.events

_last_bar = {}      # (symbol, tf_name) -> open time of the last C2 evaluated
_be_level = {}      # ticket -> BE trigger price


def _be_price(c1_high, c1_low, side):
    """C1's BE_LEVEL from the swept side: SELL swept the high, so measure down from it."""
    depth = (c1_high - c1_low) * settings["be_level"]
    return c1_high - depth if side == "SELL" else c1_low + depth


def strict_filter(c1, c2, side):
    """None if C1 and C2 pass the strict rules, else the reason they don't."""
    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    size = c1_high - c1_low
    if size <= 0:
        return "C1 has no range"

    wicks = size - abs(float(c1["close"]) - float(c1["open"]))
    if wicks / size > settings["c1_max_wick"]:
        return f"C1 wicks {wicks / size:.0%} of range (max {settings['c1_max_wick']:.0%})"

    limit = settings["c2_max_retrace"]
    if side == "SELL":
        level = c1_high - size * limit
        reach = c1_high - float(c2["low"])
        crossed = float(c2["low"]) <= level
    else:
        level = c1_low + size * limit
        reach = float(c2["high"]) - c1_low
        crossed = float(c2["high"]) >= level
    if crossed:
        return f"C2 crossed C1 {limit:.0%} level (reached {reach / size:.0%})"
    return None


def _check(symbol, tf_name, running):
    """
      pos 0 = C3 — currently forming (entry candle)
      pos 1 = C2 — just closed (sweep candle)
      pos 2 = C1 — closed before C2 (range candle)
    """
    tf, tf_seconds = TIMEFRAMES[tf_name]
    rates = mt5.copy_rates_from_pos(symbol, tf, 1, 2)
    if rates is None or len(rates) < 2:
        return False

    c1, c2  = rates[0], rates[1]
    c2_time = int(c2["time"])
    key = (symbol, tf_name)
    if _last_bar.get(key) == c2_time:
        return False          # already evaluated this C2
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False          # retry this C2 on the next poll
    _last_bar[key] = c2_time

    # Entry is valid only near C3's open
    if tick.time - (c2_time + tf_seconds) > settings["max_signal_age"]:
        return False

    signal = detect_signal(c1, c2)
    if signal is None:
        return False

    side = signal["side"]
    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    BOT.record(symbol, tf_name, "signal", side=side,
               c1_open=float(c1["open"]), c1_high=c1_high, c1_low=c1_low,
               c1_close=float(c1["close"]), c2_high=float(c2["high"]),
               c2_low=float(c2["low"]), c2_close=float(c2["close"]))

    why = strict_filter(c1, c2, side)
    if why:
        BOT.record(symbol, tf_name, "skipped", side=side, reason=why)
        return False

    sl = sl_beyond_sweep(side, signal["extreme"], tick.ask - tick.bid,
                         settings["sl_buffer_spreads"])
    be = _be_price(c1_high, c1_low, side)
    comment = f"SCRT3 {tf_name} {int(c1['time'])}"
    traded = BOT.place(symbol, tf_name, side, sl, signal["tp"], tick,
                       comment=comment, to_close=running, signal_time=c2_time,
                       be_trigger=be)
    if traded:
        for pos in BOT.open_positions(symbol):
            if pos.comment == comment:
                _be_level[pos.ticket] = be
    return traded


def _be_trigger(pos):
    """Cached, else the level journaled at open, else rebuilt from the C1 candle."""
    if pos.ticket in _be_level:
        return _be_level[pos.ticket]
    row = BOT._journal(pos.ticket)
    if row and row.get("be_trigger"):
        _be_level[pos.ticket] = row["be_trigger"]
        return row["be_trigger"]
    parts = pos.comment.split()                          # "SCRT3 <TF> <C1 open time>"
    tf_name = BOT.position_tf(pos)
    if len(parts) < 3 or tf_name is None or not parts[2].isdigit():
        return None
    c1_ts = int(parts[2])
    rates = mt5.copy_rates_from(pos.symbol, TIMEFRAMES[tf_name][0],
                                datetime.fromtimestamp(c1_ts, timezone.utc), 1)
    if rates is None or len(rates) == 0 or int(rates[0]["time"]) != c1_ts:
        return None
    side = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    _be_level[pos.ticket] = _be_price(float(rates[0]["high"]), float(rates[0]["low"]), side)
    return _be_level[pos.ticket]


def _env(name, default):
    """CRT3S_<name>, else crt3's CRT_<name>, else the default."""
    return os.environ.get(f"CRT3S_{name}", os.environ.get(f"CRT_{name}", default))


def start_crt3_strict_bot():
    settings.update({
        "symbols":           [x.strip() for x in _env("SYMBOLS", DEFAULT_SYMBOLS).split(",") if x.strip()],
        "lot":               float(_env("LOT", "0.01")),
        "deviation":         int(_env("DEVIATION", "20")),
        "magic_base":        int(os.environ.get("CRT3S_MAGIC_BASE", "860000")),
        "poll_interval":     float(_env("POLL_INTERVAL", "5")),
        "max_signal_age":    int(_env("MAX_SIGNAL_AGE", "300")),
        "min_rr":            float(_env("MIN_RR", "1.0")),
        "sl_buffer_spreads": float(_env("SL_BUFFER_SPREADS", "0")),
        "min_sl_spreads":    float(_env("MIN_SL_SPREADS", "3")),
        "partial_pct":       float(_env("PARTIAL_PCT", "50")),
        "trail_r":           float(_env("TRAIL_R", "0")),
        "c1_max_wick":       float(os.environ.get("CRT3S_C1_MAX_WICK", "0.30")),
        "c2_max_retrace":    float(os.environ.get("CRT3S_C2_MAX_RETRACE", "0.40")),
        "be_level":          float(os.environ.get("CRT3S_BE_LEVEL", "0.50")),
    })
    BOT.start(_check, _be_trigger,
              enabled_by_default=os.environ.get("CRT3S_ENABLED", "false").lower() == "true")
