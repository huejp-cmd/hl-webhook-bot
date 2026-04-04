#!/usr/bin/env python3
"""
NAS100 Reversal Strategy — Candle Exhaustion + Indecision Breakout
Timeframe : 10M (backtest sur 1H proxy + validation 5M→10M)
Instrument : QQQ (proxy NASDAQ ETF)

Flow en 6 étapes :
  1. Détecter le pattern d'épuisement (2 bougies alternées avec pentes)
  2. Attendre un candle d'indécision (doji/spinning top) — max `max_wait_bars` barres
  3. Entrer sur le breakout du candle d'indécision
  4. SL = taille du corps du candle signal (filtré 0.5$–1.5$ QQQ ≈ 10–30 pts NQ)
  5. TP = SL × rr_ratio
  6. Sortie alternative : signal opposé ou fin de session

Usage :
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
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import yfinance as yf

warnings.filterwarnings('ignore')

# ─────────────────────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────────────────────
CAPITAL_INITIAL = 10_000.0   # USDC
FRAIS_ALLER     = 0.0005     # 0.05% par sens
FRAIS_TOTAL     = FRAIS_ALLER * 2  # 0.10% aller-retour

TICKER          = "QQQ"

# Filtrage SL : corps du candle signal entre 10 et 30 pts NQ
# 1 pt NQ ≈ 0.05$ sur QQQ  →  10 pts = 0.50$, 30 pts = 1.50$
MIN_SL_QQQ = 0.50
MAX_SL_QQQ = 1.50

# États de la machine à états du backtest
IDLE                = 0   # En attente d'un pattern d'épuisement
WAITING_INDECISION  = 1   # Pattern trouvé, attente d'un candle d'indécision
WAITING_BREAKOUT    = 2   # Indécision trouvée, attente du breakout
IN_POSITION         = 3   # Position ouverte

# Grille d'optimisation par défaut
PARAM_GRID_DEFAULT = {
    "body_pct":      [0.20, 0.25, 0.30, 0.35, 0.40],  # % range totale → indécision
    "rr_ratio":      [1.0, 1.5, 2.0, 3.0],              # Risk/Reward TP
    "max_wait_bars": [2, 3, 5],                          # Barres max d'attente
}


# ─────────────────────────────────────────────────────────────
# 1. TÉLÉCHARGEMENT DES DONNÉES
# ─────────────────────────────────────────────────────────────
def download_data(ticker: str = TICKER, interval: str = "1h",
                  period: str = "2y") -> pd.DataFrame:
    """
    Télécharge les données OHLCV via yfinance.
    Essaie QQQ → NQ=F → ^NDX en cas d'échec.
    Retourne un DataFrame propre avec colonnes : open, high, low, close, volume
    """
    tickers_to_try = [ticker, "NQ=F", "^NDX"] if ticker == TICKER else [ticker]

    for tk in tickers_to_try:
        try:
            print(f"  📥 Téléchargement {tk} [{interval}, {period}]...")
            df = yf.download(tk, interval=interval, period=period,
                             progress=False, auto_adjust=True)
            if df.empty:
                print(f"  ⚠️  Données vides pour {tk}, essai suivant...")
                continue

            # Aplatir les colonnes multi-index (yfinance v0.2+)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df.columns = [c.lower() for c in df.columns]
            df = df[['open', 'high', 'low', 'close', 'volume']].copy()
            df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
            df = df[df['close'] > 0].copy()

            print(f"  ✅ {tk} : {len(df)} bougies "
                  f"({df.index[0].date()} → {df.index[-1].date()})")
            return df

        except Exception as e:
            print(f"  ❌ Erreur pour {tk} : {e}")
            continue

    raise RuntimeError(
        f"Impossible de télécharger les données pour {ticker} et ses alternatives.")


# ─────────────────────────────────────────────────────────────
# 2. RESAMPLE 5M → 10M
# ─────────────────────────────────────────────────────────────
def resample_5m_to_10m(df: pd.DataFrame) -> pd.DataFrame:
    """
    Resample 5 minutes → 10 minutes.
    OHLCV : open=first, high=max, low=min, close=last, volume=sum
    """
    df_r = df.resample('10min').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
        'volume': 'sum',
    })
    df_r.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
    df_r = df_r[df_r['close'] > 0].copy()
    print(f"  🔄 Resample 5m→10m : {len(df_r)} bougies")
    return df_r


# ─────────────────────────────────────────────────────────────
# 3. CALCUL ATR (utilisé pour référence dans le rapport)
# ─────────────────────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR via méthode Wilder (EWM)."""
    h, l, c = df['high'], df['low'], df['close']
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


