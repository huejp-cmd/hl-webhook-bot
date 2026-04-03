#!/usr/bin/env python3
"""
Lanceur combiné — Railway
Lance en parallèle :
  - Le serveur webhook (TradingView → Hyperliquid)
  - Le bot autonome (calcule ses propres signaux)
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
    """Lance le serveur Flask webhook en arrière-plan."""
    log.info("🚀 Démarrage serveur webhook (TradingView)...")
    import hl_webhook_server
    port = int(os.environ.get("PORT", 8080))
    hl_webhook_server.app.run(host="0.0.0.0", port=port, debug=False)

def run_autonomous():
    """Lance le bot autonome (boucle 29M) dans le thread principal."""
    log.info("🤖 Démarrage bot autonome (calcul signaux HMA/ADX/RSI)...")
    import autonomous_bot
    autonomous_bot.run()

# Webhook en thread background (daemon → s'arrête si le main thread s'arrête)
t_webhook = threading.Thread(target=run_webhook, daemon=True, name="webhook")
t_webhook.start()
log.info("✅ Thread webhook lancé")

# Bot autonome dans le thread principal
run_autonomous()
