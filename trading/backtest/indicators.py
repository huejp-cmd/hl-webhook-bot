"""
indicators.py — Pure numpy/pandas implementations of all indicators used by JP v5.3.

Functions
---------
hma(series, length)
calc_atr(h, l, c, length)           — RMA-based, same as Pine Script ta.rma
calc_rsi(close, length)             — RMA-based
calc_dmi_adx(h, l, c, length)       — (diplus, diminus, adx)
calc_bb(close, length, mult)        — (basis, upper, lower)
calc_vwap_vah_val(h, l, c, volume, vpK, atr, lookback=24)
build_range_bars(df, atr_length, use_fixed=False, fixed_size=1.2)
"""

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
#  Low-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _wma(series: np.ndarray, length: int) -> np.ndarray:
    """Weighted Moving Average (linearly-weighted, same as Pine ta.wma)."""
    weights = np.arange(1, length + 1, dtype=float)
    total_w = weights.sum()
    out = np.full(len(series), np.nan)
    for i in range(length - 1, len(series)):
        out[i] = np.dot(series[i - length + 1: i + 1], weights) / total_w
    return out


def _rma(series: np.ndarray, length: int) -> np.ndarray:
    """
    Running Moving Average (Wilder's MA) — equivalent to Pine Script ta.rma.
    rma[0] = alpha * src + (1-alpha) * rma[-1]   where alpha = 1 / length
    """
    alpha = 1.0 / length
    out   = np.full(len(series), np.nan)
    # seed with first non-nan SMA
    for i in range(len(series)):
        if not np.isnan(series[i]):
            out[i] = series[i]
            for j in range(i + 1, len(series)):
                out[j] = alpha * series[j] + (1 - alpha) * out[j - 1]
            break
    return out


def _sma(series: np.ndarray, length: int) -> np.ndarray:
    """Simple Moving Average."""
    s = pd.Series(series)
    return s.rolling(length, min_periods=1).mean().to_numpy()


def _stdev(series: np.ndarray, length: int) -> np.ndarray:
    s = pd.Series(series)
    return s.rolling(length, min_periods=2).std(ddof=1).to_numpy()


# ─────────────────────────────────────────────────────────────────────────────
#  Public indicator functions
# ─────────────────────────────────────────────────────────────────────────────

