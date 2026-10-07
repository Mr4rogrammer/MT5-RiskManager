# MetaTrader5 Flask API Server

A Flask REST API server running inside Wine that communicates directly with MetaTrader 5 via its Python API, exposing endpoints for data retrieval, order execution, position management, and trade history.

## Architecture

```
┌──────────────────────────────────────┐
│        Docker Container (Wine)       │
│                                      │
│  ┌────────────┐   ┌──────────────┐   │
│  │ MT5 Terminal│◄─►│  Flask API   │   │
│  │   (Wine)   │   │   :5001      │   │
│  └────────────┘   └──────────────┘   │
│                                      │
│  VNC Web Access :3000                │
└──────────────────────────────────────┘
```

## Features

- **Market Orders** — Place BUY/SELL orders with SL/TP, deviation, magic number, and filling type
- **Position Management** — Close single/all positions, modify SL/TP, list open positions
- **Market Data** — Fetch OHLCV bars by count or date range, symbol info, tick data
- **Trade History** — Query deal and order history by ticket or date range
- **API Key Authentication** — All endpoints (except health/swagger) require an API key
- **Swagger Documentation** — Auto-generated API docs at `/apidocs/`
- **VNC Access** — Web-based VNC to view the MT5 terminal GUI

## Installation on Linux

### Prerequisites

- A Linux server (Ubuntu 20.04+ / Debian 11+ recommended)
- A MetaTrader 5 broker account

### Step 1 — Install Docker & Docker Compose

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh

# Add your user to the docker group (so you don't need sudo)
sudo usermod -aG docker $USER

# Apply group change (or log out and back in)
newgrp docker

# Verify Docker is working
docker --version
docker compose version
```

### Step 2 — Clone the Repository

```bash
git clone https://github.com/Mr4rogrammer/MT5-RiskManager
cd metatrader5-server-python
```

### Step 3 — Configure Environment

```bash
cp .env.example .env
nano .env
```

Edit the `.env` file:

```env
CUSTOM_USER=admin
PASSWORD=your-vnc-password
MT5_API_PORT=5001
MT5_API_KEY=your-secret-api-key-here
```

- `CUSTOM_USER` / `PASSWORD` — Credentials to access the VNC web UI
- `MT5_API_KEY` — Any secret string you choose (used to authenticate API calls)

### Step 4 — Build & Start

```bash
docker compose up -d --build
```

First build takes **10-15 minutes** (downloads Wine, Python 3.9, MT5 installer, Mono).

Subsequent starts are fast because everything is cached in the `/config` volume.

### Step 5 — Log in to MT5 via VNC

1. Open your browser and go to: `http://your-server-ip:3000`
2. Enter your VNC credentials (`CUSTOM_USER` / `PASSWORD` from `.env`)
3. You will see the MT5 terminal running in a desktop environment
4. **Log in to your broker account** inside the MT5 terminal (File → Login to Trade Account)
5. Once connected, the Flask API can interact with MT5

### Step 6 — Verify the API is Running

```bash
# Health check (no auth required)
curl http://localhost:5001/health

# Expected response:
# {"mt5_connected": true, "mt5_initialized": true, "status": "healthy"}
```

### Step 7 — Start Using the API

```bash
# Get current positions
curl -H "Authorization: Bearer your-secret-api-key-here" \
  http://localhost:5001/get_positions

# Place a trade
curl -X POST http://localhost:5001/order \
  -H "Authorization: Bearer your-secret-api-key-here" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "EURUSD",
    "volume": 0.01,
    "type": "BUY",
    "deviation": 20
  }'
```

## Useful Commands

```bash
# View logs
docker compose logs -f mt5

# View MT5 setup log inside container
docker exec mt5 cat /var/log/mt5_setup.log

# View Flask API / CRT bot log (Flask is auto-restarted if it crashes)
docker exec mt5 tail -f /config/flask.log

# Restart the container
docker compose restart mt5

# Stop everything
docker compose down

# Stop and remove all data (Wine prefix, MT5 installation)
docker compose down -v

# Rebuild from scratch
docker compose up -d --build --force-recreate
```

## Endpoints

| Port | Service         |
| ---- | --------------- |
| 3000 | MT5 VNC Web UI  |
| 5001 | MT5 Flask API   |
| 8080 | Bot dashboard   |

## API Documentation

