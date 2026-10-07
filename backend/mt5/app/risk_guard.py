"""
Account-wide limits shared by every bot (prop-firm style).

  Max daily loss   when today's net result of ALL bot trades — closed today plus the
                   floating P/L of open ones, with commission and swap — reaches −limit,
                   every open bot trade is closed and no bot opens a new one until the
                   next broker day.
  Max open trades  a new trade is skipped while this many bot trades are open
                   (an override that replaces a trade doesn't add one).

Both are set on the dashboard, which stores them in <BOTS_CONTROL_DIR>/_limits as JSON.
Until the dashboard saves a value, these env vars apply:
  BOTS_MAX_DAILY_LOSS    account currency, 0 = off (default 0)
  BOTS_MAX_OPEN_TRADES   0 = off (default 0)
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

limits = {"max_daily_loss": 0.0, "max_open_trades": 0, "source": "env"}
state  = {"day_open": None, "halted_day": None, "today_net": None}

_bots = {}                 # bot_common.BOTS, passed in by start()
_control_dir = None
_control_mtime = None
_started = False
_start_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _load_env():
    limits.update({
        "max_daily_loss":  max(0.0, float(os.environ.get("BOTS_MAX_DAILY_LOSS", "0"))),
        "max_open_trades": max(0, int(os.environ.get("BOTS_MAX_OPEN_TRADES", "0"))),
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
            raw = json.loads(f.read(256))
        loss = float(raw["max_daily_loss"])
        trades = int(raw["max_open_trades"])
        if loss < 0 or trades < 0 or loss != loss:
            raise ValueError("negative or NaN")
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("risk guard: ignoring %s (%s)", path, exc)
        return
    limits.update({"max_daily_loss": loss, "max_open_trades": trades, "source": "dashboard"})
    logger.info("risk guard: limits from dashboard: max daily loss %.2f, max open trades %d",
                loss, trades)
    _publish_limits()


def _publish_limits():
    trade_db.set_meta("limits", json.dumps(
        {k: limits[k] for k in ("max_daily_loss", "max_open_trades", "source")}, sort_keys=True))


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


def _today_net(day_open):
    """Closed today + floating, for bot trades only, in account currency."""
    magics = set()
    for bot in list(_bots.values()):
        if bot._started and "magic_base" in bot.settings:
            base = bot.settings["magic_base"]
            magics.update(base + m for m in (15, 30, 60, 240))
    # Deal times are broker time stored as epoch seconds: query a window around it
    deals = mt5.history_deals_get(datetime.fromtimestamp(day_open - 86400, timezone.utc),
                                  datetime.fromtimestamp(time.time() + 2 * 86400, timezone.utc))
    if deals is None:
        return None
    closed = sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)
                 for d in deals if d.time >= day_open and d.magic in magics)
    floating = sum(p.profit + p.swap for p in (mt5.positions_get() or []) if p.magic in magics)
    return closed + floating


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
        if state["halted_day"] is not None and state["halted_day"] != day:
            logger.info("risk guard: new broker day — daily loss halt lifted")
            state["halted_day"] = None
            trade_db.set_meta("halted_until", "")

    limit = limits["max_daily_loss"]
    if limit <= 0:
        if halted():
            logger.info("risk guard: daily loss limit switched off — halt lifted")
            state["halted_day"] = None
            trade_db.set_meta("halted_until", "")
        return

    net = _today_net(day)
    if net is None:
        return
    state["today_net"] = net

    if not halted() and net <= -limit:
        state["halted_day"] = day
        tick = mt5.symbol_info_tick(symbol)
        broker_now = tick.time if tick else day
        until = int(time.time() + max(0, day + 86400 - broker_now))   # next broker midnight, UTC
        logger.warning("risk guard: daily loss %.2f reached the %.2f limit — closing all bot "
                       "trades, no new ones until the next broker day", net, limit)
        trade_db.set_meta("halted_until", json.dumps(
            {"until": until, "at": int(time.time()), "net": round(net, 2), "limit": limit}))

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
    logger.info("risk guard started: max daily loss %s, max open trades %s (%s)",
                limits["max_daily_loss"] or "off", limits["max_open_trades"] or "off",
                limits["source"])
