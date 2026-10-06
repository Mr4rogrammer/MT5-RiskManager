"""
Ten popular indicator strategies, each running as its own bot on bot_common.Bot,
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
  • TP = RR × the SL distance (1:2 by default). Bollinger targets the middle band.
  • One trade per symbol per bot; no new entries while it is open.
  • Filters from bot_common: min SL distance, commission + spread, broker stops level.

Strategies (key → rule, the timeframe it is usually shown on):
  ema2050   EMA 20 crosses EMA 50                                              H1
  ema921    EMA 9 crosses EMA 21, only in the direction of EMA 200             M30
  golden    SMA 50 crosses SMA 200 (golden / death cross)                      H4
  macd      MACD(12,26,9) crosses its signal below zero (BUY) / above zero
            (SELL), only in the direction of EMA 200                           H1
  rsi       RSI(14) crosses back above 30 (BUY) / below 70 (SELL)              H1
  bbands    Close back inside Bollinger(20, 2) after closing outside; TP =
            middle band                                                        H1
  supertrend Supertrend(10, 3) flips direction                                 H1
  donchian  Close above the previous 20-candle high / below the low            H4
  stoch     Stochastic(14,3,3) %K crosses %D below 20 (BUY) / above 80 (SELL),
            only in the direction of EMA 200                                   H1
  ichimoku  Tenkan crosses Kijun with price above (BUY) / below (SELL) the cloud H4

Settings (env vars, per bot — KEY is the upper-case key, e.g. IND_EMA2050_ENABLED):
  IND_ALL_ENABLED            true → start every indicator bot trading on boot
  IND_<KEY>_ENABLED          true/false (default false, unless IND_ALL_ENABLED)
  IND_<KEY>_SYMBOLS          comma list (default: CRT_SYMBOLS)
  IND_<KEY>_TFS              comma list of H4 / H1 / M30 / M15 (default all four)
  IND_<KEY>_LOT              (default 0.01; only when BOTS_RISK_PCT=0)
  IND_<KEY>_SL_ATR           SL distance in ATR(14) (default 1.5)
  IND_<KEY>_RR               TP = RR × SL distance (default 2.0)
  IND_<KEY>_MAGIC_BASE       (default 800000 + 1000 × bot number)
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


def sma(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.r_[0.0, np.cumsum(x)]
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def wilder(x, n):
    """Wilder's smoothing (RSI / ATR)."""
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


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0])
    up, dn = wilder(np.clip(d, 0, None), n), wilder(np.clip(-d, 0, None), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = up / dn
    return np.where(dn == 0, 100.0, 100 - 100 / (1 + rs))


def rolling_max(x, n):
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        out[i] = x[i - n + 1:i + 1].max()
    return out


def rolling_min(x, n):
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        out[i] = x[i - n + 1:i + 1].min()
    return out


def supertrend(h, l, c, n=10, mult=3.0):
    """Returns direction per candle: +1 up, -1 down."""
    a = atr(h, l, c, n)
    hl2 = (h + l) / 2
    upper, lower = hl2 + mult * a, hl2 - mult * a
    fu, fl = upper.copy(), lower.copy()
    d = np.ones(len(c))
    for i in range(1, len(c)):
        if np.isnan(a[i - 1]):
            continue
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if d[i - 1] == 1:
            d[i] = -1 if c[i] < fl[i] else 1
        else:
            d[i] = 1 if c[i] > fu[i] else -1
    d[np.isnan(a)] = np.nan
    return d


def crossed_up(a, b):
    """True if series a crossed above b on the last candle."""
    return a[-2] <= b[-2] and a[-1] > b[-1]


def crossed_down(a, b):
    return a[-2] >= b[-2] and a[-1] < b[-1]


# ---------------------------------------------------------------------------
# Signals — each takes candle arrays o, h, l, c (closed candles) and returns
# (side, tp_override) or None. Only the last two closed candles decide.
# ---------------------------------------------------------------------------

def sig_ema2050(o, h, l, c):
    f, s = ema(c, 20), ema(c, 50)
    if crossed_up(f, s):
        return "BUY", None
    if crossed_down(f, s):
        return "SELL", None


def sig_ema921(o, h, l, c):
    f, s, t = ema(c, 9), ema(c, 21), ema(c, 200)
    if crossed_up(f, s) and c[-1] > t[-1]:
        return "BUY", None
    if crossed_down(f, s) and c[-1] < t[-1]:
        return "SELL", None


def sig_golden(o, h, l, c):
    f, s = sma(c, 50), sma(c, 200)
    if crossed_up(f, s):
        return "BUY", None
    if crossed_down(f, s):
        return "SELL", None


def sig_macd(o, h, l, c):
    line = ema(c, 12) - ema(c, 26)
    signal = ema(np.nan_to_num(line, nan=0.0), 9)
    signal[:34] = np.nan                                 # 26 + 9 − 1 warm-up
    t = ema(c, 200)
    if crossed_up(line, signal) and line[-1] < 0 and c[-1] > t[-1]:
        return "BUY", None
    if crossed_down(line, signal) and line[-1] > 0 and c[-1] < t[-1]:
        return "SELL", None


def sig_rsi(o, h, l, c):
    r = rsi(c, 14)
    if r[-2] <= 30 < r[-1]:
        return "BUY", None
    if r[-2] >= 70 > r[-1]:
        return "SELL", None


def sig_bbands(o, h, l, c):
    mid = sma(c, 20)
    sd = np.full(len(c), np.nan)
    for i in range(19, len(c)):
        sd[i] = c[i - 19:i + 1].std()
    up, dn = mid + 2 * sd, mid - 2 * sd
    if c[-2] < dn[-2] and c[-1] > dn[-1]:
        return "BUY", mid[-1]
    if c[-2] > up[-2] and c[-1] < up[-1]:
        return "SELL", mid[-1]


def sig_supertrend(o, h, l, c):
    d = supertrend(h, l, c, 10, 3.0)
    if d[-2] == -1 and d[-1] == 1:
        return "BUY", None
    if d[-2] == 1 and d[-1] == -1:
        return "SELL", None


def sig_donchian(o, h, l, c):
    hh, ll = h[-21:-1].max(), l[-21:-1].min()           # previous 20 candles
    if c[-1] > hh and c[-2] <= h[-22:-2].max():
        return "BUY", None
    if c[-1] < ll and c[-2] >= l[-22:-2].min():
        return "SELL", None


def sig_stoch(o, h, l, c):
    hh, ll = rolling_max(h, 14), rolling_min(l, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        fast = np.where(hh > ll, 100 * (c - ll) / (hh - ll), 50.0)
    k = sma(np.nan_to_num(fast, nan=50.0), 3)
    d = sma(np.nan_to_num(k, nan=50.0), 3)
    t = ema(c, 200)
    if crossed_up(k, d) and k[-2] < 20 and c[-1] > t[-1]:
        return "BUY", None
    if crossed_down(k, d) and k[-2] > 80 and c[-1] < t[-1]:
        return "SELL", None


def sig_ichimoku(o, h, l, c):
    tenkan = (rolling_max(h, 9) + rolling_min(l, 9)) / 2
    kijun = (rolling_max(h, 26) + rolling_min(l, 26)) / 2
    span_a = (tenkan + kijun) / 2
    span_b = (rolling_max(h, 52) + rolling_min(l, 52)) / 2
    # The cloud under the current candle was plotted 26 candles ago
    top = max(span_a[-27], span_b[-27])
    bottom = min(span_a[-27], span_b[-27])
    if crossed_up(tenkan, kijun) and c[-1] > top:
        return "BUY", None
    if crossed_down(tenkan, kijun) and c[-1] < bottom:
        return "SELL", None


# key, title, comment prefix, default TF, signal, description
SPECS = [
    ("ema2050",    "EMA 20/50 cross",       "E2050 ", "H1",  sig_ema2050,
     "EMA 20 crosses EMA 50."),
    ("ema921",     "EMA 9/21 + 200 trend",  "E921 ",  "M30", sig_ema921,
     "EMA 9 crosses EMA 21, only in the direction of EMA 200."),
    ("golden",     "Golden / death cross",  "GX ",    "H4",  sig_golden,
     "SMA 50 crosses SMA 200."),
    ("macd",       "MACD + 200 EMA",        "MACD ",  "H1",  sig_macd,
     "MACD crosses its signal below zero (buy) / above zero (sell), with the EMA 200 trend."),
    ("rsi",        "RSI 30/70 reversal",    "RSI ",   "H1",  sig_rsi,
     "RSI(14) crosses back above 30 (buy) / below 70 (sell)."),
    ("bbands",     "Bollinger reversion",   "BB ",    "H1",  sig_bbands,
     "Close back inside Bollinger(20, 2) after closing outside; target the middle band."),
    ("supertrend", "Supertrend flip",       "STR ",   "H1",  sig_supertrend,
     "Supertrend(10, 3) changes direction."),
    ("donchian",   "Donchian breakout",     "DC ",    "H4",  sig_donchian,
     "Close above the previous 20-candle high / below the low."),
    ("stoch",      "Stochastic + 200 EMA",  "STO ",   "H1",  sig_stoch,
     "%K crosses %D below 20 (buy) / above 80 (sell), with the EMA 200 trend."),
    ("ichimoku",   "Ichimoku TK cross",     "ICH ",   "H4",  sig_ichimoku,
     "Tenkan crosses Kijun with price above (buy) / below (sell) the cloud."),
]


# ---------------------------------------------------------------------------
# Bot wrapper
# ---------------------------------------------------------------------------

class IndicatorBot:
    def __init__(self, number, key, title, prefix, usual_tf, signal, description):
        self.key, self.signal = key, signal
        self.default_magic = 800000 + 1000 * number
        self.bot = Bot(name=key, label=key.upper(), prefix=prefix, title=title,
                       description=description + " H4–M15, SL 1.5 × ATR, TP 1:2 unless changed.")
        self._last_bar = {}          # (symbol, tf) -> open time of the last candle evaluated

    def env(self, name, default):
        return os.environ.get(f"IND_{self.key.upper()}_{name}", default)

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
        })
        enabled = self.env("ENABLED", "true" if all_on else "false").lower() == "true"
        self.bot.start(self.check, lambda pos: None, enabled_by_default=enabled)


BOTS = [IndicatorBot(i + 1, *spec) for i, spec in enumerate(SPECS)]


def start_indicator_bots():
    for b in BOTS:
        b.start()