- **Swagger UI**: `http://localhost:5001/apidocs/`

### API Endpoints

| Method | Endpoint                     | Description                          |
| ------ | ---------------------------- | ------------------------------------ |
| GET    | `/health`                    | Health check (public)                |
| GET    | `/symbol_info_tick/<symbol>` | Latest tick (bid/ask/volume)         |
| GET    | `/symbol_info/<symbol>`      | Full symbol metadata                 |
| GET    | `/fetch_data_pos`            | OHLCV bars from current position     |
| GET    | `/fetch_data_range`          | OHLCV bars within a date range       |
| POST   | `/order`                     | Place a market order                 |
| POST   | `/close_position`            | Close a specific position            |
| POST   | `/close_all_positions`       | Close all positions (filterable)     |
| POST   | `/modify_sl_tp`              | Modify SL/TP for a position          |
| GET    | `/get_positions`             | List open positions                  |
| GET    | `/positions_total`           | Count open positions                 |
| GET    | `/get_deal_from_ticket`      | Deal details by ticket               |
| GET    | `/get_order_from_ticket`     | Order details by ticket              |
| GET    | `/history_deals_get`         | Deals in date range for a position   |
| GET    | `/history_orders_get`        | Order history by ticket              |
| GET    | `/last_error`                | MT5 last error code + message        |
| GET    | `/last_error_str`            | MT5 last error as string             |

### Authentication

All endpoints except `/health` and `/apidocs/` require an API key via the `Authorization` header:

```bash
curl -H "Authorization: Bearer your-api-key" http://localhost:5001/get_positions
```

## Environment Variables

| Variable       | Description                       |
| -------------- | --------------------------------- |
| `CUSTOM_USER`  | VNC username                      |
| `PASSWORD`     | VNC password                      |
| `MT5_API_PORT` | Flask API port (default: 5001)    |
| `MT5_API_KEY`  | API key for authentication        |

## How It Works Inside

When the container starts, it runs these steps in order:

1. **Install Mono** — .NET runtime needed by MT5 (skipped if already installed)
2. **Install MT5** — Downloads and installs MetaTrader 5 via Wine (skipped if already installed)
3. **Install Python 3.9** — Installs Windows Python inside Wine (skipped if already installed)
4. **Install Libraries** — Pip installs Flask, MetaTrader5, pandas, etc. inside Wine Python
5. **Start MT5 Terminal** — Launches `terminal64.exe` via Wine
6. **Start Flask API** — Runs `app.py` using Wine Python, listening on port 5001

All MT5 data (Wine prefix, MT5 installation, broker login) is persisted in the `./config` volume.

## CRT Auto-Trading Bot

A background thread (`app/crt_bot.py`) trades Candle Range Theory setups on
H4, H1, M30 and M15. It polls every `CRT_POLL_INTERVAL` seconds (default 5) so each
candle close is caught quickly.

### The pattern

| Candle | Role |
| ------ | ---- |
| **C1** | Range candle (closed) |
| **C2** | Sweep candle (just closed): takes out one side of C1, then closes back inside C1 |
| **C3** | Candle now opening. **Entry happens here** at market |

| Setup | Condition | Entry | SL | TP |
| ----- | --------- | ----- | -- | -- |
| **Bearish → SELL** | `C2.high > C1.high` and C2 closes inside C1 | Bid at C3 open | C2 high + spread (+ buffer) | C1 low |
| **Bullish → BUY**  | `C2.low < C1.low` and C2 closes inside C1   | Ask at C3 open | C2 low (− buffer)  | C1 high |

If C2 sweeps **both** sides, or neither, there is no signal. TP is the **opposite side
of C1** (the full range), not 50% of it.

**Why the SELL stop gets an extra spread:** MT5 candles are drawn from bid prices, but a
SELL's stop loss triggers on the ask. A stop exactly at the C2 high would be hit while the
bid is still one spread below the wick. `CRT_SL_BUFFER_SPREADS` adds more room on both sides.

The bot trades the **pattern only**. It does not check key levels, higher-timeframe bias,
sessions or lower-timeframe confirmation.

### Trade rules

1. **Break-even.** Once price has covered `CRT_BE_TRIGGER` (default 47%) of the entry→TP
   distance, the SL moves to entry **plus the round-trip commission** (minus it for a SELL),
   so a break-even exit really nets about $0. The move is skipped while price is within
   the broker's stops/freeze level. A rejected move is retried after 60 seconds, not every
   poll. Break-even runs every poll, even after `/bot/stop`.
