"""
SQLite trade journal for the bots.

  bots.db       live database, written only by this process
  dashboard.db  read-only snapshot, refreshed every BOT_DB_SYNC_SECONDS, served to
                the dashboard as a plain file (no API) — see backend/dashboard

What is stored:
  trades  one row per MT5 position: bot, symbol, timeframe, side, entry, initial
          SL, TP, risk, break-even, exit price/time/reason, profit, commission,
          swap, net and R (net ÷ money at risk)
  events  every signal / skip / open / close / break-even the bots record
  bots    every registered bot: label, description, color slot, current settings,
          enabled flag — the dashboard builds its bot list from this table, so a
          new strategy shows up without changing the dashboard
  meta    snapshot time, broker time offset

How trades get in:
  • a bot opens a trade        → trade_opened() (full detail)
  • a bot moves SL to BE       → trade_breakeven()
  • a bot closes a trade       → set_exit_hint() (e.g. "override")
  • every sync (background)    → closed positions are completed from MT5 deal
                                 history; open bot positions the DB doesn't know
                                 are adopted; on first sync per bot, the last
                                 BOT_DB_BACKFILL_DAYS of history are back-filled

Bots are recognised by magic number: register_bot(name, magic_base, ...) claims
magic_base + 15/30/60/240. Use a different magic_base for every bot.

Settings (env vars):
  BOT_DB_DIR             directory for both files (default /config/data)
  BOT_DB_SYNC_SECONDS    seconds between syncs + snapshots (default 30)
  BOT_DB_BACKFILL_DAYS   days of MT5 history to import on start (default 90)
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

DB_DIR        = os.environ.get("BOT_DB_DIR", "/config/data")
DB_PATH       = os.path.join(DB_DIR, "bots.db")
SNAPSHOT_PATH = os.path.join(DB_DIR, "dashboard.db")
SYNC_SECONDS  = float(os.environ.get("BOT_DB_SYNC_SECONDS", "30"))
BACKFILL_DAYS = int(os.environ.get("BOT_DB_BACKFILL_DAYS", "90"))

TF_MINUTES = {15: "M15", 30: "M30", 60: "H1", 240: "H4"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    ticket          INTEGER PRIMARY KEY,  -- MT5 position ticket
    bot             TEXT NOT NULL,        -- crt3 / crt2
    symbol          TEXT NOT NULL,
    timeframe       TEXT,                 -- H4 / H1 / M30 / M15
    side            TEXT NOT NULL,        -- BUY / SELL
    volume          REAL,
    magic           INTEGER,
    comment         TEXT,
    signal_time     INTEGER,              -- open time of the candle that defined the setup (broker time)
    opened_at       INTEGER,              -- broker time, unix seconds
    entry_price     REAL,
    sl_initial      REAL,
    tp              REAL,
    risk_money      REAL,                 -- loss at the initial SL, account currency, before commission
    spread_at_entry REAL,
    be_trigger      REAL,                 -- price at which SL moves to break-even
    be_sl           REAL,                 -- the break-even SL that was set
    be_at           INTEGER,              -- UTC unix seconds
    exit_hint       TEXT,                 -- set when a bot closes the trade itself (e.g. override)
    status          TEXT NOT NULL DEFAULT 'open',   -- open / closed
    closed_at       INTEGER,              -- broker time, unix seconds
    exit_price      REAL,
    exit_reason     TEXT,                 -- tp / sl / breakeven / override / manual / stopout / other
    profit          REAL,
    commission      REAL,
    swap            REAL,
    fee             REAL,
    net             REAL,                 -- profit + commission + swap + fee
    r_multiple      REAL,                 -- net / risk_money
    source          TEXT                  -- bot (opened while journaling) / history (back-filled)
);
CREATE INDEX IF NOT EXISTS trades_status_closed ON trades(status, closed_at);
CREATE INDEX IF NOT EXISTS trades_bot ON trades(bot);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    time      INTEGER NOT NULL,           -- UTC unix seconds
    bot       TEXT,
    symbol    TEXT,
    timeframe TEXT,
    status    TEXT,                       -- signal / opened / skipped / rejected / error / closed / breakeven
    side      TEXT,
    reason    TEXT,
    ticket    INTEGER,
    details   TEXT                        -- JSON
);
CREATE INDEX IF NOT EXISTS events_time ON events(time);

CREATE TABLE IF NOT EXISTS bots (
    name        TEXT PRIMARY KEY,         -- key used in trades.bot / events.bot
    label       TEXT,                     -- display name
    description TEXT,
    magic_base  INTEGER,
    color_slot  INTEGER,                  -- 1..8, fixed at first registration
    settings    TEXT,                     -- JSON, refreshed every snapshot
    enabled     INTEGER,
    first_seen  INTEGER,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_lock    = threading.RLock()
_conn    = None
_bots    = {}       # magic -> (bot name, timeframe)
_state_fns = {}     # bot name -> callable returning (settings dict, enabled bool)
_backfilled = set() # bot names already back-filled this run
_sync_started = False


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def init():
    """Open the database and start the sync thread (both once)."""
    global _conn, _sync_started
    with _lock:
        if _conn is None:
            os.makedirs(DB_DIR, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            # Rollback journal (not WAL): a single writer, and the snapshot copy
            # must be one self-contained file.
            _conn.execute("PRAGMA journal_mode=DELETE")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            _conn.commit()
            logger.info("trade_db: using %s (snapshot %s)", DB_PATH, SNAPSHOT_PATH)

        if not _sync_started:
            _sync_started = True
            threading.Thread(target=_sync_loop, daemon=True, name="trade-db-sync").start()


def register_bot(name, magic_base, label=None, description=None, state_fn=None):
    """
    Claim magic_base + 15/30/60/240 for `name` and record the bot for the dashboard.
    `state_fn()` -> (settings, enabled) is polled on every snapshot.
    """
    with _lock:
        for minutes, tf_name in TF_MINUTES.items():
            magic = magic_base + minutes
            owner = _bots.get(magic)
            if owner and owner[0] != name:
                logger.error("trade_db: magic %s of %s already used by %s", magic, name, owner[0])
            _bots[magic] = (name, tf_name)
        if state_fn:
            _state_fns[name] = state_fn
        if _conn is None:
            return
        row = _conn.execute("SELECT color_slot FROM bots WHERE name = ?", (name,)).fetchone()
        if row is None:
            used = {r[0] for r in _conn.execute("SELECT color_slot FROM bots")}
            slot = next((s for s in range(1, 9) if s not in used), None)
            _conn.execute(
                "INSERT INTO bots (name, label, description, magic_base, color_slot, first_seen)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, label or name, description, magic_base, slot, int(time.time())))
        else:
            _conn.execute("UPDATE bots SET label = ?, description = ?, magic_base = ? WHERE name = ?",
                          (label or name, description, magic_base, name))
        _conn.commit()


def _update_bot_states():
    for name, fn in list(_state_fns.items()):
        try:
            settings, enabled = fn()
        except Exception:
            logger.exception("trade_db: state of %s unavailable", name)
            continue
        _write("UPDATE bots SET settings = ?, enabled = ?, updated_at = ? WHERE name = ?",
               (json.dumps(settings, default=_json_default), 1 if enabled else 0,
                int(time.time()), name))


def _write(sql, params=()):
    if _conn is None:
        return
    with _lock:
        try:
            _conn.execute(sql, params)
            _conn.commit()
        except Exception:
            logger.exception("trade_db write failed: %s", sql.split()[0:3])


def _json_default(value):
    try:
        return float(value)       # numpy floats/ints from MT5 rates
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Called by the bots
# ---------------------------------------------------------------------------

def log_event(bot, symbol, timeframe, status, details):
    _write(
        "INSERT INTO events (time, bot, symbol, timeframe, status, side, reason, ticket, details)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(time.time()), bot, symbol, timeframe, status,
         details.get("side"), details.get("reason"), details.get("ticket"),
         json.dumps(details, default=_json_default)),
    )


def trade_opened(ticket, bot, symbol, timeframe, side, volume, magic, comment,
                 signal_time, opened_at, entry_price, sl_initial, tp, risk_money,
                 spread_at_entry, be_trigger):
    _write(
        "INSERT OR REPLACE INTO trades (ticket, bot, symbol, timeframe, side, volume, magic,"
        " comment, signal_time, opened_at, entry_price, sl_initial, tp, risk_money,"
        " spread_at_entry, be_trigger, status, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 'bot')",
        (ticket, bot, symbol, timeframe, side, volume, magic, comment, signal_time,
         opened_at, entry_price, sl_initial, tp, risk_money, spread_at_entry, be_trigger),
    )


def trade_breakeven(ticket, new_sl):
    _write("UPDATE trades SET be_sl = ?, be_at = ? WHERE ticket = ?",
           (new_sl, int(time.time()), ticket))


def set_exit_hint(ticket, hint):
    _write("UPDATE trades SET exit_hint = ? WHERE ticket = ?", (hint, ticket))


# ---------------------------------------------------------------------------
# Sync with MT5 history
# ---------------------------------------------------------------------------

def _known(ticket):
    with _lock:
        return _conn.execute("SELECT 1 FROM trades WHERE ticket = ?", (ticket,)).fetchone() is not None


def _insert_from_history(ticket, pos=None):
    """Create a trade row for a position the bots didn't journal (older or adopted)."""
    deals = mt5.history_deals_get(position=ticket) or []
    entry = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_IN), None)
    if entry is None and pos is None:
        return

    magic  = entry.magic if entry else pos.magic
    owner  = _bots.get(magic)
    if owner is None:
        return
    bot, timeframe = owner

    symbol = entry.symbol if entry else pos.symbol
    if entry:
        side = "BUY" if entry.type == mt5.DEAL_TYPE_BUY else "SELL"
        volume, price, opened_at, comment = entry.volume, entry.price, entry.time, entry.comment
    else:
        side = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
        volume, price, opened_at, comment = pos.volume, pos.price_open, pos.time, pos.comment

    # The opening order keeps the SL/TP it was sent with (before any break-even move)
    orders = mt5.history_orders_get(position=ticket) or []
    sl0 = next((o.sl for o in orders if o.sl), None) or (pos.sl if pos else None)
    tp  = next((o.tp for o in orders if o.tp), None) or (pos.tp if pos else None)

    risk_money = None
    if sl0:
        action = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        loss = mt5.order_calc_profit(action, symbol, volume, price, sl0)
        risk_money = abs(loss) if loss is not None else None

    _write(
        "INSERT OR IGNORE INTO trades (ticket, bot, symbol, timeframe, side, volume, magic,"
        " comment, opened_at, entry_price, sl_initial, tp, risk_money, status, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 'history')",
        (ticket, bot, symbol, timeframe, side, volume, magic, comment,
         int(opened_at), price, sl0, tp, risk_money),
    )


