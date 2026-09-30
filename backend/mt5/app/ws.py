"""
WebSocket server — /ws endpoint.

Supports three real-time data streams:
  • tick      – latest MT5 tick for each subscribed symbol
  • account   – periodic MT5 account snapshot
  • positions – all open positions (polled via existing get_positions() from lib.py)

Architecture:
  ONE tick-polling thread      → broadcasts to all subscribed clients
  ONE account-polling thread   → broadcasts to all connected clients
  ONE positions-polling thread → broadcasts to all connected clients

Client subscribe message:
  {"action": "subscribe", "symbols": ["EURUSD", "XAUUSD"], "events": ["tick", "account", "positions"]}

  • symbols  – list of symbols to receive tick updates for (optional)
  • events   – list of event types to receive ("tick" | "account" | "positions") (optional)
              If omitted entirely, tick + account + positions are all enabled.

Outgoing message shapes:
  {"type": "tick",      "timestamp": <unix>, "data": {"symbol": ..., "bid": ..., "ask": ..., "time": ...}}
  {"type": "account",   "timestamp": <unix>, "data": {"balance": ..., "equity": ..., "profit": ...,
                                                        "margin": ..., "freeMargin": ...}}
  {"type": "positions", "timestamp": <unix>, "data": [{ <position fields> }, ...]}
"""

import json
import logging
import os
import threading
import time

import MetaTrader5 as mt5
from flask_sock import Sock
from lib import get_positions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (override with env vars)
# ---------------------------------------------------------------------------
TICK_INTERVAL: float = float(os.environ.get("TICK_INTERVAL", "0.2"))
ACCOUNT_INTERVAL: float = float(os.environ.get("ACCOUNT_INTERVAL", "2.0"))
POSITIONS_INTERVAL: float = float(os.environ.get("POSITIONS_INTERVAL", "2.0"))
# How often (seconds) to ping each client to detect dead connections
HEARTBEAT_INTERVAL: float = float(os.environ.get("WS_HEARTBEAT_INTERVAL", "15.0"))

# ---------------------------------------------------------------------------
# Client registry
# ---------------------------------------------------------------------------
# Each entry: {"ws": <Sock ws>, "symbols": set(), "events": set(), "lock": Lock()}
_clients: list[dict] = []
_clients_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Shared MT5 tick cache: symbol -> latest tick payload (dict)
# ---------------------------------------------------------------------------
_tick_cache: dict[str, dict] = {}
_tick_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _send_safe(client: dict, payload: dict) -> bool:
    """
    Send a JSON message to a single client.
    Returns False if the send failed (connection dead).
    """
    try:
        client["ws"].send(json.dumps(payload))
        return True
    except Exception:
        return False


def _make_tick_payload(symbol: str, tick) -> dict:
    return {
        "type": "tick",
        "timestamp": int(time.time()),
        "data": {
            "symbol": symbol,
            "bid": tick.bid,
            "ask": tick.ask,
            "time": tick.time,
        },
    }


def _make_account_payload(info) -> dict:
    return {
        "type": "account",
        "timestamp": int(time.time()),
        "data": {
            "balance": info.balance,
            "equity": info.equity,
            "profit": info.profit,
            "margin": info.margin,
            "freeMargin": info.margin_free,
        },
    }


def _make_positions_payload(positions_df) -> dict:
    """
    Convert the DataFrame returned by get_positions() into a WebSocket payload.

    Each position dict contains all the raw MT5 fields from lib.get_positions():
      ticket, time, time_msc, time_update, time_update_msc,
      type (0=BUY, 1=SELL), magic, identifier, reason,
      volume, price_open, sl, tp, price_current,
      swap, profit, symbol, comment, external_id
    """
    if positions_df is None or positions_df.empty:
        records = []
    else:
        # Replace NaN with None so json.dumps doesn't produce 'NaN' literals
        records = positions_df.where(positions_df.notna(), other=None).to_dict(
            orient="records"
        )
    return {
        "type": "positions",
        "timestamp": int(time.time()),
        "data": records,
    }


def _remove_client(ws) -> None:
    """Remove a client from the registry by its ws object."""
    with _clients_lock:
        global _clients
        _clients = [c for c in _clients if c["ws"] is not ws]
    logger.info("WebSocket client removed. Active clients: %d", len(_clients))


# ---------------------------------------------------------------------------
# Background: tick polling loop (ONE loop for all clients)
# ---------------------------------------------------------------------------

