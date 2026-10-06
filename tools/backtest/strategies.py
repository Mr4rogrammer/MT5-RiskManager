"""
CRT-family signal generators. Each returns a list of signal dicts for engine.simulate():
  i (entry M1 index, filled at that minute's open), tf, side, entry, sl, tp, spread, be
  optional: trail (trailing distance), exit_i (exit at that minute's open), max_minutes

crt3 / crt2 / daily_crt(confirm_tf="M30", trend="against") reproduce the live bots
crt_bot.py / candle_two_bot.py / daily_sweep_bot.py. The other options are filters
that were tested (see README).
"""
import numpy as np

from engine import TFS


def _entry(m, k, side):
    d = m.d
    spread = d["ao"][k] - d["bo"][k]
    return (d["ao"][k] if side == "BUY" else d["bo"][k]), spread


def _sl(side, extreme, spread, buffer_spreads):
    """Same as bot_common.sl_beyond_sweep."""
    buf = spread * buffer_spreads
    return extreme + spread + buf if side == "SELL" else extreme - buf


def atr(b, n=14):
    h, l, c = b["h"], b["l"], b["c"]
    prev = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - prev), abs(l - prev)))
    out = np.empty_like(tr)
    out[0] = tr[0]
    a = 1.0 / n
    for i in range(1, len(tr)):
        out[i] = out[i - 1] + a * (tr[i] - out[i - 1])
    return out


def _htf_context(m, sma_days=20):
    """
    Per M1 index, from COMPLETED broker days only (no look-ahead): previous day's
    high/low/close, today's open, and the daily trend = previous close vs the SMA of
    the previous `sma_days` closes (+1 up, -1 down, 0 not enough history).
    """
    key = ("ctx", sma_days)
    if key in m.__dict__:
        return m.__dict__[key]
    D = m.bars["D1"]
    day_idx = np.repeat(np.arange(len(D["start"])), D["end"] - D["start"])
    prev = np.maximum(day_idx - 1, 0)
    closes = D["c"]
    csum = np.r_[0, np.cumsum(closes)]
    j = np.arange(len(closes))
    sma = np.where(j + 1 >= sma_days, (csum[j + 1] - csum[np.maximum(j + 1 - sma_days, 0)]) / sma_days, np.nan)
    day_trend = np.where(np.isnan(sma), 0, np.sign(closes - sma))   # trend known at the END of day j
    ctx = {
        "pdh": D["h"][prev], "pdl": D["l"][prev], "pdc": D["c"][prev],
        "dopen": D["o"][day_idx], "valid": day_idx > 0,
        "trend": np.where(day_idx > 0, day_trend[prev], 0),
    }
    m.__dict__[key] = ctx
    return ctx