def _exit_reason(row, last_out, exit_price):
    if row["exit_hint"]:
        return row["exit_hint"]

    reason = last_out.reason
    if reason == mt5.DEAL_REASON_TP:
        return "tp"
    if reason == mt5.DEAL_REASON_SL:
        # An SL that filled at (or past) entry is a break-even exit, whether the
        # bot journaled the move or the trade was back-filled from history.
        entry, sl0 = row["entry_price"], row["sl_initial"]
        if entry and sl0 and sl0 != entry:
            direction = 1 if row["side"] == "BUY" else -1
            r_price = direction * (exit_price - entry) / abs(entry - sl0)
            if r_price >= -0.1:
                return "breakeven"
        return "sl"
    if reason == mt5.DEAL_REASON_SO:
        return "stopout"
    if reason in (mt5.DEAL_REASON_CLIENT, mt5.DEAL_REASON_MOBILE, mt5.DEAL_REASON_WEB):
        return "manual"
    return "other"


def _complete_from_history(row):
    """Fill exit details for a position that is no longer open."""
    deals = mt5.history_deals_get(position=row["ticket"]) or []
    outs  = [d for d in deals
             if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY, mt5.DEAL_ENTRY_INOUT)]
    if not outs:
        return   # history not there yet — try again next sync

    out_volume = sum(d.volume for d in outs) or 1.0
    exit_price = sum(d.price * d.volume for d in outs) / out_volume
    last_out   = max(outs, key=lambda d: d.time_msc)
    profit     = sum(d.profit for d in deals)
    commission = sum(d.commission for d in deals)
    swap       = sum(d.swap for d in deals)
    fee        = sum(getattr(d, "fee", 0.0) for d in deals)
    net        = profit + commission + swap + fee
    risk       = row["risk_money"]
    r_multiple = net / risk if risk else None

    # Use the actual fill for entry when the deal is available
    entry = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_IN), None)
    entry_price = entry.price if entry else row["entry_price"]
    opened_at   = entry.time if entry else row["opened_at"]
    reason = _exit_reason(dict(row, entry_price=entry_price), last_out, exit_price)

    _write(
        "UPDATE trades SET status = 'closed', closed_at = ?, exit_price = ?, exit_reason = ?,"
        " profit = ?, commission = ?, swap = ?, fee = ?, net = ?, r_multiple = ?,"
        " entry_price = ?, opened_at = ? WHERE ticket = ?",
        (int(last_out.time), exit_price, reason, profit, commission, swap, fee, net,
         r_multiple, entry_price, int(opened_at), row["ticket"]),
    )


