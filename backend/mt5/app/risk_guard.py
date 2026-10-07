"""
Account-wide limits shared by every bot (prop-firm style).

  Max daily loss   when today's net result of ALL bot trades — closed today plus today's
                   floating P/L of open ones, with commission and swap — reaches −limit,
                   every open bot trade is closed and no bot opens a new one until the
                   next broker day.
  Max open trades  a new trade is skipped while this many bot trades are open
                   (an override that replaces a trade doesn't add one).

Position size lives here too, so the dashboard can change it (bot_common.risk_volume):
  Risk per trade   % of the balance lost if a trade's SL is hit (0 = each bot's fixed lot)
  Min-lot over     the broker's minimum lot may risk up to this × the target, else skip

All four are set on the dashboard, which stores them in <BOTS_CONTROL_DIR>/_limits as
JSON. Until the dashboard saves a value, these env vars apply:
  BOTS_MAX_DAILY_LOSS    account currency, 0 = off (default 0)
  BOTS_MAX_OPEN_TRADES   0 = off (default 0)
  BOTS_RISK_PCT          % of balance per trade (default 0.5)
  BOTS_RISK_MAX_OVER     (default 1.5)
  BOTS_GUARD_INTERVAL    seconds between daily-loss checks (default 2)

Only trades with a bot's magic number count; manual trades are ignored. The broker day
starts at the D1 candle open (broker midnight). A halt lasts until the next broker day —
raising the limit doesn't lift it, setting it to 0 (off) does. After a restart the halt
comes back on its own if today's closed trades already lost the limit.

Lock order as everywhere: MT5_LOCK, then the journal lock (inside trade_db).
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

import trade_db
from mt5_guard import MT5_LOCK

logger = logging.getLogger(__name__)

LIMITS_FILE = "_limits"

limits = {"max_daily_loss": 0.0, "max_open_trades": 0, "risk_pct": 0.5, "risk_max_over": 1.5,
          "source": "env"}

# key -> (type, min, max) for values the dashboard may set
_FIELDS = {
    "max_daily_loss":  (float, 0.0, None),
    "max_open_trades": (int,   0,   None),
    "risk_pct":        (float, 0.0, 10.0),
    "risk_max_over":   (float, 1.0, 10.0),
}
state  = {"day_open": None, "halted_day": None, "today_net": None}

_bots = {}                 # bot_common.BOTS, passed in by start()
_control_dir = None
_control_mtime = None
_pos_cache = {}            # position id -> opened by a bot?
_start_cache = {}          # (ticket, day_open) -> floating P/L at the D1 open
_realized_cache = {}       # (ticket, volume) -> money already booked on that position
_started = False
_start_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _load_env():
    limits.update({
        "max_daily_loss":  max(0.0, float(os.environ.get("BOTS_MAX_DAILY_LOSS", "0"))),
        "max_open_trades": max(0, int(os.environ.get("BOTS_MAX_OPEN_TRADES", "0"))),
        "risk_pct":        max(0.0, float(os.environ.get("BOTS_RISK_PCT", "0.5"))),
        "risk_max_over":   max(1.0, float(os.environ.get("BOTS_RISK_MAX_OVER", "1.5"))),
        "source":          "env",
    })


def _load_control():
    """Apply the dashboard's _limits file when it changed. Bad files are logged and ignored."""
    global _control_mtime
    path = f"{_control_dir}/{LIMITS_FILE}"   # "/" not os.path.join: Python runs under Wine
    try:
        mtime = os.stat(path).st_mtime_ns
        if mtime == _control_mtime:
            return
        _control_mtime = mtime
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = json.loads(f.read(512))
        new = {}
        for key, (cast, lo, hi) in _FIELDS.items():
            if key not in raw:
                continue                      # older file: keep the current value
            value = cast(raw[key])
            if value != value or value < lo or (hi is not None and value > hi):
                raise ValueError(f"{key}={raw[key]!r} out of range")
            new[key] = value
        if not new:
            raise ValueError("no known settings")
    except FileNotFoundError:
        return
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("risk guard: ignoring %s (%s)", path, exc)
        return
    limits.update(new, source="dashboard")
    logger.info("risk guard: settings from dashboard: %s", new)
    _publish_limits()


def _publish_limits():
    trade_db.set_meta("limits", json.dumps(
        {k: limits[k] for k in (*_FIELDS, "source")}, sort_keys=True))