# ─────────────────────────────────────────────────────────────
# 4. DÉTECTION DU PATTERN D'ÉPUISEMENT
# ─────────────────────────────────────────────────────────────
def detect_exhaustion(df: pd.DataFrame) -> pd.DataFrame:
    """
    Détecte les patterns d'épuisement directionnel sur 2 bougies consécutives.

    SELL bias (SHORT) :
      - Candle [i]   = VERT  (close > open)
      - Candle [i-1] = ROUGE (close < open)
      - HIGH[i] > HIGH[i-1]  (hauts croissants)
      - LOW[i]  > LOW[i-1]   (bas croissants)
      → Épuisement haussier → on va chercher un SHORT

    BUY bias (LONG) :
      - Candle [i]   = ROUGE (close < open)
      - Candle [i-1] = VERT  (close > open)
      - LOW[i]  < LOW[i-1]   (bas décroissants)
      - HIGH[i] < HIGH[i-1]  (hauts décroissants)
      → Épuisement baissier → on va chercher un LONG

    Ajoute les colonnes :
      - 'bias'         : +1 (long), -1 (short), 0 (neutre)
      - 'signal_body'  : taille du corps du candle signal (pour le SL)
    """
    df = df.copy()

    # Corps et direction de chaque bougie
    df['body']     = (df['close'] - df['open']).abs()
    df['is_green'] = df['close'] > df['open']
    df['is_red']   = df['close'] < df['open']

    # Valeurs du candle précédent
    df['prev_high']     = df['high'].shift(1)
    df['prev_low']      = df['low'].shift(1)
    df['prev_is_green'] = df['is_green'].shift(1)
    df['prev_is_red']   = df['is_red'].shift(1)
    df['prev_body']     = df['body'].shift(1)

    # ── SELL bias ──
    cond_short = (
        df['is_green']
        & df['prev_is_red']
        & (df['high'] > df['prev_high'])
        & (df['low']  > df['prev_low'])
    )

    # ── BUY bias ──
    cond_long = (
        df['is_red']
        & df['prev_is_green']
        & (df['low']  < df['prev_low'])
        & (df['high'] < df['prev_high'])
    )

    df['bias'] = 0
    df.loc[cond_long,  'bias'] = 1
    df.loc[cond_short, 'bias'] = -1

    # Corps du candle signal (= candle courant dans les deux cas)
    df['signal_body'] = df['body']

    # Nettoyage colonnes temporaires
    df.drop(columns=['prev_high', 'prev_low', 'prev_is_green',
                     'prev_is_red', 'prev_body'], inplace=True, errors='ignore')
    return df


# ─────────────────────────────────────────────────────────────
# 5. TEST D'INDÉCISION
# ─────────────────────────────────────────────────────────────
def is_indecision(row: pd.Series, body_pct: float = 0.30) -> bool:
    """
    Vérifie si une bougie est un candle d'indécision (doji/spinning top) :
      - Corps < body_pct × range totale (high-low)
      - Mèche haute > 0  (upper_wick > 0)
      - Mèche basse > 0  (lower_wick > 0)

    body_pct par défaut = 0.30 (corps < 30% de la range)
    """
    total_range = row['high'] - row['low']
    if total_range <= 0:
        return False

    body        = abs(row['close'] - row['open'])
    upper_wick  = row['high'] - max(row['open'], row['close'])
    lower_wick  = min(row['open'], row['close']) - row['low']

    corps_ok    = body < body_pct * total_range
    mèche_haute = upper_wick > 0
    mèche_basse = lower_wick > 0

    return corps_ok and mèche_haute and mèche_basse


