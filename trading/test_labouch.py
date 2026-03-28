"""
test_labouch.py — Tests du Labouchère Inversé avec cycles de séries et ceiling marché

Scénarios :
  A : Série 1 avec margin → ceiling atteint → vérifier split 50/50 (net = capital - margin)
  B : Série 2 sans margin → ceiling atteint → vérifier split 50/50 (net = capital)
  C : Simulation classique 10 trades alternés WIN/LOSS (garde la compat ancienne)
"""

import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from labouch_manager import LabouchManager, MARKET_CEILING_NOTIONAL, DEFAULT_CEILING_NOTIONAL

SEP  = "─" * 72
SEP2 = "═" * 72

PASS = "✅ PASS"
FAIL = "❌ FAIL"


def make_mgr(unit_factor=0.5, leverage_mult=2.0, cap_mult=4, max_mult=6.0, stop_session_pct=0.15):
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    mgr = LabouchManager(
        state_file       = tmp.name,
        unit_factor      = unit_factor,
        leverage_mult    = leverage_mult,
        cap_mult         = cap_mult,
        max_mult         = max_mult,
        stop_session_pct = stop_session_pct,
    )
    return mgr, tmp.name


def assert_eq(label, actual, expected, tol=0.01):
    ok = abs(actual - expected) <= tol
    status = PASS if ok else FAIL
    print(f"  {status}  {label}: {actual:.4f}  (attendu: {expected:.4f})")
    return ok


def assert_true(label, condition):
    status = PASS if condition else FAIL
    print(f"  {status}  {label}")
    return condition


# ===========================================================================
#  Scénario A — Série 1 avec margin, ceiling hit
# ===========================================================================

def test_scenario_a():
    print()
    print(SEP2)
    print("SCÉNARIO A — Série 1 avec margin (500 USDC capital + 500 USDC margin)")
    print("       Ceiling SOL = 70 000 USDC notional")
    print(SEP2)

    mgr, tmp = make_mgr()
    passed = []

    # Init série 1 : 500 USDC capital + 500 USDC margin
    SYMBOL       = "SOL"
    CAPITAL      = 500.0
    MARGIN       = 500.0
    TOTAL        = CAPITAL + MARGIN  # 1000 USDC effectif

    mgr.init_series_with_margin(SYMBOL, CAPITAL, MARGIN)

    sym = mgr._state[SYMBOL]
    passed.append(assert_true("series_number == 1", sym["series_number"] == 1))
    passed.append(assert_true("margin_active == True", sym["margin_active"] is True))
    passed.append(assert_eq("active_capital", sym["active_capital"], TOTAL))
    passed.append(assert_eq("initial_margin", sym["initial_margin"], MARGIN))

    # Simuler quelques wins pour faire grossir le capital
    # On pousse active_capital à 1200 USDC (gain de 200 USDC)
    CAPITAL_AT_CEILING = 1200.0
    sym["active_capital"] = CAPITAL_AT_CEILING

    # Prix SOL = 150 USDC → qty qui dépasse le ceiling
    # ceiling SOL = 70 000 USDC → qty = 70000 / 150 = 467 SOL → notional = 70 050 ≥ 70 000
    PRICE_SOL = 150.0
    CEILING   = MARKET_CEILING_NOTIONAL["SOL"]  # 70 000
    qty_at_ceiling = (CEILING / PRICE_SOL) + 0.5   # 467.17 SOL → notional = 70 075 ≥ 70 000

    print()
    print(f"  Capital effectif avant ceiling : {CAPITAL_AT_CEILING:.2f} USDC")
    print(f"  Prix SOL                       : {PRICE_SOL:.2f} USDC")
    print(f"  Qty simulée                    : {qty_at_ceiling:.2f} SOL")
    print(f"  Notional simulé                : {qty_at_ceiling * PRICE_SOL:.2f} USDC  (seuil: {CEILING})")
    print()

    ceiling_hit = mgr.check_ceiling(SYMBOL, qty_at_ceiling, PRICE_SOL)

    passed.append(assert_true("check_ceiling() retourne True", ceiling_hit is True))

    sym_after = mgr._state[SYMBOL]

    # net_capital = capital_at_ceiling - initial_margin = 1200 - 500 = 700 USDC
    expected_net     = CAPITAL_AT_CEILING - MARGIN   # 700
    expected_reserve = expected_net * 0.50           # 350
    expected_next    = expected_net * 0.50           # 350

    passed.append(assert_true("margin_active == False après ceiling", sym_after["margin_active"] is False))
    passed.append(assert_true("series_number == 2", sym_after["series_number"] == 2))
    passed.append(assert_eq("reserve", sym_after["reserve"], expected_reserve))
    passed.append(assert_eq("active_capital (base série 2)", sym_after["active_capital"], expected_next))
    passed.append(assert_true("séquence reset [1,1,1,1]", sym_after["sequence"] == [1, 1, 1, 1]))
    passed.append(assert_eq("cum_loss_units reset", sym_after["cum_loss_units"], 0.0))
    passed.append(assert_eq("initial_margin reset à 0", sym_after["initial_margin"], 0.0))

    print()
    print(f"  Capital net (après retrait margin) : {expected_net:.2f} USDC")
    print(f"  → Réserve ajoutée                  : {expected_reserve:.2f} USDC")
    print(f"  → Base série 2                     : {expected_next:.2f} USDC")

    os.unlink(tmp)
    all_ok = all(passed)
    print()
    print(f"  {'✅ Scénario A : TOUS LES TESTS PASSÉS' if all_ok else '❌ Scénario A : ÉCHEC'}")
    return all_ok


