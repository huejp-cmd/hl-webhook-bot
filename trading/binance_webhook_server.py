"""
Webhook Server — TradingView → Binance Futures USDT-M
Multi-symbol : SOLUSDT, ETHUSDT (et n'importe quelle paire Binance)

Installation:
    pip install flask python-binance

Démarrage:
    python binance_webhook_server.py

TradingView : URL webhook → http://TON_IP:5000/webhook
Header      : X-Webhook-Token: jp_bot_secret_2026
"""

import json
import logging
import math
from datetime import datetime
from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    ORDER_TYPE_MARKET,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET
)
from binance.exceptions import BinanceAPIException

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION — à remplir avec tes clés API Binance Futures
# ═══════════════════════════════════════════════════════════════
API_KEY       = "REMPLACE_PAR_TON_API_KEY_BINANCE"
API_SECRET    = "REMPLACE_PAR_TON_API_SECRET_BINANCE"
WEBHOOK_TOKEN = "jp_bot_secret_2026"   # même token dans TradingView

# ⚠️ Testnet Binance Futures : https://testnet.binancefuture.com
#    True  = testnet (sans argent réel — RECOMMANDÉ pour les tests)
#    False = MAINNET (argent réel)
TESTNET = True

# Mode simulation : si True, calcule les ordres sans les envoyer
DRY_RUN = True   # ← Mettre False seulement quand prêt pour le live

# Risque par trade (% du solde disponible)
RISK_PCT     = 0.02   # 2%
DEFAULT_LEV  = 2      # Levier par défaut si non fourni par le signal

# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("binance_orders.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Init Binance client ───────────────────────────────────────
if TESTNET:
    client = Client(API_KEY, API_SECRET, testnet=True)
    # Forcer l'URL Futures testnet
    client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
    log.info("📡 Réseau : TESTNET Binance Futures")
else:
    client = Client(API_KEY, API_SECRET)
    log.info("🔴 Réseau : MAINNET Binance Futures (ARGENT RÉEL)")

log.info(f"{'⚠️  MODE DRY RUN — aucun ordre réel' if DRY_RUN else '🚀 MODE LIVE — ordres réels activés'}")


# ═══════════════════════════════════════════════════════════════
#  UTILITAIRES
# ═══════════════════════════════════════════════════════════════

def normalize_symbol(ticker: str) -> str:
    """
    Convertit le ticker TradingView en symbole Binance Futures.
    Exemples :
      SOLUSDT.P         → SOLUSDT
      BINANCE:SOLUSDT.P → SOLUSDT
      ETHUSDT.P         → ETHUSDT
      BTCUSDT           → BTCUSDT
    """
    s = ticker.upper().strip()
    if ":" in s:
        s = s.split(":")[1]
    s = s.replace(".P", "").replace("PERP", "")
    # Ajoute USDT si manquant (ex: SOL → SOLUSDT)
    if not s.endswith("USDT") and not s.endswith("BUSD"):
        s += "USDT"
    return s


def get_symbol_info(symbol: str) -> dict:
    """Récupère les filtres de précision pour un symbole Futures"""
    try:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s
    except Exception as e:
        log.error(f"Erreur get_symbol_info({symbol}): {e}")
    return {}


def get_precision(symbol: str) -> tuple[int, int]:
    """
    Retourne (qty_precision, price_precision) pour arrondir correctement.
    qty_precision  = nombre de décimales pour la quantité
    price_precision = nombre de décimales pour le prix
    """
    info = get_symbol_info(symbol)
    qty_prec   = 3   # défaut
    price_prec = 2   # défaut
    for f in info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            qty_prec = max(0, int(round(-math.log10(step))))
        if f["filterType"] == "PRICE_FILTER":
            tick = float(f["tickSize"])
            price_prec = max(0, int(round(-math.log10(tick))))
    return qty_prec, price_prec


def round_step(value: float, precision: int) -> float:
    """Arrondit à la précision requise par Binance"""
    factor = 10 ** precision
    return math.floor(value * factor) / factor


