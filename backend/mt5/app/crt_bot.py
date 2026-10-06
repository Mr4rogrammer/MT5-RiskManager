"""
CRT (Candle Range Theory) auto-trader — v2.

Pattern (per symbol, HTF-first: H4 → H1 → M30 → M15):

  C1 = range candle
  C2 = sweep candle: closes back inside C1 after taking out one side
  C3 = the candle that is now opening  ← entry happens here

  Bearish CRT: C2.high > C1.high  AND  C2.close inside C1  →  SELL at C3 open
  Bullish CRT: C2.low  < C1.low   AND  C2.close inside C1  →  BUY  at C3 open

  SL : C2's sweep extreme  (C2.high for SELL, C2.low for BUY)
  TP : opposite side of C1 (C1.low  for SELL, C1.high for BUY)

Rules:
  • HTF priority  — check H4 first; if a trade fires on H4, skip H1/M30/M15 for
                    that symbol in the same poll cycle.
  • One trade per pair — while a CRT position is open for the symbol, only
                         timeframes HIGHER than the running trade are scanned
                         (break-even is still managed). Lower/same TF → skipped.
  • HTF override  — if a higher TF gives a signal OPPOSITE to the running trade
                    and it passes every filter, the running trade is closed at
                    market and the higher-TF trade is entered. A higher-TF signal
                    in the SAME direction is skipped (the running trade is kept).
  • C3 entry only — signal is ignored once C3 is older than CRT_MAX_SIGNAL_AGE s.
  • Break-even    — SL is moved to entry price when price reaches 50 % of TP range.
  • Fees filter   — expected TP profit (account currency) must exceed round-trip
                    commission + spread cost; otherwise the trade is skipped.

Commission modes — two types, one per symbol group:
  Flat  — fixed $ per 1.0 lot.  commission = lot × rate.
          Applies to all symbols NOT listed in CRT_COMMISSION_PCT_SYMBOLS.
          Example: XAUUSD, XAGUSD, AUDUSD → $5/lot → 0.01 lot costs $0.05.
  Pct   — % of notional value.  commission = lot × contract_size × price × (rate/100).
          Applies to symbols listed in CRT_COMMISSION_PCT_SYMBOLS.
          Example: BTCUSD at $60 000 with 0.04 % → 0.01 lot costs $0.24.

Settings (env vars):
  CRT_ENABLED                 true/false   start trading on boot (default false)
  CRT_SYMBOLS                 comma list   (default XAUUSD,EURUSD,GBPUSD,AUDUSD,USDCHF,NZDUSD,USDCAD)
  CRT_LOT                     fixed lot    (default 0.01)
  CRT_DEVIATION               max slippage in points (default 20)
  CRT_MAGIC_BASE              (default 770000)
  CRT_POLL_INTERVAL           seconds between checks (default 5)
  CRT_MAX_SIGNAL_AGE          ignore setups whose C2 closed more than N seconds ago (default 300)
  CRT_MIN_RR                  minimum reward:risk ratio (default 1.0, 0 disables)
  CRT_COMMISSION_PER_LOT      round-trip flat commission, account currency / 1.0 lot (default 5.0)
  CRT_COMMISSION_PCT_SYMBOLS  comma-separated symbols using % commission (default BTCUSD)
  CRT_COMMISSION_PCT_RATE     round-trip % rate for those symbols (default 0.04 → means 0.04 %)
"""

import logging
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import MetaTrader5 as mt5
from lib import close_position as _lib_close_position

logger = logging.getLogger(__name__)

# HTF-first order — dict insertion order is preserved (Python 3.7+).
# Higher timeframes are iterated first; once a trade fires the loop breaks.
TIMEFRAMES = {
    "H4":  (mt5.TIMEFRAME_H4,  4 * 60 * 60),
    "H1":  (mt5.TIMEFRAME_H1,  1 * 60 * 60),
    "M30": (mt5.TIMEFRAME_M30, 30 * 60),
    "M15": (mt5.TIMEFRAME_M15, 15 * 60),
}

# Priority rank — lower number = higher priority (H4=0 is most important).
# Used to decide whether a new signal overrides an existing lower-TF trade.
TF_RANK = {tf: i for i, tf in enumerate(TIMEFRAMES)}  # {"H4":0,"H1":1,"M30":2,"M15":3}

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
# Strategy — signal detection
# ---------------------------------------------------------------------------

