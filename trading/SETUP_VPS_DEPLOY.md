# JP Trading Bot — Guide de déploiement VPS Hetzner
_Version 2026-03-18_

---

## Architecture globale

```
TradingView (SOL 29M)  ──→  webhook  ──→  VPS Hetzner
TradingView (ETH 29M)  ──→  webhook  ──┘     │
                                              ├── ordres Hyperliquid
                                              ├── dashboard conditions
                                              └── log intrabar vs réel
```

---

## 1. Connexion au VPS Hetzner

### Credentials
- **Provider :** Hetzner Cloud (cloud.hetzner.com)
- **Compte :** K1267357225 / hue.jp@hotmail.fr

### Récupérer l'IP
1. Se connecter sur https://cloud.hetzner.com
2. Tableau de bord → ton serveur → copier l'IP publique

### Connexion SSH
```bash
ssh root@TON_IP
```
(remplacer `TON_IP` par l'IP copiée)

---

## 2. Installation sur le VPS

### 2.1 Mise à jour système
```bash
apt update && apt upgrade -y
apt install python3 python3-pip git screen -y
```

### 2.2 Installer les dépendances Python
```bash
pip3 install flask hyperliquid-python-sdk eth-account
```

### 2.3 Copier les fichiers du bot
```bash
mkdir -p /opt/jpbot
```

Depuis ton Mac, copier les fichiers :
```bash
scp hl_webhook_server.py root@TON_IP:/opt/jpbot/
```

Ou créer le fichier directement :
```bash
nano /opt/jpbot/hl_webhook_server.py
# Coller le contenu du fichier
```

### 2.4 Ouvrir le port 8080
```bash
ufw allow 8080
ufw allow 22
ufw enable
```

---

## 3. Lancer le serveur

### Démarrage simple (test)
```bash
cd /opt/jpbot
python3 hl_webhook_server.py
```

### Démarrage permanent (production)
```bash
# Option A : screen (simple)
screen -S jpbot
cd /opt/jpbot
python3 hl_webhook_server.py
# Ctrl+A puis D pour détacher

# Option B : service systemd (recommandé pour redémarrage auto)
cat > /etc/systemd/system/jpbot.service << 'EOF'
[Unit]
Description=JP Trading Bot
After=network.target

[Service]
WorkingDirectory=/opt/jpbot
ExecStart=/usr/bin/python3 hl_webhook_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl enable jpbot
systemctl start jpbot
systemctl status jpbot
```

### Vérifier que le serveur tourne
```bash
curl http://TON_IP:8080/status
```
→ Doit retourner un JSON avec `"status": "online"`

---

## 4. Configuration TradingView

### 4.1 Charts à créer
| Chart | Symbole | Timeframe | Script |
|-------|---------|-----------|--------|
| Chart 1 | SOLUSDT.P (Hyperliquid) | **29M** | JP v5.4 + JP_conditions_webhook |
| Chart 2 | ETHUSDT.P (Hyperliquid) | **29M** | JP v5.4 + JP_conditions_webhook |

### 4.2 Scripts Pine à charger
Sur chaque chart, ajouter **deux** indicateurs/stratégies :
1. `JP_v54_multi_tf_market.pine` — stratégie principale (ordres + alertes)
2. `JP_conditions_webhook.pine` — envoi des conditions au dashboard

### 4.3 Alertes à créer (par chart)

#### Alerte 1 — Signaux de trading (JP v5.4)
- **Condition :** Any alert() function call
- **Fréquence :** Once per bar close
- **Webhook URL :** `http://TON_IP:8080/webhook`
- **Header :** `X-Webhook-Token: jp_bot_secret_2026`
- **Message :** _(laisser le message Pine par défaut)_

#### Alerte 2 — Conditions dashboard (JP_conditions_webhook)
- **Condition :** Conditions Update
- **Fréquence :** Once per bar close
- **Webhook URL :** `http://TON_IP:8080/conditions`
- **Header :** `X-Webhook-Token: jp_bot_secret_2026`
- **Message :** _(laisser le message Pine par défaut — JSON auto)_

> ⚠️ Répéter ces deux alertes pour SOL et ETH (4 alertes au total)

---

## 5. Dashboard conditions

### Accès
```
http://TON_IP:8080/dashboard
```
Ouvrir dans un navigateur — fonctionne sur PC, tablette, smartphone.

### Contenu affiché
Pour chaque symbole (SOL + ETH) :

| Condition | LONG | SHORT |
|-----------|------|-------|
| Régime 29M (TREND/EXPLO/RANGE) | ● | ● |
| ADX > 25 (valeur) | ● | ● |
| ADX 1H (valeur) | ● | ● |
| DI direction (DI+ vs DI-) | ● | ● |
| Prix / HMA50 | ● | ● |
| Pullback HMA20 | ● | ● |
| RSI (fourchette) | ● | ● |
| Volume | ● | ● |
| Signal 29M | ● | ● |
| Confirmation 1H | ● | ● |
| Filtre Range | ● | ● |
| Anti-fort (strongBull/Bear) | ● | ● |
| **★ SIGNAL FINAL** | **⭐ ENTRÉE** | **⭐ ENTRÉE** |

🟢 vert = condition satisfaite | 🔴 rouge = condition non satisfaite
Auto-refresh toutes les 15 secondes.

---

## 6. Paramètres du bot (rappel)

### Capital alloué
| Symbole | Capital | Risk/trade | $ risqués max |
|---------|---------|-----------|---------------|
| SOL | 600 USDT | 2% | 12 USDT |
| ETH | 400 USDT | 2% | 8 USDT |
| **Total** | **1000 USDT** | — | **20 USDT** |

### Stop Loss
- Basé sur les **15 derniers candles** (lowest/highest)
- Plafonné à **1.5%** du prix d'entrée
- La quantité s'ajuste pour que la perte = exactement 2% du capital

### Take Profit
- Multiplicateur selon régime :
  - Trend : TP = distance_SL × **4.0**
  - Explosive : TP = distance_SL × **3.3**
  - Range : TP = distance_SL × **2.5**
- Bonus ×1.2 si signal 29M + 1H alignés simultanément

### Levier
- SOL : **2x** isolated margin
- ETH : **2x** isolated margin

---

## 7. Analyse intrabar vs réel (automatique)

### Fichier de log
`/opt/jpbot/intrabar_vs_real.csv`

### Format
```
type,symbol,side,prix_intrabar,prix_reel,ecart,ecart_pct,ts_intrabar,ts_reel
comparison,SOL,long,131.45,131.72,+0.27,+0.21%,...
```

### Lecture après 1 mois
```bash
# Voir tous les comparatifs
grep "^comparison" intrabar_vs_real.csv

# Moyenne des écarts (python)
python3 -c "
import csv
ecarts = []
with open('intrabar_vs_real.csv') as f:
    for row in csv.reader(f):
        if row[0] == 'comparison':
            ecarts.append(float(row[6]))
print(f'Trades analysés : {len(ecarts)}')
print(f'Écart moyen     : {sum(ecarts)/len(ecarts):.3f}%')
print(f'Écart max       : {max(ecarts):.3f}%')
print(f'Écart min       : {min(ecarts):.3f}%')
"
```

> Si l'écart moyen > 0.07% (coût commission) → tester l'entrée intrabar en prod

---

## 8. Surveillance et logs

### Logs en temps réel
```bash
tail -f /opt/jpbot/hl_orders.log
```

### Positions ouvertes
```bash
curl http://TON_IP:8080/status | python3 -m json.tool
```

### Fermeture manuelle d'urgence
```bash
# Fermer SOL
curl -X POST http://TON_IP:8080/close/SOL \
  -H "X-Webhook-Token: jp_bot_secret_2026"

# Fermer ETH
curl -X POST http://TON_IP:8080/close/ETH \
  -H "X-Webhook-Token: jp_bot_secret_2026"
```

### Redémarrer le bot
```bash
systemctl restart jpbot
```

---

## 9. Checklist de mise en production

- [ ] Récupérer l'IP du VPS sur cloud.hetzner.com
- [ ] Se connecter en SSH
- [ ] Installer Python + dépendances
- [ ] Copier `hl_webhook_server.py` dans `/opt/jpbot/`
- [ ] Ouvrir port 8080 (ufw)
- [ ] Lancer le service systemd
- [ ] Tester `/status` depuis navigateur
- [ ] Charger `JP_v54_multi_tf_market.pine` sur chart SOL 29M
- [ ] Charger `JP_v54_multi_tf_market.pine` sur chart ETH 29M
- [ ] Charger `JP_conditions_webhook.pine` sur chart SOL 29M
- [ ] Charger `JP_conditions_webhook.pine` sur chart ETH 29M
- [ ] Créer les 4 alertes TradingView (2 par chart)
- [ ] Vérifier le dashboard sur `/dashboard`
- [ ] Faire un test DRY_RUN=True pendant 24h
- [ ] Passer DRY_RUN=False pour la production

---

## 10. Fichiers du projet

| Fichier | Description |
|---------|-------------|
| `hl_webhook_server.py` | Serveur principal — ordres + dashboard + logs |
| `JP_v54_multi_tf_market.pine` | Stratégie principale (signaux + entrées) |
| `JP_conditions_webhook.pine` | Envoi des conditions au dashboard |
| `JP_conditions_table.pine` | Tableau visuel sur le chart TradingView |
| `intrabar_vs_real.csv` | Log automatique comparaison entrées |
| `hl_orders.log` | Log complet de tous les ordres passés |

---

_Document généré le 2026-03-18 — mise à jour à chaque évolution majeure_
