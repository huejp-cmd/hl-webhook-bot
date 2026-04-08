#!/usr/bin/env python3
"""
daily_signal_report.py — Rapport quotidien de concordance signaux
=================================================================
Compare les signaux TradingView reçus (via webhook) avec les trades
exécutés en DryRun sur Hyperliquid.

Détecte :
  - Trades manquants (TW a envoyé un signal, HL n'a pas exécuté)
  - Trades supplémentaires (HL a exécuté sans signal TW connu)
  - Divergences de direction
  - Écarts de prix d'entrée
  - Résultat PnL comparatif

Usage :
  python daily_signal_report.py              # rapport hier
  python daily_signal_report.py --days 2     # rapport avant-hier
  python daily_signal_report.py --today      # rapport jour en cours
"""

import json
import os
import sys
import argparse
import requests
from datetime import datetime, timezone, timedelta, date

# ==============================================================
#  CONFIG
# ==============================================================
BOT_URL      = os.environ.get("BOT_URL",   "https://hl-webhook-bot-production.up.railway.app")
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "jp_bot_secret_2026")
TIMEOUT      = 15   # secondes

# ==============================================================
#  HELPERS
# ==============================================================

def _get(path: str) -> dict | list | None:
    try:
        r = requests.get(
            f"{BOT_URL}{path}",
            headers={"X-Webhook-Token": BOT_TOKEN},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WARN] GET {path} failed: {e}")
        return None


def _date_of(ts_str: str | None) -> date | None:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.date()
    except Exception:
        return None


def _fmt_pnl(pnl_pct, pnl_usdc=None) -> str:
    if pnl_pct is None:
        return "OUVERT"
    sign = "+" if float(pnl_pct) >= 0 else ""
    s = f"{sign}{float(pnl_pct):.2f}%"
    if pnl_usdc is not None:
        sign2 = "+" if float(pnl_usdc) >= 0 else ""
        s += f" ({sign2}{float(pnl_usdc):.2f} USDC)"
    return s


def _fmt_price(p) -> str:
    if p is None:
        return "—"
    return f"{float(p):.4f}"


# ==============================================================
#  CORE
# ==============================================================

