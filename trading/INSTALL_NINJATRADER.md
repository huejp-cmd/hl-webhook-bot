# 📦 Installation — NQReversalStrategy sur NinjaTrader 8

> Stratégie : Retournement NQ 10M | ADX≤20 + Breakout +2 pts  
> Fichier source : `NQReversalStrategy.cs`  
> NinjaTrader version requise : **8.x**

---

## Étape 1 — Copier le fichier .cs dans NinjaTrader

1. Ouvre l'**Explorateur Windows** (ou Finder sur Mac si tu utilises Wine/VM)
2. Navigue vers le dossier NinjaTrader :
   ```
   Documents\NinjaTrader 8\bin\Custom\Strategies\
   ```
   *(Le chemin exact dépend de ton installation — généralement dans Mes Documents)*

3. **Copie** le fichier `NQReversalStrategy.cs` dans ce dossier

> 💡 Si le dossier `Strategies` n'existe pas, crée-le manuellement.

---

## Étape 2 — Compiler la stratégie dans NinjaTrader

1. Ouvre **NinjaTrader 8**
2. Dans la barre de menu en haut → clique sur **NinjaScript** → **Editor**
3. Dans l'éditeur NinjaScript, menu **File** → **Open** → navigue vers ton fichier `NQReversalStrategy.cs`
4. Clique sur l'icône **Compiler** (ou appuie sur **F5**)
5. ✅ Si aucune erreur rouge → la stratégie est prête

> En cas d'erreur : copie le message d'erreur et contacte JP pour debug.

**Alternative — Compilation via le menu principal :**
- Menu **NinjaScript** → **Compile** → NinjaTrader recompile tous les scripts
- Vérifie dans **NinjaScript Output** qu'il n'y a pas d'erreur

---

## Étape 3 — Ouvrir un graphique NQ 10 Minutes

1. Menu **New** (ou Ctrl+N) → **Chart**
2. Dans la boîte de dialogue :
   - **Instrument** : `NQ 03-25` (ou le contrat NQ actuel, ex: `NQ 06-26`)
   - **Type** : `Minute`
   - **Period** : `10`
   - **Date range** : 30 à 90 jours pour commencer
3. Clique **OK** — le graphique NQ 10M s'ouvre

> 💡 Assure-toi que le fuseau horaire du graphique est sur **Central Time** ou **Eastern Time** selon ta config NinjaTrader. La stratégie filtre les heures 09h30–15h50 ET.

---

## Étape 4 — Appliquer la stratégie sur le graphique

1. Fais un **clic droit** sur le graphique → **Strategies** → **Add Strategy**
2. Dans la liste, cherche et sélectionne **NQ Reversal ADX Strategy**
3. Clique **Add** / **OK**

La stratégie apparaît en bas du graphique dans la section "Strategies".

---

## Étape 5 — Configurer les paramètres

Dans la fenêtre de configuration de la stratégie, tu peux ajuster :

| Paramètre | Valeur par défaut | Description |
|---|---|---|
| **ADX Seuil (≤)** | 20 | ADX(14) maximum pour valider le signal |
| **Corps Min (points)** | 10 | Taille minimale du corps de bougie |
| **Corps Max (points)** | 30 | Taille maximale du corps de bougie |
| **Breakout (points)** | 2 | Points de dépassement pour déclencher l'entrée |
| **Barres d'attente max** | 3 | Délai d'expiration du signal en barres |
| **Stop Loss (points)** | 15 | Distance du SL (= 60 ticks, = $300/contrat) |
| **Take Profit (points)** | 40 | Distance du TP (= 160 ticks, = $800/contrat) |
| **Nombre de contrats** | 1 | 1 ou 2 contrats NQ |

> 💰 **Rappel valeur NQ :** 1 tick = 5$ | 1 point (4 ticks) = 20$  
> SL 15 pts = 300$/contrat | TP 40 pts = 800$/contrat → Ratio R:R = 2,67

---

## Étape 6 — Backtest avec le Strategy Analyzer

1. Menu **New** → **Strategy Analyzer**
2. Dans la configuration :
   - **Strategy** : `NQ Reversal ADX Strategy`
   - **Instrument** : `NQ 03-25` (ou contrat actuel)
   - **Period** : `Minute 10`
   - **Date range** : 6 mois à 1 an (recommandé)
   - **Commission** : configurer selon ton broker (ex: 2,50$/contrat NinjaTrader Brokerage)
3. Clique **Calculate** et attends les résultats

### Métriques clés à analyser :
- **Net Profit** : bénéfice total sur la période
- **Win Rate** : % de trades gagnants (objectif > 40% avec ce ratio R:R)
- **Profit Factor** : ratio gains/pertes (objectif > 1,5)
- **Max Drawdown** : perte maximale en cours de route
- **Sharpe Ratio** : rendement ajusté au risque

---

## Étape 7 — Passer en Paper Trading (simulation)

Avant de trader en réel :

1. Dans les propriétés de la stratégie → **Account** : sélectionne ton compte **Sim** (simulation)
2. Lance la stratégie en **Enable** (bouton vert)
3. Observe les entrées/sorties pendant 2-4 semaines en simulé
4. Compare avec le backtest → si cohérent → passage au réel possible

---

## ⚠️ Notes importantes

- **Ne jamais lancer en réel sans avoir backtesté et papertraded d'abord**
- La stratégie ne trade que sur NQ — ne pas l'appliquer sur d'autres instruments sans ajuster les paramètres de corps (en points)
- Le filtre horaire 09h30–15h50 ET est codé en dur — assure-toi que NinjaTrader affiche l'heure ET correctement
- `IsExitOnSessionCloseStrategy = true` avec `ExitOnSessionCloseSeconds = 600` ferme les positions ~10 min avant la fermeture de session configurée dans NinjaTrader
- Pour la sortie exacte à 15h50, configure la **Session** du graphique NQ avec une fermeture à 16h00 ET (standard) — la stratégie sortira 10 min avant

---

## 🔧 Dépannage fréquent

| Problème | Solution |
|---|---|
| Stratégie absente de la liste | Recompiler via NinjaScript → Compile |
| Erreur de compilation | Vérifier que NT8 est à jour (≥ 8.1.1) |
| Aucune entrée en backtest | Vérifier la plage de dates, la session NQ, et les paramètres de corps |
| SL/TP ne se placent pas | Normal en Calculate.OnBarClose — ils se placent à la barre suivante |
| Heures incorrectes | Vérifier le fuseau horaire dans Tools → Options → Time Zone |

---

*Fichier généré par OpenClaw — Avril 2026*
