"""
HL DCA Bot — Stratégie EMA/RSI/MACD avec DCA
Adapté pour Hyperliquid par TradeMolty

Signaux : EMA50/200 + RSI + MACD sur 1h
DCA     : 3 niveaux max à -1.5% d'écart
SL      : -3% sur position globale
Levier  : 5x ISOLATED
Risk    : 2% du capital par entrée
"""

import os, json, time, logging, requests, math
from datetime import datetime, timezone
from flask import Flask, jsonify
import threading
import pandas as pd
import ta

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════
PRIVATE_KEY      = os.environ.get("PRIVATE_KEY", "")
WALLET           = "0x01fE7894a5A41BA669Cf541f556832c8E1F164B7"
MAINNET          = os.environ.get("MAINNET", "true").lower() == "true"
API_URL          = constants.MAINNET_API_URL if MAINNET else constants.TESTNET_API_URL
INFO_URL         = API_URL + "/info"

SYMBOLS          = ["SOL", "ETH"]   # noms Hyperliquid (sans USDT)
LEVERAGE         = 5
RISK_PER_TRADE   = 0.02             # 2% du capital
MAX_DCA          = 3
DCA_DISTANCE     = 0.015            # 1.5%
STOP_LOSS        = 0.03             # 3%
INTERVAL         = "1h"
LOOP_SLEEP       = 60               # secondes entre chaque cycle
STATE_FILE       = "/tmp/hl_dca_state.json"

# ══════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hl_dca")

# ══════════════════════════════════════════
# INIT SDK
# ══════════════════════════════════════════
account  = Account.from_key(PRIVATE_KEY)
info     = Info(API_URL, skip_ws=True)
exchange = Exchange(account, API_URL, account_address=WALLET)

# ══════════════════════════════════════════
# STATE (persisté dans fichier JSON)
# ══════════════════════════════════════════
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

positions = load_state()

# ══════════════════════════════════════════
# DONNÉES OHLCV HYPERLIQUID
# ══════════════════════════════════════════
INTERVAL_MS = {
    "1h": 3600_000,
    "4h": 14400_000,
    "15m": 900_000,
    "29m": 1740_000,
    "30m": 1800_000,
}

def get_data(symbol: str, limit: int = 210) -> pd.DataFrame:
    """Récupère les bougies OHLCV via l'API Hyperliquid."""
    interval_ms = INTERVAL_MS.get(INTERVAL, 3600_000)
    end_ts   = int(time.time() * 1000)
    start_ts = end_ts - limit * interval_ms

    resp = requests.post(INFO_URL, json={
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": INTERVAL,
            "startTime": start_ts,
            "endTime": end_ts,
        }
    }, timeout=10)
    resp.raise_for_status()
    candles = resp.json()

    df = pd.DataFrame(candles, columns=["t", "T", "s", "i", "o", "c", "h", "l", "v", "n"])
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df.tail(limit)

# ══════════════════════════════════════════
# SIGNAUX
# ══════════════════════════════════════════
def compute_signal(df: pd.DataFrame):
    """EMA50/200 + RSI(14) + MACD."""
    df["ema50"]  = ta.trend.ema_indicator(df["close"], window=50)
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["rsi"]    = ta.momentum.rsi(df["close"], window=14)

    macd          = ta.trend.MACD(df["close"])
    df["macd"]    = macd.macd()
    df["macd_sig"] = macd.macd_signal()

    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    # Croisement MACD (plus fiable qu'une simple comparaison)
    macd_cross_up   = prev["macd"] <= prev["macd_sig"] and last["macd"] > last["macd_sig"]
    macd_cross_down = prev["macd"] >= prev["macd_sig"] and last["macd"] < last["macd_sig"]

    long  = (last["close"] > last["ema50"] and
             last["ema50"] > last["ema200"] and
             last["rsi"] > 50 and
             macd_cross_up)

    short = (last["close"] < last["ema50"] and
             last["ema50"] < last["ema200"] and
             last["rsi"] < 50 and
             macd_cross_down)

    return long, short, round(last["rsi"], 1), round(last["ema50"], 4)

