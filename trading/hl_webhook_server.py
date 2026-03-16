"""
Webhook Server — TradingView → Hyperliquid Perpetuals
Base : JP v5.3 — Multi-Symbol (SOL + ETH en parallèle)

Architecture :
  - SOL et ETH fonctionnent en ISOLATED MARGIN séparé
  - Chaque symbole a son propre capital alloué (envoyé par le Pine Script)
  - Les deux positions peuvent coexister simultanément sans interférence
  - Pas de KYC, connexion par clé privée MetaMask/wallet

Stratégie d'exécution des ordres :
  1. Tente d'abord un ordre LIMITE agressif (maker fee 0.02%)
     → Long  : limit = prix × (1 - LIMIT_OFFSET)  (légèrement sous le marché)
     → Short : limit = prix × (1 + LIMIT_OFFSET)  (légèrement au-dessus)
  2. Attend LIMIT_TIMEOUT secondes
  3. Si non rempli → annule + bascule automatiquement en ordre MARKET (taker 0.05%)

  Économie estimée : 0.06% × 2 sides = 0.12% de frais en moins par rapport
  à du tout-market, soit +~10 000 USDC sur 2 ans sur SOL+ETH combinés.

Installation:
    pip install flask hyperliquid-python-sdk eth-account

Démarrage:
    python hl_webhook_server.py

TradingView Alertes (une par chart) :
    URL    : http://TON_IP:5000/webhook
    Header : X-Webhook-Token: jp_bot_secret_2026
"""

import json
import logging
import math
import time
from datetime import datetime
from flask import Flask, request, jsonify

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════
PRIVATE_KEY   = "REMPLACE_PAR_TA_CLE_PRIVEE_METAMASK"   # ⚠️ Ne jamais partager !
WEBHOOK_TOKEN = "jp_bot_secret_2026"

# Réseau
# True  = Mainnet Hyperliquid (argent réel)
# False = Testnet  (pour valider sans risque)
MAINNET = False   # ← Mettre True seulement quand prêt pour le live

# Mode simulation
# True  = calcule et log les ordres sans les envoyer
# False = envoie les ordres réels sur Hyperliquid
DRY_RUN = True

# ═══════════════════════════════════════════════════════════════
#  STRATÉGIE D'EXÉCUTION — Limite agressif → Market fallback
# ═══════════════════════════════════════════════════════════════
# Offset du prix pour l'ordre limite (en fraction du prix)
# 0.0002 = 0.02% sous/au-dessus du marché → rempli comme maker
# → fee 0.02%/side au lieu de 0.05%/side taker
LIMIT_OFFSET  = 0.0002   # 0.02% — ajustable si trop souvent non rempli

# Délai d'attente avant de basculer en market (secondes)
LIMIT_TIMEOUT = 30       # 30s — ajustable (20-60s recommandé)

# ═══════════════════════════════════════════════════════════════
#  GESTION DU CAPITAL PAR SYMBOLE (Margin Isolé)
#
#  Le capital est lu depuis le signal Pine Script ("capital" field).
#  Chaque symbole (SOL, ETH) a son propre budget indépendant.
#
#  Pour forcer un capital fixe côté serveur (ignore le signal),
#  décommente et ajuste FORCED_CAPITAL_PER_SYMBOL :
# ═══════════════════════════════════════════════════════════════
# FORCED_CAPITAL_PER_SYMBOL = {
#     "SOL": 500.0,
#     "ETH": 500.0,
# }
FORCED_CAPITAL_PER_SYMBOL = {}   # vide = utilise le capital du signal Pine

DEFAULT_RISK_PCT = 0.02   # 2% si le signal ne précise pas risk_pct
DEFAULT_LEV      = 2      # Levier par défaut si absent du signal

# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("hl_orders.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Init Hyperliquid ──────────────────────────────────────────
account  = Account.from_key(PRIVATE_KEY)
base_url = constants.MAINNET_API_URL if MAINNET else constants.TESTNET_API_URL
info     = Info(base_url, skip_ws=True)
exchange = Exchange(account, base_url)

