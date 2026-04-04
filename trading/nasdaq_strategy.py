#!/usr/bin/env python3
"""
NAS100 Reversal Strategy — Candle Exhaustion Pattern
Timeframe: 10M (backtest on 1H proxy + 5M→10M validation)
Instrument: QQQ (NASDAQ ETF proxy)

Usage:
  python trading/nasdaq_strategy.py --mode backtest
  python trading/nasdaq_strategy.py --mode optimize
  python trading/nasdaq_strategy.py --mode walkforward
  python trading/nasdaq_strategy.py --mode report
"""

import argparse
import warnings
import sys
from datetime import datetime, timedelta
from itertools import product

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # Backend non-interactif pour sauvegarder en PNG
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.table import Table
import yfinance as yf

warnings.filterwarnings('ignore')

# ─────────────────────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────────────────────
CAPITAL_INITIAL = 10_000.0   # USDC
FRAIS_ALLER     = 0.0005     # 0.05% par sens
FRAIS_TOTAL     = FRAIS_ALLER * 2  # 0.10% aller-retour
TICKER          = "QQQ"

# Grille d'optimisation par défaut
PARAM_GRID_DEFAULT = {
    "body_ratio":       [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
    "indecision_ratio": [0.1, 0.2, 0.3, 0.4],
    "sl_atr":           [0.5, 1.0, 1.5, 2.0],
    "tp_atr":           [1.0, 1.5, 2.0, 3.0, 4.0],
}


# ─────────────────────────────────────────────────────────────
# 1. TÉLÉCHARGEMENT DES DONNÉES
# ─────────────────────────────────────────────────────────────
def download_data(ticker: str = TICKER, interval: str = "1h", period: str = "2y") -> pd.DataFrame:
    """
    Télécharge les données OHLCV via yfinance.
    Retourne un DataFrame propre avec colonnes : open, high, low, close, volume
    Si QQQ échoue, essaie NQ=F puis ^NDX.
    """
    tickers_to_try = [ticker, "NQ=F", "^NDX"] if ticker == TICKER else [ticker]

    for tk in tickers_to_try:
        try:
            print(f"  📥 Téléchargement {tk} [{interval}, {period}]...")
            df = yf.download(tk, interval=interval, period=period, progress=False, auto_adjust=True)
            if df.empty:
                print(f"  ⚠️  Données vides pour {tk}, on essaie le suivant...")
                continue

            # Aplatir les colonnes multi-index si présentes (yfinance v0.2+)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Normaliser les noms de colonnes en minuscules
            df.columns = [c.lower() for c in df.columns]

            # Garder uniquement les colonnes OHLCV
            df = df[['open', 'high', 'low', 'close', 'volume']].copy()

            # Supprimer les lignes avec NaN
            df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)

            # Supprimer les bougies sans volume (souvent des artefacts)
            df = df[df['close'] > 0].copy()

            print(f"  ✅ {tk} : {len(df)} bougies ({df.index[0].date()} → {df.index[-1].date()})")
            return df

        except Exception as e:
            print(f"  ❌ Erreur pour {tk} : {e}")
            continue

    raise RuntimeError(f"Impossible de télécharger les données pour {ticker} et ses alternatives.")


# ─────────────────────────────────────────────────────────────
# 2. RESAMPLE 5M → 10M
# ─────────────────────────────────────────────────────────────
def resample_5m_to_10m(df: pd.DataFrame) -> pd.DataFrame:
    """
    Resample un DataFrame 5 minutes en 10 minutes.
    Règles OHLCV : open=first, high=max, low=min, close=last, volume=sum
    """
    df_resampled = df.resample('10min').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
        'volume': 'sum',
    })
    # Supprimer les barres vides (heures de marché fermé)
    df_resampled.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
    df_resampled = df_resampled[df_resampled['close'] > 0].copy()
    print(f"  🔄 Resample 5m→10m : {len(df_resampled)} bougies")
    return df_resampled