def crt3(m, tfs=tuple(TFS), buffer_spreads=0.0, be_frac=0.47, tp_frac=1.0,
         hours=None, min_range_atr=0.0, min_reject=0.0, bias=None, sweep_level=None):
    """
    3-candle CRT (crt_bot.py). Options for candidate variants:
      hours          set of UTC entry hours allowed (session filter)
      min_range_atr  C1 range ≥ k × ATR(14) of that timeframe
      min_reject     C2 must close at least this fraction back inside C1 from the swept side
      tp_frac        TP at this fraction of the way to C1's opposite side (1.0 = live bot)
      bias           "day_open": BUY only below the broker day's open, SELL only above
                     (buy discount / sell premium); "pdc": vs previous day close
      sweep_level    "pd": the C2 sweep must also take out the previous day's high/low
    """
    ctx = _htf_context(m) if (bias or sweep_level) else None
    out = []
    for tf in tfs:
        b = m.bars[tf]
        h, l, c, start, bucket = b["h"], b["l"], b["c"], b["start"], b["bucket"]
        A = atr(b) if min_range_atr else None
        n = len(h)
        c1h, c1l = h[:-2], l[:-2]
        c2h, c2l, c2c = h[1:-1], l[1:-1], c[1:-1]
        inside = (c2c > c1l) & (c2c < c1h)
        up, dn = c2h > c1h, c2l < c1l
        ok = inside & (up ^ dn)
        consecutive = (bucket[2:] == bucket[1:-1] + 1)        # C3 opens right after C2
        ok &= consecutive
        for j in np.flatnonzero(ok):
            i1, i2 = j, j + 1                                  # C1, C2 bar indices
            rng = c1h[j] - c1l[j]
            if rng <= 0:
                continue
            if A is not None and rng < min_range_atr * A[i1]:
                continue
            side = "SELL" if up[j] else "BUY"
            if min_reject:
                back = (c1h[j] - c2c[j]) if side == "SELL" else (c2c[j] - c1l[j])
                if back / rng < min_reject:
                    continue
            k = start[j + 2]                                   # C3 first minute
            hour = (m.t[k] // 3600) % 24
            if hours is not None and hour not in hours:
                continue
            entry, spread = _entry(m, k, side)
            if ctx is not None:
                if not ctx["valid"][k]:
                    continue
                if bias == "day_open":
                    ref = ctx["dopen"][k]
                    if (side == "BUY" and entry > ref) or (side == "SELL" and entry < ref):
                        continue
                elif bias == "trend":
                    tr = ctx["trend"][k]
                    if tr == 0 or (side == "BUY") != (tr > 0):
                        continue
                elif bias == "pdc":
                    ref = ctx["pdc"][k]
                    if (side == "BUY" and entry > ref) or (side == "SELL" and entry < ref):
                        continue
                if sweep_level == "pd":
                    if side == "SELL" and not (c2h[j] > ctx["pdh"][k]):
                        continue
                    if side == "BUY" and not (c2l[j] < ctx["pdl"][k]):
                        continue
            extreme = c2h[j] if side == "SELL" else c2l[j]
            full_tp = c1l[j] if side == "SELL" else c1h[j]
            tp = entry + (full_tp - entry) * tp_frac
            sl = _sl(side, extreme, spread, buffer_spreads)
            be = entry + (tp - entry) * be_frac if be_frac else None
            out.append({"i": int(k), "tf": tf, "side": side, "entry": entry, "sl": sl,
                        "tp": tp, "spread": spread, "be": be})
    return out


def crt2(m, tfs=tuple(TFS), buffer_spreads=0.0, be_frac=0.45, hours=None):
    """
    2-candle CRT (candle_two_bot.py): C2 opens inside C1, sweeps one side, then the bid
    crosses back through C2's open → enter at the next minute's open.
    BE trigger = 45 % into C1 from the swept side.
    """
    d = m.d
    out = []
    for tf in tfs:
        b = m.bars[tf]
        for j in range(1, len(b["o"])):
            if b["bucket"][j] != b["bucket"][j - 1] + 1:
                continue                                       # C1 must be the previous bar
            c1h, c1l, c2o = b["h"][j - 1], b["l"][j - 1], b["o"][j]
            if not (c1l < c2o < c1h):
                continue
            s, e = b["start"][j], b["end"][j]
            bh, bl, bc = d["bh"][s:e], d["bl"][s:e], d["bc"][s:e]
            # first minute each side gets swept
            k_lo, k_hi = np.argmax(bl < c1l), np.argmax(bh > c1h)
            lo_ok, hi_ok = bl[k_lo] < c1l, bh[k_hi] > c1h
            cand = []
            if lo_ok:
                cross = np.flatnonzero(bc[k_lo:] > c2o)
                if len(cross):
                    kc = k_lo + cross[0]
                    if not (hi_ok and k_hi <= kc):             # other side not swept before the cross
                        cand.append((kc, "BUY"))
            if hi_ok:
                cross = np.flatnonzero(bc[k_hi:] < c2o)
                if len(cross):
                    kc = k_hi + cross[0]
                    if not (lo_ok and k_lo <= kc):
                        cand.append((kc, "SELL"))
            if not cand:
                continue
            kc, side = min(cand)
            k = s + kc + 1                                     # next minute's open
            if k >= len(m.t) or k >= e:
                continue                                       # C2 closed — the live bot would not trade it
            if hours is not None and (m.t[k] // 3600) % 24 not in hours:
                continue
            entry, spread = _entry(m, k, side)
            extreme = bl[: kc + 1].min() if side == "BUY" else bh[: kc + 1].max()
            tp = c1h if side == "BUY" else c1l
            sl = _sl(side, extreme, spread, buffer_spreads)
            depth = (c1h - c1l) * be_frac
            be = (c1l + depth if side == "BUY" else c1h - depth) if be_frac else None
            # the live bot's BE trigger may already be behind entry; engine handles it
            out.append({"i": int(k), "tf": tf, "side": side, "entry": entry, "sl": sl,
                        "tp": tp, "spread": spread, "be": be})
    return out


def _range_fade(m, groups, confirm_tf, side_ok, tp_frac, buffer_spreads, be_frac, label,
                entry_end=None, max_trades=1):
    """
    Shared logic for "sweep a reference range, close back inside, fade it":
      groups: iterable of (ref_high, ref_low, first_m1, last_m1_exclusive, end_hour_cutoff_m1)
      A confirm_tf candle that closes back inside after price swept exactly one side
      → enter at the next minute. SL beyond the extreme since the window opened, TP toward
      the opposite side (tp_frac of the way; 1.0 = opposite side, 0.5 = middle).
    """
    d, b = m.d, m.bars[confirm_tf]
    ends = b["end"]
    out = []
    for ref_h, ref_l, s, e, cutoff in groups:
        if e - s < 2 or ref_h <= ref_l:
            continue
        # confirm candles that close inside the window
        lo_i = np.searchsorted(ends, s + 1)
        hi_i = np.searchsorted(ends, min(e, cutoff), side="right")
        taken = 0
        for ci in range(lo_i, hi_i):
            ce = ends[ci]                                      # exclusive end of the candle
            if ce >= len(m.t) or ce > e:
                break
            seg_h, seg_l = d["bh"][s:ce].max(), d["bl"][s:ce].min()
            close = d["bc"][ce - 1]
            up, dn = seg_h > ref_h, seg_l < ref_l
            if up == dn:
                if up:
                    break                                      # both sides swept — no clear story
                continue
            side = "SELL" if up else "BUY"
            if not (ref_l < close < ref_h) or not side_ok(side, ce):
                continue
            k = ce
            entry, spread = _entry(m, k, side)
            extreme = seg_h if side == "SELL" else seg_l
            target = ref_l if side == "SELL" else ref_h
            tp = entry + (target - entry) * tp_frac
            sl = _sl(side, extreme, spread, buffer_spreads)
            be = entry + (tp - entry) * be_frac if be_frac else None
            out.append({"i": int(k), "tf": label, "side": side, "entry": entry, "sl": sl,
                        "tp": tp, "spread": spread, "be": be})
            taken += 1
            if taken >= max_trades:
                break
    return out


def _trend_ok(m, use, sma_days=20):
    """use: False (no filter), True / "with" (trade with the daily trend), "against"."""
    if not use:
        return lambda side, k: True
    tr = _htf_context(m, sma_days)["trend"]
    want = 1 if use in (True, "with") else -1
    return lambda side, k: tr[k] != 0 and ((1 if side == "BUY" else -1) * tr[k]) == want


def daily_crt(m, confirm_tf="H1", tp_frac=1.0, buffer_spreads=0.0, be_frac=0.0,
              entry_until_hour=24, trend=False, sma_days=20, lookback=1):
    """
    Idea A — daily CRT: C1 = previous broker day, C2 = today. When today has swept exactly
    one side of yesterday's range and a confirm_tf candle closes back inside, fade it.
    entry_until_hour: no new entries after this broker-time hour.
    """
    D = m.bars["D1"]
    groups = []
    for j in range(lookback, len(D["o"])):
        s, e = D["start"][j], D["end"][j]
        day0 = m.bt[s] // 86400 * 86400
        cutoff = s + np.searchsorted(m.bt[s:e], day0 + entry_until_hour * 3600)
        # reference range = the previous `lookback` completed days (1 = yesterday, 20 = turtle soup)
        groups.append((D["h"][j - lookback:j].max(), D["l"][j - lookback:j].min(), s, e, cutoff))
    return _range_fade(m, groups, confirm_tf, _trend_ok(m, trend, sma_days), tp_frac, buffer_spreads,
                       be_frac, "H4")


def asia_sweep(m, confirm_tf="M15", asia=(0, 6), window=(6, 11), tp_frac=1.0,
               buffer_spreads=0.0, be_frac=0.0, trend=False):
    """
    Idea B — Asian range sweep: range = UTC hours asia[0]..asia[1]; during `window`
    (UTC hours), a sweep of one side then a confirm_tf close back inside → fade it.
    The trade itself may run past the window (until SL/TP).
    """
    t = m.t
    days = t // 86400
    starts = np.flatnonzero(np.r_[True, days[1:] != days[:-1]])
    ends = np.r_[starts[1:], len(t)]
    groups = []
    for s, e in zip(starts, ends):
        day0 = days[s] * 86400
        a0, a1 = s + np.searchsorted(t[s:e], day0 + asia[0] * 3600), s + np.searchsorted(t[s:e], day0 + asia[1] * 3600)
        w0, w1 = s + np.searchsorted(t[s:e], day0 + window[0] * 3600), s + np.searchsorted(t[s:e], day0 + window[1] * 3600)
        if a1 - a0 < 60 or w1 - w0 < 30:
            continue
        ah, al = m.d["bh"][a0:a1].max(), m.d["bl"][a0:a1].min()
        groups.append((ah, al, w0, e, w1))
    return _range_fade(m, groups, confirm_tf, _trend_ok(m, trend), tp_frac, buffer_spreads,
                       be_frac, "H1")
