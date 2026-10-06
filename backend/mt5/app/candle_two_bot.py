"""
2-candle CRT (Candle Range Theory) auto-trader.

Same idea as crt_bot.py, but the entry happens INSIDE C2 instead of at C3's open.

  C1 = range candle (last closed candle)
  C2 = candle currently forming — must open inside C1

  Bullish: C2 sweeps below C1.low, then price crosses back ABOVE C2.open  →  BUY
  Bearish: C2 sweeps above C1.high, then price crosses back BELOW C2.open →  SELL

  SL : C2's sweep extreme so far (C2.low for BUY, C2.high + spread for SELL),
       plus CRT2_SL_BUFFER_SPREADS × spread on both sides
  TP : opposite side of C1 (C1.high for BUY, C1.low for SELL)
  BE : SL moves to entry ± round-trip commission once price reaches
       CRT2_BE_TRIGGER (default 45 %) into C1's range, measured from the swept side:
         BUY  trigger = C1.low  + 45 % × C1 range
         SELL trigger = C1.high − 45 % × C1 range

Rules (same as crt_bot.py):
  • HTF priority  — H4 → H1 → M30 → M15; a trade on one TF skips lower TFs that cycle.
  • One trade per pair — while a CRT2 position is open, only HIGHER timeframes are
                         scanned. An OPPOSITE higher-TF signal closes it and enters;
                         a same-direction one is skipped.
  • One trade per C2 — a C2 candle is traded at most once.
  • Fresh cross only — the cross over C2.open must be seen between two polls no more
                       than a few poll intervals apart, so the bot never chases a
                       cross it missed (e.g. after a restart).
  • Filters       — min SL distance, commission + spread, min RR, broker stops level.

Both bots run at the same time and are independent: this one uses its own magic
numbers (CRT2_MAGIC_BASE + TF minutes) and the comment "CRT2 <TF> <C1 open time>".
Commission settings (CRT_COMMISSION_*) are shared with crt_bot.py.

Settings (env vars):
  CRT2_ENABLED            true/false   start trading on boot (default false)
  CRT2_SYMBOLS            comma list   (default: same as CRT_SYMBOLS)
  CRT2_LOT                fixed lot    (default 0.01)
  CRT2_DEVIATION          max slippage in points (default 20)
  CRT2_MAGIC_BASE         (default 780000)
  CRT2_POLL_INTERVAL      seconds between checks (default 2 — entries are intrabar)
  CRT2_MIN_RR             minimum reward:risk ratio (default 0, disabled)
  CRT2_SL_BUFFER_SPREADS  extra SL room beyond the sweep, × spread (default 0)
  CRT2_MIN_SL_SPREADS     minimum SL distance, × spread (default 3, 0 disables)
  CRT2_BE_TRIGGER         fraction into C1's range that triggers break-even (default 0.45)
"""

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import MetaTrader5 as mt5

import crt_bot
from crt_bot import (
    TIMEFRAMES, TF_RANK, DEFAULT_SYMBOLS,
    _filling_mode, _normalize_volume, _spread_cost_usd, _tp_profit_usd,
    _commission_usd, _position_side, _lib_close_position,
)

logger = logging.getLogger(__name__)

COMMENT_PREFIX = "CRT2 "

# Populated by start_candle_two_bot()
settings = {}

_enabled = threading.Event()
_started = False
_start_lock = threading.Lock()

# (symbol, tf_name) -> (c2_time, bid_was_above_open, unix time of observation)
_last_seen = {}

# (symbol, tf_name, c2_time) already traded — one trade per C2
_traded = set()

# ticket -> (BE trigger price) cache; rebuilt from the C1 candle after a restart
_be_level = {}

# ticket -> unix time before which a failed break-even modify is not retried
_be_retry_after = {}
BE_RETRY_SECONDS = 60

events = deque(maxlen=100)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(symbol, tf_name, status, **details):
    event = {
        "time":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol":    symbol,
        "timeframe": tf_name,
        "status":    status,
        **details,
    }
    events.append(event)
    logger.info("CRT2 %s %s: %s %s", symbol, tf_name, status, details)


def _open_positions(symbol):
    """Every CRT2-managed position currently open for this symbol."""
    positions = mt5.positions_get(symbol=symbol) or []
    return [p for p in positions if p.comment.startswith(COMMENT_PREFIX)]


def _position_tf(pos):
    minutes = pos.magic - settings["magic_base"]
    for tf_name, (_, tf_seconds) in TIMEFRAMES.items():
        if tf_seconds // 60 == minutes:
            return tf_name
    return None


