"""
Webhook Server — TradingView → Binance Futures USDT-M
Base : JP v5.3 — Multi-Symbol (SOL + ETH en parallèle)

Architecture :
  - SOL et ETH fonctionnent en ISOLATED MARGIN séparé
  - Chaque symbole a son propre capital alloué (envoyé par le Pine Script)
  - Les deux positions peuvent coexister simultanément sans interférence
  - Le risque par trade (risk_pct) est appliqué sur le capital du symbole concerné

Installation:
    pip install flask python-binance

Démarrage:
    python binance_webhook_server.py

TradingView Alertes (une par chart) :
    URL    : http://TON_IP:5000/webhook
    Header : X-Webhook-Token: jp_bot_secret_2026
    Body   : {{strategy.order.alert_message}}  (ou coller le message Pine directement)
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
    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
)
from binance.exceptions import BinanceAPIException

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════
API_KEY       = "REMPLACE_PAR_TON_API_KEY_BINANCE"
API_SECRET    = "REMPLACE_PAR_TON_API_SECRET_BINANCE"
WEBHOOK_TOKEN = "jp_bot_secret_2026"   # ← même valeur dans TradingView

# Réseau
# True  = Binance Futures Testnet (aucun argent réel — RECOMMANDÉ pour valider)
# False = MAINNET (argent réel)
TESTNET = True

# Mode simulation
# True  = calcule et affiche les ordres sans les envoyer
# False = envoie les ordres réels sur Binance
DRY_RUN = True

# ═══════════════════════════════════════════════════════════════
#  GESTION DU CAPITAL PAR SYMBOLE (Margin Isolé)
#
#  Chaque symbole possède un budget indépendant.
#  Le capital est lu depuis le signal Pine Script ("capital" field).
#  Exemple : SOL chart → capital=500$, ETH chart → capital=500$
#  → Les deux peuvent être actifs simultanément sans conflit.
#
#  Si tu veux forcer un capital fixe côté serveur (ignore le signal),
#  décommente FORCED_CAPITAL_PER_SYMBOL et ajuste les valeurs.
# ═══════════════════════════════════════════════════════════════
# FORCED_CAPITAL_PER_SYMBOL = {
#     "SOLUSDT": 500.0,
#     "ETHUSDT": 500.0,
# }
FORCED_CAPITAL_PER_SYMBOL = {}   # vide = utilise le capital du signal

DEFAULT_RISK_PCT = 0.02    # 2% si le signal ne précise pas risk_pct
DEFAULT_LEV      = 2       # Levier si le signal ne précise pas leverage

# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("binance_orders.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Init client Binance ──────────────────────────────────────
if TESTNET:
    client = Client(API_KEY, API_SECRET, testnet=True)
    client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
    log.info("📡 Réseau : TESTNET Binance Futures")
else:
    client = Client(API_KEY, API_SECRET)
    log.info("🔴 Réseau : MAINNET Binance Futures (ARGENT RÉEL)")

log.info(f"{'⚠️  MODE DRY RUN — simulation sans ordre réel' if DRY_RUN else '🚀 MODE LIVE — ordres réels activés'}")


# ═══════════════════════════════════════════════════════════════
#  UTILITAIRES
# ═══════════════════════════════════════════════════════════════

def normalize_symbol(ticker: str) -> str:
    """
    TradingView envoie : SOLUSDT.P / BINANCE:SOLUSDT.P / SOLUSDT
    Binance attend    : SOLUSDT
    """
    s = ticker.upper().strip()
    if ":" in s:
        s = s.split(":")[1]
    s = s.replace(".P", "").replace("PERP", "").replace("-PERP", "")
    if not s.endswith("USDT") and not s.endswith("BUSD"):
        s += "USDT"
    return s


_symbol_info_cache: dict = {}

def get_symbol_filters(symbol: str) -> dict:
    """Cache des filtres Binance pour éviter les appels répétés"""
    if symbol in _symbol_info_cache:
        return _symbol_info_cache[symbol]
    try:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                filters = {f["filterType"]: f for f in s.get("filters", [])}
                _symbol_info_cache[symbol] = filters
                return filters
    except Exception as e:
        log.error(f"Erreur get_symbol_filters({symbol}): {e}")
    return {}


def get_precision(symbol: str) -> tuple:
    """
    Retourne (qty_precision, price_precision) pour ce symbole.
    Ex: SOLUSDT → (2, 2), ETHUSDT → (3, 2)
    """
    filters    = get_symbol_filters(symbol)
    qty_prec   = 3
    price_prec = 2
    lot = filters.get("LOT_SIZE")
    if lot:
        step = float(lot["stepSize"])
        if step > 0:
            qty_prec = max(0, int(round(-math.log10(step))))
    pf = filters.get("PRICE_FILTER")
    if pf:
        tick = float(pf["tickSize"])
        if tick > 0:
            price_prec = max(0, int(round(-math.log10(tick))))
    return qty_prec, price_prec


def floor_to_precision(value: float, precision: int) -> float:
    """Arrondit vers le bas à la précision requise par Binance (évite les rejets)"""
    factor = 10 ** precision
    return math.floor(value * factor) / factor


def get_futures_balance() -> float:
    """Solde USDT disponible en Futures (cross wallet)"""
    try:
        for b in client.futures_account_balance():
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
    except Exception as e:
        log.error(f"Erreur get_futures_balance: {e}")
    return 0.0


def get_open_position(symbol: str) -> float:
    """
    Taille de la position ouverte sur ce symbole.
    + = long, - = short, 0 = rien.
    """
    try:
        for p in client.futures_position_information(symbol=symbol):
            szi = float(p.get("positionAmt", 0))
            if szi != 0:
                return szi
    except Exception as e:
        log.error(f"Erreur get_open_position({symbol}): {e}")
    return 0.0


def cancel_open_orders(symbol: str):
    """Annule tous les ordres conditionnels ouverts (SL/TP périmés)"""
    try:
        result = client.futures_cancel_all_open_orders(symbol=symbol)
        log.info(f"  ✓ Ordres ouverts annulés pour {symbol}")
    except Exception as e:
        log.warning(f"  cancel_open_orders({symbol}): {e}")


def set_isolated_margin(symbol: str, leverage: int):
    """
    Configure le margin isolé et le levier pour ce symbole.
    Margin isolé = si la position est liquidée, seul le margin alloué
    à CE symbole est perdu — les autres positions (ETH, SOL...) ne sont
    pas affectées.
    """
    try:
        client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        log.info(f"  ✓ Margin ISOLATED configuré pour {symbol}")
    except BinanceAPIException as e:
        if e.code == -4046:
            pass   # Déjà en isolated — OK
        else:
            log.warning(f"  Margin type warning ({symbol}): {e.message}")
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
        log.info(f"  ✓ Levier configuré : {leverage}x pour {symbol}")
    except BinanceAPIException as e:
        log.warning(f"  Levier warning ({symbol}): {e.message}")


# ═══════════════════════════════════════════════════════════════
#  PLACEMENT D'ORDRE
# ═══════════════════════════════════════════════════════════════

def place_order(signal: dict) -> dict:
    """
    Exécute un trade complet sur Binance Futures en isolated margin :
      1. Configure margin isolé + levier pour ce symbole
      2. Ferme la position inverse si elle existe (évite le hedge)
      3. Ordre market d'entrée
      4. Stop Loss   (STOP_MARKET, closePosition=True)
      5. Take Profit (TAKE_PROFIT_MARKET, closePosition=True)
    """
    raw_symbol = signal["symbol"]
    symbol     = normalize_symbol(raw_symbol)
    side       = signal["side"].lower()          # "buy" ou "sell"
    sl_price   = float(signal["sl"])
    tp_price   = float(signal["tp"])
    entry_px   = float(signal["price"])
    lev        = int(float(signal.get("leverage",  DEFAULT_LEV)))
    risk_pct   = float(signal.get("risk_pct", DEFAULT_RISK_PCT * 100)) / 100
    regime     = signal.get("regime", "?")

    is_buy     = (side == "buy")
    side_enum  = SIDE_BUY  if is_buy else SIDE_SELL
    close_enum = SIDE_SELL if is_buy else SIDE_BUY

    log.info(f"\n{'─'*60}")
    log.info(f"{'🟢 LONG' if is_buy else '🔴 SHORT'} {symbol}  régime={regime}")
    log.info(f"  Entrée: {entry_px}  |  SL: {sl_price}  |  TP: {tp_price}  |  Levier: {lev}x")

    # ─── Précisions Binance pour ce symbole ──────────────────
    qty_prec, price_prec = get_precision(symbol)
    sl_price = round(sl_price, price_prec)
    tp_price = round(tp_price, price_prec)

    # ─── Capital alloué à ce symbole ─────────────────────────
    # Priorité : FORCED_CAPITAL > signal "capital" > balance disponible
    if symbol in FORCED_CAPITAL_PER_SYMBOL:
        capital = FORCED_CAPITAL_PER_SYMBOL[symbol]
        log.info(f"  Capital (forcé)   : {capital:.2f} USDT")
    elif "capital" in signal:
        capital = float(signal["capital"])
        log.info(f"  Capital (signal)  : {capital:.2f} USDT")
    else:
        capital = get_futures_balance()
        log.info(f"  Capital (balance) : {capital:.2f} USDT")

    # ─── Calcul quantité basée sur le risque et le capital ───
    # Formule : qty = (capital × risk_pct × leverage) / prix_entrée
    # Capped   : max exposition = capital × leverage / prix
    risk_amt       = capital * risk_pct
    qty_from_risk  = (risk_amt * lev) / entry_px
    qty_cap_expo   = (capital * lev) / entry_px
    qty_raw        = min(qty_from_risk, qty_cap_expo)
    qty            = floor_to_precision(qty_raw, qty_prec)

    log.info(f"  Risque            : {risk_amt:.2f} USDT ({risk_pct*100:.0f}%)")
    log.info(f"  Quantité calculée : {qty} ({symbol.replace('USDT', '')})")

    # Vérif minimum notional (Binance exige ~5 USDT min)
    notional = qty * entry_px
    log.info(f"  Notional          : {notional:.2f} USDT")
    if notional < 5.0:
        msg = f"Notional trop faible ({notional:.2f} USDT < 5 USDT) — ordre ignoré"
        log.error(f"  ❌ {msg}")
        return {"status": "error", "reason": msg}

    if qty <= 0:
        msg = "Quantité nulle — ordre annulé"
        log.error(f"  ❌ {msg}")
        return {"status": "error", "reason": msg}

    # ─── Mode simulation ──────────────────────────────────────
    if DRY_RUN:
        result = {
            "status":   "dry_run",
            "symbol":   symbol,
            "side":     side,
            "qty":      qty,
            "entry":    entry_px,
            "sl":       sl_price,
            "tp":       tp_price,
            "leverage": lev,
            "capital":  capital,
            "notional": round(notional, 2),
            "risk_usdt":round(risk_amt, 2),
        }
        log.info(f"  [DRY RUN] {json.dumps(result, indent=4)}")
        return result

    # ─── Margin isolé + levier (par symbole) ─────────────────
    # ⚠️  Doit être fait AVANT de placer l'ordre
    set_isolated_margin(symbol, lev)

    # ─── Ferme position inverse si elle existe ───────────────
    # Ex: si un LONG SOL est ouvert et un signal SHORT SOL arrive
    # → on ferme le LONG avant d'ouvrir le SHORT
    existing = get_open_position(symbol)
    if (existing > 0 and not is_buy) or (existing < 0 and is_buy):
        log.info(f"  ↩️  Position inverse détectée ({existing}) — fermeture...")
        cancel_open_orders(symbol)
        try:
            client.futures_create_order(
                symbol    = symbol,
                side      = close_enum,
                type      = ORDER_TYPE_MARKET,
                quantity  = abs(existing),
                reduceOnly= True,
            )
            log.info(f"  ✓ Position inverse fermée")
        except BinanceAPIException as e:
            log.error(f"  Erreur fermeture inverse: {e.message}")

    # ─── Annule les SL/TP obsolètes pour ce symbole ──────────
    cancel_open_orders(symbol)

    # ─── Ordre market d'entrée ────────────────────────────────
    try:
        order = client.futures_create_order(
            symbol   = symbol,
            side     = side_enum,
            type     = ORDER_TYPE_MARKET,
            quantity = qty,
        )
        filled_price = float(order.get("avgPrice", entry_px))
        log.info(f"  ✅ Entrée market  : orderId={order['orderId']}  avgPrice={filled_price}")
    except BinanceAPIException as e:
        log.error(f"  ❌ Erreur entrée  : {e.code} — {e.message}")
        raise

    # ─── Stop Loss ────────────────────────────────────────────
    try:
        sl_order = client.futures_create_order(
            symbol        = symbol,
            side          = close_enum,
            type          = FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice     = sl_price,
            closePosition = True,
        )
        log.info(f"  🛑 Stop Loss      : orderId={sl_order['orderId']}  @ {sl_price}")
    except BinanceAPIException as e:
        log.error(f"  ❌ Erreur SL      : {e.code} — {e.message}")

    # ─── Take Profit ──────────────────────────────────────────
    try:
        tp_order = client.futures_create_order(
            symbol        = symbol,
            side          = close_enum,
            type          = FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice     = tp_price,
            closePosition = True,
        )
        log.info(f"  🎯 Take Profit    : orderId={tp_order['orderId']}  @ {tp_price}")
    except BinanceAPIException as e:
        log.error(f"  ❌ Erreur TP      : {e.code} — {e.message}")

    return {
        "status":      "ok",
        "symbol":      symbol,
        "side":        side,
        "qty":         qty,
        "filled_price":filled_price,
        "sl":          sl_price,
        "tp":          tp_price,
        "entry_id":    order["orderId"],
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
    except BinanceAPIException as e:
        log.error(f"Erreur Binance API : {e.code} — {e.message}")
        return jsonify({"error": f"BinanceAPIError {e.code}: {e.message}"}), 500
    except Exception as e:
        log.error(f"Erreur inattendue : {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/status", methods=["GET"])
def status():
    """Santé du serveur — solde + positions ouvertes par symbole"""
    balance   = get_futures_balance()
    positions = []
    try:
        raw = client.futures_position_information()
        for p in raw:
            szi = float(p.get("positionAmt", 0))
            if szi != 0:
                positions.append({
                    "symbol":  p["symbol"],
                    "size":    szi,
                    "side":    "LONG" if szi > 0 else "SHORT",
                    "pnl":     p.get("unrealizedProfit", "?"),
                    "margin":  p.get("isolatedWallet", "?"),
                    "leverage":p.get("leverage", "?"),
                })
    except Exception as e:
        log.error(f"Erreur positions: {e}")

    return jsonify({
        "status":         "online",
        "mode":           "DRY_RUN" if DRY_RUN else "LIVE",
        "network":        "TESTNET" if TESTNET else "MAINNET",
        "balance_usdt":   f"{balance:.2f}",
        "open_positions": positions,
        "time":           datetime.utcnow().isoformat() + "Z",
    })


@app.route("/positions/<symbol>", methods=["GET"])
def position_for_symbol(symbol):
    """Détail d'une position spécifique — ex: /positions/SOLUSDT"""
    sym = normalize_symbol(symbol)
    szi = get_open_position(sym)
    return jsonify({
        "symbol":   sym,
        "position": szi,
        "side":     "LONG" if szi > 0 else "SHORT" if szi < 0 else "NONE",
    })


if __name__ == "__main__":
    log.info("🚀 Webhook Binance Futures démarré → http://0.0.0.0:5000")
    log.info(f"   Symbols supportés : SOLUSDT, ETHUSDT (et autres Binance Futures)")
    log.info(f"   Margin type       : ISOLATED par symbole")
    log.info(f"   DRY_RUN={DRY_RUN} | TESTNET={TESTNET}")
    app.run(host="0.0.0.0", port=5000, debug=False)