def get_futures_balance() -> float:
    """Retourne le solde USDT disponible en Futures"""
    try:
        balances = client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
    except Exception as e:
        log.error(f"Erreur get_futures_balance: {e}")
    return 0.0


def get_open_position(symbol: str) -> float:
    """
    Retourne la position ouverte sur ce symbole.
    Positif = long, négatif = short, 0 = pas de position.
    """
    try:
        positions = client.futures_position_information(symbol=symbol)
        for p in positions:
            szi = float(p.get("positionAmt", 0))
            if szi != 0:
                return szi
    except Exception as e:
        log.error(f"Erreur get_open_position({symbol}): {e}")
    return 0.0


def cancel_open_orders(symbol: str):
    """Annule tous les ordres ouverts sur ce symbole (SL/TP obsolètes)"""
    try:
        result = client.futures_cancel_all_open_orders(symbol=symbol)
        log.info(f"  Ordres annulés sur {symbol}: {result}")
    except Exception as e:
        log.error(f"Erreur cancel_open_orders({symbol}): {e}")


# ═══════════════════════════════════════════════════════════════
#  PLACEMENT D'ORDRE
# ═══════════════════════════════════════════════════════════════

def place_order(signal: dict) -> dict:
    """
    Exécute un trade complet sur Binance Futures :
      1. Ferme position inverse si elle existe
      2. Configure le levier (isolated margin)
      3. Ordre market d'entrée
      4. Stop Loss (STOP_MARKET)
      5. Take Profit (TAKE_PROFIT_MARKET)
    """
    raw_symbol = signal["symbol"]
    symbol     = normalize_symbol(raw_symbol)
    side       = signal["side"].lower()     # "buy" ou "sell"
    sl_price   = float(signal["sl"])
    tp_price   = float(signal["tp"])
    entry_px   = float(signal["price"])
    lev        = int(float(signal.get("leverage", DEFAULT_LEV)))

    is_buy = side == "buy"
    side_enum     = SIDE_BUY  if is_buy else SIDE_SELL
    close_side    = SIDE_SELL if is_buy else SIDE_BUY

    log.info(f"\n{'─'*60}")
    log.info(f"{'🟢 LONG' if is_buy else '🔴 SHORT'} {symbol} @ {entry_px}")
    log.info(f"  SL: {sl_price}  |  TP: {tp_price}  |  Levier: {lev}x")

    # ─── Précision symbole ───────────────────────────────────
    qty_prec, price_prec = get_precision(symbol)
    sl_price = round(sl_price, price_prec)
    tp_price = round(tp_price, price_prec)

    # ─── Calcul quantité basée sur le risque ─────────────────
    balance  = get_futures_balance()
    risk_amt = balance * RISK_PCT
    dist     = abs(entry_px - sl_price)
    dist     = dist if dist > 1e-12 else 1e-12
    qty_raw  = (risk_amt * lev) / entry_px
    qty      = round_step(qty_raw, qty_prec)

    log.info(f"  Balance USDT  : {balance:.2f}")
    log.info(f"  Risque        : {risk_amt:.2f} USDT ({RISK_PCT*100:.0f}%)")
    log.info(f"  Quantité      : {qty} {symbol.replace('USDT','')}")

    if qty <= 0:
        log.error("  ❌ Quantité calculée nulle — ordre annulé")
        return {"status": "error", "reason": "qty <= 0"}

    if DRY_RUN:
        log.info("  [DRY RUN] Ordre simulé — non envoyé à Binance")
        return {
            "status":   "dry_run",
            "symbol":   symbol,
            "side":     side,
            "qty":      qty,
            "entry":    entry_px,
            "sl":       sl_price,
            "tp":       tp_price,
            "leverage": lev,
            "balance":  balance,
        }

    # ─── Ferme position inverse ───────────────────────────────
    existing = get_open_position(symbol)
    if (existing > 0 and not is_buy) or (existing < 0 and is_buy):
        log.info(f"  ↩️  Fermeture position inverse : {existing}")
        cancel_open_orders(symbol)
        client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type=ORDER_TYPE_MARKET,
            quantity=abs(existing),
            reduceOnly=True
        )

    # ─── Configure levier (isolated) ─────────────────────────
    try:
        client.futures_change_leverage(symbol=symbol, leverage=lev)
        client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        log.info(f"  Levier configuré : {lev}x ISOLATED")
    except BinanceAPIException as e:
        # Code -4046 = margin type already set → pas grave
        if e.code != -4046:
            log.warning(f"  Warning levier/margin : {e}")

    # ─── Ordre market d'entrée ────────────────────────────────
    order = client.futures_create_order(
        symbol=symbol,
        side=side_enum,
        type=ORDER_TYPE_MARKET,
        quantity=qty
    )
    log.info(f"  ✅ Entrée market : {order.get('orderId')} — filled ~{order.get('avgPrice', entry_px)}")

    # ─── Stop Loss ────────────────────────────────────────────
    sl_order = client.futures_create_order(
        symbol      = symbol,
        side        = close_side,
        type        = FUTURE_ORDER_TYPE_STOP_MARKET,
        stopPrice   = sl_price,
        closePosition = True
    )
    log.info(f"  🛑 Stop Loss  : {sl_order.get('orderId')} @ {sl_price}")

    # ─── Take Profit ──────────────────────────────────────────
    tp_order = client.futures_create_order(
        symbol      = symbol,
        side        = close_side,
        type        = FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
        stopPrice   = tp_price,
        closePosition = True
    )
    log.info(f"  🎯 Take Profit: {tp_order.get('orderId')} @ {tp_price}")

    return {
        "status":     "ok",
        "symbol":     symbol,
        "entry_id":   order.get("orderId"),
        "sl_id":      sl_order.get("orderId"),
        "tp_id":      tp_order.get("orderId"),
    }