def detect_signal(c1, c2):
    """
    Return {"side", "sl", "tp"} if C2 forms a valid CRT setup against C1, else None.

    TP is always the OPPOSITE side of C1:
      Bearish (SELL): swept above C1.high → TP = C1.low
      Bullish (BUY):  swept below C1.low  → TP = C1.high

    c1 / c2 are MT5 rate rows (indexable by "high", "low", "close").
    """
    c1_high, c1_low = float(c1["high"]), float(c1["low"])
    c2_high, c2_low, c2_close = float(c2["high"]), float(c2["low"]), float(c2["close"])

    # C2 must close back inside C1's range
    if not (c1_low < c2_close < c1_high):
        return None

    swept_high = c2_high > c1_high
    swept_low  = c2_low  < c1_low

    # No sweep, or swept both sides simultaneously — no clear direction
    if swept_high == swept_low:
        return None

    if swept_high:
        # Bearish: price took out C1 highs then closed back inside → short to C1.low
        return {"side": "SELL", "sl": c2_high, "tp": c1_low}

    # Bullish: price took out C1 lows then closed back inside → long to C1.high
    return {"side": "BUY", "sl": c2_low, "tp": c1_high}


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
    logger.info("CRT %s %s: %s %s", symbol, tf_name, status, details)


def _filling_mode(info):
    """Return the broker-supported filling mode (bitmask: 1=FOK, 2=IOC)."""
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _normalize_volume(volume, info):
    step   = info.volume_step or 0.01
    volume = math.floor(volume / step + 1e-9) * step
    volume = max(info.volume_min, min(volume, info.volume_max))
    return round(volume, 8)


def _open_crt_positions(symbol):
    """Return every CRT-managed position currently open for this symbol."""
    positions = mt5.positions_get(symbol=symbol) or []
    return [p for p in positions if p.comment.startswith("CRT")]


def _position_tf(pos):
    """Return the timeframe name a CRT position was opened on (from its magic), or None."""
    minutes = pos.magic - settings["magic_base"]
    for tf_name, (_, tf_seconds) in TIMEFRAMES.items():
        if tf_seconds // 60 == minutes:
            return tf_name
    return None


def _position_side(pos):
    return "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"


