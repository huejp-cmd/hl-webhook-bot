#!/usr/bin/env python3
"""
Lanceur Railway — webhook-only
TradingView est l'unique source de signaux.
Le bot autonome a été désactivé pour garantir des signaux
identiques entre le backtest TradingView et l'exécution live.
"""
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("start")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "trading"))

log.info("🚀 Démarrage serveur webhook (TradingView → Hyperliquid)")
log.info("📡 Mode: WEBHOOK-ONLY — source de signaux = TradingView exclusivement")

import hl_webhook_server
port = int(os.environ.get("PORT", 8080))
hl_webhook_server.app.run(host="0.0.0.0", port=port, debug=False)
