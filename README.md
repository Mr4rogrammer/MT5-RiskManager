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
M15, M30, H1 and H4, checked every few seconds so each 15-minute close is caught.

- **C1** = range candle, **C2** = the candle that just closed
- **SELL** when C2 sweeps above C1 high and closes back inside C1. SL = C2 high
- **BUY** when C2 sweeps below C1 low and closes back inside C1. SL = C2 low
- **TP** = 50% of C1 range. Trades close on SL/TP
- Setups where price is already beyond SL/TP are skipped
- Setups with reward:risk below `CRT_MIN_RR` (default 1.0), or a TP distance not above the spread, are skipped
- Each timeframe uses its own magic number (`CRT_MAGIC_BASE` + minutes: 770015, 770030, 770060, 770240)

The bot is **off by default**. Set `CRT_ENABLED=true` in `.env` or call:

| Method | Endpoint      | Description                                   |
| ------ | ------------- | --------------------------------------------- |
| GET    | `/bot/status` | Enabled flag, settings, last 100 signals/orders |
| POST   | `/bot/start`  | Start trading                                 |
| POST   | `/bot/stop`   | Stop trading (open trades keep SL/TP)         |

Settings (`.env`): `CRT_SYMBOLS`, `CRT_LOT`, `CRT_DEVIATION`, `CRT_MAGIC_BASE`,
`CRT_POLL_INTERVAL`, `CRT_MAX_SIGNAL_AGE`, `CRT_MIN_RR`. If your broker uses suffixed symbol
names (e.g. `XAUUSDm`), list them exactly in `CRT_SYMBOLS`.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