# ─────────────────────────────────────────────────────────────
# 6. BACKTEST — MACHINE À ÉTATS
# ─────────────────────────────────────────────────────────────
def backtest(df: pd.DataFrame, rr_ratio: float = 2.0,
             body_pct: float = 0.30, max_wait_bars: int = 3) -> dict:
    """
    Simule les trades avec la stratégie en 6 étapes :

    Capital initial : 10 000 USDC — 100% exposé par trade
    Frais : 0.05% aller + 0.05% retour = 0.10% aller-retour

    Machine à états :
      IDLE → WAITING_INDECISION → WAITING_BREAKOUT → IN_POSITION → IDLE

    SL = corps du candle signal (filtré 0.5$–1.5$ QQQ)
    TP = SL × rr_ratio
    """
    df = detect_exhaustion(df)
    df = df.dropna(subset=['bias']).copy()
    df_arr = df.values
    cols = {c: i for i, c in enumerate(df.columns)}

    # Vérifier les colonnes nécessaires
    for col in ['open', 'high', 'low', 'close', 'bias', 'signal_body']:
        if col not in cols:
            raise ValueError(f"Colonne manquante : {col}")

    capital    = CAPITAL_INITIAL
    state      = IDLE
    bias       = 0        # +1 LONG, -1 SHORT
    sl_size    = 0.0      # Corps du candle signal (taille du SL)
    wait_count = 0        # Barres attendues dans l'état courant
    indc_high  = 0.0      # High du candle d'indécision
    indc_low   = 0.0      # Low du candle d'indécision
    entry_price = 0.0
    sl_price    = 0.0
    tp_price    = 0.0
    entry_time  = None
    entry_bar   = 0

    trades       = []
    equity_curve = []

    n = len(df)

    for i in range(n):
        row       = df.iloc[i]
        timestamp = df.index[i]

        # ── Détection de fin de session (jour suivant = fermeture forcée) ──
        is_last_bar_of_day = False
        if i + 1 < n:
            next_date = df.index[i + 1].date()
            curr_date = timestamp.date()
            if next_date != curr_date:
                is_last_bar_of_day = True
        else:
            is_last_bar_of_day = True  # Dernière barre du dataset

        # ────────────────────────────────────────────
        # ÉTAT : IN_POSITION — gérer la position ouverte
        # ────────────────────────────────────────────
        if state == IN_POSITION:
            hit_sl = False
            hit_tp = False
            exit_price = row['close']
            raison = 'En cours'

            if bias == 1:   # LONG
                if row['low'] <= sl_price:
                    hit_sl = True
                    exit_price = sl_price
                elif row['high'] >= tp_price:
                    hit_tp = True
                    exit_price = tp_price
            else:            # SHORT
                if row['high'] >= sl_price:
                    hit_sl = True
                    exit_price = sl_price
                elif row['low'] <= tp_price:
                    hit_tp = True
                    exit_price = tp_price

            # Sortie sur signal opposé (avant SL/TP)
            opposite_signal = (bias == 1 and row['bias'] == -1) or \
                              (bias == -1 and row['bias'] == 1)
            if not hit_sl and not hit_tp and opposite_signal:
                exit_price = row['close']
                raison = 'Signal opposé'

            # Sortie forcée en fin de session
            if not hit_sl and not hit_tp and not opposite_signal and is_last_bar_of_day:
                exit_price = row['close']
                raison = 'Fin session'

            # Clôturer la position si une sortie est déclenchée
            if hit_sl or hit_tp or opposite_signal or (raison == 'Fin session'):
                if hit_sl:
                    raison = 'SL'
                elif hit_tp:
                    raison = 'TP'

                if bias == 1:
                    pnl_pct = (exit_price - entry_price) / entry_price
                else:
                    pnl_pct = (entry_price - exit_price) / entry_price

                pnl_net  = pnl_pct - FRAIS_TOTAL
                pnl_usdc = capital * pnl_net
                capital += pnl_usdc
                duree    = i - entry_bar

                trades.append({
                    'entry_time':   entry_time,
                    'exit_time':    timestamp,
                    'direction':    'LONG' if bias == 1 else 'SHORT',
                    'entry_price':  entry_price,
                    'exit_price':   exit_price,
                    'sl_price':     sl_price,
                    'tp_price':     tp_price,
                    'sl_size':      sl_size,
                    'pnl_pct':      pnl_net * 100,
                    'pnl_usdc':     pnl_usdc,
                    'capital':      capital,
                    'raison':       raison,
                    'duree_bars':   duree,
                })

                state      = IDLE
                bias       = 0
                wait_count = 0

        # ────────────────────────────────────────────
        # ÉTAT : WAITING_BREAKOUT — chercher le breakout
        # ────────────────────────────────────────────
        elif state == WAITING_BREAKOUT:
            entered = False

            # Sortie du range du candle d'indécision par le bas → SHORT confirmé
            if bias == -1 and row['close'] < indc_low:
                entry_price = row['close']
                sl_price    = entry_price + sl_size   # SL au-dessus de l'entrée
                tp_price    = entry_price - sl_size * rr_ratio
                entered     = True

            # Sortie du range du candle d'indécision par le haut → LONG confirmé
            elif bias == 1 and row['close'] > indc_high:
                entry_price = entry_price = row['close']
                sl_price    = entry_price - sl_size   # SL en dessous de l'entrée
                tp_price    = entry_price + sl_size * rr_ratio
                entered     = True

            if entered:
                capital    -= capital * FRAIS_ALLER   # Frais d'entrée
                entry_time  = timestamp
                entry_bar   = i
                state       = IN_POSITION
                wait_count  = 0
            else:
                wait_count += 1
                # Timeout : on abandonne si le breakout ne vient pas
                if wait_count > max_wait_bars:
                    state      = IDLE
                    bias       = 0
                    wait_count = 0

        # ────────────────────────────────────────────
        # ÉTAT : WAITING_INDECISION — chercher le doji/spinning top
        # ────────────────────────────────────────────
        elif state == WAITING_INDECISION:
            if is_indecision(row, body_pct):
                # Candle d'indécision trouvé → mémoriser son range pour le breakout
                indc_high   = row['high']
                indc_low    = row['low']
                state       = WAITING_BREAKOUT
                wait_count  = 0
            else:
                wait_count += 1
                if wait_count > max_wait_bars:
                    state      = IDLE
                    bias       = 0
                    wait_count = 0

        # ────────────────────────────────────────────
        # ÉTAT : IDLE — chercher un pattern d'épuisement
        # ────────────────────────────────────────────
        if state == IDLE:
            detected_bias = row['bias']
            if detected_bias != 0:
                # Filtrer le signal par la taille du corps (10–30 pts NQ)
                sb = row['signal_body']
                if MIN_SL_QQQ <= sb <= MAX_SL_QQQ:
                    bias       = detected_bias
                    sl_size    = sb
                    state      = WAITING_INDECISION
                    wait_count = 0

        # Enregistrer l'equity à chaque barre
        equity_curve.append({'time': timestamp, 'capital': capital})

    # ── Fermer toute position encore ouverte (fin de données) ──
    if state == IN_POSITION:
        exit_price = df.iloc[-1]['close']
        if bias == 1:
            pnl_pct = (exit_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - exit_price) / entry_price
        pnl_net  = pnl_pct - FRAIS_ALLER
        pnl_usdc = capital * pnl_net
        capital += pnl_usdc
        trades.append({
            'entry_time':   entry_time,
            'exit_time':    df.index[-1],
            'direction':    'LONG' if bias == 1 else 'SHORT',
            'entry_price':  entry_price,
            'exit_price':   exit_price,
            'sl_price':     sl_price,
            'tp_price':     tp_price,
            'sl_size':      sl_size,
            'pnl_pct':      pnl_net * 100,
            'pnl_usdc':     pnl_usdc,
            'capital':      capital,
            'raison':       'Fin données',
            'duree_bars':   n - entry_bar,
        })

    stats = compute_stats(trades, equity_curve)
    stats['params'] = {
        'rr_ratio': rr_ratio,
        'body_pct': body_pct,
        'max_wait_bars': max_wait_bars,
    }

    return {
        'trades':       trades,
        'equity_curve': (pd.DataFrame(equity_curve).set_index('time')
                         if equity_curve else pd.DataFrame()),
        'stats':        stats,
    }