def _be_trigger(c1_high, c1_low, side):
    """Price at which break-even triggers: CRT2_BE_TRIGGER into C1, from the swept side."""
    depth = (c1_high - c1_low) * settings["be_trigger"]
    return c1_low + depth if side == "BUY" else c1_high - depth


def _be_level_for(pos):
    """BE trigger for an open position; rebuilt from its C1 candle if not cached."""
    if pos.ticket in _be_level:
        return _be_level[pos.ticket]

    # Comment: "CRT2 <TF> <C1 open time>"
    parts   = pos.comment.split()
    tf_name = _position_tf(pos)
    if len(parts) < 3 or tf_name is None or not parts[2].isdigit():
        return None

    c1_time = datetime.fromtimestamp(int(parts[2]), timezone.utc)
    rates   = mt5.copy_rates_range(pos.symbol, TIMEFRAMES[tf_name][0], c1_time, c1_time)
    if rates is None or len(rates) == 0:
        return None

    level = _be_trigger(float(rates[0]["high"]), float(rates[0]["low"]),
                        _position_side(pos))
    _be_level[pos.ticket] = level
    return level


def _close_for_override(positions, tf_name):
    """Close lower-TF CRT2 positions a higher-TF signal overrides. True if all closed."""
    for pos in positions:
        info   = mt5.symbol_info(pos.symbol)
        result = _lib_close_position(
            pos._asdict(),
            deviation=settings["deviation"],
            magic=pos.magic,
            comment=f"CRT2 override {tf_name}",
            type_filling=_filling_mode(info) if info else mt5.ORDER_FILLING_IOC,
        )
        if result is None:
            _record(pos.symbol, _position_tf(pos), "error", ticket=pos.ticket,
                    reason=f"failed to close for {tf_name} override",
                    mt5_error=str(mt5.last_error()))
            return False
        _record(pos.symbol, _position_tf(pos), "closed", ticket=pos.ticket,
                side=_position_side(pos), reason=f"overridden by {tf_name} signal",
                price=result.price)
    return True


# ---------------------------------------------------------------------------
# Break-even management
# ---------------------------------------------------------------------------

