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

## Quick Start

### Prerequisites

- Docker & Docker Compose
- A MetaTrader 5 broker account

### Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/Mr4rogrammer/MT5-RiskManager
   cd metatrader5-server-python
   ```

2. Create your environment file:

   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

3. Start the container:

   ```bash
   docker-compose up -d
   ```

4. Verify it is running:

   ```bash
   docker-compose ps
   ```

## Endpoints

| Port | Service         |
| ---- | --------------- |
| 3000 | MT5 VNC Web UI  |
| 5001 | MT5 Flask API   |

## API Documentation

- **Swagger UI**: `http://localhost:5001/apidocs/`

### API Endpoints

| Method | Endpoint                    | Description                          |
| ------ | --------------------------- | ------------------------------------ |
| GET    | `/health`                   | Health check (public)                |
| GET    | `/symbol_info_tick/<symbol>` | Latest tick (bid/ask/volume)        |
| GET    | `/symbol_info/<symbol>`      | Full symbol metadata                |
| GET    | `/fetch_data_pos`            | OHLCV bars from current position    |
| GET    | `/fetch_data_range`          | OHLCV bars within a date range      |
| POST   | `/order`                     | Place a market order                |
| POST   | `/close_position`            | Close a specific position           |
| POST   | `/close_all_positions`       | Close all positions (filterable)    |
| POST   | `/modify_sl_tp`              | Modify SL/TP for a position         |
| GET    | `/get_positions`             | List open positions                 |
| GET    | `/positions_total`           | Count open positions                |
| GET    | `/get_deal_from_ticket`      | Deal details by ticket              |
| GET    | `/get_order_from_ticket`     | Order details by ticket             |
| GET    | `/history_deals_get`         | Deals in date range for a position  |
| GET    | `/history_orders_get`        | Order history by ticket             |
| GET    | `/last_error`                | MT5 last error code + message       |
| GET    | `/last_error_str`            | MT5 last error as string            |

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

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
