"""
Non-CRT strategy families (trend, session, mean reversion), each with a market rationale.
Every decision uses only data available at that moment; exits that depend on
indicators are converted to an M1 index (exit_i) computed from bars that have closed.
"""
import numpy as np

from strategies import _entry, atr, _htf_context


def _sma(x, n):
    c = np.r_[0, np.cumsum(x)]
    out = np.full(len(x), np.nan)
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _rsi(close, n=2):
    diff = np.diff(close, prepend=close[0])
    up, dn = np.clip(diff, 0, None), np.clip(-diff, 0, None)
    au, ad = np.zeros_like(close), np.zeros_like(close)
    a = 1.0 / n
    for i in range(1, len(close)):
        au[i] = au[i - 1] + a * (up[i] - au[i - 1])
        ad[i] = ad[i - 1] + a * (dn[i] - ad[i - 1])
    rs = np.divide(au, ad, out=np.full_like(au, np.inf), where=ad > 0)
    return 100 - 100 / (1 + rs)


# ---------------------------------------------------------------------------
# 1. Trend following — daily Donchian breakout (time-series momentum)
# ---------------------------------------------------------------------------

def donchian(m, n=20, sl_atr=2.0, trail_atr=3.0, atr_n=20):
    """
    Daily close above the highest high of the previous n days → BUY at the next day's
    open (mirror for SELL). Initial SL sl_atr × ATR, then a trailing stop trail_atr × ATR.
    No profit target: trends are left to run.
    """
    D = m.bars["D1"]
    A = atr(D, atr_n)
    h, l, c = D["h"], D["l"], D["c"]
    out = []
    for j in range(max(n, atr_n), len(c) - 1):
        hh, ll = h[j - n:j].max(), l[j - n:j].min()
        side = "BUY" if c[j] > hh else ("SELL" if c[j] < ll else None)
        if side is None:
            continue
        k = D["start"][j + 1]
        entry, spread = _entry(m, k, side)
        dist = A[j]
        sl = entry - sl_atr * dist if side == "BUY" else entry + sl_atr * dist
        out.append({"i": int(k), "tf": "H4", "side": side, "entry": entry, "sl": sl, "tp": None,
                    "spread": spread, "be": None, "trail": trail_atr * dist,
                    "max_minutes": 60 * 24 * 120})
    return out


# ---------------------------------------------------------------------------
# 2. London breakout — trade WITH the break of the Asian range
# ---------------------------------------------------------------------------

def london_breakout(m, asia=(0, 6), window=(6, 10), confirm_tf="M15", sl_mode="mid",
                    rr=None, exit_hour=20):
    """
    Asia range = UTC asia[0]..asia[1]. During `window`, the first confirm_tf candle that
    closes outside the range → enter in that direction at the next minute.
      sl_mode "mid": SL at the middle of the range; "opposite": the other side
      rr: TP at rr × risk, or None (hold until exit_hour UTC)
    One trade per day.
    """
    t, d = m.t, m.d
    b = m.bars[confirm_tf]
    ends = b["end"]
    days = t // 86400
    starts = np.flatnonzero(np.r_[True, days[1:] != days[:-1]])
    dends = np.r_[starts[1:], len(t)]
    out = []
    for s, e in zip(starts, dends):
        day0 = days[s] * 86400
        idx = lambda hr: s + np.searchsorted(t[s:e], day0 + hr * 3600)
        a0, a1, w0, w1, x = idx(asia[0]), idx(asia[1]), idx(window[0]), idx(window[1]), idx(exit_hour)
        if a1 - a0 < 120 or w1 <= w0 or x <= w1:
            continue
        rh, rl = d["bh"][a0:a1].max(), d["bl"][a0:a1].min()
        lo_i, hi_i = np.searchsorted(ends, w0 + 1), np.searchsorted(ends, w1, side="right")
        for ci in range(lo_i, hi_i):
            ce = ends[ci]
            close = d["bc"][ce - 1]
            side = "BUY" if close > rh else ("SELL" if close < rl else None)
            if side is None:
                continue
            k = ce
            entry, spread = _entry(m, k, side)
            if sl_mode == "mid":
                sl = (rh + rl) / 2
            else:
                sl = rl if side == "BUY" else rh + spread
            risk = abs(entry - sl)
            tp = (entry + rr * risk if side == "BUY" else entry - rr * risk) if rr else None
            out.append({"i": int(k), "tf": "H1", "side": side, "entry": entry, "sl": sl, "tp": tp,
                        "spread": spread, "be": None, "exit_i": int(x)})
            break
    return out


# ---------------------------------------------------------------------------
# 3. Pullback in trend — RSI(2) dip-buying on H4 with a daily trend filter
# ---------------------------------------------------------------------------

