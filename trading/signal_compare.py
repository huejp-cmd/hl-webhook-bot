#!/usr/bin/env python3
"""
signal_compare.py — Comparaison signaux Python vs TradingView
==============================================================
Télécharge les données Hyperliquid (30m natif ou 1m agrégé),
simule la stratégie barre par barre (walk-forward),
et génère une liste de signaux à comparer avec TradingView → Strategy Tester.

Usage:
    python trading/signal_compare.py                  # SOL 29M + ETH 30M, 30 derniers jours
    python trading/signal_compare.py --coin ETH       # ETH uniquement
    python trading/signal_compare.py --days 60        # 60 derniers jours
    python trading/signal_compare.py --coin ETH --days 90
"""

import argparse
import logging
import math
import os
import sys
import time
import urllib.request
import json
from datetime import datetime, timezone, timedelta

import numpy as np

# ─── Path ───────────────────────────────────────────────────
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

from autonomous_bot import (
    detect_signal,
    COIN_TF,
    COIN_PARAMS,
    ADX_TREND_THRESH,
)

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
HL_API_URL   = "https://api.hyperliquid.xyz/info"
WARMUP_BARS  = 160   # barres de chauffe avant d'émettre des signaux

# Mapping TF → intervalle Hyperliquid natif le plus proche
# 29M n'existe pas → on utilise 30m natif (1 barre ≈ 1 barre, diff <3%)
# 30M → 30m natif exact
NATIVE_INTERVAL = {
    29: "30m",   # 29m n'existe pas en natif → 30m (acceptable pour comparaison TV)
    30: "30m",
}

# ─────────────────────────────────────────────────────────────
# FETCH AVEC PAGINATION
# ─────────────────────────────────────────────────────────────
def _hl_post(payload: dict) -> list:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(HL_API_URL, data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_candles_native(coin: str, interval: str, days: int) -> list:
    """
    Télécharge `days` jours de candles Hyperliquid à l'intervalle natif demandé.
    Gère la pagination automatiquement.
    """
    end_ts   = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    interval_minutes = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
    }.get(interval, 60)
    window_ms = 4500 * interval_minutes * 60_000

    cursor  = start_ts
    candles = []
    batch_n = 0

    print(f"  [DATA] {coin} {interval} ({days}j) — téléchargement...")

    while cursor < end_ts:
        batch_end = min(cursor + window_ms, end_ts)
        payload = {
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": cursor, "endTime": batch_end},
        }
        try:
            batch = _hl_post(payload)
        except Exception as e:
            print(f"  ⚠️  Erreur API: {e} — retry dans 3s")
            time.sleep(3)
            continue

        batch_n += 1

        if not batch:
            cursor = batch_end + interval_minutes * 60_000
            continue

        for c in batch:
            candles.append({
                "t": int(c["t"]),  "T": int(c.get("T", c["t"])),
                "o": float(c["o"]), "h": float(c["h"]),
                "l": float(c["l"]), "c": float(c["c"]),
                "v": float(c.get("v", 0)),
            })

        last_t = batch[-1]["t"]
        cursor = last_t + interval_minutes * 60_000
        time.sleep(0.2)

    # Dédupliquer et trier
    seen = {}
    for c in candles:
        seen[c["t"]] = c
    result = sorted(seen.values(), key=lambda x: x["t"])
    print(f"  [DATA] {coin} {interval}: {len(result):,} barres reçues")
    return result


