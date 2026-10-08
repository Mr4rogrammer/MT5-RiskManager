"""
Candle 2 — TTrades fractal model, made mechanical. Its own bot; the CRT bots are unchanged.

Higher timeframe (HTF): the Daily and 4H candle.  C1 = last closed HTF candle,
C2 = the HTF candle now forming — the "reversal candle".

  1. Sweep   C2 trades beyond C1's high (→ SELL setup) or low (→ BUY setup). C2 must
             have opened inside C1; a C2 that swept both sides is skipped.
  2. CISD    on the entry timeframe (D1 → M15, H4 → M5), a change in the state of
             delivery: the run of candles that pushed into the sweep extreme is broken.
               SELL: the consecutive up-close candles that made the high; a candle
                     CLOSES below the open of the first of them  → enter at market
               BUY : mirror — the down-close run into the low, a close above its open
             Only the first close through that level counts, entered within
             MAX_SIGNAL_AGE seconds of that candle's close.
  3. SL      beyond the sweep extreme (the protected high/low), + spread for SELL.
  4. TP by wick size — the article's "fuel gauge":
               wick = C2's run beyond its open to the extreme, ÷ C1's range
               small (≤ WICK_SMALL, default 0.5) → range expansion: TP = C1's other side
               large                              → back to the open:  TP = C2's open
  5. BE      at BE_TRIGGER (default 50 %) of entry→TP: SL to entry ± commission and
             PARTIAL_PCT (default 50 %) closed, as the other bots.

One setup per C2 and sweep extreme: once traded or filtered, the same sweep isn't taken
again; a new, further sweep on the same C2 can give a new setup. Shared rules from
bot_common: HTF-first (D1 before H4), one trade per symbol, an opposite D1 setup closes a
running H4 trade, plus the min-SL, commission + spread, RR and stops-level filters.
Not mechanised (yet): session-based entry timeframes, SMT with correlated pairs.

Settings (env vars):
  C2F_ENABLED          start trading on boot (default false)
  C2F_SYMBOLS          (default CRT_SYMBOLS)
  C2F_TFS              HTFs to trade (default D1,H4)
  C2F_ENTRY_D1         entry timeframe for D1 (default M15; M5 / M15 / M30 / H1)
  C2F_ENTRY_H4         entry timeframe for H4 (default M5)
  C2F_WICK_SMALL       wick ÷ C1 range at or below which TP is C1's other side (default 0.5)
  C2F_MAGIC_BASE       (default 850000)
  C2F_LOT C2F_DEVIATION C2F_POLL_INTERVAL (5) C2F_MAX_SIGNAL_AGE (120) C2F_MIN_RR (0)
  C2F_SL_BUFFER_SPREADS (0) C2F_MIN_SL_SPREADS (3) C2F_BE_TRIGGER (0.5)
  C2F_PARTIAL_PCT (50) C2F_TRAIL_R (0)
"""

import os

import MetaTrader5 as mt5

from bot_common import Bot, DEFAULT_SYMBOLS, sl_beyond_sweep

HTF = {                                    # HTF-first order
    "D1": (mt5.TIMEFRAME_D1, 86400),
    "H4": (mt5.TIMEFRAME_H4, 4 * 3600),
}
ENTRY_TF = {
    "M5":  (mt5.TIMEFRAME_M5, 300),
    "M15": (mt5.TIMEFRAME_M15, 900),
    "M30": (mt5.TIMEFRAME_M30, 1800),
    "H1":  (mt5.TIMEFRAME_H1, 3600),
}