def _close_for_override(positions, tf_name):
    """
    Close lower-TF CRT positions that a higher-TF signal overrides.
    Returns True only if every position was closed.
    """
    for pos in positions:
        info   = mt5.symbol_info(pos.symbol)
        result = _lib_close_position(
            pos._asdict(),
            deviation=settings["deviation"],
            magic=pos.magic,
            comment=f"CRT override {tf_name}",
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


def _already_traded(symbol, magic, comment):
    """Guard against placing the exact same setup twice (same magic + comment)."""
    positions = mt5.positions_get(symbol=symbol) or []
    return any(p.magic == magic and p.comment == comment for p in positions)


def _spread_cost_usd(symbol, volume, tick):
    """
    Estimate the cost of crossing the spread in account currency.
    Uses mt5.order_calc_profit on a BUY from bid→ask; returns abs value.
    Returns None if MT5 cannot calculate.
    """
    profit = mt5.order_calc_profit(
        mt5.ORDER_TYPE_BUY, symbol, volume, tick.bid, tick.ask
    )
    return abs(profit) if profit is not None else None


def _tp_profit_usd(side, symbol, volume, entry, tp):
    """Return expected profit at TP in account currency, or None on failure."""
    action = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    return mt5.order_calc_profit(action, symbol, volume, entry, tp)


def _commission_usd(symbol, volume, price, info):
    """
    Calculate the round-trip broker commission in account currency.

    Two modes (chosen per symbol via settings):
      Flat  — commission = volume × commission_per_lot
              Used for XAUUSD, XAGUSD, AUDUSD, most forex pairs, etc.
              Example: 0.01 lot × $5/lot = $0.05

      Pct   — commission = volume × contract_size × price × (pct_rate / 100)
              Used for BTCUSD and other crypto/CFD with notional-based fees.
              Example (BTCUSD @ $60 000, 0.04 %, 0.01 lot):
                0.01 × 1 × 60 000 × 0.0004 = $0.24
    """
    if symbol in settings["commission_pct_symbols"]:
        contract_size = info.trade_contract_size  # e.g. 1 BTC per lot for BTCUSD
        notional      = volume * contract_size * price
        return notional * settings["commission_pct_rate"] / 100.0
    else:
        return volume * settings["commission_per_lot"]


# ---------------------------------------------------------------------------
# Break-even management
# ---------------------------------------------------------------------------

def _manage_breakeven(symbol):
    """
    For every open CRT position on `symbol`:
    once price has moved ≥ 50 % of the entry→TP range, slide SL to entry price.
    """
    positions = mt5.positions_get(symbol=symbol) or []
    for pos in positions:
        if not pos.comment.startswith("CRT"):
            continue

        entry = pos.price_open
        sl    = pos.sl
        tp    = pos.tp

        if tp == 0 or sl == 0:
            continue

        total_range = abs(tp - entry)
        if total_range == 0:
            continue

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue

        if pos.type == mt5.POSITION_TYPE_BUY:
            current  = tick.bid
            halfway  = entry + total_range * 0.47
            # Skip if SL is already at or above entry (already at BE or better)
            if sl >= entry:
                continue
            if current >= halfway:
                _move_sl_to_breakeven(pos, entry)

        else:  # SELL
            current  = tick.ask
            halfway  = entry - total_range * 0.47
            if sl <= entry:
                continue
            if current <= halfway:
                _move_sl_to_breakeven(pos, entry)


def _move_sl_to_breakeven(pos, entry):
    info = mt5.symbol_info(pos.symbol)
    if info is None:
        return

    new_sl = round(entry, info.digits)
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": pos.ticket,
        "symbol":   pos.symbol,
        "sl":       new_sl,
        "tp":       pos.tp,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        logger.info(
            "CRT BE: %s ticket=%s SL moved to %.%df (break-even)",
            pos.symbol, pos.ticket, info.digits, new_sl
        )
        events.append({
            "time":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol":  pos.symbol,
            "status":  "breakeven",
            "ticket":  pos.ticket,
            "new_sl":  new_sl,
        })
    else:
        err = result.retcode if result else mt5.last_error()
        logger.warning(
            "CRT BE failed: %s ticket=%s err=%s", pos.symbol, pos.ticket, err
        )


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _place(symbol, tf_name, c2_time, signal, tick, to_close=()):
    """
    Attempt to open a CRT market order.

    `to_close` are lower-TF positions this signal overrides. They are closed only
    after the new setup has passed every filter, right before the new order is sent.

    Returns True if the order was successfully sent, False otherwise.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        _record(symbol, tf_name, "error",
                reason="symbol_info failed", mt5_error=str(mt5.last_error()))
        return False

    side, sl, tp = signal["side"], signal["sl"], signal["tp"]

    if side == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price      = tick.ask
        valid      = sl < price < tp
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price      = tick.bid
        valid      = tp < price < sl

    if not valid:
        _record(symbol, tf_name, "skipped", side=side,
                reason="price already beyond SL/TP",
                price=price, sl=sl, tp=tp)
        return False

    risk   = abs(price - sl)
    reward = abs(tp - price)
    spread = tick.ask - tick.bid
    rr     = reward / risk if risk > 0 else 0.0

    volume = _normalize_volume(settings["lot"], info)

    # ------------------------------------------------------------------
    # Fees + spread filter (account-currency)
    # TP profit must exceed round-trip commission + spread cost.
    # _commission_usd() picks flat or % mode automatically per symbol.
    # ------------------------------------------------------------------
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
    else:
        # Fallback: price-unit spread check when order_calc_profit is unavailable
        if reward <= spread:
            _record(symbol, tf_name, "skipped", side=side,
                    reason="TP distance not above spread",
                    price=price, sl=sl, tp=tp)
            return False


    # ------------------------------------------------------------------
    # Reward-to-Risk filter
    # ------------------------------------------------------------------
    if rr < settings["min_rr"]:
        _record(symbol, tf_name, "skipped", side=side,
                reason=f"RR {rr:.2f} below minimum {settings['min_rr']}",
                price=price, sl=sl, tp=tp)
        return False

    # ------------------------------------------------------------------
    # Broker minimum stops distance
    # ------------------------------------------------------------------
    min_distance = info.trade_stops_level * info.point
    if abs(price - sl) < min_distance or abs(tp - price) < min_distance:
        _record(symbol, tf_name, "skipped", side=side,
                reason="SL/TP inside broker stops level",
                price=price, sl=sl, tp=tp)
        return False

    # ------------------------------------------------------------------
    # Duplicate guard — same setup must never be traded twice
    # ------------------------------------------------------------------
    magic   = settings["magic_base"] + TIMEFRAMES[tf_name][1] // 60
    comment = f"CRT {tf_name} {c2_time}"

    if _already_traded(symbol, magic, comment):
        return False

    # ------------------------------------------------------------------
    # Higher-TF override — close the lower-TF trade(s) before entering
    # ------------------------------------------------------------------
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
        _record(symbol, tf_name, "error", side=side,
                reason="order_send returned None",
                mt5_error=str(mt5.last_error()), request=request)
        return False

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        _record(symbol, tf_name, "rejected", side=side,
                retcode=result.retcode, comment=result.comment, request=request)
        return False

    _record(symbol, tf_name, "opened", side=side,
            ticket=result.order, volume=result.volume,
            price=result.price, sl=request["sl"], tp=request["tp"])
    return True


# ---------------------------------------------------------------------------
# Per-symbol, per-timeframe check
# ---------------------------------------------------------------------------

def _check(symbol, tf_name, running=()):
    """
    Look for a CRT setup on (symbol, tf_name).

    `running` are open lower-TF CRT positions on this symbol. A signal opposite
    to them overrides (closes) them; a signal in the same direction is skipped.

    Candle numbering from copy_rates_from_pos:
      pos 0 = C3 — currently forming (this is our entry candle)
      pos 1 = C2 — just closed     (sweep candle)
      pos 2 = C1 — closed before C2 (range candle)

    We fetch positions 1 and 2 (C2, C1), detect a signal, then enter at
    market (which is C3's open price) — only if C3 opened recently enough.

    Returns True if a trade was successfully placed, False otherwise.
    """
    tf, tf_seconds = TIMEFRAMES[tf_name]

    # Skip pos 0 (forming C3); fetch C2 (pos 1) and C1 (pos 2)
    rates = mt5.copy_rates_from_pos(symbol, tf, 1, 2)
    if rates is None or len(rates) < 2:
        return False

    c1, c2  = rates[0], rates[1]   # rates[0]=C1 (older), rates[1]=C2 (newer)
    c2_time = int(c2["time"])

    key = (symbol, tf_name)
    if _last_bar.get(key) == c2_time:
        return False          # already evaluated this C2
    _last_bar[key] = c2_time

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    # Entry is valid only at C3's open (= c2_time + tf_seconds).
    # Reject if we're too far into C3.
    c3_open_time = c2_time + tf_seconds
    if tick.time - c3_open_time > settings["max_signal_age"]:
        return False

    signal = detect_signal(c1, c2)
    if signal is None:
        return False

    _record(symbol, tf_name, "signal", side=signal["side"],
            c1_high=float(c1["high"]), c1_low=float(c1["low"]),
            c2_high=float(c2["high"]), c2_low=float(c2["low"]),
            c2_close=float(c2["close"]))

    if running:
        if any(_position_side(p) == signal["side"] for p in running):
            _record(symbol, tf_name, "skipped", side=signal["side"],
                    reason="lower-TF trade already open in same direction",
                    running=[{"ticket": p.ticket, "timeframe": _position_tf(p)}
                             for p in running])
            return False

    return _place(symbol, tf_name, c2_time, signal, tick, to_close=running)


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

def _loop():
    logger.info(
        "CRT bot loop started: symbols=%s timeframes=%s lot=%s "
        "flat_commission=$%.2f/lot  pct_symbols=%s pct_rate=%.4f%%",
        settings["symbols"], list(TIMEFRAMES), settings["lot"],
        settings["commission_per_lot"],
        settings["commission_pct_symbols"],
        settings["commission_pct_rate"],
    )
    while True:
        if _enabled.is_set():
            try:
                if mt5.terminal_info() is None:
                    mt5.initialize()
                    for symbol in settings["symbols"]:
                        mt5.symbol_select(symbol, True)

                for symbol in settings["symbols"]:

                    # ── 1. Break-even management (always runs) ──────────────
                    try:
                        _manage_breakeven(symbol)
                    except Exception:
                        logger.exception("CRT breakeven check failed for %s", symbol)

                    # ── 2. One trade per pair, higher TF overrides ──────────
                    # If a CRT position is open, only timeframes HIGHER than
                    # the running trade are scanned:
                    #   H1 running → only H4 checked (M30/M15 skipped)
                    #   H4 running → nothing checked
                    # An opposite higher-TF signal closes the running trade
                    # and enters on the higher TF (see _check / _place).
                    running = _open_crt_positions(symbol)
                    if running:
                        ranks = [TF_RANK.get(_position_tf(p)) for p in running]
                        # Unknown magic (not ours) → treat as highest, block entries
                        top_rank = min((r if r is not None else -1) for r in ranks)
                        tf_names = [tf for tf in TIMEFRAMES if TF_RANK[tf] < top_rank]
                        if not tf_names:
                            continue
                    else:
                        tf_names = list(TIMEFRAMES)

                    # ── 3. HTF-first entry scan (cascade) ───────────────────
                    # Scan order: H4 → H1 → M30 → M15
                    # The moment a trade is placed on a timeframe, ALL lower
                    # timeframes are skipped for this symbol in this cycle:
                    #   H4 fires  → H1, M30, M15 skipped
                    #   H1 fires  → M30, M15 skipped
                    #   M30 fires → M15 skipped
                    #   M15 fires → nothing lower to skip
                    for idx, tf_name in enumerate(tf_names):
                        try:
                            traded = _check(symbol, tf_name, running)
                        except Exception:
                            logger.exception(
                                "CRT check failed for %s %s", symbol, tf_name
                            )
                            traded = False

                        if traded:
                            skipped = tf_names[idx + 1:]   # all TFs after this one
                            if skipped:
                                logger.info(
                                    "CRT %s: trade placed on %s — skipping lower TFs: %s",
                                    symbol, tf_name, ", ".join(skipped),
                                )
                            break   # do NOT check lower TFs for this symbol


            except Exception:
                logger.exception("CRT loop error")

        time.sleep(settings["poll_interval"])


def start_crt_bot():
    """Start the background thread once. Trading only runs while _enabled is set."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    settings.update({
        "symbols":                [s.strip() for s in
                                   os.environ.get("CRT_SYMBOLS", DEFAULT_SYMBOLS).split(",")
                                   if s.strip()],
        "lot":                    float(os.environ.get("CRT_LOT", "0.01")),
        "deviation":              int(os.environ.get("CRT_DEVIATION", "20")),
        "magic_base":             int(os.environ.get("CRT_MAGIC_BASE", "770000")),
        "poll_interval":          float(os.environ.get("CRT_POLL_INTERVAL", "5")),
        "max_signal_age":         int(os.environ.get("CRT_MAX_SIGNAL_AGE", "300")),
        "min_rr":                 float(os.environ.get("CRT_MIN_RR", "1.0")),
        # Flat commission — applies to all symbols NOT in commission_pct_symbols
        "commission_per_lot":     float(os.environ.get("CRT_COMMISSION_PER_LOT", "5.0")),
        # Percentage commission — for crypto/CFD with notional-based fees (e.g. BTCUSD)
        "commission_pct_symbols": {
            s.strip().upper()
            for s in os.environ.get("CRT_COMMISSION_PCT_SYMBOLS", "BTCUSD").split(",")
            if s.strip()
        },
        "commission_pct_rate":    float(os.environ.get("CRT_COMMISSION_PCT_RATE", "0.04")),
    })

    for symbol in settings["symbols"]:
        if not mt5.symbol_select(symbol, True):
            logger.warning(
                "CRT: could not select symbol %s (check broker symbol name)", symbol
            )

    if os.environ.get("CRT_ENABLED", "false").lower() == "true":
        _enabled.set()

    threading.Thread(target=_loop, daemon=True, name="crt-bot").start()


def enable():
    _enabled.set()


def disable():
    _enabled.clear()


def is_enabled():
    return _enabled.is_set()
