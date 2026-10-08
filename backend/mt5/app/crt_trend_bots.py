"""
CRT with a higher-timeframe trend filter — two bots, separate from crt_bot.py and
candle_two_bot.py, which are left unchanged:

  crt3t  3-candle CRT + trend   same signal, SL, TP, break-even and partial as crt3
  crt2t  2-candle CRT + trend   same signal, SL, TP, break-even and partial as crt2

The only difference is the trend filter, so the dashboard shows whether it helps:
crt3 vs crt3t and crt2 vs crt2t trade the same setups, except the ones filtered out.

Trend = the next higher timeframe, read on its last CLOSED candle (never mid-candle):

  CRT on   M15 → H1    M30 → H4    H1 → H4    H4 → D1

  bullish  close above EMA(TREND_EMA, default 50) and the EMA higher than
           TREND_SLOPE_BARS (default 5) candles ago  → only BUY setups are taken
  bearish  close below the EMA and the EMA lower than 5 candles ago → only SELL
  else     no clear trend → the setup is skipped

Skipped setups show as "against trend" / "no clear trend" in the dashboard's skip
reasons; every signal event also records the trend it saw.

Both bots run alongside the originals on a hedging account, so a setup that passes the
filter is traded twice (once by each bot) — with BOTS_RISK_PCT that is twice the risk on
that setup. BOTS_MAX_OPEN_TRADES and the daily loss limit count all bots together.

Settings (env vars). Every setting defaults to the original bot's value (CRT_* / CRT2_*),
so only the trend differs unless you change it:
  CRT3T_ENABLED / CRT2T_ENABLED        start trading on boot (default false)
  CRT3T_MAGIC_BASE                     (default 830000)
  CRT2T_MAGIC_BASE                     (default 840000)
  CRT3T_<X> / CRT2T_<X>                any setting of the original bot, e.g. CRT3T_SYMBOLS,
                                       CRT2T_BE_TRIGGER, CRT3T_PARTIAL_PCT
  CRT3T_TREND_EMA / CRT2T_TREND_EMA    EMA length on the higher timeframe (default 50)
  CRT3T_TREND_SLOPE_BARS / CRT2T_…     the EMA must be rising/falling vs this many
                                       candles ago (default 5)
"""

import os
import time
from datetime import datetime, timezone

import numpy as np
import MetaTrader5 as mt5

from bot_common import Bot, TIMEFRAMES, DEFAULT_SYMBOLS, sl_beyond_sweep
from crt_bot import detect_signal


def ema(x, n):
    """Exponential moving average, seeded with the SMA (numpy, oldest → newest)."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


# CRT timeframe -> timeframe the trend is read on
TREND_TF = {
    "M15": ("H1", mt5.TIMEFRAME_H1),
    "M30": ("H4", mt5.TIMEFRAME_H4),
    "H1":  ("H4", mt5.TIMEFRAME_H4),
    "H4":  ("D1", mt5.TIMEFRAME_D1),
}


def trend(symbol, tf_name, settings):
    """
    ("BUY" | "SELL" | None, description) — the side the higher timeframe allows.
    None = no clear trend (or not enough candles).
    """
    htf_name, htf = TREND_TF[tf_name]
    n, slope = settings["trend_ema"], settings["trend_slope_bars"]
    rates = mt5.copy_rates_from_pos(symbol, htf, 1, n * 4 + slope)   # closed candles only
    if rates is None or len(rates) < n + slope + 1:
        return None, f"{htf_name} not enough candles"
    close = np.asarray(rates["close"], dtype=float)
    line = ema(close, n)
    now, before = line[-1], line[-1 - slope]
    if close[-1] > now and now > before:
        return "BUY", f"{htf_name} bullish"
    if close[-1] < now and now < before:
        return "SELL", f"{htf_name} bearish"
    return None, f"{htf_name} no clear trend"


def _env(prefix, fallback, name, default):
    """CRT3T_<name>, else the original bot's CRT_<name>, else the default."""
    return os.environ.get(f"{prefix}_{name}", os.environ.get(f"{fallback}_{name}", default))


def _trend_settings(prefix):
    return {
        "trend_ema":        int(os.environ.get(f"{prefix}_TREND_EMA", "50")),
        "trend_slope_bars": int(os.environ.get(f"{prefix}_TREND_SLOPE_BARS", "5")),
    }


def _gate(bot, symbol, tf_name, side, signal_details):
    """Record the signal with its trend; True if the trend allows `side`."""
    allowed, why = trend(symbol, tf_name, bot.settings)
    bot.record(symbol, tf_name, "signal", side=side, trend=why, **signal_details)
    if allowed == side:
        return True
    reason = "no clear trend" if allowed is None else "against trend"
    bot.record(symbol, tf_name, "skipped", side=side, reason=f"{reason} ({why})")
    return False


