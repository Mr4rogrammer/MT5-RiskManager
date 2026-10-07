"""
Shared building blocks for the CRT bots (crt_bot.py, candle_two_bot.py).

A strategy module only decides WHERE to trade: the signal, SL, TP and the
break-even trigger price. Everything else is the same for every bot and lives here:

  • Bot.loop        — poll loop: break-even → one trade per pair → HTF-first scan
  • Bot.place       — filters (price, min SL, fees, RR, stops level), HTF override,
                      order send
  • Bot.breakeven   — move SL to entry ± round-trip commission at a trigger price,
                      closing `partial_pct` % of the position at the same moment
  • Bot.trail       — after break-even, trail the SL `trail_r` × the initial risk behind price
  • Bot.close       — close a position at market
  • fee / price helpers (commission, spread cost, volume, filling mode)

Every signal, skip, open, break-even and close is also written to SQLite (trade_db).

Commission settings are broker-wide and shared by every bot:
  CRT_COMMISSION_PER_LOT      flat round-trip $ per 1.0 lot (default 5.0)
  CRT_COMMISSION_PCT_SYMBOLS  symbols charged a % of notional instead (default BTCUSD)
  CRT_COMMISSION_PCT_RATE     round-trip % for those symbols (default 0.04 → 0.04 %)

Position size is also shared by every bot, settable on the dashboard (risk_guard.limits):
  BOTS_RISK_PCT               % of the account balance lost if the SL is hit (default 0.5).
                              0 = use each bot's fixed lot setting instead.
  BOTS_RISK_MAX_OVER          the broker's minimum lot may risk up to this × the target
                              before the trade is skipped (default 1.5)

Start / stop from the dashboard:
  BOTS_CONTROL_DIR            folder of control files, one per bot named after it
                              (default /config/control). The dashboard writes "run" or
                              "stop" into it; each bot reads its file on every poll, and
                              the last choice also wins over *_ENABLED after a restart.
"""

import logging
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import MetaTrader5 as mt5

import risk_guard
import telegram_alert
import trade_db
from mt5_guard import MT5_LOCK

logger = logging.getLogger(__name__)

# HTF-first order — dict insertion order is preserved (Python 3.7+).
TIMEFRAMES = {
    "H4":  (mt5.TIMEFRAME_H4,  4 * 60 * 60),
    "H1":  (mt5.TIMEFRAME_H1,  1 * 60 * 60),
    "M30": (mt5.TIMEFRAME_M30, 30 * 60),
    "M15": (mt5.TIMEFRAME_M15, 15 * 60),
}

# Priority rank — lower number = higher priority (H4=0 is most important).
TF_RANK = {tf: i for i, tf in enumerate(TIMEFRAMES)}  # {"H4":0,"H1":1,"M30":2,"M15":3}

DEFAULT_SYMBOLS = "XAUUSD,EURUSD,GBPUSD,AUDUSD,USDCHF,NZDUSD,USDCAD"

# Seconds before a rejected break-even modify is retried
BE_RETRY_SECONDS = 60

# What the common order_send retcodes mean, for the journal and the dashboard's log
RETCODES = {
    10004: "requote", 10006: "request rejected", 10007: "request canceled by trader",
    10010: "only part of the request was completed", 10011: "request processing error",
    10012: "request canceled by timeout", 10013: "invalid request", 10014: "invalid volume",
    10015: "invalid price", 10016: "invalid stops", 10017: "trading disabled for the symbol",
    10018: "market closed", 10019: "not enough money", 10020: "prices changed",
    10021: "no quotes", 10022: "invalid expiration", 10024: "too many requests",
    10026: "Algo Trading disabled by the server",
    10027: "Algo Trading disabled in the terminal (Algo Trading button is off)",
    10029: "order or position frozen", 10030: "filling mode not supported",
    10031: "no connection to the trade server", 10033: "pending orders limit reached",
    10034: "volume limit for the symbol reached", 10040: "positions limit reached",
}


