"""
CRT (Candle Range Theory) auto-trader.

Every POLL_INTERVAL seconds, for each symbol x timeframe (M15, M30, H1, H4),
look at the last two CLOSED candles:

  C1 = range candle
  C2 = candle that just closed

  Bearish CRT: C2 sweeps above C1 high, then closes back inside C1  -> SELL
  Bullish CRT: C2 sweeps below C1 low,  then closes back inside C1  -> BUY

  Entry : market order right after C2 closes
  SL    : C2's sweep extreme (C2 high for SELL, C2 low for BUY)
  TP    : 50% of C1 range

Trades are closed by their SL/TP on the broker side.

Each timeframe trades with its own magic number (CRT_MAGIC_BASE + minutes),
and every order comment is "CRT <TF> <C2 time>" so a setup is never traded twice.

Settings (env vars):
  CRT_ENABLED         true/false  start trading on boot (default false)
  CRT_SYMBOLS         comma list  (default XAUUSD,EURUSD,GBPUSD,AUDUSD,USDCHF,NZDUSD,USDCAD)
  CRT_LOT             fixed lot   (default 0.01)
  CRT_DEVIATION       max slippage in points (default 20)
  CRT_MAGIC_BASE      (default 770000)
  CRT_POLL_INTERVAL   seconds between checks (default 5)
  CRT_MAX_SIGNAL_AGE  ignore setups whose C2 closed more than N seconds ago (default 300)
"""

import logging
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

TIMEFRAMES = {
    "M15": (mt5.TIMEFRAME_M15, 15 * 60),
    "M30": (mt5.TIMEFRAME_M30, 30 * 60),
    "H1": (mt5.TIMEFRAME_H1, 60 * 60),
    "H4": (mt5.TIMEFRAME_H4, 4 * 60 * 60),
}

DEFAULT_SYMBOLS = "XAUUSD,EURUSD,GBPUSD,AUDUSD,USDCHF,NZDUSD,USDCAD"

# Populated by start_crt_bot() — env is read at start, after load_dotenv().
settings = {}

_enabled = threading.Event()
_started = False
_start_lock = threading.Lock()

# (symbol, tf_name) -> open time of the last C2 already evaluated
_last_bar = {}

# Recent signals / orders, newest last — exposed via /bot/status
events = deque(maxlen=100)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def detect_signal(c1, c2):
    """
    Return {"side", "sl", "tp"} if C2 forms a CRT setup against C1, else None.
    c1 / c2 are MT5 rate rows (anything indexable by high/low/close).
    """
    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    c2_high, c2_low, c2_close = float(c2["high"]), float(c2["low"]), float(c2["close"])

    # C2 must close back inside C1's range
    if not (c1_low < c2_close < c1_high):
        return None

    swept_high = c2_high > c1_high
    swept_low = c2_low < c1_low

    # No sweep, or swept both sides (no clear direction)
    if swept_high == swept_low:
        return None

    mid = (c1_high + c1_low) / 2

    if swept_high:
        return {"side": "SELL", "sl": c2_high, "tp": mid}
    return {"side": "BUY", "sl": c2_low, "tp": mid}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(symbol, tf_name, status, **details):
    event = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": symbol,
        "timeframe": tf_name,
        "status": status,
        **details,
    }
    events.append(event)
    logger.info("CRT %s %s: %s %s", symbol, tf_name, status, details)


def _filling_mode(info):
    # symbol_info.filling_mode is a bitmask: 1 = FOK allowed, 2 = IOC allowed
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _normalize_volume(volume, info):
    step = info.volume_step or 0.01
    volume = math.floor(volume / step + 1e-9) * step
    volume = max(info.volume_min, min(volume, info.volume_max))
    return round(volume, 8)


