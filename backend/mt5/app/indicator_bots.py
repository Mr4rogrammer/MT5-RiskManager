"""
Indicator strategies, each running as its own bot on bot_common.Bot,
for side-by-side testing on a demo account. Every bot is journaled and shows up on
the dashboard and under /bots automatically.

Shared rules (the same for every bot so they compare fairly):
  • Each bot trades H4, H1, M30 and M15 (IND_<KEY>_TFS) with the same rules as the
    CRT bots: H4 is checked first; one trade per symbol; an opposite signal on a
    higher timeframe closes the lower-timeframe trade and enters, a same-direction
    one is skipped. The dashboard's bot × timeframe table shows which TF works.
  • A signal is read on the last CLOSED candle of each timeframe, once per candle,
    and entered at market within IND_<KEY>_MAX_SIGNAL_AGE seconds of the close.
  • SL = SL_ATR × ATR(14) from entry (+ spread for SELL, whose SL triggers on the ask).
  • TP = RR × the SL distance (1:2 by default).
  • One trade per symbol per bot; no new entries while it is open.
  • Filters from bot_common: min SL distance, commission + spread, broker stops level.

Strategies (key → rule, the timeframe it is usually shown on):
  macd      MACD(12,26,9) crosses its signal below zero (BUY) / above zero
            (SELL), only in the direction of EMA 200                           H1
  donchian  Close above the previous 20-candle high / below the low            H4

Exit variant — same signal as the original, different trade management, so the
dashboard shows which exit makes more:
  donchian_pt    Donchian breakout: at 1R close 50 % and move SL to break-even, then
                 trail the rest 2R behind price; TP at 10R (effectively none)

Settings (env vars, per bot — KEY is the upper-case key, e.g. IND_MACD_ENABLED):
  IND_ALL_ENABLED            true → start every indicator bot trading on boot
  IND_<KEY>_ENABLED          true/false (default false, unless IND_ALL_ENABLED)
  IND_<KEY>_SYMBOLS          comma list (default: CRT_SYMBOLS)
  IND_<KEY>_TFS              comma list of H4 / H1 / M30 / M15 (default all four)
  IND_<KEY>_LOT              (default 0.01; only when BOTS_RISK_PCT=0)
  IND_<KEY>_SL_ATR           SL distance in ATR(14) (default 1.5)
  IND_<KEY>_RR               TP = RR × SL distance (default 2.0; variants 10)
  IND_<KEY>_BE_R             move SL to break-even once price is this many R in profit
                             (default 0 = off; variants 1.0)
  IND_<KEY>_PARTIAL_PCT      % closed at that moment (default 0; variants 50)
  IND_<KEY>_TRAIL_R          after break-even, trail the SL this × the initial risk
                             behind price (default 0 = off; variants 2.0)
  IND_<KEY>_MAGIC_BASE       (default macd 804000, donchian 808000, donchian_pt 811000 —
                             kept from when there were 12 bots, so open trades stay matched)
  IND_<KEY>_MAX_SIGNAL_AGE   seconds after the candle close (default 300)
  IND_<KEY>_MIN_SL_SPREADS   (default 3)   IND_<KEY>_POLL_INTERVAL (default 5)
"""

import os

import numpy as np
import MetaTrader5 as mt5

from bot_common import Bot, TIMEFRAMES, DEFAULT_SYMBOLS

HISTORY = 320          # closed candles fetched per check — enough to warm up EMA 200


# ---------------------------------------------------------------------------
# Indicators (numpy, oldest → newest)
# ---------------------------------------------------------------------------

