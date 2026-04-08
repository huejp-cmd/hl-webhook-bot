#!/usr/bin/env python3
"""
signal_compare.py — Comparaison signaux Python vs TradingView
==============================================================
Simule la stratégie barre par barre (walk-forward) avec la logique exacte
du bot v7.1 (range bars, HA 1H, per-coin params) sur les données Hyperliquid,
et génère une liste de signaux à comparer avec TradingView → Strategy Tester.

Usage:
    python trading/signal_compare.py                  # SOL + ETH, 30 jours
    python trading/signal_compare.py --coin ETH       # ETH uniquement
    python trading/signal_compare.py --days 60        # 60 derniers jours
"""

import argparse
import json
import logging
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

import numpy as np

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

from autonomous_bot import (
    detect_signal,
    compute_range_bars,
    calc_atr,
    calc_hma,
    COIN_TF,
    COIN_PARAMS,
    ATR_LEN,
)

HL_API_URL  = "https://api.hyperliquid.xyz/info"
WARMUP_BARS = 160

# ─────────────────────────────────────────────────────────────
# FETCH DONNÉES
# ─────────────────────────────────────────────────────────────
def _hl_post(payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(HL_API_URL, data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_candles(coin: str, interval: str, days: int) -> list:
    end_ts   = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    interval_min = {"1m":1,"15m":15,"30m":30,"1h":60,"4h":240}.get(interval, 30)
    window_ms    = 4500 * interval_min * 60_000

    cursor, candles = start_ts, []
    print(f"  [DATA] {coin} {interval} ({days}j)...")
    while cursor < end_ts:
        batch_end = min(cursor + window_ms, end_ts)
        try:
            batch = _hl_post({"type":"candleSnapshot","req":{
                "coin":coin,"interval":interval,"startTime":cursor,"endTime":batch_end}})
        except Exception as e:
            print(f"  ⚠ API error: {e}"); time.sleep(3); continue
        if not batch:
            cursor = batch_end + interval_min * 60_000; continue
        for c in batch:
            candles.append({"t":int(c["t"]),"T":int(c.get("T",c["t"])),
                "o":float(c["o"]),"h":float(c["h"]),"l":float(c["l"]),
                "c":float(c["c"]),"v":float(c.get("v",0))})
        cursor = batch[-1]["t"] + interval_min * 60_000
        time.sleep(0.2)
    seen = {}
    for c in candles: seen[c["t"]] = c
    result = sorted(seen.values(), key=lambda x: x["t"])
    print(f"  [DATA] {len(result):,} barres {interval} reçues")
    return result


def fetch_ha_1h_history(coin: str, days: int) -> list:
    """Récupère l'historique 1H et calcule les HA pour chaque barre."""
    candles = fetch_candles(coin, "1h", days + 5)
    if len(candles) < 2:
        return []
    ha_close = [(c["o"]+c["h"]+c["l"]+c["c"])/4.0 for c in candles]
    ha_open  = [candles[0]["o"]]
    for i in range(1, len(candles)):
        ha_open.append((ha_open[-1] + ha_close[i-1]) / 2.0)
    result = []
    for i, c in enumerate(candles):
        result.append({
            "t":       c["t"],
            "T":       c["T"],
            "ha_bull": ha_close[i] > ha_open[i],
            "ha_bear": ha_close[i] < ha_open[i],
        })
    return result


# ─────────────────────────────────────────────────────────────
# WALK-FORWARD BACKTEST
# ─────────────────────────────────────────────────────────────
def get_ha_for_bar(ha_1h: list, bar_ts_ms: int) -> tuple:
    """Retourne (ha_bull, ha_bear) de la dernière barre 1H FERMÉE avant bar_ts."""
    if not ha_1h:
        return None, None
    # cherche la dernière barre 1H dont T (close time) < bar_ts
    best = None
    for h in ha_1h:
        if h["T"] < bar_ts_ms:
            best = h
        else:
            break
    if best is None:
        return None, None
    return best["ha_bull"], best["ha_bear"]


def run_backtest(coin: str, days: int) -> list:
    tf     = COIN_TF[coin]
    params = COIN_PARAMS[coin]
    use_ha = params.get("use_ha_filter", False)
    iv     = "30m"  # 29m n'existe pas → 30m natif (différence < 0.5%)

    print(f"\n{'='*65}")
    print(f"  {coin} — TF: {tf}M | HMA {params['hma_fast']}/{params['hma_slow']}")
    print(f"  ADX trend>{params['adx_trend']} range<{params['adx_range']}")
    print(f"  HA 1H filter: {'OUI' if use_ha else 'NON'} | Range bars: OUI")
    print(f"  Période: {days} jours")
    print(f"{'='*65}")

    total_days = days + math.ceil(WARMUP_BARS * tf / 1440) + 5
    all_bars   = fetch_candles(coin, iv, total_days)

    ha_1h = []
    if use_ha:
        ha_1h = fetch_ha_1h_history(coin, total_days)

    if len(all_bars) < WARMUP_BARS + 10:
        print(f"  ❌ Pas assez de barres ({len(all_bars)})")
        return []

    cutoff_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    start_idx = next((i for i, b in enumerate(all_bars) if b["t"] >= cutoff_ts), len(all_bars)-1)
    start_idx = max(start_idx, WARMUP_BARS)
    print(f"  Analyse: index {start_idx}→{len(all_bars)-1} ({len(all_bars)-start_idx} barres)")

    signals     = []
    in_position = None

    # Désactiver les logs pendant le backtest
    root_logger = logging.getLogger()
    old_level   = root_logger.level
    root_logger.setLevel(logging.CRITICAL)

    try:
        for i in range(start_idx, len(all_bars)):
            bars_so_far = all_bars[:i+1]
            bar         = all_bars[i]
            bar_ts      = bar["t"]

            # Gérer position ouverte (SL/TP/DCA)
            if in_position:
                h = float(bar["h"])
                l = float(bar["l"])
                c = float(bar["c"])
                side  = in_position["side"]
                sl    = in_position["sl"]
                tp    = in_position["tp"]
                entry = in_position["entry_price"]

                # DCA check
                dca_count = in_position.get("dca_count", 0)
                last_dca  = in_position.get("last_dca_price", entry)
                if dca_count < 2:
                    if side == "long" and c < last_dca * 0.98:
                        in_position["dca_count"]      = dca_count + 1
                        in_position["last_dca_price"] = c
                        # recalcul avg + nouveau TP
                        avg = (entry + c) / 2.0
                        in_position["avg_price"] = avg
                        rr  = 4.0 if in_position.get("regime") == "TREND" else 3.0
                        in_position["tp"] = avg + (avg - sl) * rr
                        tp = in_position["tp"]
                    elif side == "short" and c > last_dca * 1.02:
                        in_position["dca_count"]      = dca_count + 1
                        in_position["last_dca_price"] = c
                        avg = (entry + c) / 2.0
                        in_position["avg_price"] = avg
                        rr  = 4.0 if in_position.get("regime") == "TREND" else 3.0
                        in_position["tp"] = avg - (sl - avg) * rr
                        tp = in_position["tp"]

                # SL/TP hit
                hit = None
                if side == "long":
                    if l <= sl:   hit, exit_price = "SL", sl
                    elif h >= tp: hit, exit_price = "TP", tp
                else:
                    if h >= sl:   hit, exit_price = "SL", sl
                    elif l <= tp: hit, exit_price = "TP", tp

                if hit:
                    avg_e = in_position.get("avg_price", entry)
                    pnl   = ((exit_price-avg_e)/avg_e*100 if side=="long"
                             else (avg_e-exit_price)/avg_e*100)
                    in_position.update({
                        "exit_ts": bar_ts, "exit_price": exit_price,
                        "exit_type": hit,  "pnl_pct": round(pnl, 2),
                    })
                    signals.append(in_position)
                    in_position = None
                continue

            # HA 1H pour SOL
            ha_bull, ha_bear = get_ha_for_bar(ha_1h, bar_ts) if use_ha else (None, None)

            sig, sl, tp, entry, meta = detect_signal(coin, bars_so_far, ha_bull, ha_bear)
            if sig:
                in_position = {
                    "coin": coin, "side": sig,
                    "entry_ts": bar_ts, "entry_price": entry,
                    "avg_price": entry,
                    "sl": sl, "tp": tp,
                    "last_dca_price": entry, "dca_count": 0,
                    "exit_ts": None, "exit_price": None,
                    "exit_type": None, "pnl_pct": None,
                    "regime": meta.get("regime","?"),
                    "adx":    round(meta.get("adx",0),1),
                    "rsi":    round(meta.get("rsi",0),1),
                }
    finally:
        root_logger.setLevel(old_level)

    # Position encore ouverte
    if in_position:
        last  = all_bars[-1]
        c_l   = float(last["c"])
        avg_e = in_position.get("avg_price", in_position["entry_price"])
        s     = in_position["side"]
        pnl   = ((c_l-avg_e)/avg_e*100) if s=="long" else ((avg_e-c_l)/avg_e*100)
        in_position.update({"exit_ts":last["t"],"exit_price":c_l,
                             "exit_type":"OPEN","pnl_pct":round(pnl,2)})
        signals.append(in_position)

    return signals


# ─────────────────────────────────────────────────────────────
# AFFICHAGE
# ─────────────────────────────────────────────────────────────
def ts(ms):
    if ms is None: return "—"
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def print_signals(signals: list, coin: str):
    tf = COIN_TF[coin]
    if not signals:
        print(f"\n  ⚠️  Aucun signal détecté pour {coin} {tf}M"); return

    closed = [s for s in signals if s["exit_type"] not in (None,"OPEN")]
    open_  = [s for s in signals if s["exit_type"] == "OPEN"]
    wins   = [s for s in closed if (s.get("pnl_pct") or 0) > 0]
    losses = [s for s in closed if (s.get("pnl_pct") or 0) <= 0]

    print(f"\n{'─'*108}")
    print(f"  {coin} {tf}M — {len(signals)} signaux | clôturés: {len(closed)} "
          f"(✅ {len(wins)} TP  ❌ {len(losses)} SL) | ⏳ {len(open_)} ouvert(s)")
    print(f"{'─'*108}")
    print(f"  {'#':<4} {'Entrée UTC':<18} {'Dir':<8} {'Entrée$':<11} {'SL$':<11} "
          f"{'TP$':<11} {'Exit':<5} {'PnL%':<9} {'Régime':<11} {'ADX':<6} {'RSI':<5} Sortie UTC")
    print(f"  {'─'*104}")

    for idx, s in enumerate(signals, 1):
        pnl_s = f"{s['pnl_pct']:+.2f}%" if s["pnl_pct"] is not None else "—"
        icon  = "✅" if (s["pnl_pct"] or 0) > 0 else ("❌" if s["exit_type"]=="SL" else "⏳")
        dir_s = "🟢 LONG " if s["side"]=="long" else "🔴 SHORT"
        print(
            f"  {idx:<4} {ts(s['entry_ts']):<18} {dir_s} "
            f"{s['entry_price']:<11.4f} {s['sl']:<11.4f} {s['tp']:<11.4f} "
            f"{(s['exit_type'] or '—'):<5} {pnl_s:<9} {icon} "
            f"{s['regime']:<11} {s['adx']:<6} {s['rsi']:<5} {ts(s['exit_ts'])}"
        )

    if closed:
        wr      = len(wins)/len(closed)*100
        avg_pnl = sum(s["pnl_pct"] for s in closed)/len(closed)
        total   = sum(s["pnl_pct"] for s in closed)
        print(f"  {'─'*104}")
        print(f"  Win Rate: {wr:.1f}% | PnL moyen: {avg_pnl:+.2f}% | PnL cumulé: {total:+.2f}%")


def export_csv(signals: list, coin: str, days: int) -> str:
    tf    = COIN_TF[coin]
    fname = os.path.join(THIS_DIR, f"signals_{coin}_{tf}M_{days}d.csv")
    with open(fname, "w") as f:
        f.write("Coin,TF,#,EntryUTC,Direction,EntryPrice,SL,TP,ExitPrice,ExitType,PnL%,Regime,ADX,RSI,ExitUTC\n")
        for idx, s in enumerate(signals, 1):
            pnl = f"{s['pnl_pct']:+.2f}" if s["pnl_pct"] is not None else ""
            f.write(f"{coin},{tf}M,{idx},{ts(s['entry_ts'])},{s['side'].upper()},"
                    f"{s['entry_price']:.4f},{s['sl']:.4f},{s['tp']:.4f},"
                    f"{(s['exit_price'] or 0):.4f},{s['exit_type'] or ''},{pnl},"
                    f"{s['regime']},{s['adx']},{s['rsi']},{ts(s['exit_ts'])}\n")
    print(f"\n  📄 CSV: {fname}")
    return fname


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coin", default="ALL", help="SOL | ETH | ALL")
    parser.add_argument("--days", type=int, default=30)
    args   = parser.parse_args()
    coins  = ["SOL","ETH"] if args.coin.upper()=="ALL" else [args.coin.upper()]

    print(f"\n🔍 COMPARAISON SIGNAUX PYTHON v7.1 vs TRADINGVIEW")
    print(f"   Logique exacte: range bars + HA 1H (SOL) + DCA + per-coin params")
    print(f"\n   📌 Comment comparer avec TradingView :")
    print(f"      1. Ouvre le chart {', '.join(coins)} avec le script V7")
    print(f"      2. Strategy Tester → 'List of Trades'")
    print(f"      3. Compare les dates/directions ci-dessous\n")

    for coin in coins:
        signals = run_backtest(coin, args.days)
        print_signals(signals, coin)
        if signals:
            export_csv(signals, coin, args.days)

    print(f"\n{'='*65}")
    print(f"  ✅ Comparaison terminée. Fichiers CSV disponibles dans trading/")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
