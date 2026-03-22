# Setup Webhook TradingView → Hyperliquid

## Circuit complet

```
TradingView (SOL29_v5_webhook.pine)
        ↓ Alerte JSON (webhook)
hl_webhook_server.py (port 5000)
        ↓ API
Hyperliquid (ordres réels)
```

---

## Étape 1 — Installer les dépendances

```bash
pip3 install flask hyperliquid-python-sdk eth-account
```

---

## Étape 2 — Configurer le serveur

Ouvre `hl_webhook_server.py` et modifie :

```python
PRIVATE_KEY   = "ta_clé_privée_MetaMask"
DRY_RUN       = True   # ← commence en DRY RUN !
WEBHOOK_TOKEN = "sol29_secret_token_2026"  # change ce token
```

> ⚠️ **La clé privée MetaMask** se trouve dans MetaMask → 
> Paramètres → Sécurité → "Exporter la clé privée"
> Ne la partage jamais et ne la mets pas dans un fichier public.

---

## Étape 3 — Démarrer le serveur

```bash
cd /Users/huejeanpierre/.openclaw/workspace/trading
python3 hl_webhook_server.py
```

Vérifie que tout fonctionne :
```bash
curl http://localhost:5000/status
```

---

## Étape 4 — Exposer le serveur (ngrok)

TradingView a besoin d'une URL publique HTTPS. Utilise ngrok :

```bash
# Installe ngrok : https://ngrok.com
ngrok http 5000
```

Copie l'URL HTTPS fournie, ex : `https://abc123.ngrok.io`

---

## Étape 5 — Configurer l'alerte sur TradingView

1. Charge `SOL29_v5_webhook.pine` dans TradingView
2. Règle le paramètre **"Symbole Hyperliquid"** → `SOL`
3. Crée une alerte :
   - **Condition** : `SOL29 v5.3 - Webhook Hyperliquid` → `alert() function calls`
   - **Webhook URL** : `https://abc123.ngrok.io/webhook?token=sol29_secret_token_2026`
   - **Message** : laisser vide (le script envoie son propre JSON)
   - **Expire** : Open-ended
4. Sauvegarde

---

## Étape 6 — Test en DRY RUN

Attends un signal du script (ou force manuellement). Vérifie les logs :

```
🟢 LONG SOL
  Prix:     185.40
  Qty:      2.7
  SL:       182.10
  TP:       199.60
  [DRY RUN] Ordre simulé — non envoyé
```

---

## Étape 7 — Passer en LIVE

Quand tu es satisfait des tests :
1. Ouvre `hl_webhook_server.py`
2. Change `DRY_RUN = False`
3. Redémarre le serveur

> ⚠️ RAPPEL : demande confirmation à JP (moi) avant de changer DRY_RUN → False

---

## Consulter les ordres

- **Positions en cours** : https://app.hyperliquid.xyz (onglet Positions)
- **Historique** : https://app.hyperliquid.xyz (onglet Orders / Fills)
- **Logs locaux** : `webhook_orders.log`