# ---------------------------------------------------------------------------
# Bot trades
# ---------------------------------------------------------------------------

def _owner(pos):
    """The bot that opened `pos`, or None for a manual / foreign trade."""
    for bot in list(_bots.values()):
        if bot._started and "magic_base" in bot.settings and bot.position_tf(pos):
            return bot
    return None


def _bot_positions():
    return [p for p in (mt5.positions_get() or []) if _owner(p)]


def _day_open():
    """Broker-time open of today's D1 candle, or None if MT5 can't say."""
    for bot in list(_bots.values()):
        for symbol in bot.settings.get("symbols", []):
            d1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, 1)
            if d1 is not None and len(d1):
                return int(d1[0]["time"]), symbol
    return None


def _bot_magics():
    magics = set()
    for bot in list(_bots.values()):
        if bot._started and "magic_base" in bot.settings:
            base = bot.settings["magic_base"]
            magics.update(base + m for m in (15, 30, 60, 240))
    return magics


def _is_bot_position(position_id, magics):
    """
    True if the position was opened by a bot. A close done by hand in MT5 or a stop-out
    carries magic 0, so the opening deal decides — looked up once per position.
    """
    if position_id not in _pos_cache:
        deals = mt5.history_deals_get(position=position_id) or []
        _pos_cache[position_id] = any(d.magic in magics and d.entry == mt5.DEAL_ENTRY_IN
                                      for d in deals)
    return _pos_cache[position_id]


def _start_profit(pos, day_open):
    """Floating P/L of an older position at today's open (the D1 open price), or None."""
    key = (pos.ticket, day_open)
    if key not in _start_cache:
        d1 = mt5.copy_rates_from_pos(pos.symbol, mt5.TIMEFRAME_D1, 0, 1)
        value = None
        if d1 is not None and len(d1) and int(d1[0]["time"]) >= day_open:
            action = mt5.ORDER_TYPE_BUY if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_SELL
            value = mt5.order_calc_profit(action, pos.symbol, pos.volume,
                                          pos.price_open, float(d1[0]["open"]))
        if value is None:
            return None                      # not cached: try again next pass
        _start_cache[key] = value
    return _start_cache[key]


def _realized(pos):
    """Booked so far on an open position: entry commission + any partial close, cached."""
    key = (pos.ticket, pos.volume)          # a partial close changes the volume → re-read
    if key not in _realized_cache:
        deals = mt5.history_deals_get(position=pos.ticket)
        if deals is None:
            return 0.0
        _realized_cache[key] = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
                                   for d in deals)
    return _realized_cache[key]


def _open_pnl(magics):
    """Running result of each open bot trade (ticket -> booked + floating), for the dashboard."""
    live = {}
    for p in mt5.positions_get() or []:
        if p.magic in magics:
            live[p.ticket] = round(_realized(p) + p.profit + p.swap, 2)
    for key in [k for k in _realized_cache if k[0] not in live]:
        del _realized_cache[key]
    return live


def _today_net(day_open):
    """
    Today's result of bot trades, like a prop firm's daily loss: (closed, floating).

      closed    every deal of a bot position dated today: profit + commission + swap + fee,
                whoever closed it (bot, SL/TP, by hand in MT5, stop-out)
      floating  open bot positions — opened today: their full P/L + swap;
                opened earlier: only the move since today's open (P/L now − P/L at the
                D1 open price), so yesterday's floating isn't counted again
    """
    magics = _bot_magics()
    # Deal times are broker time stored as epoch seconds: query a window around it
    deals = mt5.history_deals_get(datetime.fromtimestamp(day_open - 86400, timezone.utc),
                                  datetime.fromtimestamp(time.time() + 2 * 86400, timezone.utc))
    if deals is None:
        return None
    closed = 0.0
    for d in deals:
        if d.time < day_open or not d.position_id:      # position 0 = balance / credit
            continue
        if d.magic in magics or _is_bot_position(d.position_id, magics):
            closed += d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)

    floating = 0.0
    for p in mt5.positions_get() or []:
        if p.magic not in magics:
            continue
        if p.time >= day_open:
            floating += p.profit + p.swap
        else:
            start = _start_profit(p, day_open)
            floating += p.profit - start if start is not None else p.profit + p.swap
    return closed, floating


