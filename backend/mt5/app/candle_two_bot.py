"""
2-candle CRT (Candle Range Theory) auto-trader.

Same idea as crt_bot.py, but the entry happens INSIDE C2 instead of at C3's open.

  C1 = range candle (last closed candle)
  C2 = candle currently forming — must open inside C1

  Bullish: C2 sweeps below C1.low, then price crosses back ABOVE C2.open  →  BUY
  Bearish: C2 sweeps above C1.high, then price crosses back BELOW C2.open →  SELL

  SL : C2's sweep extreme so far (C2.low for BUY, C2.high + spread for SELL),
       plus CRT2_SL_BUFFER_SPREADS × spread on both sides
  TP : opposite side of C1 (C1.high for BUY, C1.low for SELL)
  BE : SL moves to entry ± round-trip commission once price reaches
       CRT2_BE_TRIGGER (default 45 %) into C1's range, measured from the swept side:
         BUY  trigger = C1.low  + 45 % × C1 range
         SELL trigger = C1.high − 45 % × C1 range

Specific to this bot:
  • Fresh cross only — the cross over C2.open must be seen between two polls no more
                       than 3 poll intervals apart, so the bot never chases a cross
                       it missed (e.g. after a restart).
  • One trade per C2 — a C2 candle is traded at most once.

Shared rules (HTF priority, one trade per pair, higher-TF override, filters,
break-even mechanics, journaling to SQLite) live in bot_common.py.

Both bots run at the same time and are independent: this one uses its own magic
numbers (CRT2_MAGIC_BASE + TF minutes) and the comment "CRT2 <TF> <C1 open time>".

Settings (env vars):
  CRT2_ENABLED            true/false   start trading on boot (default false)
  CRT2_SYMBOLS            comma list   (default: same as CRT_SYMBOLS)
  CRT2_LOT                fixed lot    (default 0.01; only when BOTS_RISK_PCT=0)
  CRT2_DEVIATION          max slippage in points (default 20)
  CRT2_MAGIC_BASE         (default 780000)
  CRT2_POLL_INTERVAL      seconds between checks (default 2 — entries are intrabar)
  CRT2_MIN_RR             minimum reward:risk ratio (default 0, disabled)
  CRT2_SL_BUFFER_SPREADS  extra SL room beyond the sweep, × spread (default 0)
  CRT2_MIN_SL_SPREADS     minimum SL distance, × spread (default 3, 0 disables)
  CRT2_BE_TRIGGER         fraction into C1's range that triggers break-even (default 0.45)
"""

import os
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

from bot_common import Bot, TIMEFRAMES, DEFAULT_SYMBOLS, sl_beyond_sweep

BOT = Bot(name="crt2", label="CRT2", prefix="CRT2 ", title="2-candle CRT",
          description="C2 sweeps C1, then crosses back through its own open; enter inside C2. "
                      "TP opposite side of C1, break-even 45% into C1.")

# Module-level names used by routes/bot.py
settings = BOT.settings
events   = BOT.events

# (symbol, tf_name) -> (c2_time, bid_was_above_open, unix time of observation)
_last_seen = {}

# (symbol, tf_name, c2_time) already traded — one trade per C2
_traded = set()

# ticket -> BE trigger price; rebuilt from the C1 candle after a restart
_be_level = {}


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def _be_price(c1_high, c1_low, side):
    """CRT2_BE_TRIGGER into C1's range, measured from the swept side."""
    depth = (c1_high - c1_low) * settings["be_trigger"]
    return c1_low + depth if side == "BUY" else c1_high - depth


def _be_trigger(pos):
    """Break-even trigger for an open position; rebuilt from its C1 candle if not cached."""
    if pos.ticket in _be_level:
        return _be_level[pos.ticket]

    # Comment: "CRT2 <TF> <C1 open time>"
    parts   = pos.comment.split()
    tf_name = BOT.position_tf(pos)
    if len(parts) < 3 or tf_name is None or not parts[2].isdigit():
        return None

    c1_time = datetime.fromtimestamp(int(parts[2]), timezone.utc)
    rates   = mt5.copy_rates_range(pos.symbol, TIMEFRAMES[tf_name][0], c1_time, c1_time)
    if rates is None or len(rates) == 0:
        return None

    side  = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    level = _be_price(float(rates[0]["high"]), float(rates[0]["low"]), side)
    _be_level[pos.ticket] = level
    return level