def ema(x, n):
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()                       # seeded with the SMA
    for i in range(n, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def wilder(x, n):
    """Wilder's smoothing (ATR)."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = out[i - 1] + (x[i] - out[i - 1]) / n
    return out


def atr(h, l, c, n=14):
    prev = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    return wilder(tr, n)


def crossed_up(a, b):
    """True if series a crossed above b on the last candle."""
    return a[-2] <= b[-2] and a[-1] > b[-1]


def crossed_down(a, b):
    return a[-2] >= b[-2] and a[-1] < b[-1]


# ---------------------------------------------------------------------------
# Signals — each takes candle arrays o, h, l, c (closed candles) and returns
# (side, tp_override) or None. Only the last two closed candles decide.
# ---------------------------------------------------------------------------

def sig_macd(o, h, l, c):
    line = ema(c, 12) - ema(c, 26)
    signal = ema(np.nan_to_num(line, nan=0.0), 9)
    signal[:34] = np.nan                                 # 26 + 9 − 1 warm-up
    t = ema(c, 200)
    if crossed_up(line, signal) and line[-1] < 0 and c[-1] > t[-1]:
        return "BUY", None
    if crossed_down(line, signal) and line[-1] > 0 and c[-1] < t[-1]:
        return "SELL", None


def sig_donchian(o, h, l, c):
    hh, ll = h[-21:-1].max(), l[-21:-1].min()           # previous 20 candles
    if c[-1] > hh and c[-2] <= h[-22:-2].max():
        return "BUY", None
    if c[-1] < ll and c[-2] >= l[-22:-2].min():
        return "SELL", None


# number, key, title, comment prefix, default TF, signal, description
# The number sets the default magic base (800000 + 1000 × number). These are the numbers
# from when there were 12 indicator bots — don't renumber, or open trades lose their bot.
# Defaults of the exit variant (each still overridable with IND_<KEY>_*)
TRAIL_DEFAULTS = {"RR": "10", "BE_R": "1.0", "PARTIAL_PCT": "50", "TRAIL_R": "2.0"}

SPECS = [
    (4,  "macd",        "MACD + 200 EMA",    "MACD ", "H1", sig_macd,
     "MACD crosses its signal below zero (buy) / above zero (sell), with the EMA 200 trend."),
    (8,  "donchian",    "Donchian breakout", "DC ",   "H4", sig_donchian,
     "Close above the previous 20-candle high / below the low."),
    # Exit variant: the same signal, managed to let winners run
    (11, "donchian_pt", "Donchian + trail",  "DCT ",  "H4", sig_donchian,
     "Donchian breakout; at 1R close half and move SL to break-even, then trail 2R behind.",
     TRAIL_DEFAULTS),
]


# ---------------------------------------------------------------------------
# Bot wrapper
# ---------------------------------------------------------------------------

class IndicatorBot:
    def __init__(self, number, key, title, prefix, usual_tf, signal, description, defaults=None):
        self.key, self.signal = key, signal
        self.defaults = defaults or {}
        self.default_magic = 800000 + 1000 * number
        tail = (" H4–M15, SL 1.5 × ATR." if defaults
                else " H4–M15, SL 1.5 × ATR, TP 1:2 unless changed.")
        self.bot = Bot(name=key, label=key.upper(), prefix=prefix, title=title,
                       description=description + tail)
        self._last_bar = {}          # (symbol, tf) -> open time of the last candle evaluated

    def env(self, name, default):
        return os.environ.get(f"IND_{self.key.upper()}_{name}", self.defaults.get(name, default))

    def be_trigger(self, pos):
        """Break-even once price is BE_R × the initial risk in profit (None = off)."""
        be_r = self.bot.settings["be_r"]
        if be_r <= 0:
            return None
        row = self.bot._journal(pos.ticket)
        if not row or not row["sl_initial"]:
            return None
        risk = abs(pos.price_open - row["sl_initial"])
        return pos.price_open + be_r * risk if pos.type == mt5.POSITION_TYPE_BUY \
            else pos.price_open - be_r * risk

    def check(self, symbol, tf_name, running):
        s = self.bot.settings
        if tf_name not in s["tfs"]:
            return False
        tf, tf_seconds = TIMEFRAMES[tf_name]
        key = (symbol, tf_name)

        last = mt5.copy_rates_from_pos(symbol, tf, 1, 1)
        if last is None or len(last) == 0:
            return False
        bar_time = int(last[0]["time"])
        if self._last_bar.get(key) == bar_time:
            return False
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return False
        self._last_bar[key] = bar_time
        if tick.time - (bar_time + tf_seconds) > s["max_signal_age"]:
            return False                # candle closed too long ago (e.g. after a restart)

        rates = mt5.copy_rates_from_pos(symbol, tf, 1, HISTORY)
        if rates is None or len(rates) < 60:
            return False
        o, h, l, c = (np.asarray(rates[k], dtype=float) for k in ("open", "high", "low", "close"))
        out = self.signal(o, h, l, c)
        if not out:
            return False
        side, tp_override = out

        a = atr(h, l, c, 14)[-1]
        if not np.isfinite(a) or a <= 0:
            return False
        spread = tick.ask - tick.bid
        entry = tick.ask if side == "BUY" else tick.bid
        dist = s["sl_atr"] * a
        sl = entry - dist if side == "BUY" else entry + dist + spread
        if tp_override is not None:
            tp = float(tp_override)
        else:
            risk = abs(entry - sl)
            tp = entry + s["rr"] * risk if side == "BUY" else entry - s["rr"] * risk

        self.bot.record(symbol, tf_name, "signal", side=side, close=float(c[-1]),
                        atr=round(float(a), 6))
        return self.bot.place(symbol, tf_name, side, sl, tp, tick,
                              comment=f"{self.bot.prefix}{tf_name} {bar_time}",
                              to_close=running, signal_time=bar_time)

    def start(self):
        all_on = os.environ.get("IND_ALL_ENABLED", "false").lower() == "true"
        default_symbols = os.environ.get("CRT_SYMBOLS", DEFAULT_SYMBOLS)
        tfs = [x.strip().upper() for x in self.env("TFS", ",".join(TIMEFRAMES)).split(",")]
        tfs = [tf for tf in TIMEFRAMES if tf in tfs] or list(TIMEFRAMES)   # keep H4 → M15 order
        self.bot.settings.update({
            "tfs":            tfs,
            "symbols":        [x.strip() for x in self.env("SYMBOLS", default_symbols).split(",") if x.strip()],
            "lot":            float(self.env("LOT", "0.01")),
            "deviation":      int(self.env("DEVIATION", "20")),
            "magic_base":     int(self.env("MAGIC_BASE", str(self.default_magic))),
            "poll_interval":  float(self.env("POLL_INTERVAL", "5")),
            "max_signal_age": int(self.env("MAX_SIGNAL_AGE", "300")),
            "sl_atr":         float(self.env("SL_ATR", "1.5")),
            "rr":             float(self.env("RR", "2.0")),
            "min_rr":         0.0,
            "min_sl_spreads": float(self.env("MIN_SL_SPREADS", "3")),
            "be_r":           float(self.env("BE_R", "0")),
            "partial_pct":    float(self.env("PARTIAL_PCT", "0")),
            "trail_r":        float(self.env("TRAIL_R", "0")),
        })
        enabled = self.env("ENABLED", "true" if all_on else "false").lower() == "true"
        self.bot.start(self.check, self.be_trigger, enabled_by_default=enabled)


BOTS = [IndicatorBot(*spec) for spec in SPECS]


def start_indicator_bots():
    for b in BOTS:
        b.start()
