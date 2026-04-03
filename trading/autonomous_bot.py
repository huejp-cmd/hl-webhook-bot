#!/usr/bin/env python3
"""
Autonomous Trading Bot — JP v6.2 Logic
========================================
Runs independently of TradingView.
Fetches OHLCV directly from Hyperliquid, aggregates 1m → 29m,
detects signals (trend + explosive), sizes via Labouchere INVERSÉ,
and places orders on Hyperliquid perpetuals.

Coins     : SOL, ETH
Timeframe : 29M
Capital   : 500 USDC each
Mode      : DRY_RUN=true by default (logs orders without placing them)
"""

import os
import sys
import time
import math
import json
import logging
import threading
import requests
import numpy as np
from datetime import datetime, timezone, timedelta

# ── Add script directory to sys.path so labouch_manager is importable ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labouch_manager import LabouchManager

# ==============================================================
#  CONFIGURATION
# ==============================================================
COINS       = ["SOL", "ETH"]
TF_MINUTES  = 29
BARS_NEEDED = 220          # 29m bars to fetch (covers RSI-100 + warmup)

CAPITAL = {"SOL": 500.0, "ETH": 500.0}

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
MAINNET     = os.environ.get("MAINNET", "true").lower()  == "true"
DRY_RUN     = os.environ.get("DRY_RUN",  "true").lower() == "true"

HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# ── Indicator parameters (mirrors Pine Script v6.2) ──
ATR_LEN     = 10
ADX_LEN     = 14
RSI_LEN     = 100
BB_LEN      = 20
BB_MULT     = 2.0
VOL_SMA_LEN = 20
HMA_FAST    = 20
HMA_SLOW    = 50
VP_K        = 0.75   # VWAP band offset (×ATR)
VOL_MULT    = 1.4    # volume threshold vs SMA
VWAP_BARS   = 24     # rolling window for VWAP/VAH/VAL
ATR_SMA_LEN = 20     # SMA of ATR for volHigh detection

TP_MULT_TREND = 4.0
TP_MULT_EXPLO = 3.3

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
_positions: dict = {}   # coin → {side, entry, qty, sl, tp, regime, ts, capital}

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


def aggregate_to_29m(candles_1m: list) -> list:
    """
    Aggregate 1m candles → 29m bars aligned to midnight UTC.
    Bar k covers minutes [k×29, (k+1)×29) since 00:00 UTC.
    Drops current (incomplete) bar.
    """
    if not candles_1m:
        return []

    def bar_key(t_ms):
        dt  = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
        min_of_day = dt.hour * 60 + dt.minute
        return dt.strftime("%Y-%m-%d"), min_of_day // TF_MINUTES

    # Group 1m candles by (date, bar_index)
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
    # (close time < 90s ago → bar still open)
    if bars:
        now_ms = int(time.time() * 1000)
        if (now_ms - bars[-1]["T"]) < 90_000:
            bars.pop()
            log.debug("Dropped last (still-forming) 29m bar")

    log.debug(f"Aggregated {len(candles_1m)} 1m → {len(bars)} 29m bars")
    return bars