# ─────────────────────────────────────────────────────────────
# WALK-FORWARD BACKTEST
# ─────────────────────────────────────────────────────────────
def run_backtest(coin: str, days: int) -> list:
    tf     = COIN_TF[coin]
    params = COIN_PARAMS[coin]
    iv     = NATIVE_INTERVAL.get(tf, "30m")

    print(f"\n{'='*65}")
    print(f"  {coin} — TF bot: {tf}M | données: {iv} natif Hyperliquid")
    print(f"  HMA {params['hma_fast']}/{params['hma_slow']} | ADX>{ADX_TREND_THRESH} | RSI pullback: 35–65")
    print(f"  Période: {days} derniers jours")
    print(f"{'='*65}")

    # Télécharger warmup + période d'analyse
    total_days = days + math.ceil(WARMUP_BARS * tf / 1440) + 3
    all_bars = fetch_candles_native(coin, iv, total_days)

    if len(all_bars) < WARMUP_BARS + 20:
        print(f"  ❌ Pas assez de barres ({len(all_bars)}), besoin de {WARMUP_BARS + 20}")
        return []

    # Index de début : garder seulement les `days` derniers jours
    cutoff_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    start_idx = next((i for i, b in enumerate(all_bars) if b["t"] >= cutoff_ts), len(all_bars) - 1)
    start_idx = max(start_idx, WARMUP_BARS)

    print(f"  Analyse de l'index {start_idx} à {len(all_bars)-1} "
          f"({len(all_bars) - start_idx} barres sur {days} jours)")

    signals      = []
    in_position  = None

    # Désactiver les logs du bot pendant le backtest
    root_logger = logging.getLogger()
    old_level   = root_logger.level
    root_logger.setLevel(logging.CRITICAL)

    try:
        for i in range(start_idx, len(all_bars)):
            bars_so_far = all_bars[:i + 1]
            bar = all_bars[i]
            bar_ts = bar["t"]

            # Gérer la position ouverte (SL/TP hit ?)
            if in_position:
                h = float(bar["h"])
                l = float(bar["l"])
                side  = in_position["side"]
                sl    = in_position["sl"]
                tp    = in_position["tp"]
                entry = in_position["entry_price"]

                hit = None
                if side == "long":
                    if l <= sl:   hit, exit_price = "SL", sl
                    elif h >= tp: hit, exit_price = "TP", tp
                else:
                    if h >= sl:   hit, exit_price = "SL", sl
                    elif l <= tp: hit, exit_price = "TP", tp

                if hit:
                    pnl = ((exit_price - entry) / entry * 100 if side == "long"
                           else (entry - exit_price) / entry * 100)
                    in_position.update({
                        "exit_ts": bar_ts, "exit_price": exit_price,
                        "exit_type": hit,  "pnl_pct": round(pnl, 2),
                    })
                    signals.append(in_position)
                    in_position = None
                continue  # pas de nouveau signal si en position

            # Chercher un signal
            sig, sl, tp, entry, meta = detect_signal(coin, bars_so_far)

            if sig:
                in_position = {
                    "coin": coin, "side": sig,
                    "entry_ts": bar_ts, "entry_price": entry,
                    "sl": sl, "tp": tp,
                    "exit_ts": None, "exit_price": None,
                    "exit_type": None, "pnl_pct": None,
                    "regime": meta.get("regime", "?"),
                    "adx":    round(meta.get("adx", 0), 1),
                    "rsi":    round(meta.get("rsi", 0), 1),
                }

    finally:
        root_logger.setLevel(old_level)

    # Position encore ouverte en fin de période
    if in_position:
        last = all_bars[-1]
        c_last = float(last["c"])
        e = in_position["entry_price"]
        s = in_position["side"]
        pnl = ((c_last - e) / e * 100) if s == "long" else ((e - c_last) / e * 100)
        in_position.update({
            "exit_ts": last["t"], "exit_price": c_last,
            "exit_type": "OPEN", "pnl_pct": round(pnl, 2),
        })
        signals.append(in_position)

    return signals


