"""
Labouchère Inversé Dynamique — Position Sizing Manager
=======================================================
Logique :
  WIN  → séquence grossit, multiplicateur augmente
  LOSS → séquence rétrécit, multiplicateur diminue (min 0.5×)

Cycles de séries avec ceiling marché :
  Série 1 : capital_propre + margin_initiale (levier)
    → Ceiling hit : net = capital_total - margin
                    50% → réserve sécurisée
                    50% → base série 2 (sans margin)
  Séries 2+ : sans margin
    → Ceiling hit : 50% → réserve supplémentaire
                    50% → base série suivante

Règles Labouchère :
  1. Séquence de départ : [1, 1, 1, 1]
  2. Mise courante = sequence[0] + sequence[-1]  (en unités)
  3. 1 unité = unit_factor × la position normale
  4. mult_effectif = bet_units × unit_factor × leverage_mult  (≥ 0.5, ≤ max_mult)
  5. WIN  : ajoute bet_units à la fin
  6. LOSS : retire premier + dernier ; séquence vide → reset [1,1,1,1]
  7. Plafond dynamique : bet_units ≤ cap_mult × cum_loss_units  (min 2)
  8. Plafond absolu    : mult_effectif ≤ max_mult
  9. Reset mensuel     : séquence + cum_loss_units remis à zéro
 10. Stop session      : perte journalière > stop_session_pct du capital → pause
"""

import json
import logging
import os
from datetime import datetime
from typing import Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Chemin par défaut du fichier d'état (même dossier que ce module)
# ---------------------------------------------------------------------------
_HERE      = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "labouch_state.json")

# ---------------------------------------------------------------------------
# Paramètres par défaut
# ---------------------------------------------------------------------------
UNIT_FACTOR       = 0.5    # 1 unité = 0.5× la position normale
LEVERAGE_MULT     = 2.0    # levier Labouchère
CAP_MULT          = 4      # plafond = 4× pertes cumulées
MAX_MULT          = 6.0    # multiplicateur absolu max
MIN_MULT          = 0.5    # multiplicateur absolu min
RESET_MONTHLY     = True
STOP_SESSION_PCT  = 0.15   # 15 % de perte journalière → pause

# ---------------------------------------------------------------------------
# Ceiling marché (taille notionnelle max par symbole en USDC)
# ---------------------------------------------------------------------------
# Mode "réaliste" : exécution propre sans slippage
MARKET_CEILING_REALISTIC = {
    "ETH": 50_000,
    "SOL": 70_000,
    "BTC": 100_000,
}

# Mode "haut" : ceiling élevé pour maximiser la performance
# (correspond à la liquidité profonde du carnet d'ordres)
MARKET_CEILING_HIGH = {
    "ETH": 500_000,
    "SOL": 500_000,
    "BTC": 1_000_000,
}

DEFAULT_CEILING_NOTIONAL = 50_000

# Mode actif : "realistic" ou "high"
CEILING_MODE = "high"  # JP: ceiling haut pour test et performance max

# Alias rétrocompat (pointe sur le mode réaliste par défaut)
MARKET_CEILING_NOTIONAL = MARKET_CEILING_REALISTIC


def _default_sym_state() -> dict:
    return {
        # --- Cycle de séries ---
        "series_number":       1,
        "margin_active":       False,
        "initial_margin":      0.0,
        "initial_capital":     0.0,
        "active_capital":      0.0,
        "reserve":             0.0,
        # --- Labouchère ---
        "sequence":            [1, 1, 1, 1],
        "cum_loss_units":      0.0,
        "last_entry_price":    None,
        "last_entry_qty":      None,
        "last_entry_side":     None,
        "last_entry_capital":  None,
        "last_bet_units":      2,
        # --- Stats ---
        "daily_pnl":           0.0,
        "daily_start_capital": None,
        "daily_date":          None,
        "last_month":          None,
        "trade_count":         0,
        "total_pnl":           0.0,
    }


# ===========================================================================
#  LabouchManager
# ===========================================================================

