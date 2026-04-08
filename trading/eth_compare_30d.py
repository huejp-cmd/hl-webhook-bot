#!/usr/bin/env python3
"""
Comparaison 30 jours ETH 45M — OPT vs NON-OPT
Données réelles Hyperliquid
"""
import json, time, urllib.request
import numpy as np
from datetime import datetime, timezone

HL_API = "https://api.hyperliquid.xyz/info"

def hl_post(payload, retries=3):
    data = json.dumps(payload).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(HL_API, data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            time.sleep(2**i)
    raise RuntimeError("API unreachable")

def fetch_eth_45m(days=30):
    """Fetch ETH 15m candles and aggregate to 45m"""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86400 * 1000
    raw = hl_post({"type": "candleSnapshot", "req": {
        "coin": "ETH", "interval": "15m",
        "startTime": start_ms, "endTime": now_ms
    }})
    candles = []
    for c in raw:
        candles.append({
            "t": c["t"], "o": float(c["o"]), "h": float(c["h"]),
            "l": float(c["l"]), "c": float(c["c"]), "v": float(c["v"])
        })
    candles.sort(key=lambda x: x["t"])
    # Aggregate 3x15m → 45m
    agg = []
    i = 0
    while i + 2 < len(candles):
        g = candles[i:i+3]
        agg.append({
            "t": g[0]["t"], "o": g[0]["o"],
            "h": max(c["h"] for c in g), "l": min(c["l"] for c in g),
            "c": g[2]["c"], "v": sum(c["v"] for c in g)
        })
        i += 3
    return agg

def hma(prices, length):
    """Hull Moving Average"""
    prices = np.array(prices, dtype=float)
    n = len(prices)
    if n < length:
        return np.full(n, np.nan)
    half = max(1, length // 2)
    sqrt_l = max(1, int(round(length ** 0.5)))
    
    def wma(arr, period):
        out = np.full(len(arr), np.nan)
        w = np.arange(1, period + 1, dtype=float)
        wsum = w.sum()
        for j in range(period - 1, len(arr)):
            out[j] = np.dot(arr[j-period+1:j+1], w) / wsum
        return out
    
    wma_half = wma(prices, half)
    wma_full = wma(prices, length)
    diff = 2 * wma_half - wma_full
    diff_clean = np.where(np.isnan(diff), 0, diff)
    result = wma(diff_clean, sqrt_l)
    return result

def rsi_calc(prices, length=14):
    prices = np.array(prices, dtype=float)
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.full(len(prices), np.nan)
    avg_loss = np.full(len(prices), np.nan)
    if len(gains) < length:
        return np.full(len(prices), 50.0)
    ag = gains[:length].mean()
    al = losses[:length].mean()
    avg_gain[length] = ag
    avg_loss[length] = al
    for i in range(length + 1, len(prices)):
        avg_gain[i] = (avg_gain[i-1] * (length-1) + gains[i-1]) / length
        avg_loss[i] = (avg_loss[i-1] * (length-1) + losses[i-1]) / length
    rs = np.where(avg_loss == 0, 100, avg_gain / avg_loss)
    rsi = np.where(np.isnan(avg_gain), 50.0, 100 - 100 / (1 + rs))
    return rsi

def adx_calc(highs, lows, closes, length=14):
    highs = np.array(highs); lows = np.array(lows); closes = np.array(closes)
    n = len(closes)
    adx_arr = np.full(n, 25.0)
    diplus_arr = np.full(n, 25.0)
    diminus_arr = np.full(n, 25.0)
    if n < length * 2:
        return adx_arr, diplus_arr, diminus_arr
    tr = np.maximum(highs[1:] - lows[1:],
         np.maximum(abs(highs[1:] - closes[:-1]), abs(lows[1:] - closes[:-1])))
    dp = np.where(highs[1:] - highs[:-1] > lows[:-1] - lows[1:],
                  np.maximum(highs[1:] - highs[:-1], 0), 0)
    dm = np.where(lows[:-1] - lows[1:] > highs[1:] - highs[:-1],
                  np.maximum(lows[:-1] - lows[1:], 0), 0)
    def smooth(arr, l):
        out = np.full(len(arr), np.nan)
        out[l-1] = arr[:l].sum()
        for i in range(l, len(arr)):
            out[i] = out[i-1] - out[i-1]/l + arr[i]
        return out
    atr_s = smooth(tr, length)
    dp_s  = smooth(dp, length)
    dm_s  = smooth(dm, length)
    dip = np.where(atr_s > 0, 100 * dp_s / atr_s, 25)
    dim = np.where(atr_s > 0, 100 * dm_s / atr_s, 25)
    dx = np.where((dip + dim) > 0, 100 * abs(dip - dim) / (dip + dim), 0)
    adx_raw = np.full(len(dx), np.nan)
    start = length * 2 - 2
    if start < len(dx):
        adx_raw[start] = dx[start-length+1:start+1].mean()
        for i in range(start+1, len(dx)):
            adx_raw[i] = (adx_raw[i-1] * (length-1) + dx[i]) / length
    adx_out = np.where(np.isnan(adx_raw), 25.0, adx_raw)
    for i in range(1, n):
        if i-1 < len(adx_out):
            adx_arr[i] = adx_out[i-1]
            diplus_arr[i] = dip[i-1] if i-1 < len(dip) else 25.0
            diminus_arr[i] = dim[i-1] if i-1 < len(dim) else 25.0
    return adx_arr, diplus_arr, diminus_arr

def backtest(candles, hma_fast, hma_slow, adx_thresh, rsi_low, rsi_high, tp_mode="fixed", tp_val=5.0):
    closes = [c["c"] for c in candles]
    highs  = [c["h"] for c in candles]
    lows   = [c["l"] for c in candles]
    
    hma_f = hma(closes, hma_fast)
    hma_s = hma(closes, hma_slow)
    rsi_v = rsi_calc(closes)
    adx_v, dip_v, dim_v = adx_calc(highs, lows, closes)
    
    trades = []
    equity = 10000.0
    peak   = equity
    max_dd = 0.0
    
    i = max(hma_slow + 10, 20)
    while i < len(candles) - 1:
        c = closes[i]; h = highs[i]; l = lows[i]
        mf = hma_f[i]; ms = hma_s[i]
        rv = rsi_v[i]; av = adx_v[i]
        dp = dip_v[i]; dm = dim_v[i]
        
        if np.isnan(mf) or np.isnan(ms):
            i += 1; continue
        
        is_trending  = av > 25
        is_ranging   = av < 20
        
        # TP multiplier
        if tp_mode == "fixed":
            tpm = tp_val
        else:  # adaptive
            tpm = 5.0 if is_trending else (3.5 if is_ranging else 4.5)
        
        # Signal LONG
        bull = dp > dm and c > ms
        pullback_long = l <= mf and c > mf and rv > rsi_low and rv < rsi_high
        
        # Signal SHORT
        bear = dm > dp and c < ms
        pullback_short = h >= mf and c < mf and rv > rsi_low and rv < rsi_high
        
        if is_trending and bull and pullback_long:
            # Simuler trade long
            atr_approx = np.mean([abs(highs[j] - lows[j]) for j in range(max(0,i-10), i)])
            sl = c - atr_approx
            tp = c + atr_approx * tpm
            risk_pct = 0.02
            # Chercher sortie sur les 20 prochaines bougies
            result = None
            for j in range(i+1, min(i+21, len(candles))):
                if lows[j] <= sl:
                    result = "loss"; break
                if highs[j] >= tp:
                    result = "win"; break
            if result:
                pnl = equity * risk_pct * (tpm if result=="win" else -1)
                equity += pnl
                peak = max(peak, equity)
                dd = (peak - equity) / peak * 100
                max_dd = max(max_dd, dd)
                trades.append(result)
                i += 21
                continue
        
        elif is_trending and bear and pullback_short:
            atr_approx = np.mean([abs(highs[j] - lows[j]) for j in range(max(0,i-10), i)])
            sl = c + atr_approx
            tp = c - atr_approx * tpm
            risk_pct = 0.02
            result = None
            for j in range(i+1, min(i+21, len(candles))):
                if highs[j] >= sl:
                    result = "loss"; break
                if lows[j] <= tp:
                    result = "win"; break
            if result:
                pnl = equity * risk_pct * (tpm if result=="win" else -1)
                equity += pnl
                peak = max(peak, equity)
                dd = (peak - equity) / peak * 100
                max_dd = max(max_dd, dd)
                trades.append(result)
                i += 21
                continue
        i += 1
    
    total = len(trades)
    wins  = trades.count("win")
    wr    = wins / total * 100 if total > 0 else 0
    ret   = (equity - 10000) / 10000 * 100
    pf    = (wins * tp_val) / max((total - wins), 1) if total > 0 else 0
    
    return {"trades": total, "wr": round(wr, 1), "ret": round(ret, 1),
            "max_dd": round(max_dd, 1), "pf": round(pf, 2), "equity": round(equity, 0)}

# === MAIN ===
print("📥 Téléchargement ETH 45M (30 jours) depuis Hyperliquid...")
candles = fetch_eth_45m(days=30)
print(f"✅ {len(candles)} bougies 45M chargées\n")

print("🔬 ETH 45M — VERSION OPT (HMA 25/40, TP fixe 5.0, RSI 35-65)")
opt = backtest(candles, hma_fast=25, hma_slow=40, adx_thresh=20,
               rsi_low=35, rsi_high=65, tp_mode="fixed", tp_val=5.0)
print(f"   Trades: {opt['trades']} | WR: {opt['wr']}% | Ret: +{opt['ret']}% | DD: {opt['max_dd']}% | PF: {opt['pf']}")

print("\n🔬 ETH 45M — VERSION NON-OPT (HMA 25/40, TP adaptatif 3.5-5.0, RSI 35-70/30-65)")
nopt = backtest(candles, hma_fast=25, hma_slow=40, adx_thresh=20,
                rsi_low=35, rsi_high=70, tp_mode="adaptive", tp_val=5.0)
print(f"   Trades: {nopt['trades']} | WR: {nopt['wr']}% | Ret: +{nopt['ret']}% | DD: {nopt['max_dd']}% | PF: {nopt['pf']}")

print("\n📊 RÉSUMÉ COMPARATIF :")
print(f"{'':20} {'OPT':>10} {'NON-OPT':>10}")
print(f"{'Trades':20} {opt['trades']:>10} {nopt['trades']:>10}")
print(f"{'Win Rate':20} {opt['wr']:>9}% {nopt['wr']:>9}%")
print(f"{'Rendement 30J':20} {opt['ret']:>9}% {nopt['ret']:>9}%")
print(f"{'Max Drawdown':20} {opt['max_dd']:>9}% {nopt['max_dd']:>9}%")
print(f"{'Profit Factor':20} {opt['pf']:>10} {nopt['pf']:>10}")

# Verdict
if nopt['ret'] > opt['ret'] and nopt['max_dd'] < 30:
    verdict = "NON-OPT (meilleur rendement, DD acceptable)"
elif opt['wr'] > nopt['wr'] and opt['pf'] > nopt['pf']:
    verdict = "OPT (meilleur ratio risque/rendement)"
else:
    verdict = "À surveiller — résultats proches"

print(f"\n✅ RECOMMANDATION 30J : {verdict}")