# ==============================================================
#  INDICATOR CALCULATIONS
# ==============================================================
def _wma(arr: np.ndarray, period: int) -> np.ndarray:
    """Weighted Moving Average (weights = 1, 2, …, period)."""
    n = len(arr)
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
        up   = high[i]     - high[i - 1]
        down = low[i - 1]  - low[i]
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
        rs = avg_g / avg_l if avg_l > 0 else np.inf
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
#  SIGNAL DETECTION  (mirrors JP v6.2 Pine Script logic)
# ==============================================================
def detect_signal(coin: str, bars: list) -> tuple:
    """
    Analyse 29m bars and return signal + trade parameters.

    Returns:
        (signal, sl_price, tp_price, entry_price, meta_dict)
        signal = "long" | "short" | None
    """
    min_bars = max(RSI_LEN + 50, 120)
    if len(bars) < min_bars:
        log.warning(f"[{coin}] Only {len(bars)} bars — need {min_bars} for reliable indicators")

    # Extract OHLCV
    highs   = np.array([b["h"] for b in bars], dtype=float)
    lows    = np.array([b["l"] for b in bars], dtype=float)
    closes  = np.array([b["c"] for b in bars], dtype=float)
    volumes = np.array([b["v"] for b in bars], dtype=float)

    # ── Calculate all indicators ──
    hma20   = calc_hma(closes, HMA_FAST)
    hma50   = calc_hma(closes, HMA_SLOW)
    atr     = calc_atr(highs, lows, closes, ATR_LEN)
    atr_sma = calc_sma(atr, ATR_SMA_LEN)
    vol_sma = calc_sma(volumes, VOL_SMA_LEN)
    rsi     = calc_rsi(closes, RSI_LEN)
    di_plus, di_minus, adx = calc_adx(highs, lows, closes, ADX_LEN)

    # VWAP / VAH / VAL
    typical = (highs + lows + closes) / 3.0
    vwap    = calc_rolling_vwap(typical, volumes, VWAP_BARS)
    vwap_h  = vwap + VP_K * atr
    vwap_l  = vwap - VP_K * atr

    n   = len(closes)
    vah = np.full(n, np.nan)
    val = np.full(n, np.nan)
    for i in range(VWAP_BARS - 1, n):
        w = slice(i - VWAP_BARS + 1, i + 1)
        vah[i] = float(np.nanmax(vwap_h[w]))
        val[i] = float(np.nanmin(vwap_l[w]))

    # ── Last (just-closed) bar ──
    i      = -1
    c      = float(closes[i])
    h      = float(highs[i])
    l      = float(lows[i])
    v      = float(volumes[i])
    c_prev = float(closes[i - 1]) if n > 1 else c

    cur_hma20    = float(hma20[i])
    cur_hma50    = float(hma50[i])
    cur_atr      = float(atr[i])
    cur_atr_sma  = float(atr_sma[i])
    cur_adx      = float(adx[i])
    cur_di_plus  = float(di_plus[i])
    cur_di_minus = float(di_minus[i])
    cur_rsi      = float(rsi[i])
    cur_vol_sma  = float(vol_sma[i])
    cur_vah      = float(vah[i]) if not np.isnan(vah[i]) else np.nan
    cur_val      = float(val[i]) if not np.isnan(val[i]) else np.nan

    # Validate critical indicators
    nan_fields = {
        k: v for k, v in {
            "hma20": cur_hma20, "hma50": cur_hma50, "atr": cur_atr,
            "adx": cur_adx, "di+": cur_di_plus, "di-": cur_di_minus,
            "rsi": cur_rsi, "vol_sma": cur_vol_sma,
        }.items() if math.isnan(v)
    }
    if nan_fields:
        log.warning(f"[{coin}] NaN indicators {list(nan_fields.keys())} — need more bars")
        return None, 0.0, 0.0, c, {}

    # ── REGIMES ──
    vol_high    = cur_atr > cur_atr_sma * 1.5
    vol_up      = v > cur_vol_sma * VOL_MULT
    explosive   = vol_high and (cur_rsi > 75 or cur_rsi < 25) and vol_up
    is_trending = cur_adx > 25 and not explosive
    is_ranging  = cur_adx < 20 and not (cur_adx > 25 or explosive)
    is_explosive = explosive and not (cur_adx > 25 or is_ranging)

    regime_str = ("TREND"     if is_trending  else
                  "EXPLOSIVE" if is_explosive else
                  "RANGE"     if is_ranging   else "NEUTRAL")

    # ── LONG CONDITIONS ──
    bull_trend    = cur_di_plus > cur_di_minus and c > cur_hma50
    pullback_long = l <= cur_hma20 and c > cur_hma20 and 40 < cur_rsi < 65
    entry_lt      = is_trending and bull_trend and pullback_long

    vah_ok      = not math.isnan(cur_vah)
    breakout_up = (vah_ok and c > cur_vah and vol_high
                   and (c - c_prev) > cur_atr * 0.8 and vol_up)
    entry_le    = is_explosive and breakout_up

    # ── SHORT CONDITIONS ──
    bear_trend     = cur_di_minus > cur_di_plus and c < cur_hma50
    pullback_short = h >= cur_hma20 and c < cur_hma20 and 35 < cur_rsi < 60
    entry_st       = is_trending and bear_trend and pullback_short

    val_ok        = not math.isnan(cur_val)
    breakout_down = (val_ok and c < cur_val and vol_high
                     and (c - c_prev) < -cur_atr * 0.8 and vol_up)
    entry_se      = is_explosive and breakout_down

    # ── ANTI-FILTERS ──
    strong_bull = cur_adx > 30 and cur_di_plus  > cur_di_minus * 1.5
    strong_bear = cur_adx > 30 and cur_di_minus > cur_di_plus  * 1.5

    entry_long  = (entry_lt or entry_le)  and not strong_bear
    entry_short = (entry_st or entry_se)  and not strong_bull

    # ── SL / TP ──
    recent_lows  = lows[max(0, n - 15):]
    recent_highs = highs[max(0, n - 15):]
    tp_mult      = TP_MULT_TREND if is_trending else TP_MULT_EXPLO

    sl_long  = max(float(np.min(recent_lows))  * 0.995, c * 0.980)
    tp_long  = c + (c - sl_long)  * tp_mult

    sl_short = min(float(np.max(recent_highs)) * 1.005, c * 1.020)
    tp_short = c - (sl_short - c) * tp_mult

    # ── LOG BAR SUMMARY ──
    log.info(
        f"[{coin}] close={c:.4f} | ADX={cur_adx:.1f} DI+={cur_di_plus:.1f} "
        f"DI-={cur_di_minus:.1f} | RSI={cur_rsi:.1f} | ATR={cur_atr:.4f} | "
        f"regime={regime_str} volHigh={vol_high} volUp={vol_up}"
    )
    log.info(
        f"[{coin}] HMA20={cur_hma20:.4f} HMA50={cur_hma50:.4f} | "
        f"VAH={'n/a' if math.isnan(cur_vah) else f'{cur_vah:.4f}'} "
        f"VAL={'n/a' if math.isnan(cur_val) else f'{cur_val:.4f}'}"
    )
    log.info(
        f"[{coin}] LT={entry_lt} LE={entry_le} ST={entry_st} SE={entry_se} | "
        f"strongBull={strong_bull} strongBear={strong_bear}"
    )

    meta = {
        "close": c, "atr": cur_atr, "adx": cur_adx,
        "di_plus": cur_di_plus, "di_minus": cur_di_minus,
        "rsi": cur_rsi, "hma20": cur_hma20, "hma50": cur_hma50,
        "regime": regime_str, "is_trending": is_trending,
        "is_explosive": is_explosive, "tp_mult": tp_mult,
        "vah": cur_vah, "val": cur_val,
    }

    if entry_long:
        log.info(
            f"[{coin}] ✅ SIGNAL LONG | "
            f"SL={sl_long:.4f} TP={tp_long:.4f} (RR={tp_mult}:1)"
        )
        return "long", sl_long, tp_long, c, meta

    if entry_short:
        log.info(
            f"[{coin}] ✅ SIGNAL SHORT | "
            f"SL={sl_short:.4f} TP={tp_short:.4f} (RR={tp_mult}:1)"
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
            p   = ap.get("position", {})
            if p.get("coin") == coin:
                szi = float(p.get("szi", 0))
                if szi != 0:
                    return szi
    except Exception as e:
        log.error(f"get_open_position({coin}): {e}")
    return 0.0


# ==============================================================
#  DRY_RUN POSITION MONITOR
# ==============================================================
def monitor_position(coin: str, current_price: float):
    """
    Simulate SL/TP hit in DRY_RUN mode.
    In live mode, exchange manages SL/TP trigger orders.
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

    pnl_usdc = qty * (current_price - entry) * pnl_sign
    pnl_pct  = pnl_usdc / cap * 100
    cap_after = cap + pnl_usdc

    log.info(
        f"[{coin}] [DRY RUN] {hit} HIT! "
        f"{side.upper()} {entry:.4f} → {current_price:.4f} | "
        f"PnL={pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%)"
    )

    labouch.on_close(coin, current_price, cap_after)
    del _positions[coin]

    status = labouch.get_status(coin)
    log.info(
        f"[{coin}] Labouchere after close: "
        f"seq={status['sequence']} mult={status['multiplier']:.2f}x "
        f"capital={status['active_capital']:.2f}"
    )


# ==============================================================
#  OPEN POSITION
# ==============================================================
def open_position(coin: str, side: str,
                  price: float, sl: float, tp: float,
                  meta: dict):
    """
    Size via Labouchere INVERSÉ, then place order on Hyperliquid.
    In DRY_RUN: logs the order without sending it.
    """
    capital = CAPITAL[coin]

    # Stop-session check
    ok, stop_reason = labouch.should_trade(coin, capital)
    if not ok:
        log.warning(f"[{coin}] 🛑 {stop_reason}")
        return

    # Labouchere sizing
    lab_mult = labouch.get_multiplier(coin, capital)
    qty_raw  = (capital * lab_mult / 100.0) / price
    qty      = _round_qty(qty_raw, coin) if qty_raw > 0 else 0.0

    if qty <= 0:
        log.error(f"[{coin}] Qty=0, skipping (price={price}, mult={lab_mult})")
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
        f"mult={lab_mult:.2f}x notional={notional:.0f} USDC | "
        f"regime={meta.get('regime','?')} RR={meta.get('tp_mult',4)}:1"
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
        labouch.on_entry(coin, entry_px, qty, side, capital)
        log.info(
            f"[{coin}] [DRY RUN] Simulated {side.upper()} {qty} @ {entry_px} | "
            f"SL={sl_px} TP={tp_px}"
        )
        log.info(f"[{coin}] Labouchere: {labouch.get_status(coin)}")
        return

    # ── LIVE ORDER ──
    if _exchange is None:
        log.error(f"[{coin}] Exchange not initialized — cannot place live order")
        return

    try:
        # Set isolated leverage ×2
        try:
            _exchange.update_leverage(2, coin, is_cross=False)
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

        # SL trigger
        sl_is_buy = not is_buy
        sl_lim    = _round_price(sl_px * 1.10 if sl_is_buy else sl_px * 0.90)
        sl_resp   = _exchange.order(
            coin, is_buy=sl_is_buy, sz=qty, limit_px=sl_lim,
            order_type={"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True,
        )
        log.info(f"[{coin}] SL @ {sl_px}: {sl_resp}")

        # TP trigger
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

    except Exception as e:
        log.error(f"[{coin}] open_position LIVE error: {e}", exc_info=True)


# ==============================================================
#  MAIN PROCESSING CYCLE (one coin)
# ==============================================================
def process_coin(coin: str):
    """Full cycle: fetch → aggregate → indicators → signal → trade."""
    log.info(f"\n── [{coin}] ──────────────────────────────────────────")

    # Fetch 1m data (enough for BARS_NEEDED 29m bars + warmup)
    n_1m = BARS_NEEDED * TF_MINUTES + 120
    try:
        candles_1m = fetch_candles_1m(coin, n_1m)
    except Exception as e:
        log.error(f"[{coin}] fetch_candles_1m error: {e}")
        return

    if not candles_1m:
        log.error(f"[{coin}] No 1m candles returned!")
        return

    bars_29m = aggregate_to_29m(candles_1m)
    log.info(f"[{coin}] {len(candles_1m)} 1m → {len(bars_29m)} 29m bars")

    if len(bars_29m) < 60:
        log.error(f"[{coin}] Too few 29m bars ({len(bars_29m)}) — waiting for more data")
        return

    current_price = float(bars_29m[-1]["c"])

    # DRY_RUN: check if SL/TP was hit
    if DRY_RUN:
        monitor_position(coin, current_price)

    # Detect signal
    try:
        signal, sl, tp, price, meta = detect_signal(coin, bars_29m)
    except Exception as e:
        log.error(f"[{coin}] detect_signal error: {e}", exc_info=True)
        return

    # Check existing position
    existing = get_open_position(coin)
    if existing != 0:
        direction = "LONG" if existing > 0 else "SHORT"
        log.info(f"[{coin}] Already in {direction} ({existing}) — no new entry")
        return

    # Act
    if signal:
        open_position(coin, signal, price, sl, tp, meta)
    else:
        log.info(f"[{coin}] No action this bar")


# ==============================================================
#  TIMING
# ==============================================================
def next_bar_close_utc() -> datetime:
    """
    UTC datetime of the next 29m bar close.
    Bars aligned from midnight UTC: bar k = [k×29 min, (k+1)×29 min).
    """
    now            = datetime.now(timezone.utc)
    min_of_day     = now.hour * 60 + now.minute
    current_bar    = min_of_day // TF_MINUTES
    next_bar_min   = (current_bar + 1) * TF_MINUTES

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
        next_dt += timedelta(minutes=TF_MINUTES)

    return next_dt


# ==============================================================
#  MAIN LOOP
# ==============================================================
def run():
    log.info("=" * 60)
    log.info("🤖 Autonomous Trading Bot — JP v6.2 Logic")
    log.info(f"   Coins     : {COINS}")
    log.info(f"   Timeframe : {TF_MINUTES}m")
    log.info(f"   Capital   : {CAPITAL}")
    log.info(f"   DRY_RUN   : {DRY_RUN}")
    log.info(f"   MAINNET   : {MAINNET}")
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

    # Immediate first run on startup
    log.info("\n📊 Running initial scan on startup...")
    for coin in COINS:
        try:
            process_coin(coin)
        except Exception as e:
            log.error(f"Startup [{coin}]: {e}", exc_info=True)

    # Main loop — wake at each 29m bar close
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

            log.info(
                f"\n{'='*60}\n"
                f"🕯️  BAR CLOSE — "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )

            for coin in COINS:
                try:
                    process_coin(coin)
                except Exception as e:
                    log.error(f"[{coin}] process error: {e}", exc_info=True)

        except KeyboardInterrupt:
            log.info("🛑 Bot stopped")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            log.info("Sleeping 60s before retry...")
            time.sleep(60)


# ==============================================================
if __name__ == "__main__":
    run()
