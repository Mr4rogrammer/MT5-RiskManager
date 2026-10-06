"""
Backtest engine that mirrors the live bots (bot_common.py) on M1 bid/ask data.

Data   : FXCM public M1 candles (bid + ask OHLC), UTC.
Candles: M15/M30/H1/H4/D1 built in broker time = UTC+2 (winter) / UTC+3 (US summer),
         the usual MT5 "New York close" alignment, so H4/D1 match MT5.
Fills  : BUY at ask, SELL at bid; SL/TP checked on bid for BUY, ask for SELL.
         Same-minute SL+TP → SL first (pessimistic). Commission $5/lot round trip.
Rules  : HTF-first (H4 → M15), one trade per symbol, opposite higher-TF signal closes
         the running trade and enters, same-direction higher-TF signal is skipped,
         break-even moves SL to entry ± commission once the trigger is reached.
"""
import glob
import gzip
import io
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data", "fxcm")      # <SYMBOL>/<year>-<week>.csv.gz (fetch_fxcm.sh)
CACHE_DIR = os.path.join(BASE, "data", "cache")    # parsed .npz per symbol
TFS = {"H4": 14400, "H1": 3600, "M30": 1800, "M15": 900}
RANK = {tf: i for i, tf in enumerate(TFS)}
NY = ZoneInfo("America/New_York")
COMMISSION_PER_LOT = 5.0          # USD round trip
LOT = 0.01


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_fxcm(sym):
    """M1 bid/ask arrays for a symbol, cached as .npz after the first parse."""
    cache = os.path.join(CACHE_DIR, f"{sym}.npz")
    if os.path.exists(cache):
        z = np.load(cache)
        return {k: z[k] for k in z.files}
    frames = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, sym, "*.csv.gz"))):
        if os.path.getsize(path) == 0:
            continue
        with gzip.open(path, "rb") as f:
            raw = f.read()
        # Some FXCM files are UTF-16 encoded
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8")
        frames.append(pd.read_csv(io.StringIO(text)))
    if not frames:
        raise FileNotFoundError(f"no data for {sym} in {DATA_DIR} — run ./fetch_fxcm.sh first")
    df = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(df["DateTime"], format="%m/%d/%Y %H:%M:%S.%f", utc=True)
    df["t"] = (ts - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)   # unit-independent
    df = df.drop_duplicates("t").sort_values("t")
    out = {"t": df["t"].to_numpy(np.int64)}
    for side, pre in (("b", "Bid"), ("a", "Ask")):
        for k, col in (("o", "Open"), ("h", "High"), ("l", "Low"), ("c", "Close")):
            out[side + k] = df[pre + col].to_numpy(np.float64)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    np.savez_compressed(cache, **out)
    return out


def broker_offsets(t):
    """Seconds to add to UTC for MT5 broker time (UTC+2 winter / UTC+3 US summer)."""
    days = t // 86400
    uniq, inv = np.unique(days, return_inverse=True)
    off = np.array([
        int(datetime.fromtimestamp(int(d) * 86400 + 43200, timezone.utc).astimezone(NY)
            .utcoffset().total_seconds()) + 7 * 3600
        for d in uniq], dtype=np.int64)
    return off[inv]


class Market:
    """One symbol's M1 data plus candles for every timeframe."""

    def __init__(self, sym, data):
        self.sym = sym
        self.d = data
        self.t = data["t"]
        self.usd_base = sym.startswith("USD")       # P/L in quote currency → divide by price
        self.contract = 100000.0
        self.pip = 0.01 if sym.endswith("JPY") else 0.0001
        self.bt = self.t + broker_offsets(self.t)   # broker time
        self.bars = {tf: self._candles(sec) for tf, sec in {**TFS, "D1": 86400}.items()}

    def _candles(self, sec):
        bucket = self.bt // sec
        starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
        ends = np.r_[starts[1:], len(bucket)]                 # exclusive
        d = self.d
        return {
            "bucket": bucket[starts], "start": starts, "end": ends,
            "o": d["bo"][starts], "c": d["bc"][ends - 1],
            "h": np.maximum.reduceat(d["bh"], starts), "l": np.minimum.reduceat(d["bl"], starts),
        }

    # -- money helpers (0.01 lot) --
    def usd_per_price(self, price):
        v = self.contract * LOT
        return v / price if self.usd_base else v

    def commission_price(self, price):
        """Round-trip commission as a price distance."""
        return COMMISSION_PER_LOT * LOT / self.usd_per_price(price)


# ---------------------------------------------------------------------------
# Trade outcome
# ---------------------------------------------------------------------------

def _first(mask):
    i = int(np.argmax(mask))
    return i if mask[i] else -1


