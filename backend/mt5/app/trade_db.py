"""
SQLite trade journal for the bots.

  bots.db       live database with full detail, written only by this process
  dashboard.db  small read-only snapshot for the dashboard, served as a plain file
                (no API) — see backend/dashboard. It holds only what the page shows:
                trades (dashboard columns), bots, meta and event_counts. Raw events
                stay in bots.db. It is rewritten only when something changed:
                trade / bot changes within BOT_DB_SYNC_SECONDS, event-count-only
                changes at most every BOT_DB_EVENT_SNAPSHOT_SECONDS.

What is stored:
  trades  one row per MT5 position: bot, symbol, timeframe, side, entry, initial
          SL, TP, risk, break-even, exit price/time/reason, profit, commission,
          swap, net and R (net ÷ money at risk)
  events  every signal / skip / open / close / break-even the bots record
  event_counts  the same events counted per UTC day × bot × symbol × timeframe ×
          status × reason (numbers in reasons masked), kept up to date on every
          write — this is what the dashboard reads, so its size grows by days,
          not by events. Days older than BOT_DB_DAILY_COUNT_DAYS are merged into
          weekly rows (Monday start).
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

Threads: every bot thread and the sync thread share ONE connection behind ONE
re-entrant lock (_lock), so writes are serialized and SQLite never sees two writers.
Lock order is always MT5_LOCK → _lock (see mt5_guard.py): nothing in this module calls
MT5 while holding _lock, so the two locks cannot deadlock.

Bots are recognised by magic number: register_bot(name, magic_base, ...) claims
magic_base + 15/30/60/240. Use a different magic_base for every bot.

Settings (env vars):
  BOT_DB_DIR             directory for both files (default /config/data)
  BOT_DB_SYNC_SECONDS    seconds between syncs + snapshots (default 30)
  BOT_DB_BACKFILL_DAYS   days of MT5 history to import on start (default 90)
  BOT_DB_EVENT_SNAPSHOT_SECONDS  min seconds between snapshots caused only by new
                         events (default 300)
  BOT_DB_DAILY_COUNT_DAYS  days of daily event counts before merging into weeks (default 30)

Clear all data (dashboard button): the page writes a request id to
<BOTS_CONTROL_DIR>/_reset. On the next sync every trade and event is deleted (no backup),
bots and meta (limits, broker offset) are kept, and trades opened before the reset are
never imported again — not by the back-fill, not by adopting open positions.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

from mt5_guard import MT5_LOCK

logger = logging.getLogger(__name__)

# Settings — read in init() (after app.py's load_dotenv), see _load_settings()
DB_DIR = DB_PATH = SNAPSHOT_PATH = None
SYNC_SECONDS = 30.0
BACKFILL_DAYS = 90
EVENT_SNAPSHOT_SECONDS = 300.0
DAILY_COUNT_DAYS = 30
CONTROL_DIR = "/config/control"


def _load_settings():
    global DB_DIR, DB_PATH, SNAPSHOT_PATH, SYNC_SECONDS, BACKFILL_DAYS
    global EVENT_SNAPSHOT_SECONDS, DAILY_COUNT_DAYS, CONTROL_DIR
    DB_DIR        = os.environ.get("BOT_DB_DIR", "/config/data")
    DB_PATH       = os.path.join(DB_DIR, "bots.db")
    SNAPSHOT_PATH = os.path.join(DB_DIR, "dashboard.db")
    SYNC_SECONDS  = float(os.environ.get("BOT_DB_SYNC_SECONDS", "30"))
    BACKFILL_DAYS = int(os.environ.get("BOT_DB_BACKFILL_DAYS", "90"))
    EVENT_SNAPSHOT_SECONDS = float(os.environ.get("BOT_DB_EVENT_SNAPSHOT_SECONDS", "300"))
    DAILY_COUNT_DAYS = int(os.environ.get("BOT_DB_DAILY_COUNT_DAYS", "30"))
    CONTROL_DIR   = os.environ.get("BOTS_CONTROL_DIR", "/config/control").rstrip("/\\")

TF_MINUTES = {15: "M15", 30: "M30", 60: "H1", 240: "H4", 1440: "D1"}   # D1: candle2_fractal_bot

# Bots whose code was deleted: their trades, events and dashboard entry are removed on start
REMOVED_BOTS = ("dsweep", "ema2050", "ema921", "golden", "rsi", "bbands", "supertrend",
                "stoch", "ichimoku", "supertrend_pt", "macd", "donchian", "donchian_pt")

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

CREATE TABLE IF NOT EXISTS event_counts (
    day       INTEGER NOT NULL,           -- UTC day start, unix seconds
    bot       TEXT NOT NULL DEFAULT '',
    symbol    TEXT NOT NULL DEFAULT '',
    timeframe TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT '',
    reason    TEXT NOT NULL DEFAULT '',   -- numbers replaced by N
    n         INTEGER NOT NULL,
    PRIMARY KEY (day, bot, symbol, timeframe, status, reason)
);

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
_dirty   = True     # trades / bots / meta changed since the last snapshot
_dirty_events = False  # only event counts changed since the last snapshot
_last_snapshot = 0.0
_last_compact  = 0.0
_bot_state_cache = {}  # bot name -> last written (settings JSON, enabled)

# Newest raw events copied into the snapshot for the dashboard's bot log
RECENT_EVENTS = int(os.environ.get("BOT_DB_RECENT_EVENTS", "300"))

# Columns the dashboard reads (the cost / timing detail is for its trade export);
# everything else stays in bots.db only
SNAPSHOT_TRADE_COLUMNS = (
    "ticket, bot, symbol, timeframe, side, volume, signal_time, opened_at, entry_price,"
    " sl_initial, tp, risk_money, spread_at_entry, be_trigger, be_sl, be_at, status,"
    " closed_at, exit_price, exit_reason, profit, commission, swap, fee, net, r_multiple, source"
)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def init():
    """Open the database and start the sync thread (both once)."""
    global _conn, _sync_started
    with _lock:
        if _conn is None:
            _load_settings()
            os.makedirs(DB_DIR, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            # Rollback journal (not WAL): a single writer, and the snapshot copy
            # must be one self-contained file.
            _conn.execute("PRAGMA journal_mode=DELETE")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            _conn.commit()
            _purge_removed_bots()
            _rebuild_event_counts_if_empty()
            logger.info("trade_db: using %s (snapshot %s)", DB_PATH, SNAPSHOT_PATH)

        if not _sync_started:
            _sync_started = True
            threading.Thread(target=_sync_loop, daemon=True, name="trade-db-sync").start()


def _purge_removed_bots():
    """Delete everything recorded for bots whose code was removed, so the dashboard drops them."""
    marks = ",".join("?" * len(REMOVED_BOTS))
    found = _conn.execute(f"SELECT COUNT(*) FROM bots WHERE name IN ({marks})",
                          REMOVED_BOTS).fetchone()[0]
    if not found:
        return
    for table in ("trades", "events", "event_counts", "bots"):
        col = "name" if table == "bots" else "bot"
        _conn.execute(f"DELETE FROM {table} WHERE {col} IN ({marks})", REMOVED_BOTS)
    _conn.commit()
    logger.warning("trade_db: removed the data of %d deleted bot(s)", found)


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
        state = (json.dumps(settings, default=_json_default, sort_keys=True), 1 if enabled else 0)
        if _bot_state_cache.get(name) == state:
            continue   # unchanged — don't mark the snapshot stale
        _write("UPDATE bots SET settings = ?, enabled = ?, updated_at = ? WHERE name = ?",
               (state[0], state[1], int(time.time()), name))
        _bot_state_cache[name] = state


def _write(sql, params=()):
    _write_many([(sql, params)])


def _write_many(statements, events_only=False):
    """Run statements in one transaction and mark the snapshot as stale."""
    global _dirty, _dirty_events
    if _conn is None:
        return
    with _lock:
        try:
            for sql, params in statements:
                _conn.execute(sql, params)
            _conn.commit()
            if events_only:
                _dirty_events = True
            else:
                _dirty = True
        except Exception:
            _conn.rollback()
            logger.exception("trade_db write failed: %s", statements[0][0].split()[0:3])


def normalize_reason(reason):
    """Group skip reasons that differ only by numbers ("RR 0.42 below minimum 1.0")."""
    if not reason:
        return ""
    if reason.startswith("RR "):
        return "Reward:risk below minimum"
    m = re.match(r"rejected \d+", reason)        # keep the broker retcode, mask the rest
    if m:
        return m.group(0) + re.sub(r"\d+(\.\d+)?", "N", reason[m.end():])
    return re.sub(r"\d+(\.\d+)?", "N", reason)


def _rebuild_event_counts_if_empty():
    """One-time migration: build event_counts from events recorded before it existed."""
    if _conn.execute("SELECT 1 FROM event_counts LIMIT 1").fetchone():
        return
    if not _conn.execute("SELECT 1 FROM events LIMIT 1").fetchone():
        return
    _conn.create_function("norm_reason", 1, normalize_reason)
    _conn.execute(
        "INSERT INTO event_counts (day, bot, symbol, timeframe, status, reason, n)"
        " SELECT time - time % 86400, COALESCE(bot, ''), COALESCE(symbol, ''),"
        "        COALESCE(timeframe, ''), COALESCE(status, ''), norm_reason(reason), COUNT(*)"
        " FROM events GROUP BY 1, 2, 3, 4, 5, 6")
    _conn.commit()
    logger.info("trade_db: built event_counts from existing events")


def _json_default(value):
    try:
        return float(value)       # numpy floats/ints from MT5 rates
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Called by the bots
# ---------------------------------------------------------------------------

def log_event(bot, symbol, timeframe, status, details):
    now = int(time.time())
    _write_many([
        ("INSERT INTO events (time, bot, symbol, timeframe, status, side, reason, ticket, details)"
         " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
         (now, bot, symbol, timeframe, status,
          details.get("side"), details.get("reason"), details.get("ticket"),
          json.dumps(details, default=_json_default))),
        ("INSERT INTO event_counts (day, bot, symbol, timeframe, status, reason, n)"
         " VALUES (?, ?, ?, ?, ?, ?, 1)"
         " ON CONFLICT (day, bot, symbol, timeframe, status, reason) DO UPDATE SET n = n + 1",
         (now - now % 86400, bot or "", symbol or "", timeframe or "", status or "",
          normalize_reason(details.get("reason")))),
    ], events_only=True)


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


def trade_info(ticket):
    """Entry, initial SL, opened volume and BE trigger of a journaled trade (dict), or None."""
    if _conn is None:
        return None
    with _lock:
        row = _conn.execute("SELECT entry_price, sl_initial, volume, be_trigger FROM trades"
                            " WHERE ticket = ?", (ticket,)).fetchone()
    return dict(row) if row else None


def set_meta(key, value):
    """Store a dashboard value in meta; only writes (and dirties the snapshot) on a change."""
    if _conn is None:
        return
    with _lock:
        row = _conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] != value:
        _write("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


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
            if r_price >= 0.3:
                return "trail"          # an SL this far in profit was trailed there
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
    # Classify by the LAST fill: the average includes any partial close taken earlier
    reason = _exit_reason(dict(row, entry_price=entry_price), last_out, last_out.price)

    _write(
        "UPDATE trades SET status = 'closed', closed_at = ?, exit_price = ?, exit_reason = ?,"
        " profit = ?, commission = ?, swap = ?, fee = ?, net = ?, r_multiple = ?,"
        " entry_price = ?, opened_at = ? WHERE ticket = ?",
        (int(last_out.time), exit_price, reason, profit, commission, swap, fee, net,
         r_multiple, entry_price, int(opened_at), row["ticket"]),
    )


def _meta(key):
    with _lock:
        row = _conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _reset_cutoff():
    """Broker time of the last "Clear all data"; trades opened before it stay out. 0 = none."""
    try:
        return int(_meta("reset_at_broker") or 0)
    except ValueError:
        return 0


def _check_reset():
    """Run a "Clear all data" requested by the dashboard (once per request id)."""
    try:
        with open(f"{CONTROL_DIR}/_reset", encoding="ascii", errors="replace") as f:
            request = f.read(64).strip()
    except OSError:
        return
    if request and request != _meta("reset_id"):
        reset(request)


def reset(request_id):
    """Delete every trade and event (no backup); keep bots and meta. See module docstring."""
    global _dirty
    try:
        offset = int(_meta("broker_offset_seconds") or 0)
    except ValueError:
        offset = 0
    now = int(time.time())
    with _lock:
        for table in ("trades", "events", "event_counts"):
            _conn.execute(f"DELETE FROM {table}")
        _conn.execute("UPDATE bots SET first_seen = ?", (now,))
        for key, value in (("reset_id", request_id), ("reset_at", str(now)),
                           ("reset_at_broker", str(now + offset))):
            _conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        _conn.commit()
        _conn.execute("VACUUM")          # give the space back; outside any transaction
        _dirty = True
    logger.warning("trade_db: all trades and events cleared from the dashboard (request %s)",
                   request_id)
    snapshot(force=True)


def _backfill(bot_name):
    """Import this bot's positions from the last BACKFILL_DAYS of MT5 history."""
    magics = {m for m, (name, _) in _bots.items() if name == bot_name}
    date_from = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
    date_to   = datetime.now(timezone.utc) + timedelta(days=1)   # broker time can run ahead of UTC
    deals = mt5.history_deals_get(date_from, date_to)
    if deals is None:
        raise RuntimeError(f"history_deals_get failed: {mt5.last_error()}")

    cutoff = _reset_cutoff()
    tickets = {d.position_id for d in deals
               if d.magic in magics and d.entry == mt5.DEAL_ENTRY_IN and d.time >= cutoff}
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

    # Adopt open bot positions the DB doesn't know about (not ones from before a reset)
    cutoff = _reset_cutoff()
    for pos in mt5.positions_get() or []:
        if pos.magic in _bots and pos.time >= cutoff and not _known(pos.ticket):
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
    offset = str(round(diff / 1800) * 1800)
    with _lock:
        row = _conn.execute("SELECT value FROM meta WHERE key = 'broker_offset_seconds'").fetchone()
    if row is None or row["value"] != offset:
        _write("INSERT OR REPLACE INTO meta (key, value) VALUES ('broker_offset_seconds', ?)",
               (offset,))