def hma(series: pd.Series, length: int) -> pd.Series:
    """
    Hull Moving Average.
    hma = WMA(2*WMA(src, n/2) − WMA(src, n), sqrt(n))
    """
    src   = series.to_numpy(dtype=float)
    n2    = max(1, int(length // 2))
    sqrtn = max(1, int(np.floor(np.sqrt(length))))
    wma1  = _wma(src, n2)
    wma2  = _wma(src, length)
    delta = 2.0 * wma1 - wma2
    result = _wma(delta, sqrtn)
    return pd.Series(result, index=series.index)


def calc_atr(h: pd.Series, l: pd.Series, c: pd.Series, length: int) -> pd.Series:
    """
    ATR using RMA (Wilder's smoothing) — matches Pine Script calcAtr().
    True Range = max(h-l, |h-prev_c|, |l-prev_c|)
    """
    high_  = h.to_numpy(dtype=float)
    low_   = l.to_numpy(dtype=float)
    close_ = c.to_numpy(dtype=float)
    prev_c = np.roll(close_, 1)
    prev_c[0] = close_[0]

    tr = np.maximum(
        np.maximum(high_ - low_, np.abs(high_ - prev_c)),
        np.abs(low_ - prev_c),
    )
    atr = _rma(tr, length)
    return pd.Series(atr, index=h.index)


def calc_rsi(close: pd.Series, length: int) -> pd.Series:
    """RSI using RMA smoothing (matches Pine Script ta.rsi)."""
    delta = close.diff().to_numpy(dtype=float)
    gains = np.where(delta > 0, delta,  0.0)
    losses= np.where(delta < 0, -delta, 0.0)
    avg_gain = _rma(gains,  length)
    avg_loss = _rma(losses, length)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs  = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
        rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = np.where(avg_loss == 0, 100.0, rsi)
    return pd.Series(rsi, index=close.index)


def calc_dmi_adx(h: pd.Series, l: pd.Series, c: pd.Series,
                 length: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    DMI + ADX matching Pine Script calcDmi().
    Returns: (diplus, diminus, adx)
    """
    high_  = h.to_numpy(dtype=float)
    low_   = l.to_numpy(dtype=float)
    close_ = c.to_numpy(dtype=float)
    n      = len(high_)

    up   = np.maximum(np.diff(high_, prepend=high_[0]), 0)
    down = np.maximum(-np.diff(low_,  prepend=low_[0]),  0)

    # +DM / -DM
    plus_dm  = np.where((up > down) & (up > 0),   up,   0.0)
    minus_dm = np.where((down > up) & (down > 0), down,  0.0)

    # True Range
    prev_c    = np.roll(close_, 1); prev_c[0] = close_[0]
    tr = np.maximum(
        np.maximum(high_ - low_, np.abs(high_ - prev_c)),
        np.abs(low_ - prev_c),
    )

    s_plus  = _rma(plus_dm,  length)
    s_minus = _rma(minus_dm, length)
    s_tr    = _rma(tr,       length)

    with np.errstate(divide="ignore", invalid="ignore"):
        dip = np.where(s_tr != 0, 100 * s_plus  / s_tr, 0.0)
        dim = np.where(s_tr != 0, 100 * s_minus / s_tr, 0.0)
        dx  = np.where((dip + dim) != 0,
                       100 * np.abs(dip - dim) / (dip + dim), 0.0)

    adx_val = _rma(dx, length)

    idx = h.index
    return (
        pd.Series(dip,     index=idx),
        pd.Series(dim,     index=idx),
        pd.Series(adx_val, index=idx),
    )


def calc_bb(close: pd.Series, length: int,
            mult: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands.
    Returns: (basis, upper, lower)
    """
    basis = close.rolling(length, min_periods=1).mean()
    dev   = close.rolling(length, min_periods=2).std(ddof=1) * mult
    return basis, basis + dev, basis - dev


def calc_vwap_vah_val(h: pd.Series, l: pd.Series, c: pd.Series,
                      volume: pd.Series, vpK: float, atr: pd.Series,
                      lookback: int = 24) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Cumulative VWAP + VAH/VAL bands.
    vwap = cumsum(typical_price * volume) / cumsum(volume)
    vah  = rolling max of (vwap + vpK * atr) over lookback bars
    val  = rolling min of (vwap - vpK * atr) over lookback bars
    """
    tp     = (h + l + c) / 3.0
    cum_pv = (tp * volume).cumsum()
    cum_v  = volume.cumsum()
    vwap   = cum_pv / cum_v.replace(0, np.nan)

    upper_band = vwap + vpK * atr
    lower_band = vwap - vpK * atr

    vah = upper_band.rolling(lookback, min_periods=1).max()
    val = lower_band.rolling(lookback, min_periods=1).min()

    return vwap, vah, val


def build_range_bars(df: pd.DataFrame, atr_length: int = 10,
                     use_fixed: bool = False,
                     fixed_size: float = 1.2) -> pd.DataFrame:
    """
    Convert a standard OHLCV DataFrame to range bars.

    A new range bar is created when |close − rangeOpen| >= rangeSize.
    rangeSize = fixed_size  (if use_fixed)
              = ATR(atr_length) of the underlying bars  (otherwise)

    The returned DataFrame has the same columns as the input plus:
      - 'bar_idx'  : sequential range-bar index
      - 'n_source' : number of source bars merged into this range bar

    Note: we need to compute ATR on the *source* bars first.
    """
    df = df.copy()

    # compute ATR on source bars (used when not fixed)
    if not use_fixed:
        atr_s = calc_atr(df["high"], df["low"], df["close"], atr_length)
    else:
        atr_s = pd.Series(fixed_size, index=df.index)

    rows = []
    range_open  = df["open"].iloc[0]
    range_high  = df["high"].iloc[0]
    range_low   = df["low"].iloc[0]
    range_close = df["close"].iloc[0]
    range_vol   = df["volume"].iloc[0]
    bar_start_ts= df.index[0]
    n_src       = 1
    bar_idx     = 0

    for i in range(1, len(df)):
        row   = df.iloc[i]
        c_src = row["close"]
        h_src = row["high"]
        l_src = row["low"]
        v_src = row["volume"]
        ts    = df.index[i]

        range_high  = max(range_high, h_src)
        range_low   = min(range_low,  l_src)
        range_close = c_src
        range_vol  += v_src
        n_src      += 1

        range_size  = float(atr_s.iloc[i])
        if range_size <= 0:
            range_size = fixed_size

        if abs(c_src - range_open) >= range_size:
            rows.append({
                "timestamp": bar_start_ts,
                "open":      range_open,
                "high":      range_high,
                "low":       range_low,
                "close":     range_close,
                "volume":    range_vol,
                "bar_idx":   bar_idx,
                "n_source":  n_src,
            })
            bar_idx    += 1
            range_open  = c_src
            range_high  = h_src
            range_low   = l_src
            range_close = c_src
            range_vol   = 0.0
            bar_start_ts= ts
            n_src       = 0

    # flush last incomplete bar
    if n_src > 0:
        rows.append({
            "timestamp": bar_start_ts,
            "open":      range_open,
            "high":      range_high,
            "low":       range_low,
            "close":     range_close,
            "volume":    range_vol,
            "bar_idx":   bar_idx,
            "n_source":  n_src,
        })

    rng_df = pd.DataFrame(rows).set_index("timestamp")
    rng_df.sort_index(inplace=True)
    return rng_df