class FractalBot(Bot):
    """A Bot on the D1 / H4 timeframes instead of bot_common's H4 → M15 set."""

    def magic(self, tf_name):
        return self.settings["magic_base"] + HTF[tf_name][1] // 60

    def position_tf(self, pos):
        minutes = pos.magic - self.settings["magic_base"]
        return next((tf for tf, (_, sec) in HTF.items() if sec // 60 == minutes), None)

    def scan_order(self, running):
        tfs = [tf for tf in HTF if tf in self.settings["tfs"]]
        if not running:
            return tfs
        rank = {tf: i for i, tf in enumerate(HTF)}
        top = min(rank.get(self.position_tf(p), -1) for p in running)
        return [tf for tf in tfs if rank[tf] < top]


BOT = FractalBot(name="c2f", label="C2F", prefix="C2F ", title="Candle 2 (fractal)",
                 description="TTrades candle 2: D1/H4 candle sweeps the previous high/low, "
                             "CISD on M15/M5 to enter. TP by wick size: small → previous "
                             "candle's other side, large → back to the candle's open.")
settings = BOT.settings

_done = set()       # (symbol, htf, c2_time, side, extreme) already traded or filtered


def cisd(candles, side, c1_level):
    """
    The CISD on the entry candles of C2 (closed, oldest → newest), or None.

    SELL: the highest candle must be above C1's high. The run of up-close candles ending
    at it (or just before it, if it closed down) started at `level` = that run's first
    open. CISD = the newest candle is the FIRST to close below `level` after the high.
    Returns (extreme, level).
    """
    if len(candles) < 2:
        return None
    sell = side == "SELL"
    key = "high" if sell else "low"
    vals = [float(c[key]) for c in candles]
    i = vals.index(max(vals)) if sell else vals.index(min(vals))
    extreme = vals[i]
    if (sell and extreme <= c1_level) or (not sell and extreme >= c1_level):
        return None
    if i == len(candles) - 1:
        return None                                  # the extreme candle itself: no break yet

    def pushes(c):                                   # candle delivering INTO the extreme
        return c["close"] > c["open"] if sell else c["close"] < c["open"]
    j = i if pushes(candles[i]) else i - 1
    if j < 0 or not pushes(candles[j]):
        level = float(candles[i]["open"])
    else:
        while j > 0 and pushes(candles[j - 1]):
            j -= 1
        level = float(candles[j]["open"])

    def through(c):
        return c["close"] < level if sell else c["close"] > level
    after = candles[i + 1:]
    if not through(after[-1]) or any(through(c) for c in after[:-1]):
        return None                                  # no break yet, or not the first one
    return extreme, level


def _check(symbol, tf_name, running):
    s = settings
    htf, htf_seconds = HTF[tf_name]
    rates = mt5.copy_rates_from_pos(symbol, htf, 0, 2)
    if rates is None or len(rates) < 2:
        return False
    c1, c2 = rates[0], rates[1]
    c2_time = int(c2["time"])
    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    c2_open = float(c2["open"])
    if not (c1_low < c2_open < c1_high):
        return False
    swept_high = float(c2["high"]) > c1_high
    swept_low = float(c2["low"]) < c1_low
    if swept_high == swept_low:
        return False                                 # no sweep yet, or both sides
    side = "SELL" if swept_high else "BUY"

    entry_name = s["entry_tf"][tf_name]
    ltf, ltf_seconds = ENTRY_TF[entry_name]
    count = htf_seconds // ltf_seconds + 2
    candles = mt5.copy_rates_from_pos(symbol, ltf, 1, count)   # closed entry candles
    if candles is None or len(candles) == 0:
        return False
    candles = [c for c in candles if int(c["time"]) >= c2_time]
    found = cisd(candles, side, c1_high if side == "SELL" else c1_low)
    if found is None:
        return False
    extreme, level = found

    key = (symbol, tf_name, c2_time, side, extreme)
    if key in _done:
        return False
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    if tick.time - (int(candles[-1]["time"]) + ltf_seconds) > s["max_signal_age"]:
        _done.add(key)                               # a CISD the bot didn't see in time
        return False
    _done.add(key)
    for k in [k for k in _done if k[2] < c2_time - 7 * 86400]:
        _done.discard(k)

    c1_range = c1_high - c1_low
    wick = (extreme - c2_open) if side == "SELL" else (c2_open - extreme)
    ratio = wick / c1_range if c1_range > 0 else 0.0
    small = ratio <= s["wick_small"]
    tp = (c1_low if side == "SELL" else c1_high) if small else c2_open

    BOT.record(symbol, tf_name, "signal", side=side, entry_tf=entry_name,
               c1_high=c1_high, c1_low=c1_low, c2_open=c2_open, extreme=extreme,
               cisd_level=level, wick_ratio=round(ratio, 2),
               wick="small" if small else "large", tp=tp)

    # SL beyond C2's true extreme — the forming entry candle may have gone further
    beyond = max(extreme, float(c2["high"])) if side == "SELL" else min(extreme, float(c2["low"]))
    sl = sl_beyond_sweep(side, beyond, tick.ask - tick.bid, s["sl_buffer_spreads"])
    entry = tick.ask if side == "BUY" else tick.bid
    return BOT.place(symbol, tf_name, side, sl, tp, tick,
                     comment=f"C2F {tf_name} {c2_time}", to_close=running,
                     signal_time=c2_time,
                     be_trigger=entry + (tp - entry) * s["be_trigger"])


def _be_trigger(pos):
    if pos.tp == 0:
        return None
    return pos.price_open + (pos.tp - pos.price_open) * settings["be_trigger"]


def start_candle2_fractal_bot():
    env = lambda name, default: os.environ.get(f"C2F_{name}", default)
    tfs = [t.strip().upper() for t in env("TFS", "D1,H4").split(",") if t.strip().upper() in HTF]
    entry = {"D1": env("ENTRY_D1", "M15").upper(), "H4": env("ENTRY_H4", "M5").upper()}
    entry = {k: (v if v in ENTRY_TF else ("M15" if k == "D1" else "M5")) for k, v in entry.items()}
    settings.update({
        "symbols":           [x.strip() for x in
                              env("SYMBOLS", os.environ.get("CRT_SYMBOLS", DEFAULT_SYMBOLS)).split(",")
                              if x.strip()],
        "tfs":               tfs or list(HTF),
        "entry_tf":          entry,
        "wick_small":        float(env("WICK_SMALL", "0.5")),
        "lot":               float(env("LOT", "0.01")),
        "deviation":         int(env("DEVIATION", "20")),
        "magic_base":        int(env("MAGIC_BASE", "850000")),
        "poll_interval":     float(env("POLL_INTERVAL", "5")),
        "max_signal_age":    int(env("MAX_SIGNAL_AGE", "120")),
        "min_rr":            float(env("MIN_RR", "0")),
        "sl_buffer_spreads": float(env("SL_BUFFER_SPREADS", "0")),
        "min_sl_spreads":    float(env("MIN_SL_SPREADS", "3")),
        "be_trigger":        float(env("BE_TRIGGER", "0.5")),
        "partial_pct":       float(env("PARTIAL_PCT", "50")),
        "trail_r":           float(env("TRAIL_R", "0")),
    })
    BOT.start(_check, _be_trigger,
              enabled_by_default=env("ENABLED", "false").lower() == "true")
