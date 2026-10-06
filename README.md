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
| `CRT_LOT` | `0.01` | Fixed lot size (normalized to the broker's volume step/min/max) |
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
| `CRT2_LOT` | `0.01` | Fixed lot size |
| `CRT2_DEVIATION` | `20` | Maximum slippage in points |
| `CRT2_MAGIC_BASE` | `780000` | Base for the magic number of each timeframe |
| `CRT2_POLL_INTERVAL` | `2` | Seconds between checks |
| `CRT2_MIN_RR` | `0` | Minimum reward:risk (`0` disables) |
| `CRT2_SL_BUFFER_SPREADS` | `0` | Extra SL room beyond the sweep, in multiples of the spread |
| `CRT2_MIN_SL_SPREADS` | `3` | Skip setups whose SL is closer than N × spread (`0` disables) |
| `CRT2_BE_TRIGGER` | `0.45` | How far into C1's range (from the swept side) break-even triggers |

## Bot Code Layout

All bot logic lives in `backend/mt5/app/`:

| File | What it holds |
| ---- | ------------- |
| `bot_common.py` | Shared base for every bot: the `Bot` class (poll loop, H4 → M15 priority, one trade per symbol, higher-timeframe override), order placement with every filter (price, min SL, commission + spread, reward:risk, stops level), break-even mechanics, closing positions, and the commission/spread helpers |
| `trade_db.py` | SQLite trade journal (see below) |
| `crt_bot.py` | 3-candle CRT strategy only: the signal, SL/TP, break-even trigger |
| `candle_two_bot.py` | 2-candle CRT strategy only |

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

`trade_db.py` records everything the bots do in `config/data/bots.db` on the VPS. The
file stays small: a year of trades is a few MB.

| Table | Contents |
| ----- | -------- |
| `trades` | One row per position: bot, symbol, timeframe, side, volume, entry, initial SL, TP, money at risk, spread at entry, break-even trigger/SL/time, exit price/time/reason, profit, commission, swap, net, R (net ÷ money at risk) |
| `events` | Every signal, skip (with reason), open, rejection, override close and break-even |
| `bots` | Each registered bot: name, description, color slot, current settings, running/stopped |
| `meta` | Snapshot time, broker time offset |

- **Exit reasons:** `tp`, `sl`, `breakeven` (SL filled at or past entry), `override` (closed
  by a higher-timeframe signal), `manual`, `stopout`, `other`. They come from MT5's deal
  history, so they're correct even when the broker closes the trade.
- **History is imported.** On start, bot trades from the last `BOT_DB_BACKFILL_DAYS` (default
  90) of MT5 history are imported, using the magic numbers. Open bot positions the journal
  doesn't know about are adopted.
- **Sync.** Every `BOT_DB_SYNC_SECONDS` (default 30), closed positions are completed from deal
  history and a consistent copy is written to `config/data/dashboard.db`.
- Query it directly on the VPS with `sqlite3 config/data/bots.db`, e.g.
  `SELECT bot, exit_reason, COUNT(*), ROUND(SUM(net), 2) FROM trades GROUP BY 1, 2;`

## Bot Dashboard

`http://your-server-ip:8080` shows, for any date range, bot, symbol and timeframe:

- net P/L, win rate (break-even excluded), average R, profit factor, open trades
- one card per bot: description, running/stopped, headline stats, settings
- cumulative net profit per bot over time (hover for values; also as a table)
- how trades closed (TP / SL / break-even / override) per bot
- signals vs trades taken, and the most common reasons setups were skipped
- breakdown by bot × timeframe and by symbol, open trades, and the last 100 closed trades

**There is no API behind it.** The `dashboard` container (nginx) serves a static page and
the read-only `dashboard.db` snapshot. The page loads the database into the browser with
sql.js and runs the queries there, refreshing every minute. Only that one file is reachable;
the rest of `./config` is mounted read-only and not served.

Set the login in `.env`. The container refuses to start without it:

```env
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=a-long-password
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
