#!/usr/bin/env python3
"""
Autonomous Trading Bot — JP v7.1
========================================
Exact parity with TradingView Pine Scripts JP v7 (SOL + ETH).
Fetches OHLCV directly from Hyperliquid, aggregates 1m → TF per coin,
computes range bars, detects signals (trend + explosive),
applies HA 1H filter (SOL), sizes via Labouchere INVERSÉ,
manages DCA and daily stop, places orders on Hyperliquid perpetuals.

Coins     : SOL (29M), ETH (30M)
Capital   : 500 USDC each
Mode      : DRY_RUN=true by default (logs orders without placing them)
"""

import os
import sys
import time
import math
import json
import logging
import requests
import numpy as np
from datetime import datetime, timezone, timedelta

# ── Add script directory to sys.path so labouch_manager is importable ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labouch_manager import LabouchManager
import trade_journal

# ==============================================================
#  CONFIGURATION
# ==============================================================
COINS       = ["SOL", "ETH"]
COIN_TF     = {"SOL": 29, "ETH": 30}   # timeframe per coin (minutes)
BARS_NEEDED = 160          # bars to fetch per coin (covers RSI-100 + warmup)

CAPITAL = {"SOL": 500.0, "ETH": 500.0}

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
MAINNET     = os.environ.get("MAINNET", "true").lower() == "true"
DRY_RUN     = os.environ.get("DRY_RUN",  "true").lower() == "true"

HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# ── Indicator parameters (COMMON — identical SOL & ETH) ──
ATR_LEN     = 10
ADX_LEN     = 14
RSI_LEN     = 100
BB_LEN      = 20
BB_MULT     = 2.0
VOL_SMA_LEN = 20
VP_K        = 0.75    # VWAP band offset (×ATR)
VOL_MULT    = 1.4     # volume threshold vs SMA
VWAP_BARS   = 24      # rolling window for VWAP/VAH/VAL
ATR_SMA_LEN = 20      # SMA of ATR for volHigh detection

TP_MULT     = 5.0     # risk:reward multiplier (all regimes)
TP_SLIPPAGE = 0.001   # 0.10% simulated TP slippage

# DCA parameters
DCA_DIST        = 0.02   # 2% adverse move to trigger DCA
MAX_DCA         = 2
DCA_TP_RR_TREND = 4.0    # TP RR after DCA when entry was trending
DCA_TP_RR_OTHER = 3.0    # TP RR after DCA otherwise

# Daily stop
DAILY_STOP_PCT = 0.15    # 15% daily drawdown → pause

MAX_SIGNAL_AGE_BARS = 1  # Ne pas entrer si signal > 1 barre après clôture

# inRng ADX threshold (common, used for volume-filtered range detection)
IN_RNG_ADX_THRESH = 20

# ── Per-coin parameters (SOL vs ETH differ here) ──
COIN_PARAMS = {
    "SOL": {
        # HMA periods (JP_v7_SOL_45M.pine)
        "hma_fast":          20,
        "hma_slow":          50,
        # ADX thresholds
        "adx_trend":         20,    # isTrending = adx > 20
        "adx_range":         15,    # isRanging: adx < 15 + BB
        "strong_thresh":     25,    # strongBull/Bear: adx > 25
        "di_ratio":          1.5,   # DI ratio for strong condition
        # Heikin Ashi 1H filter
        "use_ha_filter":     True,
        # Labouchere leverage multiplier
        "leverage_lab_mult": 2.0,
    },
    "ETH": {
        # HMA periods (JP_v7_ETH_30M.pine)
        "hma_fast":          25,
        "hma_slow":          40,
        # ADX thresholds
        "adx_trend":         25,    # isTrending = adx > 25
        "adx_range":         20,    # isRanging: adx < 20 + BB
        "strong_thresh":     30,    # strongBull/Bear: adx > 30
        "di_ratio":          1.5,
        # Heikin Ashi filter OFF for ETH
        "use_ha_filter":     False,
        # Labouchere leverage multiplier
        "leverage_lab_mult": 3.0,
    },
}

# Fallback coin precision if SDK not available
COIN_PRECISION_DEFAULT = {"SOL": 3, "ETH": 4, "BTC": 5}

# ==============================================================
#  LOGGING
# ==============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("autonomous_bot")

# ==============================================================
#  STATE
# ==============================================================
labouch = LabouchManager()

# Track virtual positions (always used for DRY_RUN; also as cache for live)
_positions: dict = {}   # coin → {side, entry, qty, sl, tp, regime, ts, capital, journal_id}

# Per-coin extended state (range bars, DCA, daily stop)
_coin_state: dict = {}

# ── Persistent state ──
PERSIST_DIR = os.environ.get("PERSIST_DIR", "/data")
POSITIONS_FILE: str = None  # resolved at startup in run()

def _resolve_persist_dir() -> str:
    for d in [PERSIST_DIR, "/tmp"]:
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".wtest")
            open(t, "w").write("ok"); os.remove(t)
            log.info(f"[persist] dir={d}")
            return d
        except Exception:
            continue
    return "/tmp"

def _save_positions():
    if not POSITIONS_FILE: return
    try:
        with open(POSITIONS_FILE, "w") as f: json.dump(_positions, f, indent=2, default=str)
        log.info(f"[persist] saved {len(_positions)} position(s)")
    except Exception as e:
        log.warning(f"[persist] save failed: {e}")

def _load_positions():
    global _positions
    if not POSITIONS_FILE or not os.path.exists(POSITIONS_FILE): return
    try:
        saved = json.load(open(POSITIONS_FILE))
        if saved:
            _positions.update(saved)
            for c, p in saved.items():
                log.info(f"[persist] ♻️  RESTORED {c} {p.get('side','?').upper()} @ {p.get('entry','?')}")
    except Exception as e:
        log.warning(f"[persist] load failed: {e}")


def _get_coin_state(coin: str) -> dict:
    """Return (and lazily init) per-coin mutable state dict."""
    if coin not in _coin_state:
        _coin_state[coin] = {
            # DCA tracking
            "dca_count":          0,
            "last_dca_price":     None,
            "avg_price":          None,
            "sl_entry":           None,      # original SL at entry
            "entry_is_trending":  False,     # was entry in TREND regime?
            "dca_qty":            None,      # qty per unit (for DCA sizing)
            # Daily stop
            "daily_start_equity": None,
            "daily_date":         None,
            "daily_stop":         False,
            "equity":             CAPITAL.get(coin, 500.0),
        }
    return _coin_state[coin]


# ==============================================================
#  HYPERLIQUID CLIENT
# ==============================================================
_account  = None
_exchange = None
_info     = None