# ===========================================================================
#  Scénario B — Série 2 sans margin, ceiling hit
# ===========================================================================

def test_scenario_b():
    print()
    print(SEP2)
    print("SCÉNARIO B — Série 2 sans margin (350 USDC base)")
    print("       Ceiling ETH = 50 000 USDC notional")
    print(SEP2)

    mgr, tmp = make_mgr()
    passed = []

    SYMBOL = "ETH"

    # Simuler directement une série 2 (sans margin)
    # On part de la situation post-ceiling du scénario A
    sym = mgr._get_sym(SYMBOL)
    sym["series_number"]  = 2
    sym["margin_active"]  = False
    sym["initial_margin"] = 0.0
    sym["initial_capital"]= 350.0
    sym["active_capital"] = 350.0
    sym["reserve"]        = 350.0   # déjà 350 en réserve de la série 1
    sym["sequence"]       = [1, 1, 1, 1]
    sym["cum_loss_units"] = 0.0

    # Simuler des wins → capital monte à 600 USDC
    CAPITAL_AT_CEILING = 600.0
    sym["active_capital"] = CAPITAL_AT_CEILING

    # Prix ETH = 2000 USDC → qty qui dépasse le ceiling
    # ceiling ETH = 50 000 → qty = 25.1 ETH → notional = 50 200 ≥ 50 000
    PRICE_ETH = 2000.0
    CEILING   = MARKET_CEILING_NOTIONAL["ETH"]   # 50 000
    qty_at_ceiling = (CEILING / PRICE_ETH) + 0.1  # 25.1 ETH

    print()
    print(f"  Capital série 2 avant ceiling  : {CAPITAL_AT_CEILING:.2f} USDC")
    print(f"  Réserve accumulée (série 1)    : {sym['reserve']:.2f} USDC")
    print(f"  Prix ETH                       : {PRICE_ETH:.2f} USDC")
    print(f"  Qty simulée                    : {qty_at_ceiling:.2f} ETH")
    print(f"  Notional simulé                : {qty_at_ceiling * PRICE_ETH:.2f} USDC  (seuil: {CEILING})")
    print()

    ceiling_hit = mgr.check_ceiling(SYMBOL, qty_at_ceiling, PRICE_ETH)

    passed.append(assert_true("check_ceiling() retourne True", ceiling_hit is True))

    sym_after = mgr._state[SYMBOL]

    # Pas de margin → net_capital = capital_at_ceiling = 600
    expected_net          = CAPITAL_AT_CEILING         # 600
    expected_reserve_gain = expected_net * 0.50        # 300
    expected_reserve_total= 350.0 + expected_reserve_gain  # 650
    expected_next         = expected_net * 0.50        # 300

    passed.append(assert_true("margin_active == False", sym_after["margin_active"] is False))
    passed.append(assert_true("series_number == 3", sym_after["series_number"] == 3))
    passed.append(assert_eq("reserve_total", sym_after["reserve"], expected_reserve_total))
    passed.append(assert_eq("active_capital (base série 3)", sym_after["active_capital"], expected_next))
    passed.append(assert_true("séquence reset [1,1,1,1]", sym_after["sequence"] == [1, 1, 1, 1]))
    passed.append(assert_eq("cum_loss_units reset", sym_after["cum_loss_units"], 0.0))

    print()
    print(f"  Net capital                        : {expected_net:.2f} USDC")
    print(f"  → Réserve ajoutée                  : {expected_reserve_gain:.2f} USDC")
    print(f"  → Réserve totale                   : {expected_reserve_total:.2f} USDC")
    print(f"  → Base série 3                     : {expected_next:.2f} USDC")

    os.unlink(tmp)
    all_ok = all(passed)
    print()
    print(f"  {'✅ Scénario B : TOUS LES TESTS PASSÉS' if all_ok else '❌ Scénario B : ÉCHEC'}")
    return all_ok