def retcode_reason(result):
    """'rejected 10027: Algo Trading disabled … (broker: AutoTrading disabled by client)'."""
    text = RETCODES.get(result.retcode, "unknown retcode")
    broker = (result.comment or "").strip()
    return f"rejected {result.retcode}: {text}" + (f" (broker: {broker})" if broker else "")

# Broker commission settings — filled by load_fees()
fees = {}


# Dashboard start/stop files — see Bot._apply_control
CONTROL_DIR = os.environ.get("BOTS_CONTROL_DIR", "/config/control").rstrip("/\\")

# Every Bot created, by name — used by the generic /bots routes
BOTS = {}

ACCOUNT_HEDGING = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)


def account_problem():
    """
    None if the account can run several bots on the same symbol, else the reason.
    Netting accounts keep ONE position per symbol, so bots would merge into each
    other's trades (SL/TP overwritten, magic numbers lost). BOTS_ALLOW_NETTING=true
    overrides the check (only sensible with a single bot per symbol).
    """
    if os.environ.get("BOTS_ALLOW_NETTING", "false").lower() == "true":
        return None
    with MT5_LOCK:
        info = mt5.account_info()
    if info is None:
        return None                     # not logged in yet — checked again on start
    if info.margin_mode != ACCOUNT_HEDGING:
        return ("account is NETTING (one position per symbol): bots would merge into each "
                "other's trades. Use a hedging account, or set BOTS_ALLOW_NETTING=true")
    return None


def load_fees():
    if fees:
        return
    fees.update({
        "commission_per_lot":     float(os.environ.get("CRT_COMMISSION_PER_LOT", "5.0")),
        "commission_pct_symbols": sorted({
            s.strip().upper()
            for s in os.environ.get("CRT_COMMISSION_PCT_SYMBOLS", "BTCUSD").split(",")
            if s.strip()
        }),
        "commission_pct_rate":    float(os.environ.get("CRT_COMMISSION_PCT_RATE", "0.04")),
    })


# ---------------------------------------------------------------------------
# Fee / price helpers
# ---------------------------------------------------------------------------

def filling_mode(info):
    """Return the broker-supported filling mode (bitmask: 1=FOK, 2=IOC)."""
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def normalize_volume(volume, info):
    step   = info.volume_step or 0.01
    volume = math.floor(volume / step + 1e-9) * step
    volume = max(info.volume_min, min(volume, info.volume_max))
    return round(volume, 8)


def risk_volume(side, symbol, price, sl, info):
    """
    Lot size that loses BOTS_RISK_PCT % of the balance if the SL is hit.

    Returns (volume, None), or (None, (reason, details)) when the trade should be skipped.
      e.g. balance $10 000, 0.5 % → $50 at risk; EURUSD SL 20 pips = $200 per lot
           → 0.25 lot. Rounded DOWN to the volume step, so risk never exceeds the target
           — except at the broker's minimum lot, allowed up to BOTS_RISK_MAX_OVER × target.
    """
    account = mt5.account_info()
    if account is None or account.balance <= 0:
        return None, ("account_info unavailable", {})
    target   = account.balance * risk_guard.limits["risk_pct"] / 100.0
    loss_lot = profit_usd(side, symbol, 1.0, price, sl)
    if not loss_lot:
        return None, ("order_calc_profit failed for risk sizing", {})
    volume = normalize_volume(target / abs(loss_lot), info)
    risk   = abs(loss_lot) * volume
    if risk > target * risk_guard.limits["risk_max_over"]:
        return None, (f"minimum lot risks over {risk_guard.limits['risk_max_over']}x the "
                      f"{risk_guard.limits['risk_pct']}% target",
                      {"volume": volume, "risk": round(risk, 2), "target": round(target, 2)})
    return volume, None