def _init_hl():
    global _account, _exchange, _info
    if not PRIVATE_KEY:
        log.warning("⚠️  No PRIVATE_KEY — full simulation mode (no exchange calls)")
        return
    try:
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        _account = Account.from_key(PRIVATE_KEY)
        base_url = constants.MAINNET_API_URL if MAINNET else constants.TESTNET_API_URL
        _info    = Info(base_url, skip_ws=True)
        if not DRY_RUN:
            _exchange = Exchange(_account, base_url)
        log.info(
            f"✅ HL ready | wallet={_account.address} | "
            f"{'MAINNET' if MAINNET else 'TESTNET'} | "
            f"{'DRY_RUN' if DRY_RUN else '🔴 LIVE'}"
        )
    except Exception as e:
        log.error(f"HL init failed: {e}")


def _hl_post(payload: dict):
    """Raw POST to Hyperliquid Info API."""
    resp = requests.post(HL_INFO_URL, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ==============================================================
#  DATA FETCHING
# ==============================================================
def fetch_candles_1m(coin: str, n_1m_bars: int) -> list:
    """
    Fetch n_1m_bars 1m candles from Hyperliquid REST API.
    Returns list of dicts: {t, T, o, h, l, c, v}
    """
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - (n_1m_bars + 60) * 60 * 1000  # buffer

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin":      coin,
            "interval":  "1m",
            "startTime": start_ms,
            "endTime":   now_ms,
        }
    }
    raw = _hl_post(payload)
    candles = []
    for c in raw:
        candles.append({
            "t": int(c["t"]),
            "T": int(c["T"]),
            "o": float(c["o"]),
            "h": float(c["h"]),
            "l": float(c["l"]),
            "c": float(c["c"]),
            "v": float(c["v"]),
        })
    candles.sort(key=lambda x: x["t"])
    log.debug(f"[{coin}] Fetched {len(candles)} 1m candles")
    return candles


def fetch_ha_1h_candles(coin: str, n_bars: int = 10) -> tuple:
    """
    Fetch 1H candles from Hyperliquid and compute Heikin Ashi.
    Used for SOL HA filter (useHAFilter=TRUE).
    Returns (ha_bull_1h, ha_bear_1h) booleans based on last COMPLETED 1H bar.
    """
    try:
        now_ms   = int(time.time() * 1000)
        start_ms = now_ms - (n_bars + 5) * 3600 * 1000
        payload  = {
            "type": "candleSnapshot",
            "req": {
                "coin":      coin,
                "interval":  "1h",
                "startTime": start_ms,
                "endTime":   now_ms,
            }
        }
        raw = _hl_post(payload)
        candles = sorted([{
            "o": float(c["o"]),
            "h": float(c["h"]),
            "l": float(c["l"]),
            "c": float(c["c"]),
            "T": int(c["T"]),
        } for c in raw], key=lambda x: x["T"])

        # Drop current still-forming bar
        now_ms2 = int(time.time() * 1000)
        if candles and (now_ms2 - candles[-1]["T"]) < 90_000:
            candles = candles[:-1]

        if len(candles) < 2:
            log.warning(f"[{coin}] Not enough 1H candles for HA filter ({len(candles)})")
            return None, None

        # Compute Heikin Ashi
        # haClose[i] = (open + high + low + close) / 4
        # haOpen[i]  = (haOpen[i-1] + haClose[i-1]) / 2  (first: open[0])
        ha_close = [(c_["o"] + c_["h"] + c_["l"] + c_["c"]) / 4.0 for c_ in candles]
        ha_open  = [candles[0]["o"]]
        for i in range(1, len(candles)):
            ha_open.append((ha_open[-1] + ha_close[i - 1]) / 2.0)

        ha_bull_1h = ha_close[-1] > ha_open[-1]
        ha_bear_1h = ha_close[-1] < ha_open[-1]

        log.debug(
            f"[{coin}] HA 1H: haClose={ha_close[-1]:.4f} haOpen={ha_open[-1]:.4f} "
            f"bull={ha_bull_1h} bear={ha_bear_1h}"
        )
        return ha_bull_1h, ha_bear_1h

    except Exception as e:
        log.error(f"[{coin}] fetch_ha_1h_candles error: {e}")
        return None, None


def aggregate_to_tf(candles_1m: list, tf_minutes: int) -> list:
    """
    Aggregate 1m candles → tf_minutes bars aligned to midnight UTC.
    Bar k covers minutes [k×tf, (k+1)×tf) since 00:00 UTC.
    Drops current (incomplete) bar.
    """
    if not candles_1m:
        return []

    def bar_key(t_ms):
        dt         = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
        min_of_day = dt.hour * 60 + dt.minute
        return dt.strftime("%Y-%m-%d"), min_of_day // tf_minutes

    groups: dict = {}
    for c in candles_1m:
        k = bar_key(c["t"])
        groups.setdefault(k, []).append(c)

    bars = []
    for k in sorted(groups.keys()):
        grp = sorted(groups[k], key=lambda x: x["t"])
        bar = {
            "t":  grp[0]["t"],
            "T":  grp[-1]["T"],
            "o":  grp[0]["o"],
            "h":  max(c["h"] for c in grp),
            "l":  min(c["l"] for c in grp),
            "c":  grp[-1]["c"],
            "v":  sum(c["v"] for c in grp),
            "_n": len(grp),
        }
        bars.append(bar)

    # Drop last bar if it's the currently-forming bar
    if bars:
        now_ms = int(time.time() * 1000)
        if (now_ms - bars[-1]["T"]) < 90_000:
            bars.pop()
            log.debug(f"Dropped last (still-forming) {tf_minutes}m bar")

    log.debug(f"Aggregated {len(candles_1m)} 1m → {len(bars)} {tf_minutes}m bars")
    return bars


# ==============================================================
#  INDICATOR CALCULATIONS
# ==============================================================
def _wma(arr: np.ndarray, period: int) -> np.ndarray:
    """Weighted Moving Average (weights = 1, 2, …, period)."""
    n       = len(arr)
    result  = np.full(n, np.nan)
    weights = np.arange(1, period + 1, dtype=float)
    w_sum   = weights.sum()
    for i in range(period - 1, n):
        window = arr[i - period + 1: i + 1]
        if not np.any(np.isnan(window)):
            result[i] = np.dot(weights, window) / w_sum
    return result


