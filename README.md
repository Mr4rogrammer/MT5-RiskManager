# MetaTrader5 Quant Server (Python)

A quantitative trading server built with Python that integrates MetaTrader 5 with Django and Flask, featuring automated trading algorithms, real-time position management, and comprehensive monitoring.

## Author

**Krishna Kumar Eswaran** — [krish110222002@gmail.com](mailto:krish110222002@gmail.com)

## Architecture

The project consists of the following components:

- **MT5 Flask API** — A Flask server running inside Wine that communicates directly with MetaTrader 5 via its Python API, exposing REST endpoints for data retrieval, order execution, position management, and trade history.
- **Django Backend** — A Django REST Framework application that manages trade records, close price mutations, and orchestrates quantitative trading algorithms (mean reversion entry, trailing stops, close detection) via Celery task scheduling.
- **PostgreSQL** — Database for persisting trade records and mutations.
- **Redis** — Message broker for Celery workers and task scheduling.
- **Celery / Celery Beat** — Asynchronous task execution and periodic scheduling for trading algorithms.
- **Monitoring Stack** — Grafana, Prometheus, Loki, Promtail, cAdvisor, Node-Exporter, and Alertmanager for full observability.
- **Traefik** — Reverse proxy with automatic HTTPS via Let's Encrypt.

## Features

- **Mean Reversion Algorithm** — Bollinger Band-based mean reversion entry signals across configurable trading pairs.
- **Trailing Stop Management** — Multi-step trailing stop algorithm that dynamically adjusts stop-loss levels as profit increases.
- **Trade Lifecycle Tracking** — Automatic detection of closed trades with database persistence of entry/exit details, PnL, and commissions.
- **REST API** — Full CRUD access to trade history with filtering, pagination, and ordering via Django REST Framework.
- **Swagger Documentation** — Auto-generated API documentation for the MT5 Flask API.
- **Monitoring & Alerting** — Pre-configured Grafana dashboards for node metrics, container metrics, and log search with Prometheus alerting rules.

## Quick Start

### Prerequisites

- Docker & Docker Compose
- A MetaTrader 5 broker account

### Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/Mr4rogrammer/MT5-RiskManager
   cd metatrader5-quant-server-python
   ```

2. Create your environment file:

   ```bash
   cp .env.example .env
   # Edit .env with your broker credentials and domain settings
   ```

3. Create the Traefik network:

   ```bash
   docker network create traefik-public
   ```

4. Start the stack:

   ```bash
   docker-compose up -d
   ```

5. Verify all containers are running:

   ```bash
   docker-compose ps
   ```

## Services & Endpoints

| Service         | Internal URL              | External Port |
| --------------- | ------------------------- | ------------- |
| MT5 VNC         | http://mt5:3000           | via Traefik   |
| MT5 Flask API   | http://mt5:5001           | via Traefik   |
| Django API      | http://django:8000        | 8000          |
| Grafana         | http://grafana:3000       | 3000          |
| Prometheus      | http://prometheus:9090    | 9090          |
| Alertmanager    | http://alertmanager:9093  | 9093          |
| Loki            | http://loki:3100          | 3100          |
| Redis           | http://redis:6379         | 6379          |

## API Documentation

- **MT5 Flask API Swagger**: `https://<API_DOMAIN>/apidocs/`
- **Django REST API**: `https://<DJANGO_DOMAIN>/v1/`
- **Django Admin**: `https://<DJANGO_DOMAIN>/admin/`

## Project Structure

```
├── backend/
│   ├── django/                    # Django backend
│   │   ├── app/
│   │   │   ├── nexus/             # Trade models, views, serializers
│   │   │   ├── quant/             # Trading algorithms & indicators
│   │   │   │   ├── algorithms/
│   │   │   │   │   ├── mean_reversion/  # Entry, trailing stop, config
│   │   │   │   │   └── close/           # Close detection algorithm
│   │   │   │   └── indicators/          # Technical indicators
│   │   │   └── utils/             # Utilities (API clients, DB ops, math)
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── mt5/                       # MT5 Flask server (runs in Wine)
│       ├── app/
│       │   ├── routes/            # Flask API routes
│       │   ├── app.py             # Flask application entry point
│       │   ├── lib.py             # MT5 helper functions
│       │   └── constants.py       # MT5 constants & enums
│       ├── scripts/               # Setup & startup scripts
│       └── Dockerfile
├── monitoring/                    # Monitoring stack configs & dashboards
│   ├── configs/                   # Grafana, Prometheus, Loki, etc.
│   └── dashboards/                # Pre-built Grafana dashboards
├── docker-compose.yml
├── .env.example
├── LICENSE
└── README.md
```

## Trading Algorithm Configuration

Edit `backend/django/app/quant/algorithms/mean_reversion/config.py` to customize:

- **Trading pairs** — Symbols to trade
- **Timeframe** — Chart timeframe for signal generation
- **Capital per trade** — USD amount per position
- **Leverage** — Trading leverage
- **TP/SL multipliers** — Take profit and stop loss as a ratio of capital
- **Trailing stop steps** — Multi-level trailing stop configuration

## Monitoring

The monitoring stack provides:

- **Node Metrics Dashboard** — CPU, memory, disk, and network metrics
- **Container Metrics Dashboard** — Docker container resource usage
- **Log Search Dashboard** — Centralized log search across all services
- **Alerting Rules** — Pre-configured Prometheus alerts with Alertmanager routing

Access Grafana at `http://localhost:3000` with default anonymous admin access.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