2. **One trade per pair, higher timeframe wins.** While a CRT trade is open on a symbol,
   the bot only scans timeframes **higher** than that trade's:

   | Running trade | New signal on | Result |
   | ------------- | ------------- | ------ |
   | H1 BUY | H4 **SELL** (opposite) | H1 trade is closed at market, H4 SELL is entered |
   | H1 BUY | H4 BUY (same direction) | Skipped, H1 trade is kept |
   | H1 BUY | M30 / M15 / H1 (any) | Not scanned. H1 keeps running |
   | H4 (any) | anything | Not scanned |

   The running trade is only closed once the higher-timeframe setup has passed every
   filter below, so it is never closed for a trade that would then be skipped. If the
   close fails, the new trade is not entered. The running trade's timeframe is taken
   from its magic number. A trade is logged as `closed` with reason
   `overridden by <TF> signal`.
3. **Higher timeframe first.** Timeframes are scanned H4 → H1 → M30 → M15. When a trade is
   placed on one, the lower timeframes are skipped for that symbol in that cycle.
4. **C3 entry only.** A setup is ignored if more than `CRT_MAX_SIGNAL_AGE` seconds
   (default 300) have passed since C3 opened. Each C2 is evaluated only once.

### Filters a signal must pass (in order)

1. **Price still between SL and TP.** It is skipped if price has already moved past either.
2. **Minimum stop distance.** The SL must be at least `CRT_MIN_SL_SPREADS` (default 3) times the
   current spread from entry. Stops only a spread or two away get hit by normal noise.
3. **Fees.** The expected profit at TP (in account currency, from `order_calc_profit`)
   must be greater than the round-trip commission plus the spread cost. If MT5 cannot
   calculate profit, the bot falls back to a simpler check: the TP distance must be
   larger than the spread.
4. **Reward:risk.** It must be at least `CRT_MIN_RR` (default 1.0; `0` turns this off).
5. **Broker stops level.** SL and TP must both be at least `trade_stops_level` points
   from price.
6. **No duplicates.** A position with the same magic number and comment must not
   already be open.

### Commission model

Two modes, chosen per symbol:

| Mode | Applies to | Formula | Example |
| ---- | ---------- | ------- | ------- |
| **Flat** | All symbols **not** in `CRT_COMMISSION_PCT_SYMBOLS` (forex, XAUUSD, XAGUSD…) | `lot × CRT_COMMISSION_PER_LOT` | 0.01 lot × $5 = **$0.05** |
| **Percentage** | Symbols in `CRT_COMMISSION_PCT_SYMBOLS` (e.g. BTCUSD) | `lot × contract_size × price × CRT_COMMISSION_PCT_RATE / 100` | 0.01 × 1 × 60 000 × 0.04% = **$0.24** |

Both rates are **round-trip** (opening and closing combined).

### Order tagging

- Magic number = `CRT_MAGIC_BASE` + timeframe minutes: 770015 (M15), 770030 (M30),
  770060 (H1), 770240 (H4)
- Comment = `CRT <TF> <C2 open time>` (e.g. `CRT H1 1759737600`)

### Control

The bot is **off by default**. Set `CRT_ENABLED=true` in `.env` or call:

| Method | Endpoint      | Description                                     |
| ------ | ------------- | ----------------------------------------------- |
| GET    | `/bot/status` | Enabled flag, Algo Trading flag, settings, last 100 events |
| POST   | `/bot/start`  | Start trading                                   |
| POST   | `/bot/stop`   | Stop new entries and overrides. Open trades keep their SL/TP, and break-even still runs |

Event statuses in `/bot/status`: `signal`, `opened`, `closed` (higher-TF override),
`skipped` (with a `reason`), `rejected` (broker retcode), `error`, `breakeven`.

> The MT5 terminal's **Algo Trading** button must be on (`algo_trading: true` in
> `/bot/status`), or the broker will reject the orders.