# ─────────────────────────────────────────────────────────────
# AFFICHAGE
# ─────────────────────────────────────────────────────────────
def ts_to_str(ts_ms):
    if ts_ms is None:
        return "—"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def print_signals(signals: list, coin: str):
    tf = COIN_TF[coin]
    if not signals:
        print(f"\n  ⚠️  Aucun signal détecté pour {coin} {tf}M")
        return

    closed = [s for s in signals if s["exit_type"] not in (None, "OPEN")]
    open_  = [s for s in signals if s["exit_type"] == "OPEN"]
    wins   = [s for s in closed if (s.get("pnl_pct") or 0) > 0]
    losses = [s for s in closed if (s.get("pnl_pct") or 0) <= 0]

    print(f"\n{'─'*105}")
    print(f"  {coin} {tf}M — {len(signals)} signaux "
          f"| {len(closed)} clôturés (✅ {len(wins)} TP  ❌ {len(losses)} SL)"
          f"  {len(open_)} encore ouvert(s)")
    print(f"{'─'*105}")
    print(f"  {'#':<4} {'Entrée (UTC)':<18} {'Dir':<8} {'Entrée$':<11} "
          f"{'Sortie$':<11} {'Exit':<5} {'PnL%':<9} {'Régime':<11} {'ADX':<6} {'RSI':<6} Sortie (UTC)")
    print(f"  {'─'*101}")

    for idx, s in enumerate(signals, 1):
        pnl_s = f"{s['pnl_pct']:+.2f}%" if s["pnl_pct"] is not None else "—"
        icon  = "✅" if (s["pnl_pct"] or 0) > 0 else ("❌" if s["exit_type"] == "SL" else "⏳")
        dir_s = "🟢 LONG " if s["side"] == "long" else "🔴 SHORT"

        print(
            f"  {idx:<4} {ts_to_str(s['entry_ts']):<18} {dir_s:<8} "
            f"{s['entry_price']:<11.4f} "
            f"{(s['exit_price'] or 0):<11.4f} "
            f"{(s['exit_type'] or '—'):<5} "
            f"{pnl_s:<9} {icon} "
            f"{s['regime']:<11} "
            f"{s['adx']:<6} {s['rsi']:<6} "
            f"{ts_to_str(s['exit_ts'])}"
        )

    if closed:
        wr       = len(wins) / len(closed) * 100
        avg_pnl  = sum(s["pnl_pct"] for s in closed) / len(closed)
        total    = sum(s["pnl_pct"] for s in closed)
        print(f"  {'─'*101}")
        print(f"  Win Rate: {wr:.1f}%  |  PnL moyen/trade: {avg_pnl:+.2f}%  |  PnL cumulé: {total:+.2f}%")


def export_csv(signals: list, coin: str, days: int) -> str:
    tf    = COIN_TF[coin]
    fname = os.path.join(THIS_DIR, f"signals_{coin}_{tf}M_{days}d.csv")
    with open(fname, "w") as f:
        f.write("Coin,TF,#,EntryUTC,Direction,EntryPrice,ExitPrice,ExitType,PnL%,Regime,ADX,RSI,ExitUTC\n")
        for idx, s in enumerate(signals, 1):
            pnl = f"{s['pnl_pct']:+.2f}" if s["pnl_pct"] is not None else ""
            f.write(
                f"{coin},{tf}M,{idx},"
                f"{ts_to_str(s['entry_ts'])},{s['side'].upper()},"
                f"{s['entry_price']:.4f},{(s['exit_price'] or 0):.4f},"
                f"{s['exit_type'] or ''},{pnl},"
                f"{s['regime']},{s['adx']},{s['rsi']},"
                f"{ts_to_str(s['exit_ts'])}\n"
            )
    print(f"\n  📄 CSV : {fname}")
    return fname


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coin",  default="ALL",  help="SOL | ETH | ALL")
    parser.add_argument("--days",  type=int, default=30, help="Nb jours (défaut: 30)")
    args = parser.parse_args()

    coins = ["SOL", "ETH"] if args.coin.upper() == "ALL" else [args.coin.upper()]

    print(f"\n🔍 COMPARAISON SIGNAUX PYTHON vs TRADINGVIEW")
    print(f"   Paramètres v7.1 :")
    for c in coins:
        p = COIN_PARAMS[c]
        print(f"   {c}: TF={COIN_TF[c]}M | HMA {p['hma_fast']}/{p['hma_slow']} | ADX>{ADX_TREND_THRESH} | RSI 35-65")

    print(f"\n   📌 Comment comparer avec TradingView :")
    print(f"      1. Ouvre le chart ETH 30M (ou SOL 29M) avec le script V7")
    print(f"      2. Strategy Tester → onglet 'List of Trades'")
    print(f"      3. Compare les dates/heures d'entrée avec ce tableau")
    print(f"      Note: Données Hyperliquid vs Binance → légères différences de prix possibles\n")

    for coin in coins:
        signals = run_backtest(coin, args.days)
        print_signals(signals, coin)
        if signals:
            export_csv(signals, coin, args.days)

    print(f"\n{'='*65}")
    print(f"  ✅ Comparaison terminée.")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
