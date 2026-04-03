#!/usr/bin/env python3
"""
optimizer.py — Hyperliquid Strategy Optimizer
Teste des combinaisons de paramètres sur les données OHLCV Hyperliquid,
sur plusieurs timeframes, et trouve les meilleures configs.

Score combiné : (win_rate × profit_factor) / max(max_drawdown_pct, 0.01)

Usage:
    python optimizer.py                         # full run (SOL + ETH, 6 TF, 150 combos)
    python optimizer.py --quick                 # test rapide (SOL, 1H, 10 combos)
    python optimizer.py --coins SOL --tfs 4H --combos 50
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from itertools import product
import urllib.request
import urllib.error

import numpy as np

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

WORKSPACE    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_JSON  = os.path.join(WORKSPACE, "optimization_results.json")
OUTPUT_REPORT = os.path.join(WORKSPACE, "optimization_report.txt")

HL_API_URL = "https://api.hyperliquid.xyz/info"

# Timeframes cibles et leur stratégie de téléchargement
# Hyperliquid supporte nativement: 1m, 15m, 30m, 1h, 2h, 4h, 1d
# 45m et 3h n'existent pas → on les agrège depuis des intervalles plus fins
TF_CONFIG = {
    #  TF     interval_HL  agg_mult  lookback_days
    "30M": ("30m", 1,  90),
    "45M": ("15m", 3,  52),   # 15m data disponible ~52j max
    "1H":  ("1h",  1,  90),
    "2H":  ("2h",  1,  90),
    "3H":  ("1h",  3,  90),
    "4H":  ("4h",  1,  90),
}
TIMEFRAMES = ["30M", "45M", "1H", "2H", "3H", "4H"]
COINS = ["SOL", "ETH"]

COMBOS_PER_TF_COIN = 150
INITIAL_CAPITAL    = 10_000.0
QTY_PCT            = 0.02    # 2% du capital par trade

PARAM_GRID = {
    "hma_fast":   [15, 20, 25],
    "hma_slow":   [40, 50, 60],
    "tp_mult":    [3.0, 4.0, 5.0],
    "adx_thresh": [20, 25, 30],
    "rsi_low":    [35, 40, 45],   # seuil bas RSI pour LONG
    "rsi_high":   [60, 65, 70],   # seuil haut RSI pour LONG
}

# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def _hl_post(payload: dict, retries: int = 3) -> list:
    """POST vers l'API Hyperliquid avec retry exponentiel."""
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                HL_API_URL, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            wait = 2 ** attempt
            print(f"  [WARN] API error (attempt {attempt+1}/{retries}): {e} — retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Hyperliquid API unreachable after {retries} attempts")


