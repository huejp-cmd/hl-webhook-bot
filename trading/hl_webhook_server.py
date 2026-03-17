"""
Webhook Server -- TradingView -> Hyperliquid Perpetuals
Version compatible JP v5.4 (Multi-TF 30M+1H, ordres marche)

Architecture :
  - SOL et ETH en ISOLATED MARGIN separe
  - Chaque symbole a son propre capital alloue (envoye par Pine Script)
  - Les deux positions coexistent sans interference

Strategie d'execution (ENTREE) :
  - Si order_type == "market"  -> market_open direct (v5.4)
  - Si order_type absent/limit -> limite agressif -> 30s -> market fallback (v5.3)

Strategie d'execution (TAKE PROFIT) :
  - TP place en ordre LIMITE au prix cible
  - Thread background surveille pendant TP_LIMIT_TIMEOUT secondes
  - Si non rempli -> annule + close MARKET

Strategie d'execution (STOP LOSS) :
  - Trigger market (declenche market des que le prix croise le SL)

Action "close" (depuis v5.4 Pine) :
  - market_close immediat sur le coin concerne

Installation:
    pip install flask hyperliquid-python-sdk eth-account

Demarrage:
    python hl_webhook_server.py

TradingView Alertes :
    URL    : http://TON_IP:5000/webhook
    Header : X-Webhook-Token: jp_bot_secret_2026
"""

import json
import logging
import math
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

# =============================================================================
#  CONFIGURATION
# =============================================================================
PRIVATE_KEY   = "REMPLACE_PAR_TA_CLE_PRIVEE_METAMASK"
WEBHOOK_TOKEN = "jp_bot_secret_2026"

# True  = Mainnet Hyperliquid (argent reel)
# False = Testnet (test sans risque)
MAINNET = False

# True  = simule sans envoyer d'ordre reel
# False = ordres reels sur Hyperliquid
DRY_RUN = True

# =============================================================================
#  TIMEOUTS LIMIT -> MARKET
# =============================================================================
# Entree : offset du prix limite (fraction du prix)
# 0.0002 = 0.02% sous/au-dessus du marche -> maker fee 0.02%/side
ENTRY_LIMIT_OFFSET  = 0.0002

# Entree : attente avant bascule market (secondes)
ENTRY_LIMIT_TIMEOUT = 30

# TP : offset du prix limite pour le take profit
# 0.0001 = 0.01% -> se remplit facilement quand le prix touche le TP
TP_LIMIT_OFFSET  = 0.0001

# TP : attente avant bascule market (secondes)
# Plus court que l'entree car au TP le prix peut repartir vite
TP_LIMIT_TIMEOUT = 30

# =============================================================================
#  CAPITAL PAR SYMBOLE
# =============================================================================
# Laisser vide = utilise le capital du signal Pine (recommande pour v5.4)
# Forcer : {"SOL": 600.0, "ETH": 400.0}
FORCED_CAPITAL_PER_SYMBOL = {}

DEFAULT_RISK_PCT = 0.02
DEFAULT_LEV      = 2

# =============================================================================

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

# --- Init Hyperliquid ---
account  = Account.from_key(PRIVATE_KEY)
base_url = constants.MAINNET_API_URL if MAINNET else constants.TESTNET_API_URL
info     = Info(base_url, skip_ws=True)
exchange = Exchange(account, base_url)

log.info(f"Wallet  : {account.address}")
log.info(f"Reseau  : {'MAINNET' if MAINNET else 'TESTNET'} Hyperliquid")
log.info(f"Mode    : {'DRY_RUN (simulation)' if DRY_RUN else 'LIVE (ordres reels)'}")


# =============================================================================
#  UTILITAIRES
# =============================================================================