# ===========================================================================
#  Scénario C — check_ceiling() ne déclenche PAS si notional < ceiling
# ===========================================================================

def test_scenario_c_no_ceiling():
    print()
    print(SEP2)
    print("SCÉNARIO C — check_ceiling() avec notional < ceiling (ne doit pas déclencher)")
    print(SEP2)

    mgr, tmp = make_mgr()
    passed = []

    SYMBOL = "BTC"
    mgr.init_series_with_margin(SYMBOL, 1000.0, 1000.0)

    # Prix BTC = 60 000 USDC, qty = 1 BTC → notional = 60 000 < ceiling 100 000
    PRICE_BTC = 60_000.0
    qty       = 1.0   # notional = 60 000 < 100 000

    ceiling_hit = mgr.check_ceiling(SYMBOL, qty, PRICE_BTC)

    passed.append(assert_true("check_ceiling() retourne False (pas de ceiling)", ceiling_hit is False))

    sym = mgr._state[SYMBOL]
    passed.append(assert_true("series_number inchangé == 1", sym["series_number"] == 1))
    passed.append(assert_true("margin_active inchangé == True", sym["margin_active"] is True))

    os.unlink(tmp)
    all_ok = all(passed)
    print()
    print(f"  {'✅ Scénario C : TOUS LES TESTS PASSÉS' if all_ok else '❌ Scénario C : ÉCHEC'}")
    return all_ok


# ===========================================================================
#  Scénario D — get_status() inclut les nouveaux champs
# ===========================================================================