def _manage_breakeven(symbol):
    for pos in _open_positions(symbol):
        entry, sl, tp = pos.price_open, pos.sl, pos.tp
        if tp == 0 or sl == 0:
            continue
        if time.time() < _be_retry_after.get(pos.ticket, 0):
            continue

        level = _be_level_for(pos)
        tick  = mt5.symbol_info_tick(symbol)
        info  = mt5.symbol_info(symbol)
        if level is None or tick is None or info is None:
            continue

        is_buy  = pos.type == mt5.POSITION_TYPE_BUY
        current = tick.bid if is_buy else tick.ask
        if (is_buy and current < level) or (not is_buy and current > level):
            continue

        commission = _commission_usd(symbol, pos.volume, entry, info)
        per_point  = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, pos.volume,
                                           entry, entry + info.point)
        offset = commission / per_point * info.point if per_point else 0.0
        new_sl = round(entry + offset if is_buy else entry - offset, info.digits)

        if (is_buy and sl >= new_sl) or (not is_buy and sl <= new_sl):
            continue   # already at (or past) break-even

        # SL must sit on the losing side of price, outside the stops/freeze level.
        # If entry is already past the 45 % level, this waits until price is far
        # enough beyond entry for the broker to accept the stop.
        min_dist = max(info.trade_stops_level, info.trade_freeze_level, 1) * info.point
        gap = current - new_sl if is_buy else new_sl - current
        if gap < min_dist:
            continue

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "symbol":   symbol,
            "sl":       new_sl,
            "tp":       tp,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            _be_retry_after.pop(pos.ticket, None)
            _record(symbol, _position_tf(pos), "breakeven",
                    ticket=pos.ticket, entry=entry, new_sl=new_sl)
        else:
            _be_retry_after[pos.ticket] = time.time() + BE_RETRY_SECONDS
            err = result.retcode if result else mt5.last_error()
            logger.warning("CRT2 BE failed: %s ticket=%s err=%s (retry in %ss)",
                           symbol, pos.ticket, err, BE_RETRY_SECONDS)


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _place(symbol, tf_name, c1, c2, side, tick, to_close=()):
    info = mt5.symbol_info(symbol)
    if info is None:
        _record(symbol, tf_name, "error",
                reason="symbol_info failed", mt5_error=str(mt5.last_error()))
        return False

    spread = tick.ask - tick.bid
    buffer = spread * settings["sl_buffer_spreads"]

    # MT5 candles are BID prices; a SELL's SL triggers on the ASK, so add the spread.
    if side == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price      = tick.ask
        sl         = float(c2["low"]) - buffer
        tp         = float(c1["high"])
        valid      = sl < price < tp
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price      = tick.bid
        sl         = float(c2["high"]) + spread + buffer
        tp         = float(c1["low"])
        valid      = tp < price < sl

    if not valid:
        _record(symbol, tf_name, "skipped", side=side,
                reason="price already beyond SL/TP", price=price, sl=sl, tp=tp)
        return False

    risk   = abs(price - sl)
    reward = abs(tp - price)
    rr     = reward / risk if risk > 0 else 0.0

    if risk < spread * settings["min_sl_spreads"]:
        _record(symbol, tf_name, "skipped", side=side,
                reason=f"SL distance below {settings['min_sl_spreads']}x spread",
                price=price, sl=sl, spread=round(spread, info.digits))
        return False

    volume          = _normalize_volume(settings["lot"], info)
    tp_profit       = _tp_profit_usd(side, symbol, volume, price, tp)
    spread_cost     = _spread_cost_usd(symbol, volume, tick)
    commission_cost = _commission_usd(symbol, volume, price, info)

    if tp_profit is not None and spread_cost is not None:
        min_profit = commission_cost + spread_cost
        if tp_profit <= min_profit:
            _record(symbol, tf_name, "skipped", side=side,
                    reason="TP profit below commission + spread",
                    tp_profit=round(tp_profit, 4),
                    commission=round(commission_cost, 4),
                    spread_cost=round(spread_cost, 4),
                    min_required=round(min_profit, 4))
            return False
    elif reward <= spread:
        _record(symbol, tf_name, "skipped", side=side,
                reason="TP distance not above spread", price=price, sl=sl, tp=tp)
        return False

    if rr < settings["min_rr"]:
        _record(symbol, tf_name, "skipped", side=side,
                reason=f"RR {rr:.2f} below minimum {settings['min_rr']}",
                price=price, sl=sl, tp=tp)
        return False

    min_distance = info.trade_stops_level * info.point
    if risk < min_distance or reward < min_distance:
        _record(symbol, tf_name, "skipped", side=side,
                reason="SL/TP inside broker stops level", price=price, sl=sl, tp=tp)
        return False

    magic   = settings["magic_base"] + TIMEFRAMES[tf_name][1] // 60
    comment = f"{COMMENT_PREFIX}{tf_name} {int(c1['time'])}"

    if to_close:
        if not _close_for_override(to_close, tf_name):
            return False
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            _record(symbol, tf_name, "error", side=side,
                    reason="no tick after override close")
            return False
        price = tick.ask if side == "BUY" else tick.bid

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         order_type,
        "price":        price,
        "sl":           round(sl, info.digits),
        "tp":           round(tp, info.digits),
        "deviation":    settings["deviation"],
        "magic":        magic,
        "comment":      comment,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": _filling_mode(info),
    }

    result = mt5.order_send(request)
    if result is None:
        _record(symbol, tf_name, "error", side=side, reason="order_send returned None",
                mt5_error=str(mt5.last_error()), request=request)
        return False
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        _record(symbol, tf_name, "rejected", side=side,
                retcode=result.retcode, comment=result.comment, request=request)
        return False

    if result.order:
        _be_level[result.order] = _be_trigger(float(c1["high"]), float(c1["low"]), side)
    _record(symbol, tf_name, "opened", side=side,
            ticket=result.order, volume=result.volume,
            price=result.price, sl=request["sl"], tp=request["tp"])
    return True


# ---------------------------------------------------------------------------
# Per-symbol, per-timeframe check
# ---------------------------------------------------------------------------