# ══════════════════════════════════════════
# UTILS PRIX
# ══════════════════════════════════════════
def round_price(x: float) -> float:
    """Arrondit à 5 chiffres significatifs (exigence Hyperliquid)."""
    if x == 0:
        return 0.0
    d = math.ceil(math.log10(abs(x)))
    factor = 10 ** (5 - d)
    return round(x * factor) / factor

# ══════════════════════════════════════════
# CAPITAL & QTY
# ══════════════════════════════════════════
def get_equity() -> float:
    """Equity = perp margin + USDC spot (compte unifié Hyperliquid)."""
    state = info.user_state(WALLET)
    perp_val = float(state.get("marginSummary", {}).get("accountValue", 0))
    spot_state = info.spot_user_state(WALLET)
    spot_usdc = next(
        (float(b["total"]) for b in spot_state.get("balances", []) if b["coin"] == "USDC"),
        0.0,
    )
    total = perp_val + spot_usdc
    return total if total > 0 else perp_val

def get_mark_price(symbol: str) -> float:
    mids = info.all_mids()
    return float(mids.get(symbol, 0))

def calc_qty(symbol: str, price: float) -> float:
    equity = get_equity()
    size_usd = equity * RISK_PER_TRADE * LEVERAGE
    qty = size_usd / price
    # Arrondi selon les specs Hyperliquid (généralement 3 décimales pour SOL, 4 pour ETH)
    decimals = 4 if symbol == "ETH" else 3
    return round(qty, decimals)

# ══════════════════════════════════════════
# ORDRES HYPERLIQUID
# ══════════════════════════════════════════
def set_leverage(symbol: str):
    try:
        exchange.update_leverage(LEVERAGE, symbol, is_cross=False)
        log.info(f"[{symbol}] Levier {LEVERAGE}x ISOLATED configuré")
    except Exception as e:
        log.warning(f"[{symbol}] Levier déjà configuré ou erreur : {e}")

def place_market_order(symbol: str, side: str, qty: float) -> bool:
    """side = 'buy' ou 'sell' — ordre agressif GTC (remplit quasi-instantanément)."""
    try:
        is_buy   = side == "buy"
        price    = get_mark_price(symbol)
        # Prix agressif 1% au-delà du marché pour garantir le fill
        limit_px = round_price(price * (1.01 if is_buy else 0.99))
        result   = exchange.order(
            symbol, is_buy, qty, limit_px,
            {"limit": {"tif": "Gtc"}}
        )
        log.info(f"[{symbol}] Ordre {side} {qty} @ {limit_px} — {result}")
        return result.get("status") == "ok"
    except Exception as e:
        log.error(f"[{symbol}] Erreur ordre {side} : {e}")
        return False

def close_position(symbol: str) -> bool:
    """Ferme la position ouverte sur symbol."""
    try:
        state = info.user_state(WALLET)
        for pos in state.get("assetPositions", []):
            p = pos["position"]
            if p["coin"] == symbol and float(p["szi"]) != 0:
                size = abs(float(p["szi"]))
                is_long = float(p["szi"]) > 0
                side = not is_long  # on vend si long, achète si short
                price = get_mark_price(symbol)
                limit_px = round(price * (0.995 if side else 1.005), 4)
                result = exchange.order(
                    symbol, side, size, limit_px,
                    {"limit": {"tif": "Ioc"}},
                    reduce_only=True
                )
                log.info(f"[{symbol}] Position fermée : {result}")
                return True
        log.warning(f"[{symbol}] Aucune position à fermer")
        return False
    except Exception as e:
        log.error(f"[{symbol}] Erreur fermeture : {e}")
        return False