log.info(f"✅ Wallet : {account.address}")
log.info(f"📡 Réseau : {'MAINNET' if MAINNET else 'TESTNET'} Hyperliquid")
log.info(f"{'⚠️  MODE DRY RUN — simulation sans ordre réel' if DRY_RUN else '🚀 MODE LIVE — ordres réels activés'}")


# ═══════════════════════════════════════════════════════════════
#  UTILITAIRES
# ═══════════════════════════════════════════════════════════════

def normalize_coin(ticker: str) -> str:
    """
    TradingView envoie : SOLUSDT.P / BINANCE:SOLUSDT.P / SOLUSDT
    Hyperliquid attend : SOL / ETH / BTC
    """
    s = ticker.upper().strip()
    if ":" in s:
        s = s.split(":")[1]
    s = s.replace(".P", "").replace("PERP", "").replace("-PERP", "")
    # Supprime le suffixe USDT / BUSD
    for suffix in ("USDT", "BUSD", "USD"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s   # "SOL", "ETH", "BTC"...


def get_account_value() -> float:
    """Valeur totale du compte (USDC) sur Hyperliquid"""
    try:
        state = info.user_state(account.address)
        return float(state.get("marginSummary", {}).get("accountValue", 0))
    except Exception as e:
        log.error(f"Erreur get_account_value: {e}")
        return 0.0


def get_open_position(coin: str) -> float:
    """
    Taille de la position ouverte sur ce coin.
    + = long, - = short, 0 = rien.
    """
    try:
        state = info.user_state(account.address)
        for pos in state.get("assetPositions", []):
            p = pos.get("position", {})
            if p.get("coin") == coin:
                szi = float(p.get("szi", 0))
                if szi != 0:
                    return szi
    except Exception as e:
        log.error(f"Erreur get_open_position({coin}): {e}")
    return 0.0


def get_coin_precision(coin: str) -> int:
    """
    Retourne le nombre de décimales pour la quantité de ce coin.
    Hyperliquid publie les specs dans /meta.
    """
    try:
        meta = info.meta()
        for asset in meta.get("universe", []):
            if asset.get("name") == coin:
                sz_decimals = asset.get("szDecimals", 3)
                return sz_decimals
    except Exception as e:
        log.error(f"Erreur get_coin_precision({coin}): {e}")
    return 3   # défaut


def round_qty(qty: float, coin: str) -> float:
    """Arrondit la quantité à la précision requise par Hyperliquid"""
    prec   = get_coin_precision(coin)
    factor = 10 ** prec
    return math.floor(qty * factor) / factor


def close_position(coin: str, existing_szi: float):
    """Ferme une position existante via market order"""
    is_buy = existing_szi < 0   # short existant → on achète pour fermer
    qty    = abs(existing_szi)
    log.info(f"  ↩️  Fermeture position {coin} {existing_szi} → {'BUY' if is_buy else 'SELL'} {qty}")
    if not DRY_RUN:
        result = exchange.market_close(coin)
        log.info(f"  Fermeture : {result}")


# ═══════════════════════════════════════════════════════════════
#  PLACEMENT D'ORDRE
# ═══════════════════════════════════════════════════════════════

def _entry_limit_or_market(coin: str, is_buy: bool,
                            qty: float, ref_price: float) -> dict | None:
    """
    Stratégie d'exécution en deux temps :

    1. Ordre LIMITE agressif (maker fee 0.02%/side)
       → Long  : limit_px = ref_price × (1 - LIMIT_OFFSET)
       → Short : limit_px = ref_price × (1 + LIMIT_OFFSET)
       Le prix est légèrement défavorable → passe en maker, se remplit
       dans les premières secondes sur un marché liquide (SOL/ETH).

    2. Si non rempli après LIMIT_TIMEOUT secondes :
       → Annule l'ordre limite
       → Bascule en ordre MARKET (taker fee 0.05%/side)

    Retourne le résultat de l'ordre exécuté, ou None si tout échoue.
    """
    # ── Calcul du prix limite ──────────────────────────────────
    if is_buy:
        limit_px = round(ref_price * (1 - LIMIT_OFFSET), 6)
    else:
        limit_px = round(ref_price * (1 + LIMIT_OFFSET), 6)

    log.info(f"  📋 Tentative limite {'BUY' if is_buy else 'SELL'} @ {limit_px} "
             f"(ref={ref_price}, offset={LIMIT_OFFSET*100:.3f}%)")

    # ── Place l'ordre limite GTC ───────────────────────────────
    try:
        resp = exchange.order(
            coin,
            is_buy     = is_buy,
            sz         = qty,
            limit_px   = limit_px,
            order_type = {"limit": {"tif": "Gtc"}},
        )
        # Extrait l'order ID
        oid = None
        if isinstance(resp, dict):
            status = resp.get("response", {}).get("data", {}).get("statuses", [{}])[0]
            oid = status.get("resting", {}).get("oid") or status.get("filled", {}).get("oid")

        # Vérifie si déjà rempli immédiatement
        if oid is None or _is_filled(resp):
            log.info(f"  ✅ Limite remplie immédiatement (maker 0.02%)")
            return resp

        log.info(f"  ⏳ Ordre limite en attente (oid={oid}) — timeout {LIMIT_TIMEOUT}s...")

    except Exception as e:
        log.warning(f"  ⚠️  Ordre limite échoué : {e} → bascule market")
        oid = None

    # ── Attente + vérification du remplissage ─────────────────
    if oid is not None:
        deadline = time.time() + LIMIT_TIMEOUT
        while time.time() < deadline:
            time.sleep(3)
            try:
                open_orders = info.open_orders(account.address)
                still_open  = any(o.get("oid") == oid for o in open_orders)
                if not still_open:
                    log.info(f"  ✅ Limite remplie avant timeout (maker 0.02%)")
                    return {"status": "filled_limit", "oid": oid}
            except Exception:
                pass

        # ── Timeout atteint : annule le limite ─────────────────
        log.info(f"  ⏰ Timeout {LIMIT_TIMEOUT}s — annulation limite, bascule market")
        try:
            exchange.cancel(coin, oid)
            log.info(f"  ✓ Ordre limite annulé (oid={oid})")
        except Exception as e:
            log.warning(f"  Annulation échouée (peut déjà être rempli) : {e}")

    # ── Fallback : ordre MARKET ────────────────────────────────
    try:
        log.info(f"  🔄 Ordre MARKET {'BUY' if is_buy else 'SELL'} {qty} {coin} (taker 0.05%)")
        result = exchange.market_open(coin, is_buy, qty)
        log.info(f"  ✅ Entrée market : {result}")
        return result
    except Exception as e:
        log.error(f"  ❌ Ordre market échoué : {e}")
        return None


def _is_filled(order_resp: dict) -> bool:
    """Vérifie si la réponse d'ordre indique un remplissage immédiat."""
    try:
        statuses = order_resp.get("response", {}).get("data", {}).get("statuses", [])
        for s in statuses:
            if "filled" in s:
                return True
    except Exception:
        pass
    return False


def place_order(signal: dict) -> dict:
    """
    Exécute un trade complet sur Hyperliquid en isolated margin :
      1. Configure levier + isolated margin pour ce coin
      2. Ferme la position inverse si elle existe
      3. Ordre market d'entrée
      4. Stop Loss   (trigger order, reduce_only)
      5. Take Profit (trigger order, reduce_only)

    SOL et ETH sont totalement indépendants :
      - chaque coin a son propre margin isolé
      - une liquidation SOL ne touche pas ETH et vice-versa
    """
    raw_ticker = signal["symbol"]
    coin       = normalize_coin(raw_ticker)
    side       = signal["side"].lower()           # "buy" ou "sell"
    sl_price   = float(signal["sl"])
    tp_price   = float(signal["tp"])
    entry_px   = float(signal["price"])
    lev        = int(float(signal.get("leverage",  DEFAULT_LEV)))
    risk_pct   = float(signal.get("risk_pct", DEFAULT_RISK_PCT * 100)) / 100
    regime     = signal.get("regime", "?")

    is_buy     = (side == "buy")

    log.info(f"\n{'─'*60}")
    log.info(f"{'🟢 LONG' if is_buy else '🔴 SHORT'} {coin}  régime={regime}")
    log.info(f"  Entrée: {entry_px}  |  SL: {sl_price}  |  TP: {tp_price}  |  Levier: {lev}x")

    # ─── Capital alloué à ce coin ─────────────────────────────
    if coin in FORCED_CAPITAL_PER_SYMBOL:
        capital = FORCED_CAPITAL_PER_SYMBOL[coin]
        log.info(f"  Capital (forcé)   : {capital:.2f} USDC")
    elif "capital" in signal:
        capital = float(signal["capital"])
        log.info(f"  Capital (signal)  : {capital:.2f} USDC")
    else:
        capital = get_account_value()
        log.info(f"  Capital (compte)  : {capital:.2f} USDC")

    # ─── Calcul quantité ──────────────────────────────────────
    risk_amt      = capital * risk_pct
    qty_from_risk = (risk_amt * lev) / entry_px
    qty_cap_expo  = (capital * lev) / entry_px
    qty_raw       = min(qty_from_risk, qty_cap_expo)
    qty           = round_qty(qty_raw, coin)

    log.info(f"  Risque            : {risk_amt:.2f} USDC ({risk_pct*100:.0f}%)")
    log.info(f"  Quantité          : {qty} {coin}")

    if qty <= 0:
        msg = f"Quantité nulle pour {coin} — ordre annulé"
        log.error(f"  ❌ {msg}")
        return {"status": "error", "reason": msg}

    # ─── Mode simulation ──────────────────────────────────────
    if DRY_RUN:
        result = {
            "status":    "dry_run",
            "coin":      coin,
            "side":      side,
            "qty":       qty,
            "entry":     entry_px,
            "sl":        sl_price,
            "tp":        tp_price,
            "leverage":  lev,
            "capital":   capital,
            "risk_usdc": round(risk_amt, 2),
            "notional":  round(qty * entry_px, 2),
        }
        log.info(f"  [DRY RUN]\n{json.dumps(result, indent=4)}")
        return result

    # ─── Configure levier + isolated margin ───────────────────
    # is_cross=False → isolated margin
    # Chaque coin est totalement isolé → SOL liquidé ≠ touche ETH
    try:
        lev_result = exchange.update_leverage(lev, coin, is_cross=False)
        log.info(f"  ✓ Levier {lev}x ISOLATED configuré pour {coin} : {lev_result}")
    except Exception as e:
        log.warning(f"  Levier warning ({coin}): {e}")

    # ─── Ferme position inverse si elle existe ────────────────
    existing = get_open_position(coin)
    if (existing > 0 and not is_buy) or (existing < 0 and is_buy):
        log.info(f"  Position inverse détectée ({existing}) — fermeture...")
        close_position(coin, existing)

    # ─── Entrée : Limite agressif → Market fallback ───────────
    order = _entry_limit_or_market(coin, is_buy, qty, entry_px)
    if order is None:
        raise RuntimeError(f"Échec entrée sur {coin} (limite + market ont échoué)")

    # ─── Stop Loss ────────────────────────────────────────────
    try:
        sl_order = exchange.order(
            coin,
            is_buy      = not is_buy,
            sz          = qty,
            limit_px    = sl_price,
            order_type  = {"trigger": {"triggerPx": sl_price, "isMarket": True, "tpsl": "sl"}},
            reduce_only = True,
        )
        log.info(f"  🛑 Stop Loss   @ {sl_price} : {sl_order}")
    except Exception as e:
        log.error(f"  ❌ Erreur SL   : {e}")

    # ─── Take Profit ──────────────────────────────────────────
    try:
        tp_order = exchange.order(
            coin,
            is_buy      = not is_buy,
            sz          = qty,
            limit_px    = tp_price,
            order_type  = {"trigger": {"triggerPx": tp_price, "isMarket": True, "tpsl": "tp"}},
            reduce_only = True,
        )
        log.info(f"  🎯 Take Profit @ {tp_price} : {tp_order}")
    except Exception as e:
        log.error(f"  ❌ Erreur TP   : {e}")

    return {
        "status": "ok",
        "coin":   coin,
        "side":   side,
        "qty":    qty,
        "entry":  entry_px,
        "sl":     sl_price,
        "tp":     tp_price,
    }

    return {
        "status": "ok",
        "coin":   coin,
        "side":   side,
        "qty":    qty,
        "entry":  entry_px,
        "sl":     sl_price,
        "tp":     tp_price,
    }


# ═══════════════════════════════════════════════════════════════
#  ENDPOINTS FLASK
# ═══════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    """Endpoint principal — reçoit les alertes TradingView"""

    # Vérification du token de sécurité
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        log.warning(f"⛔ Token invalide : {token!r}")
        return jsonify({"error": "unauthorized"}), 401

    try:
        data = request.get_json(force=True)
    except Exception as e:
        log.error(f"JSON invalide : {e}")
        return jsonify({"error": "invalid json"}), 400

    log.info(f"\n{'='*60}")
    log.info(f"📨 Signal reçu :\n{json.dumps(data, indent=2)}")

    # Validation des champs requis
    required = ["action", "side", "symbol", "price", "sl", "tp"]
    for field in required:
        if field not in data:
            log.error(f"Champ manquant : {field}")
            return jsonify({"error": f"missing field: {field}"}), 400

    if data["action"] != "open":
        log.info(f"Action ignorée : {data['action']}")
        return jsonify({"status": "ignored", "action": data["action"]}), 200

    try:
        result = place_order(data)
        return jsonify(result), 200
    except Exception as e:
        log.error(f"Erreur inattendue : {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/status", methods=["GET"])
def status():
    """Santé du serveur — solde + positions ouvertes"""
    account_val = get_account_value()
    positions   = []
    try:
        state = info.user_state(account.address)
        for pos in state.get("assetPositions", []):
            p   = pos.get("position", {})
            szi = float(p.get("szi", 0))
            if szi != 0:
                positions.append({
                    "coin":     p.get("coin"),
                    "size":     szi,
                    "side":     "LONG" if szi > 0 else "SHORT",
                    "pnl":      p.get("unrealizedPnl", "?"),
                    "leverage": p.get("leverage", {}).get("value", "?"),
                    "margin":   p.get("marginUsed", "?"),
                    "isolated": p.get("leverage", {}).get("type") == "isolated",
                })
    except Exception as e:
        log.error(f"Erreur positions: {e}")

    return jsonify({
        "status":         "online",
        "wallet":         account.address,
        "mode":           "DRY_RUN" if DRY_RUN else "LIVE",
        "network":        "MAINNET" if MAINNET else "TESTNET",
        "account_value":  f"{account_val:.2f} USDC",
        "open_positions": positions,
        "time":           datetime.utcnow().isoformat() + "Z",
    })


@app.route("/position/<coin>", methods=["GET"])
def position_for_coin(coin):
    """Détail d'une position — ex: /position/SOL ou /position/ETH"""
    c   = coin.upper()
    szi = get_open_position(c)
    return jsonify({
        "coin":     c,
        "position": szi,
        "side":     "LONG" if szi > 0 else "SHORT" if szi < 0 else "NONE",
    })


if __name__ == "__main__":
    log.info("🚀 Webhook Hyperliquid démarré → http://0.0.0.0:5000")
    log.info("   Coins supportés : SOL, ETH (et tout coin Hyperliquid)")
    log.info("   Margin type     : ISOLATED par coin")
    log.info(f"  DRY_RUN={DRY_RUN} | MAINNET={MAINNET}")
    app.run(host="0.0.0.0", port=5000, debug=False)
