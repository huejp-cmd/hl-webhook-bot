// =============================================================================
//  NQReversalStrategy.cs — Stratégie NinjaTrader 8 pour NQ Futures
//  Auteur     : JP (configurée par OpenClaw)
//  Timeframe  : 10 minutes
//  Instrument : NQ (E-mini Nasdaq-100 Futures)
//  Version    : 1.0 — Avril 2026
//
//  LOGIQUE :
//  ─────────
//  LONG  → Candle ROUGE (corps 10-30 pts) + ADX(14)≤20
//           Attendre breakout du High rouge + 2 pts (max 3 barres)
//           SL = entrée - 15 pts | TP = entrée + 40 pts
//
//  SHORT → Candle VERTE (corps 10-30 pts) + ADX(14)≤20
//           Attendre breakout du Low vert - 2 pts (max 3 barres)
//           SL = entrée + 15 pts | TP = entrée - 40 pts
//
//  Conversion NQ : 1 point = 4 ticks (tick size = 0.25 pt)
//  SL 15 pts = 60 ticks | TP 40 pts = 160 ticks
//
//  Heures actives : 09h30 – 15h50 ET (heure de New York)
//  Sortie forcée  : 15h50 ET / 21h50 UTC
// =============================================================================

#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class NQReversalStrategy : Strategy
    {
        // ─────────────────────────────────────────────────────────────────────
        //  PARAMÈTRES CONFIGURABLES (visibles dans l'interface NinjaTrader)
        // ─────────────────────────────────────────────────────────────────────

        [NinjaScriptProperty]
        [Range(1, 50)]
        [Display(Name = "ADX Seuil (≤)", Description = "ADX(14) doit être ≤ à cette valeur pour valider le signal", Order = 1, GroupName = "Filtres")]
        public int AdxThreshold { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Corps Min (points)", Description = "Taille minimale du corps de la bougie signal (en points NQ)", Order = 2, GroupName = "Filtres")]
        public int BodyMinPts { get; set; }

        [NinjaScriptProperty]
        [Range(1, 200)]
        [Display(Name = "Corps Max (points)", Description = "Taille maximale du corps de la bougie signal (en points NQ)", Order = 3, GroupName = "Filtres")]
        public int BodyMaxPts { get; set; }

        [NinjaScriptProperty]
        [Range(1, 20)]
        [Display(Name = "Breakout (points)", Description = "Points au-dessus du High (ou sous le Low) pour déclencher l'entrée", Order = 4, GroupName = "Filtres")]
        public int BreakoutPts { get; set; }

        [NinjaScriptProperty]
        [Range(1, 10)]
        [Display(Name = "Barres d'attente max", Description = "Nombre maximum de barres pour attendre le breakout avant expiration du signal", Order = 5, GroupName = "Filtres")]
        public int MaxWaitBars { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Stop Loss (points)", Description = "Distance du Stop Loss en points NQ (1 pt = 4 ticks)", Order = 1, GroupName = "Gestion du risque")]
        public int SlPoints { get; set; }

        [NinjaScriptProperty]
        [Range(1, 200)]
        [Display(Name = "Take Profit (points)", Description = "Distance du Take Profit en points NQ (1 pt = 4 ticks)", Order = 2, GroupName = "Gestion du risque")]
        public int TpPoints { get; set; }

        [NinjaScriptProperty]
        [Range(1, 10)]
        [Display(Name = "Nombre de contrats", Description = "Nombre de contrats NQ à trader (1 ou 2 recommandé)", Order = 3, GroupName = "Gestion du risque")]
        public int Contracts { get; set; }

        // ─────────────────────────────────────────────────────────────────────
        //  VARIABLES INTERNES (état du signal en cours)
        // ─────────────────────────────────────────────────────────────────────

        private ADX adxIndicator;   // Indicateur ADX(14) calculé automatiquement

        private int    signalBar       = -1; // Numéro de la barre qui a généré le signal (-1 = aucun signal)
        private double signalLevel     = 0;  // Niveau de prix à franchir pour entrer
        private int    signalDirection = 0;  // Direction : +1 = LONG, -1 = SHORT, 0 = aucun

        // ─────────────────────────────────────────────────────────────────────
        //  INITIALISATION DE LA STRATÉGIE
        // ─────────────────────────────────────────────────────────────────────

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                // Métadonnées affichées dans NinjaTrader
                Name        = "NQ Reversal ADX Strategy";
                Description = "Stratégie de retournement NQ 10M — Candle rouge/verte + ADX≤20 + Breakout +2 pts\n"
                            + "LONG sur breakout du High rouge | SHORT sur breakout du Low vert\n"
                            + "Heures : 09h30–15h50 ET uniquement";

                // Mode de calcul : à la clôture de chaque bougie
                Calculate = Calculate.OnBarClose;

                // Activer la sortie automatique à la fin de session
                IsExitOnSessionCloseStrategy = true;
                // Sortir 600 secondes (10 min) avant la fermeture de session = ~15h50 ET
                ExitOnSessionCloseSeconds    = 600;

                // Valeurs par défaut des paramètres configurables
                AdxThreshold = 20;
                BodyMinPts   = 10;
                BodyMaxPts   = 30;
                BreakoutPts  = 2;
                MaxWaitBars  = 3;
                SlPoints     = 15;
                TpPoints     = 40;
                Contracts    = 1;
            }
            else if (State == State.Configure)
            {
                // Attacher l'indicateur ADX(14) à la stratégie
                // Il sera calculé automatiquement sur les données du graphique
                adxIndicator = ADX(14);
            }
        }

        // ─────────────────────────────────────────────────────────────────────
        //  LOGIQUE PRINCIPALE — exécutée à chaque clôture de bougie
        // ─────────────────────────────────────────────────────────────────────

        protected override void OnBarUpdate()
        {
            // Attendre au moins 20 barres pour que l'ADX soit calculé correctement
            if (CurrentBar < 20) return;

            // ── FILTRE HORAIRE ───────────────────────────────────────────────
            // On ne trade qu'entre 09h30 et 15h50 heure ET (New York)
            // NinjaTrader utilise l'heure configurée dans les propriétés de l'instrument
            // Vérifier que le graphique NQ est configuré sur "Eastern Time" (US/Eastern)
            int barTime = ToTime(Time[0]); // Format HHMMSS (ex: 093000 = 09h30:00)
            if (barTime < 093000 || barTime >= 155000) return;

            // ── CALCUL DES VARIABLES DE LA BOUGIE ACTUELLE ──────────────────
            double body     = Math.Abs(Close[0] - Open[0]); // Corps de la bougie en points
            double adxValue = adxIndicator[0];               // Valeur actuelle de l'ADX(14)
            bool   isRed    = Close[0] < Open[0];            // Bougie rouge (baissière)
            bool   isGreen  = Close[0] > Open[0];            // Bougie verte (haussière)
            bool   bodyOk   = body >= BodyMinPts && body <= BodyMaxPts; // Corps dans la plage
            bool   adxOk    = adxValue <= AdxThreshold;     // Marché sans tendance forte

            // ── DÉTECTION D'UN NOUVEAU SIGNAL ───────────────────────────────
            // On cherche un nouveau signal seulement si aucun signal n'est en attente
            // ET si on n'a pas de position ouverte
            if (signalBar == -1 && Position.MarketPosition == MarketPosition.Flat)
            {
                if (isRed && bodyOk && adxOk)
                {
                    // ✅ SIGNAL LONG détecté
                    // Bougie rouge + corps valide + ADX faible
                    // → Attendre que le prix franchisse le High + BreakoutPts
                    signalBar       = CurrentBar;
                    signalLevel     = High[0] + BreakoutPts;  // Ex: High=18500, Level=18502
                    signalDirection = 1;                       // 1 = LONG

                    Print($"[NQ] Signal LONG détecté | Barre {CurrentBar} | Niveau entrée: {signalLevel:F2} | ADX: {adxValue:F1}");
                }
                else if (isGreen && bodyOk && adxOk)
                {
                    // ✅ SIGNAL SHORT détecté
                    // Bougie verte + corps valide + ADX faible
                    // → Attendre que le prix passe sous le Low - BreakoutPts
                    signalBar       = CurrentBar;
                    signalLevel     = Low[0] - BreakoutPts;   // Ex: Low=18500, Level=18498
                    signalDirection = -1;                      // -1 = SHORT

                    Print($"[NQ] Signal SHORT détecté | Barre {CurrentBar} | Niveau entrée: {signalLevel:F2} | ADX: {adxValue:F1}");
                }
            }

            // ── GESTION DU SIGNAL EN ATTENTE ────────────────────────────────
            // Si un signal est actif ET qu'on n'a pas de position → surveiller le breakout
            if (signalBar != -1 && Position.MarketPosition == MarketPosition.Flat)
            {
                int barsElapsed = CurrentBar - signalBar; // Barres écoulées depuis le signal

                if (barsElapsed > MaxWaitBars)
                {
                    // ❌ Signal expiré — trop de barres sans breakout
                    Print($"[NQ] Signal expiré après {barsElapsed} barres | Niveau: {signalLevel:F2}");
                    ResetSignal();
                }
                else if (signalDirection == 1 && High[0] >= signalLevel)
                {
                    // 🚀 ENTRÉE LONG — le High de cette bougie atteint ou dépasse le niveau cible
                    // Conversion : points → ticks (1 point NQ = 4 ticks car tick=0.25 pt)
                    // SL = 15 pts × 4 = 60 ticks sous l'entrée
                    // TP = 40 pts × 4 = 160 ticks au-dessus de l'entrée
                    Print($"[NQ] ENTRÉE LONG | Prix: {signalLevel:F2} | SL: {SlPoints} pts | TP: {TpPoints} pts | Contrats: {Contracts}");

                    EnterLong(Contracts, "Long_NQ");
                    SetStopLoss("Long_NQ",    CalculationMode.Ticks, SlPoints * 4, false);
                    SetProfitTarget("Long_NQ", CalculationMode.Ticks, TpPoints * 4);

                    ResetSignal();
                }
                else if (signalDirection == -1 && Low[0] <= signalLevel)
                {
                    // 🔻 ENTRÉE SHORT — le Low de cette bougie atteint ou passe sous le niveau cible
                    // SL = 15 pts × 4 = 60 ticks au-dessus de l'entrée
                    // TP = 40 pts × 4 = 160 ticks sous l'entrée
                    Print($"[NQ] ENTRÉE SHORT | Prix: {signalLevel:F2} | SL: {SlPoints} pts | TP: {TpPoints} pts | Contrats: {Contracts}");

                    EnterShort(Contracts, "Short_NQ");
                    SetStopLoss("Short_NQ",    CalculationMode.Ticks, SlPoints * 4, false);
                    SetProfitTarget("Short_NQ", CalculationMode.Ticks, TpPoints * 4);

                    ResetSignal();
                }
            }
        }

        // ─────────────────────────────────────────────────────────────────────
        //  MÉTHODE UTILITAIRE — Réinitialiser l'état du signal
        // ─────────────────────────────────────────────────────────────────────

        private void ResetSignal()
        {
            signalBar       = -1;
            signalLevel     = 0;
            signalDirection = 0;
        }
    }
}
