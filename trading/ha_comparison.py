#!/usr/bin/env python3
"""
ha_comparison.py — Comparaison backtest : sans filtre HA 1H vs avec filtre HA 1H
Utilise les paramètres exacts de JP v7 SOL 45M et ETH 30M.

Résultats sur 3 fenêtres : 30J, 90J, 1Y
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

import numpy as np

# ─────────────────────────────────────────────────────
# PARAMÈTRES V7
# ─────────────────────────────────────────────────────
CONFIGS = {
    "SOL": {
        "tf_interval": "15m",  # 15m × 3 = 45M
        "tf_agg":       3,
        "tf_days":      60,    # max dispo pour 15m
        "1h_days":      365,
        "hma_fast":     20,
        "hma_slow":     50,
        "tp_mult":      5.0,
        "adx_thresh":   20,
        "rsi_low":      35,
        "rsi_high":     65,
    },
    "ETH": {
        "tf_interval": "30m",
        "tf_agg":       1,
        "tf_days":      365,
        "1h_days":      365,
        "hma_fast":     25,
        "hma_slow":     40,
        "tp_mult":      5.0,
        "adx_thresh":   25,
        "rsi_low":      35,
        "rsi_high":     65,
    },
}

INITIAL_CAPITAL = 10_000.0
QTY_PCT         = 0.02   # 2% du capital par trade
HL_API_URL      = "https://api.hyperliquid.xyz/info"

WINDOWS = {
    "30J":  30,
    "90J":  90,
    "1an": 365,
}

# ─────────────────────────────────────────────────────
# API HYPERLIQUID
# ─────────────────────────────────────────────────────
def hl_post(payload, retries=3):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(HL_API_URL, data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            wait = 2 ** attempt
            print(f"  [WARN] API {attempt+1}/{retries}: {e} — retry {wait}s")
            time.sleep(wait)
    raise RuntimeError("Hyperliquid API unreachable")


def fetch_candles_with_ts(coin, interval, days):
    """Télécharge les candles avec timestamps. Retourne liste de dicts {t,o,h,l,c,v}."""
    interval_min = {"1m":1,"5m":5,"15m":15,"30m":30,"1h":60,"2h":120,"4h":240}.get(interval, 60)
    end_ts   = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    window_ms = 4500 * interval_min * 60_000

    cursor  = start_ts
    candles = {}
    batch_n = 0

    print(f"  [DATA] {coin} {interval} ({days}j)…", end="", flush=True)
    while cursor < end_ts:
        batch_end = min(cursor + window_ms, end_ts)
        payload = {"type": "candleSnapshot",
                   "req": {"coin": coin, "interval": interval,
                           "startTime": cursor, "endTime": batch_end}}
        batch = hl_post(payload)
        batch_n += 1
        if batch:
            for c in batch:
                t = int(c["t"])
                candles[t] = {"t": t, "o": float(c["o"]), "h": float(c["h"]),
                              "l": float(c["l"]), "c": float(c["c"]), "v": float(c.get("v", 0))}
            cursor = batch[-1]["t"] + interval_min * 60_000
        else:
            cursor = batch_end + interval_min * 60_000
        time.sleep(0.2)

    result = sorted(candles.values(), key=lambda x: x["t"])
    print(f" {len(result):,} candles")
    return result


# ─────────────────────────────────────────────────────
# AGRÉGATION 45M / TIMEFRAME CUSTOM
# ─────────────────────────────────────────────────────
def aggregate_with_ts(candles, mult):
    """Agrège les candles par `mult` et conserve le timestamp de la première bougie."""
    if mult == 1:
        return candles
    buckets = {}
    for i, c in enumerate(candles):
        b = i // mult
        if b not in buckets:
            buckets[b] = {"t": c["t"], "o": c["o"], "h": c["h"],
                          "l": c["l"], "c": c["c"], "v": c["v"]}
        else:
            bk = buckets[b]
            bk["h"] = max(bk["h"], c["h"])
            bk["l"] = min(bk["l"], c["l"])
            bk["c"] = c["c"]
            bk["v"] += c["v"]
    return [buckets[k] for k in sorted(buckets.keys())]


# ─────────────────────────────────────────────────────
# INDICATEURS
# ─────────────────────────────────────────────────────
def wma(arr, p):
    n = len(arr)
    res = np.full(n, np.nan)
    w = np.arange(1, p + 1, dtype=float)
    ws = w.sum()
    for i in range(p - 1, n):
        res[i] = np.dot(arr[i - p + 1:i + 1], w) / ws
    return res


def hma(arr, p):
    sq = max(int(p ** 0.5), 2)
    return wma(2 * wma(arr, max(p // 2, 2)) - wma(arr, p), sq)


def rsi(closes, p=14):
    n = len(closes)
    res = np.full(n, np.nan)
    if n < p + 2:
        return res
    d = np.diff(closes)
    ag = float(np.mean(np.where(d > 0, d, 0)[:p]))
    al = float(np.mean(np.where(d < 0, -d, 0)[:p]))
    for i in range(p, n - 1):
        ag = (ag * (p - 1) + max(d[i], 0)) / p
        al = (al * (p - 1) + max(-d[i], 0)) / p
        res[i + 1] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return res


def adx_dmi(highs, lows, closes, p=14):
    n = len(closes)
    adx_a = np.full(n, np.nan)
    pdi_a = np.full(n, np.nan)
    mdi_a = np.full(n, np.nan)
    if n < p * 2 + 2:
        return adx_a, pdi_a, mdi_a
    tr  = np.zeros(n)
    pdm = np.zeros(n)
    mdm = np.zeros(n)
    for i in range(1, n):
        tr[i]  = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        up   = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        pdm[i] = up   if up > down   and up > 0   else 0.0
        mdm[i] = down if down > up   and down > 0 else 0.0
    smtr = float(np.sum(tr[1:p+1]))
    smp  = float(np.sum(pdm[1:p+1]))
    smm  = float(np.sum(mdm[1:p+1]))
    buf  = []
    for i in range(p, n):
        if i > p:
            smtr = smtr - smtr / p + tr[i]
            smp  = smp  - smp  / p + pdm[i]
            smm  = smm  - smm  / p + mdm[i]
        pd  = 100 * smp  / smtr if smtr else 0.0
        md  = 100 * smm  / smtr if smtr else 0.0
        pdi_a[i] = pd
        mdi_a[i] = md
        dx = 100 * abs(pd - md) / (pd + md) if (pd + md) else 0.0
        buf.append(dx)
        if len(buf) >= p:
            adx_a[i] = float(np.mean(buf)) if len(buf) == p else (adx_a[i-1] * (p-1) + dx) / p
    return adx_a, pdi_a, mdi_a


# ─────────────────────────────────────────────────────
# HEIKIN ASHI 1H
# ─────────────────────────────────────────────────────
def calc_ha_bull(candles_1h):
    """
    Calcule les bougies Heikin Ashi sur les candles 1H.
    Retourne dict {timestamp_ms: True/False} — True = HA haussier.
    """
    n = len(candles_1h)
    ha_close = np.array([(c["o"] + c["h"] + c["l"] + c["c"]) / 4 for c in candles_1h])
    ha_open  = np.zeros(n)
    ha_open[0] = (candles_1h[0]["o"] + candles_1h[0]["c"]) / 2
    for i in range(1, n):
        ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2

    # Map : timestamp 1H → bullish (True) ou bearish (False)
    result = {}
    for i in range(n):
        result[candles_1h[i]["t"]] = ha_close[i] > ha_open[i]
    return result


def get_ha_state(ts_ms, ha_map_1h):
    """
    Pour un timestamp donné (45M/30M), retourne l'état HA de la 1H bar courante.
    La 1H bar correspondante = floor(ts_ms / 3600000) * 3600000.
    On prend la dernière 1H bar FERMÉE (ts 1H ≤ ts courant - 1h).
    """
    bar_1h = (ts_ms // 3_600_000) * 3_600_000
    # Utiliser la barre 1H précédente (fermée) pour éviter le lookahead
    prev_1h = bar_1h - 3_600_000
    # Chercher la valeur la plus récente disponible ≤ prev_1h
    candidates = [t for t in ha_map_1h if t <= prev_1h]
    if not candidates:
        return None  # pas encore de donnée 1H
    return ha_map_1h[max(candidates)]


# ─────────────────────────────────────────────────────
# BACKTEST ENGINE (avec option filtre HA)
# ─────────────────────────────────────────────────────
def run_backtest(candles, params, ha_map_1h=None, use_ha_filter=False):
    """
    Simule la stratégie sur `candles` (avec timestamps).
    use_ha_filter=True : filtre les signaux selon HA 1H.
    Retourne dict de métriques ou None si < 5 trades.
    """
    n = len(candles)
    closes = np.array([c["c"] for c in candles])
    highs  = np.array([c["h"] for c in candles])
    lows   = np.array([c["l"] for c in candles])
    timestamps = [c["t"] for c in candles]

    hma_f  = hma(closes, params["hma_fast"])
    hma_s  = hma(closes, params["hma_slow"])
    rsi_v  = rsi(closes, 14)
    adx_v, pdi_v, mdi_v = adx_dmi(highs, lows, closes, 14)

    warmup = max(params["hma_slow"] + 20, 70)
    capital = INITIAL_CAPITAL
    peak    = capital
    max_dd  = 0.0
    trades  = []

    in_long  = False
    in_short = False
    entry_p  = sl = tp = qty = 0.0

    for i in range(warmup, n):
        price = closes[i]
        ts    = timestamps[i]

        # Clôture positions ouvertes
        if in_long:
            if lows[i] <= sl or highs[i] >= tp:
                exit_p = tp if (highs[i] >= tp and lows[i] > sl) else sl
                pnl = (exit_p - entry_p) * qty
                capital += pnl
                trades.append(pnl)
                in_long = False
                peak = max(peak, capital)
                max_dd = max(max_dd, (peak - capital) / peak * 100)
            continue

        if in_short:
            if highs[i] >= sl or lows[i] <= tp:
                exit_p = tp if (lows[i] <= tp and highs[i] < sl) else sl
                pnl = (entry_p - exit_p) * qty
                capital += pnl
                trades.append(pnl)
                in_short = False
                peak = max(peak, capital)
                max_dd = max(max_dd, (peak - capital) / peak * 100)
            continue

        # Indicateurs valides ?
        if any(np.isnan(v) for v in [adx_v[i], hma_f[i], hma_s[i], rsi_v[i]]):
            continue

        adx_ok     = adx_v[i] > params["adx_thresh"]
        bull_trend = pdi_v[i] > mdi_v[i] and price > hma_s[i]
        bear_trend = mdi_v[i] > pdi_v[i] and price < hma_s[i]

        pb_long  = lows[i] <= hma_f[i] and price > hma_f[i] \
                   and params["rsi_low"] < rsi_v[i] < params["rsi_high"]
        pb_short = highs[i] >= hma_f[i] and price < hma_f[i] \
                   and (100 - params["rsi_high"]) < rsi_v[i] < (100 - params["rsi_low"])

        # Filtre HA 1H
        ha_bull = ha_bear = True  # sans filtre, tout est autorisé
        if use_ha_filter and ha_map_1h is not None:
            ha_state = get_ha_state(ts, ha_map_1h)
            if ha_state is None:
                continue
            ha_bull = ha_state      # HA 1H haussier → LONG ok
            ha_bear = not ha_state  # HA 1H baissier → SHORT ok

        # Entrée LONG
        if adx_ok and bull_trend and pb_long and ha_bull:
            look = max(0, i - 15)
            sl_raw = float(np.min(lows[look:i+1])) * 0.995
            sl = max(sl_raw, price * 0.980)
            risk = price - sl
            if risk <= 0:
                continue
            tp      = price + risk * params["tp_mult"]
            qty     = (capital * QTY_PCT) / risk
            entry_p = price
            in_long = True

        # Entrée SHORT
        elif adx_ok and bear_trend and pb_short and ha_bear:
            look = max(0, i - 15)
            sl_raw = float(np.max(highs[look:i+1])) * 1.005
            sl = min(sl_raw, price * 1.020)
            risk = sl - price
            if risk <= 0:
                continue
            tp      = price - risk * params["tp_mult"]
            qty     = (capital * QTY_PCT) / risk
            entry_p = price
            in_short = True

    if len(trades) < 5:
        return None

    wins = sum(1 for p in trades if p > 0)
    gp   = sum(p for p in trades if p > 0)
    gl   = abs(sum(p for p in trades if p < 0))
    ret  = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    pf   = gp / gl if gl > 0 else gp

    return {
        "trades":    len(trades),
        "win_rate":  round(wins / len(trades) * 100, 1),
        "pf":        round(pf, 2),
        "dd":        round(max_dd, 1),
        "return":    round(ret, 1),
    }


# ─────────────────────────────────────────────────────
# FENÊTRES TEMPORELLES
# ─────────────────────────────────────────────────────
def slice_window(candles, days):
    """Retourne les candles des `days` derniers jours."""
    cutoff_ms = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    return [c for c in candles if c["t"] >= cutoff_ms]


# ─────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  COMPARAISON BACKTEST : Sans HA 1H vs Avec HA 1H")
    print("  Bugfix trades à 0 appliqué dans les deux cas")
    print("=" * 60)

    for coin, cfg in CONFIGS.items():
        print(f"\n{'─'*60}")
        print(f"  {coin} — TF: {cfg['tf_interval']}{'×'+str(cfg['tf_agg']) if cfg['tf_agg']>1 else ''}")
        print(f"  Params: HMA {cfg['hma_fast']}/{cfg['hma_slow']}, TP×{cfg['tp_mult']}, ADX>{cfg['adx_thresh']}, RSI {cfg['rsi_low']}-{cfg['rsi_high']}")
        print(f"{'─'*60}")

        # Télécharger données stratégie (45M ou 30M)
        raw = fetch_candles_with_ts(coin, cfg["tf_interval"], cfg["tf_days"])
        candles = aggregate_with_ts(raw, cfg["tf_agg"])
        print(f"  → {len(candles)} bougies après agrégation")

        # Télécharger données 1H pour HA
        raw_1h = fetch_candles_with_ts(coin, "1h", cfg["1h_days"])
        ha_map = calc_ha_bull(raw_1h)
        ha_bull_count = sum(1 for v in ha_map.values() if v)
        ha_bear_count = sum(1 for v in ha_map.values() if not v)
        print(f"  HA 1H : {ha_bull_count} bougies haussières / {ha_bear_count} baissières sur {len(ha_map)} total")

        params = {k: cfg[k] for k in ["hma_fast","hma_slow","tp_mult","adx_thresh","rsi_low","rsi_high"]}

        print(f"\n  {'Fenêtre':<8}  {'Version':<20}  {'Trades':>6}  {'WR%':>6}  {'PF':>5}  {'DD%':>6}  {'Ret%':>8}")
        print(f"  {'-'*8}  {'-'*20}  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*8}")

        for win_name, win_days in WINDOWS.items():
            sliced = slice_window(candles, win_days)
            if len(sliced) < 100:
                print(f"  {win_name:<8}  ⚠ Pas assez de données ({len(sliced)} bougies)")
                continue

            # Version A : sans filtre HA
            res_a = run_backtest(sliced, params, use_ha_filter=False)
            # Version B : avec filtre HA 1H
            res_b = run_backtest(sliced, params, ha_map_1h=ha_map, use_ha_filter=True)

            def fmt(r, label):
                if r is None:
                    return f"  {win_name:<8}  {label:<20}  {'—':>6}  {'—':>6}  {'—':>5}  {'—':>6}  {'—':>8}"
                sign = "+" if r["return"] >= 0 else ""
                return (f"  {win_name:<8}  {label:<20}  {r['trades']:>6}  "
                        f"{r['win_rate']:>5.1f}%  {r['pf']:>5.2f}  "
                        f"{r['dd']:>5.1f}%  {sign}{r['return']:>7.1f}%")

            print(fmt(res_a, "Sans filtre HA"))
            print(fmt(res_b, "Avec filtre HA 1H"))
            print()

    print("=" * 60)
    print("  TERMINÉ")
    print("=" * 60)


if __name__ == "__main__":
    main()