def compact_event_counts():
    """Merge daily event counts older than DAILY_COUNT_DAYS into weekly rows (Monday start)."""
    cutoff = int(time.time()) // 86400 * 86400 - DAILY_COUNT_DAYS * 86400
    # 1970-01-01 was a Thursday: +3 days shifts the week boundary to Monday
    week = "(day - ((day / 86400 + 3) % 7) * 86400)"
    with _lock:
        stale = _conn.execute(
            f"SELECT COUNT(*) FROM event_counts WHERE day < ? AND day != {week}", (cutoff,)).fetchone()[0]
        if not stale:
            return
        try:
            _conn.execute(f"""
                CREATE TEMP TABLE merged AS
                SELECT {week} AS day, bot, symbol, timeframe, status, reason, SUM(n) AS n
                FROM event_counts WHERE day < ? GROUP BY 1, 2, 3, 4, 5, 6""", (cutoff,))
            _conn.execute("DELETE FROM event_counts WHERE day < ?", (cutoff,))
            _conn.execute("INSERT INTO event_counts SELECT * FROM merged")
            _conn.execute("DROP TABLE merged")
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
    logger.info("trade_db: merged %d daily event-count rows into weeks", stale)
    _write_many([], events_only=True)


def snapshot(force=False):
    """
    Write the dashboard snapshot — only the tables and columns the page reads —
    to a temp file, then swap it in atomically (readers never see a half-written
    file). Skipped when nothing changed; when only event counts changed, at most
    every EVENT_SNAPSHOT_SECONDS.
    """
    global _dirty, _dirty_events, _last_snapshot
    if _conn is None:
        return
    _update_bot_states()
    events_due = _dirty_events and time.time() - _last_snapshot >= EVENT_SNAPSHOT_SECONDS
    if not (force or _dirty or events_due):
        return

    tmp = SNAPSHOT_PATH + ".tmp"
    with _lock:
        if os.path.exists(tmp):
            os.remove(tmp)
        _conn.execute("ATTACH DATABASE ? AS snap", (tmp,))
        try:
            _conn.executescript(f"""
                CREATE TABLE snap.trades AS SELECT {SNAPSHOT_TRADE_COLUMNS} FROM main.trades;
                CREATE TABLE snap.event_counts AS SELECT * FROM main.event_counts;
                CREATE TABLE snap.bots AS SELECT * FROM main.bots;
                CREATE TABLE snap.meta AS SELECT * FROM main.meta;
                CREATE TABLE snap.recent_events AS
                    SELECT time, bot, symbol, timeframe, status, side, reason, details
                    FROM main.events ORDER BY id DESC LIMIT {RECENT_EVENTS};
            """)
            _conn.execute("INSERT INTO snap.meta (key, value) VALUES ('snapshot_at', ?)",
                          (str(int(time.time())),))
            _conn.commit()
        finally:
            _conn.execute("DETACH DATABASE snap")
        _dirty = _dirty_events = False
        _last_snapshot = time.time()
    os.replace(tmp, SNAPSHOT_PATH)


def _sync_loop():
    global _last_compact
    time.sleep(5)   # let both bots register first
    while True:
        try:
            _check_reset()        # DB only, no MT5 call
        except Exception:
            logger.exception("trade_db: clear-all-data failed")
        try:
            with MT5_LOCK:        # MT5 reads; the DB writes inside take _lock second
                sync()
        except Exception:
            logger.exception("trade_db: sync failed")
        if time.time() - _last_compact >= 3600:
            _last_compact = time.time()
            try:
                compact_event_counts()
            except Exception:
                logger.exception("trade_db: event-count compaction failed")
        try:
            snapshot()
        except Exception:
            logger.exception("trade_db: snapshot failed")
        time.sleep(SYNC_SECONDS)