# ═══════════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    """Endpoint principal — reçoit les alertes TradingView"""

    # Vérification du token de sécurité
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        log.warning(f"⛔ Token invalide : {token}")
        return jsonify({"error": "unauthorized"}), 401

    try:
        data = request.get_json(force=True)
    except Exception as e:
        log.error(f"JSON invalide : {e}")
        return jsonify({"error": "invalid json"}), 400

    log.info(f"\n{'='*60}")
    log.info(f"📨 Signal reçu : {json.dumps(data, indent=2)}")

    # Validation des champs requis
    required = ["action", "side", "symbol", "price", "sl", "tp"]
    for field in required:
        if field not in data:
            log.error(f"Champ manquant : {field}")
            return jsonify({"error": f"missing field: {field}"}), 400

    if data["action"] != "open":
        log.info(f"Action ignorée : {data['action']}")
        return jsonify({"status": "ignored"}), 200

    try:
        result = place_order(data)
        return jsonify(result), 200
    except BinanceAPIException as e:
        log.error(f"Erreur Binance : {e.code} — {e.message}")
        return jsonify({"error": f"BinanceAPIException {e.code}: {e.message}"}), 500
    except Exception as e:
        log.error(f"Erreur inattendue : {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/status", methods=["GET"])
def status():
    """Santé du serveur — solde + positions ouvertes"""
    balance = get_futures_balance()
    positions = []
    try:
        raw = client.futures_position_information()
        for p in raw:
            szi = float(p.get("positionAmt", 0))
            if szi != 0:
                positions.append({
                    "symbol": p["symbol"],
                    "size":   szi,
                    "pnl":    p.get("unrealizedProfit", "?")
                })
    except Exception as e:
        log.error(f"Erreur positions: {e}")

    return jsonify({
        "status":    "online",
        "mode":      "DRY_RUN" if DRY_RUN else "LIVE",
        "network":   "TESTNET" if TESTNET else "MAINNET",
        "balance":   f"{balance:.2f} USDT",
        "positions": positions,
        "time":      datetime.utcnow().isoformat()
    })


if __name__ == "__main__":
    log.info("🚀 Webhook Binance Futures démarré sur port 5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