def _publish_today(closed, floating, force=False):
    """
    Today's figures and each open trade's running P/L, for the dashboard. Throttled
    (at most every 30 s, and only on a move of ≥ 1 or 2 % of the limit), because each
    write refreshes the snapshot the page downloads.
    """
    net = closed + floating
    live = _open_pnl(_bot_magics())
    total = sum(live.values())
    last = state.get("published")
    step = max(1.0, limits["max_daily_loss"] * 0.02)
    if not force and last and (time.time() - last[0] < 30 or
                               (abs(net - last[1]) < step and abs(total - last[2]) < step
                                and set(live) == last[3])):
        return
    state["published"] = (time.time(), net, total, set(live))
    trade_db.set_meta("today", json.dumps({
        "net": round(net, 2), "closed": round(closed, 2), "floating": round(floating, 2),
        "open_total": round(total, 2), "open": {str(k): v for k, v in live.items()},
        "day_open": state["day_open"], "at": int(time.time())}))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def halted():
    return state["halted_day"] is not None and state["halted_day"] == state["day_open"]


def entry_block(replacing=0):
    """
    None if a new bot trade may open, else (reason, details). Called from Bot.place
    with MT5_LOCK held. `replacing` = bot trades the new one closes (override).
    """
    if halted():
        return ("daily loss limit hit", {"limit": limits["max_daily_loss"],
                                         "today_net": round(state["today_net"] or 0, 2)})
    cap = limits["max_open_trades"]
    if cap > 0:
        n = len(_bot_positions())
        if n - replacing >= cap:
            return ("max open trades reached", {"open": n, "limit": cap})
    return None


def _close_all():
    for pos in _bot_positions():
        bot = _owner(pos)
        if bot.close(pos, "limit") is not None:
            bot.record(pos.symbol, bot.position_tf(pos), "limit_close", ticket=pos.ticket,
                       profit=round(pos.profit, 2))


def check():
    """One pass: new day → clear the halt; loss ≥ limit → halt and close everything."""
    got = _day_open()
    if got is None:
        return
    day, symbol = got
    if state["day_open"] != day:
        state["day_open"] = day
        _pos_cache.clear()
        _start_cache.clear()
        if state["halted_day"] is not None and state["halted_day"] != day:
            logger.info("risk guard: new broker day — daily loss halt lifted")
            state["halted_day"] = None
            trade_db.set_meta("halted_until", "")

    got = _today_net(day)
    if got is None:
        return
    closed, floating = got
    net = closed + floating
    state["today_net"] = net

    limit = limits["max_daily_loss"]
    if limit <= 0:
        _publish_today(closed, floating)
        if halted():
            logger.info("risk guard: daily loss limit switched off — halt lifted")
            state["halted_day"] = None
            trade_db.set_meta("halted_until", "")
        return

    if not halted() and net <= -limit:
        _publish_today(closed, floating, force=True)
        state["halted_day"] = day
        tick = mt5.symbol_info_tick(symbol)
        broker_now = tick.time if tick else day
        until = int(time.time() + max(0, day + 86400 - broker_now))   # next broker midnight, UTC
        logger.warning("risk guard: daily loss %.2f reached the %.2f limit — closing all bot "
                       "trades, no new ones until the next broker day", net, limit)
        trade_db.set_meta("halted_until", json.dumps(
            {"until": until, "at": int(time.time()), "net": round(net, 2), "limit": limit}))
    else:
        _publish_today(closed, floating)

    if halted():
        _close_all()   # repeats every pass until nothing is left (a failed close is retried)


def _loop(interval):
    while True:
        try:
            _load_control()
            with MT5_LOCK:
                check()
        except Exception:
            logger.exception("risk guard check failed")
        time.sleep(interval)


def start(bots, control_dir):
    """Start the guard thread once (from the first Bot.start)."""
    global _started, _control_dir, _bots
    with _start_lock:
        if _started:
            return
        _started = True
    _bots = bots                      # the live registry: bots started later are seen too
    _control_dir = control_dir
    _load_env()
    _publish_limits()
    _load_control()
    try:
        with MT5_LOCK:
            check()                   # before any bot trades: restores a halt after a restart
    except Exception:
        logger.exception("risk guard first check failed")
    interval = float(os.environ.get("BOTS_GUARD_INTERVAL", "2"))
    threading.Thread(target=_loop, args=(interval,), daemon=True, name="risk-guard").start()
    logger.info("risk guard started: max daily loss %s, max open trades %s, risk %s%% (%s)",
                limits["max_daily_loss"] or "off", limits["max_open_trades"] or "off",
                limits["risk_pct"], limits["source"])