def _tick_loop() -> None:
    """
    Continuously poll MT5 for ticks of all currently-subscribed symbols,
    update the shared cache, and broadcast to subscribed clients.
    Runs in a single background daemon thread.
    """
    logger.info("Tick polling loop started (interval=%.2fs)", TICK_INTERVAL)
    while True:
        try:
            # 1. Collect the union of all subscribed symbols
            with _clients_lock:
                clients_snapshot = list(_clients)

            needed_symbols: set[str] = set()
            for client in clients_snapshot:
                if "tick" in client["events"]:
                    needed_symbols.update(client["symbols"])

            # 2. Fetch ticks from MT5 (once per symbol)
            fresh: dict[str, dict] = {}
            for symbol in needed_symbols:
                tick = mt5.symbol_info_tick(symbol)
                if tick is not None:
                    fresh[symbol] = _make_tick_payload(symbol, tick)

            # 3. Update cache
            with _tick_cache_lock:
                _tick_cache.update(fresh)

            # 4. Broadcast to clients — collect dead ones
            dead = []
            for client in clients_snapshot:
                if "tick" not in client["events"]:
                    continue
                for symbol in client["symbols"]:
                    if symbol not in fresh:
                        continue
                    if not _send_safe(client, fresh[symbol]):
                        dead.append(client["ws"])
                        break  # no point sending more to a dead connection

            # 5. Remove dead clients
            if dead:
                with _clients_lock:
                    global _clients
                    _clients = [c for c in _clients if c["ws"] not in dead]
                logger.warning(
                    "Removed %d dead client(s) during tick broadcast.", len(dead)
                )

        except Exception as exc:
            logger.exception("Unexpected error in tick loop: %s", exc)

        time.sleep(TICK_INTERVAL)


# ---------------------------------------------------------------------------
# Background: account polling loop (ONE loop for all clients)
# ---------------------------------------------------------------------------

def _account_loop() -> None:
    """
    Periodically fetch MT5 account info and broadcast to all clients
    that have subscribed to the 'account' event.
    Runs in a single background daemon thread.
    """
    logger.info("Account polling loop started (interval=%.2fs)", ACCOUNT_INTERVAL)
    while True:
        try:
            info = mt5.account_info()
            if info is not None:
                payload = _make_account_payload(info)

                dead = []
                with _clients_lock:
                    clients_snapshot = list(_clients)

                for client in clients_snapshot:
                    if "account" not in client["events"]:
                        continue
                    if not _send_safe(client, payload):
                        dead.append(client["ws"])

                if dead:
                    with _clients_lock:
                        global _clients
                        _clients = [c for c in _clients if c["ws"] not in dead]
                    logger.warning(
                        "Removed %d dead client(s) during account broadcast.", len(dead)
                    )

        except Exception as exc:
            logger.exception("Unexpected error in account loop: %s", exc)

        time.sleep(ACCOUNT_INTERVAL)


# ---------------------------------------------------------------------------
# Background: positions polling loop (ONE loop for all clients)
# ---------------------------------------------------------------------------

def _positions_loop() -> None:
    """
    Periodically call get_positions() (from lib.py) and broadcast the full list
    of open positions to all clients subscribed to the 'positions' event.
    Runs in a single background daemon thread.
    """
    logger.info("Positions polling loop started (interval=%.2fs)", POSITIONS_INTERVAL)
    while True:
        try:
            positions_df = get_positions()          # reuses existing lib.py function
            payload = _make_positions_payload(positions_df)

            dead = []
            with _clients_lock:
                clients_snapshot = list(_clients)

            for client in clients_snapshot:
                if "positions" not in client["events"]:
                    continue
                if not _send_safe(client, payload):
                    dead.append(client["ws"])

            if dead:
                with _clients_lock:
                    global _clients
                    _clients = [c for c in _clients if c["ws"] not in dead]
                logger.warning(
                    "Removed %d dead client(s) during positions broadcast.", len(dead)
                )

        except Exception as exc:
            logger.exception("Unexpected error in positions loop: %s", exc)

        time.sleep(POSITIONS_INTERVAL)


# ---------------------------------------------------------------------------
# Background thread launcher (called once from app startup)
# ---------------------------------------------------------------------------

_background_started = False
_background_lock = threading.Lock()


def start_background_loops() -> None:
    """
    Start the tick and account polling threads.
    Safe to call multiple times — threads are only created once.
    """
    global _background_started
    with _background_lock:
        if _background_started:
            return
        _background_started = True

    t_tick = threading.Thread(target=_tick_loop, daemon=True, name="ws-tick-loop")
    t_account = threading.Thread(
        target=_account_loop, daemon=True, name="ws-account-loop"
    )
    t_positions = threading.Thread(
        target=_positions_loop, daemon=True, name="ws-positions-loop"
    )
    t_tick.start()
    t_account.start()
    t_positions.start()
    logger.info("WebSocket background loops started.")


# ---------------------------------------------------------------------------
# Flask-Sock WebSocket handler
# ---------------------------------------------------------------------------