def calc_hma(arr: np.ndarray, period: int) -> np.ndarray:
    """Hull Moving Average: WMA(2×WMA(n/2) − WMA(n), √n)"""
    half    = max(1, period // 2)
    sq_root = max(1, int(math.sqrt(period)))
    wma_h   = _wma(arr, half)
    wma_f   = _wma(arr, period)
    diff    = 2.0 * wma_h - wma_f
    return _wma(diff, sq_root)


def calc_atr(high: np.ndarray, low: np.ndarray,
             close: np.ndarray, period: int) -> np.ndarray:
    """ATR with Wilder smoothing — matches Pine Script ta.atr()."""
    n  = len(high)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i]  - close[i - 1]),
        )
    atr = np.full(n, np.nan)
    if period < n:
        atr[period] = float(np.nanmean(tr[1: period + 1]))
        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def calc_adx(high: np.ndarray, low: np.ndarray,
             close: np.ndarray, period: int = 14):
    """
    DMI / ADX with Wilder smoothing.
    Returns (di_plus, di_minus, adx) — all np.ndarray.
    """
    n        = len(high)
    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    tr       = np.zeros(n)

    for i in range(1, n):
        up         = high[i]    - high[i - 1]
        down       = low[i - 1] - low[i]
        plus_dm[i]  = up   if up   > down and up   > 0 else 0.0
        minus_dm[i] = down if down > up   and down > 0 else 0.0
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i]  - close[i - 1]),
        )

    def wilder(arr, p):
        res = np.full(n, np.nan)
        if p >= n:
            return res
        res[p] = float(np.sum(arr[1: p + 1]))
        for i in range(p + 1, n):
            res[i] = res[i - 1] - res[i - 1] / p + arr[i]
        return res

    s_tr  = wilder(tr,       period)
    s_pdm = wilder(plus_dm,  period)
    s_mdm = wilder(minus_dm, period)

    di_plus  = np.where(s_tr > 0, 100.0 * s_pdm / s_tr, np.nan)
    di_minus = np.where(s_tr > 0, 100.0 * s_mdm / s_tr, np.nan)

    dx_num = np.abs(di_plus - di_minus)
    dx_den = di_plus + di_minus
    dx     = np.where(dx_den > 0, 100.0 * dx_num / dx_den, np.nan)

    adx   = np.full(n, np.nan)
    start = 2 * period
    if start < n:
        chunk = dx[period: start + 1]
        valid = chunk[~np.isnan(chunk)]
        if len(valid) >= period:
            adx[start] = float(np.mean(valid[-period:]))
            for i in range(start + 1, n):
                if not np.isnan(adx[i - 1]) and not np.isnan(dx[i]):
                    adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return di_plus, di_minus, adx


def calc_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI with Wilder smoothing — matches Pine Script ta.rsi()."""
    n      = len(close)
    rsi    = np.full(n, np.nan)
    delta  = np.diff(close)
    gains  = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)

    if n <= period:
        return rsi

    avg_g = float(np.mean(gains[:period]))
    avg_l = float(np.mean(losses[:period]))

    for i in range(period, n - 1):
        if i > period:
            avg_g = (avg_g * (period - 1) + gains[i])  / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs        = avg_g / avg_l if avg_l > 0 else np.inf
        rsi[i + 1] = 100.0 - 100.0 / (1.0 + rs)

    return rsi


def calc_sma(arr: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average."""
    n      = len(arr)
    result = np.full(n, np.nan)
    for i in range(period - 1, n):
        window = arr[i - period + 1: i + 1]
        if not np.any(np.isnan(window)):
            result[i] = float(np.mean(window))
    return result


def calc_bollinger_bands(close: np.ndarray, period: int,
                         mult: float) -> tuple:
    """
    Bollinger Bands — matches Pine Script ta.bb().
    Returns (upper, lower, mid) as np.ndarray.
    Uses population std (ddof=0) as Pine Script does.
    """
    n     = len(close)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    mid   = np.full(n, np.nan)
    for i in range(period - 1, n):
        w = close[i - period + 1: i + 1]
        if not np.any(np.isnan(w)):
            m         = float(np.mean(w))
            s         = float(np.std(w, ddof=0))
            mid[i]   = m
            upper[i] = m + mult * s
            lower[i] = m - mult * s
    return upper, lower, mid


def calc_rolling_vwap(typical: np.ndarray, volume: np.ndarray,
                      window: int) -> np.ndarray:
    """Rolling VWAP over `window` bars."""
    n      = len(typical)
    result = np.full(n, np.nan)
    for i in range(window - 1, n):
        tp_w    = typical[i - window + 1: i + 1]
        v_w     = volume[i  - window + 1: i + 1]
        vol_sum = float(np.sum(v_w))
        if vol_sum > 0:
            result[i] = float(np.sum(tp_w * v_w)) / vol_sum
    return result


# ==============================================================
#  RANGE BARS (Pine Script exact replica)
# ==============================================================
def compute_range_bars(closes: np.ndarray, atr_arr: np.ndarray) -> tuple:
    """
    Compute Pine Script-style range bars over entire closes history.

    Logic (per bar i):
        rSize  = atr[i]
        newBar = r_open is None OR abs(close - r_open) >= rSize
        if newBar:
            r_open = prev_close
            r_high = max(prev_close, close)   ← prev_close is bar seed
            r_low  = min(prev_close, close)
        else:
            r_high = max(r_high, close)
            r_low  = min(r_low,  close)
        r_close = close  (always = current close)

    Returns:
        r_high_hist, r_low_hist  — np.ndarray same length as closes
    """
    n           = len(closes)
    r_high_hist = np.full(n, np.nan)
    r_low_hist  = np.full(n, np.nan)

    r_open: float | None = None
    r_high: float        = 0.0
    r_low:  float        = 0.0

    for i in range(n):
        c      = float(closes[i])
        prev_c = float(closes[i - 1]) if i > 0 else c
        atr_v  = float(atr_arr[i])

        if r_open is None or math.isnan(atr_v):
            # Initialize new range bar
            r_open = prev_c
            r_high = max(prev_c, c)
            r_low  = min(prev_c, c)
        else:
            new_bar = abs(c - r_open) >= atr_v
            if new_bar:
                r_open = prev_c
                r_high = max(prev_c, c)
                r_low  = min(prev_c, c)
            else:
                r_high = max(r_high, c)
                r_low  = min(r_low,  c)

        r_high_hist[i] = r_high
        r_low_hist[i]  = r_low

    return r_high_hist, r_low_hist


# ==============================================================
#  DAILY STOP MANAGEMENT
# ==============================================================
def check_daily_stop(coin: str) -> bool:
    """
    Check daily drawdown stop.
    Resets at midnight UTC.
    Returns True if trading is paused today for this coin.
    """
    state = _get_coin_state(coin)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Midnight UTC reset
    if state["daily_date"] != today:
        prev_eq                   = state["equity"]
        state["daily_date"]       = today
        state["daily_start_equity"] = prev_eq
        state["daily_stop"]       = False
        log.info(f"[{coin}] 📅 New day ({today}) — daily equity reset to {prev_eq:.2f}")

    # First-run init
    if state["daily_start_equity"] is None:
        state["daily_start_equity"] = state["equity"]

    # Threshold check
    threshold = state["daily_start_equity"] * (1.0 - DAILY_STOP_PCT)
    if state["equity"] < threshold:
        if not state["daily_stop"]:
            log.warning(
                f"[{coin}] ⛔ Daily stop triggered! "
                f"equity={state['equity']:.2f} < {threshold:.2f} "
                f"(start={state['daily_start_equity']:.2f}, -{DAILY_STOP_PCT*100:.0f}%)"
            )
        state["daily_stop"] = True

    return state["daily_stop"]