def rsi2_pullback(m, tf="H4", rsi_n=2, lo=10, hi=90, trend_days=50, exit_sma=5,
                  sl_atr=3.0, max_bars=10):
    """
    Daily trend up (previous close > SMA(trend_days) of daily closes) and RSI(2) on the
    tf close < lo → BUY at the next bar's open. Exit at the open of the bar after the
    first close above SMA(exit_sma), or after max_bars. Protective SL sl_atr × ATR.
    Mirror for SELL in a downtrend (RSI > hi).
    """
    ctx = _htf_context(m, trend_days) if trend_days else None
    b = m.bars[tf]
    c, start = b["c"], b["start"]
    R, S, A = _rsi(c, rsi_n), _sma(c, exit_sma), atr(b)
    out, j = [], max(rsi_n, exit_sma, 20)
    n = len(c)
    while j < n - 2:
        k = start[j + 1]
        if ctx is None:                                  # no trend filter: pure mean reversion
            side = "BUY" if R[j] < lo else ("SELL" if R[j] > hi else None)
        else:
            tr = ctx["trend"][k]
            side = "BUY" if (tr > 0 and R[j] < lo) else ("SELL" if (tr < 0 and R[j] > hi) else None)
        if side is None:
            j += 1
            continue
        # exit bar: first later close back across the short SMA, or the time stop
        x = None
        for q in range(j + 1, min(j + 1 + max_bars, n - 1)):
            if (side == "BUY" and c[q] > S[q]) or (side == "SELL" and c[q] < S[q]):
                x = q + 1
                break
        x = x if x is not None else min(j + 1 + max_bars, n - 1)
        entry, spread = _entry(m, k, side)
        sl = entry - sl_atr * A[j] if side == "BUY" else entry + sl_atr * A[j]
        out.append({"i": int(k), "tf": tf if tf in ("H4", "H1", "M30", "M15") else "H4",
                    "side": side, "entry": entry, "sl": sl, "tp": None, "spread": spread,
                    "be": None, "exit_i": int(start[x])})
        j = x                                            # no overlapping signals
    return out


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def london_clock(m):
    """Per M1 index: London local hour, minute-of-day, weekday and London calendar day."""
    if "_lon" in m.__dict__:
        return m.__dict__["_lon"]
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    LON = ZoneInfo("Europe/London")
    days = m.t // 86400
    uniq, inv = np.unique(days, return_inverse=True)
    off = np.array([int(datetime.fromtimestamp(int(d) * 86400 + 43200, timezone.utc)
                        .astimezone(LON).utcoffset().total_seconds()) for d in uniq])[inv]
    local = m.t + off
    clk = {"minute": (local % 86400) // 60, "hour": (local % 86400) // 3600,
           "day": local // 86400, "dow": ((local // 86400) + 3) % 7}   # 0 = Monday
    m.__dict__["_lon"] = clk
    return clk


def _at_minute_each_day(m, minute_of_day):
    """M1 index of the first bar at/after `minute_of_day` London time, for each London weekday."""
    clk = london_clock(m)
    day = clk["day"]
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    ends = np.r_[starts[1:], len(day)]
    out = {}
    for s, e in zip(starts, ends):
        if clk["dow"][s] > 4:
            continue
        k = s + np.searchsorted(clk["minute"][s:e], minute_of_day)
        if k < e and clk["minute"][k] - minute_of_day < 10:          # bar exists near that time
            out[int(day[s])] = int(k)
    return out


# ---------------------------------------------------------------------------
# 4. Dollar-flow windows: hold the dollar from London hour a to b
# ---------------------------------------------------------------------------

def usd_window(m, start_hour, end_hour, usd_up=True, sl_pct=1.0):
    """
    Every weekday at start_hour London, take the USD side (usd_up: buy USD) and close at
    end_hour London. Protective SL sl_pct % away. One trade per day.
    """
    starts = _at_minute_each_day(m, start_hour * 60)
    ends = _at_minute_each_day(m, end_hour * 60) if end_hour > start_hour else {}
    usd_base = m.sym.startswith("USD")
    side = "BUY" if (usd_base == usd_up) else "SELL"
    out = []
    for day, k in starts.items():
        x = ends.get(day) if end_hour > start_hour else _at_minute_each_day(m, end_hour * 60).get(day + 1)
        if x is None or x <= k:
            continue
        entry, spread = _entry(m, k, side)
        sl = entry * (1 - sl_pct / 100) if side == "BUY" else entry * (1 + sl_pct / 100)
        out.append({"i": k, "tf": "H1", "side": side, "entry": entry, "sl": sl, "tp": None,
                    "spread": spread, "be": None, "exit_i": x})
    return out


# ---------------------------------------------------------------------------
# 5. New York afternoon vs the London morning move
# ---------------------------------------------------------------------------

def ny_vs_london(m, london=(7, 12), exit_hour=20, min_move_atr=1.0, mode="fade", sl_atr=1.5):
    """
    At london[1] (London time), measure the move since london[0]. If it is at least
    min_move_atr × the daily ATR (from completed days), trade against it ("fade") or with
    it ("follow") until exit_hour London. SL sl_atr × daily ATR.
    """
    D = m.bars["D1"]
    A = atr(D, 14)
    day_of = np.repeat(np.arange(len(D["start"])), D["end"] - D["start"])
    a = _at_minute_each_day(m, london[0] * 60)
    b = _at_minute_each_day(m, london[1] * 60)
    x = _at_minute_each_day(m, exit_hour * 60)
    d = m.d
    out = []
    for day, k0 in a.items():
        k1, kx = b.get(day), x.get(day)
        if k1 is None or kx is None or not (k0 < k1 < kx):
            continue
        j = day_of[k1] - 1                                   # last completed broker day
        if j < 14:
            continue
        move = d["bo"][k1] - d["bo"][k0]
        if abs(move) < min_move_atr * A[j]:
            continue
        up = move > 0
        side = ("SELL" if up else "BUY") if mode == "fade" else ("BUY" if up else "SELL")
        entry, spread = _entry(m, k1, side)
        sl = entry - sl_atr * A[j] if side == "BUY" else entry + sl_atr * A[j]
        out.append({"i": k1, "tf": "H1", "side": side, "entry": entry, "sl": sl, "tp": None,
                    "spread": spread, "be": None, "exit_i": kx})
    return out
