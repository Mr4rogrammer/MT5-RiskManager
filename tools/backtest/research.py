"""
Run strategies over symbols and score them per period.

Periods (fixed in advance — keep it that way):
  P1 2023-01 → 2024-12   design / tuning: explore here only
  P2 2025-01 → 2025-12   validation: shortlisted ideas must also be positive here
  P3 2026-01 → …         final test: look once, for the last few candidates (show_test=True)
"""
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import engine

SYMBOLS = ["EURUSD", "GBPUSD", "AUDUSD", "USDCHF", "NZDUSD", "USDCAD"]
P2 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
P3 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())

pd.set_option("display.width", 260)
pd.set_option("display.max_columns", 60)
pd.set_option("display.max_rows", 300)

_markets = {}


def market(sym):
    """Load a symbol once per process."""
    if sym not in _markets:
        _markets[sym] = engine.Market(sym, engine.load_fxcm(sym))
    return _markets[sym]


def backtest(gen, symbols=SYMBOLS, filt=None, **params):
    """Run generator `gen(market, **params)` through the portfolio rules on each symbol."""
    trades = []
    for sym in symbols:
        m = market(sym)
        for t in engine.simulate(m, gen(m, **params), filt=filt):
            t["sym"] = sym
            trades.append(t)
    return trades


def evaluate(name, gen, show_test=False, symbols=SYMBOLS, filt=None, **kw):
    """
    One row of results per strategy:
      n, avgR, t (t-statistic of avgR — below ~2 can easily be luck), stressR (avgR with
      costs 50 % higher), win%, DD (max drawdown in R), sym+ (symbols with positive R),
      bp (average net result in basis points — useful for timed trades)
    Returns (row, trades DataFrame).
    """
    df = pd.DataFrame(backtest(gen, symbols, filt=filt, **kw))
    row = {"strategy": name}
    if df.empty:
        return row, df
    df = df.sort_values("t_exit")
    risk = (df["entry"] - df["sl"]).abs()
    comm = df.apply(lambda r: market(r["sym"]).commission_price(r["entry"]), axis=1)
    df["R_stress"] = df["R"] - 0.5 * (df["spread"] + comm) / risk
    df["bp"] = df["R"] * risk / df["entry"] * 1e4

    periods = [("P1", df.t_entry < P2), ("P2", (df.t_entry >= P2) & (df.t_entry < P3))]
    if show_test:
        periods.append(("P3", df.t_entry >= P3))
    for per, mask in periods:
        g = df[mask]
        R = g["R"].to_numpy()
        if len(R) == 0:
            row[f"{per}_n"] = 0
            continue
        eq = np.cumsum(R)
        dd = (np.maximum.accumulate(np.r_[0, eq])[1:] - eq).max()
        sd = R.std(ddof=1) if len(R) > 1 else np.nan
        bysym = g.groupby("sym")["R"].sum()
        row.update({
            f"{per}_n": len(R),
            f"{per}_avgR": round(R.mean(), 3),
            f"{per}_t": round(R.mean() / (sd / np.sqrt(len(R))), 2) if sd else np.nan,
            f"{per}_stressR": round(g["R_stress"].mean(), 3),
            f"{per}_win%": round(100 * (R > 0.05).mean(), 1),
            f"{per}_DD": round(dd, 1),
            f"{per}_sym+": f"{(bysym > 0).sum()}/{len(bysym)}",
            f"{per}_bp": round(g["bp"].mean(), 2),
        })
    return row, df


def table(rows, columns=None):
    df = pd.DataFrame(rows)
    return (df[columns] if columns else df).to_string(index=False)