# ─────────────────────────────────────────────────────────────
# 7. STATISTIQUES
# ─────────────────────────────────────────────────────────────
def compute_stats(trades: list, equity_curve: list) -> dict:
    """
    Calcule les métriques de performance à partir de la liste de trades.
    """
    if not trades:
        return {k: 0 for k in [
            'n_trades', 'win_rate', 'profit_factor', 'sharpe',
            'max_dd', 'total_pnl', 'avg_trade', 'avg_bars',
            'trades_per_month', 'final_capital',
        ]}

    pnls  = [t['pnl_usdc'] for t in trades]
    wins  = [p for p in pnls if p > 0]
    loss  = [p for p in pnls if p <= 0]

    win_rate      = len(wins) / len(pnls) * 100
    profit_factor = (sum(wins) / abs(sum(loss))) if loss else float('inf')
    total_pnl     = sum(pnls)
    avg_trade     = float(np.mean(pnls))
    avg_bars      = float(np.mean([t['duree_bars'] for t in trades]))

    # Durée totale en mois
    def to_dt(x):
        return x.to_pydatetime() if hasattr(x, 'to_pydatetime') else x

    t0       = to_dt(trades[0]['entry_time'])
    t1       = to_dt(trades[-1]['exit_time'])
    n_months = max(1, (t1 - t0).days / 30)
    trades_pm = len(pnls) / n_months

    # Sharpe annualisé sur les PnL % par trade
    pnl_pcts = [t['pnl_pct'] for t in trades]
    if len(pnl_pcts) > 1 and np.std(pnl_pcts) > 0:
        sharpe = (np.mean(pnl_pcts) / np.std(pnl_pcts)) * np.sqrt(252)
    else:
        sharpe = 0.0

    # Max drawdown
    max_dd = 0.0
    if equity_curve:
        peak = CAPITAL_INITIAL
        for e in equity_curve:
            c = e['capital']
            if c > peak:
                peak = c
            dd = (peak - c) / peak * 100
            if dd > max_dd:
                max_dd = dd

    return {
        'n_trades':         len(trades),
        'win_rate':         win_rate,
        'profit_factor':    profit_factor,
        'sharpe':           sharpe,
        'max_dd':           max_dd,
        'total_pnl':        total_pnl,
        'avg_trade':        avg_trade,
        'avg_bars':         avg_bars,
        'trades_per_month': trades_pm,
        'final_capital':    trades[-1]['capital'],
    }