# ==============================================================
#  SIGNAL DETECTION  (mirrors JP v7 Pine Script exactly)
# ==============================================================
def detect_signal(coin: str, bars: list,
                  ha_bull_1h: bool = None,
                  ha_bear_1h: bool = None) -> tuple:
    """
    Analyse coin TF bars and return signal + trade parameters.

    Returns:
        (signal, sl_price, tp_price, entry_price, meta_dict)
        signal = "long" | "short" | None
    """
    params = COIN_PARAMS[coin]
    hma_f            = params["hma_fast"]
    hma_s            = params["hma_slow"]
    adx_trend_thresh = params["adx_trend"]
    adx_range_thresh = params["adx_range"]
    strong_thresh    = params["strong_thresh"]
    di_ratio         = params["di_ratio"]

    min_bars = max(RSI_LEN + 50, 120)
    if len(bars) < min_bars:
        log.warning(f"[{coin}] Only {len(bars)} bars — need {min_bars} for reliable indicators")

    # Extract OHLCV arrays
    highs   = np.array([b["h"] for b in bars], dtype=float)
    lows    = np.array([b["l"] for b in bars], dtype=float)
    closes  = np.array([b["c"] for b in bars], dtype=float)
    volumes = np.array([b["v"] for b in bars], dtype=float)

    n      = len(closes)
    i      = n - 1        # index of last (just-closed) bar
    c      = float(closes[i])
    prev_c = float(closes[i - 1]) if n > 1 else c
    cur_vol = float(volumes[i])

    # ── Indicators ──
    hma_fast_arr   = calc_hma(closes, hma_f)
    hma_slow_arr   = calc_hma(closes, hma_s)
    atr            = calc_atr(highs, lows, closes, ATR_LEN)
    atr_sma        = calc_sma(atr, ATR_SMA_LEN)
    vol_sma        = calc_sma(volumes, VOL_SMA_LEN)
    rsi            = calc_rsi(closes, RSI_LEN)
    di_plus, di_minus, adx = calc_adx(highs, lows, closes, ADX_LEN)
    upper_bb, lower_bb, bb_mid = calc_bollinger_bands(closes, BB_LEN, BB_MULT)

    # ── Range bars (Pine Script exact replica) ──
    r_high_hist, r_low_hist = compute_range_bars(closes, atr)

    # VWAP / VAH / VAL
    typical = (highs + lows + closes) / 3.0
    vwap    = calc_rolling_vwap(typical, volumes, VWAP_BARS)
    vwap_h  = vwap + VP_K * atr
    vwap_l  = vwap - VP_K * atr
    vah     = np.full(n, np.nan)
    val     = np.full(n, np.nan)
    for j in range(VWAP_BARS - 1, n):
        w      = slice(j - VWAP_BARS + 1, j + 1)
        vah[j] = float(np.nanmax(vwap_h[w]))
        val[j] = float(np.nanmin(vwap_l[w]))

    # ── Last-bar scalars ──
    r_high       = float(r_high_hist[i])
    r_low        = float(r_low_hist[i])
    cur_hma_fast = float(hma_fast_arr[i])
    cur_hma_slow = float(hma_slow_arr[i])
    cur_atr      = float(atr[i])
    cur_atr_sma  = float(atr_sma[i])
    cur_adx      = float(adx[i])
    cur_di_plus  = float(di_plus[i])
    cur_di_minus = float(di_minus[i])
    cur_rsi      = float(rsi[i])
    cur_vol_sma  = float(vol_sma[i])
    cur_upper_bb = float(upper_bb[i])
    cur_lower_bb = float(lower_bb[i])
    cur_bb_mid   = float(bb_mid[i])   # SMA20 = ma20 in Pine
    cur_vah      = float(vah[i]) if not np.isnan(vah[i]) else float("nan")
    cur_val      = float(val[i]) if not np.isnan(val[i]) else float("nan")

    # ── Validate critical indicators ──
    nan_fields = {k: v for k, v in {
        "hma_fast": cur_hma_fast, "hma_slow": cur_hma_slow,
        "atr": cur_atr, "adx": cur_adx,
        "rsi": cur_rsi, "vol_sma": cur_vol_sma,
    }.items() if math.isnan(v)}
    if nan_fields:
        log.warning(f"[{coin}] NaN indicators {list(nan_fields.keys())} — need more bars")
        return None, 0.0, 0.0, c, {}

    # ── inRng filter: ADX < 20 AND curVol < vol25 (25th pct of last 100 bars) ──
    vol_hist = volumes[max(0, n - 100):]
    vol25    = float(np.percentile(vol_hist, 25))
    in_rng   = cur_adx < IN_RNG_ADX_THRESH and cur_vol < vol25

    # ── Regime detection ──
    vol_high  = cur_atr > cur_atr_sma * 1.5
    vol_up    = cur_vol > cur_vol_sma * VOL_MULT
    explosive = vol_high and (cur_rsi > 75 or cur_rsi < 25) and vol_up

    is_trending = cur_adx > adx_trend_thresh and not explosive

    # isRanging: per-coin ADX threshold + r_high/r_low within BB
    bb_valid    = (not math.isnan(r_high) and not math.isnan(r_low)
                   and not math.isnan(cur_upper_bb) and not math.isnan(cur_lower_bb))
    is_ranging  = (cur_adx < adx_range_thresh
                   and not explosive
                   and bb_valid
                   and r_high <= cur_upper_bb and r_low >= cur_lower_bb)

    is_explosive = explosive and not is_trending and not is_ranging

    regime_str = ("TREND"     if is_trending  else
                  "EXPLOSIVE" if is_explosive else
                  "RANGE"     if is_ranging   else "NEUTRAL")

    # ── ma20 for pullback conditions = HMA fast (= HMA20 for SOL, HMA25 for ETH) ──
    ma20_val = cur_hma_fast

    # ── Pullback conditions use r_high/r_low (NOT candle high/low) ──
    pullback_long  = (not math.isnan(r_low)
                      and r_low <= ma20_val and c > ma20_val
                      and 35 < cur_rsi < 65)
    pullback_short = (not math.isnan(r_high)
                      and r_high >= ma20_val and c < ma20_val
                      and 35 < cur_rsi < 65)

    # ── Trend entry conditions ──
    bull_trend = cur_di_plus  > cur_di_minus and c > cur_hma_slow
    bear_trend = cur_di_minus > cur_di_plus  and c < cur_hma_slow

    entry_lt = is_trending and bull_trend and pullback_long
    entry_st = is_trending and bear_trend and pullback_short

    # ── Explosive entry conditions ──
    breakout_up   = (not math.isnan(cur_vah) and c > cur_vah and vol_high
                     and (c - prev_c) > cur_atr * 0.8 and vol_up)
    breakout_down = (not math.isnan(cur_val) and c < cur_val and vol_high
                     and (c - prev_c) < -cur_atr * 0.8 and vol_up)

    entry_le = is_explosive and breakout_up
    entry_se = is_explosive and breakout_down

    # ── Anti-filters: strongBull/Bear (per-coin params) ──
    strong_bull = cur_adx > strong_thresh and cur_di_plus  > cur_di_minus * di_ratio
    strong_bear = cur_adx > strong_thresh and cur_di_minus > cur_di_plus  * di_ratio

    entry_long  = (entry_lt or entry_le)  and not strong_bear
    entry_short = (entry_st or entry_se)  and not strong_bull

    # ── Heikin Ashi 1H filter (SOL only) ──
    if params["use_ha_filter"]:
        if entry_long and ha_bull_1h is not None and not ha_bull_1h:
            entry_long = False
            log.info(f"[{coin}] HA 1H filter: blocked LONG (1H HA not bullish)")
        if entry_short and ha_bear_1h is not None and not ha_bear_1h:
            entry_short = False
            log.info(f"[{coin}] HA 1H filter: blocked SHORT (1H HA not bearish)")

    # ── SL calculation uses r_low_hist / r_high_hist (not raw candle high/low) ──
    rlow_15  = r_low_hist[max(0, n - 15):]
    rhigh_15 = r_high_hist[max(0, n - 15):]

    if is_ranging or in_rng:
        # Ranging mode: SL derived from VAH/VAL
        sl_long  = cur_val * 0.995 if not math.isnan(cur_val) else c * 0.980
        sl_short = cur_vah * 1.005 if not math.isnan(cur_vah) else c * 1.020
    else:
        # Trending/explosive: swing low/high from range bars
        sl_long  = max(float(np.nanmin(rlow_15))  * 0.995, c * 0.980)
        sl_short = min(float(np.nanmax(rhigh_15)) * 1.005, c * 1.020)

    # ── TP = entry ± (entry - SL) × TP_MULT ──
    tp_long  = c + (c - sl_long)  * TP_MULT
    tp_short = c - (sl_short - c) * TP_MULT

    # ── Log bar summary ──
    log.info(
        f"[{coin}] close={c:.4f} | ADX={cur_adx:.1f} DI+={cur_di_plus:.1f} "
        f"DI-={cur_di_minus:.1f} | RSI={cur_rsi:.1f} | ATR={cur_atr:.4f} | "
        f"regime={regime_str} volHigh={vol_high} volUp={vol_up} inRng={in_rng}"
    )
    log.info(
        f"[{coin}] HMA{hma_f}={cur_hma_fast:.4f} HMA{hma_s}={cur_hma_slow:.4f} | "
        f"rHigh={r_high:.4f} rLow={r_low:.4f} | ma20={ma20_val:.4f} | "
        f"BB=[{cur_lower_bb:.4f}…{cur_upper_bb:.4f}] | "
        f"VAH={'n/a' if math.isnan(cur_vah) else f'{cur_vah:.4f}'} "
        f"VAL={'n/a' if math.isnan(cur_val) else f'{cur_val:.4f}'}"
    )
    log.info(
        f"[{coin}] LT={entry_lt} LE={entry_le} ST={entry_st} SE={entry_se} | "
        f"strongBull={strong_bull} strongBear={strong_bear}"
    )

    meta = {
        "close":       c,
        "atr":         cur_atr,
        "adx":         cur_adx,
        "di_plus":     cur_di_plus,
        "di_minus":    cur_di_minus,
        "rsi":         cur_rsi,
        "hma_fast":    cur_hma_fast,
        "hma_slow":    cur_hma_slow,
        "r_high":      r_high,
        "r_low":       r_low,
        "regime":      regime_str,
        "is_trending": is_trending,
        "is_explosive": is_explosive,
        "is_ranging":  is_ranging,
        "in_rng":      in_rng,
        "tp_mult":     TP_MULT,
        "vah":         cur_vah,
        "val":         cur_val,
        "ha_bull_1h":  ha_bull_1h,
        "ha_bear_1h":  ha_bear_1h,
    }

    if entry_long:
        log.info(
            f"[{coin}] ✅ SIGNAL LONG | "
            f"SL={sl_long:.4f} TP={tp_long:.4f} (RR={TP_MULT}:1)"
        )
        return "long", sl_long, tp_long, c, meta

    if entry_short:
        log.info(
            f"[{coin}] ✅ SIGNAL SHORT | "
            f"SL={sl_short:.4f} TP={tp_short:.4f} (RR={TP_MULT}:1)"
        )
        return "short", sl_short, tp_short, c, meta

    # ── No signal — explain why ──
    reasons = []
    if not is_trending and not is_explosive:
        reasons.append(f"regime={regime_str}(ADX={cur_adx:.1f})")
    if not bull_trend and not bear_trend:
        reasons.append("no_DI_direction")
    if not (pullback_long or pullback_short or breakout_up or breakout_down):
        reasons.append("no_entry_pattern")
    if strong_bull:
        reasons.append("strongBull_filter")
    if strong_bear:
        reasons.append("strongBear_filter")

    log.info(f"[{coin}] — No signal | {' | '.join(reasons) or 'conditions not met'}")
    return None, 0.0, 0.0, c, meta