def build_report(target_date: date) -> str:
    """Génère le rapport de concordance pour target_date."""

    day_label  = target_date.strftime("%d/%m/%Y")
    prev_label = (target_date - timedelta(days=1)).strftime("%d/%m/%Y")

    lines = []
    lines.append(f"📊 RAPPORT CONCORDANCE SIGNAUX")
    lines.append(f"Date : {day_label}  (backtest TW = {prev_label})")
    lines.append("─" * 44)

    # ----------------------------------------------------------
    #  1. Signaux TradingView reçus par le bot (webhook)
    # ----------------------------------------------------------
    tw_raw = _get("/tw_signals")
    if tw_raw is None:
        tw_signals = []
        lines.append("⚠️  Endpoint /tw_signals indisponible")
        lines.append("   (mise à jour bot requise)")
    else:
        tw_signals = tw_raw.get("signals", [])

    # Filtrer sur la date cible
    tw_day = [s for s in tw_signals if _date_of(s.get("ts")) == target_date]

    # ----------------------------------------------------------
    #  2. Trades journal HL (bot autonome)
    # ----------------------------------------------------------
    journal_raw = _get("/journal")
    hl_trades   = (journal_raw or {}).get("trades", [])

    # Trades ouverts ou fermés le jour cible
    hl_day = []
    for t in hl_trades:
        entry_d = _date_of(t.get("entry_ts"))
        exit_d  = _date_of(t.get("exit_ts"))
        if entry_d == target_date or exit_d == target_date:
            hl_day.append(t)

    # ----------------------------------------------------------
    #  3. Trades webhook (trade_log — clôtures TW)
    # ----------------------------------------------------------
    tlog_raw   = _get("/trade_log")
    tlog_trades = (tlog_raw or {}).get("trades", [])
    tlog_day    = [t for t in tlog_trades if _date_of(t.get("ts")) == target_date]

    # Fusionner journal + trade_log sans doublons (par coin+ts)
    hl_all = list(hl_day)
    for t in tlog_day:
        duplicate = any(
            x.get("coin") == t.get("symbol") and
            abs(float(x.get("entry_price", 0)) - float(t.get("entry", 0))) < 1
            for x in hl_all
        )
        if not duplicate:
            # Normaliser format trade_log vers format journal
            hl_all.append({
                "coin":        t.get("symbol", t.get("coin", "?")),
                "side":        "long" if t.get("side") == "buy" else "short",
                "entry_price": t.get("entry"),
                "exit_price":  t.get("exit"),
                "pnl_pct":     t.get("pnl_pct"),
                "pnl_usdc":    t.get("pnl_usdc"),
                "status":      "CLOSED" if t.get("exit") else "OPEN",
                "entry_ts":    t.get("ts"),
                "source":      "webhook",
            })

    # ----------------------------------------------------------
    #  4. Comparaison par coin
    # ----------------------------------------------------------
    coins   = ["SOL", "ETH"]
    missing = []   # signal TW sans trade HL
    extra   = []   # trade HL sans signal TW
    diffs   = []   # trades présents des deux côtés mais différents

    for coin in coins:
        tw_coin = [s for s in tw_day   if s.get("coin", "").upper() == coin]
        hl_coin = [t for t in hl_all   if str(t.get("coin", "")).upper() == coin]

        lines.append(f"\n🔸 {coin}")

        if not tw_coin and not hl_coin:
            lines.append("   Aucun signal / aucun trade (journée calme)")
            continue

        # TW signals
        if tw_coin:
            for s in tw_coin:
                dir_arrow = "▲" if s.get("side", "").lower() == "buy" else "▼"
                lines.append(
                    f"   TW {dir_arrow} {s.get('side','?').upper()} "
                    f"@{_fmt_price(s.get('price'))} "
                    f"SL={_fmt_price(s.get('sl'))} "
                    f"TP={_fmt_price(s.get('tp'))}"
                )
        else:
            lines.append("   TW : aucun signal reçu")

        # HL trades
        if hl_coin:
            for t in hl_coin:
                status = t.get("status", "?")
                dir_arrow = "▲" if t.get("side", "").lower() == "long" else "▼"
                source = " [webhook]" if t.get("source") == "webhook" else " [auto]"
                lines.append(
                    f"   HL {dir_arrow} {t.get('side','?').upper()} "
                    f"@{_fmt_price(t.get('entry_price'))} "
                    f"→ {_fmt_pnl(t.get('pnl_pct'), t.get('pnl_usdc'))}"
                    f"{source}"
                )
        else:
            lines.append("   HL : aucun trade exécuté")

        # Détection anomalies
        if tw_coin and not hl_coin:
            for s in tw_coin:
                missing.append(f"{coin} {s.get('side','?').upper()} @{_fmt_price(s.get('price'))}")

        elif not tw_coin and hl_coin:
            for t in hl_coin:
                extra.append(f"{coin} {t.get('side','?').upper()} @{_fmt_price(t.get('entry_price'))}")

        elif tw_coin and hl_coin:
            # Vérifier concordance direction
            for s in tw_coin:
                tw_side = "long" if s.get("side", "").lower() in ("buy", "long") else "short"
                matched = [t for t in hl_coin if t.get("side","").lower() == tw_side]
                if not matched:
                    diffs.append(
                        f"{coin} ❌ TW={tw_side.upper()} vs "
                        f"HL={hl_coin[0].get('side','?').upper()}"
                    )
                else:
                    # Vérifier écart prix
                    for t in matched:
                        tw_px = float(s.get("price", 0) or 0)
                        hl_px = float(t.get("entry_price", 0) or 0)
                        if tw_px > 0 and hl_px > 0:
                            ecart_pct = abs(hl_px - tw_px) / tw_px * 100
                            if ecart_pct > 1.0:
                                diffs.append(
                                    f"{coin} ⚠️  Écart prix: TW@{tw_px:.2f} vs HL@{hl_px:.2f} "
                                    f"(+{ecart_pct:.1f}%)"
                                )

    # ----------------------------------------------------------
    #  5. Section anomalies
    # ----------------------------------------------------------
    lines.append("─" * 44)
    lines.append("🔍 ANOMALIES")

    total_anomalies = len(missing) + len(extra) + len(diffs)
    if total_anomalies == 0:
        lines.append("   ✅ Aucune anomalie — concordance parfaite")
    else:
        if missing:
            lines.append(f"\n❌ TRADES MANQUANTS ({len(missing)}) :")
            for m in missing:
                lines.append(f"   • {m}")
        if extra:
            lines.append(f"\n➕ TRADES SANS SIGNAL TW ({len(extra)}) :")
            for e in extra:
                lines.append(f"   • {e}")
        if diffs:
            lines.append(f"\n⚠️  DIVERGENCES ({len(diffs)}) :")
            for d in diffs:
                lines.append(f"   • {d}")

    # ----------------------------------------------------------
    #  6. Bilan PnL du jour
    # ----------------------------------------------------------
    lines.append("─" * 44)
    closed = [t for t in hl_all if t.get("status") == "CLOSED" and t.get("pnl_usdc") is not None]
    if closed:
        total_pnl = sum(float(t.get("pnl_usdc", 0) or 0) for t in closed)
        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"💰 BILAN JOUR : {sign}{total_pnl:.2f} USDC ({len(closed)} trade(s) fermé(s))")
        for t in closed:
            coin = str(t.get("coin", "?")).upper()
            pnl  = float(t.get("pnl_usdc", 0) or 0)
            sign2 = "+" if pnl >= 0 else ""
            lines.append(f"   {coin} : {sign2}{pnl:.2f} USDC ({_fmt_pnl(t.get('pnl_pct'))})")
    else:
        open_count = len([t for t in hl_all if t.get("status") == "OPEN"])
        if open_count:
            lines.append(f"⏳ {open_count} position(s) encore ouverte(s) — PnL en cours")
        else:
            lines.append("💰 Aucun trade fermé ce jour")

    # ----------------------------------------------------------
    #  7. Pied de rapport
    # ----------------------------------------------------------
    lines.append("─" * 44)
    now_asc = datetime.now(timezone(timedelta(hours=-4)))  # AST
    lines.append(f"⏱ Généré le {now_asc.strftime('%d/%m %H:%M')} AST — Mode DRY RUN")

    return "\n".join(lines)


# ==============================================================
#  MAIN
# ==============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",  type=int, default=1,
                        help="Rapport d'il y a N jours (défaut=1 = hier)")
    parser.add_argument("--today", action="store_true",
                        help="Rapport du jour en cours")
    args = parser.parse_args()

    if args.today:
        target = date.today()
    else:
        target = date.today() - timedelta(days=args.days)

    report = build_report(target)
    print(report)