def _check(symbol, tf_name, running=()):
    """
    Look for a 2-candle CRT entry on (symbol, tf_name).

      pos 0 = C2 — currently forming (sweep + entry candle)
      pos 1 = C1 — last closed candle (range)

    Returns True if a trade was placed.
    """
    tf, _ = TIMEFRAMES[tf_name]
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, 2)
    if rates is None or len(rates) < 2:
        return False

    c1, c2  = rates[0], rates[1]
    c2_time = int(c2["time"])
    if (symbol, tf_name, c2_time) in _traded:
        return False

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    c2_open = float(c2["open"])
    above   = tick.bid > c2_open
    now     = time.time()

    # Detect a cross of C2.open between the previous poll and this one
    key  = (symbol, tf_name)
    prev = _last_seen.get(key)
    _last_seen[key] = (c2_time, above, now)
    fresh = (prev is not None and prev[0] == c2_time
             and now - prev[2] <= settings["poll_interval"] * 3)
    if not fresh or prev[1] == above:
        return False

    # C2 must open inside C1
    if not (c1_low < c2_open < c1_high):
        return False

    swept_low  = float(c2["low"])  < c1_low
    swept_high = float(c2["high"]) > c1_high
    if swept_low == swept_high:
        return False          # no sweep yet, or both sides swept

    if swept_low and above:
        side = "BUY"          # swept C1 low, crossed back up through C2 open
    elif swept_high and not above:
        side = "SELL"         # swept C1 high, crossed back down through C2 open
    else:
        return False

    _record(symbol, tf_name, "signal", side=side,
            c1_high=c1_high, c1_low=c1_low, c2_open=c2_open,
            c2_high=float(c2["high"]), c2_low=float(c2["low"]), bid=tick.bid)

    if running and any(_position_side(p) == side for p in running):
        _record(symbol, tf_name, "skipped", side=side,
                reason="lower-TF trade already open in same direction",
                running=[{"ticket": p.ticket, "timeframe": _position_tf(p)}
                         for p in running])
        return False

    traded = _place(symbol, tf_name, c1, c2, side, tick, to_close=running)
    if traded:
        _traded.add((symbol, tf_name, c2_time))
    return traded


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

def _loop():
    logger.info("CRT2 bot loop started: symbols=%s timeframes=%s lot=%s",
                settings["symbols"], list(TIMEFRAMES), settings["lot"])
    while True:
        try:
            if mt5.terminal_info() is None:
                mt5.initialize()
                for symbol in settings["symbols"]:
                    mt5.symbol_select(symbol, True)

            for symbol in settings["symbols"]:

                # 1. Break-even — runs even after /bot2/stop
                try:
                    _manage_breakeven(symbol)
                except Exception:
                    logger.exception("CRT2 breakeven check failed for %s", symbol)

                if not _enabled.is_set():
                    continue

                # 2. One trade per pair — only TFs higher than the running trade
                running = _open_positions(symbol)
                if running:
                    # Unknown magic → treat as highest, block entries
                    top_rank = min(TF_RANK.get(_position_tf(p), -1) for p in running)
                    tf_names = [tf for tf in TIMEFRAMES if TF_RANK[tf] < top_rank]
                    if not tf_names:
                        continue
                else:
                    tf_names = list(TIMEFRAMES)

                # 3. HTF-first scan: stop at the first TF that trades
                for tf_name in tf_names:
                    try:
                        if _check(symbol, tf_name, running):
                            break
                    except Exception:
                        logger.exception("CRT2 check failed for %s %s", symbol, tf_name)

            # Forget traded C2s older than a day
            cutoff = time.time() - 24 * 3600 - 4 * 3600
            for k in [k for k in _traded if k[2] < cutoff]:
                _traded.discard(k)

        except Exception:
            logger.exception("CRT2 loop error")

        time.sleep(settings["poll_interval"])


def start_candle_two_bot():
    """Start the background thread once. Call after crt_bot.start_crt_bot() (shared commission settings)."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    if not crt_bot.settings:
        crt_bot.start_crt_bot()

    default_symbols = os.environ.get("CRT_SYMBOLS", DEFAULT_SYMBOLS)
    settings.update({
        "symbols":           [s.strip() for s in
                              os.environ.get("CRT2_SYMBOLS", default_symbols).split(",")
                              if s.strip()],
        "lot":               float(os.environ.get("CRT2_LOT", "0.01")),
        "deviation":         int(os.environ.get("CRT2_DEVIATION", "20")),
        "magic_base":        int(os.environ.get("CRT2_MAGIC_BASE", "780000")),
        "poll_interval":     float(os.environ.get("CRT2_POLL_INTERVAL", "2")),
        "min_rr":            float(os.environ.get("CRT2_MIN_RR", "0")),
        "sl_buffer_spreads": float(os.environ.get("CRT2_SL_BUFFER_SPREADS", "0")),
        "min_sl_spreads":    float(os.environ.get("CRT2_MIN_SL_SPREADS", "3")),
        "be_trigger":        float(os.environ.get("CRT2_BE_TRIGGER", "0.45")),
    })

    for symbol in settings["symbols"]:
        if not mt5.symbol_select(symbol, True):
            logger.warning("CRT2: could not select symbol %s", symbol)

    if os.environ.get("CRT2_ENABLED", "false").lower() == "true":
        _enabled.set()

    threading.Thread(target=_loop, daemon=True, name="crt2-bot").start()


def enable():
    _enabled.set()


def disable():
    _enabled.clear()


def is_enabled():
    return _enabled.is_set()