def normalize_coin(ticker: str) -> str:
    """SOLUSDT.P / BINANCE:SOLUSDT.P -> SOL"""
    s = ticker.upper().strip()
    if ":" in s:
        s = s.split(":")[1]
    s = s.replace(".P", "").replace("PERP", "").replace("-PERP", "")
    for suffix in ("USDT", "BUSD", "USD"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s


def get_account_value() -> float:
    try:
        state = info.user_state(account.address)
        return float(state.get("marginSummary", {}).get("accountValue", 0))
    except Exception as e:
        log.error(f"get_account_value: {e}")
        return 0.0


def get_open_position(coin: str) -> float:
    """+ = long, - = short, 0 = rien"""
    try:
        state = info.user_state(account.address)
        for pos in state.get("assetPositions", []):
            p   = pos.get("position", {})
            if p.get("coin") == coin:
                szi = float(p.get("szi", 0))
                if szi != 0:
                    return szi
    except Exception as e:
        log.error(f"get_open_position({coin}): {e}")
    return 0.0


def get_coin_precision(coin: str) -> int:
    try:
        meta = info.meta()
        for asset in meta.get("universe", []):
            if asset.get("name") == coin:
                return asset.get("szDecimals", 3)
    except Exception as e:
        log.error(f"get_coin_precision({coin}): {e}")
    return 3


def round_qty(qty: float, coin: str) -> float:
    prec   = get_coin_precision(coin)
    factor = 10 ** prec
    return math.floor(qty * factor) / factor


def _is_filled(order_resp: dict) -> bool:
    try:
        statuses = order_resp.get("response", {}).get("data", {}).get("statuses", [])
        return any("filled" in s for s in statuses)
    except Exception:
        return False


def _extract_oid(order_resp: dict):
    try:
        status = order_resp.get("response", {}).get("data", {}).get("statuses", [{}])[0]
        return (status.get("resting", {}).get("oid")
                or status.get("filled", {}).get("oid"))
    except Exception:
        return None


def _cancel_order(coin: str, oid):
    try:
        exchange.cancel(coin, oid)
        log.info(f"  Ordre annule (oid={oid})")
    except Exception as e:
        log.warning(f"  Annulation (oid={oid}) : {e}")


def _is_order_still_open(oid) -> bool:
    try:
        open_orders = info.open_orders(account.address)
        return any(o.get("oid") == oid for o in open_orders)
    except Exception:
        return True   # par precaution : suppose toujours ouvert


# =============================================================================
#  ENTREE : LIMITE AGRESSIF -> 30s -> MARKET
# =============================================================================

def _entry_limit_or_market(coin: str, is_buy: bool,
                            qty: float, ref_price: float,
                            force_market: bool = False) -> dict | None:
    """
    force_market=True (v5.4 order_type=market) -> market direct
    force_market=False                          -> limite -> 30s -> market
    """
    if force_market:
        log.info(f"  Entree MARKET direct {'BUY' if is_buy else 'SELL'} {qty} {coin}")
        try:
            result = exchange.market_open(coin, is_buy, qty)
            log.info(f"  Entree market : {result}")
            return result
        except Exception as e:
            log.error(f"  Entree market echouee : {e}")
            return None

    # -- Limite agressif --
    limit_px = round(ref_price * (1 - ENTRY_LIMIT_OFFSET if is_buy
                                   else 1 + ENTRY_LIMIT_OFFSET), 6)
    log.info(f"  Tentative limite {'BUY' if is_buy else 'SELL'} @ {limit_px} "
             f"(ref={ref_price}, offset={ENTRY_LIMIT_OFFSET*100:.3f}%)")
    oid = None
    try:
        resp = exchange.order(coin, is_buy=is_buy, sz=qty,
                              limit_px=limit_px,
                              order_type={"limit": {"tif": "Gtc"}})
        if _is_filled(resp):
            log.info("  Limite remplie immediatement (maker 0.02%)")
            return resp
        oid = _extract_oid(resp)
        log.info(f"  Ordre limite en attente (oid={oid}) -- timeout {ENTRY_LIMIT_TIMEOUT}s")
    except Exception as e:
        log.warning(f"  Ordre limite echoue : {e} -> bascule market")

    # -- Attente --
    if oid is not None:
        deadline = time.time() + ENTRY_LIMIT_TIMEOUT
        while time.time() < deadline:
            time.sleep(3)
            if not _is_order_still_open(oid):
                log.info("  Limite remplie avant timeout (maker 0.02%)")
                return {"status": "filled_limit", "oid": oid}
        log.info(f"  Timeout {ENTRY_LIMIT_TIMEOUT}s -- annulation limite, bascule market")
        _cancel_order(coin, oid)

    # -- Fallback market --
    try:
        log.info(f"  Ordre MARKET {'BUY' if is_buy else 'SELL'} {qty} {coin} (taker 0.05%)")
        result = exchange.market_open(coin, is_buy, qty)
        log.info(f"  Entree market : {result}")
        return result
    except Exception as e:
        log.error(f"  Ordre market echoue : {e}")
        return None


# =============================================================================
#  TAKE PROFIT : LIMITE -> 30s -> MARKET  (thread background)
# =============================================================================

def _tp_limit_or_market_bg(coin: str, is_buy_tp: bool,
                            qty: float, tp_price: float):
    """
    Lance en background (thread) :
    1. Place un ordre limite au prix TP
    2. Attend TP_LIMIT_TIMEOUT secondes
    3. Si non rempli -> annule + market_close

    is_buy_tp : True si la position est SHORT (on achete pour fermer)
                False si la position est LONG  (on vend pour fermer)
    """
    log.info(f"  [TP thread] Limite {'BUY' if is_buy_tp else 'SELL'} @ {tp_price} "
             f"(timeout {TP_LIMIT_TIMEOUT}s)")

    # Offset : on ameliore legerement le prix du TP pour etre maker
    # Long  -> on vend un peu AU-DESSUS du TP (meilleur prix)
    # Short -> on achete un peu EN-DESSOUS du TP
    if is_buy_tp:
        limit_px = round(tp_price * (1 - TP_LIMIT_OFFSET), 6)
    else:
        limit_px = round(tp_price * (1 + TP_LIMIT_OFFSET), 6)

    oid = None
    try:
        resp = exchange.order(
            coin,
            is_buy      = is_buy_tp,
            sz          = qty,
            limit_px    = limit_px,
            order_type  = {"limit": {"tif": "Gtc"}},
            reduce_only = True,
        )
        if _is_filled(resp):
            log.info("  [TP thread] Limite TP remplie immediatement")
            return
        oid = _extract_oid(resp)
        log.info(f"  [TP thread] Ordre TP limite en attente (oid={oid})")
    except Exception as e:
        log.warning(f"  [TP thread] Ordre TP limite echoue : {e} -> market_close")

    # -- Attente --
    if oid is not None:
        deadline = time.time() + TP_LIMIT_TIMEOUT
        while time.time() < deadline:
            time.sleep(3)
            if not _is_order_still_open(oid):
                log.info("  [TP thread] Limite TP remplie avant timeout")
                return
        log.info(f"  [TP thread] Timeout {TP_LIMIT_TIMEOUT}s -- annulation TP limite, market_close")
        _cancel_order(coin, oid)

    # -- Fallback market_close --
    try:
        log.info(f"  [TP thread] market_close {coin}")
        result = exchange.market_close(coin)
        log.info(f"  [TP thread] market_close resultat : {result}")
    except Exception as e:
        log.error(f"  [TP thread] market_close echoue : {e}")


def place_tp_async(coin: str, is_buy_tp: bool, qty: float, tp_price: float):
    """Lance le suivi TP dans un thread daemon (non bloquant)."""
    t = threading.Thread(
        target=_tp_limit_or_market_bg,
        args=(coin, is_buy_tp, qty, tp_price),
        daemon=True,
        name=f"tp-{coin}"
    )
    t.start()
    log.info(f"  Thread TP demarre pour {coin} @ {tp_price}")


# =============================================================================
#  FERMETURE POSITION (action "close" depuis v5.4)
# =============================================================================

def close_position_market(coin: str) -> dict:
    """
    Ferme immediatement la position ouverte sur ce coin au marche.
    Appele quand Pine v5.4 envoie action="close" (TP atteint sur la bougie).
    """
    szi = get_open_position(coin)
    if szi == 0:
        log.warning(f"  close_position_market({coin}) : aucune position ouverte")
        return {"status": "no_position", "coin": coin}

    log.info(f"  Fermeture marche {coin} (position={szi})")

    if DRY_RUN:
        result = {"status": "dry_run_close", "coin": coin, "szi": szi}
        log.info(f"  [DRY RUN] {result}")
        return result

    try:
        result = exchange.market_close(coin)
        log.info(f"  Fermeture marche : {result}")
        return {"status": "closed", "coin": coin, "result": str(result)}
    except Exception as e:
        log.error(f"  Fermeture echouee : {e}")
        return {"status": "error", "coin": coin, "reason": str(e)}


# =============================================================================
#  OUVERTURE DE POSITION
# =============================================================================

def place_order(signal: dict) -> dict:
    """
    Ouvre une position sur Hyperliquid :
    1. Configure levier + isolated margin
    2. Ferme position inverse si elle existe
    3. Entree : market direct (v5.4) OU limite->30s->market (v5.3)
    4. SL : trigger market
    5. TP : limite->30s->market en thread background
    """
    raw_ticker   = signal["symbol"]
    coin         = normalize_coin(raw_ticker)
    side         = signal["side"].lower()
    sl_price     = float(signal["sl"])
    tp_price     = float(signal["tp"])
    entry_px     = float(signal["price"])
    lev          = int(float(signal.get("leverage",  DEFAULT_LEV)))
    risk_pct     = float(signal.get("risk_pct", DEFAULT_RISK_PCT * 100)) / 100
    regime       = signal.get("regime", "?")
    tf_src       = signal.get("tf",       "?")
    dual_tf      = signal.get("dual_tf",  False)
    order_type   = signal.get("order_type", "limit").lower()  # "market" ou "limit"
    force_market = (order_type == "market")

    is_buy = (side == "buy")

    log.info(f"\n{'='*60}")
    log.info(f"{'LONG' if is_buy else 'SHORT'} {coin}  |  regime={regime}  "
             f"tf={tf_src}  dual={dual_tf}  order_type={order_type}")
    log.info(f"  Entree: {entry_px}  SL: {sl_price}  TP: {tp_price}  Levier: {lev}x")

    # -- Capital --
    if coin in FORCED_CAPITAL_PER_SYMBOL:
        capital = FORCED_CAPITAL_PER_SYMBOL[coin]
        log.info(f"  Capital (force)  : {capital:.2f} USDC")
    elif "capital" in signal:
        capital = float(signal["capital"])
        log.info(f"  Capital (signal) : {capital:.2f} USDC")
    else:
        capital = get_account_value()
        log.info(f"  Capital (compte) : {capital:.2f} USDC")

    # -- Quantite --
    risk_amt      = capital * risk_pct
    qty_from_risk = (risk_amt * lev) / entry_px
    qty_cap_expo  = (capital * lev) / entry_px
    qty_raw       = min(qty_from_risk, qty_cap_expo)
    qty           = round_qty(qty_raw, coin)

    log.info(f"  Risque  : {risk_amt:.2f} USDC ({risk_pct*100:.0f}%)  "
             f"Quantite : {qty} {coin}")

    if qty <= 0:
        msg = f"Quantite nulle pour {coin} -- ordre annule"
        log.error(f"  {msg}")
        return {"status": "error", "reason": msg}

    # -- Mode simulation --
    if DRY_RUN:
        result = {
            "status":       "dry_run",
            "coin":         coin,
            "side":         side,
            "order_type":   order_type,
            "qty":          qty,
            "entry":        entry_px,
            "sl":           sl_price,
            "tp":           tp_price,
            "leverage":     lev,
            "capital":      capital,
            "risk_usdc":    round(risk_amt, 2),
            "notional":     round(qty * entry_px, 2),
            "tf":           tf_src,
            "dual_tf":      dual_tf,
        }
        log.info(f"  [DRY RUN]\n{json.dumps(result, indent=4)}")
        return result

    # -- Levier + isolated margin --
    try:
        lev_result = exchange.update_leverage(lev, coin, is_cross=False)
        log.info(f"  Levier {lev}x ISOLATED configure : {lev_result}")
    except Exception as e:
        log.warning(f"  Levier warning ({coin}): {e}")

    # -- Ferme position inverse --
    existing = get_open_position(coin)
    if (existing > 0 and not is_buy) or (existing < 0 and is_buy):
        log.info(f"  Position inverse ({existing}) -> fermeture...")
        try:
            exchange.market_close(coin)
        except Exception as e:
            log.error(f"  Fermeture inverse echouee : {e}")

    # -- Entree --
    order = _entry_limit_or_market(coin, is_buy, qty, entry_px,
                                   force_market=force_market)
    if order is None:
        raise RuntimeError(f"Echec entree sur {coin}")

    # -- Stop Loss (trigger market) --
    try:
        sl_order = exchange.order(
            coin,
            is_buy      = not is_buy,
            sz          = qty,
            limit_px    = sl_price,
            order_type  = {"trigger": {"triggerPx": sl_price,
                                       "isMarket": True, "tpsl": "sl"}},
            reduce_only = True,
        )
        log.info(f"  SL trigger market @ {sl_price} : {sl_order}")
    except Exception as e:
        log.error(f"  Erreur SL : {e}")

    # -- Take Profit (limite -> 30s -> market, thread background) --
    # is_buy_tp : pour fermer un long on vend, pour fermer un short on achete
    place_tp_async(coin, is_buy_tp=(not is_buy), qty=qty, tp_price=tp_price)

    return {
        "status":     "ok",
        "coin":       coin,
        "side":       side,
        "order_type": order_type,
        "qty":        qty,
        "entry":      entry_px,
        "sl":         sl_price,
        "tp":         tp_price,
        "tf":         tf_src,
    }


# =============================================================================
#  ENDPOINTS FLASK
# =============================================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    """Endpoint principal -- reçoit les alertes TradingView"""

    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        log.warning(f"Token invalide : {token!r}")
        return jsonify({"error": "unauthorized"}), 401

    try:
        data = request.get_json(force=True)
    except Exception as e:
        log.error(f"JSON invalide : {e}")
        return jsonify({"error": "invalid json"}), 400

    log.info(f"\n{'='*60}")
    log.info(f"Signal recu :\n{json.dumps(data, indent=2)}")

    action = data.get("action", "").lower()

    # ----------------------------------------------------------------
    #  ACTION "open" -- nouvelle position
    # ----------------------------------------------------------------
    if action == "open":
        required = ["side", "symbol", "price", "sl", "tp"]
        for field in required:
            if field not in data:
                log.error(f"Champ manquant : {field}")
                return jsonify({"error": f"missing field: {field}"}), 400
        try:
            result = place_order(data)
            return jsonify(result), 200
        except Exception as e:
            log.error(f"Erreur place_order : {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    # ----------------------------------------------------------------
    #  ACTION "close" -- fermeture marche (TP atteint cote Pine v5.4)
    # ----------------------------------------------------------------
    elif action == "close":
        if "symbol" not in data:
            return jsonify({"error": "missing field: symbol"}), 400
        coin = normalize_coin(data["symbol"])
        log.info(f"Action close recue pour {coin} (raison: {data.get('reason','?')})")
        try:
            result = close_position_market(coin)
            return jsonify(result), 200
        except Exception as e:
            log.error(f"Erreur close : {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    # ----------------------------------------------------------------
    #  AUTRE ACTION -- ignoree
    # ----------------------------------------------------------------
    else:
        log.info(f"Action ignoree : {action!r}")
        return jsonify({"status": "ignored", "action": action}), 200


@app.route("/status", methods=["GET"])
def status():
    """Sante du serveur -- solde + positions ouvertes"""
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
        "status":              "online",
        "wallet":              account.address,
        "mode":                "DRY_RUN" if DRY_RUN else "LIVE",
        "network":             "MAINNET" if MAINNET else "TESTNET",
        "account_value":       f"{account_val:.2f} USDC",
        "open_positions":      positions,
        "entry_limit_timeout": f"{ENTRY_LIMIT_TIMEOUT}s",
        "tp_limit_timeout":    f"{TP_LIMIT_TIMEOUT}s",
        "time":                datetime.utcnow().isoformat() + "Z",
    })


@app.route("/position/<coin>", methods=["GET"])
def position_for_coin(coin):
    """Detail d'une position -- ex: /position/SOL"""
    c   = coin.upper()
    szi = get_open_position(c)
    return jsonify({
        "coin":     c,
        "position": szi,
        "side":     "LONG" if szi > 0 else "SHORT" if szi < 0 else "NONE",
    })


@app.route("/close/<coin>", methods=["POST"])
def manual_close(coin):
    """Fermeture manuelle d'urgence -- ex: POST /close/SOL"""
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    c      = coin.upper()
    result = close_position_market(c)
    return jsonify(result), 200


if __name__ == "__main__":
    log.info("Webhook Hyperliquid demarre -> http://0.0.0.0:5000")
    log.info("Coins supportes : SOL, ETH (et tout coin Hyperliquid)")
    log.info("Margin type     : ISOLATED par coin")
    log.info(f"DRY_RUN={DRY_RUN}  MAINNET={MAINNET}")
    log.info(f"Entry timeout   : {ENTRY_LIMIT_TIMEOUT}s")
    log.info(f"TP timeout      : {TP_LIMIT_TIMEOUT}s")
    app.run(host="0.0.0.0", port=5000, debug=False)