def run_trade_trail(m, sig, max_minutes):
    """
    Trailing-stop / timed exit. sig["trail"] = trailing distance (price); the stop is
    max(initial SL, highest bid since entry − trail) for BUY (mirror for SELL), using only
    PREVIOUS minutes' extremes (no same-minute look-ahead). Optional sig["tp"] (None = no
    target) and sig["exit_i"] (M1 index: exit at that minute's open if still open).
    """
    d, i0, side = m.d, sig["i"], sig["side"]
    end = min(len(m.t), sig.get("exit_i") or len(m.t), i0 + max_minutes)
    if end <= i0 + 1:
        k = min(i0 + 1, len(m.t) - 1)
        return {"k": k, "price": close_at(m, side, k), "reason": "time"}
    trail, sl, tp = sig.get("trail"), sig["sl"], sig.get("tp")
    if side == "BUY":
        hi, lo = d["bh"][i0:end], d["bl"][i0:end]
        stop = np.full(end - i0, sl)
        if trail:
            run_max = np.maximum.accumulate(hi)
            stop = np.maximum(stop, np.r_[-np.inf, run_max[:-1]] - trail)
        stop_hit = lo <= stop
        tp_hit = hi >= tp if tp is not None else np.zeros(end - i0, bool)
    else:
        hi, lo = d["ah"][i0:end], d["al"][i0:end]
        stop = np.full(end - i0, sl)
        if trail:
            run_min = np.minimum.accumulate(lo)
            stop = np.minimum(stop, np.r_[np.inf, run_min[:-1]] + trail)
        stop_hit = hi >= stop
        tp_hit = lo <= tp if tp is not None else np.zeros(end - i0, bool)
    k_s, k_t = _first(stop_hit), _first(tp_hit)
    INF = 10**12
    ks, kt = (k if k >= 0 else INF for k in (k_s, k_t))
    if ks == INF and kt == INF:
        k = min(end, len(m.t) - 1)
        return {"k": k, "price": close_at(m, side, k), "reason": "time"}
    if ks <= kt:
        reason = "sl" if stop[k_s] == sl else "trail"
        return {"k": i0 + k_s, "price": stop[k_s], "reason": reason}
    return {"k": i0 + k_t, "price": tp, "reason": "tp"}


def run_trade(m, sig, max_minutes=60 * 24 * 30):
    if sig.get("trail") or sig.get("exit_i") or sig.get("tp") is None:
        return run_trade_trail(m, sig, sig.get("max_minutes", max_minutes))
    """
    Simulate one trade from sig["i"] (M1 index of the entry minute; fill at its open).
    Returns dict with exit index, exit price, reason. BE per sig["be"] (trigger price or None).
    """
    d, i0, side = m.d, sig["i"], sig["side"]
    entry, sl, tp, be = sig["entry"], sig["sl"], sig["tp"], sig.get("be")
    comm = m.commission_price(entry)
    end = min(len(m.t), i0 + max_minutes)
    point = 0.00001
    # Like Bot.breakeven: the SL only moves once price is beyond the new SL, so a
    # trigger already behind entry effectively becomes "price past entry + commission"
    if side == "BUY":
        hi, lo = d["bh"][i0:end], d["bl"][i0:end]          # exits on bid
        sl_hit, tp_hit = lo <= sl, hi >= tp
        be_sl = entry + comm
        if be is not None:
            be = max(be, be_sl + point)
        trig_hit = hi >= be if be is not None else None
    else:
        hi, lo = d["ah"][i0:end], d["al"][i0:end]          # exits on ask
        sl_hit, tp_hit = hi >= sl, lo <= tp
        be_sl = entry - comm
        if be is not None:
            be = min(be, be_sl - point)
        trig_hit = lo <= be if be is not None else None

    k_sl, k_tp = _first(sl_hit), _first(tp_hit)
    k_tr = _first(trig_hit) if be is not None else -1
    INF = 10**12
    k_sl_, k_tp_, k_tr_ = (k if k >= 0 else INF for k in (k_sl, k_tp, k_tr))

    if k_tr_ < min(k_sl_, k_tp_) or (k_tr_ == k_tp_ and k_tr_ < k_sl_):
        # Break-even armed (strictly before any SL hit); from the next minute SL = be_sl
        if k_tp_ == k_tr_:
            return {"k": i0 + k_tp, "price": tp, "reason": "tp"}
        rest = slice(k_tr + 1, None)
        be_hit = (lo[rest] <= be_sl) if side == "BUY" else (hi[rest] >= be_sl)
        k_be, k_tp2 = _first(be_hit), _first(tp_hit[rest])
        k_be_ = k_be if k_be >= 0 else INF
        k_tp2_ = k_tp2 if k_tp2 >= 0 else INF
        if k_be_ == INF and k_tp2_ == INF:
            return {"k": end - 1, "price": d["bc"][end - 1] if side == "BUY" else d["ac"][end - 1], "reason": "timeout"}
        if k_be_ <= k_tp2_:
            return {"k": i0 + k_tr + 1 + k_be, "price": be_sl, "reason": "breakeven"}
        return {"k": i0 + k_tr + 1 + k_tp2, "price": tp, "reason": "tp"}

    if k_sl_ == INF and k_tp_ == INF:
        return {"k": end - 1, "price": d["bc"][end - 1] if side == "BUY" else d["ac"][end - 1], "reason": "timeout"}
    if k_sl_ <= k_tp_:
        return {"k": i0 + k_sl, "price": sl, "reason": "sl"}
    return {"k": i0 + k_tp, "price": tp, "reason": "tp"}


