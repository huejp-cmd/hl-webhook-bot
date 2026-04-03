#!/usr/bin/env python3
"""
Lanceur combiné — Railway
Lance en parallèle :
  - Le serveur webhook (TradingView → Hyperliquid) sur port 8080
  - Le bot autonome (calcule ses propres signaux, indépendant de TradingView)
"""
import threading
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("start")

# Ajoute le dossier trading au path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "trading"))

def run_webhook():
    log.info("🚀 Démarrage serveur webhook...")
    import hl_webhook_server  # démarre Flask en interne

def run_autonomous():
    log.info("🤖 Démarrage bot autonome...")
    import autonomous_bot
    autonomous_bot.run()

# Webhook server en thread background
t_webhook = threading.Thread(target=run_webhook, daemon=True, name="webhook")
t_webhook.start()

# Bot autonome dans le thread principal
run_autonomous()
