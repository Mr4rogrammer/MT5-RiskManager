"""
EJPC3 — CRT + CISD multi-timeframe model (port of the TradingView "CRT + CISD Multi-Timeframe"
indicator), made mechanical. Its own bot; the other bots are unchanged.

Entry timeframe → higher timeframe (HTF), fixed pairs:
    M1 → M15   M5 → H1   M15 → H4   H1 → D1   H4 → W1
The HTF candle running now is C3, the one that just closed is C2, the one before is C1.

  1. CRT      C2 sweeps ONE side of C1 and closes back inside C1's range. If C2 took
              both sides (or the opposite side before its CISD) there is no model.
  2. C2 CISD  on the entry timeframe, inside C2 after the sweep: a close through the open
              of the candle series that delivered the sweep extreme (the latest CISD from
              the most extreme sweep). Required.
  3. C3 CISD  inside C3: a candle closes through the open of a fresh opposite run that
              began after the C2 CISD → enter at market (first such CISD per C3 only).
              Dead if C3 trades beyond C2's extreme before that.
  4. SL       beyond the C3 CISD leg's swing (+ spread for SELL).
  5. TP       STD -2: C2 CISD level ± 2 × |C2 CISD level − C2 sweep extreme|.
  6. BE       at BE_TRIGGER (default 50 %) of entry→TP: half closed, SL to entry ± commission.

Size comes from the shared risk guard (BOTS_RISK_PCT, default 0.5 % per trade). Shared rules
from bot_common: HTF-first (W1 → M15), one trade per symbol, an opposite higher-HTF setup
closes a running trade, plus the min-SL, commission + spread, RR and stops-level filters.
Not mechanised: SMT, T-Spot.

Settings (env vars):
  EJPC3_ENABLED          start trading on boot (default false)
  EJPC3_SYMBOLS          (default the six major forex pairs, no gold)
  EJPC3_TFS              HTFs to trade (default W1,D1,H4,H1,M15)
  EJPC3_STD_MULT         STD multiple for TP (default 2)
  EJPC3_MAGIC_BASE       (default 870000)
  EJPC3_LOT EJPC3_DEVIATION EJPC3_POLL_INTERVAL (5) EJPC3_MAX_SIGNAL_AGE (120) EJPC3_MIN_RR (0)
  EJPC3_SL_BUFFER_SPREADS (0) EJPC3_MIN_SL_SPREADS (3) EJPC3_BE_TRIGGER (0.5)
  EJPC3_PARTIAL_PCT (50) EJPC3_TRAIL_R (0)
"""

import os

import MetaTrader5 as mt5

from bot_common import Bot, sl_beyond_sweep

DEFAULT_SYMBOLS = "EURUSD,GBPUSD,AUDUSD,USDCHF,NZDUSD,USDCAD"

# HTF-first order: name -> (HTF timeframe, seconds, entry timeframe, entry seconds)
PAIRS = {
    "W1":  (mt5.TIMEFRAME_W1,  7 * 86400, mt5.TIMEFRAME_H4,  4 * 3600),
    "D1":  (mt5.TIMEFRAME_D1,  86400,     mt5.TIMEFRAME_H1,  3600),
    "H4":  (mt5.TIMEFRAME_H4,  4 * 3600,  mt5.TIMEFRAME_M15, 900),
    "H1":  (mt5.TIMEFRAME_H1,  3600,      mt5.TIMEFRAME_M5,  300),
    "M15": (mt5.TIMEFRAME_M15, 900,       mt5.TIMEFRAME_M1,  60),
}
LOOKBACK = 100          # entry candles before C2, for the delivery series
SERIES_CAP = 200        # same cap as the indicator