def _check(symbol, tf_name, running):
    """
    Look for a 2-candle CRT entry on (symbol, tf_name).

      pos 0 = C2 — currently forming (sweep + entry candle)
      pos 1 = C1 — last closed candle (range)

    Returns True if a trade was placed.
    """
    tf, _ = TIMEFRAMES[tf_name]
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, 2)
    if rates is None or len(rates) < 2:
        return False

    c1, c2  = rates[0], rates[1]
    c2_time = int(c2["time"])
    if (symbol, tf_name, c2_time) in _traded:
        return False

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    c2_open = float(c2["open"])
    above   = tick.bid > c2_open
    now     = time.time()

    # Detect a cross of C2.open between the previous poll and this one
    key  = (symbol, tf_name)
    prev = _last_seen.get(key)
    _last_seen[key] = (c2_time, above, now)
    fresh = (prev is not None and prev[0] == c2_time
             and now - prev[2] <= settings["poll_interval"] * 3)
    if not fresh or prev[1] == above:
        return False

    # C2 must open inside C1
    if not (c1_low < c2_open < c1_high):
        return False

    swept_low  = float(c2["low"])  < c1_low
    swept_high = float(c2["high"]) > c1_high
    if swept_low == swept_high:
        return False          # no sweep yet, or both sides swept

    if swept_low and above:
        side, extreme, tp = "BUY", float(c2["low"]), c1_high      # swept low, crossed back up
    elif swept_high and not above:
        side, extreme, tp = "SELL", float(c2["high"]), c1_low     # swept high, crossed back down
    else:
        return False

    BOT.record(symbol, tf_name, "signal", side=side,
               c1_high=c1_high, c1_low=c1_low, c2_open=c2_open,
               c2_high=float(c2["high"]), c2_low=float(c2["low"]), bid=tick.bid)

    sl = sl_beyond_sweep(side, extreme, tick.ask - tick.bid, settings["sl_buffer_spreads"])
    be = _be_price(c1_high, c1_low, side)

    traded = BOT.place(symbol, tf_name, side, sl, tp, tick,
                       comment=f"CRT2 {tf_name} {int(c1['time'])}", to_close=running,
                       signal_time=int(c1["time"]), be_trigger=be)
    if traded:
        _traded.add((symbol, tf_name, c2_time))
        # Position ticket = opening order ticket; cache its BE level
        for pos in BOT.open_positions(symbol):
            if pos.comment == f"CRT2 {tf_name} {int(c1['time'])}":
                _be_level[pos.ticket] = be

        # Forget traded C2s older than a day
        cutoff = c2_time - 24 * 3600
        for k in [k for k in _traded if k[2] < cutoff]:
            _traded.discard(k)
    return traded


# ---------------------------------------------------------------------------
# Start / control
# ---------------------------------------------------------------------------

def start_candle_two_bot():
    default_symbols = os.environ.get("CRT_SYMBOLS", DEFAULT_SYMBOLS)
    settings.update({
        "symbols":           [s.strip() for s in
                              os.environ.get("CRT2_SYMBOLS", default_symbols).split(",")
                              if s.strip()],
        "lot":               float(os.environ.get("CRT2_LOT", "0.01")),
        "deviation":         int(os.environ.get("CRT2_DEVIATION", "20")),
        "magic_base":        int(os.environ.get("CRT2_MAGIC_BASE", "780000")),
        "poll_interval":     float(os.environ.get("CRT2_POLL_INTERVAL", "2")),
        "min_rr":            float(os.environ.get("CRT2_MIN_RR", "0")),
        "sl_buffer_spreads": float(os.environ.get("CRT2_SL_BUFFER_SPREADS", "0")),
        "min_sl_spreads":    float(os.environ.get("CRT2_MIN_SL_SPREADS", "3")),
        "be_trigger":        float(os.environ.get("CRT2_BE_TRIGGER", "0.45")),
    })
    BOT.start(_check, _be_trigger,
              enabled_by_default=os.environ.get("CRT2_ENABLED", "false").lower() == "true")


def enable():
    return BOT.try_enable()


def disable():
    BOT.enabled.clear()


def is_enabled():
    return BOT.enabled.is_set()