# ══════════════════════════════════════════
# LOGIQUE PRINCIPALE
# ══════════════════════════════════════════
def run_symbol(symbol: str):
    try:
        df = get_data(symbol)
        long, short, rsi, ema50 = compute_signal(df)
        price = get_mark_price(symbol)

        log.info(f"[{symbol}] Prix={price:.4f} RSI={rsi} EMA50={ema50} | LONG={long} SHORT={short}")

        # ── Pas encore de position ────────────────────────────────
        if symbol not in positions:
            if long:
                set_leverage(symbol)
                qty = calc_qty(symbol, price)
                if qty <= 0:
                    log.warning(f"[{symbol}] qty=0, ordre annulé (equity insuffisante?)")
                    return
                if place_market_order(symbol, "buy", qty):
                    positions[symbol] = {"side": "LONG", "entry": price, "dca": 0, "qty": qty}
                    log.info(f"[{symbol}] LONG ouvert @ {price} qty={qty}")

            elif short:
                set_leverage(symbol)
                qty = calc_qty(symbol, price)
                if qty <= 0:
                    log.warning(f"[{symbol}] qty=0, ordre annulé (equity insuffisante?)")
                    return
                if place_market_order(symbol, "sell", qty):
                    positions[symbol] = {"side": "SHORT", "entry": price, "dca": 0, "qty": qty}
                    log.info(f"[{symbol}] SHORT ouvert @ {price} qty={qty}")
            return

        # ── Position existante ────────────────────────────────────
        pos = positions[symbol]
        entry = pos["entry"]

        if pos["side"] == "LONG":
            pnl_pct = (price - entry) / entry

            # Stop Loss
            if pnl_pct <= -STOP_LOSS:
                log.warning(f"[{symbol}] SL déclenché ({pnl_pct*100:.2f}%) — fermeture")
                if close_position(symbol):
                    del positions[symbol]
                return

            # DCA baisse
            if pnl_pct <= -DCA_DISTANCE and pos["dca"] < MAX_DCA:
                qty = calc_qty(symbol, price)
                if place_market_order(symbol, "buy", qty):
                    pos["dca"] += 1
                    pos["entry"] = (entry + price) / 2  # prix moyen simplifié
                    log.info(f"[{symbol}] DCA #{pos['dca']} LONG @ {price}")

            # Signal inverse → sortie
            if short:
                log.info(f"[{symbol}] Signal SHORT → fermeture LONG")
                if close_position(symbol):
                    del positions[symbol]

        elif pos["side"] == "SHORT":
            pnl_pct = (entry - price) / entry

            # Stop Loss
            if pnl_pct <= -STOP_LOSS:
                log.warning(f"[{symbol}] SL déclenché ({pnl_pct*100:.2f}%) — fermeture")
                if close_position(symbol):
                    del positions[symbol]
                return

            # DCA hausse
            if pnl_pct <= -DCA_DISTANCE and pos["dca"] < MAX_DCA:
                qty = calc_qty(symbol, price)
                if place_market_order(symbol, "sell", qty):
                    pos["dca"] += 1
                    pos["entry"] = (entry + price) / 2
                    log.info(f"[{symbol}] DCA #{pos['dca']} SHORT @ {price}")

            # Signal inverse → sortie
            if long:
                log.info(f"[{symbol}] Signal LONG → fermeture SHORT")
                if close_position(symbol):
                    del positions[symbol]

    except Exception as e:
        log.error(f"[{symbol}] Erreur cycle : {e}")

# ══════════════════════════════════════════
# HEALTH CHECK (Flask)
# ══════════════════════════════════════════
app = Flask(__name__)

@app.route("/status")
def status():
    try:
        equity = get_equity()
    except:
        equity = None
    return jsonify({
        "status": "online",
        "bot": "hl_dca_bot",
        "equity": equity,
        "positions": positions,
        "symbols": SYMBOLS,
        "leverage": LEVERAGE,
        "time": datetime.now(timezone.utc).isoformat(),
    })

@app.route("/close/<coin>", methods=["POST"])
def close(coin):
    coin = coin.upper()
    ok = close_position(coin)
    if coin in positions:
        del positions[coin]
        save_state(positions)
    return jsonify({"status": "closed" if ok else "error", "coin": coin})

# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════
def trading_loop():
    log.info("HL DCA Bot démarré — EMA/RSI/MACD + DCA")
    while True:
        for symbol in SYMBOLS:
            run_symbol(symbol)
        save_state(positions)
        log.info(f"Cycle terminé — prochain dans {LOOP_SLEEP}s")
        time.sleep(LOOP_SLEEP)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    threading.Thread(target=trading_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False)