class EjpBot(Bot):
    """A Bot on the W1 / D1 / H4 / H1 / M15 HTFs; the magic number carries the HTF."""

    def magic(self, tf_name):
        return self.settings["magic_base"] + PAIRS[tf_name][1] // 60

    def position_tf(self, pos):
        minutes = pos.magic - self.settings["magic_base"]
        return next((tf for tf, p in PAIRS.items() if p[1] // 60 == minutes), None)

    def scan_order(self, running):
        tfs = [tf for tf in PAIRS if tf in self.settings["tfs"]]
        if not running:
            return tfs
        rank = {tf: i for i, tf in enumerate(PAIRS)}
        top = min(rank.get(self.position_tf(p), -1) for p in running)
        return [tf for tf in tfs if rank[tf] < top]


BOT = EjpBot(name="ejpc3", label="EJPC3", prefix="EJPC3 ", title="EJPC3 (CRT + CISD)",
             description="CRT on the HTF (M15/H1/H4/D1/W1) with a C2 CISD, then a C3 CISD on "
                         "the entry timeframe to enter. SL beyond the C3 CISD swing, TP at "
                         "STD -2, half closed at break-even.")
settings = BOT.settings

_done = set()       # (symbol, htf, c3_time) already traded or filtered


def _series_back(cs, i, bearish):
    """Bars back to the first candle of the down-close (bearish) / up-close series feeding i."""
    def hit(c):
        return c[4] < c[1] if bearish else c[4] > c[1]
    k = 0 if hit(cs[i]) else 1
    start = None
    while k < SERIES_CAP and i - k >= 0 and hit(cs[i - k]):
        start = k
        k += 1
    return start


def c2_model(cs, a, b, c1_high, c1_low):
    """
    Replay C2's entry candles cs[a:b]. Returns (dir, both, cisd) where dir is 1 (C1 low
    swept → BUY) or -1 (C1 high swept → SELL) and cisd is the latest C2 CISD or None.
    """
    d, both, cisd = 0, False, None
    ext = ext_x = lvl = lvl_x = None
    for i in range(a, b):
        _, _, h, l, c = cs[i]
        swept_high, swept_low = h > c1_high, l < c1_low
        if d == 0:
            if swept_high and swept_low:
                both = True
            elif swept_high or swept_low:
                d = -1 if swept_high else 1
        elif cisd is None and (swept_low if d == -1 else swept_high):
            both = True
        if both:
            break
        if d == 0:
            continue
        if ext is None or (l < ext if d == 1 else h > ext):
            ext, ext_x = (l if d == 1 else h), i
            back = _series_back(cs, i, d == 1)
            if back is not None:
                lvl, lvl_x = cs[i - back][1], i - back
        if lvl is not None and (c > lvl if d == 1 else c < lvl):
            cisd = {"bar": i, "level": lvl, "x": lvl_x, "ext": ext}
            lvl = None
    return d, both, cisd


def c3_cisd(cs, c3_start, d, c2_high, c2_low, c2_cisd_bar):
    """
    The first C3 CISD in the model direction, or None (none yet, or C2's extreme was broken).
    Runs the indicator's CISD engine over every candle; only C3 candles can confirm.
    Returns (index, swing, level).
    """
    bull_open = bear_open = bull_bar = bear_bar = None
    bull_high = bull_hx = bear_low = bear_lx = None
    prev_up = prev_dn = False
    for i, (_, o, h, l, c) in enumerate(cs):
        up, dn = c > o, c < o
        bear_cisd = dn and bull_open is not None and c < bull_open
        bull_cisd = up and bear_open is not None and c > bear_open
        lvl_bear, x_bear = bull_open, bull_bar
        lvl_bull, x_bull = bear_open, bear_bar

        if bull_high is None or h > bull_high:
            bull_high = h
        if bull_hx is None or h >= bull_high:
            bull_hx = i
        if bear_low is None or l < bear_low:
            bear_low = l
        if bear_lx is None or l <= bear_low:
            bear_lx = i
        swing_bear, swing_bull = bull_high, bear_low

        if bear_cisd:
            bull_open = None
        if bull_cisd:
            bear_open = None
        if up and not prev_up:
            bull_open, bull_bar, bull_high, bull_hx = o, i, h, i
        if dn and not prev_dn:
            bear_open, bear_bar, bear_low, bear_lx = o, i, l, i
        prev_up, prev_dn = up, dn

        if i < c3_start:
            continue
        if (d == -1 and h > c2_high) or (d == 1 and l < c2_low):
            return None
        if d == 1 and bull_cisd and x_bull is not None and x_bull > c2_cisd_bar:
            return i, swing_bull, lvl_bull
        if d == -1 and bear_cisd and x_bear is not None and x_bear > c2_cisd_bar:
            return i, swing_bear, lvl_bear
    return None


def _check(symbol, tf_name, running):
    s = settings
    htf, htf_seconds, ltf, ltf_seconds = PAIRS[tf_name]
    rates = mt5.copy_rates_from_pos(symbol, htf, 0, 3)          # C1, C2, C3 (running)
    if rates is None or len(rates) < 3:
        return False
    c1, c2, c3 = rates[0], rates[1], rates[2]
    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    c2_high, c2_close = float(c2["high"]), float(c2["close"])
    c2_low = float(c2["low"])
    c2_time, c3_time = int(c2["time"]), int(c3["time"])
    if not c1_low <= c2_close <= c1_high:
        return False                                  # C2 must close back inside C1

    key = (symbol, tf_name, c3_time)
    if key in _done:
        return False

    count = 2 * htf_seconds // ltf_seconds + LOOKBACK
    raw = mt5.copy_rates_from_pos(symbol, ltf, 1, count)        # closed entry candles
    if raw is None or len(raw) == 0:
        return False
    cs = [(int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
          for r in raw]
    a = next((i for i, c in enumerate(cs) if c[0] >= c2_time), None)
    b = next((i for i, c in enumerate(cs) if c[0] >= c3_time), None)
    if a is None or b is None:
        return False                                  # no closed candle in C3 yet

    d, both, cisd = c2_model(cs, a, b, c1_high, c1_low)
    if d == 0 or both or cisd is None:
        return False
    found = c3_cisd(cs, b, d, c2_high, c2_low, cisd["bar"])
    if found is None:
        return False
    idx, swing, level = found

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
    _done.add(key)
    for k in [k for k in _done if k[2] < c3_time - 14 * 86400]:
        _done.discard(k)
    if tick.time - (cs[idx][0] + ltf_seconds) > s["max_signal_age"]:
        return False                                  # a CISD the bot didn't see in time

    side = "BUY" if d == 1 else "SELL"
    leg = abs(cisd["level"] - cisd["ext"])
    if leg <= 0:
        return False
    tp = cisd["level"] + d * s["std_mult"] * leg
    sl = sl_beyond_sweep(side, swing, tick.ask - tick.bid, s["sl_buffer_spreads"])
    entry = tick.ask if side == "BUY" else tick.bid

    BOT.record(symbol, tf_name, "signal", side=side, c1_high=c1_high, c1_low=c1_low,
               c2_cisd_level=cisd["level"], c2_extreme=cisd["ext"], c3_cisd_level=level,
               c3_swing=swing, sl=sl, tp=tp)
    return BOT.place(symbol, tf_name, side, sl, tp, tick,
                     comment=f"EJPC3 {tf_name} {c3_time}", to_close=running,
                     signal_time=c3_time,
                     be_trigger=entry + (tp - entry) * s["be_trigger"])


def _be_trigger(pos):
    if pos.tp == 0:
        return None
    return pos.price_open + (pos.tp - pos.price_open) * settings["be_trigger"]


def start_ejpc3_bot():
    env = lambda name, default: os.environ.get(f"EJPC3_{name}", default)
    tfs = [t.strip().upper() for t in env("TFS", ",".join(PAIRS)).split(",")
           if t.strip().upper() in PAIRS]
    settings.update({
        "symbols":           [x.strip() for x in env("SYMBOLS", DEFAULT_SYMBOLS).split(",")
                              if x.strip()],
        "tfs":               tfs or list(PAIRS),
        "std_mult":          float(env("STD_MULT", "2")),
        "lot":               float(env("LOT", "0.01")),
        "deviation":         int(env("DEVIATION", "20")),
        "magic_base":        int(env("MAGIC_BASE", "870000")),
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