def _backfill(bot_name):
    """Import this bot's positions from the last BACKFILL_DAYS of MT5 history."""
    magics = {m for m, (name, _) in _bots.items() if name == bot_name}
    date_from = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
    date_to   = datetime.now(timezone.utc) + timedelta(days=1)   # broker time can run ahead of UTC
    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
        raise RuntimeError(f"history_deals_get failed: {mt5.last_error()}")

    tickets = {d.position_id for d in deals
               if d.magic in magics and d.entry == mt5.DEAL_ENTRY_IN}
    added = 0
    for ticket in tickets:
        if not _known(ticket):
            _insert_from_history(ticket)
            added += 1
    logger.info("trade_db: back-filled %d %s trade(s) from %d days of history",
                added, bot_name, BACKFILL_DAYS)


def sync():
    if _conn is None or mt5.terminal_info() is None:
        return

    # Back-fill each registered bot once per run
    for bot_name in {name for name, _ in _bots.values()} - _backfilled:
        try:
            _backfill(bot_name)
            _backfilled.add(bot_name)
        except Exception:
            logger.exception("trade_db: back-fill failed for %s", bot_name)

    # Adopt open bot positions the DB doesn't know about
    for pos in mt5.positions_get() or []:
        if pos.magic in _bots and not _known(pos.ticket):
            _insert_from_history(pos.ticket, pos)

    # Complete trades that are no longer open
    with _lock:
        rows = _conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
    for row in rows:
        if not mt5.positions_get(ticket=row["ticket"]):
            _complete_from_history(row)

    _update_broker_offset()


