#!/bin/bash
# Arrête l'ancien bot et redémarre sur port 80 avec le bon wallet
pkill -f hl_webhook_server.py 2>/dev/null
sleep 2
cd /Users/huejeanpierre/.openclaw/workspace/trading

# Charger les variables d'environnement depuis .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

nohup python3 hl_webhook_server.py >> hl_orders.log 2>&1 &
echo "Bot redemarré (PID: $!)"
echo "Wallet attendu: 0xaF6542067Cab6D8D9E3D7BaA5AaE16DB86f83fBb"