# Sock instance is created here but attached to the Flask app inside
# register_ws() so that app.py controls the app object.
sock = Sock()


def register_ws(app) -> None:
    """
    Attach flask-sock to the Flask app and register the /ws endpoint.
    Call this from app.py after creating `app`.
    """
    sock.init_app(app)
    start_background_loops()


@sock.route("/ws")
def ws_handler(ws) -> None:
    """
    Single WebSocket endpoint. Handles one client for the lifetime of the
    connection.  Reads subscribe messages; sends initial snapshots; then
    blocks on heartbeat pings while the background threads push data.
    """
    logger.info("WebSocket client connected.")

    # Register client with empty subscriptions
    client: dict = {
        "ws": ws,
        "symbols": set(),
        # Default: subscribe to nothing until the client sends a subscribe msg.
        # If the client omits 'events', we default to both after subscription.
        "events": set(),
    }

    with _clients_lock:
        _clients.append(client)

    try:
        # ----------------------------------------------------------------
        # Message receive loop + heartbeat
        # ----------------------------------------------------------------
        last_heartbeat = time.time()

        while True:
            # Non-blocking receive with a short timeout so we can send pings
            try:
                # flask-sock's receive has a timeout parameter (seconds).
                # We use HEARTBEAT_INTERVAL so we can ping periodically.
                raw = ws.receive(timeout=HEARTBEAT_INTERVAL)
            except Exception:
                # Timeout or connection closed
                break

            if raw is None:
                # Connection closed by client
                break

            # ---- Handle incoming message --------------------------------
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON WebSocket message: %r", raw)
                continue

            action = msg.get("action")

            if action == "subscribe":
                _handle_subscribe(ws, client, msg)

            elif action == "unsubscribe":
                _handle_unsubscribe(client, msg)

            elif action == "pong":
                # Client responded to our ping — connection is alive
                last_heartbeat = time.time()

            else:
                logger.debug("Unknown WebSocket action: %r", action)

            # ---- Send heartbeat ping if needed --------------------------
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                if not _send_safe(client, {"type": "ping"}):
                    break
                last_heartbeat = now

    except Exception as exc:
        logger.exception("WebSocket handler error: %s", exc)

    finally:
        _remove_client(ws)
        logger.info("WebSocket connection closed.")


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe helpers
# ---------------------------------------------------------------------------

def _handle_subscribe(ws, client: dict, msg: dict) -> None:
    """
    Process a subscribe message from a client.
    Immediately sends current account snapshot, latest ticks, and open positions.
    """
    symbols = [str(s).upper() for s in msg.get("symbols", [])]
    events_raw = msg.get("events", None)

    # If 'events' is omitted, default to all three streams
    if events_raw is None:
        events = {"tick", "account", "positions"}
    else:
        events = {str(e).lower() for e in events_raw}

    client["symbols"].update(symbols)
    client["events"].update(events)

    logger.info(
        "Client subscribed — symbols=%s events=%s", client["symbols"], client["events"]
    )

    # ---- Send immediate snapshots ----------------------------------------

    # Account snapshot (if subscribed)
    if "account" in client["events"]:
        try:
            info = mt5.account_info()
            if info is not None:
                _send_safe(client, _make_account_payload(info))
        except Exception as exc:
            logger.warning("Could not send initial account snapshot: %s", exc)

    # Positions snapshot (if subscribed)
    if "positions" in client["events"]:
        try:
            positions_df = get_positions()
            _send_safe(client, _make_positions_payload(positions_df))
        except Exception as exc:
            logger.warning("Could not send initial positions snapshot: %s", exc)

    # Tick snapshots for newly subscribed symbols
    if "tick" in client["events"]:
        for symbol in symbols:
            # Try cache first, fall back to live fetch
            with _tick_cache_lock:
                cached = _tick_cache.get(symbol)

            if cached:
                _send_safe(client, cached)
            else:
                try:
                    tick = mt5.symbol_info_tick(symbol)
                    if tick is not None:
                        payload = _make_tick_payload(symbol, tick)
                        with _tick_cache_lock:
                            _tick_cache[symbol] = payload
                        _send_safe(client, payload)
                except Exception as exc:
                    logger.warning(
                        "Could not send initial tick for %s: %s", symbol, exc
                    )


def _handle_unsubscribe(client: dict, msg: dict) -> None:
    """Remove symbols and/or events from a client's subscription."""
    symbols = [str(s).upper() for s in msg.get("symbols", [])]
    events = [str(e).lower() for e in msg.get("events", [])]

    client["symbols"].difference_update(symbols)
    client["events"].difference_update(events)

    logger.info(
        "Client unsubscribed — symbols=%s events=%s", client["symbols"], client["events"]
    )