# ==============================================================
#  POSITION UTILITIES
# ==============================================================
def _round_price(x: float) -> float:
    """5 significant digits — Hyperliquid requirement."""
    if x <= 0:
        return x
    d      = math.ceil(math.log10(abs(x)))
    factor = 10 ** (5 - d)
    return round(x * factor) / factor


def _get_coin_precision(coin: str) -> int:
    if _info is not None:
        try:
            meta = _info.meta()
            for asset in meta.get("universe", []):
                if asset.get("name") == coin:
                    return asset.get("szDecimals", 3)
        except Exception:
            pass
    return COIN_PRECISION_DEFAULT.get(coin, 3)


def _round_qty(qty: float, coin: str) -> float:
    prec   = _get_coin_precision(coin)
    factor = 10 ** prec
    return math.floor(qty * factor) / factor


def get_open_position(coin: str) -> float:
    """Returns size: >0 long, <0 short, 0 none."""
    if DRY_RUN:
        pos = _positions.get(coin)
        if pos:
            return pos["qty"] if pos["side"] == "long" else -pos["qty"]
        return 0.0

    if _info is None or _account is None:
        return 0.0
    try:
        state = _info.user_state(_account.address)
        for ap in state.get("assetPositions", []):
            p = ap.get("position", {})
            if p.get("coin") == coin:
                szi = float(p.get("szi", 0))
                if szi != 0:
                    return szi
    except Exception as e:
        log.error(f"get_open_position({coin}): {e}")
    return 0.0


