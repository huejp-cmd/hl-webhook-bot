# HEARTBEAT.md

## Note architecture (2026-03-20)
La surveillance trading (Hyperliquid + Moltbook) est désormais gérée par **TradeMolty** en cron isolé toutes les 30 min.
Le heartbeat principal ne fait plus de surveillance trading pour éviter les doublons.

## Tâches heartbeat principal (assistant général)
- Vérifier les emails urgents (hue.jp@hotmail.fr)
- Vérifier le calendrier (événements < 2h)
- Mettre à jour la mémoire si nécessaire
- Si rien → répondre HEARTBEAT_OK
