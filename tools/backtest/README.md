# Bot Backtester

Tests the bots' strategies on 1-minute **bid/ask** data before they go live. It uses the same
rules as `backend/mt5/app/bot_common.py`, so a backtest result reflects what the live bot
would have done.

## Setup

```bash
cd tools/backtest
python3 -m venv venv && venv/bin/pip install -r requirements.txt
./fetch_fxcm.sh            # ~100 MB, about 15 minutes; resumable
venv/bin/python backtest.py
```

The data is FXCM's free public archive: EURUSD, GBPUSD, AUDUSD, USDCHF, NZDUSD and USDCAD,
1-minute bid + ask, January 2023 onward. Gold (XAUUSD) isn't in it. Files live in `data/`
(gitignored). The first run builds a cache (about 1 minute); after that a strategy runs in
seconds.

## Usage

```bash
venv/bin/python backtest.py                         # the three live bots
venv/bin/python backtest.py --list                  # every registered strategy
venv/bin/python backtest.py crt3 dsweep --by year   # breakdown by year / sym / tf / reason
venv/bin/python backtest.py dsweep --symbols EURUSD,GBPUSD
venv/bin/python backtest.py dsweep --test           # also show the final-test period
```

## How it works

| | |
| - | - |
| Candles | M15/M30/H1/H4/D1 built in broker time (UTC+2 winter / UTC+3 US summer), so they match MT5 |
| Fills | BUY at ask, SELL at bid, at the entry minute's open; SL/TP checked on bid for BUY, ask for SELL |
| Same-minute SL and TP | SL counts first (pessimistic) |
| Costs | Real spread from the data plus $5/lot round-trip commission |
| Bot rules | H4 → M15 priority, one trade per symbol, an opposite higher-TF signal closes the trade and enters, a same-direction one is skipped, break-even to entry + commission |
| Exits | Fixed SL/TP, break-even, trailing stop (`trail`), timed or indicator exit (`exit_i`) |

## Reading the results — and not fooling yourself

Results are split into three periods. **Keep this split**: it's what stops a lucky backtest
from looking like a strategy.

| Period | Use |
| ------ | --- |
| **P1** 2023–2024 | Design and tuning. Explore here only. |
| **P2** 2025 | Validation. A shortlisted idea must also be positive here. |
| **P3** 2026 → | Final test. Look once (`--test`), for the last 2–3 candidates. |

| Column | Meaning |
| ------ | ------- |
| `n` | Trades |
| `avgR` | Average result per trade in R (1R = the initial stop distance), after costs |
| `t` | t-statistic of `avgR`. **Below about 2 it can easily be luck**, and after trying many variants, the best one reaching 2–3 is still normal luck |
| `stressR` | `avgR` with costs 50% higher |
| `win%`, `DD` | Win rate; max drawdown in R |
| `sym+` | Symbols with positive total R. An edge should show on most of them |

A setting is worth trading only if it's positive in P1, P2 *and* P3, on most symbols, with
`stressR` > 0, and its neighbours (slightly different parameters) are positive too.

## What has been tested (Jan 2023 → Sep 2026)

| Strategy | Result |
| -------- | ------ |
| `crt3`, `crt2` (live) | **Losing: −0.25 / −0.27R per trade over ~100k trades.** Stops of 3–6 pips make spread + commission ≈ 0.2R per trade, and before costs the pattern is a coin flip |
| CRT variants: H4 only, key levels, trend, session filters | Around 0 before costs |
| `dsweep` (live) and variants | About 0 in 2023–25; final test negative. Results flip with small changes (e.g. the entry cut-off hour), so it's fragile, not an edge |
| Trend following (`donchian20`) | Negative, and few trades |
| `london-breakout`, `asia-fade` | Negative |
| RSI(2) mean reversion | 13% of 144 settings positive, about what chance gives |
| Dollar-flow windows (`usd-07-13`) | A real intraday dollar pattern (t ≈ 4 for some hours), but smaller than the costs; failed 2025 |
| `ny-fade` | Failed 2025 |

## Adding a strategy

1. Write a generator in `strategies.py` (CRT family) or `strategies_other.py`:

   ```python
   def my_idea(m, some_param=1.0):
       """m is an engine.Market: m.d (M1 bid/ask arrays), m.t (UTC seconds),
       m.bars["H1"] etc. (candles with start/end M1 indices)."""
       signals = []
       ...
       signals.append({
           "i": k,               # M1 index of the entry minute (filled at its open)
           "tf": "H1",           # H4/H1/M30/M15 — used for the priority/override rules
           "side": "BUY",
           "entry": price, "spread": spread,
           "sl": sl, "tp": tp,   # tp=None for trailing/timed exits
           "be": None,           # break-even trigger price, or None
           # optional: "trail": distance, "exit_i": M1 index to exit at, "max_minutes": n
       })
       return signals
   ```

   Only use data available at that moment: candles that have closed and minutes before `i`.
   Look-ahead makes any strategy look brilliant.

2. Register it in `STRATEGIES` in `backtest.py` and run it.
3. If it passes all three periods, implement it as a bot on `bot_common.Bot` (see the main README).