def _update_broker_offset():
    """
    Trade times are broker server time; events are UTC. Store the difference
    (rounded to 30 min) so the dashboard can show both in the viewer's local time.
    A stale tick (weekend) gives a gap far beyond any real offset and is ignored.
    """
    with _lock:
        row = _conn.execute("SELECT symbol FROM trades ORDER BY opened_at DESC LIMIT 1").fetchone()
    tick = mt5.symbol_info_tick(row["symbol"]) if row else None
    if tick is None:
        return
    diff = tick.time - time.time()
    if abs(diff) > 14 * 3600:
        return
    offset = round(diff / 1800) * 1800
    _write("INSERT OR REPLACE INTO meta (key, value) VALUES ('broker_offset_seconds', ?)",
           (str(offset),))


def snapshot():
    """Copy the live DB to SNAPSHOT_PATH atomically (readers never see a half-written file)."""
    if _conn is None:
        return
    _update_bot_states()
    _write("INSERT OR REPLACE INTO meta (key, value) VALUES ('snapshot_at', ?)",
           (str(int(time.time())),))
    tmp = SNAPSHOT_PATH + ".tmp"
    with _lock:
        if os.path.exists(tmp):
            os.remove(tmp)
        dst = sqlite3.connect(tmp)
        try:
            _conn.backup(dst)
        finally:
            dst.close()
    os.replace(tmp, SNAPSHOT_PATH)


def _sync_loop():
    time.sleep(5)   # let both bots register first
    while True:
        try:
            sync()
        except Exception:
            logger.exception("trade_db: sync failed")
        try:
            snapshot()
        except Exception:
            logger.exception("trade_db: snapshot failed")
        time.sleep(SYNC_SECONDS)