def commission_usd(symbol, volume, price, info):
    """
    Round-trip broker commission in account currency.

      Flat — volume × CRT_COMMISSION_PER_LOT
             e.g. 0.01 lot × $5/lot = $0.05
      Pct  — volume × contract_size × price × CRT_COMMISSION_PCT_RATE / 100
             for symbols in CRT_COMMISSION_PCT_SYMBOLS
             e.g. BTCUSD @ 60 000, 0.04 %, 0.01 lot → $0.24
    """
    if symbol.upper() in fees["commission_pct_symbols"]:
        notional = volume * info.trade_contract_size * price
        return notional * fees["commission_pct_rate"] / 100.0
    return volume * fees["commission_per_lot"]


def commission_offset(symbol, volume, price, info):
    """Round-trip commission converted to a price distance (0 if MT5 can't price it)."""
    per_point = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, volume,
                                      price, price + info.point)
    if not per_point:
        return 0.0
    return commission_usd(symbol, volume, price, info) / per_point * info.point


def spread_cost_usd(symbol, volume, tick):
    """Cost of crossing the spread in account currency, or None if MT5 can't price it."""
    profit = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, volume, tick.bid, tick.ask)
    return abs(profit) if profit is not None else None


def profit_usd(side, symbol, volume, entry, exit_price):
    """Profit (negative = loss) from entry to exit, in account currency, or None."""
    action = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    return mt5.order_calc_profit(action, symbol, volume, entry, exit_price)


def position_side(pos):
    return "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"