# ==============================================================
#  DCA MANAGEMENT
# ==============================================================
def check_and_execute_dca(coin: str, current_price: float) -> bool:
    """
    Check DCA condition for an open position and execute if triggered.
    Returns True if DCA was executed.

    LONG:  DCA if close < last_dca_price * (1 - 0.02) AND dca_count < MAX_DCA
    SHORT: DCA if close > last_dca_price * (1 + 0.02) AND dca_count < MAX_DCA
    After DCA: recalculate TP using avg_price and original SL.
    """
    pos   = _positions.get(coin)
    state = _get_coin_state(coin)

    if not pos:
        return False

    side      = pos["side"]
    dca_count = state.get("dca_count", 0)
    last_dca  = state.get("last_dca_price")

    if dca_count >= MAX_DCA:
        return False
    if last_dca is None:
        return False

    # Check DCA trigger distance
    if side == "long":
        triggered = current_price < last_dca * (1.0 - DCA_DIST)
    elif side == "short":
        triggered = current_price > last_dca * (1.0 + DCA_DIST)
    else:
        return False

    if not triggered:
        return False

    # Execute DCA
    dca_qty = state.get("dca_qty") or pos["qty"]
    if dca_qty <= 0:
        dca_qty = pos["qty"]

    log.info(
        f"[{coin}] 📈 DCA #{dca_count + 1} triggered | "
        f"side={side} price={current_price:.4f} (prev_dca={last_dca:.4f}, "
        f"drop={abs(current_price/last_dca - 1)*100:.2f}%)"
    )

    # Recalculate average price
    old_qty    = pos["qty"]
    total_qty  = old_qty + dca_qty
    avg_price  = (pos["entry"] * old_qty + current_price * dca_qty) / total_qty

    # New TP based on original SL and average price
    sl_entry       = state.get("sl_entry", pos["sl"])
    entry_trending = state.get("entry_is_trending", False)
    rr             = DCA_TP_RR_TREND if entry_trending else DCA_TP_RR_OTHER

    if side == "long":
        new_tp = avg_price + (avg_price - sl_entry) * rr
    else:
        new_tp = avg_price - (sl_entry - avg_price) * rr

    new_tp_px = _round_price(new_tp)

    # Update state
    state["dca_count"]      = dca_count + 1
    state["last_dca_price"] = current_price
    state["avg_price"]      = avg_price

    # Update position dict (SL unchanged)
    pos["qty"]   = total_qty
    pos["entry"] = avg_price
    pos["tp"]    = new_tp_px

    log.info(
        f"[{coin}] DCA #{state['dca_count']} done | "
        f"avg_price={avg_price:.4f} new_tp={new_tp_px:.4f} "
        f"sl={pos['sl']:.4f} (unchanged) | rr={rr} trending={entry_trending}"
    )

    # Live order
    if not DRY_RUN and _exchange is not None:
        try:
            is_buy = (side == "long")
            _exchange.market_open(coin, is_buy, dca_qty)
            log.info(f"[{coin}] DCA live order placed: {dca_qty} {coin}")
            # Note: TP trigger update would need cancel+replace logic here
        except Exception as e:
            log.error(f"[{coin}] DCA live order error: {e}")

    return True


# ==============================================================
#  DRY_RUN POSITION MONITOR (SL/TP simulation)
# ==============================================================
def monitor_position(coin: str, current_price: float):
    """
    Simulate SL/TP hit in DRY_RUN mode.
    In live mode, exchange manages SL/TP trigger orders.
    Also updates daily equity tracking.
    """
    pos = _positions.get(coin)
    if not pos:
        return

    side  = pos["side"]
    sl    = pos["sl"]
    tp    = pos["tp"]
    entry = pos["entry"]
    cap   = pos.get("capital", CAPITAL.get(coin, 500.0))
    qty   = pos["qty"]

    hit = None
    if side == "long":
        if current_price <= sl:
            hit = "SL"
        elif current_price >= tp:
            hit = "TP"
        pnl_sign = 1
    else:  # short
        if current_price >= sl:
            hit = "SL"
        elif current_price <= tp:
            hit = "TP"
        pnl_sign = -1

    if hit is None:
        return

    pnl_usdc  = qty * (current_price - entry) * pnl_sign
    pnl_pct   = pnl_usdc / cap * 100
    cap_after = cap + pnl_usdc

    log.info(
        f"[{coin}] [DRY RUN] {hit} HIT! "
        f"{side.upper()} {entry:.4f} → {current_price:.4f} | "
        f"PnL={pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%)"
    )

    # Record exit in trade journal
    journal_id = pos.get("journal_id")
    if journal_id:
        trade_journal.record_exit(
            trade_id=journal_id,
            exit_price=current_price,
            exit_reason=hit,
            pnl_usdc=pnl_usdc,
            pnl_pct=pnl_pct,
        )

    labouch.on_close(coin, current_price, cap_after)
    del _positions[coin]
    _save_positions()

    # Update equity for daily stop tracking
    state = _get_coin_state(coin)
    state["equity"] = state.get("equity", cap) + pnl_usdc

    # Reset DCA state
    state["dca_count"]       = 0
    state["last_dca_price"]  = None
    state["avg_price"]       = None
    state["sl_entry"]        = None
    state["dca_qty"]         = None
    state["entry_is_trending"] = False

    status = labouch.get_status(coin)
    log.info(
        f"[{coin}] Labouchere after close: "
        f"seq={status['sequence']} mult={status['multiplier']:.2f}x "
        f"capital={status['active_capital']:.2f} | "
        f"equity={state['equity']:.2f}"
    )


