#!/usr/bin/env python3
"""
Backtest the bots' strategies on FXCM M1 bid/ask data.

  python backtest.py                     # the three live bots, periods P1 + P2
  python backtest.py crt3 dsweep         # chosen strategies
  python backtest.py --list              # every registered strategy
  python backtest.py dsweep --test       # also show P3 (final test — use sparingly)
  python backtest.py crt3 --symbols EURUSD,GBPUSD --by sym

Add a strategy: write a generator in strategies.py / strategies_other.py (see README),
then register it in STRATEGIES below.
"""
import argparse
import sys
import time

import research
import strategies as st
import strategies_other as other

# name -> (generator, params, description)
STRATEGIES = {
    # live bots
    "crt3":   (st.crt3, {}, "Live 3-candle CRT (crt_bot.py)"),
    "crt2":   (st.crt2, {}, "Live 2-candle CRT (candle_two_bot.py)"),
    "dsweep": (st.daily_crt, dict(confirm_tf="M30", trend="against"), "Live daily sweep (daily_sweep_bot.py)"),
    # CRT variants tested
    "crt3-h4-pd":      (st.crt3, dict(tfs=("H4",), sweep_level="pd"), "3-candle CRT, H4 only, C2 also sweeps yesterday's high/low"),
    "dsweep-no-late":  (st.daily_crt, dict(confirm_tf="M30", trend="against", entry_until_hour=20),
                        "Daily sweep, no entries after 20:00 broker time (~17-18 UTC)"),
    "dsweep-with":     (st.daily_crt, dict(confirm_tf="M30", trend="with"), "Daily sweep, with the trend"),
    "asia-fade":       (st.asia_sweep, {}, "Fade a sweep of the Asian range at London open"),
    # other families tested
    "donchian20":      (other.donchian, dict(n=20), "Daily 20-day breakout, 2 ATR stop, 3 ATR trailing"),
    "london-breakout": (other.london_breakout, dict(sl_mode="opposite"), "Trade the Asian-range break, exit 20:00 UTC"),
    "rsi2-h4":         (other.rsi2_pullback, dict(tf="H4", trend_days=50), "RSI(2) dip in the daily trend, H4"),
    "usd-07-13":       (other.usd_window, dict(start_hour=7, end_hour=13, usd_up=True), "Long USD 07:00-13:00 London"),
    "ny-fade":         (other.ny_vs_london, dict(london=(7, 11), exit_hour=20, min_move_atr=0.5, mode="fade"),
                        "Fade the London-morning move into the NY afternoon"),
}

COLUMNS = ["strategy"] + [f"{p}_{k}" for p in ("P1", "P2", "P3")
                          for k in ("n", "avgR", "t", "stressR", "win%", "DD", "sym+")]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="strategies to run (default: the live bots)")
    ap.add_argument("--list", action="store_true", help="list registered strategies")
    ap.add_argument("--test", action="store_true", help="also show P3, the final-test period")
    ap.add_argument("--symbols", help="comma list (default: all six)")
    ap.add_argument("--by", choices=["sym", "tf", "reason", "year"], help="also break the results down")
    args = ap.parse_args()

    if args.list:
        for k, (_, _, desc) in STRATEGIES.items():
            print(f"  {k:16} {desc}")
        return

    names = args.names or ["crt3", "crt2", "dsweep"]
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        sys.exit(f"unknown strategy: {', '.join(unknown)} (see --list)")
    symbols = args.symbols.split(",") if args.symbols else research.SYMBOLS

    t0, rows, frames = time.time(), [], {}
    for name in names:
        gen, params, _ = STRATEGIES[name]
        row, df = research.evaluate(name, gen, show_test=args.test, symbols=symbols, **params)
        rows.append(row)
        frames[name] = df
    cols = [c for c in COLUMNS if any(c in r for r in rows)]
    print(research.table(rows, cols))

    if args.by:
        for name, df in frames.items():
            if df.empty:
                continue
            if args.by == "year":
                df = df.assign(year=(df.t_entry // (365.25 * 86400) + 1970).astype(int))
            if not args.test:
                df = df[df.t_entry < research.P3]
            print(f"\n{name} by {args.by}:")
            print(df.groupby(args.by)["R"].agg(trades="count", avgR="mean", totalR="sum").round(3).to_string())
    print(f"\n{time.time() - t0:.0f}s · P1 2023-24 (design) · P2 2025 (validation)"
          + (" · P3 2026 (final test)" if args.test else " · P3 hidden (--test)"))


if __name__ == "__main__":
    main()