# ---------------------------------------------------------------------------
# 3-candle CRT + trend  (signal: crt_bot.detect_signal)
# ---------------------------------------------------------------------------

BOT3 = Bot(name="crt3t", label="CRT3T", prefix="TCRT3 ", title="3-candle CRT + trend",
           description="3-candle CRT, only in the direction of the next higher timeframe "
                       "(close vs rising/falling EMA 50). Otherwise the same as 3-candle CRT.")
_last_bar3 = {}     # (symbol, tf_name) -> open time of the last C2 evaluated


def _check3(symbol, tf_name, running):
    s = BOT3.settings
    tf, tf_seconds = TIMEFRAMES[tf_name]
    rates = mt5.copy_rates_from_pos(symbol, tf, 1, 2)
    if rates is None or len(rates) < 2:
        return False
    c1, c2 = rates[0], rates[1]
    c2_time = int(c2["time"])
    key = (symbol, tf_name)
    if _last_bar3.get(key) == c2_time:
        return False
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    _last_bar3[key] = c2_time
    if tick.time - (c2_time + tf_seconds) > s["max_signal_age"]:
        return False

    signal = detect_signal(c1, c2)
    if signal is None:
        return False
    side = signal["side"]
    if not _gate(BOT3, symbol, tf_name, side, {
            "c1_high": float(c1["high"]), "c1_low": float(c1["low"]),
            "c2_high": float(c2["high"]), "c2_low": float(c2["low"]),
            "c2_close": float(c2["close"])}):
        return False

    sl = sl_beyond_sweep(side, signal["extreme"], tick.ask - tick.bid, s["sl_buffer_spreads"])
    tp = signal["tp"]
    entry = tick.ask if side == "BUY" else tick.bid
    return BOT3.place(symbol, tf_name, side, sl, tp, tick,
                      comment=f"TCRT3 {tf_name} {c2_time}", to_close=running,
                      signal_time=c2_time,
                      be_trigger=entry + (tp - entry) * s["be_trigger"])


def _be_trigger3(pos):
    if pos.tp == 0:
        return None
    return pos.price_open + (pos.tp - pos.price_open) * BOT3.settings["be_trigger"]


def start_crt3_trend_bot():
    e = lambda name, default: _env("CRT3T", "CRT", name, default)
    BOT3.settings.update({
        "symbols":           [x.strip() for x in e("SYMBOLS", DEFAULT_SYMBOLS).split(",") if x.strip()],
        "lot":               float(e("LOT", "0.01")),
        "deviation":         int(e("DEVIATION", "20")),
        "magic_base":        int(os.environ.get("CRT3T_MAGIC_BASE", "830000")),
        "poll_interval":     float(e("POLL_INTERVAL", "5")),
        "max_signal_age":    int(e("MAX_SIGNAL_AGE", "300")),
        "min_rr":            float(e("MIN_RR", "1.0")),
        "sl_buffer_spreads": float(e("SL_BUFFER_SPREADS", "0")),
        "min_sl_spreads":    float(e("MIN_SL_SPREADS", "3")),
        "be_trigger":        float(e("BE_TRIGGER", "0.47")),
        "partial_pct":       float(e("PARTIAL_PCT", "50")),
        "trail_r":           float(e("TRAIL_R", "0")),
        **_trend_settings("CRT3T"),
    })
    BOT3.start(_check3, _be_trigger3,
               enabled_by_default=os.environ.get("CRT3T_ENABLED", "false").lower() == "true")


# ---------------------------------------------------------------------------
# 2-candle CRT + trend  (same logic as candle_two_bot._check)
# ---------------------------------------------------------------------------

BOT2 = Bot(name="crt2t", label="CRT2T", prefix="TCRT2 ", title="2-candle CRT + trend",
           description="2-candle CRT, only in the direction of the next higher timeframe "
                       "(close vs rising/falling EMA 50). Otherwise the same as 2-candle CRT.")
_last_seen2 = {}    # (symbol, tf_name) -> (c2_time, bid_was_above_open, unix time)
_done2 = set()      # (symbol, tf_name, c2_time) traded or filtered out — one decision per C2
_be_level2 = {}     # ticket -> BE trigger price


def _be_price2(c1_high, c1_low, side):
    depth = (c1_high - c1_low) * BOT2.settings["be_trigger"]
    return c1_low + depth if side == "BUY" else c1_high - depth