# ==============================================================
#  OPEN POSITION
# ==============================================================
def open_position(coin: str, side: str,
                  price: float, sl: float, tp: float,
                  meta: dict):
    """
    Size via Labouchere INVERSÉ (with per-coin leverage_lab_mult cap),
    then place order on Hyperliquid.
    In DRY_RUN: logs the order without sending it.
    """
    capital = CAPITAL[coin]
    params  = COIN_PARAMS[coin]
    lev_cap = params["leverage_lab_mult"]

    # Daily stop check
    if check_daily_stop(coin):
        log.warning(f"[{coin}] ⛔ Daily stop active — no new entry")
        return

    # Stop-session check (Labouchere)
    ok, stop_reason = labouch.should_trade(coin, capital)
    if not ok:
        log.warning(f"[{coin}] 🛑 {stop_reason}")
        return

    # Guard post-reboot: skip si déjà dans le même sens (restauré depuis disque)
    existing_pos = _positions.get(coin)
    if existing_pos and existing_pos.get("side") == side:
        log.info(f"[{coin}] Already in {side.upper()} (restored from persist) — skipping re-entry")
        return

    # Labouchere sizing
    lab_mult = labouch.get_multiplier(coin, capital)
    # Cap at leverageLabMult (per coin: 2.0 for SOL, 3.0 for ETH)
    lab_mult = min(lab_mult, lev_cap * 100.0 / (capital / capital))  # keep as pct

    qty_raw = (capital * lab_mult / 100.0) / price
    qty     = _round_qty(qty_raw, coin) if qty_raw > 0 else 0.0

    if qty <= 0:
        log.error(f"[{coin}] Qty=0, skipping (price={price}, mult={lab_mult:.2f})")
        return

    is_buy   = (side == "long")
    sl_px    = _round_price(sl)
    tp_px    = _round_price(tp)
    entry_px = _round_price(price)
    notional = qty * price

    log.info(
        f"\n{'='*60}\n"
        f"[{coin}] 🎯 {'BUY LONG' if is_buy else 'SELL SHORT'} | "
        f"qty={qty} @ {entry_px} | "
        f"SL={sl_px} TP={tp_px} | "
        f"mult={lab_mult:.2f}% notional={notional:.0f} USDC | "
        f"regime={meta.get('regime','?')} RR={TP_MULT}:1 "
        f"lev_cap={lev_cap}x"
    )

    if DRY_RUN:
        _positions[coin] = {
            "side":    side,
            "entry":   entry_px,
            "qty":     qty,
            "sl":      sl_px,
            "tp":      tp_px,
            "regime":  meta.get("regime"),
            "capital": capital,
            "ts":      datetime.now(timezone.utc).isoformat(),
        }

        # Initialize DCA state
        state = _get_coin_state(coin)
        state["dca_count"]        = 0
        state["last_dca_price"]   = entry_px
        state["avg_price"]        = entry_px
        state["sl_entry"]         = sl_px
        state["entry_is_trending"] = meta.get("is_trending", False)
        state["dca_qty"]          = qty

        # Record entry in trade journal
        journal_id = trade_journal.record_entry(
            coin=coin,
            side=side,
            entry_price=entry_px,
            qty=qty,
            sl=sl_px,
            tp=tp_px,
            regime=meta.get("regime", "UNKNOWN"),
            capital=capital,
            lab_mult=lab_mult,
        )
        _positions[coin]["journal_id"] = journal_id
        _save_positions()

        labouch.on_entry(coin, entry_px, qty, side, capital)
        log.info(
            f"[{coin}] [DRY RUN] Simulated {side.upper()} {qty} @ {entry_px} | "
            f"SL={sl_px} TP={tp_px} | journal_id={journal_id[:8]}"
        )
        log.info(f"[{coin}] Labouchere: {labouch.get_status(coin)}")
        return

    # ── LIVE ORDER ──
    if _exchange is None:
        log.error(f"[{coin}] Exchange not initialized — cannot place live order")
        return

    try:
        # Set isolated leverage (use leverage_lab_mult as leverage level)
        lev_int = max(1, int(lev_cap))
        try:
            _exchange.update_leverage(lev_int, coin, is_cross=False)
        except Exception as e:
            log.warning(f"[{coin}] Leverage warning: {e}")

        # Close opposite position if any
        existing = get_open_position(coin)
        if (existing > 0 and not is_buy) or (existing < 0 and is_buy):
            log.info(f"[{coin}] Closing opposite position ({existing})")
            _exchange.market_close(coin)
            time.sleep(2)

        if (existing > 0 and is_buy) or (existing < 0 and not is_buy):
            log.info(f"[{coin}] Already {side} — skipping")
            return

        # Market entry
        result = _exchange.market_open(coin, is_buy, qty)
        log.info(f"[{coin}] Entry result: {result}")

        labouch.on_entry(coin, entry_px, qty, side, capital)

        # Initialize DCA state
        state = _get_coin_state(coin)
        state["dca_count"]        = 0
        state["last_dca_price"]   = entry_px
        state["avg_price"]        = entry_px
        state["sl_entry"]         = sl_px
        state["entry_is_trending"] = meta.get("is_trending", False)
        state["dca_qty"]          = qty

        # SL trigger order
        sl_is_buy = not is_buy
        sl_lim    = _round_price(sl_px * 1.10 if sl_is_buy else sl_px * 0.90)
        sl_resp   = _exchange.order(
            coin, is_buy=sl_is_buy, sz=qty, limit_px=sl_lim,
            order_type={"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True,
        )
        log.info(f"[{coin}] SL @ {sl_px}: {sl_resp}")

        # TP trigger order
        tp_is_buy = not is_buy
        tp_lim    = _round_price(tp_px * 0.95 if tp_is_buy else tp_px * 1.05)
        tp_resp   = _exchange.order(
            coin, is_buy=tp_is_buy, sz=qty, limit_px=tp_lim,
            order_type={"trigger": {"triggerPx": tp_px, "isMarket": True, "tpsl": "tp"}},
            reduce_only=True,
        )
        log.info(f"[{coin}] TP @ {tp_px}: {tp_resp}")

        _positions[coin] = {
            "side": side, "entry": entry_px, "qty": qty,
            "sl": sl_px, "tp": tp_px, "regime": meta.get("regime"),
            "capital": capital, "ts": datetime.now(timezone.utc).isoformat(),
        }
        _save_positions()

    except Exception as e:
        log.error(f"[{coin}] open_position LIVE error: {e}", exc_info=True)


# ==============================================================
#  MAIN PROCESSING CYCLE (one coin)
# ==============================================================
def process_coin(coin: str):
    """Full cycle: fetch → aggregate → indicators → signal → trade."""
    tf = COIN_TF[coin]
    log.info(f"\n── [{coin}] ──────────────────────────────────────────")

    # Fetch 1m data (enough for BARS_NEEDED TF bars + warmup)
    n_1m = BARS_NEEDED * tf + 120
    try:
        candles_1m = fetch_candles_1m(coin, n_1m)
    except Exception as e:
        log.error(f"[{coin}] fetch_candles_1m error: {e}")
        return

    if not candles_1m:
        log.error(f"[{coin}] No 1m candles returned!")
        return

    bars_tf = aggregate_to_tf(candles_1m, tf)
    log.info(f"[{coin}] {len(candles_1m)} 1m → {len(bars_tf)} {tf}m bars")

    if len(bars_tf) < 60:
        log.error(f"[{coin}] Too few {tf}m bars ({len(bars_tf)}) — waiting for more data")
        return

    current_price = float(bars_tf[-1]["c"])

    # ── Check daily stop (reset at midnight UTC) ──
    if check_daily_stop(coin):
        log.warning(f"[{coin}] ⛔ Daily stop active — no trading today")
        return

    # ── DRY_RUN: simulate SL/TP hits ──
    if DRY_RUN:
        monitor_position(coin, current_price)

    # ── HA 1H filter (SOL only) ──
    ha_bull_1h: bool | None = None
    ha_bear_1h: bool | None = None
    if COIN_PARAMS[coin]["use_ha_filter"]:
        ha_bull_1h, ha_bear_1h = fetch_ha_1h_candles(coin)

    # ── Check open position ──
    existing = get_open_position(coin)
    if existing != 0:
        direction = "LONG" if existing > 0 else "SHORT"
        # Attempt DCA while in position
        check_and_execute_dca(coin, current_price)
        log.info(f"[{coin}] Already in {direction} ({existing}) — no new entry")
        return

    # ── Detect signal ──
    try:
        signal, sl, tp, price, meta = detect_signal(
            coin, bars_tf, ha_bull_1h, ha_bear_1h
        )
    except Exception as e:
        log.error(f"[{coin}] detect_signal error: {e}", exc_info=True)
        return

    # ── Act on signal ──
    if signal:
        # Anti-stale: skip si on est trop loin de la clôture de bougie
        tf_min  = COIN_TF.get(coin, 30)
        max_lag = timedelta(minutes=tf_min * MAX_SIGNAL_AGE_BARS + 5)
        bar_dt  = datetime.fromtimestamp(bars_tf[-1]["T"] / 1000, tz=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        if (now_utc - bar_dt) > max_lag:
            log.warning(
                f"[{coin}] ⏰ Stale signal "
                f"({int((now_utc - bar_dt).total_seconds() // 60)}min old, "
                f"max={int(max_lag.total_seconds() // 60)}min) — skipping"
            )
        else:
            open_position(coin, signal, price, sl, tp, meta)
    else:
        log.info(f"[{coin}] No action this bar")


# ==============================================================
#  TIMING
# ==============================================================
def next_bar_close_for_tf(tf_minutes: int) -> datetime:
    """
    UTC datetime of the next bar close for a given timeframe.
    Bars aligned from midnight UTC: bar k = [k×tf min, (k+1)×tf min).
    """
    now          = datetime.now(timezone.utc)
    min_of_day   = now.hour * 60 + now.minute
    current_bar  = min_of_day // tf_minutes
    next_bar_min = (current_bar + 1) * tf_minutes

    if next_bar_min >= 1440:
        next_dt = (now.replace(hour=0, minute=0, second=8, microsecond=0)
                   + timedelta(days=1))
    else:
        next_dt = now.replace(
            hour=next_bar_min // 60,
            minute=next_bar_min % 60,
            second=8,       # 8s after bar open for candle to finalize
            microsecond=0,
        )

    # Safeguard: if already past, skip to next bar
    if next_dt <= now:
        next_dt += timedelta(minutes=tf_minutes)

    return next_dt


def next_bar_close_utc() -> datetime:
    """
    Returns the earliest next bar close across all coins (SOL 29m, ETH 30m).
    """
    return min(next_bar_close_for_tf(tf) for tf in COIN_TF.values())


# ==============================================================
#  MAIN LOOP
# ==============================================================
def run():
    log.info("=" * 60)
    log.info("🤖 Autonomous Trading Bot — JP v7.1 (TradingView parity)")
    log.info(f"   Coins     : {COINS}")
    log.info(f"   Timeframes: SOL={COIN_TF['SOL']}m  ETH={COIN_TF['ETH']}m")
    log.info(f"   Capital   : {CAPITAL}")
    log.info(f"   DRY_RUN   : {DRY_RUN}")
    log.info(f"   MAINNET   : {MAINNET}")
    log.info("── Per-coin params ──────────────────────────────────")
    for coin, p in COIN_PARAMS.items():
        log.info(
            f"   {coin}: HMA {p['hma_fast']}/{p['hma_slow']} | "
            f"ADX trend>{p['adx_trend']} range<{p['adx_range']} "
            f"strong>{p['strong_thresh']} | "
            f"HA={p['use_ha_filter']} | lev={p['leverage_lab_mult']}x"
        )
    log.info(f"   TP_MULT={TP_MULT} | DCA max={MAX_DCA} dist={DCA_DIST*100:.0f}% | "
             f"Daily stop={DAILY_STOP_PCT*100:.0f}%")
    log.info("=" * 60)

    _init_hl()

    # Init Labouchere
    for coin in COINS:
        status = labouch.get_status(coin)
        cap    = CAPITAL[coin]
        if status.get("series_number", 0) == 0 or status.get("active_capital", 0.0) == 0.0:
            labouch.init_series_with_margin(coin, capital=cap, margin=cap)
            log.info(f"[{coin}] Labouchere init: capital={cap} + margin={cap}")
        else:
            log.info(
                f"[{coin}] Labouchere loaded: "
                f"series={status['series_number']} "
                f"capital={status['active_capital']:.0f} "
                f"seq={status['sequence']}"
            )
        # Init per-coin state
        _get_coin_state(coin)

    # ── Restore positions from disk (persist across Railway redeploys) ──
    global POSITIONS_FILE
    POSITIONS_FILE = os.path.join(_resolve_persist_dir(), "positions.json")
    _load_positions()
    if _positions:
        log.info(f"[persist] ⚠️  Positions restored after restart: {list(_positions.keys())} — will skip re-entry in same direction")

    # Immediate first run on startup
    log.info("\n📊 Running initial scan on startup...")
    for coin in COINS:
        try:
            process_coin(coin)
        except Exception as e:
            log.error(f"Startup [{coin}]: {e}", exc_info=True)

    # Main loop — wake at earliest bar close across all coins
    while True:
        try:
            next_close = next_bar_close_utc()
            now        = datetime.now(timezone.utc)
            wait_secs  = (next_close - now).total_seconds()

            if wait_secs > 0:
                log.info(
                    f"\n⏳ Sleeping {wait_secs:.0f}s until next bar close: "
                    f"{next_close.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                )
                time.sleep(wait_secs)

            now_wakeup = datetime.now(timezone.utc)
            log.info(
                f"\n{'='*60}\n"
                f"🕯️  BAR CLOSE — "
                f"{now_wakeup.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )

            # Only process coins whose bar is closing now (±90s tolerance)
            for coin in COINS:
                tf         = COIN_TF[coin]
                coin_close = next_bar_close_for_tf(tf)
                last_close = coin_close - timedelta(minutes=tf)
                delta      = abs((now_wakeup - last_close).total_seconds())
                if delta <= 90:
                    try:
                        process_coin(coin)
                    except Exception as e:
                        log.error(f"[{coin}] process error: {e}", exc_info=True)
                else:
                    log.info(
                        f"[{coin}] Bar not due yet "
                        f"(next in {int((coin_close - now_wakeup).total_seconds())}s) — skipping"
                    )

        except KeyboardInterrupt:
            log.info("🛑 Bot stopped")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            log.info("Sleeping 60s before retry...")
            time.sleep(60)


# ==============================================================
def main():
    run()

if __name__ == "__main__":
    run()
