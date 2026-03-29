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
import os
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

from labouch_manager import LabouchManager
labouch = LabouchManager()

# =============================================================================
#  CONFIGURATION
# =============================================================================
PRIVATE_KEY   = os.environ.get("PRIVATE_KEY", "")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "jp_bot_secret_2026")

# =============================================================================
#  TRADE LOG
# =============================================================================
TRADE_LOG_FILE = os.environ.get("TRADE_LOG_FILE", "/tmp/trade_log.json")
TRADE_LOG_MAX  = 500   # entrées max conservées en mémoire/fichier

# Log en mémoire (Railway : /tmp peut être éphémère)
_trade_log_memory: list = []

# Positions virtuelles DRY_RUN (coin -> {entry, qty, side, capital, tp, sl, ts})
_dry_positions: dict = {}

# True  = Mainnet Hyperliquid (argent reel)
# False = Testnet (test sans risque)
MAINNET = True

# True  = simule sans envoyer d'ordre reel
# False = ordres reels sur Hyperliquid
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"  # env ou True par défaut

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
# Capital de base par symbole (JP : SOL=600, ETH=400)
# Le bot applique un effet compound REEL en scalant la qty selon l'equity du compte
BASE_CAPITAL_PER_SYMBOL = {"SOL": 500.0, "ETH": 500.0}
TOTAL_BASE_CAPITAL      = 1000.0   # SOL 500 + ETH 500
FORCED_CAPITAL_PER_SYMBOL = {}     # Laisser vide = compound reel actif

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

# --- Init Hyperliquid (lazy) ---
account  = Account.from_key(PRIVATE_KEY) if PRIVATE_KEY else None
base_url = constants.MAINNET_API_URL if MAINNET else constants.TESTNET_API_URL

_info     = None
_exchange = None

def get_info():
    global _info
    if _info is None:
        _info = Info(base_url, skip_ws=True)
    return _info

def get_exchange():
    global _exchange
    if _exchange is None:
        _exchange = Exchange(account, base_url)
    return _exchange

# Alias pour compatibilite
info     = type('LazyInfo', (), {
    '__getattr__': lambda self, name: getattr(get_info(), name)
})()
exchange = type('LazyExchange', (), {
    '__getattr__': lambda self, name: getattr(get_exchange(), name)
})()

log.info(f"Wallet  : {account.address}")
log.info(f"Reseau  : {'MAINNET' if MAINNET else 'TESTNET'} Hyperliquid")
log.info(f"Mode    : {'DRY_RUN (simulation)' if DRY_RUN else 'LIVE (ordres reels)'}")


# =============================================================================
#  UTILITAIRES
# =============================================================================

# =============================================================================
#  LOGGING DES TRADES
# =============================================================================

def log_trade_result(symbol: str, side: str, entry_price: float, exit_price: float,
                     pnl_usdc: float, pnl_pct: float, reason: str, labouch_state: dict):
    """
    Enregistre le résultat d'un trade fermé dans le log JSON persistant.

    Args:
        symbol      : "ETH", "SOL", etc.
        side        : "buy" (long fermé) ou "sell" (short fermé)
        entry_price : prix d'entrée
        exit_price  : prix de sortie
        pnl_usdc    : PnL net en USDC (estimé si non fourni)
        pnl_pct     : PnL en pourcentage
        reason      : "tp", "sl", "close", "manual", "ceiling", etc.
        labouch_state : dict retourné par labouch.get_status(symbol)
    """
    global _trade_log_memory
    try:
        lab = labouch_state or {}
        entry = {
            "ts":         datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            "symbol":     symbol,
            "side":       side,
            "entry":      round(entry_price, 6) if entry_price else 0.0,
            "exit":       round(exit_price, 6) if exit_price else 0.0,
            "pnl_usdc":   round(pnl_usdc, 2),
            "pnl_pct":    round(pnl_pct, 4),
            "reason":     reason,
            "series":     lab.get("series_number", 0),
            "sequence":   lab.get("sequence", []),
            "multiplier": lab.get("multiplier", 1.0),
            "capital":    round(float(lab.get("active_capital", 0.0)), 2),
        }

        # 1. Stocker en mémoire (survit tant que le process tourne)
        _trade_log_memory.append(entry)
        if len(_trade_log_memory) > TRADE_LOG_MAX:
            _trade_log_memory = _trade_log_memory[-TRADE_LOG_MAX:]

        # 2. Persister sur disque (/tmp ou chemin configuré)
        try:
            existing = []
            if os.path.exists(TRADE_LOG_FILE):
                with open(TRADE_LOG_FILE, "r") as f:
                    existing = json.load(f)
            existing.append(entry)
            if len(existing) > TRADE_LOG_MAX:
                existing = existing[-TRADE_LOG_MAX:]
            with open(TRADE_LOG_FILE, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as disk_err:
            log.warning(f"[trade_log] Écriture disque impossible : {disk_err} — log mémoire OK")

        log.info(f"[trade_log] {symbol} {side.upper()} {reason.upper()} "
                 f"entry={entry_price} exit={exit_price} "
                 f"PnL={pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%)")
    except Exception as e:
        log.error(f"[trade_log] Erreur log_trade_result : {e}")


def _get_trade_log(n: int = 50) -> list:
    """Retourne les N derniers trades (mémoire + disque fusionnés)."""
    combined = list(_trade_log_memory)
    # Essayer de compléter depuis le disque si mémoire vide (redémarrage)
    if not combined and os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE, "r") as f:
                combined = json.load(f)
        except Exception:
            pass
    return combined[-n:]


