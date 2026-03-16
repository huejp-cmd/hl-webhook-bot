"""
data_fetcher.py — Fetch OHLCV data from Binance public API and cache to CSV.

Symbols    : SOLUSDT, ETHUSDT, BTCUSDT
Native TFs : 15m, 30m, 1h, 4h  (fetched from Binance)
Derived TFs : 45m (3×15m), 2h (2×1h), 3h (3×1h)  — built by resampling
History    : 500+ days
"""

import time
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = "https://api.binance.com/api/v3/klines"
DATA_DIR = Path(__file__).parent / "data"
SYMBOLS  = ["SOLUSDT", "ETHUSDT", "BTCUSDT"]

# Timeframes actually fetched from Binance
NATIVE_TFS = ["15m", "30m", "1h", "4h"]

# Derived timeframes: (name, source_tf, pandas_resample_rule)
#   45m  ← 3 × 15m   →  "45min"
#   2h   ← 2 × 1h    →  "2h"
#   3h   ← 3 × 1h    →  "3h"
DERIVED_TFS = [
    ("45m", "15m", "45min"),
    ("2h",  "1h",  "2h"),
    ("3h",  "1h",  "3h"),
]

# All timeframes exposed to the rest of the system
ALL_TIMEFRAMES = ["15m", "30m", "45m", "1h", "2h", "3h", "4h"]

DAYS_BACK = 550   # slightly over 500 to guarantee enough warm-up bars
LIMIT     = 1000  # max klines per Binance request

# Binance interval string → milliseconds
TF_MS = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}


def _binance_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Fetch all klines between start_ms and end_ms (paginated)."""
    rows = []
    current = start_ms
    while current < end_ms:
        params = {
            "symbol":    symbol,
            "interval":  interval,
            "startTime": current,
            "endTime":   end_ms,
            "limit":     LIMIT,
        }
        resp = requests.get(BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        rows.extend(data)
        # Advance past the last returned candle
        last_open_time = data[-1][0]
        current = last_open_time + TF_MS[interval]
        if len(data) < LIMIT:
            break
        time.sleep(0.12)   # gentle rate-limit
    return rows


def _to_dataframe(raw: list) -> pd.DataFrame:
    """Convert raw Binance klines list to a typed DataFrame."""
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "n_trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
    df.rename(columns={"open_time": "timestamp"}, inplace=True)
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)
    df = df[~df.index.duplicated(keep="last")]
    return df


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Resample a standard OHLCV DataFrame to a coarser timeframe.

    Parameters
    ----------
    df   : DataFrame with DatetimeIndex (UTC) and columns open/high/low/close/volume
    rule : pandas offset alias, e.g. "45min", "2h", "3h"

    Returns
    -------
    Resampled DataFrame (same column layout, incomplete last candle dropped).
    """
    agg = {
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }
    # closed="left" / label="left" keeps the open-time as the bar label
    resampled = (
        df.resample(rule, closed="left", label="left")
        .agg(agg)
        .dropna(subset=["open", "close"])  # drop empty buckets
    )
    # Drop the last candle if it looks incomplete (volume == 0 edge case)
    if len(resampled) > 1 and resampled["volume"].iloc[-1] == 0:
        resampled = resampled.iloc[:-1]
    return resampled