### Settings (`.env`)

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `CRT_ENABLED` | `false` | Start trading on boot |
| `CRT_SYMBOLS` | `XAUUSD,EURUSD,GBPUSD,AUDUSD,USDCHF,NZDUSD,USDCAD` | Symbols to trade |
| `CRT_LOT` | `0.01` | Fixed lot size — used only when `BOTS_RISK_PCT=0` (normalized to the broker's volume step/min/max) |
| `CRT_DEVIATION` | `20` | Maximum slippage in points |
| `CRT_MAGIC_BASE` | `770000` | Base for the magic number of each timeframe |
| `CRT_POLL_INTERVAL` | `5` | Seconds between checks |
| `CRT_MAX_SIGNAL_AGE` | `300` | Maximum seconds after C3 opens that an entry is still allowed |
| `CRT_MIN_RR` | `1.0` | Minimum reward:risk (`0` disables) |
| `CRT_COMMISSION_PER_LOT` | `5.0` | Flat round-trip commission per 1.0 lot, in account currency |
| `CRT_COMMISSION_PCT_SYMBOLS` | `BTCUSD` | Comma-separated symbols that use percentage commission |
| `CRT_COMMISSION_PCT_RATE` | `0.04` | Round-trip percentage rate for those symbols (`0.04` = 0.04%) |
| `CRT_SL_BUFFER_SPREADS` | `0` | Extra SL room beyond the C2 wick, in multiples of the spread |
| `CRT_MIN_SL_SPREADS` | `3` | Skip setups whose SL is closer than N × spread (`0` disables) |
| `CRT_BE_TRIGGER` | `0.47` | Fraction of the entry→TP distance at which SL moves to break-even |

If your broker uses suffixed symbol names (e.g. `XAUUSDm`), list them exactly in
`CRT_SYMBOLS`. Note that `CRT_COMMISSION_PCT_SYMBOLS` is uppercased when it is read, so a
symbol with a lowercase suffix (e.g. `BTCUSDm`) will not match and falls back to flat commission.

## 2-Candle CRT Bot

A second, independent bot (`app/candle_two_bot.py`) trades the same pattern **inside C2**,
instead of waiting for C3. It runs at the same time as the 3-candle bot.

| Candle | Role |
| ------ | ---- |
| **C1** | Range candle (last closed candle) |
| **C2** | Candle now forming. It must open inside C1, sweep one side of C1, then cross back through its own open. **Entry happens here** |

| Setup | Condition | Entry | SL | TP | Break-even trigger |
| ----- | --------- | ----- | -- | -- | ------------------ |
| **Bullish → BUY** | C2 goes below C1 low, then bid crosses back **above** C2 open | Ask | C2 low so far (− buffer) | C1 high | C1 low + 45% of C1 range |
| **Bearish → SELL** | C2 goes above C1 high, then bid crosses back **below** C2 open | Bid | C2 high so far + spread (+ buffer) | C1 low | C1 high − 45% of C1 range |

Example (BUY): C1 = 1.1000–1.1100, C2 opens at 1.1030, drops to 1.0990, and the bid crosses back
above 1.1030 → BUY with SL 1.0990, TP 1.1100. When the bid reaches 1.1045, the SL moves to entry
+ commission. If entry is already past the 45% level, the SL moves as soon as price is far enough
past entry for the broker to accept it.

**Same rules as the 3-candle bot:** H4 → H1 → M30 → M15 priority, one trade per symbol, an
opposite higher-timeframe signal closes the running trade and enters, a same-direction one
is skipped, plus the same min-SL, commission + spread, reward:risk and stops-level filters.

**Specific to this bot:**
- **Fresh cross only.** The cross through C2's open must happen between two polls no more
  than 3 poll intervals apart. A cross the bot didn't see (e.g. during a restart) is not chased.
- **One trade per C2.** If the trade stops out, the same C2 candle is not traded again.
- **Both sweeps → no trade.** If C2 has swept both sides of C1 before the cross, it is skipped.
- Polls every 2 seconds by default, since entries happen mid-candle.

**Running both bots together.** They don't block each other: the 3-candle bot manages only
positions whose comment starts with `CRT `, and this bot only `CRT2 ` positions, so a symbol
can have one trade from each. Magic numbers are `CRT2_MAGIC_BASE` + timeframe minutes:
780015, 780030, 780060, 780240. The comment is `CRT2 <TF> <C1 open time>`; break-even uses it to
find C1 again after a restart. Commission settings (`CRT_COMMISSION_*`) are shared.

| Method | Endpoint       | Description                                        |
| ------ | -------------- | -------------------------------------------------- |
| GET    | `/bot2/status` | Enabled flag, Algo Trading flag, settings, last 100 events |
| POST   | `/bot2/start`  | Start trading                                      |
| POST   | `/bot2/stop`   | Stop new entries and overrides (break-even still runs) |

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `CRT2_ENABLED` | `false` | Start trading on boot |
| `CRT2_SYMBOLS` | same as `CRT_SYMBOLS` | Symbols to trade |
| `CRT2_LOT` | `0.01` | Fixed lot size (only when `BOTS_RISK_PCT=0`) |
| `CRT2_DEVIATION` | `20` | Maximum slippage in points |
| `CRT2_MAGIC_BASE` | `780000` | Base for the magic number of each timeframe |
| `CRT2_POLL_INTERVAL` | `2` | Seconds between checks |
| `CRT2_MIN_RR` | `0` | Minimum reward:risk (`0` disables) |
| `CRT2_SL_BUFFER_SPREADS` | `0` | Extra SL room beyond the sweep, in multiples of the spread |
| `CRT2_MIN_SL_SPREADS` | `3` | Skip setups whose SL is closer than N × spread (`0` disables) |
| `CRT2_BE_TRIGGER` | `0.45` | How far into C1's range (from the swept side) break-even triggers |

## Daily Sweep Bot

A third bot (`app/daily_sweep_bot.py`): CRT on the **daily** candle, the classic
"turtle soup" failed breakout.

- **C1** = yesterday's broker-day candle, **C2** = today.
- **SELL** when today has gone above yesterday's high and an M30 candle closes back below it,
  **only if the daily trend is up** (yesterday's close above the 20-day average): a failed new
  high at the end of a run, where breakout buyers are trapped.
- **BUY** is the mirror: below yesterday's low, close back above it, daily trend down.
- **SL** beyond today's extreme so far (+ spread for SELL). **TP** = the other side of
  yesterday's range. No break-even by default, since targets are about 5× the risk.
- One trade per symbol per day. If today sweeps both sides, no trade.

**Why daily:** stops are 15–40 pips instead of 3–6, so spread and commission are a small part
of each trade. They are what sinks the M15/M30 CRT setups.

**What the backtest showed** (6 FX pairs, FXCM 1-minute bid/ask, Jan 2023 → Sep 2026,
same costs and rules as live; gold not tested):

| | Avg per trade | Win rate | Max drawdown |
| - | - | - | - |
| 3-candle CRT (live rules) | −0.25R | 37% | — |
| Daily sweep, 2023–24 | +0.005R | 17% | ~148R |
| Daily sweep, 2025–26 (not used for design) | +0.007R | 17% | ~141R |

That's roughly break-even: far better than the M15–H4 CRT bots, but not a proven edge, and
with long losing streaks. Trading **against** the trend beat no filter, which beat trading
**with** it, in both periods. EURUSD and GBPUSD were positive in both. Run it at small size
and let the dashboard judge it.

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `DSW_ENABLED` | `false` | Start trading on boot |
| `DSW_SYMBOLS` | same as `CRT_SYMBOLS` | Symbols to trade |
| `DSW_LOT` | `0.01` | Fixed lot (only when `BOTS_RISK_PCT=0`) |
| `DSW_MAGIC_BASE` | `790000` | Trades use 790030 |
| `DSW_TREND` | `against` | `against` / `with` / `off` |
| `DSW_SMA_DAYS` | `20` | Daily trend average length |
| `DSW_TP_FRAC` | `1.0` | TP as a fraction of the way to the other side of yesterday's range |
| `DSW_MIN_SL_SPREADS` | `3` | Minimum SL distance in spreads |
| `DSW_SL_BUFFER_SPREADS` | `0` | Extra SL room beyond today's extreme |
| `DSW_BE_TRIGGER` | `0` | Break-even trigger as a fraction of entry→TP (`0` = off) |
| `DSW_POLL_INTERVAL` / `DSW_MAX_SIGNAL_AGE` / `DSW_MIN_RR` / `DSW_DEVIATION` | `5` / `300` / `0` / `20` | As for the other bots |

### Controlling any bot

Generic routes work for every bot, including future ones:

| Method | Endpoint | Description |
| ------ | -------- | ----------- |
| GET | `/bots` | All bots: name, title, enabled, settings |
| GET | `/bots/<name>/status` | One bot's settings and last 100 events |
| POST | `/bots/<name>/start` | Start new entries |
| POST | `/bots/<name>/stop` | Stop new entries (open trades keep SL/TP, break-even still runs) |

Bot names: `crt3`, `crt2`, `dsweep`. The older `/bot/*` and `/bot2/*` routes still work.
The easier way is the **Start / Stop** button on each bot's card in the dashboard (see below).

## Backtesting

`tools/backtest/` tests strategies on 1-minute bid/ask data (6 FX pairs, 2023 onward) with the
same rules and costs as the live bots, before they trade real money. See
[tools/backtest/README.md](tools/backtest/README.md).

```bash
cd tools/backtest && python3 -m venv venv && venv/bin/pip install -r requirements.txt
./fetch_fxcm.sh && venv/bin/python backtest.py
```

## Indicator Bots (10 popular strategies)

`app/indicator_bots.py` runs ten widely shared indicator strategies, **each as its own bot**,
for side-by-side testing on a demo account. They all use the same rules so the comparison is
fair:

- **Timeframes:** H4, H1, M30 and M15, with the same rules as the CRT bots. H4 is checked
  first, one trade per symbol, and an opposite higher-timeframe signal closes the trade and
  enters. The dashboard's *bot × timeframe* table shows which timeframe works.
- **Signals:** read on each closed candle, entered within 5 minutes of the close.
- **Risk:** SL = 1.5 × ATR(14); TP = 2 × the SL distance (Bollinger targets the middle band).
  No break-even.

| Bot | Rule |
| --- | ---- |
| `ema2050` EMA 20/50 cross | EMA 20 crosses EMA 50 |
| `ema921` EMA 9/21 + 200 trend | EMA 9 crosses EMA 21, only in the direction of EMA 200 |
| `golden` Golden / death cross | SMA 50 crosses SMA 200 |
| `macd` MACD + 200 EMA | MACD crosses its signal below zero (buy) / above zero (sell), with the EMA 200 trend |
| `rsi` RSI 30/70 reversal | RSI(14) crosses back above 30 / below 70 |
| `bbands` Bollinger reversion | Close back inside Bollinger(20, 2) after closing outside → middle band |
| `supertrend` Supertrend flip | Supertrend(10, 3) changes direction |
| `donchian` Donchian breakout | Close above the previous 20-candle high / below the low |
| `stoch` Stochastic + 200 EMA | %K crosses %D below 20 / above 80, with the EMA 200 trend |
| `ichimoku` Ichimoku TK cross | Tenkan crosses Kijun with price on the right side of the cloud |

Turn them all on with `IND_ALL_ENABLED=true`, or one at a time with `IND_<KEY>_ENABLED=true`
(e.g. `IND_MACD_ENABLED`). Per-bot settings: `IND_<KEY>_SYMBOLS`, `_TFS`, `_LOT`, `_SL_ATR`,
`_RR`, `_MAGIC_BASE` (default 801000, 802000, … 810000), `_MAX_SIGNAL_AGE`, `_MIN_SL_SPREADS`,
`_POLL_INTERVAL`. Start and stop each one with `/bots/<key>/start|stop`.

The dashboard's **leaderboard** ranks every bot, and each bot card has its own equity curve.
Judge them on average R over 100+ trades, not on win rate or the first few weeks.

### Threads and locking

Every bot runs in its own thread, plus one journal-sync thread. Two locks keep that safe:

- **`MT5_LOCK`** (`mt5_guard.py`). The MetaTrader5 package isn't reliably thread-safe, so a bot
  holds this lock while it processes one symbol (milliseconds), and the sync thread holds it
  while reading MT5 history. Bots still run in parallel, but MT5 calls never overlap.
- **The journal lock** (`trade_db.py`). One SQLite connection, one re-entrant lock, so there
  is a single writer.
- **Lock order is always MT5 lock → journal lock.** Nothing calls MT5 while holding the
  journal lock, so the two can't deadlock. The dashboard reads only the snapshot file and never
  touches either lock.

**Hedging account required.** Several bots trade the same symbols, so each needs its own
position. On a **netting** account MT5 keeps one position per symbol and the bots would
merge into each other's trades. At start (and again once MT5 is logged in), every bot checks
the account type and refuses to trade on netting, recording the reason in its events. Set
`BOTS_ALLOW_NETTING=true` only if you run a single bot per symbol.

**Position size: 0.5 % of the balance per trade.** Every bot sizes its lot so that hitting the
SL loses `BOTS_RISK_PCT` % (default `0.5`) of the account balance, so a 5-pip EURUSD stop and a
$10 gold stop risk the same money and the bots' results are comparable in $ as well as R.

| Balance | Risk | Trade | Lot |
| ------- | ---- | ----- | --- |
| $10,000 | $50 | EURUSD, SL 20 pips ($200 per lot) | 0.25 |
| $10,000 | $50 | XAUUSD, SL $5 ($500 per lot) | 0.10 |

The lot is rounded **down** to the broker's volume step, so the risk never exceeds the target.
The one exception is the broker's minimum lot (usually 0.01): if even that risks more than
`BOTS_RISK_MAX_OVER` × the target (default 1.5), the trade is skipped and shows up in the
dashboard's skip reasons. Set `BOTS_RISK_PCT=0` to go back to each bot's fixed `*_LOT`.

Tested with all 13 bots polling 25× faster than live, plus 8 extra journal writers, for 20 s:
0 overlapping MT5 calls, 0 database errors, no deadlock. With the MT5 lock disabled, the same
test showed ~17,000 overlapping calls.

## Bot Code Layout

All bot logic lives in `backend/mt5/app/`:

| File | What it holds |
| ---- | ------------- |
| `bot_common.py` | Shared base for every bot: the `Bot` class (poll loop, H4 → M15 priority, one trade per symbol, higher-timeframe override), order placement with every filter (price, min SL, commission + spread, reward:risk, stops level), break-even mechanics, closing positions, and the commission/spread helpers |
| `trade_db.py` | SQLite trade journal (see below) |
| `crt_bot.py` | 3-candle CRT strategy only: the signal, SL/TP, break-even trigger |
| `candle_two_bot.py` | 2-candle CRT strategy only |
| `daily_sweep_bot.py` | Daily sweep strategy only |
| `indicator_bots.py` | The 10 indicator strategies |
| `mt5_guard.py` | The lock that serializes MT5 calls across bot threads |
| `risk_guard.py` | Account-wide limits for all bots: max daily loss (closes everything and pauses until the next day) and max open trades |

The bots don't use the API helpers in `lib.py` or `routes/`; `routes/bot.py` only exposes
start/stop/status.

### Adding a new strategy

1. Create `app/my_bot.py` with:
   - `check(symbol, tf_name, running)`: look for a setup; when there is one, compute
     SL/TP and call `BOT.place(...)`, returning its result.
   - `be_trigger(pos)`: the price at which break-even should trigger for an open position,
     or `None`.
2. Create the bot with a **unique** name, comment prefix and magic base, for example:
   `Bot(name="fvg1", label="FVG", prefix="FVG ", title="FVG retest", description="...")`
   with `magic_base` 790000 in its settings.
3. Call its start function from `app.py`.

It is journaled automatically and appears on the dashboard with its own color, stats and
settings. No dashboard changes are needed.

## Trade Journal (SQLite)

`trade_db.py` records everything the bots do in `config/data/bots.db` on the VPS, in full
detail. It grows by roughly 60 MB a year, almost all of it raw events.

| Table | Contents |
| ----- | -------- |
| `trades` | One row per position: bot, symbol, timeframe, side, volume, entry, initial SL, TP, money at risk, spread at entry, break-even trigger/SL/time, exit price/time/reason, profit, commission, swap, net, R (net ÷ money at risk) |
| `events` | Every signal, skip (with reason), open, rejection, override close and break-even, with full details |
| `event_counts` | The same events counted per day × bot × symbol × timeframe × status × reason (numbers in reasons masked). Days older than 30 are merged into weeks |
| `bots` | Each registered bot: name, description, color slot, current settings, running/stopped |
| `meta` | Snapshot time, broker time offset |

- **Exit reasons:** `tp`, `sl`, `breakeven` (SL filled at or past entry), `override` (closed
  by a higher-timeframe signal), `manual`, `stopout`, `other`. They come from MT5's deal
  history, so they're correct even when the broker closes the trade.
- **History is imported.** On start, bot trades from the last `BOT_DB_BACKFILL_DAYS` (default
  90) of MT5 history are imported, using the magic numbers. Open bot positions the journal
  doesn't know about are adopted.
- **Sync.** Every `BOT_DB_SYNC_SECONDS` (default 30), closed positions are completed from deal
  history.
- **Dashboard snapshot.** `config/data/dashboard.db` holds only what the dashboard shows:
  trades (dashboard columns), `event_counts`, `bots` and `meta`. Raw events are left out. It
  is rewritten only when something changed: trade or bot changes within 30 s, new events at
  most every `BOT_DB_EVENT_SNAPSHOT_SECONDS` (default 300). A simulated year (4,000 trades,
  250,000 events) gives a 1.9 MB snapshot, **0.3 MB gzipped**.
- Query it directly on the VPS with `sqlite3 config/data/bots.db`, e.g.
  `SELECT bot, exit_reason, COUNT(*), ROUND(SUM(net), 2) FROM trades GROUP BY 1, 2;`

## Bot Dashboard

`http://your-server-ip:8080` shows, for any date range, bot, symbol and timeframe:

- net P/L, win rate (break-even excluded), average R, profit factor, open trades
- one card per bot: description, running/stopped with a **Start / Stop button**, headline
  stats, settings
- cumulative net profit per bot over time (hover for values; also as a table)
- how trades closed (TP / SL / break-even / override) per bot
- signals vs trades taken, and the most common reasons setups were skipped
- breakdown by bot × timeframe and by symbol, open trades, and the last 100 closed trades

**There is no API behind it.** The `dashboard` container (nginx) serves a static page and
the read-only `dashboard.db` snapshot. The page loads the database into the browser with
sql.js and runs the queries there. Only that one file is reachable; the rest of `./config`
is mounted read-only and not served.

**Start / Stop.** The button writes `run` or `stop` into `config/control/<bot name>` (nginx
`PUT`, same login as the page; the only folder it can write to). Each bot reads its file on
every poll, so it reacts within seconds, and the card shows "Stopping…" until the next
snapshot confirms it (about 30 s).

- **Stop** = no new trades. Trades already open keep running to their SL / TP, and
  break-even still applies. To close them, close them in MT5.
- **The choice survives restarts**: a bot stopped from the dashboard stays stopped after a
  rebuild, even if its `*_ENABLED=true`. Delete `config/control/<bot name>` to go back to the
  `.env` setting.
- A start is refused on a netting account, as at boot; the reason is in the bot's events.

**Account limits (prop-firm rules).** The *Account limits* card sets two limits for **all bots
together** (`risk_guard.py`). 0 = off.

| Limit | What happens |
| ----- | ------------ |
| **Max daily loss** (account currency) | Counts every bot trade closed today plus the floating P/L of open ones, with commission and swap. When it reaches −limit, **every open bot trade is closed** and no bot opens a new one until the next broker day (broker midnight). |
| **Max open trades** | A new trade is skipped while this many bot trades are open. A higher-TF override that replaces a trade still goes ahead. |

- Checked every 2 seconds (`BOTS_GUARD_INTERVAL`). Manual trades are ignored, both in the
  count and when closing.
- Saved to `config/control/_limits`; the bots apply it within seconds. Until it's set on
  the dashboard, `BOTS_MAX_DAILY_LOSS` and `BOTS_MAX_OPEN_TRADES` in `.env` apply.
- A halt lasts until the next broker day. Raising the limit doesn't lift it; setting it to
  0 does. After a restart the halt comes back if today's closed trades already lost the limit.
- Skipped entries show in the skip reasons ("daily loss limit hit", "max open trades
  reached"), and trades closed by the halt show as exit reason "Daily loss limit".
- The `/bots/<name>/start|stop` API routes still work, and the latest action (API or
  button) wins until the button is pressed again.

**Data transferred:**

| What | Size | When |
| ---- | ---- | ---- |
| Page | ~12 KB gzipped | Each visit (revalidated) |
| sql.js from cdnjs | ~370 KB | First visit only, then cached by the browser |
| Snapshot | ~0.3 MB gzipped after a year | Only when it changed |
| Check for changes | ~200 bytes (304) | Every minute while the tab is open |

Skip and signal counts on the dashboard refresh every 5 minutes; trades within a minute.
For ranges over 30 days, event counts are rounded to whole weeks.

Set the login in `.env`. The container refuses to start without it:

```env
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=a-long-password
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