# ─────────────────────────────────────────────────────────────
# 8. OPTIMISATION
# ─────────────────────────────────────────────────────────────
def optimize(df: pd.DataFrame, param_grid: dict = None) -> dict:
    """
    Grid search sur tous les paramètres.
    Score composite = Sharpe × (1 + WR/100) × max(PF, 0)
    Retourne best_params, best_score, ranking (DataFrame trié)
    """
    if param_grid is None:
        param_grid = PARAM_GRID_DEFAULT

    keys   = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(product(*values))
    total  = len(combos)
    print(f"\n🔍 Optimisation en cours... ({total} combinaisons)")

    results = []
    for idx, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        try:
            res = backtest(df, **params)
            s   = res['stats']
            if s['n_trades'] < 5:
                continue
            score = (s['sharpe']
                     * (1 + s['win_rate'] / 100)
                     * max(s['profit_factor'], 0))
            results.append({**params, **s, 'score': score})
        except Exception:
            continue

        if (idx + 1) % 20 == 0 or idx + 1 == total:
            print(f"  ... {idx+1}/{total} ({(idx+1)/total*100:.0f}%)")

    if not results:
        print("  ⚠️  Aucun résultat valide (trop peu de trades).")
        return {'best_params': {}, 'best_score': 0, 'ranking': pd.DataFrame()}

    ranking = (pd.DataFrame(results)
               .sort_values('score', ascending=False)
               .reset_index(drop=True))
    best = ranking.iloc[0]
    best_params = {k: best[k] for k in keys}

    print(f"\n✅ Meilleurs paramètres :")
    print(f"   body_pct={best_params['body_pct']}, "
          f"rr_ratio={best_params['rr_ratio']}, "
          f"max_wait_bars={int(best_params['max_wait_bars'])}")
    print(f"   Win Rate: {best['win_rate']:.1f}% | "
          f"PF: {best['profit_factor']:.2f} | "
          f"Sharpe: {best['sharpe']:.2f} | "
          f"Max DD: -{best['max_dd']:.1f}%")
    print(f"   Total trades: {best['n_trades']} | "
          f"Avg/mois: {best['trades_per_month']:.2f}")

    return {'best_params': best_params, 'best_score': float(best['score']),
            'ranking': ranking}