def fetch_candles(coin: str, interval: str, days: int) -> list:
    """
    Télécharge `days` jours de candles Hyperliquid à l'intervalle demandé.
    Gère la pagination : fenêtre glissante si > 5000 candles attendues.
    Retourne une liste de dicts {t, o, h, l, c, v} triée par t.
    """
    end_ts   = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    # Taille d'une fenêtre = 4500 × durée_candle_ms (légèrement sous la limite)
    interval_minutes = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
    }.get(interval, 60)
    window_ms = 4500 * interval_minutes * 60_000

    cursor  = start_ts
    candles = []
    batch_n = 0

    print(f"  [DATA] {coin} {interval} ({days}j) — téléchargement en cours…")

    while cursor < end_ts:
        batch_end = min(cursor + window_ms, end_ts)
        payload = {
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": cursor, "endTime": batch_end},
        }
        batch = _hl_post(payload)
        batch_n += 1

        if not batch:
            # Aucun résultat → avancer d'une fenêtre
            cursor = batch_end + interval_minutes * 60_000
            continue

        for c in batch:
            candles.append({
                "t": int(c["t"]), "o": float(c["o"]),
                "h": float(c["h"]), "l": float(c["l"]),
                "c": float(c["c"]), "v": float(c.get("v", 0)),
            })

        last_t = batch[-1]["t"]
        cursor = last_t + interval_minutes * 60_000

        ft = datetime.fromtimestamp(batch[0]["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        lt = datetime.fromtimestamp(last_t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"    batch {batch_n}: {len(batch)} candles [{ft} → {lt}]")

        time.sleep(0.25)   # rate-limiting poli

    # Dédupliquer et trier
    seen = {}
    for c in candles:
        seen[c["t"]] = c
    result = sorted(seen.values(), key=lambda x: x["t"])
    print(f"  [DATA] {coin} {interval}: {len(result):,} candles (total)")
    return result


def aggregate_candles(candles: list, mult: int) -> dict:
    """
    Agrège des candles par un facteur `mult` (ex. 15m × 3 = 45m).
    Retourne un dict {"open", "high", "low", "close", "volume"} de np.arrays.
    """
    if not candles or mult == 1:
        opens   = np.array([c["o"] for c in candles])
        highs   = np.array([c["h"] for c in candles])
        lows    = np.array([c["l"] for c in candles])
        closes  = np.array([c["c"] for c in candles])
        volumes = np.array([c["v"] for c in candles])
        return {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}

    buckets = {}
    for i, c in enumerate(candles):
        bucket = i // mult
        if bucket not in buckets:
            buckets[bucket] = {"o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
        else:
            b = buckets[bucket]
            b["h"] = max(b["h"], c["h"])
            b["l"] = min(b["l"], c["l"])
            b["c"] = c["c"]
            b["v"] += c["v"]

    sorted_b = [buckets[k] for k in sorted(buckets.keys())]
    return {
        "open":   np.array([b["o"] for b in sorted_b]),
        "high":   np.array([b["h"] for b in sorted_b]),
        "low":    np.array([b["l"] for b in sorted_b]),
        "close":  np.array([b["c"] for b in sorted_b]),
        "volume": np.array([b["v"] for b in sorted_b]),
    }


# ─────────────────────────────────────────────
# INDICATEURS
# ─────────────────────────────────────────────

def calc_wma(arr: np.ndarray, period: int) -> np.ndarray:
    """Weighted Moving Average."""
    n = len(arr)
    result = np.full(n, np.nan)
    weights = np.arange(1, period + 1, dtype=float)
    wsum = weights.sum()
    for i in range(period - 1, n):
        result[i] = np.dot(arr[i - period + 1: i + 1], weights) / wsum
    return result


def calc_hma(arr: np.ndarray, period: int) -> np.ndarray:
    """Hull Moving Average."""
    n2 = max(period // 2, 2)
    sq = max(int(period ** 0.5), 2)
    wma1 = calc_wma(arr, n2)
    wma2 = calc_wma(arr, period)
    return calc_wma(2 * wma1 - wma2, sq)


def calc_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI (Wilder's smoothing)."""
    n = len(closes)
    result = np.full(n, np.nan)
    if n < period + 2:
        return result
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = float(np.mean(gains[:period]))
    avg_l  = float(np.mean(losses[:period]))
    for i in range(period, n - 1):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l == 0:
            result[i + 1] = 100.0
        else:
            result[i + 1] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return result


def calc_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
             period: int = 14) -> tuple:
    """ADX + DI+ + DI- (Wilder's smoothing)."""
    n = len(closes)
    adx_arr  = np.full(n, np.nan)
    plus_di  = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    if n < period * 2 + 2:
        return adx_arr, plus_di, minus_di

    tr_arr   = np.zeros(n)
    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        hl  = highs[i] - lows[i]
        hpc = abs(highs[i]  - closes[i - 1])
        lpc = abs(lows[i]   - closes[i - 1])
        tr_arr[i] = max(hl, hpc, lpc)
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i]  = up   if (up > down   and up > 0)   else 0.0
        minus_dm[i] = down if (down > up   and down > 0) else 0.0

    smtr  = float(np.sum(tr_arr[1:period + 1]))
    sm_p  = float(np.sum(plus_dm[1:period + 1]))
    sm_m  = float(np.sum(minus_dm[1:period + 1]))

    adx_buf = []
    for i in range(period, n):
        if i > period:
            smtr = smtr - smtr / period + tr_arr[i]
            sm_p = sm_p - sm_p / period + plus_dm[i]
            sm_m = sm_m - sm_m / period + minus_dm[i]
        pdi = 100 * sm_p / smtr if smtr else 0.0
        mdi = 100 * sm_m / smtr if smtr else 0.0
        plus_di[i]  = pdi
        minus_di[i] = mdi
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0
        adx_buf.append(dx)
        if len(adx_buf) >= period:
            if len(adx_buf) == period:
                adx_arr[i] = float(np.mean(adx_buf))
            else:
                adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx) / period

    return adx_arr, plus_di, minus_di


def calc_explosive(highs: np.ndarray, lows: np.ndarray,
                   closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Détecte les bougies à volatilité explosive (ATR > 2× ATR moyen)."""
    n = len(closes)
    atr = np.zeros(n)
    for i in range(1, n):
        atr[i] = max(
            highs[i] - lows[i],
            abs(highs[i]  - closes[i - 1]),
            abs(lows[i]   - closes[i - 1]),
        )
    explosive = np.zeros(n, dtype=bool)
    for i in range(period, n):
        avg = np.mean(atr[i - period:i])
        explosive[i] = atr[i] > 2 * avg if avg else False
    return explosive


# ─────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────

def run_backtest(ohlcv: dict, params: dict) -> dict:
    """
    Simule la stratégie HMA/ADX/RSI sur les données OHLCV.
    Retourne les métriques ou {} si pas assez de trades.
    """
    opens  = ohlcv["open"]
    highs  = ohlcv["high"]
    lows   = ohlcv["low"]
    closes = ohlcv["close"]
    n = len(closes)

    hma_fast   = params["hma_fast"]
    hma_slow   = params["hma_slow"]
    tp_mult    = params["tp_mult"]
    adx_thresh = params["adx_thresh"]
    rsi_low    = params["rsi_low"]
    rsi_high   = params["rsi_high"]

    warmup = max(hma_slow + 20, 70)
    if n < warmup + 20:
        return {}

    # Calcul des indicateurs
    hma_f     = calc_hma(closes, hma_fast)
    hma_s     = calc_hma(closes, hma_slow)
    rsi       = calc_rsi(closes, 14)
    adx, pdi, mdi = calc_adx(highs, lows, closes, 14)
    explosive = calc_explosive(highs, lows, closes, 14)

    # Simulation barre par barre
    capital = INITIAL_CAPITAL
    peak    = capital
    max_dd  = 0.0
    trades  = []

    in_long  = False
    in_short = False
    entry_p  = sl = tp = qty = 0.0
    LOOKBACK_SL = 15

    for i in range(warmup, n):
        price = closes[i]

        # ── Vérifier clôture position ──
        if in_long:
            hit_sl = lows[i]  <= sl
            hit_tp = highs[i] >= tp
            if hit_sl or hit_tp:
                exit_p = (tp if (hit_tp and not hit_sl) else sl)
                pnl = (exit_p - entry_p) * qty
                capital += pnl
                trades.append(pnl)
                in_long = False
                if capital > peak:
                    peak = capital
                dd = (peak - capital) / peak * 100
                if dd > max_dd:
                    max_dd = dd
            continue  # une position à la fois → skip entrée si déjà fermée

        if in_short:
            hit_sl = highs[i] >= sl
            hit_tp = lows[i]  <= tp
            if hit_sl or hit_tp:
                exit_p = (tp if (hit_tp and not hit_sl) else sl)
                pnl = (entry_p - exit_p) * qty
                capital += pnl
                trades.append(pnl)
                in_short = False
                if capital > peak:
                    peak = capital
                dd = (peak - capital) / peak * 100
                if dd > max_dd:
                    max_dd = dd
            continue

        # ── Signaux d'entrée ──
        if np.isnan(adx[i]) or np.isnan(hma_f[i]) or np.isnan(hma_s[i]) or np.isnan(rsi[i]):
            continue

        adx_ok     = adx[i] > adx_thresh and not explosive[i]
        bull_trend = pdi[i] > mdi[i] and price > hma_s[i]
        bear_trend = mdi[i] > pdi[i] and price < hma_s[i]

        # LONG : pullback sur HMA fast + RSI dans la zone
        pb_long = (lows[i] <= hma_f[i] and price > hma_f[i]
                   and rsi_low < rsi[i] < rsi_high)

        # SHORT : pullback sur HMA fast (symétrique)
        rsi_s_low  = 100 - rsi_high
        rsi_s_high = 100 - rsi_low
        pb_short = (highs[i] >= hma_f[i] and price < hma_f[i]
                    and rsi_s_low < rsi[i] < rsi_s_high)

        if adx_ok and bull_trend and pb_long:
            look = max(0, i - LOOKBACK_SL)
            sl_raw = float(np.min(lows[look:i + 1])) * 0.995
            sl = max(sl_raw, price * 0.980)
            risk = price - sl
            if risk <= 0:
                continue
            tp   = price + risk * tp_mult
            qty  = (capital * QTY_PCT) / risk
            entry_p = price
            in_long = True

        elif adx_ok and bear_trend and pb_short:
            look = max(0, i - LOOKBACK_SL)
            sl_raw = float(np.max(highs[look:i + 1])) * 1.005
            sl = min(sl_raw, price * 1.020)
            risk = sl - price
            if risk <= 0:
                continue
            tp   = price - risk * tp_mult
            qty  = (capital * QTY_PCT) / risk
            entry_p = price
            in_short = True

    # ── Métriques ──
    if len(trades) < 5:
        return {}

    wins         = sum(1 for p in trades if p > 0)
    win_rate     = wins / len(trades)
    gross_profit = sum(p for p in trades if p > 0)
    gross_loss   = abs(sum(p for p in trades if p < 0))
    pf           = gross_profit / gross_loss if gross_loss > 0 else gross_profit
    total_ret    = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    score        = (win_rate * pf) / max(max_dd, 0.01)

    return {
        "win_rate":         round(win_rate, 4),
        "profit_factor":    round(pf, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "total_return_pct": round(total_ret, 3),
        "num_trades":       len(trades),
        "score":            round(score, 4),
    }


# ─────────────────────────────────────────────
# OPTIMISATION
# ─────────────────────────────────────────────

def sample_combos(grid: dict, n: int, seed: int = 42) -> list:
    """Retourne n combos aléatoires du param grid."""
    all_combos = [dict(zip(grid.keys(), v)) for v in product(*grid.values())]
    random.seed(seed)
    return random.sample(all_combos, min(n, len(all_combos)))


def optimize_tf_coin(raw_candles: list, tf: str, coin: str, combos: list) -> list:
    """
    Lance len(combos) backtests pour un TF/coin.
    Retourne les résultats triés par score desc.
    """
    _, agg_mult, _ = TF_CONFIG[tf]
    ohlcv = aggregate_candles(raw_candles, agg_mult)
    n_bars = len(ohlcv["close"])

    if n_bars < 80:
        print(f"  [WARN] {coin} {tf}: trop peu de bougies ({n_bars}) après agrégation")
        return []

    print(f"  [INFO] {coin} {tf}: {n_bars} bougies — {len(combos)} backtests")

    results = []
    for idx, params in enumerate(combos, 1):
        metrics = run_backtest(ohlcv, params)
        if not metrics:
            continue

        # Log de progression
        if idx % 25 == 0 or idx == len(combos):
            print(
                f"  [TF={tf} | {coin} | combo {idx}/{len(combos)}] "
                f"WR={metrics['win_rate']*100:.1f}% "
                f"DD={metrics['max_drawdown_pct']:.1f}% "
                f"PF={metrics['profit_factor']:.2f} "
                f"Score={metrics['score']:.2f}"
            )

        results.append({"timeframe": tf, "coin": coin, "params": params, **metrics})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ─────────────────────────────────────────────
# AFFICHAGE / RAPPORT
# ─────────────────────────────────────────────

def print_top3(all_results: list, coins: list, tfs: list):
    print("\n" + "═" * 72)
    print("  TOP 3 PAR TIMEFRAME / COIN")
    print("═" * 72)
    for coin in coins:
        for tf in tfs:
            subset = [r for r in all_results if r["coin"] == coin and r["timeframe"] == tf]
            if not subset:
                continue
            print(f"\n  {coin} — {tf}  ({len(subset)} résultats valides)")
            for rank, r in enumerate(subset[:3], 1):
                p = r["params"]
                print(
                    f"    #{rank}  hma_f={p['hma_fast']} hma_s={p['hma_slow']} "
                    f"tp={p['tp_mult']} adx={p['adx_thresh']} "
                    f"rsi=[{p['rsi_low']},{p['rsi_high']}]  "
                    f"Score={r['score']:.2f}  WR={r['win_rate']*100:.1f}%  "
                    f"DD={r['max_drawdown_pct']:.1f}%  PF={r['profit_factor']:.2f}  "
                    f"Trades={r['num_trades']}"
                )
    print("═" * 72)


def generate_report(all_results: list, run_date: str, coins: list, tfs: list) -> str:
    lines = []
    for coin in coins:
        lines.append(f"=== RAPPORT OPTIMISATION — {coin} ===")
        lines.append(f"Date : {run_date}")
        lines.append("")
        coin_res = [r for r in all_results if r["coin"] == coin]
        for tf in tfs:
            tf_res = [r for r in coin_res if r["timeframe"] == tf]
            if not tf_res:
                lines.append(f"TF {tf} — Aucun résultat")
                lines.append("")
                continue
            best = tf_res[0]
            p = best["params"]
            lines.append(f"TF {tf} — Best combo :")
            lines.append(
                f"  hma_fast={p['hma_fast']}, hma_slow={p['hma_slow']}, "
                f"tp_mult={p['tp_mult']}, adx_thresh={p['adx_thresh']}, "
                f"rsi_low={p['rsi_low']}, rsi_high={p['rsi_high']}"
            )
            lines.append(
                f"  WR={best['win_rate']*100:.1f}%, DD={best['max_drawdown_pct']:.1f}%, "
                f"PF={best['profit_factor']:.2f}, Score={best['score']:.2f}, "
                f"Trades={best['num_trades']}, Return={best['total_return_pct']:.1f}%"
            )
            lines.append("  Top 3 :")
            for rank, r in enumerate(tf_res[:3], 1):
                p2 = r["params"]
                lines.append(
                    f"    #{rank}  hma_fast={p2['hma_fast']}, hma_slow={p2['hma_slow']}, "
                    f"tp_mult={p2['tp_mult']}, adx_thresh={p2['adx_thresh']}  "
                    f"→ Score={r['score']:.2f}  WR={r['win_rate']*100:.1f}%  "
                    f"DD={r['max_drawdown_pct']:.1f}%"
                )
            lines.append("")
        lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Strategy Optimizer")
    parser.add_argument("--quick",  action="store_true", help="Test rapide : SOL, 1H, 10 combos")
    parser.add_argument("--coins",  nargs="+", default=None)
    parser.add_argument("--tfs",    nargs="+", default=None)
    parser.add_argument("--combos", type=int,  default=None)
    args = parser.parse_args()

    coins    = ["SOL"] if args.quick else (args.coins  or COINS)
    tfs      = ["1H"]  if args.quick else (args.tfs    or TIMEFRAMES)
    combos_n = 10      if args.quick else (args.combos or COMBOS_PER_TF_COIN)

    run_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'═'*72}")
    print(f"  HYPERLIQUID STRATEGY OPTIMIZER — {run_date}")
    print(f"  Coins : {coins}  |  TF : {tfs}  |  Combos/TF : {combos_n}")
    print(f"{'═'*72}\n")

    t_start  = time.time()
    combos   = sample_combos(PARAM_GRID, combos_n)
    all_results = []

    for coin in coins:
        print(f"\n{'─'*60}  COIN : {coin}  {'─'*10}")

        # Regrouper les TF par intervalle source pour éviter les downloads redondants
        # ex: 1H et 3H utilisent tous les deux "1h"
        intervals_needed = {}
        for tf in tfs:
            interval, _, days = TF_CONFIG[tf]
            key = (interval, days)
            if key not in intervals_needed:
                intervals_needed[key] = []
            intervals_needed[key].append(tf)

        cached_raw = {}   # (interval, days) → list of candles

        for (interval, days), tf_list in intervals_needed.items():
            try:
                raw = fetch_candles(coin, interval, days)
            except RuntimeError as e:
                print(f"  [ERROR] {e}")
                continue
            cached_raw[(interval, days)] = raw

            for tf in tf_list:
                print(f"\n  ── Timeframe {tf} ({interval} × {TF_CONFIG[tf][1]}) ──")
                tf_res = optimize_tf_coin(raw, tf, coin, combos)
                all_results.extend(tf_res)
                if tf_res:
                    best = tf_res[0]
                    print(
                        f"  ✅ {coin} {tf}: Score={best['score']:.2f}  "
                        f"WR={best['win_rate']*100:.1f}%  "
                        f"DD={best['max_drawdown_pct']:.1f}%  "
                        f"PF={best['profit_factor']:.2f}  "
                        f"Trades={best['num_trades']}"
                    )

    elapsed = time.time() - t_start
    print(f"\n  Durée totale : {elapsed:.1f}s ({elapsed/60:.1f} min)")

    if not all_results:
        print("\n[ERROR] Aucun résultat. Vérifier la connexion et les données.")
        sys.exit(1)

    # Affichage console
    print_top3(all_results, coins, tfs)

    # Sauvegarde JSON
    os.makedirs(WORKSPACE, exist_ok=True)
    with open(OUTPUT_JSON, "w") as f:
        json.dump({
            "run_date": run_date,
            "config": {"coins": coins, "timeframes": tfs, "combos": combos_n},
            "results": all_results,
        }, f, indent=2)
    print(f"\n  [OUT] JSON  → {OUTPUT_JSON}")

    # Rapport texte
    report = generate_report(all_results, run_date, coins, tfs)
    with open(OUTPUT_REPORT, "w") as f:
        f.write(report)
    print(f"  [OUT] Texte → {OUTPUT_REPORT}")
    print("\n  ✓ Optimisation terminée.\n")


if __name__ == "__main__":
    main()