def close_at(m, side, k):
    """Market close at the open of minute k."""
    return m.d["bo"][k] if side == "BUY" else m.d["ao"][k]


# ---------------------------------------------------------------------------
# Filters (same as Bot.place)
# ---------------------------------------------------------------------------

def passes(m, sig, min_sl_spreads=3.0, min_rr=0.0):
    entry, sl, tp, side, spread = sig["entry"], sig["sl"], sig["tp"], sig["side"], sig["spread"]
    if tp is None:                                      # trailing / timed exits: SL checks only
        return (sl < entry if side == "BUY" else sl > entry) and abs(entry - sl) >= spread * min_sl_spreads
    if side == "BUY" and not (sl < entry < tp):
        return False
    if side == "SELL" and not (tp < entry < sl):
        return False
    risk, reward = abs(entry - sl), abs(tp - entry)
    if risk < spread * min_sl_spreads:
        return False
    if reward <= m.commission_price(entry) + spread:     # fees filter
        return False
    if min_rr and reward / risk < min_rr:
        return False
    return True


# ---------------------------------------------------------------------------
# Portfolio loop (one symbol)
# ---------------------------------------------------------------------------

def simulate(m, signals, filt=None):
    """
    signals: list of dicts with i (entry M1 index), tf, side, entry, sl, tp, spread, be.
    Returns closed trades with R, USD (0.01 lot), reason.
    """
    filt = filt or {}
    sigs = sorted(signals, key=lambda s: (s["i"], RANK[s["tf"]]))
    trades, pos = [], None
    for s in sigs:
        if pos is not None and pos["exit_k"] <= s["i"]:
            trades.append(pos)
            pos = None
        if pos is not None:
            # Only HIGHER timeframes are scanned while a trade is open
            if RANK[s["tf"]] >= RANK[pos["tf"]] or s["side"] == pos["side"]:
                continue
            if not passes(m, s, **filt):
                continue
            # Opposite higher-TF signal: close the running trade at market, then enter
            px = close_at(m, pos["side"], s["i"])
            _finish(m, pos, s["i"], px, "override")
            trades.append(pos)
            pos = None
        if not passes(m, s, **filt):
            continue
        out = run_trade(m, s)
        pos = dict(s)
        _finish(m, pos, out["k"], out["price"], out["reason"])
    if pos is not None:
        trades.append(pos)
    return trades


def _finish(m, pos, k, price, reason):
    side_mult = 1 if pos["side"] == "BUY" else -1
    entry = pos["entry"]
    comm = m.commission_price(entry)
    gross = (price - entry) * side_mult
    pos["exit_k"], pos["exit_price"], pos["reason"] = k, price, reason
    pos["R"] = (gross - comm) / abs(entry - pos["sl"])
    pos["usd"] = (gross - comm) * m.usd_per_price(price)
    pos["t_entry"], pos["t_exit"] = int(m.t[pos["i"]]), int(m.t[min(k, len(m.t) - 1)])


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def metrics(trades):
    if not trades:
        return {"n": 0}
    df = pd.DataFrame(trades).sort_values("t_exit")
    R = df["R"].to_numpy()
    eq = np.cumsum(R)
    dd = (np.maximum.accumulate(np.r_[0, eq])[1:] - eq).max()
    wins, losses = R[R > 0.05], R[R < -0.05]
    return {
        "n": len(df),
        "win%": round(100 * len(wins) / max(len(wins) + len(losses), 1), 1),
        "avgR": round(R.mean(), 3),
        "totR": round(R.sum(), 1),
        "PF": round(wins.sum() / -losses.sum(), 2) if len(losses) else float("inf"),
        "maxDD_R": round(dd, 1),
        "usd": round(df["usd"].sum(), 2),
        "tp%": round(100 * (df["reason"] == "tp").mean(), 1),
        "be%": round(100 * (df["reason"] == "breakeven").mean(), 1),
        "sl%": round(100 * (df["reason"] == "sl").mean(), 1),
    }