def sl_beyond_sweep(side, extreme, spread, buffer_spreads):
    """
    SL just beyond the sweep extreme.

    MT5 candles are BID prices, but a SELL's SL triggers on the ASK, so a SELL SL
    exactly at the wick is hit while bid is still one spread below it. SELL gets
    the spread added; both sides get an optional buffer of N × spread.
    """
    buffer = spread * buffer_spreads
    if side == "SELL":
        return extreme + spread + buffer
    return extreme - buffer


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class Bot:
    """
    One trading bot. `name` is its key in the database (e.g. "crt3"), `label` its
    log prefix (e.g. "CRT"), `prefix` the start of every order comment it places
    (e.g. "CRT "), which is how it recognises its own positions. `title` and
    `description` are what the dashboard shows.

    Adding a new strategy: create a module with a check(symbol, tf, running) and a
    be_trigger(pos) function, a Bot with a unique name, prefix and magic_base, and
    call BOT.start(...) from app.py. It appears on the dashboard and under the
    /bots API routes automatically.

    Required settings: symbols, lot, deviation, magic_base, poll_interval,
    min_rr, min_sl_spreads. Optional: partial_pct (% closed at break-even, default 0),
    trail_r (trailing distance in R after break-even, default 0 = off).
    """

    def __init__(self, name, label, prefix, title=None, description=None):
        self.name     = name
        self.label    = label
        self.prefix   = prefix
        self.title    = title or label          # shown on the dashboard
        self.description = description
        self.settings = {}
        self.events   = deque(maxlen=100)
        self.enabled  = threading.Event()
        BOTS[name] = self
        self._started = False
        self._start_lock = threading.Lock()
        self._be_retry_after = {}   # ticket -> unix time
        self._trail_retry_after = {}
        self._partial_done = set()  # tickets already partly closed (or that can't be)
        self._trade_info = {}       # ticket -> journal row (entry, initial SL, volume)
        self._account_checked = False
        self._control_mtime = None  # mtime of the control file last applied

    # -- bookkeeping --------------------------------------------------------

    def record(self, symbol, tf_name, status, **details):
        event = {
            "time":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol":    symbol,
            "timeframe": tf_name,
            "status":    status,
            **details,
        }
        self.events.append(event)
        logger.info("%s %s %s: %s %s", self.label, symbol, tf_name, status, details)
        trade_db.log_event(self.name, symbol, tf_name, status, details)
        telegram_alert.alert(self.title, self.name, symbol, tf_name, status, details)

    def magic(self, tf_name):
        return self.settings["magic_base"] + TIMEFRAMES[tf_name][1] // 60

    def position_tf(self, pos):
        """Timeframe a position was opened on (from its magic), or None."""
        minutes = pos.magic - self.settings["magic_base"]
        for tf_name, (_, tf_seconds) in TIMEFRAMES.items():
            if tf_seconds // 60 == minutes:
                return tf_name
        return None

    def open_positions(self, symbol):
        positions = mt5.positions_get(symbol=symbol) or []
        return [p for p in positions if p.comment.startswith(self.prefix)]

    def scan_order(self, running):
        """
        Timeframes to scan for a symbol. With no trade open: all, H4 first.
        With a trade open: only timeframes HIGHER than it (H1 running → H4 only).
        A position with an unknown magic blocks all new entries.
        """
        if not running:
            return list(TIMEFRAMES)
        top_rank = min(TF_RANK.get(self.position_tf(p), -1) for p in running)
        return [tf for tf in TIMEFRAMES if TF_RANK[tf] < top_rank]

    # -- trading ------------------------------------------------------------

    def close(self, pos, reason):
        """Close a position at market. Returns the fill price, or None on failure."""
        tick = mt5.symbol_info_tick(pos.symbol)
        info = mt5.symbol_info(pos.symbol)
        if tick is None or info is None:
            return None

        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "position":     pos.ticket,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price":        tick.bid if is_buy else tick.ask,
            "deviation":    self.settings["deviation"],
            "magic":        pos.magic,
            "comment":      f"{self.label} {reason}"[:31],
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode(info),
        }
        trade_db.set_exit_hint(pos.ticket, reason)
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            trade_db.set_exit_hint(pos.ticket, None)
            err = result.retcode if result else mt5.last_error()
            logger.warning("%s close failed: %s ticket=%s err=%s",
                           self.label, pos.symbol, pos.ticket, err)
            return None
        return result.price

    def _close_for_override(self, positions, tf_name):
        """Close lower-TF positions a higher-TF signal overrides. True if all closed."""
        for pos in positions:
            price = self.close(pos, "override")
            if price is None:
                self.record(pos.symbol, self.position_tf(pos), "error", ticket=pos.ticket,
                            reason=f"failed to close for {tf_name} override",
                            mt5_error=str(mt5.last_error()))
                return False
            self.record(pos.symbol, self.position_tf(pos), "closed", ticket=pos.ticket,
                        side=position_side(pos), reason=f"overridden by {tf_name} signal",
                        price=price)
        return True

    def place(self, symbol, tf_name, side, sl, tp, tick, comment,
              to_close=(), signal_time=None, be_trigger=None):
        """
        Run every filter, close overridden lower-TF positions, then send a market order.

        `sl` must already include any spread/buffer adjustment (see sl_beyond_sweep).
        `to_close` are the open lower-TF positions on this symbol: a signal in the
        same direction is skipped (the running trade is kept); an opposite one
        closes them, but only once the new setup has passed every filter.
        Returns True if the order was filled.
        """
        if any(position_side(p) == side for p in to_close):
            self.record(symbol, tf_name, "skipped", side=side,
                        reason="lower-TF trade already open in same direction",
                        running=[{"ticket": p.ticket, "timeframe": self.position_tf(p)}
                                 for p in to_close])
            return False

        # Account-wide limits (risk_guard.py): daily loss halt, max open trades
        blocked = risk_guard.entry_block(replacing=len(to_close))
        if blocked:
            reason, details = blocked
            self.record(symbol, tf_name, "skipped", side=side, reason=reason, **details)
            return False

        info = mt5.symbol_info(symbol)
        if info is None:
            self.record(symbol, tf_name, "error",
                        reason="symbol_info failed", mt5_error=str(mt5.last_error()))
            return False

        spread = tick.ask - tick.bid
        if side == "BUY":
            order_type, price = mt5.ORDER_TYPE_BUY, tick.ask
            valid = sl < price < tp
        else:
            order_type, price = mt5.ORDER_TYPE_SELL, tick.bid
            valid = tp < price < sl

        if not valid:
            self.record(symbol, tf_name, "skipped", side=side,
                        reason="price already beyond SL/TP", price=price, sl=sl, tp=tp)
            return False

        risk   = abs(price - sl)
        reward = abs(tp - price)
        rr     = reward / risk if risk > 0 else 0.0

        # Minimum SL distance — a stop only a spread or two away is hit by noise
        if risk < spread * self.settings["min_sl_spreads"]:
            self.record(symbol, tf_name, "skipped", side=side,
                        reason=f"SL distance below {self.settings['min_sl_spreads']}x spread",
                        price=price, sl=sl, spread=round(spread, info.digits))
            return False

        # Size — BOTS_RISK_PCT % of the balance at the SL, or the bot's fixed lot
        if risk_guard.limits["risk_pct"] > 0:
            volume, why = risk_volume(side, symbol, price, sl, info)
            if volume is None:
                reason, details = why
                self.record(symbol, tf_name, "skipped", side=side, reason=reason,
                            price=price, sl=sl, **details)
                return False
        else:
            volume = normalize_volume(self.settings["lot"], info)

        # Fees — TP profit must exceed round-trip commission + spread cost
        tp_profit       = profit_usd(side, symbol, volume, price, tp)
        spread_cost     = spread_cost_usd(symbol, volume, tick)
        commission_cost = commission_usd(symbol, volume, price, info)

        if tp_profit is not None and spread_cost is not None:
            min_profit = commission_cost + spread_cost
            if tp_profit <= min_profit:
                self.record(symbol, tf_name, "skipped", side=side,
                            reason="TP profit below commission + spread",
                            tp_profit=round(tp_profit, 4),
                            commission=round(commission_cost, 4),
                            spread_cost=round(spread_cost, 4),
                            min_required=round(min_profit, 4))
                return False
        elif reward <= spread:
            # Fallback when order_calc_profit is unavailable
            self.record(symbol, tf_name, "skipped", side=side,
                        reason="TP distance not above spread", price=price, sl=sl, tp=tp)
            return False

        if rr < self.settings["min_rr"]:
            self.record(symbol, tf_name, "skipped", side=side,
                        reason=f"RR {rr:.2f} below minimum {self.settings['min_rr']}",
                        price=price, sl=sl, tp=tp)
            return False

        min_distance = info.trade_stops_level * info.point
        if risk < min_distance or reward < min_distance:
            self.record(symbol, tf_name, "skipped", side=side,
                        reason="SL/TP inside broker stops level", price=price, sl=sl, tp=tp)
            return False

        # Duplicate guard — the same setup is never open twice
        magic = self.magic(tf_name)
        if any(p.magic == magic and p.comment == comment
               for p in (mt5.positions_get(symbol=symbol) or [])):
            return False

        if to_close:
            if not self._close_for_override(to_close, tf_name):
                return False
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                self.record(symbol, tf_name, "error", side=side,
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
            "deviation":    self.settings["deviation"],
            "magic":        magic,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode(info),
        }

        result = mt5.order_send(request)
        if result is None:
            self.record(symbol, tf_name, "error", side=side, reason="order_send returned None",
                        mt5_error=str(mt5.last_error()), request=request)
            return False
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.record(symbol, tf_name, "rejected", side=side, reason=retcode_reason(result),
                        retcode=result.retcode, comment=result.comment, request=request)
            return False

        fill = result.price or price
        risk_money = profit_usd(side, symbol, result.volume, fill, request["sl"])
        trade_db.trade_opened(
            ticket=result.order, bot=self.name, symbol=symbol, timeframe=tf_name,
            side=side, volume=result.volume, magic=magic, comment=comment,
            signal_time=signal_time, opened_at=int(tick.time), entry_price=fill,
            sl_initial=request["sl"], tp=request["tp"],
            risk_money=abs(risk_money) if risk_money is not None else None,
            spread_at_entry=spread, be_trigger=be_trigger,
        )
        self.record(symbol, tf_name, "opened", side=side,
                    ticket=result.order, volume=result.volume,
                    price=fill, sl=request["sl"], tp=request["tp"])
        return True

    # -- open-trade management ------------------------------------------------

    def manage(self, pos, trigger):
        """Break-even (+ partial close), then the trailing stop. Caller holds MT5_LOCK."""
        self.breakeven(pos, trigger)
        if self.settings.get("trail_r", 0) > 0:
            self.trail(pos)

    def _journal(self, ticket):
        """The trade's journal row (entry, initial SL, opened volume), cached; None if unknown."""
        if ticket not in self._trade_info:
            row = trade_db.trade_info(ticket)
            if row is None:
                return None
            self._trade_info[ticket] = row
        return self._trade_info[ticket]

    def _partial_close(self, pos, info, tick):
        """
        Close `partial_pct` % of the position at market, once. Skipped (and logged once)
        when either part would be below the broker's minimum lot — e.g. a 0.01 lot trade.
        """
        pct = self.settings.get("partial_pct", 0)
        if pct <= 0 or pos.ticket in self._partial_done:
            return
        row = self._journal(pos.ticket)
        if row and row["volume"] and pos.volume < row["volume"] - 1e-9:
            self._partial_done.add(pos.ticket)         # already partly closed (e.g. before a restart)
            return
        self._partial_done.add(pos.ticket)
        step = info.volume_step or 0.01
        part = round(math.floor(pos.volume * pct / 100.0 / step + 1e-9) * step, 8)
        rest = round(pos.volume - part, 8)
        if part < info.volume_min or rest < info.volume_min:
            self.record(pos.symbol, self.position_tf(pos), "partial_skipped", ticket=pos.ticket,
                        volume=pos.volume, reason="volume too small to split")
            return
        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "position":     pos.ticket,
            "symbol":       pos.symbol,
            "volume":       part,
            "type":         mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price":        tick.bid if is_buy else tick.ask,
            "deviation":    self.settings["deviation"],
            "magic":        pos.magic,
            "comment":      f"{self.label} partial"[:31],
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode(info),
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.record(pos.symbol, self.position_tf(pos), "partial", ticket=pos.ticket,
                        closed=part, remaining=rest, price=result.price)
        else:
            err = result.retcode if result else mt5.last_error()
            logger.warning("%s partial close failed: %s ticket=%s err=%s",
                           self.label, pos.symbol, pos.ticket, err)

    def trail(self, pos):
        """
        Once the SL is at break-even or better, keep it `trail_r` × the initial risk
        behind price (bid for BUY, ask for SELL). Only ever tightens, in steps of at
        least 0.1R; the TP is kept.
        """
        if pos.sl == 0 or time.time() < self._trail_retry_after.get(pos.ticket, 0):
            return
        row = self._journal(pos.ticket)
        if not row or not row["sl_initial"] or not row["entry_price"]:
            return
        risk = abs(row["entry_price"] - row["sl_initial"])
        if risk <= 0:
            return
        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        entry = pos.price_open
        if (is_buy and pos.sl < entry) or (not is_buy and pos.sl > entry):
            return                                       # not at break-even yet

        tick = mt5.symbol_info_tick(pos.symbol)
        info = mt5.symbol_info(pos.symbol)
        if tick is None or info is None:
            return
        current = tick.bid if is_buy else tick.ask
        dist = self.settings["trail_r"] * risk
        new_sl = round(current - dist if is_buy else current + dist, info.digits)
        gain = new_sl - pos.sl if is_buy else pos.sl - new_sl
        if gain < max(0.1 * risk, info.point):
            return
        min_dist = max(info.trade_stops_level, info.trade_freeze_level, 1) * info.point
        if dist < min_dist:
            return

        result = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket,
                                 "symbol": pos.symbol, "sl": new_sl, "tp": pos.tp})
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self._trail_retry_after.pop(pos.ticket, None)
            logger.info("%s trail: %s ticket=%s SL %s -> %s", self.label, pos.symbol,
                        pos.ticket, pos.sl, new_sl)
        else:
            self._trail_retry_after[pos.ticket] = time.time() + BE_RETRY_SECONDS
            err = result.retcode if result else mt5.last_error()
            logger.warning("%s trail failed: %s ticket=%s err=%s (retry in %ss)",
                           self.label, pos.symbol, pos.ticket, err, BE_RETRY_SECONDS)

    def breakeven(self, pos, trigger):
        """
        Once price reaches `trigger`, close `partial_pct` % of the position (if set)
        and move SL to entry ± round-trip commission so a break-even exit really nets
        ~$0. Skipped while the new SL would sit inside the broker's stops/freeze
        level; a rejected modify is retried after 60 s.
        """
        entry, sl, tp = pos.price_open, pos.sl, pos.tp
        if trigger is None or tp == 0 or sl == 0:
            return
        if time.time() < self._be_retry_after.get(pos.ticket, 0):
            return

        tick = mt5.symbol_info_tick(pos.symbol)
        info = mt5.symbol_info(pos.symbol)
        if tick is None or info is None:
            return

        is_buy  = pos.type == mt5.POSITION_TYPE_BUY
        current = tick.bid if is_buy else tick.ask
        if (is_buy and current < trigger) or (not is_buy and current > trigger):
            return

        offset = commission_offset(pos.symbol, pos.volume, entry, info)
        new_sl = round(entry + offset if is_buy else entry - offset, info.digits)

        if (is_buy and sl >= new_sl) or (not is_buy and sl <= new_sl):
            return   # already at (or past) break-even

        # SL must sit on the losing side of price, outside the stops/freeze level.
        # Checked before the partial so a trigger already behind entry (CRT2 entering
        # past its 45 % level) doesn't close half the trade at entry, before any profit.
        min_dist = max(info.trade_stops_level, info.trade_freeze_level, 1) * info.point
        gap = current - new_sl if is_buy else new_sl - current
        if gap < min_dist:
            return

        # Bank part of the profit at the moment the SL goes to break-even
        self._partial_close(pos, info, tick)

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "symbol":   pos.symbol,
            "sl":       new_sl,
            "tp":       tp,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self._be_retry_after.pop(pos.ticket, None)
            trade_db.trade_breakeven(pos.ticket, new_sl)
            self.record(pos.symbol, self.position_tf(pos), "breakeven",
                        ticket=pos.ticket, entry=entry, new_sl=new_sl)
        else:
            self._be_retry_after[pos.ticket] = time.time() + BE_RETRY_SECONDS
            err = result.retcode if result else mt5.last_error()
            logger.warning("%s BE failed: %s ticket=%s err=%s (retry in %ss)",
                           self.label, pos.symbol, pos.ticket, err, BE_RETRY_SECONDS)

    def try_enable(self):
        """Start new entries unless the account can't run several bots. Returns (ok, reason)."""
        problem = account_problem()
        if problem:
            self.enabled.clear()
            logger.error("%s not started: %s", self.label, problem)
            self.record("", None, "error", reason=f"not started: {problem}")
            return False, problem
        self.enabled.set()
        return True, None

    # -- dashboard control ----------------------------------------------------

    def _read_control(self):
        """
        "run" / "stop" if the dashboard's control file changed since the last read,
        else None. Joined with "/" (not os.path.join) because Python runs under Wine.
        """
        path = f"{CONTROL_DIR}/{self.name}"
        try:
            mtime = os.stat(path).st_mtime_ns
            if mtime == self._control_mtime:
                return None
            self._control_mtime = mtime
            with open(path, encoding="ascii", errors="replace") as f:
                want = f.read(16).strip().lower()
        except OSError:
            return None                     # no file: the dashboard never touched this bot
        if want not in ("run", "stop"):
            logger.warning("%s: ignoring control file %s containing %r", self.label, path, want)
            return None
        return want

    def _apply_control(self):
        """Start or stop new entries when the dashboard asks. Open trades are left alone."""
        want = self._read_control()
        if want == "stop" and self.enabled.is_set():
            self.enabled.clear()
            self.record("", None, "control", action="stop", source="dashboard")
        elif want == "run" and not self.enabled.is_set():
            ok, why = self.try_enable()
            self.record("", None, "control", action="run", source="dashboard",
                        **({} if ok else {"failed": why}))

    # -- loop ---------------------------------------------------------------

    def start(self, check, be_trigger, enabled_by_default):
        """
        Start the background thread once.

          check(symbol, tf_name, running) -> bool   look for a setup, place it
          be_trigger(pos) -> price | None           break-even trigger for a position
        """
        with self._start_lock:
            if self._started:
                return
            self._started = True

        load_fees()
        trade_db.init()
        trade_db.register_bot(
            self.name, self.settings["magic_base"], label=self.title,
            description=self.description,
            state_fn=lambda: (dict(self.settings), self.enabled.is_set()))

        with MT5_LOCK:
            for symbol in self.settings["symbols"]:
                if not mt5.symbol_select(symbol, True):
                    logger.warning("%s: could not select symbol %s (check broker symbol name)",
                                   self.label, symbol)

        # A start/stop from the dashboard outlives restarts and wins over *_ENABLED
        want = self._read_control()
        if want is not None:
            enabled_by_default = want == "run"
            logger.info("%s: dashboard control file says %s", self.label, want)
        if enabled_by_default:
            self.try_enable()

        risk_guard.start(BOTS, CONTROL_DIR)

        threading.Thread(target=self._loop, args=(check, be_trigger),
                         daemon=True, name=f"{self.name}-bot").start()

    def _loop(self, check, be_trigger):
        logger.info("%s bot loop started: symbols=%s timeframes=%s settings=%s fees=%s",
                    self.label, self.settings["symbols"], list(TIMEFRAMES),
                    self.settings, fees)
        while True:
            try:
                self._apply_control()
                with MT5_LOCK:
                    if mt5.terminal_info() is None:
                        mt5.initialize()
                        for symbol in self.settings["symbols"]:
                            mt5.symbol_select(symbol, True)
                    # The account may not have been logged in at start: check it once it is
                    if not self._account_checked and mt5.account_info() is not None:
                        self._account_checked = True
                        if self.enabled.is_set():
                            self.try_enable()

                for symbol in self.settings["symbols"]:
                    # One symbol at a time under MT5_LOCK; other bots run between symbols
                    with MT5_LOCK:
                        self._process(symbol, check, be_trigger)

            except Exception:
                logger.exception("%s loop error", self.label)

            time.sleep(self.settings["poll_interval"])

    def _process(self, symbol, check, be_trigger):
        """One symbol: break-even, then the HTF-first entry scan. Caller holds MT5_LOCK."""
        # 1. Break-even, partial close, trailing — runs even while stopped
        try:
            for pos in self.open_positions(symbol):
                self.manage(pos, be_trigger(pos))
        except Exception:
            logger.exception("%s breakeven check failed for %s", self.label, symbol)

        if not self.enabled.is_set():
            return   # stopped: no new entries or overrides

        # 2. One trade per pair — only TFs higher than the running trade
        running  = self.open_positions(symbol)
        tf_names = self.scan_order(running)

        # 3. HTF-first scan: stop at the first TF that trades
        for tf_name in tf_names:
            try:
                if check(symbol, tf_name, running):
                    break
            except Exception:
                logger.exception("%s check failed for %s %s", self.label, symbol, tf_name)
