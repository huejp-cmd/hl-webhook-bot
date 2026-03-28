"""
test_labouch.py — Simulation de 10 trades alternés WIN/LOSS
Affiche l'évolution de la séquence et du multiplicateur trade par trade.
"""

import os
import sys
import json
import tempfile

# Dossier courant dans le path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from labouch_manager import LabouchManager

# ---------------------------------------------------------------------------
# Setup : état éphémère (ne pollue pas labouch_state.json)
# ---------------------------------------------------------------------------
tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
tmp.close()

mgr = LabouchManager(
    state_file    = tmp.name,
    unit_factor   = 0.5,
    leverage_mult = 2.0,
    cap_mult      = 4,
    max_mult      = 6.0,
    stop_session_pct = 0.15,
)

SYMBOL   = "SOL"
CAPITAL  = 600.0
ENTRY_PX = 140.0

SCENARIO = [
    ("WIN",  1.0),   # trade 1  — capital + 1%
    ("LOSS", -0.5),  # trade 2
    ("WIN",  1.2),   # trade 3
    ("WIN",  0.8),   # trade 4
    ("LOSS", -0.7),  # trade 5
    ("LOSS", -0.6),  # trade 6
    ("LOSS", -0.4),  # trade 7
    ("WIN",  1.1),   # trade 8
    ("WIN",  0.9),   # trade 9
    ("LOSS", -0.5),  # trade 10
]

SEP = "─" * 72

print(SEP)
print(f"{'Trade':>5}  {'Résultat':<8}  {'Séquence':<20}  {'Bet':>4}  {'Mult':>6}  {'PnL':>8}  {'Cap':>8}")
print(SEP)

cap = CAPITAL

for i, (outcome, pnl_pct) in enumerate(SCENARIO, 1):
    # 1. should_trade
    ok, reason = mgr.should_trade(SYMBOL, cap)
    if not ok:
        print(f"  ⛔ Trade {i} bloqué : {reason}")
        continue

    # 2. get_multiplier
    mult = mgr.get_multiplier(SYMBOL, cap)

    # 3. on_entry  (qty fictive = 1)
    mgr.on_entry(SYMBOL, ENTRY_PX, 1.0 * mult, "buy", cap)

    # Etat AVANT fermeture
    st = mgr.get_status(SYMBOL)
    seq_before = list(st["sequence"])
    bet        = st["bet_units"]

    # 4. Simuler fermeture
    pnl       = cap * abs(pnl_pct) / 100
    cap_after = cap + pnl if outcome == "WIN" else cap - pnl
    mgr.on_close(SYMBOL, 0, cap_after)

    # Etat APRÈS fermeture
    st_after = mgr.get_status(SYMBOL)

    icon = "✅" if outcome == "WIN" else "❌"
    delta = cap_after - cap
    print(
        f"  {i:>3}  {icon} {outcome:<5}  "
        f"{str(seq_before):<20}  "
        f"{bet:>4}  {mult:>6.2f}×  "
        f"{delta:>+8.2f}  {cap_after:>8.2f}"
    )
    print(
        f"       {'':8}  → {str(st_after['sequence']):<20}  "
        f"cum_loss={st_after['cum_loss_units']:.1f}u  "
        f"next_mult={st_after['multiplier']:.2f}×"
    )
    cap = cap_after

print(SEP)
st = mgr.get_status(SYMBOL)
print(f"  Résumé final  {SYMBOL}")
print(f"  Trades        : {st['trade_count']}")
print(f"  P&L total     : {st['total_pnl']:+.2f} USDC")
print(f"  P&L journalier: {st['daily_pnl']:+.2f} USDC ({st['daily_loss_pct']:.1f}%)")
print(f"  Séquence      : {st['sequence']}")
print(f"  Multiplicateur courant : {st['multiplier']:.3f}×")
print(SEP)

# Nettoyage fichier tmp
os.unlink(tmp.name)
print("✔  Test terminé — fichier d'état temporaire supprimé")