def _be_trigger2(pos):
    """Cached, else the level journaled at open, else rebuilt from the C1 candle."""
    if pos.ticket in _be_level2:
        return _be_level2[pos.ticket]
    row = BOT2._journal(pos.ticket)
    if row and row.get("be_trigger"):
        _be_level2[pos.ticket] = row["be_trigger"]
        return row["be_trigger"]
    parts = pos.comment.split()                          # "TCRT2 <TF> <C1 open time>"
    tf_name = BOT2.position_tf(pos)
    if len(parts) < 3 or tf_name is None or not parts[2].isdigit():
        return None
    c1_ts = int(parts[2])
    rates = mt5.copy_rates_from(pos.symbol, TIMEFRAMES[tf_name][0],
                                datetime.fromtimestamp(c1_ts, timezone.utc), 1)
    if rates is None or len(rates) == 0 or int(rates[0]["time"]) != c1_ts:
        return None
    side = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    _be_level2[pos.ticket] = _be_price2(float(rates[0]["high"]), float(rates[0]["low"]), side)
    return _be_level2[pos.ticket]


def _check2(symbol, tf_name, running):
    s = BOT2.settings
    tf, _ = TIMEFRAMES[tf_name]
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, 2)
    if rates is None or len(rates) < 2:
        return False
    c1, c2 = rates[0], rates[1]
    c2_time = int(c2["time"])
    if (symbol, tf_name, c2_time) in _done2:
        return False
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    c2_open = float(c2["open"])
    above = tick.bid > c2_open
    now = time.time()
    key = (symbol, tf_name)
    prev = _last_seen2.get(key)
    _last_seen2[key] = (c2_time, above, now)
    fresh = (prev is not None and prev[0] == c2_time
             and now - prev[2] <= s["poll_interval"] * 3)
    if not fresh or prev[1] == above:
        return False
    if not (c1_low < c2_open < c1_high):
        return False
    swept_low = float(c2["low"]) < c1_low
    swept_high = float(c2["high"]) > c1_high
    if swept_low == swept_high:
        return False
    if swept_low and above:
        side, extreme, tp = "BUY", float(c2["low"]), c1_high
    elif swept_high and not above:
        side, extreme, tp = "SELL", float(c2["high"]), c1_low
    else:
        return False

    # Forget decided C2s older than a day
    cutoff = c2_time - 24 * 3600
    for k in [k for k in _done2 if k[2] < cutoff]:
        _done2.discard(k)

    # The higher-TF candle can't close inside C2, so the trend can't change for this C2:
    # decide once, instead of re-signalling on every cross back and forth
    if not _gate(BOT2, symbol, tf_name, side, {
            "c1_high": c1_high, "c1_low": c1_low, "c2_open": c2_open,
            "c2_high": float(c2["high"]), "c2_low": float(c2["low"]), "bid": tick.bid}):
        _done2.add((symbol, tf_name, c2_time))
        return False

    sl = sl_beyond_sweep(side, extreme, tick.ask - tick.bid, s["sl_buffer_spreads"])
    be = _be_price2(c1_high, c1_low, side)
    comment = f"TCRT2 {tf_name} {int(c1['time'])}"
    traded = BOT2.place(symbol, tf_name, side, sl, tp, tick, comment=comment,
                        to_close=running, signal_time=int(c1["time"]), be_trigger=be)
    if traded:
        _done2.add((symbol, tf_name, c2_time))
        for pos in BOT2.open_positions(symbol):
            if pos.comment == comment:
                _be_level2[pos.ticket] = be
    return traded


def start_crt2_trend_bot():
    e = lambda name, default: _env("CRT2T", "CRT2", name, default)
    BOT2.settings.update({
        "symbols":           [x.strip() for x in
                              e("SYMBOLS", os.environ.get("CRT_SYMBOLS", DEFAULT_SYMBOLS)).split(",")
                              if x.strip()],
        "lot":               float(e("LOT", "0.01")),
        "deviation":         int(e("DEVIATION", "20")),
        "magic_base":        int(os.environ.get("CRT2T_MAGIC_BASE", "840000")),
        "poll_interval":     float(e("POLL_INTERVAL", "2")),
        "min_rr":            float(e("MIN_RR", "0")),
        "sl_buffer_spreads": float(e("SL_BUFFER_SPREADS", "0")),
        "min_sl_spreads":    float(e("MIN_SL_SPREADS", "3")),
        "be_trigger":        float(e("BE_TRIGGER", "0.45")),
        "partial_pct":       float(e("PARTIAL_PCT", "50")),
        "trail_r":           float(e("TRAIL_R", "0")),
        **_trend_settings("CRT2T"),
    })
    BOT2.start(_check2, _be_trigger2,
               enabled_by_default=os.environ.get("CRT2T_ENABLED", "false").lower() == "true")


def start_crt_trend_bots():
    start_crt3_trend_bot()
    start_crt2_trend_bot()
