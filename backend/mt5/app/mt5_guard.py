"""
One lock for every MetaTrader5 call made by the bots and the journal sync.

The MetaTrader5 package talks to the terminal over IPC and is not reliably
thread-safe: concurrent calls from many bot threads can return None or mix up
replies. Each bot holds MT5_LOCK while it processes one symbol (milliseconds),
so bots still run in their own threads but never call MT5 at the same instant.

Lock order (prevents deadlock): MT5_LOCK first, then trade_db's database lock.
Code holding the database lock must never call MT5 or take MT5_LOCK.
"""
import threading

MT5_LOCK = threading.RLock()