def _fetch_native(symbol: str, timeframe: str, days: int = DAYS_BACK,
                  force_refresh: bool = False) -> pd.DataFrame:
    """
    Return OHLCV DataFrame for a *native* Binance timeframe.
    Loads from CSV cache if available and recent; otherwise fetches from API.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = DATA_DIR / f"{symbol}_{timeframe}.csv"

    if cache_path.exists() and not force_refresh:
        df = pd.read_csv(cache_path, index_col="timestamp", parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        age_s = (datetime.now(tz=timezone.utc) - df.index[-1]).total_seconds()
        if age_s < TF_MS[timeframe] / 1000:
            print(f"  [cache] {symbol} {timeframe}: {len(df)} rows (fresh)")
            return df
        # incremental update
        start_ms = int(df.index[-1].timestamp() * 1000) + TF_MS[timeframe]
        print(f"  [cache] {symbol} {timeframe}: {len(df)} rows → updating…")
        end_ms   = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        new_rows = _binance_klines(symbol, timeframe, start_ms, end_ms)
        if new_rows:
            new_df = _to_dataframe(new_rows)
            df     = pd.concat([df, new_df])
            df     = df[~df.index.duplicated(keep="last")]
            df.sort_index(inplace=True)
        df.to_csv(cache_path)
        print(f"  [cache] updated → {len(df)} rows")
        return df

    # full fetch
    now_ms   = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ms = now_ms - days * 86_400_000
    print(f"  [fetch] {symbol} {timeframe}: downloading {days} days…")
    raw = _binance_klines(symbol, timeframe, start_ms, now_ms)
    df  = _to_dataframe(raw)
    df.to_csv(cache_path)
    print(f"  [fetch] saved {len(df)} rows → {cache_path.name}")
    return df


def fetch(symbol: str, timeframe: str, days: int = DAYS_BACK,
          force_refresh: bool = False) -> pd.DataFrame:
    """
    Return OHLCV DataFrame for symbol/timeframe.

    For native Binance timeframes (15m, 30m, 1h, 4h) → fetches directly.
    For derived timeframes (45m, 2h, 3h) → resamples from the source native TF
    and caches the result to CSV.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Check if it's a derived timeframe
    derived_map = {name: (src, rule) for name, src, rule in DERIVED_TFS}
    if timeframe in derived_map:
        src_tf, rule = derived_map[timeframe]
        cache_path = DATA_DIR / f"{symbol}_{timeframe}.csv"

        # Get (possibly updated) source data first
        src_df = _fetch_native(symbol, src_tf, days=days, force_refresh=force_refresh)

        # Re-build derived cache (always, it's fast)
        df = resample_ohlcv(src_df, rule)
        df.to_csv(cache_path)
        print(f"  [resample] {symbol} {timeframe} ({rule} from {src_tf}): {len(df)} rows")
        return df

    # Native timeframe
    return _fetch_native(symbol, timeframe, days=days, force_refresh=force_refresh)


def fetch_all(symbols=None, timeframes=None,
              days=DAYS_BACK, force_refresh=False) -> dict:
    """
    Fetch all symbol/timeframe combinations.

    Fetches native TFs first (to populate cache), then builds derived TFs.
    Returns nested dict: result[symbol][timeframe] = DataFrame.
    """
    if symbols    is None: symbols    = SYMBOLS
    if timeframes is None: timeframes = ALL_TIMEFRAMES

    result = {}
    for sym in symbols:
        result[sym] = {}
        # native first
        for tf in NATIVE_TFS:
            if tf in timeframes:
                result[sym][tf] = _fetch_native(sym, tf, days=days,
                                                force_refresh=force_refresh)
        # derived second
        for name, src_tf, rule in DERIVED_TFS:
            if name in timeframes:
                src_df = result[sym].get(src_tf) or _fetch_native(
                    sym, src_tf, days=days, force_refresh=force_refresh)
                df = resample_ohlcv(src_df, rule)
                cache_path = DATA_DIR / f"{sym}_{name}.csv"
                df.to_csv(cache_path)
                print(f"  [resample] {sym} {name}: {len(df)} rows")
                result[sym][name] = df
        # any remaining native TFs not yet fetched
        for tf in timeframes:
            if tf not in result[sym]:
                result[sym][tf] = fetch(sym, tf, days=days,
                                        force_refresh=force_refresh)
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "SOLUSDT"
    tf  = sys.argv[2] if len(sys.argv) > 2 else "30m"
    df  = fetch(sym, tf)
    print(df.tail(5))
    print(f"\nRows: {len(df)}  |  From: {df.index[0]}  →  {df.index[-1]}")