class LabouchManager:
    """Gestionnaire Labouchère Inversé par symbole avec cycles de séries."""

    def __init__(
        self,
        state_file:       str   = STATE_FILE,
        unit_factor:      float = UNIT_FACTOR,
        leverage_mult:    float = LEVERAGE_MULT,
        cap_mult:         int   = CAP_MULT,
        max_mult:         float = MAX_MULT,
        reset_monthly:    bool  = RESET_MONTHLY,
        stop_session_pct: float = STOP_SESSION_PCT,
    ):
        self.state_file       = state_file
        self.unit_factor      = unit_factor
        self.leverage_mult    = leverage_mult
        self.cap_mult         = cap_mult
        self.max_mult         = max_mult
        self.reset_monthly    = reset_monthly
        self.stop_session_pct = stop_session_pct
        self._state: dict     = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistance
    # ------------------------------------------------------------------

    def _load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    self._state = json.load(f)
                # Migration : ajouter les nouveaux champs manquants
                for sym_key, sym_val in self._state.items():
                    defaults = _default_sym_state()
                    for k, v in defaults.items():
                        if k not in sym_val:
                            sym_val[k] = v
                log.info(f"[Labouchère] État chargé depuis {self.state_file}")
            except Exception as e:
                log.warning(f"[Labouchère] Impossible de lire l'état ({e}) → reset")
                self._state = {}
        else:
            self._state = {}

    def _save(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            log.error(f"[Labouchère] Impossible de sauvegarder l'état : {e}")

    def _get_sym(self, symbol: str) -> dict:
        """Retourne l'état du symbole, l'initialise si absent."""
        if symbol not in self._state:
            self._state[symbol] = _default_sym_state()
        return self._state[symbol]

    # ------------------------------------------------------------------
    # Resets
    # ------------------------------------------------------------------

    def _check_monthly_reset(self, sym: dict):
        if not self.reset_monthly:
            return
        current_month = datetime.utcnow().strftime("%Y-%m")
        if sym.get("last_month") != current_month:
            log.info(f"[Labouchère] Reset mensuel ({current_month})")
            sym["sequence"]            = [1, 1, 1, 1]
            sym["cum_loss_units"]      = 0.0
            sym["last_month"]          = current_month
            sym["daily_pnl"]           = 0.0
            sym["daily_start_capital"] = None
            sym["daily_date"]          = None

    def _check_daily_reset(self, sym: dict, capital: float):
        """Initialise le capital journalier de référence si nouveau jour."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if sym.get("daily_date") != today:
            sym["daily_pnl"]           = 0.0
            sym["daily_start_capital"] = capital
            sym["daily_date"]          = today
            log.info(f"[Labouchère] Nouveau jour {today}, capital de référence={capital:.2f}")

    # ------------------------------------------------------------------
    # Calcul interne
    # ------------------------------------------------------------------

    def _calc_bet_units(self, sym: dict) -> int:
        """Calcule bet_units depuis la séquence avec plafond dynamique."""
        seq = sym.get("sequence") or []
        if not seq:
            seq = [1, 1, 1, 1]
            sym["sequence"] = seq

        if len(seq) == 1:
            bet_units = seq[0] * 2
        else:
            bet_units = seq[0] + seq[-1]

        # Plafond dynamique : min 2 unités
        cum_loss  = max(0.0, sym.get("cum_loss_units", 0.0))
        cap_units = max(2, self.cap_mult * cum_loss)
        bet_units = min(bet_units, int(cap_units))

        return max(1, int(bet_units))

    def _mult_from_units(self, bet_units: int) -> float:
        """Convertit bet_units en multiplicateur final clampé."""
        mult = bet_units * self.unit_factor * self.leverage_mult
        return max(MIN_MULT, min(self.max_mult, mult))

    # ------------------------------------------------------------------
    # Ceiling marché — cycles de séries
    # ------------------------------------------------------------------

    def _get_ceiling_notional(self, symbol: str) -> float:
        """Retourne le seuil notionnel critique pour ce symbole (mode actif)."""
        sym = self._get_sym(symbol)
        if "ceiling_usdc" in sym:
            return sym["ceiling_usdc"]
        if "ceiling_mode" in sym:
            mode = sym["ceiling_mode"]
            ceilings = MARKET_CEILING_HIGH if mode == "high" else MARKET_CEILING_REALISTIC
            return ceilings.get(symbol.upper(), DEFAULT_CEILING_NOTIONAL)
        # Fallback rétrocompat : utilise MARKET_CEILING_NOTIONAL (mode realistic)
        return MARKET_CEILING_NOTIONAL.get(symbol.upper(), DEFAULT_CEILING_NOTIONAL)

    def _get_current_capital(self, symbol: str) -> float:
        """Retourne active_capital ou estime depuis l'état."""
        sym = self._get_sym(symbol)
        return sym.get("active_capital", sym.get("initial_capital", 0.0))

    def _trigger_ceiling(self, symbol: str, current_capital: float) -> dict:
        """
        Déclenche l'événement CEILING :
          - Retire la margin si active (série 1)
          - Split 50/50 : réserve + base série suivante
          - Reset séquence
        """
        sym = self._get_sym(symbol)

        if sym["margin_active"]:
            net_capital = current_capital - sym["initial_margin"]
            sym["margin_active"] = False
        else:
            net_capital = current_capital

        # Sécurité : net_capital ne peut pas être négatif
        net_capital = max(0.0, net_capital)

        reserve_gain = net_capital * 0.50
        next_capital = net_capital * 0.50

        sym["reserve"]         += reserve_gain
        sym["active_capital"]   = next_capital
        sym["initial_capital"]  = next_capital
        sym["initial_margin"]   = 0.0
        sym["sequence"]         = [1, 1, 1, 1]
        sym["cum_loss_units"]   = 0.0
        sym["series_number"]   += 1

        log.info(
            f"[Labouchère] *** CEILING {symbol} — Série {sym['series_number'] - 1} terminée ***\n"
            f"  Capital net    : {net_capital:.2f} USDC\n"
            f"  → Réserve +    : {reserve_gain:.2f} USDC  (total: {sym['reserve']:.2f})\n"
            f"  → Base série {sym['series_number']} : {next_capital:.2f} USDC\n"
            f"  Margin active  : {sym['margin_active']}"
        )
        self._save()

        return {
            "event":               "ceiling_hit",
            "symbol":              symbol,
            "series_completed":    sym["series_number"] - 1,
            "reserve_added":       reserve_gain,
            "reserve_total":       sym["reserve"],
            "next_series_capital": next_capital,
            "next_series":         sym["series_number"],
        }

    def check_ceiling(
        self,
        symbol:        str,
        next_qty:      float,
        current_price: float,
    ) -> bool:
        """
        Vérifie si la prochaine position atteindrait le seuil critique marché.
        Si oui : déclenche le CEILING EVENT (split capital, reset séquence).

        Retourne True si ceiling atteint (= ne pas placer cet ordre, série terminée).
        """
        sym = self._get_sym(symbol)

        # Priorité : ceiling stocké dans l'état (défini par init_from_ceiling)
        # Sinon : fallback sur MARKET_CEILING_NOTIONAL (rétrocompat)
        if "ceiling_usdc" in sym:
            ceiling = sym["ceiling_usdc"]
        elif "ceiling_mode" in sym:
            mode = sym["ceiling_mode"]
            ceilings = MARKET_CEILING_HIGH if mode == "high" else MARKET_CEILING_REALISTIC
            ceiling = ceilings.get(symbol, DEFAULT_CEILING_NOTIONAL)
        else:
            ceiling = MARKET_CEILING_NOTIONAL.get(symbol, DEFAULT_CEILING_NOTIONAL)

        notional = next_qty * current_price

        if notional >= ceiling:
            log.info(
                f"[Labouchère] CEILING CHECK {symbol}: "
                f"notional={notional:.0f} ≥ ceiling={ceiling:.0f} USDC → CEILING HIT"
            )
            self._trigger_ceiling(symbol, self._get_current_capital(symbol))
            return True

        log.debug(
            f"[Labouchère] CEILING CHECK {symbol}: "
            f"notional={notional:.0f} < ceiling={ceiling:.0f} USDC → OK"
        )
        return False

    # ------------------------------------------------------------------
    # Initialisation série avec margin
    # ------------------------------------------------------------------

    def init_series_with_margin(
        self,
        symbol:  str,
        capital: float,
        margin:  float,
    ):
        """
        Initialise la série 1 avec capital propre + margin.
        À appeler une fois au démarrage ou après un reset manuel.
        """
        sym = self._get_sym(symbol)
        sym["series_number"]   = 1
        sym["margin_active"]   = True
        sym["initial_margin"]  = margin
        sym["initial_capital"] = capital
        sym["active_capital"]  = capital + margin
        sym["reserve"]         = 0.0
        sym["sequence"]        = [1, 1, 1, 1]
        sym["cum_loss_units"]  = 0.0
        self._save()
        log.info(
            f"[Labouchère] {symbol} init série 1 : "
            f"capital={capital} + margin={margin} = {capital + margin} USDC"
        )

    def init_from_ceiling(
        self,
        symbol:       str,
        ceiling_qty:  float,
        price:        float,
        ceiling_mode: str = None,
    ) -> dict:
        """
        Initialise automatiquement à partir du ceiling marché.

        La règle fondamentale : capital_propre = ceiling / 2, margin = ceiling / 2
        Le risque absolu maximum = capital_propre initial (jamais plus).

        Args:
            symbol       : "ETH", "SOL", etc.
            ceiling_qty  : taille critique en unités du token (ex: 25 pour 25 ETH)
                           Si None → utilise MARKET_CEILING selon ceiling_mode
            price        : prix actuel en USDC
            ceiling_mode : "realistic" ou "high". Si None → utilise CEILING_MODE global.

        Exemple :
            labouch.init_from_ceiling("ETH", ceiling_qty=25, price=2000)
            # → capital = 25 000 USDC, margin = 25 000 USDC, ceiling = 50 000 USDC

            labouch.init_from_ceiling("SOL", ceiling_qty=None, price=130)
            # → utilise MARKET_CEILING_HIGH["SOL"] = 500 000
            # → capital = 250 000 USDC, margin = 250 000 USDC
        """
        mode = ceiling_mode or CEILING_MODE

        if ceiling_qty is not None:
            ceiling_usdc = ceiling_qty * price
        else:
            ceilings = MARKET_CEILING_HIGH if mode == "high" else MARKET_CEILING_REALISTIC
            ceiling_usdc = ceilings.get(symbol, DEFAULT_CEILING_NOTIONAL)

        capital = ceiling_usdc / 2
        margin  = ceiling_usdc / 2

        # Met à jour aussi le ceiling du symbole dans l'état
        sym = self._get_sym(symbol)
        sym["ceiling_usdc"] = ceiling_usdc
        sym["ceiling_mode"] = mode

        self.init_series_with_margin(symbol, capital, margin)

        log.info(
            f"[Labouchère] {symbol} init_from_ceiling [{mode}]:\n"
            f"  Ceiling  : {ceiling_usdc:,.0f} USDC notional\n"
            f"  Capital  : {capital:,.0f} USDC propre\n"
            f"  Margin   : {margin:,.0f} USDC\n"
            f"  Risque max absolu ≈ {capital:,.0f} USDC"
        )
        return {"capital": capital, "margin": margin, "ceiling_usdc": ceiling_usdc}

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def get_multiplier(self, symbol: str, capital: float) -> float:
        """
        Retourne le multiplicateur à appliquer à la qty de base.
        Applique les resets mensuels/journaliers si nécessaire.
        Met à jour active_capital avec le capital courant.
        """
        sym = self._get_sym(symbol)
        self._check_monthly_reset(sym)
        self._check_daily_reset(sym, capital)

        # Sync active_capital si jamais non initialisé
        if sym["active_capital"] == 0.0:
            sym["active_capital"] = capital
            sym["initial_capital"] = capital

        bet_units = self._calc_bet_units(sym)
        sym["last_bet_units"] = bet_units
        mult = self._mult_from_units(bet_units)

        log.info(
            f"[Labouchère] {symbol}: seq={sym['sequence']} "
            f"bet={bet_units}u × {self.unit_factor} × {self.leverage_mult} "
            f"= mult={mult:.2f}×"
        )
        self._save()
        return mult

    def on_entry(
        self,
        symbol:  str,
        price:   float,
        qty:     float,
        side:    str,
        capital: float,
    ):
        """
        Enregistre les paramètres de l'entrée en position.
        Doit être appelé APRÈS get_multiplier (pour avoir last_bet_units).
        Met à jour active_capital.
        """
        sym = self._get_sym(symbol)
        sym["last_entry_price"]   = price
        sym["last_entry_qty"]     = qty
        sym["last_entry_side"]    = side
        sym["last_entry_capital"] = capital
        sym["active_capital"]     = capital  # sync capital courant

        if sym.get("daily_start_capital") is None:
            sym["daily_start_capital"] = capital
        if sym.get("daily_date") is None:
            sym["daily_date"] = datetime.utcnow().strftime("%Y-%m-%d")

        log.info(
            f"[Labouchère] {symbol} ENTRÉE: {side} {qty}@{price:.4f}  "
            f"capital={capital:.2f} USDC  bet_units={sym['last_bet_units']}"
        )
        self._save()

    def on_close(self, symbol: str, close_price: float, capital_after: float):
        """
        Met à jour la séquence après fermeture.
        close_price peut être 0 (on utilise alors le delta capital).
        WIN  si capital_after > last_entry_capital
        LOSS sinon
        Met à jour active_capital avec le capital après clôture.
        """
        sym           = self._get_sym(symbol)
        entry_capital = sym.get("last_entry_capital")
        bet_units     = sym.get("last_bet_units", 2)

        # ---------- P&L ----------
        if entry_capital is not None and entry_capital > 0:
            pnl = capital_after - entry_capital
        else:
            pnl = 0.0

        is_win = pnl > 0

        # Mise à jour P&L journalier + total
        sym["daily_pnl"]   = sym.get("daily_pnl", 0.0) + pnl
        sym["total_pnl"]   = sym.get("total_pnl", 0.0) + pnl
        sym["trade_count"] = sym.get("trade_count", 0) + 1

        # Sync active_capital
        sym["active_capital"] = capital_after

        # ---------- Mise à jour séquence ----------
        if is_win:
            # WIN : ajoute bet_units à la fin
            sym["sequence"].append(bet_units)
            log.info(
                f"[Labouchère] {symbol} WIN  pnl={pnl:+.2f} USDC | "
                f"séquence → {sym['sequence']}"
            )
        else:
            # LOSS : retire premier + dernier
            sym["cum_loss_units"] = sym.get("cum_loss_units", 0.0) + bet_units
            seq = sym["sequence"]
            if len(seq) >= 2:
                sym["sequence"] = seq[1:-1]
            elif len(seq) == 1:
                sym["sequence"] = []

            if not sym["sequence"]:
                sym["sequence"] = [1, 1, 1, 1]
                log.info(f"[Labouchère] {symbol} séquence épuisée → reset [1,1,1,1]")

            log.info(
                f"[Labouchère] {symbol} LOSS pnl={pnl:+.2f} USDC | "
                f"cum_loss={sym['cum_loss_units']:.1f}u | "
                f"séquence → {sym['sequence']}"
            )

        # Réinitialise les champs d'entrée
        sym["last_entry_price"]   = None
        sym["last_entry_qty"]     = None
        sym["last_entry_side"]    = None
        sym["last_entry_capital"] = None
        self._save()

    def should_trade(self, symbol: str, capital: float) -> Tuple[bool, str]:
        """
        Vérifie si le trading est autorisé (stop session journalier).
        Retourne (True, "ok") ou (False, raison).
        """
        sym = self._get_sym(symbol)
        self._check_monthly_reset(sym)
        self._check_daily_reset(sym, capital)

        daily_start = sym.get("daily_start_capital") or 0.0
        daily_pnl   = sym.get("daily_pnl", 0.0)

        if daily_start > 0 and daily_pnl < 0:
            loss_pct = (-daily_pnl) / daily_start
            if loss_pct >= self.stop_session_pct:
                reason = (
                    f"Stop session {symbol}: perte journalière "
                    f"{daily_pnl:.2f} USDC ({loss_pct*100:.1f}% ≥ "
                    f"{self.stop_session_pct*100:.0f}%)"
                )
                log.warning(f"[Labouchère] {reason}")
                return False, reason

        return True, "ok"

    def get_status(self, symbol: str) -> dict:
        """Retourne l'état courant du symbole (lecture seule)."""
        sym = self._get_sym(symbol)
        seq = sym.get("sequence") or [1, 1, 1, 1]

        if len(seq) == 1:
            bet_units = seq[0] * 2
        else:
            bet_units = seq[0] + seq[-1]

        bet_units = max(1, int(bet_units))
        mult      = self._mult_from_units(bet_units)

        daily_start = sym.get("daily_start_capital") or 0.0
        daily_pnl   = sym.get("daily_pnl", 0.0)
        daily_loss_pct = (
            (-daily_pnl / daily_start * 100) if daily_start > 0 else 0.0
        )

        return {
            "symbol":           symbol,
            "series_number":    sym.get("series_number", 1),
            "margin_active":    sym.get("margin_active", False),
            "initial_margin":   sym.get("initial_margin", 0.0),
            "active_capital":   round(sym.get("active_capital", 0.0), 2),
            "reserve":          round(sym.get("reserve", 0.0), 2),
            "sequence":         seq,
            "seq_length":       len(seq),
            "bet_units":        bet_units,
            "multiplier":       round(mult, 3),
            "cum_loss_units":   round(sym.get("cum_loss_units", 0.0), 2),
            "daily_pnl":        round(daily_pnl, 2),
            "daily_loss_pct":   round(daily_loss_pct, 2),
            "total_pnl":        round(sym.get("total_pnl", 0.0), 2),
            "trade_count":      sym.get("trade_count", 0),
            "last_month":       sym.get("last_month"),
            "last_entry_side":  sym.get("last_entry_side"),
            "stop_active":      daily_loss_pct >= self.stop_session_pct * 100,
            "ceiling_notional": self._get_ceiling_notional(symbol),
            "ceiling_usdc":     sym.get("ceiling_usdc", DEFAULT_CEILING_NOTIONAL),
            "ceiling_mode":     sym.get("ceiling_mode", CEILING_MODE),
        }

    def get_all_status(self) -> dict:
        """Retourne l'état de tous les symboles connus."""
        return {sym: self.get_status(sym) for sym in self._state}