def round_price(x: float) -> float:
    """Arrondit un prix à 5 chiffres significatifs (exigence Hyperliquid)."""
    if x == 0:
        return 0.0
    d = math.ceil(math.log10(abs(x)))
    factor = 10 ** (5 - d)
    return round(x * factor) / factor


def normalize_coin(ticker: str, price: float = None) -> str:
    """SOLUSDT.P / BINANCE:SOLUSDT.P -> SOL
    Fallback sur le prix si ticker non résolu (ex: {{ticker}} Pine Script)"""
    s = ticker.upper().strip()
    # Placeholder non résolu par TradingView → fallback sur le prix
    if "{" in s or "}" in s:
        if price is not None:
            if price > 500:
                log.warning(f"Ticker non résolu '{ticker}' → ETH (prix={price})")
                return "ETH"
            else:
                log.warning(f"Ticker non résolu '{ticker}' → SOL (prix={price})")
                return "SOL"
        log.error(f"Ticker non résolu '{ticker}' et pas de prix — impossible de déterminer le coin")
        return "UNKNOWN"
    if ":" in s:
        s = s.split(":")[1]
    s = s.replace(".P", "").replace("PERP", "").replace("-PERP", "")
    for suffix in ("USDT", "BUSD", "USD"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s


def get_account_value() -> float:
    """Retourne l'equity du compte PERP uniquement (capital + margin positions ouvertes).
    Le spot USDC est une réserve — il n'entre pas dans le calcul du compound."""
    try:
        state = info.user_state(account.address)
        # accountValue = balance perp disponible + margin utilisée + PnL non réalisé
        perp_equity = float(state.get("marginSummary", {}).get("accountValue", 0))
        if perp_equity > 0:
            return perp_equity
        # Fallback : lire le spot USDC si le compte perp est vide
        spot_state = info.spot_user_state(account.address)
        spot_usdc = next(
            (float(b["total"]) for b in spot_state.get("balances", []) if b["coin"] == "USDC"),
            0.0,
        )
        return spot_usdc
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

def place_tp_trigger(coin: str, is_buy_tp: bool, qty: float, tp_price: float):
    """
    Place un ordre Take Profit trigger natif Hyperliquid (tpsl='tp').
    Géré côté exchange → reste ouvert jusqu'au prix cible, sans timer.

    is_buy_tp : True si SHORT (on achète pour fermer)
                False si LONG  (on vend pour fermer)
    """
    # limit_px = prix worst-case avec 5% slippage (garantit execution)
    if is_buy_tp:
        limit_px = round_price(tp_price * 0.95)   # SHORT TP : on achète, limit en dessous
    else:
        limit_px = round_price(tp_price * 1.05)   # LONG TP  : on vend, limit au dessus

    log.info(f"  TP trigger {'BUY' if is_buy_tp else 'SELL'} @ {tp_price} (limit_px={limit_px})")
    try:
        resp = exchange.order(
            coin,
            is_buy      = is_buy_tp,
            sz          = qty,
            limit_px    = limit_px,
            order_type  = {"trigger": {"triggerPx": tp_price,
                                       "isMarket": True, "tpsl": "tp"}},
            reduce_only = True,
        )
        log.info(f"  TP trigger placé : {resp}")
        return resp
    except Exception as e:
        log.error(f"  Erreur TP trigger : {e}")


def place_tp_async(coin: str, is_buy_tp: bool, qty: float, tp_price: float):
    """Place le TP trigger natif Hyperliquid (non bloquant)."""
    t = threading.Thread(
        target=place_tp_trigger,
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
    if DRY_RUN:
        pos = _dry_positions.pop(coin, None)
        szi = pos["qty"] if pos else 0
        result = {"status": "dry_run_close", "coin": coin, "szi": szi, "virtual_pos": pos}
        log.info(f"  [DRY RUN CLOSE] {result}")
        return result

    szi = get_open_position(coin)
    if szi == 0:
        log.warning(f"  close_position_market({coin}) : aucune position ouverte")
        return {"status": "no_position", "coin": coin}

    log.info(f"  Fermeture marche {coin} (position={szi})")

    try:
        result = exchange.market_close(coin)
        log.info(f"  Fermeture marche : {result}")
        cap_after = get_account_value()
        labouch.on_close(coin, 0, cap_after)
        log.info(f"  Labouchère: {labouch.get_status(coin)}")
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
    entry_px_raw = float(signal.get("price", 0))
    coin         = normalize_coin(raw_ticker, price=entry_px_raw)
    side         = signal["side"].lower()
    sl_price     = round_price(float(signal["sl"]))
    tp_price     = round_price(float(signal["tp"]))
    entry_px     = round_price(float(signal["price"]))
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
        # Fallback DRY_RUN : si capital = 0 (pas de clé privée), utiliser BASE_CAPITAL
        if capital <= 0 and DRY_RUN:
            capital = BASE_CAPITAL_PER_SYMBOL.get(coin, 500.0)
            log.info(f"  Capital (DRY_RUN fallback) : {capital:.2f} USDC")

    # -- Quantite (v5.5 : Pine qty + compound reel sur equity du compte) --
    pine_qty = float(signal.get("qty", 0))
    if pine_qty > 0 and coin in BASE_CAPITAL_PER_SYMBOL:
        # Compound reel : scale la qty Pine selon l'equity actuelle du compte
        base_capital  = BASE_CAPITAL_PER_SYMBOL[coin]          # ex: 600 SOL
        real_equity   = get_account_value()                     # equity reelle HL
        coin_alloc    = base_capital / TOTAL_BASE_CAPITAL       # 0.60 pour SOL
        real_capital  = real_equity * coin_alloc                # capital reel alloue
        real_capital  = max(real_capital, base_capital * 0.5)  # plancher 50%
        scale_factor  = real_capital / base_capital
        qty           = round_qty(pine_qty * scale_factor, coin)
        risk_amt      = real_capital * risk_pct
        log.info(f"  Compound reel    : equity={real_equity:.2f} USDC → "
                 f"capital {coin}={real_capital:.2f} USDC (x{scale_factor:.2f}) "
                 f"→ qty {pine_qty} × {scale_factor:.2f} = {qty} {coin}")
    elif pine_qty > 0:
        qty      = round_qty(pine_qty, coin)
        risk_amt = capital * risk_pct
        log.info(f"  Quantite (Pine)  : {qty} {coin}")
    else:
        # Fallback : calcul risk-based
        risk_amt      = capital * risk_pct
        qty_from_risk = (risk_amt * lev) / entry_px
        qty_cap_expo  = (capital * lev) / entry_px
        qty_raw       = min(qty_from_risk, qty_cap_expo)
        qty           = round_qty(qty_raw, coin)
        log.info(f"  Quantite (calc)  : {qty} {coin}")

    if qty <= 0:
        msg = f"Quantite nulle pour {coin} -- ordre annule"
        log.error(f"  {msg}")
        return {"status": "error", "reason": msg}

    # --- Labouchère ---
    ok, reason = labouch.should_trade(coin, capital)
    if not ok:
        return {"status": "skipped_labouch", "reason": reason}
    lab_mult = labouch.get_multiplier(coin, capital)
    qty = round_qty(qty * lab_mult, coin)
    log.info(f"  Labouchère mult={lab_mult:.2f}x → qty={qty} {coin}")

    # Vérifier ceiling AVANT d'ouvrir la position
    if labouch.check_ceiling(coin, qty, entry_px):
        ceiling_info = labouch.get_status(coin)
        msg = f"🎯 CEILING {coin} — Série terminée. Réserve: {ceiling_info['reserve']:.0f} USDC. Nouvelle base: {ceiling_info['active_capital']:.0f} USDC"
        log.info(f"  {msg}")
        return {"status": "ceiling_hit", "details": ceiling_info}

    labouch.on_entry(coin, entry_px, qty, side, capital)

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
        # Tracker la position virtuelle pour log quand elle ferme
        _dry_positions[coin] = {
            "entry": entry_px, "qty": qty, "side": side,
            "capital": capital, "tp": tp_price, "sl": sl_price,
            "ts": datetime.utcnow().isoformat() + "Z"
        }
        return result

    # -- Levier + isolated margin --
    try:
        lev_result = exchange.update_leverage(lev, coin, is_cross=False)
        log.info(f"  Levier {lev}x ISOLATED configure : {lev_result}")
    except Exception as e:
        log.warning(f"  Levier warning ({coin}): {e}")

    # -- Vérifie position existante --
    existing = get_open_position(coin)
    same_direction = (existing > 0 and is_buy) or (existing < 0 and not is_buy)
    if same_direction:
        msg = f"Position {coin} déjà ouverte dans le même sens ({existing}) — signal ignoré"
        log.warning(f"  {msg}")
        return {"status": "skipped", "reason": msg, "existing_position": existing}

    # -- Ferme position inverse --
    if (existing > 0 and not is_buy) or (existing < 0 and is_buy):
        log.info(f"  Position inverse ({existing}) -> fermeture...")
        try:
            exchange.market_close(coin)
        except Exception as e:
            log.error(f"  Fermeture inverse echouee : {e}")

    # -- Log entrée réelle (comparaison avec déclenchement intrabar théorique) --
    real_ts   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    real_side = "long" if is_buy else "short"
    _log_real_entry(coin, real_side, entry_px, real_ts)
    # Calculer et logger l'écart si on avait un déclenchement intrabar
    key = f"{coin}_{real_side}"
    if key in _intrabar_first:
        ib    = _intrabar_first.pop(key)
        delta = entry_px - ib["price"]
        pct   = (delta / ib["price"]) * 100 if ib["price"] else 0
        direction = "plus cher" if (is_buy and delta > 0) or (not is_buy and delta < 0) else "moins cher"
        log.info(f"  [COMPARAISON] {coin} {real_side.upper()}")
        log.info(f"    Intrabar : {ib['price']} @ {ib['time']}")
        log.info(f"    Réel     : {entry_px}    @ {real_ts}")
        log.info(f"    Écart    : {delta:+.4f} ({pct:+.3f}%) — entrée réelle {direction}")
        with open(INTRABAR_LOG, "a") as f:
            f.write(f"comparison,{coin},{real_side},{ib['price']},{entry_px},{delta:.4f},{pct:.3f},{ib['time']},{real_ts}\n")

    # -- Entree --
    order = _entry_limit_or_market(coin, is_buy, qty, entry_px,
                                   force_market=force_market)
    if order is None:
        raise RuntimeError(f"Echec entree sur {coin}")

    # -- Stop Loss (trigger market) --
    # limit_px = prix worst-case avec 10% slippage pour garantir l'execution
    sl_is_buy = not is_buy
    sl_limit_px = round_price(sl_price * 1.10 if sl_is_buy else sl_price * 0.90)
    sl_price    = round_price(sl_price)
    try:
        sl_order = exchange.order(
            coin,
            is_buy      = sl_is_buy,
            sz          = qty,
            limit_px    = sl_limit_px,
            order_type  = {"trigger": {"triggerPx": sl_price,
                                       "isMarket": True, "tpsl": "sl"}},
            reduce_only = True,
        )
        log.info(f"  SL trigger market @ {sl_price} (limit_px={sl_limit_px}) : {sl_order}")
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

@app.route("/webhook/jp_bot_secret_2026", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    """Endpoint principal -- reçoit les alertes TradingView"""

    # Route /webhook/jp_bot_secret_2026 = token dans le chemin (pour TradingView)
    token_in_path = request.path == "/webhook/jp_bot_secret_2026"
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if not token_in_path and token != WEBHOOK_TOKEN:
        log.warning(f"Token invalide : {token!r}")
        return jsonify({"error": "unauthorized"}), 401

    try:
        raw = request.get_data(as_text=True)
        log.info(f"Body brut recu : {raw[:500]!r}")
        data = json.loads(raw)
    except Exception as e:
        # Texte brut (ex: alerte Max Drawdown) — ignorer proprement
        log.warning(f"Body non-JSON ignore : {raw[:200]!r}")
        return jsonify({"status": "ignored", "reason": "not json"}), 200

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
        # Répondre immédiatement à TradingView pour éviter le timeout
        # L'ordre est traité en arrière-plan
        def _process():
            try:
                place_order(data)
            except Exception as e:
                log.error(f"Erreur place_order (bg) : {e}", exc_info=True)
        threading.Thread(target=_process, daemon=True).start()
        return jsonify({"status": "received"}), 200

    # ----------------------------------------------------------------
    #  ACTION "close" -- fermeture marche (TP atteint cote Pine v5.4)
    # ----------------------------------------------------------------
    elif action == "close":
        if "coin" not in data and "symbol" not in data:
            return jsonify({"error": "missing field: coin or symbol"}), 400
        coin = data.get("coin") or normalize_coin(data["symbol"])
        msg  = data.get("msg", "")
        pnl  = data.get("pnl_pct", "?")
        entry = data.get("entry", "?")
        exit_ = data.get("exit", "?")
        equity = data.get("equity", "?")
        log.info(f"")
        log.info(f"{'='*60}")
        log.info(f"CLOTURE {coin} | {data.get('side','?').upper()}")
        log.info(f"  Entree : {entry}")
        log.info(f"  Sortie : {exit_}")
        log.info(f"  PnL    : {pnl}%")
        log.info(f"  Equity : {equity} USDC")
        if msg:
            log.info(f"  >>> {msg}")
        try:
            result = close_position_market(coin)
            result["pnl_pct"] = pnl
            result["msg"] = msg

            # --- Log trade result ---
            try:
                side_str   = data.get("side", "?")
                entry_f    = float(entry) if entry not in ("?", None, "") else 0.0
                exit_f     = float(exit_) if exit_ not in ("?", None, "") else 0.0
                pnl_pct_f  = float(pnl)   if pnl   not in ("?", None, "") else 0.0
                # Capital : depuis position virtuelle DRY_RUN, ou labouch state (lecture locale)
                equity_f   = float(data.get("equity", 0.0) or 0.0)
                dry_pos    = result.get("virtual_pos") or {}
                capital_f  = float(dry_pos.get("capital", 0.0)) if dry_pos else 0.0
                lab_state  = labouch.get_status(coin)  # lecture locale uniquement
                if capital_f <= 0:
                    capital_f = float(lab_state.get("active_capital", 0.0)) or equity_f or 1000.0
                pnl_usdc_f = capital_f * pnl_pct_f / 100.0
                msg_lower  = msg.lower()
                if "tp" in msg_lower or "take profit" in msg_lower or "profit" in msg_lower:
                    trade_reason = "tp"
                elif "sl" in msg_lower or "stop loss" in msg_lower or "stop" in msg_lower:
                    trade_reason = "sl"
                else:
                    trade_reason = data.get("reason", "close")
                log_trade_result(coin, side_str, entry_f, exit_f,
                                 pnl_usdc_f, pnl_pct_f, trade_reason, lab_state)
            except Exception as le:
                log.warning(f"[trade_log] Échec log action close : {le}")
            # --- Fin log ---

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


# =============================================================================
#  CONDITIONS DASHBOARD
# =============================================================================

# Stockage en mémoire : dernières conditions reçues par symbole
_conditions: dict = {}

# Log CSV des déclenchements théoriques intrabar vs réels
INTRABAR_LOG = "intrabar_vs_real.csv"

# Suivi du premier déclenchement intrabar par symbole (réinitialisé à chaque signal réel)
_intrabar_first: dict = {}  # symbol -> {"time": ..., "price": ..., "side": ...}

def _log_intrabar(symbol: str, side: str, price: float, ts: str):
    """Enregistre le premier moment où toutes les conditions sont vertes (intrabar)."""
    import os
    write_header = not os.path.exists(INTRABAR_LOG)
    with open(INTRABAR_LOG, "a") as f:
        if write_header:
            f.write("type,symbol,side,price,timestamp\n")
        f.write(f"intrabar,{symbol},{side},{price},{ts}\n")
    log.info(f"  [INTRABAR] Premier déclenchement théorique {side} {symbol} @ {price} ({ts})")

def _log_real_entry(symbol: str, side: str, price: float, ts: str):
    """Enregistre l'entrée réelle (candle close) pour comparaison."""
    with open(INTRABAR_LOG, "a") as f:
        f.write(f"real_entry,{symbol},{side},{price},{ts}\n")
    log.info(f"  [REAL] Entrée réelle {side} {symbol} @ {price} ({ts})")

@app.route("/conditions", methods=["POST"])
def receive_conditions():
    """Reçoit l'état des conditions depuis TradingView (Pine alertcondition)."""
    token = request.headers.get("X-Webhook-Token") or request.args.get("token")
    if token != WEBHOOK_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    try:
        data = json.loads(request.get_data(as_text=True))
    except Exception as e:
        return jsonify({"error": f"invalid json: {e}"}), 400

    symbol = normalize_coin(data.get("symbol", "UNKNOWN"))
    ts     = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    data["symbol"]     = symbol
    data["updated_at"] = datetime.utcnow().strftime("%H:%M:%S UTC")

    prev = _conditions.get(symbol, {})
    _conditions[symbol] = data

    # --- Détection premier déclenchement intrabar ---
    # Si valid_long ou valid_short passe à True pour la première fois depuis
    # le dernier signal réel → on enregistre l'heure et le prix
    side = None
    if data.get("valid_long")  and not prev.get("valid_long"):
        side = "long"
    elif data.get("valid_short") and not prev.get("valid_short"):
        side = "short"

    if side:
        # Ne logger qu'une fois par signal (jusqu'à la prochaine entrée réelle)
        key = f"{symbol}_{side}"
        if key not in _intrabar_first:
            price = float(data.get("price", 0))
            _intrabar_first[key] = {"time": ts, "price": price, "side": side}
            _log_intrabar(symbol, side, price, ts)

    log.info(f"Conditions reçues pour {symbol} | long={data.get('valid_long')} short={data.get('valid_short')}")
    return jsonify({"status": "ok", "symbol": symbol}), 200


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Dashboard HTML temps réel — SOL et ETH côte à côte."""
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JP Trading Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d0d1a;
    color: #e0e0e0;
    font-family: 'Courier New', monospace;
    padding: 16px;
  }
  h1 {
    text-align: center;
    color: #7eb8f7;
    font-size: 1.1rem;
    margin-bottom: 4px;
    letter-spacing: 2px;
  }
  #updated {
    text-align: center;
    color: #555;
    font-size: 0.72rem;
    margin-bottom: 14px;
  }
  .grid {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    justify-content: center;
  }
  .card {
    background: #13132a;
    border: 1px solid #2a2a4a;
    border-radius: 10px;
    padding: 14px;
    min-width: 310px;
    flex: 1;
    max-width: 460px;
  }
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
    padding-bottom: 8px;
    border-bottom: 1px solid #2a2a4a;
  }
  .symbol {
    font-size: 1.3rem;
    font-weight: bold;
    color: #f0c040;
  }
  .price { color: #aaa; font-size: 0.85rem; }
  .signal-badge {
    padding: 4px 10px;
    border-radius: 20px;
    font-size: 0.8rem;
    font-weight: bold;
  }
  .sig-long  { background: #0e3b1e; color: #3ddc84; border: 1px solid #3ddc84; }
  .sig-short { background: #3b0e0e; color: #ff5c5c; border: 1px solid #ff5c5c; }
  .sig-none  { background: #1a1a2e; color: #555;    border: 1px solid #333; }

  table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
  thead th {
    color: #7eb8f7;
    text-align: center;
    padding: 4px 6px;
    border-bottom: 1px solid #2a2a4a;
    font-size: 0.75rem;
  }
  thead th:first-child { text-align: left; }
  tbody tr:hover { background: #1a1a30; }
  td {
    padding: 4px 6px;
    border-bottom: 1px solid #1a1a30;
    vertical-align: middle;
  }
  td:first-child { color: #aaa; }
  td.ok    { color: #3ddc84; text-align: center; }
  td.nok   { color: #ff5c5c; text-align: center; }
  td.val   { color: #e0e0e0; text-align: center; }
  td.warn  { color: #f0c040; text-align: center; }

  .regime {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: bold;
  }
  .regime-TREND  { background: #0e3b1e; color: #3ddc84; }
  .regime-EXPLO  { background: #3b2a0e; color: #f0a040; }
  .regime-RANGE  { background: #3b3b0e; color: #f0e040; }
  .regime-NEUTRE { background: #1a1a2e; color: #666; }

  .separator td { border-bottom: 1px solid #2a2a4a; padding: 2px 0; }
  .no-data { color: #444; text-align: center; padding: 30px; font-size: 0.85rem; }

  .dot-ok  { color: #3ddc84; }
  .dot-nok { color: #ff5c5c; }
  .dot-warn { color: #f0c040; }

  .footer {
    text-align: center;
    color: #333;
    font-size: 0.68rem;
    margin-top: 14px;
  }
  .refresh-bar {
    height: 2px;
    background: #2a2a4a;
    border-radius: 1px;
    margin-bottom: 12px;
    overflow: hidden;
  }
  .refresh-progress {
    height: 100%;
    background: #7eb8f7;
    width: 100%;
    animation: shrink 15s linear infinite;
  }
  @keyframes shrink { from { width: 100%; } to { width: 0%; } }
</style>
</head>
<body>
<h1>⚡ JP TRADING — CONDITIONS TEMPS RÉEL</h1>
<div id="updated">Chargement...</div>
<div class="refresh-bar"><div class="refresh-progress" id="progress"></div></div>
<div class="grid" id="grid">
  <div class="no-data">En attente des données TradingView...<br><br>
  Ajoute JP_conditions_webhook.pine sur tes charts SOL et ETH<br>
  et configure les alertes vers ce serveur.</div>
</div>
<div class="footer">Refresh auto toutes les 15s — données: TradingView Pine alertcondition</div>

<script>
const SYMBOLS = ['SOL','ETH'];

function dot(v)  { return v ? '<span class="dot-ok">●</span>' : '<span class="dot-nok">○</span>'; }
function ok(v)   { return '<td class="' + (v ? 'ok' : 'nok') + '">' + dot(v) + ' ' + (v ? 'OUI' : 'NON') + '</td>'; }
function val(v)  { return '<td class="val">' + v + '</td>'; }

function regimeBadge(r) {
  return '<span class="regime regime-' + r + '">' + r + '</span>';
}

function signalBadge(d) {
  if (d.valid_long)  return '<span class="signal-badge sig-long">⭐ LONG</span>';
  if (d.valid_short) return '<span class="signal-badge sig-short">⭐ SHORT</span>';
  return '<span class="signal-badge sig-none">— Pas de signal</span>';
}

function renderCard(sym, d) {
  if (!d) return `<div class="card">
    <div class="card-header">
      <span class="symbol">${sym}</span>
      <span class="signal-badge sig-none">En attente...</span>
    </div>
    <div class="no-data">Aucune donnée reçue</div>
  </div>`;

  const rows = [
    ['Régime 29M',
      `<td colspan="2" style="text-align:center">${regimeBadge(d.regime)}</td>`],
    ['ADX 29M',
      `<td class="${d.trending ? 'ok':'nok'}">${dot(d.trending)} ${parseFloat(d.adx).toFixed(1)}</td>`,
      `<td class="${d.trending ? 'ok':'nok'}">${dot(d.trending)} ${parseFloat(d.adx).toFixed(1)}</td>`],
    ['ADX 1H',
      `<td class="${d.adx1h > 25 ? 'ok':'nok'}">${dot(d.adx1h > 25)} ${parseFloat(d.adx1h).toFixed(1)}</td>`,
      `<td class="${d.adx1h > 25 ? 'ok':'nok'}">${dot(d.adx1h > 25)} ${parseFloat(d.adx1h).toFixed(1)}</td>`],
    ['DI direction',
      `<td class="${d.bull_trend ? 'ok':'nok'}">${dot(d.bull_trend)} DI+ ${parseFloat(d.di_plus).toFixed(1)} > DI- ${parseFloat(d.di_minus).toFixed(1)}</td>`,
      `<td class="${d.bear_trend ? 'ok':'nok'}">${dot(d.bear_trend)} DI- ${parseFloat(d.di_minus).toFixed(1)} > DI+ ${parseFloat(d.di_plus).toFixed(1)}</td>`],
    ['Prix / HMA50',
      ok(d.bull_trend),
      ok(d.bear_trend)],
    ['Pullback HMA20',
      ok(d.pb_long),
      ok(d.pb_short)],
    ['RSI ' + parseFloat(d.rsi).toFixed(1),
      ok(parseFloat(d.rsi) > 40 && parseFloat(d.rsi) < 65),
      ok(parseFloat(d.rsi) > 35 && parseFloat(d.rsi) < 60)],
    ['Volume',
      ok(!d.vol_up === false || true),  // volFilter ignoré côté dashboard
      ok(!d.vol_up === false || true)],
    null, // séparateur
    ['Signal 29M',
      ok(d.sig29_long),
      ok(d.sig29_short)],
    ['Confirm. 1H',
      ok(d.sig1h_long),
      ok(d.sig1h_short)],
    ['Filtre Range',
      ok(!d.in_range),
      ok(!d.in_range)],
    ['Anti-fort',
      ok(!d.strong_bear),
      ok(!d.strong_bull)],
  ];

  let tbody = '';
  for (const r of rows) {
    if (!r) {
      tbody += '<tr class="separator"><td colspan="3"></td></tr>';
      continue;
    }
    const [label, tdL, tdR] = r;
    if (r.length === 2) {
      tbody += `<tr><td>${label}</td>${tdL}</tr>`;
    } else {
      tbody += `<tr><td>${label}</td>${tdL}${tdR}</tr>`;
    }
  }

  return `<div class="card">
    <div class="card-header">
      <div>
        <span class="symbol">${sym}</span>
        <span class="price"> @ ${parseFloat(d.price).toFixed(2)}</span>
      </div>
      ${signalBadge(d)}
    </div>
    <table>
      <thead>
        <tr>
          <th>Condition</th>
          <th>▲ LONG</th>
          <th>▼ SHORT</th>
        </tr>
      </thead>
      <tbody>${tbody}</tbody>
    </table>
    <div style="font-size:0.68rem;color:#444;margin-top:6px;text-align:right">
      Mis à jour: ${d.updated_at || '—'}
    </div>
  </div>`;
}

async function refresh() {
  try {
    const resp = await fetch('/conditions/data');
    const data = await resp.json();
    const grid = document.getElementById('grid');
    const cards = SYMBOLS.map(s => renderCard(s, data[s] || null));
    // Ajouter autres symboles non listés
    for (const [sym, d] of Object.entries(data)) {
      if (!SYMBOLS.includes(sym)) cards.push(renderCard(sym, d));
    }
    if (cards.length === 0) {
      grid.innerHTML = '<div class="no-data">En attente des données...</div>';
    } else {
      grid.innerHTML = cards.join('');
    }
    document.getElementById('updated').textContent =
      'Dernière MAJ: ' + new Date().toLocaleTimeString('fr-FR');
    // Restart animation
    const p = document.getElementById('progress');
    p.style.animation = 'none';
    p.offsetHeight;
    p.style.animation = '';
  } catch(e) {
    document.getElementById('updated').textContent = 'Erreur de connexion...';
  }
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


@app.route("/conditions/data", methods=["GET"])
def conditions_data():
    """Retourne les dernières conditions en JSON (utilisé par le dashboard JS)."""
    return jsonify(_conditions), 200


@app.route("/labouch_status", methods=["GET"])
def labouch_status():
    """Retourne l'état Labouchère de tous les symboles."""
    return jsonify(labouch.get_all_status())


@app.route("/trade_log", methods=["GET"])
def trade_log_endpoint():
    """
    Retourne les 50 derniers trades loggés.
    ?limit=N pour changer la limite (max 200).
    """
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        trades = _get_trade_log(limit)
        return jsonify({
            "count":  len(trades),
            "trades": trades,
        }), 200
    except Exception as e:
        log.error(f"[trade_log] endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


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


# --- Init Labouchère si premier démarrage ---
def _init_labouch_if_needed():
    """Initialise la série 1 avec capital 500 + margin 500 par symbole."""
    for symbol, capital in BASE_CAPITAL_PER_SYMBOL.items():
        status = labouch.get_status(symbol)
        # Si pas encore initialisé (series_number == 0 ou absent)
        if status.get("series_number", 0) == 0 or status.get("active_capital", 0) == 0:
            log.info(f"[Labouchère] Init automatique {symbol}: capital={capital} + margin={capital}")
            labouch.init_series_with_margin(symbol, capital=capital, margin=capital)
            # Définir le ceiling dans l'état
            sym = labouch._get_sym(symbol)
            sym["ceiling_usdc"] = 50_000.0 if symbol == "ETH" else 70_000.0
            sym["ceiling_mode"] = "realistic"
            labouch._save()
        else:
            log.info(f"[Labouchère] {symbol} état existant: série {status['series_number']}, "
                     f"capital actif={status['active_capital']:.0f}, réserve={status['reserve']:.0f}")

_init_labouch_if_needed()

if __name__ == "__main__":
    log.info("Webhook Hyperliquid demarre -> http://0.0.0.0:5000")
    log.info("Coins supportes : SOL, ETH (et tout coin Hyperliquid)")
    log.info("Margin type     : ISOLATED par coin")
    log.info(f"DRY_RUN={DRY_RUN}  MAINNET={MAINNET}")
    log.info(f"Entry timeout   : {ENTRY_LIMIT_TIMEOUT}s")
    log.info(f"TP timeout      : {TP_LIMIT_TIMEOUT}s")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