# ─────────────────────────────────────────────────────────────
# 3. CALCUL ATR
# ─────────────────────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calcule l'ATR (Average True Range) sur `period` bougies.
    True Range = max(high-low, |high-close_prev|, |low-close_prev|)
    """
    high  = df['high']
    low   = df['low']
    close = df['close']

    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Première valeur : moyenne simple, puis EWM (méthode Wilder)
    atr = tr.ewm(span=period, min_periods=period, adjust=False).mean()
    return atr


# ─────────────────────────────────────────────────────────────
# 4. DÉTECTION DES SIGNAUX
# ─────────────────────────────────────────────────────────────
def detect_signals(df: pd.DataFrame, body_ratio: float = 0.3,
                   indecision_ratio: float = 0.25) -> pd.DataFrame:
    """
    Détecte les signaux d'entrée et de sortie.

    Signaux d'entrée :
      +1  (LONG)  : candle rouge après candle vert avec pentes baissières
      -1  (SHORT) : candle vert après candle rouge avec pentes haussières

    Signal de sortie :
      exit_signal = True si le corps du candle < indecision_ratio * ATR(14)
      (candle d'indécision = épuisement du mouvement)

    body_ratio : le signal n'est valide que si le corps des 2 candles
                 est > body_ratio * ATR (filtre les petites bougies)
    """
    df = df.copy()

    # Corps et direction des bougies
    df['body']      = (df['close'] - df['open']).abs()
    df['is_green']  = df['close'] > df['open']   # Bougie verte (haussière)
    df['is_red']    = df['close'] < df['open']   # Bougie rouge (baissière)

    # ATR
    df['atr'] = compute_atr(df, period=14)

    # Valeurs du candle précédent
    df['prev_high']     = df['high'].shift(1)
    df['prev_low']      = df['low'].shift(1)
    df['prev_close']    = df['close'].shift(1)
    df['prev_open']     = df['open'].shift(1)
    df['prev_body']     = df['body'].shift(1)
    df['prev_is_green'] = df['is_green'].shift(1)
    df['prev_is_red']   = df['is_red'].shift(1)

    # ── Signal VENTE (SHORT) ──
    # Candle courant = VERT, candle précédent = ROUGE
    # HIGH(vert) > HIGH(rouge) : pente haussière des hauts
    # LOW(vert)  > LOW(rouge)  : pente haussière des bas
    # → Épuisement haussier → on va shorter
    cond_short = (
        df['is_green']                          # Candle courant = vert
        & df['prev_is_red']                     # Candle précédent = rouge
        & (df['high'] > df['prev_high'])        # Hauts croissants
        & (df['low']  > df['prev_low'])         # Bas croissants
        & (df['body'] > body_ratio * df['atr'])          # Corps vert significatif
        & (df['prev_body'] > body_ratio * df['atr'])     # Corps rouge significatif
    )

    # ── Signal ACHAT (LONG) ──
    # Candle courant = ROUGE, candle précédent = VERT
    # LOW(rouge)  < LOW(vert)  : pente baissière des bas
    # HIGH(rouge) < HIGH(vert) : pente baissière des hauts
    # → Épuisement baissier → on va longer
    cond_long = (
        df['is_red']                            # Candle courant = rouge
        & df['prev_is_green']                   # Candle précédent = vert
        & (df['low']  < df['prev_low'])         # Bas décroissants
        & (df['high'] < df['prev_high'])        # Hauts décroissants
        & (df['body'] > body_ratio * df['atr'])          # Corps rouge significatif
        & (df['prev_body'] > body_ratio * df['atr'])     # Corps vert significatif
    )

    df['signal'] = 0
    df.loc[cond_long,  'signal'] = 1
    df.loc[cond_short, 'signal'] = -1

    # ── Signal de sortie : candle d'indécision ──
    # Corps < indecision_ratio * ATR → marché hésitant → on sort
    df['exit_signal'] = df['body'] < (indecision_ratio * df['atr'])

    # Nettoyer les colonnes intermédiaires
    cols_temp = ['prev_high','prev_low','prev_close','prev_open',
                 'prev_body','prev_is_green','prev_is_red']
    df.drop(columns=cols_temp, inplace=True, errors='ignore')

    return df


# ─────────────────────────────────────────────────────────────
# 5. BACKTEST
# ─────────────────────────────────────────────────────────────
def backtest(df: pd.DataFrame, sl_atr: float = 1.0, tp_atr: float = 2.0,
             body_ratio: float = 0.3, indecision_ratio: float = 0.25) -> dict:
    """
    Simule les trades sur les données.
    - Capital initial : 10 000 USDC
    - Taille de position : 100% du capital disponible
    - Frais : 0.05% par sens (0.10% aller-retour)
    - Sortie : SL / TP en multiple ATR, ou candle d'indécision
    Retourne : {trades, equity_curve, stats}
    """
    # Détection des signaux
    df = detect_signals(df, body_ratio=body_ratio, indecision_ratio=indecision_ratio)
    df = df.dropna(subset=['atr']).copy()

    capital      = CAPITAL_INITIAL
    position     = 0        # +1 = long, -1 = short, 0 = hors marché
    entry_price  = 0.0
    entry_atr    = 0.0
    entry_time   = None
    sl_price     = 0.0
    tp_price     = 0.0

    trades        = []       # Liste des trades terminés
    equity_curve  = []       # Évolution du capital barre par barre

    for i, (timestamp, row) in enumerate(df.iterrows()):
        # ── Gérer la position ouverte ──
        if position != 0:
            hit_sl = False
            hit_tp = False

            if position == 1:  # LONG
                if row['low'] <= sl_price:
                    hit_sl = True
                    exit_price = sl_price
                elif row['high'] >= tp_price:
                    hit_tp = True
                    exit_price = tp_price

            else:  # SHORT (position == -1)
                if row['high'] >= sl_price:
                    hit_sl = True
                    exit_price = sl_price
                elif row['low'] <= tp_price:
                    hit_tp = True
                    exit_price = tp_price

            # Sortie sur candle d'indécision (si ni SL ni TP touché)
            if not hit_sl and not hit_tp and row['exit_signal']:
                exit_price = row['close']

            # Clôturer si condition de sortie rencontrée
            if hit_sl or hit_tp or (not hit_sl and not hit_tp and row['exit_signal']):
                # PnL brut (en %)
                if position == 1:
                    pnl_pct = (exit_price - entry_price) / entry_price
                else:
                    pnl_pct = (entry_price - exit_price) / entry_price

                pnl_net = pnl_pct - FRAIS_TOTAL
                pnl_usdc = capital * pnl_net
                capital += pnl_usdc

                raison = "SL" if hit_sl else ("TP" if hit_tp else "Indécision")
                duree_bars = i - df.index.get_loc(entry_time)

                trades.append({
                    'entry_time':  entry_time,
                    'exit_time':   timestamp,
                    'direction':   'LONG' if position == 1 else 'SHORT',
                    'entry_price': entry_price,
                    'exit_price':  exit_price,
                    'pnl_pct':     pnl_net * 100,
                    'pnl_usdc':    pnl_usdc,
                    'capital':     capital,
                    'raison':      raison,
                    'duree_bars':  duree_bars,
                })

                position = 0

        # ── Chercher une entrée (si hors marché) ──
        if position == 0 and row['signal'] != 0:
            position    = row['signal']
            entry_price = row['close']
            entry_atr   = row['atr']
            entry_time  = timestamp

            if position == 1:   # LONG
                sl_price = entry_price - sl_atr * entry_atr
                tp_price = entry_price + tp_atr * entry_atr
            else:               # SHORT
                sl_price = entry_price + sl_atr * entry_atr
                tp_price = entry_price - tp_atr * entry_atr

            # Frais d'entrée
            capital -= capital * FRAIS_ALLER

        equity_curve.append({'time': timestamp, 'capital': capital})

    # Fermer la position ouverte en fin de données (au cours de clôture)
    if position != 0:
        exit_price = df.iloc[-1]['close']
        if position == 1:
            pnl_pct = (exit_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - exit_price) / entry_price
        pnl_net   = pnl_pct - FRAIS_ALLER  # seulement la sortie
        pnl_usdc  = capital * pnl_net
        capital  += pnl_usdc
        trades.append({
            'entry_time':  entry_time,
            'exit_time':   df.index[-1],
            'direction':   'LONG' if position == 1 else 'SHORT',
            'entry_price': entry_price,
            'exit_price':  exit_price,
            'pnl_pct':     pnl_net * 100,
            'pnl_usdc':    pnl_usdc,
            'capital':     capital,
            'raison':      'Fin données',
            'duree_bars':  len(df) - df.index.get_loc(entry_time),
        })

    # Calcul des statistiques
    stats = compute_stats(trades, equity_curve)
    stats['params'] = {
        'sl_atr': sl_atr, 'tp_atr': tp_atr,
        'body_ratio': body_ratio, 'indecision_ratio': indecision_ratio,
    }

    return {
        'trades':       trades,
        'equity_curve': pd.DataFrame(equity_curve).set_index('time') if equity_curve else pd.DataFrame(),
        'stats':        stats,
    }


# ─────────────────────────────────────────────────────────────
# 6. STATISTIQUES
# ─────────────────────────────────────────────────────────────
def compute_stats(trades: list, equity_curve: list) -> dict:
    """
    Calcule les statistiques de performance d'une série de trades.
    Retourne un dict avec win_rate, profit_factor, sharpe, max_dd, etc.
    """
    if not trades:
        return {
            'n_trades': 0, 'win_rate': 0, 'profit_factor': 0,
            'sharpe': 0, 'max_dd': 0, 'total_pnl': 0,
            'avg_trade': 0, 'avg_bars': 0, 'trades_per_month': 0,
        }

    pnls = [t['pnl_usdc'] for t in trades]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]

    win_rate      = len(wins) / len(pnls) * 100
    profit_factor = sum(wins) / abs(sum(loss)) if loss else float('inf')
    total_pnl     = sum(pnls)
    avg_trade     = np.mean(pnls)
    avg_bars      = np.mean([t['duree_bars'] for t in trades])

    # Durée totale en mois
    t_start = trades[0]['entry_time']
    t_end   = trades[-1]['exit_time']
    if hasattr(t_start, 'to_pydatetime'):
        t_start = t_start.to_pydatetime()
    if hasattr(t_end, 'to_pydatetime'):
        t_end = t_end.to_pydatetime()
    n_months = max(1, (t_end - t_start).days / 30)
    trades_per_month = len(pnls) / n_months

    # Sharpe ratio (annualisé sur les PnL %)
    pnl_pcts = [t['pnl_pct'] for t in trades]
    if len(pnl_pcts) > 1 and np.std(pnl_pcts) > 0:
        # Approximation : on suppose ~252 trades/an
        sharpe = (np.mean(pnl_pcts) / np.std(pnl_pcts)) * np.sqrt(252)
    else:
        sharpe = 0

    # Max drawdown
    if equity_curve:
        capitals = [e['capital'] for e in equity_curve]
        peak     = CAPITAL_INITIAL
        max_dd   = 0
        for cap in capitals:
            if cap > peak:
                peak = cap
            dd = (peak - cap) / peak * 100
            if dd > max_dd:
                max_dd = dd
    else:
        max_dd = 0

    return {
        'n_trades':        len(trades),
        'win_rate':        win_rate,
        'profit_factor':   profit_factor,
        'sharpe':          sharpe,
        'max_dd':          max_dd,
        'total_pnl':       total_pnl,
        'avg_trade':       avg_trade,
        'avg_bars':        avg_bars,
        'trades_per_month':trades_per_month,
        'final_capital':   trades[-1]['capital'] if trades else CAPITAL_INITIAL,
    }


# ─────────────────────────────────────────────────────────────
# 7. OPTIMISATION
# ─────────────────────────────────────────────────────────────
def optimize(df: pd.DataFrame, param_grid: dict = None) -> dict:
    """
    Grille de recherche (grid search) sur tous les paramètres.
    Score = Sharpe * (1 + win_rate/100) * profit_factor
    Retourne : {best_params, best_score, ranking (DataFrame)}
    """
    if param_grid is None:
        param_grid = PARAM_GRID_DEFAULT

    print("\n🔍 Optimisation en cours...")
    keys   = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(product(*values))
    total  = len(combos)
    print(f"  {total} combinaisons à tester...")

    results = []
    for idx, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        try:
            res = backtest(df, **params)
            s   = res['stats']
            if s['n_trades'] < 5:
                continue  # Trop peu de trades → résultat non significatif
            score = s['sharpe'] * (1 + s['win_rate'] / 100) * max(s['profit_factor'], 0)
            results.append({**params, **s, 'score': score})
        except Exception:
            continue

        if (idx + 1) % 50 == 0:
            print(f"  ... {idx+1}/{total} ({(idx+1)/total*100:.0f}%)")

    if not results:
        print("  ⚠️  Aucun résultat valide trouvé.")
        return {'best_params': {}, 'best_score': 0, 'ranking': pd.DataFrame()}

    ranking = pd.DataFrame(results).sort_values('score', ascending=False).reset_index(drop=True)
    best    = ranking.iloc[0]

    best_params = {k: best[k] for k in keys}
    best_score  = best['score']

    print(f"\n✅ Meilleurs paramètres trouvés :")
    print(f"   body_ratio={best_params['body_ratio']}, "
          f"indecision_ratio={best_params['indecision_ratio']}, "
          f"sl_atr={best_params['sl_atr']}, "
          f"tp_atr={best_params['tp_atr']}")
    print(f"   Win Rate: {best['win_rate']:.1f}% | "
          f"PF: {best['profit_factor']:.2f} | "
          f"Sharpe: {best['sharpe']:.2f} | "
          f"Max DD: -{best['max_dd']:.1f}%")
    print(f"   Total trades: {best['n_trades']} | "
          f"Avg/mois: {best['trades_per_month']:.2f}")

    return {'best_params': best_params, 'best_score': best_score, 'ranking': ranking}


# ─────────────────────────────────────────────────────────────
# 8. WALK-FORWARD
# ─────────────────────────────────────────────────────────────
def walk_forward(df: pd.DataFrame, train_days: int = 180,
                 test_days: int = 30, step_days: int = 30) -> dict:
    """
    Walk-forward (fenêtre glissante) :
    - Train sur train_days → optimise les paramètres
    - Test sur les test_days suivants avec ces paramètres
    - Avance de step_days et recommence
    Retourne : equity WF, params par période, résumé comparatif
    """
    print(f"\n🧠 Walk-forward (auto-learning)...")
    print(f"   Fenêtre train={train_days}j, test={test_days}j, pas={step_days}j")

    # Créer un index de dates unique
    dates = df.index.normalize().unique()

    periods      = []
    wf_trades    = []
    wf_equity    = [{'time': df.index[0], 'capital': CAPITAL_INITIAL}]
    wf_capital   = CAPITAL_INITIAL

    period_num = 0
    train_start_idx = 0

    while True:
        # Définir les fenêtres temporelles
        train_start = dates[train_start_idx] if train_start_idx < len(dates) else None
        if train_start is None:
            break

        train_end_date = train_start + timedelta(days=train_days)
        test_end_date  = train_end_date + timedelta(days=test_days)

        # Filtrer les données
        df_train = df[(df.index.normalize() >= train_start) &
                      (df.index.normalize() <  train_end_date)]
        df_test  = df[(df.index.normalize() >= train_end_date) &
                      (df.index.normalize() <  test_end_date)]

        if len(df_train) < 100 or len(df_test) < 20:
            break  # Pas assez de données

        # ── Phase d'entraînement : optimisation ──
        # Grille réduite pour la vitesse
        mini_grid = {
            "body_ratio":       [0.2, 0.3, 0.4],
            "indecision_ratio": [0.2, 0.3],
            "sl_atr":           [1.0, 1.5],
            "tp_atr":           [2.0, 3.0],
        }
        opt_result = optimize(df_train, param_grid=mini_grid)
        if not opt_result['best_params']:
            break

        best_params  = opt_result['best_params']
        train_result = backtest(df_train, **best_params)
        train_stats  = train_result['stats']

        # ── Phase de test : application des meilleurs paramètres ──
        test_result = backtest(df_test, **best_params)
        test_stats  = test_result['stats']

        # Ajouter les trades de test à la courbe WF globale
        # On rebase le capital sur le capital WF courant
        if test_result['trades']:
            scale = wf_capital / CAPITAL_INITIAL
            for trade in test_result['trades']:
                wf_trade = trade.copy()
                wf_trade['pnl_usdc'] = trade['pnl_usdc'] * scale
                wf_capital += wf_trade['pnl_usdc']
                wf_trade['capital'] = wf_capital
                wf_trades.append(wf_trade)

            for eq_point in test_result['equity_curve'].itertuples():
                cap_scaled = CAPITAL_INITIAL + (eq_point.capital - CAPITAL_INITIAL) * scale
                wf_equity.append({'time': eq_point.Index, 'capital': wf_capital})

        period_num += 1
        print(f"  Période {period_num} ({train_start.strftime('%Y-%m')} → "
              f"{train_end_date.strftime('%Y-%m')}) : "
              f"Train WR={train_stats['win_rate']:.0f}% ({train_stats['n_trades']} trades) | "
              f"Test WR={test_stats['win_rate']:.0f}% ({test_stats['n_trades']} trades)")

        periods.append({
            'period':          period_num,
            'train_start':     train_start,
            'train_end':       train_end_date,
            'test_start':      train_end_date,
            'test_end':        test_end_date,
            'params':          best_params,
            'train_wr':        train_stats['win_rate'],
            'test_wr':         test_stats['win_rate'],
            'train_pf':        train_stats['profit_factor'],
            'test_pf':         test_stats['profit_factor'],
            'train_sharpe':    train_stats['sharpe'],
            'test_sharpe':     test_stats['sharpe'],
            'train_n_trades':  train_stats['n_trades'],
            'test_n_trades':   test_stats['n_trades'],
        })

        # Avancer la fenêtre
        next_start = train_end_date + timedelta(days=step_days) - timedelta(days=train_days)
        next_start = max(next_start, train_start + timedelta(days=step_days))
        try:
            train_start_idx = np.searchsorted(dates, next_start.to_datetime64()
                                              if hasattr(next_start, 'to_datetime64')
                                              else np.datetime64(next_start))
        except Exception:
            train_start_idx += step_days
        if train_start_idx >= len(dates):
            break

    if not periods:
        print("  ⚠️  Pas assez de données pour le walk-forward.")
        return {'periods': [], 'wf_equity': pd.DataFrame(),
                'wf_trades': [], 'wf_stats': {}}

    wf_stats = compute_stats(wf_trades, wf_equity)
    wf_eq_df = pd.DataFrame(wf_equity).set_index('time') if wf_equity else pd.DataFrame()

    print(f"\n  📊 Walk-forward global : "
          f"WR={wf_stats['win_rate']:.1f}% | "
          f"Sharpe={wf_stats['sharpe']:.2f} | "
          f"Max DD=-{wf_stats['max_dd']:.1f}% | "
          f"Capital final : ${wf_capital:.0f}")

    return {
        'periods':    periods,
        'wf_equity':  wf_eq_df,
        'wf_trades':  wf_trades,
        'wf_stats':   wf_stats,
    }


# ─────────────────────────────────────────────────────────────
# 9. RAPPORT VISUEL
# ─────────────────────────────────────────────────────────────
def generate_report(backtest_result: dict, wf_result: dict = None,
                    output_path: str = "trading/nasdaq_report.png"):
    """
    Génère un rapport graphique complet en 4 panneaux :
    1. Equity curve (backtest complet + walk-forward overlay)
    2. Distribution PnL (histogramme + win/loss)
    3. Heatmap des paramètres WF par période
    4. Tableau des statistiques
    """
    fig = plt.figure(figsize=(18, 14))
    fig.patch.set_facecolor('#0d1117')
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.4, wspace=0.35)

    color_bg    = '#0d1117'
    color_panel = '#161b22'
    color_text  = '#e6edf3'
    color_green = '#3fb950'
    color_red   = '#f85149'
    color_blue  = '#58a6ff'
    color_gold  = '#d29922'

    def style_ax(ax, title):
        ax.set_facecolor(color_panel)
        ax.tick_params(colors=color_text, labelsize=9)
        ax.xaxis.label.set_color(color_text)
        ax.yaxis.label.set_color(color_text)
        ax.title.set_color(color_text)
        ax.set_title(title, fontsize=11, fontweight='bold', pad=10)
        for spine in ax.spines.values():
            spine.set_color('#30363d')

    # ── Panneau 1 : Equity Curve ──────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    style_ax(ax1, "📈 Equity Curve")

    eq = backtest_result.get('equity_curve', pd.DataFrame())
    if not eq.empty:
        ax1.plot(eq.index, eq['capital'], color=color_blue, linewidth=1.5,
                 label='Backtest complet', alpha=0.9)

    if wf_result and not wf_result['wf_equity'].empty:
        wf_eq = wf_result['wf_equity']
        ax1.plot(wf_eq.index, wf_eq['capital'], color=color_gold, linewidth=2,
                 label='Walk-forward', alpha=0.85, linestyle='--')

    ax1.axhline(y=CAPITAL_INITIAL, color='#8b949e', linestyle=':', linewidth=1)
    ax1.set_ylabel('Capital (USDC)', color=color_text)
    ax1.legend(facecolor=color_panel, labelcolor=color_text, fontsize=8)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # ── Panneau 2 : Distribution PnL ─────────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    style_ax(ax2, "💰 Distribution des PnL")

    trades = backtest_result.get('trades', [])
    if trades:
        pnls  = [t['pnl_usdc'] for t in trades]
        wins  = [p for p in pnls if p > 0]
        losses= [p for p in pnls if p <= 0]

        bins = min(40, max(10, len(pnls) // 5))
        ax2.hist(losses, bins=bins // 2, color=color_red,   alpha=0.75, label=f'Pertes ({len(losses)})')
        ax2.hist(wins,   bins=bins // 2, color=color_green, alpha=0.75, label=f'Gains ({len(wins)})')
        ax2.axvline(x=0, color='white', linestyle='--', linewidth=1)
        avg_pnl = np.mean(pnls)
        ax2.axvline(x=avg_pnl, color=color_gold, linestyle='-',
                    linewidth=1.5, label=f'Moy: ${avg_pnl:.1f}')
        ax2.set_xlabel('PnL (USDC)', color=color_text)
        ax2.set_ylabel('Fréquence', color=color_text)
        ax2.legend(facecolor=color_panel, labelcolor=color_text, fontsize=8)

    # ── Panneau 3 : Walk-forward params ──────────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    style_ax(ax3, "🧠 Walk-Forward : WR Train vs Test")

    if wf_result and wf_result.get('periods'):
        periods  = wf_result['periods']
        x        = [p['period'] for p in periods]
        train_wr = [p['train_wr'] for p in periods]
        test_wr  = [p['test_wr']  for p in periods]
        labels   = [f"P{p['period']}" for p in periods]

        width = 0.35
        x_arr = np.arange(len(x))
        bars1 = ax3.bar(x_arr - width/2, train_wr, width, color=color_blue,
                        alpha=0.8, label='Train WR')
        bars2 = ax3.bar(x_arr + width/2, test_wr,  width, color=color_gold,
                        alpha=0.8, label='Test WR')
        ax3.set_xticks(x_arr)
        ax3.set_xticklabels(labels, rotation=45, fontsize=8)
        ax3.axhline(y=50, color='#8b949e', linestyle=':', linewidth=1)
        ax3.set_ylabel('Win Rate (%)', color=color_text)
        ax3.legend(facecolor=color_panel, labelcolor=color_text, fontsize=8)
        ax3.set_ylim(0, 100)

        # Afficher les paramètres utilisés par période
        for i, p in enumerate(periods):
            bp = p['params']
            label = f"br={bp['body_ratio']}\nsl={bp['sl_atr']}\ntp={bp['tp_atr']}"
            ax3.annotate(label, xy=(i, 5), ha='center', fontsize=6,
                         color=color_text, alpha=0.7)
    else:
        ax3.text(0.5, 0.5, 'Walk-forward non exécuté', ha='center', va='center',
                 color=color_text, transform=ax3.transAxes)

    # ── Panneau 4 : Tableau des statistiques ─────────────────
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.set_facecolor(color_panel)
    ax4.axis('off')
    ax4.set_title('📊 Statistiques Complètes', fontsize=11, fontweight='bold',
                  pad=10, color=color_text)

    stats = backtest_result.get('stats', {})
    params = stats.get('params', {})

    rows = [
        ('Capital initial',    f"${CAPITAL_INITIAL:,.0f}"),
        ('Capital final',      f"${stats.get('final_capital', CAPITAL_INITIAL):,.0f}"),
        ('Total PnL',          f"${stats.get('total_pnl', 0):+,.2f}"),
        ('Retour total',       f"{(stats.get('final_capital', CAPITAL_INITIAL) / CAPITAL_INITIAL - 1)*100:+.1f}%"),
        ('─────────────',      '─────────'),
        ('Total trades',       f"{stats.get('n_trades', 0)}"),
        ('Win Rate',           f"{stats.get('win_rate', 0):.1f}%"),
        ('Profit Factor',      f"{stats.get('profit_factor', 0):.2f}"),
        ('Sharpe (annualisé)', f"{stats.get('sharpe', 0):.2f}"),
        ('Max Drawdown',       f"-{stats.get('max_dd', 0):.1f}%"),
        ('─────────────',      '─────────'),
        ('Trades / mois',      f"{stats.get('trades_per_month', 0):.1f}"),
        ('Durée moy. (bars)',  f"{stats.get('avg_bars', 0):.1f}"),
        ('PnL moyen / trade',  f"${stats.get('avg_trade', 0):+.2f}"),
        ('─────────────',      '─────────'),
        ('body_ratio',         str(params.get('body_ratio', '-'))),
        ('indecision_ratio',   str(params.get('indecision_ratio', '-'))),
        ('SL (× ATR)',         str(params.get('sl_atr', '-'))),
        ('TP (× ATR)',         str(params.get('tp_atr', '-'))),
    ]

    y_pos = 0.97
    for label, value in rows:
        if '───' in label:
            ax4.plot([0.02, 0.98], [y_pos + 0.01, y_pos + 0.01],
                     color='#30363d', linewidth=0.5, transform=ax4.transAxes)
            y_pos -= 0.03
            continue

        # Colorer les valeurs positives/négatives
        val_color = color_text
        if value.startswith('+'):
            val_color = color_green
        elif value.startswith('-') and value != '-':
            val_color = color_red

        ax4.text(0.05, y_pos, label, transform=ax4.transAxes,
                 color='#8b949e', fontsize=9, va='top')
        ax4.text(0.65, y_pos, value, transform=ax4.transAxes,
                 color=val_color, fontsize=9, va='top', fontweight='bold')
        y_pos -= 0.05

    # Titre global
    fig.suptitle(
        f'NAS100 Reversal Strategy — QQQ   |   {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=13, fontweight='bold', color=color_text, y=0.98
    )

    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=color_bg, edgecolor='none')
    plt.close()
    print(f"\n✅ Rapport sauvegardé : {output_path}")


# ─────────────────────────────────────────────────────────────
# 10. POINT D'ENTRÉE PRINCIPAL
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="NAS100 Reversal Strategy")
    parser.add_argument('--mode', choices=['backtest', 'optimize', 'walkforward', 'report'],
                        default='report', help='Mode d\'exécution')
    parser.add_argument('--ticker', default=TICKER, help='Ticker yfinance')
    parser.add_argument('--interval', default='1h', help='Intervalle (1h, 5m, ...)')
    args = parser.parse_args()

    print("=" * 60)
    print("  NAS100 REVERSAL STRATEGY — Candle Exhaustion Pattern")
    print("=" * 60)

    # ── Téléchargement des données principales ──
    print("\n📦 Chargement des données...")
    df_1h = download_data(args.ticker, interval='1h', period='2y')

    # ── Validation court terme : 5m → 10m ──
    df_10m = None
    try:
        df_5m  = download_data(args.ticker, interval='5m', period='60d')
        df_10m = resample_5m_to_10m(df_5m)
    except Exception as e:
        print(f"  ⚠️  Données 5m indisponibles ({e}). Validation court terme ignorée.")

    # ── MODES ──
    if args.mode == 'backtest':
        print("\n🔄 Backtest avec paramètres par défaut...")
        result = backtest(df_1h, sl_atr=1.5, tp_atr=3.0,
                          body_ratio=0.3, indecision_ratio=0.2)
        s = result['stats']
        print(f"\nRésultats backtest (1H, 2 ans) :")
        print(f"  Win Rate   : {s['win_rate']:.1f}%")
        print(f"  PF         : {s['profit_factor']:.2f}")
        print(f"  Sharpe     : {s['sharpe']:.2f}")
        print(f"  Max DD     : -{s['max_dd']:.1f}%")
        print(f"  Trades     : {s['n_trades']} | {s['trades_per_month']:.1f}/mois")
        print(f"  Capital    : ${CAPITAL_INITIAL:,.0f} → ${s['final_capital']:,.0f} "
              f"({(s['final_capital']/CAPITAL_INITIAL-1)*100:+.1f}%)")

    elif args.mode == 'optimize':
        opt = optimize(df_1h)
        if df_10m is not None and len(df_10m) > 50:
            print("\n🔄 Validation sur données 5m→10m (60 jours) :")
            val = backtest(df_10m, **opt['best_params'])
            sv  = val['stats']
            print(f"  Win Rate : {sv['win_rate']:.1f}% | "
                  f"PF : {sv['profit_factor']:.2f} | "
                  f"Sharpe : {sv['sharpe']:.2f}")

    elif args.mode == 'walkforward':
        wf = walk_forward(df_1h, train_days=180, test_days=30, step_days=30)

    elif args.mode == 'report':
        # ── Mode rapport complet ──
        print("\n🔍 Optimisation pour le rapport...")
        opt = optimize(df_1h, param_grid={
            "body_ratio":       [0.2, 0.3, 0.4],
            "indecision_ratio": [0.2, 0.3],
            "sl_atr":           [1.0, 1.5, 2.0],
            "tp_atr":           [2.0, 3.0, 4.0],
        })

        best_params = opt['best_params']
        if not best_params:
            best_params = {
                'sl_atr': 1.5, 'tp_atr': 3.0,
                'body_ratio': 0.3, 'indecision_ratio': 0.2,
            }

        print("\n🔄 Backtest complet avec meilleurs paramètres...")
        bt_result = backtest(df_1h, **best_params)

        print("\n🧠 Walk-forward (auto-learning)...")
        wf_result = walk_forward(df_1h, train_days=180, test_days=30, step_days=30)

        generate_report(bt_result, wf_result,
                        output_path="trading/nasdaq_report.png")

        # Résumé console
        s = bt_result['stats']
        print("\n" + "=" * 60)
        print("  RÉSUMÉ FINAL")
        print("=" * 60)
        print(f"  Best params: body_ratio={best_params['body_ratio']}, "
              f"indecision_ratio={best_params['indecision_ratio']}, "
              f"sl_atr={best_params['sl_atr']}, "
              f"tp_atr={best_params['tp_atr']}")
        print(f"  Win Rate: {s['win_rate']:.1f}% | "
              f"PF: {s['profit_factor']:.2f} | "
              f"Sharpe: {s['sharpe']:.2f} | "
              f"Max DD: -{s['max_dd']:.1f}%")
        print(f"  Total trades: {s['n_trades']} | "
              f"Avg/mois: {s['trades_per_month']:.2f}")
        print(f"  Capital: ${CAPITAL_INITIAL:,.0f} → ${s['final_capital']:,.0f} "
              f"({(s['final_capital']/CAPITAL_INITIAL-1)*100:+.1f}%)")


if __name__ == "__main__":
    main()
