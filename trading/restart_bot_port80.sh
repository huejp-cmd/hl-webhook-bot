#!/bin/bash
# Arrête l'ancien bot (port 8080) et redémarre sur port 80
pkill -f hl_webhook_server.py 2>/dev/null
sleep 2
cd /Users/huejeanpierre/.openclaw/workspace/trading
nohup python3 hl_webhook_server.py >> hl_orders.log 2>&1 &
echo "Bot redemarré sur port 80 (PID: $!)"