def _already_traded(symbol, magic, comment):
    positions = mt5.positions_get(symbol=symbol) or []
    return any(p.magic == magic and p.comment == comment for p in positions)


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _place(symbol, tf_name, c2_time, signal, tick):
    info = mt5.symbol_info(symbol)
    if info is None:
        _record(symbol, tf_name, "error", reason="symbol_info failed", mt5_error=str(mt5.last_error()))
        return

    side, sl, tp = signal["side"], signal["sl"], signal["tp"]

    if side == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
        valid = sl < price < tp
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
        valid = tp < price < sl

    if not valid:
        _record(symbol, tf_name, "skipped", side=side, reason="price already beyond SL/TP",
                price=price, sl=sl, tp=tp)
        return

    # Broker minimum distance for stops
    min_distance = info.trade_stops_level * info.point
    if abs(price - sl) < min_distance or abs(tp - price) < min_distance:
        _record(symbol, tf_name, "skipped", side=side, reason="SL/TP inside broker stops level",
                price=price, sl=sl, tp=tp)
        return

    magic = settings["magic_base"] + TIMEFRAMES[tf_name][1] // 60
    comment = f"CRT {tf_name} {c2_time}"

    if _already_traded(symbol, magic, comment):
        return

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": _normalize_volume(settings["lot"], info),
        "type": order_type,
        "price": price,
        "sl": round(sl, info.digits),
        "tp": round(tp, info.digits),
        "deviation": settings["deviation"],
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _filling_mode(info),
    }

    result = mt5.order_send(request)

    if result is None:
        _record(symbol, tf_name, "error", side=side, reason="order_send returned None",
                mt5_error=str(mt5.last_error()), request=request)
        return

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        _record(symbol, tf_name, "rejected", side=side, retcode=result.retcode,
                comment=result.comment, request=request)
        return

    _record(symbol, tf_name, "opened", side=side, ticket=result.order, volume=result.volume,
            price=result.price, sl=request["sl"], tp=request["tp"])


def _check(symbol, tf_name):
    tf, tf_seconds = TIMEFRAMES[tf_name]

    # pos 0 is the forming candle, so pos 1..2 are the last two closed ones
    rates = mt5.copy_rates_from_pos(symbol, tf, 1, 2)
    if rates is None or len(rates) < 2:
        return

    c1, c2 = rates[0], rates[1]
    c2_time = int(c2["time"])

    key = (symbol, tf_name)
    if _last_bar.get(key) == c2_time:
        return
    _last_bar[key] = c2_time

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return

    # Don't trade setups that closed long ago (e.g. right after bot start)
    if tick.time - (c2_time + tf_seconds) > settings["max_signal_age"]:
        return

    signal = detect_signal(c1, c2)
    if signal is None:
        return

    _record(symbol, tf_name, "signal", side=signal["side"],
            c1_high=float(c1["high"]), c1_low=float(c1["low"]),
            c2_high=float(c2["high"]), c2_low=float(c2["low"]), c2_close=float(c2["close"]))

    _place(symbol, tf_name, c2_time, signal, tick)


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

def _loop():
    logger.info("CRT bot loop started: symbols=%s timeframes=%s lot=%s",
                settings["symbols"], list(TIMEFRAMES), settings["lot"])
    while True:
        if _enabled.is_set():
            try:
                if mt5.terminal_info() is None:
                    mt5.initialize()
                    for symbol in settings["symbols"]:
                        mt5.symbol_select(symbol, True)

                for symbol in settings["symbols"]:
                    for tf_name in TIMEFRAMES:
                        try:
                            _check(symbol, tf_name)
                        except Exception:
                            logger.exception("CRT check failed for %s %s", symbol, tf_name)
            except Exception:
                logger.exception("CRT loop error")

        time.sleep(settings["poll_interval"])


def start_crt_bot():
    """Start the background thread once. Trading only runs while enabled."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    settings.update({
        "symbols": [s.strip() for s in os.environ.get("CRT_SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()],
        "lot": float(os.environ.get("CRT_LOT", "0.01")),
        "deviation": int(os.environ.get("CRT_DEVIATION", "20")),
        "magic_base": int(os.environ.get("CRT_MAGIC_BASE", "770000")),
        "poll_interval": float(os.environ.get("CRT_POLL_INTERVAL", "5")),
        "max_signal_age": int(os.environ.get("CRT_MAX_SIGNAL_AGE", "300")),
    })

    for symbol in settings["symbols"]:
        if not mt5.symbol_select(symbol, True):
            logger.warning("CRT: could not select symbol %s (check broker symbol name)", symbol)

    if os.environ.get("CRT_ENABLED", "false").lower() == "true":
        _enabled.set()

    threading.Thread(target=_loop, daemon=True, name="crt-bot").start()


def enable():
    _enabled.set()


def disable():
    _enabled.clear()


def is_enabled():
    return _enabled.is_set()