def test_scenario_d_status():
    print()
    print(SEP2)
    print("SCÉNARIO D — get_status() inclut les nouveaux champs")
    print(SEP2)

    mgr, tmp = make_mgr()
    passed = []

    SYMBOL = "SOL"
    mgr.init_series_with_margin(SYMBOL, 500.0, 500.0)

    status = mgr.get_status(SYMBOL)

    required_fields = [
        "symbol", "series_number", "margin_active", "initial_margin",
        "active_capital", "reserve", "sequence", "bet_units", "multiplier",
        "cum_loss_units", "daily_pnl", "daily_loss_pct", "total_pnl",
        "trade_count", "stop_active", "ceiling_notional",
    ]

    for field in required_fields:
        passed.append(assert_true(f"champ '{field}' présent dans get_status()", field in status))

    passed.append(assert_eq("ceiling_notional SOL", status["ceiling_notional"], 70_000.0))
    passed.append(assert_true("series_number == 1", status["series_number"] == 1))
    passed.append(assert_true("margin_active == True", status["margin_active"] is True))
    passed.append(assert_eq("active_capital == 1000", status["active_capital"], 1000.0))
    passed.append(assert_eq("reserve == 0", status["reserve"], 0.0))

    os.unlink(tmp)
    all_ok = all(passed)
    print()
    print(f"  {'✅ Scénario D : TOUS LES TESTS PASSÉS' if all_ok else '❌ Scénario D : ÉCHEC'}")
    return all_ok


# ===========================================================================
#  Scénario E — Simulation classique 10 trades (rétrocompatibilité)
# ===========================================================================

def test_scenario_e_classic():
    print()
    print(SEP2)
    print("SCÉNARIO E — Simulation classique 10 trades WIN/LOSS")
    print(SEP2)

    mgr, tmp = make_mgr()

    SYMBOL   = "SOL"
    CAPITAL  = 600.0
    ENTRY_PX = 140.0

    SCENARIO = [
        ("WIN",  1.0),
        ("LOSS", -0.5),
        ("WIN",  1.2),
        ("WIN",  0.8),
        ("LOSS", -0.7),
        ("LOSS", -0.6),
        ("LOSS", -0.4),
        ("WIN",  1.1),
        ("WIN",  0.9),
        ("LOSS", -0.5),
    ]

    print(f"{'Trade':>5}  {'Résultat':<8}  {'Séquence':<20}  {'Bet':>4}  {'Mult':>6}  {'PnL':>8}  {'Cap':>8}")
    print(SEP)

    cap = CAPITAL

    for i, (outcome, pnl_pct) in enumerate(SCENARIO, 1):
        ok, reason = mgr.should_trade(SYMBOL, cap)
        if not ok:
            print(f"  ⛔ Trade {i} bloqué : {reason}")
            continue

        mult = mgr.get_multiplier(SYMBOL, cap)
        mgr.on_entry(SYMBOL, ENTRY_PX, 1.0 * mult, "buy", cap)

        st         = mgr.get_status(SYMBOL)
        seq_before = list(st["sequence"])
        bet        = st["bet_units"]

        pnl       = cap * abs(pnl_pct) / 100
        cap_after = cap + pnl if outcome == "WIN" else cap - pnl
        mgr.on_close(SYMBOL, 0, cap_after)

        st_after = mgr.get_status(SYMBOL)
        icon     = "✅" if outcome == "WIN" else "❌"
        delta    = cap_after - cap

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

    os.unlink(tmp)
    print()
    print("  ✅ Scénario E : simulation classique terminée")
    return True


# ===========================================================================
#  Main
# ===========================================================================

if __name__ == "__main__":
    results = []

    results.append(("Scénario A (série avec margin → ceiling)", test_scenario_a()))
    results.append(("Scénario B (série sans margin → ceiling)", test_scenario_b()))
    results.append(("Scénario C (pas de ceiling si notional < seuil)", test_scenario_c_no_ceiling()))
    results.append(("Scénario D (get_status nouveaux champs)",  test_scenario_d_status()))
    results.append(("Scénario E (simulation classique 10 trades)", test_scenario_e_classic()))

    print()
    print(SEP2)
    print("RÉSUMÉ FINAL")
    print(SEP2)
    all_passed = True
    for name, ok in results:
        icon = "✅" if ok else "❌"
        print(f"  {icon}  {name}")
        if not ok:
            all_passed = False

    print()
    if all_passed:
        print("  🎉 TOUS LES TESTS PASSÉS")
    else:
        print("  ⚠️  CERTAINS TESTS ONT ÉCHOUÉ")
        sys.exit(1)
