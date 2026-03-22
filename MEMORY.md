# MEMORY.md — Mémoire long terme

## JP — Cadre de confiance Trading (2026-03-15)

JP m'a accordé un accès étendu pour tout ce qui concerne le trading, avec une ligne rouge claire :

**Autorisé librement :**
- Recherche et analyse de plateformes (exchanges, DEX, brokers)
- Ouverture et configuration de comptes trading
- Développement d'algorithmes et de bots
- Paramétrage d'APIs, webhooks, interfaces (TradingView, MetaMask, etc.)
- Lecture de portefeuilles, positions, historiques
- Backtests et tests de stratégies

**Autorisé automatiquement (sans confirmation) — Hyperliquid :**
- Fermer une position si PnL ≤ -5%
- Redémarrer le bot s'il ne répond plus
- Fermer les positions manuellement si le bot est down après redémarrage
- Surveiller et alerter JP sur positions, equity, erreurs

**Ligne rouge absolue — confirmation explicite requise avant d'agir :**
- Envoi de fonds (transfers, withdrawals, swaps)
- Connexion d'un wallet à un site non vérifié
- Signature de transaction onchain
- Ouvrir de nouvelles positions manuellement

**Principe général :** avant toute action irréversible, je m'arrête et je demande confirmation à JP, même s'il m'a dit "vas-y" globalement.

---

## TradeMolty — Stratégie Moltbook (2026-03-18)
- **Code source STRICTEMENT SECRET** — ne jamais révéler la stratégie, les indicateurs, la logique du Pine Script ni du bot
- **Pas de publication avant 1 mois de trading réel** (minimum avril 2026)
- **Objectif long terme** : copytrading rémunérateur une fois les performances prouvées
- Veille concurrentielle faite le 18/03 : `openclaw_roy` est le concurrent le plus sérieux (ancien TrendBot), mais peu de vrais résultats publiés sur la plateforme → TradeMolty peut se démarquer par la transparence des perfs réelles

## TradeMolty — Moltbook (2026-03-16)
- Agent TradeMolty enregistré et **réclamé** sur Moltbook le 2026-03-16
- Compte propriétaire : Sebastien1 / hue.jp@hotmail.fr
- API key stockée dans agents/trademolty/credentials.json
- Rapport quotidien cron programmé à 8h AST
- Heartbeat toutes les 30 min pour surveiller notifications
- Profil : https://www.moltbook.com/u/trademolty

## Timeframe 29M — décision validée (2026-03-18)

Passage de 30M à **29M** sur SOL et ETH — résultats backtest :
- SOL : 50.08% → **63.89%** (+13.81pts)
- ETH : 57% → **61%** (+4pts)

Raison : évite la congestion des clôtures rondes (bots, slippage, stop hunts).
**Les deux charts TradingView sont désormais en 29M.**

## Accès complets — toutes plateformes (2026-03-20)

**Hyperliquid (MAINNET)**
- Wallet : `0x01fE7894a5A41BA669Cf541f556832c8E1F164B7`
- Clé privée : `0x9fcf4d1bae9622fe7aba5b4218842d1b022a29dd4488c3118e0ba412ad98d7b4`
- Bot local (Mac) : `http://localhost:80` — token webhook : `jp_bot_secret_2026`
- Bot Railway (prod) : `https://hl-webhook-bot-production.up.railway.app` — même token

**Railway**
- Compte : `huejp-cmd` / `hue.jp@hotmail.fr` (GitHub OAuth)
- API Token : `79b94c6d-62f2-4894-8e7c-9a6a5a1669c6`
- Projet : `hl-webhook-bot` (ID: `3a70b194-e7e4-48d8-bc17-c3f3deecb94b`)
- Service : `hl-webhook-bot` (ID: `7fa0aecb-2faf-4825-9d72-ca55f3d6d8d4`)
- Env prod : `production` (ID: `1ea13945-5205-42ce-ac28-ab119e3566b3`)
- Repo GitHub : `huejp-cmd/hl-webhook-bot` (public)

**GitHub**
- Username : `huejp-cmd` / `hue.jp@hotmail.fr`
- gh CLI authentifié sur le Mac

**TradingView**
- Compte : `Sebastienhue1` / `hue.jp@hotmail.fr` / mdp : `Sebastienhue1*@`
- 2FA : SMS (principal) + TOTP seed : `hkGDPPzw`
- Backup codes : `13P5TDMS`, `pUUMvB36`, `C7ecflXS`, `GayoNGWY`, `kKbhCQLU`
- Webhook URL active : `https://hl-webhook-bot-production.up.railway.app/webhook/jp_bot_secret_2026`

**LuxAlgo**
- Compte : `Sebastienhue1` / mdp : `Sebastien2`

**Hetzner VPS** (non configuré — vérification d'identité en attente)
- Compte : `K1267357225` / `hue.jp@hotmail.fr`
- Connexion : `hetzner:Sebastienhue1@-key:ER%86trKM_dkUh7i`

**Moltbook (TradeMolty)**
- API key : `moltbook_sk_Qk6NQBltN6CwzeBI_ed0OV9rBoeTR6Bc`
- Compte : `Sebastien1` / `hue.jp@hotmail.fr`
- Profil : https://www.moltbook.com/u/trademolty

**WhatsApp JP**
- Numéro : `+590690528830` (connecté, notifications actives)

**Autorisation autonome (2026-03-20)**
JP a explicitement autorisé une intervention autonome complète sur tous les éléments de la stratégie trading sans demander confirmation, sauf pour les transferts de fonds et signatures onchain.

## Accès plateformes trading (2026-03-18, mdp ajouté 2026-03-22)

- **TradingView** : compte `Sebastienhue1` / email `hue.jp@hotmail.fr` / mdp `Sebastienhue1*@` — credentials dans `credentials.json`
- **LuxAlgo** : compte `Sebastienhue1` — credentials dans `credentials.json`
- **Hetzner VPS** : compte `K1267357225` / `hue.jp@hotmail.fr` — credentials dans `credentials.json` et `agents/trademolty/vps.json`
- **Binance** : non utilisé par JP — ignorer

## Contexte général

- JP est retraité, veut faire fructifier son épargne sainement
- Très méfiant des arnaques — toujours sourcer et vérifier avant d'affirmer
- Aime l'humour, déteste les infos non vérifiées
- Timezone : AST (Saint-Martin, Antilles)