# ─────────────────────────────────────────────────────────────
# 9. WALK-FORWARD
# ─────────────────────────────────────────────────────────────
def walk_forward(df: pd.DataFrame, train_days: int = 180,
                 test_days: int = 30, step_days: int = 30) -> dict:
    """
    Walk-forward (fenêtre glissante) :
    - Train sur train_days → optimise les paramètres (mini grille)
    - Test sur les test_days suivants avec ces paramètres
    - Avance de step_days → recommence
    Retourne equity WF, params par période, résumé comparatif
    """
    print(f"\n🧠 Walk-forward (auto-learning)...")
    print(f"   train={train_days}j  test={test_days}j  pas={step_days}j")

    # Grille réduite pour vitesse
    mini_grid = {
        "body_pct":      [0.25, 0.30, 0.35],
        "rr_ratio":      [1.5, 2.0, 3.0],
        "max_wait_bars": [3, 5],
    }

    dates = df.index.normalize().unique()

    periods    = []
    wf_trades  = []
    wf_equity  = [{'time': df.index[0], 'capital': CAPITAL_INITIAL}]
    wf_capital = CAPITAL_INITIAL
    period_num = 0
    start_idx  = 0

    while True:
        if start_idx >= len(dates):
            break

        train_start    = dates[start_idx]
        train_end_date = train_start + timedelta(days=train_days)
        test_end_date  = train_end_date + timedelta(days=test_days)

        df_train = df[(df.index.normalize() >= train_start) &
                      (df.index.normalize() <  train_end_date)]
        df_test  = df[(df.index.normalize() >= train_end_date) &
                      (df.index.normalize() <  test_end_date)]

        if len(df_train) < 80 or len(df_test) < 10:
            break

        # ── Optimisation sur la fenêtre d'entraînement ──
        opt = optimize(df_train, param_grid=mini_grid)
        if not opt['best_params']:
            break
        best_params  = opt['best_params']

        train_res    = backtest(df_train, **best_params)
        train_stats  = train_res['stats']
        test_res     = backtest(df_test,  **best_params)
        test_stats   = test_res['stats']

        # Rebase le PnL de la phase test sur le capital WF courant
        if test_res['trades']:
            scale = wf_capital / CAPITAL_INITIAL
            for trade in test_res['trades']:
                t = trade.copy()
                t['pnl_usdc'] = trade['pnl_usdc'] * scale
                wf_capital   += t['pnl_usdc']
                t['capital']  = wf_capital
                wf_trades.append(t)
            wf_equity.append({'time': df_test.index[-1], 'capital': wf_capital})

        period_num += 1
        print(f"  Période {period_num} "
              f"({train_start.strftime('%Y-%m')} → "
              f"{train_end_date.strftime('%Y-%m')}) : "
              f"Train WR={train_stats['win_rate']:.0f}% "
              f"({train_stats['n_trades']} trades) | "
              f"Test WR={test_stats['win_rate']:.0f}% "
              f"({test_stats['n_trades']} trades)")

        periods.append({
            'period':         period_num,
            'train_start':    train_start,
            'train_end':      train_end_date,
            'test_start':     train_end_date,
            'test_end':       test_end_date,
            'params':         best_params,
            'train_wr':       train_stats['win_rate'],
            'test_wr':        test_stats['win_rate'],
            'train_pf':       train_stats['profit_factor'],
            'test_pf':        test_stats['profit_factor'],
            'train_sharpe':   train_stats['sharpe'],
            'test_sharpe':    test_stats['sharpe'],
            'train_n_trades': train_stats['n_trades'],
            'test_n_trades':  test_stats['n_trades'],
        })

        # Avancer la fenêtre de step_days
        next_start = train_start + timedelta(days=step_days)
        try:
            ns64      = np.datetime64(next_start.date(), 'D')
            start_idx = int(np.searchsorted(dates.values, ns64))
        except Exception:
            start_idx += max(1, step_days // (
                (dates[-1] - dates[0]).days // len(dates) or 1))

    if not periods:
        print("  ⚠️  Pas assez de données pour le walk-forward.")
        return {'periods': [], 'wf_equity': pd.DataFrame(),
                'wf_trades': [], 'wf_stats': {}}

    wf_stats = compute_stats(wf_trades, wf_equity)
    wf_eq_df = (pd.DataFrame(wf_equity).set_index('time')
                if wf_equity else pd.DataFrame())

    print(f"\n  📊 Walk-forward global : "
          f"WR={wf_stats['win_rate']:.1f}% | "
          f"Sharpe={wf_stats['sharpe']:.2f} | "
          f"Max DD=-{wf_stats['max_dd']:.1f}% | "
          f"Capital final: ${wf_capital:,.0f}")

    return {
        'periods':   periods,
        'wf_equity': wf_eq_df,
        'wf_trades': wf_trades,
        'wf_stats':  wf_stats,
    }


# ─────────────────────────────────────────────────────────────
# 10. RAPPORT VISUEL
# ─────────────────────────────────────────────────────────────
def generate_report(backtest_result: dict, wf_result: dict = None,
                    output_path: str = "trading/nasdaq_report.png"):
    """
    Rapport graphique 4 panneaux (thème sombre) :
    1. Equity curve (backtest + walk-forward overlay)
    2. Distribution PnL (histogramme gains/pertes)
    3. Win Rate train vs test par période WF
    4. Tableau des statistiques complètes
    """
    fig = plt.figure(figsize=(18, 14))
    fig.patch.set_facecolor('#0d1117')
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.4, wspace=0.35)

    C_BG    = '#0d1117'
    C_PANEL = '#161b22'
    C_TEXT  = '#e6edf3'
    C_GREEN = '#3fb950'
    C_RED   = '#f85149'
    C_BLUE  = '#58a6ff'
    C_GOLD  = '#d29922'
    C_GREY  = '#8b949e'

    def style_ax(ax, title):
        ax.set_facecolor(C_PANEL)
        ax.tick_params(colors=C_TEXT, labelsize=9)
        ax.xaxis.label.set_color(C_TEXT)
        ax.yaxis.label.set_color(C_TEXT)
        for spine in ax.spines.values():
            spine.set_color('#30363d')
        ax.set_title(title, fontsize=11, fontweight='bold',
                     pad=10, color=C_TEXT)

    # ── Panneau 1 : Equity Curve ──────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    style_ax(ax1, '📈 Equity Curve')

    eq = backtest_result.get('equity_curve', pd.DataFrame())
    if not eq.empty:
        ax1.plot(eq.index, eq['capital'], color=C_BLUE,
                 linewidth=1.5, label='Backtest complet', alpha=0.9)

    if wf_result and not wf_result.get('wf_equity', pd.DataFrame()).empty:
        wf_eq = wf_result['wf_equity']
        ax1.plot(wf_eq.index, wf_eq['capital'], color=C_GOLD,
                 linewidth=2, linestyle='--', label='Walk-forward', alpha=0.85)

    ax1.axhline(y=CAPITAL_INITIAL, color=C_GREY, linestyle=':', linewidth=1)
    ax1.set_ylabel('Capital (USDC)', color=C_TEXT)
    ax1.legend(facecolor=C_PANEL, labelcolor=C_TEXT, fontsize=8)
    ax1.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # ── Panneau 2 : Distribution PnL ─────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    style_ax(ax2, '💰 Distribution des PnL')

    trades = backtest_result.get('trades', [])
    if trades:
        pnls   = [t['pnl_usdc'] for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        bins   = min(40, max(10, len(pnls) // 4))

        if losses:
            ax2.hist(losses, bins=bins // 2, color=C_RED,
                     alpha=0.75, label=f'Pertes ({len(losses)})')
        if wins:
            ax2.hist(wins, bins=bins // 2, color=C_GREEN,
                     alpha=0.75, label=f'Gains ({len(wins)})')
        ax2.axvline(x=0, color='white', linestyle='--', linewidth=1)
        moy = float(np.mean(pnls))
        ax2.axvline(x=moy, color=C_GOLD, linewidth=1.5,
                    label=f'Moy: ${moy:.1f}')
        ax2.set_xlabel('PnL (USDC)', color=C_TEXT)
        ax2.set_ylabel('Fréquence', color=C_TEXT)
        ax2.legend(facecolor=C_PANEL, labelcolor=C_TEXT, fontsize=8)

    # ── Panneau 3 : Walk-forward WR par période ───────────
    ax3 = fig.add_subplot(gs[1, 0])
    style_ax(ax3, '🧠 Walk-Forward : WR Train vs Test')

    if wf_result and wf_result.get('periods'):
        periods  = wf_result['periods']
        x_arr    = np.arange(len(periods))
        train_wr = [p['train_wr'] for p in periods]
        test_wr  = [p['test_wr']  for p in periods]
        labels   = [f"P{p['period']}" for p in periods]
        w = 0.35

        ax3.bar(x_arr - w/2, train_wr, w, color=C_BLUE,  alpha=0.8, label='Train WR')
        ax3.bar(x_arr + w/2, test_wr,  w, color=C_GOLD,  alpha=0.8, label='Test WR')
        ax3.axhline(y=50, color=C_GREY, linestyle=':', linewidth=1)
        ax3.set_xticks(x_arr)
        ax3.set_xticklabels(labels, rotation=45, fontsize=8)
        ax3.set_ylabel('Win Rate (%)', color=C_TEXT)
        ax3.set_ylim(0, 105)
        ax3.legend(facecolor=C_PANEL, labelcolor=C_TEXT, fontsize=8)

        # Annoter les paramètres par période (compact)
        for i, p in enumerate(periods):
            bp = p['params']
            lbl = (f"bp={bp['body_pct']}\n"
                   f"rr={bp['rr_ratio']}\n"
                   f"w={int(bp['max_wait_bars'])}")
            ax3.annotate(lbl, xy=(i, 3), ha='center', fontsize=5.5,
                         color=C_TEXT, alpha=0.65)
    else:
        ax3.text(0.5, 0.5, 'Walk-forward non exécuté',
                 ha='center', va='center', color=C_TEXT,
                 transform=ax3.transAxes)

    # ── Panneau 4 : Tableau des statistiques ─────────────
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.set_facecolor(C_PANEL)
    ax4.axis('off')
    ax4.set_title('📊 Statistiques Complètes', fontsize=11,
                  fontweight='bold', pad=10, color=C_TEXT)

    stats  = backtest_result.get('stats', {})
    params = stats.get('params', {})
    final  = stats.get('final_capital', CAPITAL_INITIAL)
    retour = (final / CAPITAL_INITIAL - 1) * 100

    rows = [
        ('Capital initial',    f"${CAPITAL_INITIAL:,.0f}"),
        ('Capital final',      f"${final:,.0f}"),
        ('Retour total',       f"{retour:+.1f}%"),
        ('Total PnL',          f"${stats.get('total_pnl', 0):+,.2f}"),
        ('SEP', None),
        ('Total trades',       f"{stats.get('n_trades', 0)}"),
        ('Win Rate',           f"{stats.get('win_rate', 0):.1f}%"),
        ('Profit Factor',      f"{stats.get('profit_factor', 0):.2f}"),
        ('Sharpe (annualisé)', f"{stats.get('sharpe', 0):.2f}"),
        ('Max Drawdown',       f"-{stats.get('max_dd', 0):.1f}%"),
        ('SEP', None),
        ('Trades / mois',      f"{stats.get('trades_per_month', 0):.1f}"),
        ('Durée moy. (bars)',  f"{stats.get('avg_bars', 0):.1f}"),
        ('PnL moyen / trade',  f"${stats.get('avg_trade', 0):+.2f}"),
        ('SEP', None),
        ('body_pct',           str(params.get('body_pct', '-'))),
        ('rr_ratio',           str(params.get('rr_ratio', '-'))),
        ('max_wait_bars',      str(params.get('max_wait_bars', '-'))),
        ('SL min/max (QQQ)',   f"${MIN_SL_QQQ:.2f} / ${MAX_SL_QQQ:.2f}"),
    ]

    y = 0.97
    for label, value in rows:
        if label == 'SEP':
            ax4.plot([0.02, 0.98], [y + 0.01, y + 0.01],
                     color='#30363d', linewidth=0.5,
                     transform=ax4.transAxes)
            y -= 0.03
            continue

        vc = C_TEXT
        if isinstance(value, str):
            if value.startswith('+'):
                vc = C_GREEN
            elif value.startswith('-') and value not in ('-', '-0.0%'):
                vc = C_RED

        ax4.text(0.05, y, label,  transform=ax4.transAxes,
                 color=C_GREY, fontsize=9, va='top')
        ax4.text(0.65, y, value,  transform=ax4.transAxes,
                 color=vc, fontsize=9, va='top', fontweight='bold')
        y -= 0.048

    # Titre global
    fig.suptitle(
        f'NAS100 Reversal — Exhaustion + Indecision Breakout  |  '
        f'QQQ 1H  |  {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=12, fontweight='bold', color=C_TEXT, y=0.985,
    )

    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=C_BG, edgecolor='none')
    plt.close()
    print(f"\n✅ Rapport sauvegardé : {output_path}")


# ─────────────────────────────────────────────────────────────
# 11. POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='NAS100 Reversal — Exhaustion + Indecision Breakout')
    parser.add_argument('--mode',
        choices=['backtest', 'optimize', 'walkforward', 'report'],
        default='report')
    parser.add_argument('--ticker',   default=TICKER)
    parser.add_argument('--interval', default='1h')
    args = parser.parse_args()

    print("=" * 65)
    print("  NAS100 REVERSAL — Exhaustion + Indecision Breakout Strategy")
    print("=" * 65)

    print("\n📦 Chargement des données...")
    df_1h = download_data(args.ticker, interval='1h', period='2y')

    df_10m = None
    try:
        df_5m  = download_data(args.ticker, interval='5m', period='60d')
        df_10m = resample_5m_to_10m(df_5m)
    except Exception as e:
        print(f"  ⚠️  Données 5m indisponibles ({e}). Validation court terme ignorée.")

    # ── MODE BACKTEST ──────────────────────────────────────
    if args.mode == 'backtest':
        print("\n🔄 Backtest avec paramètres par défaut...")
        res = backtest(df_1h, rr_ratio=2.0, body_pct=0.30, max_wait_bars=3)
        s   = res['stats']
        print(f"\n  Résultats (1H, 2 ans) :")
        print(f"  Win Rate   : {s['win_rate']:.1f}%")
        print(f"  PF         : {s['profit_factor']:.2f}")
        print(f"  Sharpe     : {s['sharpe']:.2f}")
        print(f"  Max DD     : -{s['max_dd']:.1f}%")
        print(f"  Trades     : {s['n_trades']} | {s['trades_per_month']:.1f}/mois")
        print(f"  Capital    : ${CAPITAL_INITIAL:,.0f} → ${s['final_capital']:,.0f} "
              f"({(s['final_capital']/CAPITAL_INITIAL-1)*100:+.1f}%)")

        # Ventilation des raisons de sortie
        if res['trades']:
            raisons = {}
            for t in res['trades']:
                raisons[t['raison']] = raisons.get(t['raison'], 0) + 1
            print(f"\n  Sorties : {raisons}")

    # ── MODE OPTIMIZE ──────────────────────────────────────
    elif args.mode == 'optimize':
        opt = optimize(df_1h)

        if df_10m is not None and len(df_10m) > 50 and opt['best_params']:
            print("\n🔄 Validation sur données 5m→10m (60 jours) :")
            val = backtest(df_10m, **opt['best_params'])
            sv  = val['stats']
            print(f"  Win Rate : {sv['win_rate']:.1f}% | "
                  f"PF : {sv['profit_factor']:.2f} | "
                  f"Sharpe : {sv['sharpe']:.2f} | "
                  f"Trades : {sv['n_trades']}")

    # ── MODE WALKFORWARD ───────────────────────────────────
    elif args.mode == 'walkforward':
        walk_forward(df_1h, train_days=180, test_days=30, step_days=30)

    # ── MODE REPORT (complet) ──────────────────────────────
    elif args.mode == 'report':
        print("\n🔍 Optimisation pour le rapport...")
        opt = optimize(df_1h, param_grid={
            "body_pct":      [0.25, 0.30, 0.35],
            "rr_ratio":      [1.5, 2.0, 3.0],
            "max_wait_bars": [3, 5],
        })

        best_params = opt.get('best_params') or {
            'rr_ratio': 2.0, 'body_pct': 0.30, 'max_wait_bars': 3}

        print("\n🔄 Backtest complet avec meilleurs paramètres...")
        bt_result = backtest(df_1h, **best_params)

        print("\n🧠 Walk-forward (auto-learning)...")
        wf_result = walk_forward(df_1h, train_days=180,
                                 test_days=30, step_days=30)

        generate_report(bt_result, wf_result,
                        output_path="trading/nasdaq_report.png")

        s = bt_result['stats']
        print("\n" + "=" * 65)
        print("  RÉSUMÉ FINAL")
        print("=" * 65)
        print(f"  Best params: body_pct={best_params['body_pct']}, "
              f"rr_ratio={best_params['rr_ratio']}, "
              f"max_wait_bars={int(best_params['max_wait_bars'])}")
        print(f"  Win Rate: {s['win_rate']:.1f}% | "
              f"PF: {s['profit_factor']:.2f} | "
              f"Sharpe: {s['sharpe']:.2f} | "
              f"Max DD: -{s['max_dd']:.1f}%")
        print(f"  Total trades: {s['n_trades']} | "
              f"Avg/mois: {s['trades_per_month']:.2f}")
        print(f"  Capital: ${CAPITAL_INITIAL:,.0f} → ${s['final_capital']:,.0f} "
              f"({(s['final_capital']/CAPITAL_INITIAL-1)*100:+.1f}%)")

        # Ventilation des sorties
        if bt_result['trades']:
            raisons = {}
            for t in bt_result['trades']:
                raisons[t['raison']] = raisons.get(t['raison'], 0) + 1
            print(f"  Sorties : {raisons}")


if __name__ == "__main__":
    main()
