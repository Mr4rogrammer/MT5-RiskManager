"""
CRT (Candle Range Theory) auto-trader — 3 candles.

Pattern (per symbol, HTF-first: H4 → H1 → M30 → M15):

  C1 = range candle
  C2 = sweep candle: closes back inside C1 after taking out one side
  C3 = the candle that is now opening  ← entry happens here

  Bearish CRT: C2.high > C1.high  AND  C2.close inside C1  →  SELL at C3 open
  Bullish CRT: C2.low  < C1.low   AND  C2.close inside C1  →  BUY  at C3 open

  SL : beyond C2's sweep extreme (C2.high + spread for SELL, C2.low for BUY),
       plus CRT_SL_BUFFER_SPREADS × spread on both sides
  TP : opposite side of C1 (C1.low for SELL, C1.high for BUY)
  BE : SL moves to entry ± round-trip commission once price covers
       CRT_BE_TRIGGER (default 47 %) of the entry→TP distance, and at that moment
       CRT_PARTIAL_PCT (default 50 %) of the position is closed to bank profit

  C3 entry only — a setup is ignored once C3 is older than CRT_MAX_SIGNAL_AGE s.

Shared rules (HTF priority, one trade per pair, higher-TF override, filters,
break-even mechanics, journaling to SQLite) live in bot_common.py.

Settings (env vars):
  CRT_ENABLED             true/false   start trading on boot (default false)
  CRT_SYMBOLS             comma list   (default XAUUSD,EURUSD,GBPUSD,AUDUSD,USDCHF,NZDUSD,USDCAD)
  CRT_LOT                 fixed lot    (default 0.01; only when BOTS_RISK_PCT=0)
  CRT_DEVIATION           max slippage in points (default 20)
  CRT_MAGIC_BASE          (default 770000)
  CRT_POLL_INTERVAL       seconds between checks (default 5)
  CRT_MAX_SIGNAL_AGE      ignore setups whose C3 opened more than N seconds ago (default 300)
  CRT_MIN_RR              minimum reward:risk ratio (default 1.0, 0 disables)
  CRT_SL_BUFFER_SPREADS   extra SL room beyond the wick, × spread (default 0)
  CRT_MIN_SL_SPREADS      minimum SL distance, × spread (default 3, 0 disables)
  CRT_BE_TRIGGER          fraction of entry→TP that triggers break-even (default 0.47)
  CRT_PARTIAL_PCT         % of the position closed when break-even triggers (default 50, 0 = off)
  CRT_TRAIL_R             after break-even, trail the SL this × the initial risk behind price
                          (default 0 = off; the TP stays at C1's far side)
  CRT_COMMISSION_*        see bot_common.py
"""

import os

import MetaTrader5 as mt5

from bot_common import Bot, TIMEFRAMES, DEFAULT_SYMBOLS, sl_beyond_sweep

BOT = Bot(name="crt3", label="CRT", prefix="CRT ", title="3-candle CRT",
          description="C2 sweeps C1 and closes back inside; enter at C3 open. "
                      "TP opposite side of C1, break-even at 47% of entry→TP.")

# Module-level names used by routes/bot.py
settings = BOT.settings
events   = BOT.events

# (symbol, tf_name) -> open time of the last C2 already evaluated
_last_bar = {}


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def detect_signal(c1, c2):
    """
    Return {"side", "extreme", "tp"} if C2 forms a valid CRT setup against C1, else None.
    `extreme` is C2's sweep wick (SL goes beyond it); TP is the opposite side of C1.
    """
    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    c2_high, c2_low, c2_close = float(c2["high"]), float(c2["low"]), float(c2["close"])

    # C2 must close back inside C1's range
    if not (c1_low < c2_close < c1_high):
        return None

    swept_high = c2_high > c1_high
    swept_low  = c2_low  < c1_low

    # No sweep, or swept both sides — no clear direction
    if swept_high == swept_low:
        return None

    if swept_high:
        return {"side": "SELL", "extreme": c2_high, "tp": c1_low}
    return {"side": "BUY", "extreme": c2_low, "tp": c1_high}


def _check(symbol, tf_name, running):
    """
    Look for a CRT setup on (symbol, tf_name).

      pos 0 = C3 — currently forming (entry candle)
      pos 1 = C2 — just closed (sweep candle)
      pos 2 = C1 — closed before C2 (range candle)

    Returns True if a trade was placed.
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

    BOT.record(symbol, tf_name, "signal", side=signal["side"],
               c1_high=float(c1["high"]), c1_low=float(c1["low"]),
               c2_high=float(c2["high"]), c2_low=float(c2["low"]),
               c2_close=float(c2["close"]))

    side  = signal["side"]
    sl    = sl_beyond_sweep(side, signal["extreme"], tick.ask - tick.bid,
                            settings["sl_buffer_spreads"])
    tp    = signal["tp"]
    entry = tick.ask if side == "BUY" else tick.bid

    return BOT.place(symbol, tf_name, side, sl, tp, tick,
                     comment=f"CRT {tf_name} {c2_time}", to_close=running,
                     signal_time=c2_time,
                     be_trigger=entry + (tp - entry) * settings["be_trigger"])


def _be_trigger(pos):
    """Break-even triggers CRT_BE_TRIGGER of the way from entry to TP."""
    if pos.tp == 0:
        return None
    return pos.price_open + (pos.tp - pos.price_open) * settings["be_trigger"]


# ---------------------------------------------------------------------------
# Start / control
# ---------------------------------------------------------------------------

def start_crt_bot():
    settings.update({
        "symbols":           [s.strip() for s in
                              os.environ.get("CRT_SYMBOLS", DEFAULT_SYMBOLS).split(",")
                              if s.strip()],
        "lot":               float(os.environ.get("CRT_LOT", "0.01")),
        "deviation":         int(os.environ.get("CRT_DEVIATION", "20")),
        "magic_base":        int(os.environ.get("CRT_MAGIC_BASE", "770000")),
        "poll_interval":     float(os.environ.get("CRT_POLL_INTERVAL", "5")),
        "max_signal_age":    int(os.environ.get("CRT_MAX_SIGNAL_AGE", "300")),
        "min_rr":            float(os.environ.get("CRT_MIN_RR", "1.0")),
        "sl_buffer_spreads": float(os.environ.get("CRT_SL_BUFFER_SPREADS", "0")),
        "min_sl_spreads":    float(os.environ.get("CRT_MIN_SL_SPREADS", "3")),
        "be_trigger":        float(os.environ.get("CRT_BE_TRIGGER", "0.47")),
        "partial_pct":       float(os.environ.get("CRT_PARTIAL_PCT", "50")),
        "trail_r":           float(os.environ.get("CRT_TRAIL_R", "0")),
    })
    BOT.start(_check, _be_trigger,
              enabled_by_default=os.environ.get("CRT_ENABLED", "false").lower() == "true")


def enable():
    return BOT.try_enable()


def disable():
    BOT.enabled.clear()


def is_enabled():
    return BOT.enabled.is_set()
