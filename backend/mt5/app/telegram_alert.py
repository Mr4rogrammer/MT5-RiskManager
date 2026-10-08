"""
Telegram alerts for bot problems: every rejected order, failed close / SL move
(break-even, trailing, partial close) and error a bot records is sent to a Telegram chat, so a broker problem that keeps rejecting trades (Algo
Trading off, market closed, invalid stops …) is noticed without watching the logs.

Messages are sent from a background thread: a bot never waits on Telegram, and
never while it holds MT5_LOCK. The same problem (bot + reason, numbers masked) is
sent at most once every TELEGRAM_REPEAT_MINUTES; the next message for it says how
many more happened in between, so a rejection that keeps repeating still shows up
— as one message per window, not hundreds.

Settings (env vars):
  TELEGRAM_BOT_TOKEN       token from @BotFather (alerts are off without it)
  TELEGRAM_CHAT_ID         chat to send to: send /start to the bot, then read the
                           id from https://api.telegram.org/bot<token>/getUpdates
  TELEGRAM_STATUSES        event statuses that alert (default rejected,failed,error)
  TELEGRAM_REPEAT_MINUTES  minutes before the same problem is sent again (default 30)
  TELEGRAM_STARTUP_MESSAGE true (default) = send "bots started" when the app starts, so a
                           wrong token / chat id shows up at once in flask.log

Check /config/flask.log for "telegram:" lines: whether alerts are on, and Telegram's own
error when a send fails (e.g. "Unauthorized" = bad token, "chat not found" = bad chat id or
/start not sent to the bot).
"""

import json
import logging
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.request

import trade_db

logger = logging.getLogger(__name__)

TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
STATUSES = {s.strip().lower() for s in
            os.environ.get("TELEGRAM_STATUSES", "rejected,failed,error").split(",") if s.strip()}
REPEAT_SECONDS = float(os.environ.get("TELEGRAM_REPEAT_MINUTES", "30")) * 60
STARTUP_MESSAGE = os.environ.get("TELEGRAM_STARTUP_MESSAGE", "true").strip().lower() in ("1", "true", "yes", "on")

_queue   = queue.Queue(maxsize=200)
_lock    = threading.Lock()
_last    = {}       # (bot, normalized reason) -> unix time last sent
_held    = {}       # (bot, normalized reason) -> alerts not sent since then
_started = False


def enabled():
    return bool(TOKEN and CHAT_ID)


def _ssl_context():
    """Windows Python under Wine may have no CA store: use certifi (pip ships a copy)."""
    for module in ("certifi", "pip._vendor.certifi"):
        try:
            return ssl.create_default_context(cafile=__import__(module, fromlist=["where"]).where())
        except Exception:
            continue
    return ssl.create_default_context()


def _sender():
    context = _ssl_context()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    while True:
        text = _queue.get()
        body = json.dumps({"chat_id": CHAT_ID, "text": text,
                           "disable_web_page_preview": True}).encode()
        request = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=15, context=context).read()
        except urllib.error.HTTPError as exc:   # Telegram says why, e.g. "chat not found"
            try:
                why = json.loads(exc.read().decode()).get("description", "")
            except Exception:
                why = ""
            logger.warning("telegram: send failed (HTTP %s %s)", exc.code, why)
        except Exception as exc:          # never log the URL: it holds the token
            logger.warning("telegram: send failed (%s: %s)", type(exc).__name__,
                           str(exc).replace(TOKEN, "<token>")[:200])
        time.sleep(1)                     # Telegram allows ~1 message/second per chat


def _start():
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_sender, daemon=True, name="telegram-alerts").start()
    logger.info("telegram: alerts on for %s, same problem at most every %.0f min",
                sorted(STATUSES), REPEAT_SECONDS / 60)


def startup():
    """Call once at app start: log whether alerts are on and send a test message."""
    if not enabled():
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", TOKEN), ("TELEGRAM_CHAT_ID", CHAT_ID)) if not v]
        logger.info("telegram: alerts off (%s not set)", ", ".join(missing))
        return
    _start()
    if STARTUP_MESSAGE:
        try:
            _queue.put_nowait(f"✅ MT5 bots started — alerts on for {', '.join(sorted(STATUSES))}")
        except queue.Full:
            pass


def alert(bot_title, bot_name, symbol, tf_name, status, details):
    """Queue a message for this event if its status alerts and it isn't a repeat."""
    if status not in STATUSES or not enabled():
        return
    _start()
    reason = details.get("reason") or details.get("comment") or "(no reason)"
    key = (bot_name, trade_db.normalize_reason(reason))
    now = time.time()
    with _lock:
        if now - _last.get(key, 0) < REPEAT_SECONDS:
            _held[key] = _held.get(key, 0) + 1
            return
        _last[key] = now
        more = _held.pop(key, 0)

    where = " ".join(x for x in (symbol, tf_name, details.get("side")) if x)
    lines = [f"⚠️ {bot_title}: {status.upper()}" + (f" · {where}" if where else ""), reason]
    if details.get("request"):
        r = details["request"]
        lines.append(f"price {r.get('price')} · SL {r.get('sl')} · TP {r.get('tp')} · lot {r.get('volume')}")
    if more:
        lines.append(f"+ {more} more like this in the last {REPEAT_SECONDS / 60:.0f} min")
    try:
        _queue.put_nowait("\n".join(lines))
    except queue.Full:
        pass
