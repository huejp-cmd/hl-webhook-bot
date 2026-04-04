#!/usr/bin/env python3
"""
NAS100 Reversal Strategy — Candle Exhaustion + Indecision Breakout
Instrument : NQ E-mini Futures (1 point = 20$) — simulé via NQ=F / QQQ proxy
Compte    : Apex Funding 100 000$ — règles intégrées
Timeframe : 10M (backtest sur 1H proxy RTH, validation 5M→10M)

Flow en 6 étapes :
  1. Pattern d'épuisement (2 bougies alternées avec pentes)
  2. Attendre candle d'indécision (corps < body_pct × range, mèches des 2 côtés)
  3. Entrée sur breakout du candle d'indécision
  4. SL = corps du candle signal en pts NQ (filtre strict : 10–30 pts)
  5. TP = SL × rr_ratio
  6. Sortie : signal opposé OU fin de session RTH OU règles Apex

Règles Apex intégrées :
  - Daily loss limit   : -2 000$/jour (= -2% × 100k)
  - Trailing max DD    : -8 000$ depuis le pic (= -8%)
  - Filtre sessions    : RTH uniquement 9h30–16h00 ET

Usage :
  python trading/nasdaq_strategy.py --mode backtest
  python trading/nasdaq_strategy.py --mode optimize
  python trading/nasdaq_strategy.py --mode walkforward
  python trading/nasdaq_strategy.py --mode report
  python trading/nasdaq_strategy.py --mode compare
"""

import argparse
import warnings
from collections import defaultdict
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
# CONSTANTES APEX / NQ
# ─────────────────────────────────────────────────────────────
ACCOUNT_SIZE      = 100_000.0  # $ — compte Apex 100k
POINT_VALUE       = 20.0       # $/point pour NQ E-mini
N_CONTRACTS       = 1          # 1 contrat NQ par défaut
FRAIS_RT          = 5.0        # $ aller-retour (commissions + frais CME estimés)

# Règles Apex
DAILY_LOSS_LIMIT  = -2_000.0   # $ — arrêt journalier si atteint (-2% × 100k)
MAX_DD_LIMIT      = -8_000.0   # $ — arrêt définitif trailing (-8%)
PROFIT_TARGET     = 10_000.0   # $ — objectif challenge (+10%)
TARGET_DAILY_PNL  =  1_000.0   # $ — objectif quotidien JP

# Filtre SL : corps du candle signal en points NQ
SL_MIN_PTS = 10   # Corps minimum valide
SL_MAX_PTS = 30   # Corps maximum valide (relevé 20→30 pour ~5 signaux/jour)

# Sessions RTH : 9h30–16h00 ET
RTH_START_H, RTH_START_M = 9, 30
RTH_END_H,   RTH_END_M   = 16,  0

# Grille d'optimisation par défaut
PARAM_GRID_DEFAULT = {
    "body_pct":      [0.20, 0.25, 0.30, 0.35],  # % range → indécision
    "rr_ratio":      [1.5, 2.0, 3.0],            # Risk/Reward TP
    "max_wait_bars": [2, 3, 5],                   # Barres max d'attente
}

# États machine à états
IDLE               = 0
WAIT_INDECISION    = 1
WAIT_BREAKOUT      = 2
IN_POSITION        = 3


# ─────────────────────────────────────────────────────────────
# 1. TÉLÉCHARGEMENT — NQ=F en priorité, QQQ en fallback
# ─────────────────────────────────────────────────────────────
def download_data(interval: str = "1h", period: str = "2y") -> tuple[pd.DataFrame, float]:
    """
    Télécharge les données OHLCV.
    Retourne (df, conversion_factor) où :
      - conversion_factor = 1.0   si données en points NQ directs (NQ=F)
      - conversion_factor = 40.0  si données en $ QQQ (1 pt QQQ ≈ 40 pts NQ)

    Ordre d'essai : NQ=F → ^NDX → QQQ
    """
    candidates = [
        ("NQ=F",  1.0),    # Futures NQ directs (corps en pts NQ)
        ("^NDX",  1.0),    # Index NDX (même échelle que NQ)
        ("QQQ",  40.0),    # ETF proxy : 1$ QQQ ≈ 40 pts NQ
    ]

    for ticker, conv in candidates:
        try:
            print(f"  📥 Téléchargement {ticker} [{interval}, {period}]...")
            df = yf.download(ticker, interval=interval, period=period,
                             progress=False, auto_adjust=True)
            if df.empty:
                print(f"  ⚠️  Vide pour {ticker}, essai suivant...")
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]
            df = df[['open', 'high', 'low', 'close', 'volume']].copy()
            df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
            df = df[df['close'] > 0].copy()

            print(f"  ✅ {ticker} : {len(df)} bougies "
                  f"({df.index[0].date()} → {df.index[-1].date()})  "
                  f"[conv={conv}×]")
            return df, conv

        except Exception as e:
            print(f"  ❌ {ticker} : {e}")

    raise RuntimeError("Impossible de télécharger les données.")


# ─────────────────────────────────────────────────────────────
# 2. FILTRE SESSIONS RTH (9h30–16h00 ET)
# ─────────────────────────────────────────────────────────────
def filter_rth(df: pd.DataFrame) -> pd.DataFrame:
    """
    Conserve uniquement les barres des heures de marché régulières
    (Regular Trading Hours) : 9h30–16h00 heure de New York.
    Élimine le pré-market, l'after-hours et les week-ends.
    """
    try:
        idx = df.index
        # Ajouter timezone UTC si absente
        if idx.tz is None:
            idx = idx.tz_localize('UTC')

        # Convertir en heure de New York (gère EDT/EST automatiquement)
        idx_et = idx.tz_convert('America/New_York')

        # Filtre : lundi-vendredi, 9h30 ≤ heure < 16h00
        in_rth = (
            (idx_et.dayofweek < 5) &
            (
                (idx_et.hour > RTH_START_H) |
                ((idx_et.hour == RTH_START_H) & (idx_et.minute >= RTH_START_M))
            ) &
            (idx_et.hour < RTH_END_H)
        )

        # in_rth peut être un np.ndarray ou un pandas array selon la version
        mask   = np.asarray(in_rth, dtype=bool)
        df_rth = df.iloc[mask].copy()
        removed = len(df) - len(df_rth)
        print(f"  🕐 Filtre RTH : {len(df_rth)} barres conservées "
              f"({removed} hors-session retirées)")
        return df_rth

    except Exception as e:
        print(f"  ⚠️  Filtre RTH impossible ({e}) — toutes les barres conservées")
        return df


# ─────────────────────────────────────────────────────────────
# 3. RESAMPLE 5M → 10M
# ─────────────────────────────────────────────────────────────
def resample_5m_to_10m(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 5M → 10M (OHLCV correct)."""
    df_r = df.resample('10min').agg({
        'open': 'first', 'high': 'max',
        'low':  'min',   'close': 'last', 'volume': 'sum',
    })
    df_r.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
    df_r = df_r[df_r['close'] > 0].copy()
    print(f"  🔄 Resample 5m→10m : {len(df_r)} bougies")
    return df_r


# ─────────────────────────────────────────────────────────────
# 4. ATR (référence)
# ─────────────────────────────────────────────────────────────
def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR Wilder (EWM). Résultat en unités natives du ticker."""
    h, l, c = df['high'], df['low'], df['close']
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


# ─────────────────────────────────────────────────────────────
# 4b. ADX — Average Directional Index (régime de marché)
# ─────────────────────────────────────────────────────────────
def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calcule l'ADX (Average Directional Index) sur les données OHLCV.
    Retourne une Series avec l'ADX pour chaque barre.

    ADX < 25  → marché en range (retournement probable)
    ADX >= 25 → tendance forte (éviter les retournements)
    """
    high  = df['high']
    low   = df['low']
    close = df['close']

    # True Range
    tr = pd.DataFrame({
        'hl': high - low,
        'hc': (high - close.shift(1)).abs(),
        'lc': (low  - close.shift(1)).abs(),
    }).max(axis=1)

    # Directional Movement
    dm_plus  = high.diff()
    dm_minus = -low.diff()
    dm_plus  = dm_plus.where((dm_plus > dm_minus)  & (dm_plus  > 0), 0.0)
    dm_minus = dm_minus.where((dm_minus > dm_plus) & (dm_minus > 0), 0.0)

    # Smoothed (EWM)
    atr      = tr.ewm(span=period, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm(span=period,  adjust=False).mean() / atr
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / atr

    denom = (di_plus + di_minus).replace(0, np.nan)
    dx    = (100 * (di_plus - di_minus).abs() / denom).fillna(0)
    adx   = dx.ewm(span=period, adjust=False).mean()
    return adx


# ─────────────────────────────────────────────────────────────
# 5. DÉTECTION DU PATTERN D'ÉPUISEMENT
# ─────────────────────────────────────────────────────────────
def detect_exhaustion(df: pd.DataFrame, conv: float = 1.0) -> pd.DataFrame:
    """
    Détecte les patterns d'épuisement sur 2 bougies consécutives.

    SELL bias (SHORT) :
      Candle[i] = VERT, Candle[i-1] = ROUGE
      HIGH[i] > HIGH[i-1]  et  LOW[i] > LOW[i-1]

    BUY bias (LONG) :
      Candle[i] = ROUGE, Candle[i-1] = VERT
      LOW[i]  < LOW[i-1]  et  HIGH[i] < HIGH[i-1]

    Ajoute :
      'bias'         : +1 LONG | -1 SHORT | 0 neutre
      'signal_pts'   : corps du candle signal en POINTS NQ
                       (abs(close-open) × conv)
      'signal_valid' : True si SL_MIN_PTS ≤ corps ≤ SL_MAX_PTS pts NQ
    """
    df = df.copy()

    df['body']      = (df['close'] - df['open']).abs()
    df['is_green']  = df['close'] > df['open']
    df['is_red']    = df['close'] < df['open']

    ph  = df['high'].shift(1)
    pl  = df['low'].shift(1)
    pig = df['is_green'].shift(1)
    pir = df['is_red'].shift(1)

    cond_short = (
        df['is_green'] & pir &
        (df['high'] > ph) & (df['low'] > pl)
    )
    cond_long = (
        df['is_red'] & pig &
        (df['low'] < pl) & (df['high'] < ph)
    )

    df['bias'] = 0
    df.loc[cond_long,  'bias'] = 1
    df.loc[cond_short, 'bias'] = -1

    # Corps en points NQ (× facteur de conversion si QQQ)
    df['signal_pts']   = df['body'] * conv
    df['signal_valid'] = (
        (df['bias'] != 0) &
        (df['signal_pts'] >= SL_MIN_PTS) &
        (df['signal_pts'] <= SL_MAX_PTS)
    )

    return df


# ─────────────────────────────────────────────────────────────
# 6. TEST D'INDÉCISION
# ─────────────────────────────────────────────────────────────
def is_indecision(row: pd.Series, body_pct: float = 0.30) -> bool:
    """
    Doji / Spinning Top :
      - Corps < body_pct × range totale (high-low)
      - Mèche haute > 0  ET  Mèche basse > 0
    """
    total_range = row['high'] - row['low']
    if total_range <= 0:
        return False
    body        = abs(row['close'] - row['open'])
    upper_wick  = row['high'] - max(row['open'], row['close'])
    lower_wick  = min(row['open'], row['close']) - row['low']
    return (body < body_pct * total_range) and (upper_wick > 0) and (lower_wick > 0)


# ─────────────────────────────────────────────────────────────
# 7. BACKTEST — machine à états + règles Apex
# ─────────────────────────────────────────────────────────────
def backtest(df: pd.DataFrame, conv: float = 1.0,
             rr_ratio: float = 2.0, body_pct: float = 0.30,
             max_wait_bars: int = 3,
             contracts: int = 1) -> dict:
    """
    Simule les trades avec règles Apex intégrées.

    Compte : 100 000$ / N contrats NQ / 1 pt = 20$
    Frais  : 5$ aller-retour par contrat
    Apex   : daily loss limit -2 000$/jour, trailing DD -8 000$
    Filtre : SL 10–30 pts NQ, sessions RTH uniquement
    """
    df = detect_exhaustion(df, conv=conv)
    df = df.dropna(subset=['bias']).copy()
    n  = len(df)

    frais_rt = FRAIS_RT * contracts  # Frais proportionnels au nb de contrats

    # ── Compte ──
    account   = ACCOUNT_SIZE
    peak_acct = ACCOUNT_SIZE   # Pour trailing drawdown
    halted    = False          # True si trailing DD atteint

    # ── Suivi journalier ──
    daily_pnl      = 0.0       # PnL du jour en cours (en $)
    daily_stopped  = False     # True si daily limit atteint ce jour
    current_date   = None      # Date ET de la session en cours
    n_daily_limit  = 0         # Nombre de jours où daily limit touché

    # ── Machine à états ──
    state       = IDLE
    bias        = 0
    sl_pts      = 0.0    # Taille du SL en points NQ
    wait_count  = 0
    indc_high   = 0.0
    indc_low    = 0.0
    entry_price = 0.0
    sl_price    = 0.0
    tp_price    = 0.0
    entry_time  = None
    entry_bar   = 0

    trades       = []
    equity_curve = []
    daily_stats  = []   # Une entrée par jour de trading

    # Convertir l'index en ET pour la gestion des sessions
    try:
        idx = df.index
        if idx.tz is None:
            idx_et = idx.tz_localize('UTC').tz_convert('America/New_York')
        else:
            idx_et = idx.tz_convert('America/New_York')
    except Exception:
        idx_et = df.index  # Fallback sans timezone

    for i in range(n):
        row       = df.iloc[i]
        timestamp = df.index[i]

        # Récupérer la date ET de cette barre
        try:
            bar_date = idx_et[i].date()
        except Exception:
            bar_date = timestamp.date()

        # ── Détection changement de journée ──
        if bar_date != current_date:
            # Sauvegarder les stats du jour précédent
            if current_date is not None:
                if daily_stopped:
                    n_daily_limit += 1
                daily_stats.append({
                    'date':     current_date,
                    'pnl':      daily_pnl,
                    'stopped':  daily_stopped,
                })
            # Nouveau jour : reset compteurs journaliers
            current_date  = bar_date
            daily_pnl     = 0.0
            daily_stopped = False

        # ── Vérifier si trading autorisé ──
        trading_allowed = (not halted) and (not daily_stopped)

        # ── Dernière barre de la session RTH ──
        next_is_new_day = False
        if i + 1 < n:
            try:
                next_is_new_day = (idx_et[i + 1].date() != bar_date)
            except Exception:
                next_is_new_day = (df.index[i + 1].date() != timestamp.date())
        else:
            next_is_new_day = True

        # ────────────────────────────────────────────
        # ÉTAT : IN_POSITION
        # ────────────────────────────────────────────
        if state == IN_POSITION:
            hit_sl = False
            hit_tp = False
            exit_price = row['close']
            raison = 'En cours'

            if bias == 1:   # LONG
                if row['low'] <= sl_price:
                    hit_sl = True;  exit_price = sl_price
                elif row['high'] >= tp_price:
                    hit_tp = True;  exit_price = tp_price
            else:            # SHORT
                if row['high'] >= sl_price:
                    hit_sl = True;  exit_price = sl_price
                elif row['low'] <= tp_price:
                    hit_tp = True;  exit_price = tp_price

            # Signal opposé → sortie
            opp = (bias == 1 and row['signal_valid'] and row['bias'] == -1) or \
                  (bias == -1 and row['signal_valid'] and row['bias'] == 1)
            if not hit_sl and not hit_tp and opp:
                exit_price = row['close']
                raison = 'Signal opposé'

            # Fin de session RTH → sortie forcée
            force_close = (not hit_sl and not hit_tp and
                           raison != 'Signal opposé' and next_is_new_day)
            if force_close:
                exit_price = row['close']
                raison = 'Fin session'

            if hit_sl or hit_tp or opp or force_close:
                if hit_sl:  raison = 'SL'
                elif hit_tp: raison = 'TP'

                # PnL en points puis en $ (× contrats)
                if bias == 1:
                    pnl_pts = (exit_price - entry_price) * conv
                else:
                    pnl_pts = (entry_price - exit_price) * conv
                pnl_usd = pnl_pts * POINT_VALUE * contracts - frais_rt
                account    += pnl_usd
                daily_pnl  += pnl_usd

                # Mettre à jour le pic pour le trailing DD
                if account > peak_acct:
                    peak_acct = account

                # Vérifier trailing drawdown (règle Apex)
                dd_from_peak = account - peak_acct
                if dd_from_peak <= MAX_DD_LIMIT:
                    halted = True

                # Vérifier daily loss limit (règle Apex)
                if daily_pnl <= DAILY_LOSS_LIMIT:
                    daily_stopped = True

                trades.append({
                    'entry_time':   entry_time,
                    'exit_time':    timestamp,
                    'direction':    'LONG' if bias == 1 else 'SHORT',
                    'entry_price':  entry_price,
                    'exit_price':   exit_price,
                    'sl_price':     sl_price,
                    'tp_price':     tp_price,
                    'sl_pts':       sl_pts,
                    'sl_usd':       sl_pts * POINT_VALUE * contracts,
                    'pnl_pts':      pnl_pts,
                    'pnl_usd':      pnl_usd,
                    'account':      account,
                    'raison':       raison,
                    'duree_bars':   i - entry_bar,
                    'daily_pnl':    daily_pnl,
                    'contracts':    contracts,
                })

                state      = IDLE
                bias       = 0
                wait_count = 0

        # ────────────────────────────────────────────
        # ÉTAT : WAIT_BREAKOUT
        # ────────────────────────────────────────────
        elif state == WAIT_BREAKOUT and trading_allowed:
            entered = False

            if bias == -1 and row['close'] < indc_low:
                entry_price = row['close']
                sl_price    = entry_price + (sl_pts / conv)   # SL en prix natif
                tp_price    = entry_price - (sl_pts * rr_ratio / conv)
                entered     = True

            elif bias == 1 and row['close'] > indc_high:
                entry_price = row['close']
                sl_price    = entry_price - (sl_pts / conv)
                tp_price    = entry_price + (sl_pts * rr_ratio / conv)
                entered     = True

            if entered:
                entry_time = timestamp
                entry_bar  = i
                state      = IN_POSITION
                wait_count = 0
                daily_pnl -= frais_rt / 2  # Frais d'entrée (moitié A/R)
            else:
                wait_count += 1
                if wait_count > max_wait_bars or next_is_new_day:
                    state = IDLE; bias = 0; wait_count = 0

        # ────────────────────────────────────────────
        # ÉTAT : WAIT_INDECISION
        # ────────────────────────────────────────────
        elif state == WAIT_INDECISION and trading_allowed:
            if is_indecision(row, body_pct):
                indc_high  = row['high']
                indc_low   = row['low']
                state      = WAIT_BREAKOUT
                wait_count = 0
            else:
                wait_count += 1
                if wait_count > max_wait_bars or next_is_new_day:
                    state = IDLE; bias = 0; wait_count = 0

        # ────────────────────────────────────────────
        # ÉTAT : IDLE — chercher épuisement
        # ────────────────────────────────────────────
        if state == IDLE and trading_allowed:
            if row['signal_valid']:
                bias       = int(row['bias'])
                sl_pts     = float(row['signal_pts'])
                state      = WAIT_INDECISION
                wait_count = 0

        # Equity à chaque barre
        equity_curve.append({'time': timestamp, 'account': account})

    # Fermer toute position ouverte en fin de données
    if state == IN_POSITION:
        exit_price = df.iloc[-1]['close']
        if bias == 1:
            pnl_pts = (exit_price - entry_price) * conv
        else:
            pnl_pts = (entry_price - exit_price) * conv
        pnl_usd = pnl_pts * POINT_VALUE * contracts - frais_rt / 2
        account += pnl_usd
        trades.append({
            'entry_time': entry_time, 'exit_time': df.index[-1],
            'direction': 'LONG' if bias == 1 else 'SHORT',
            'entry_price': entry_price, 'exit_price': exit_price,
            'sl_price': sl_price, 'tp_price': tp_price,
            'sl_pts': sl_pts, 'sl_usd': sl_pts * POINT_VALUE * contracts,
            'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd,
            'account': account, 'raison': 'Fin données',
            'duree_bars': n - entry_bar, 'daily_pnl': daily_pnl,
            'contracts': contracts,
        })

    # Dernier jour
    if current_date is not None:
        if daily_stopped:
            n_daily_limit += 1
        daily_stats.append({'date': current_date, 'pnl': daily_pnl,
                            'stopped': daily_stopped})

    eq_df  = (pd.DataFrame(equity_curve).set_index('time')
              if equity_curve else pd.DataFrame())
    stats  = compute_stats(trades, equity_curve, daily_stats, halted, account,
                           contracts=contracts)
    stats['params'] = {
        'rr_ratio': rr_ratio, 'body_pct': body_pct,
        'max_wait_bars': max_wait_bars, 'contracts': contracts,
    }
    stats['n_daily_limit'] = n_daily_limit

    return {
        'trades':       trades,
        'equity_curve': eq_df,
        'daily_stats':  daily_stats,
        'stats':        stats,
        'halted':       halted,
    }


# ─────────────────────────────────────────────────────────────
# 8. STATISTIQUES
# ─────────────────────────────────────────────────────────────
def compute_stats(trades: list, equity_curve: list,
                  daily_stats: list = None, halted: bool = False,
                  final_account: float = None,
                  contracts: int = 1) -> dict:
    """
    Calcule toutes les métriques : trading classiques + métriques Apex.
    """
    empty = {k: 0 for k in [
        'n_trades', 'win_rate', 'profit_factor', 'sharpe',
        'max_dd_usd', 'max_dd_pct', 'total_pnl_usd', 'avg_trade_usd',
        'avg_bars', 'trades_per_day', 'avg_sl_pts', 'avg_sl_usd',
        'max_sl_usd', 'avg_risk_usd', 'trades_before_daily_limit',
        'pct_days_stopped', 'apex_halted', 'final_account',
        'profit_toward_target_pct', 'avg_pnl_per_day', 'n_daily_limit',
    ]}
    if not trades:
        return empty

    pnls  = [t['pnl_usd'] for t in trades]
    wins  = [p for p in pnls if p > 0]
    loss  = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) * 100
    pf       = (sum(wins) / abs(sum(loss))) if loss else float('inf')
    total_pnl = sum(pnls)
    avg_trade = float(np.mean(pnls))
    avg_bars  = float(np.mean([t['duree_bars'] for t in trades]))

    # Durée en jours
    def to_dt(x):
        if hasattr(x, 'to_pydatetime'):
            return x.to_pydatetime().replace(tzinfo=None)
        return x

    t0       = to_dt(trades[0]['entry_time'])
    t1       = to_dt(trades[-1]['exit_time'])
    n_days   = max(1, (t1 - t0).days)

    # Jours de trading actifs
    trade_dates = defaultdict(int)
    for t in trades:
        d = to_dt(t['entry_time'])
        trade_dates[d.date() if hasattr(d, 'date') else d] += 1
    n_trading_days = max(1, len(trade_dates))
    avg_trades_day = np.mean(list(trade_dates.values())) if trade_dates else 0
    avg_pnl_per_day = total_pnl / n_trading_days

    # Sharpe annualisé sur les PnL $
    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252)
    else:
        sharpe = 0.0

    # Max drawdown en $ et %
    max_dd_usd = 0.0
    peak = ACCOUNT_SIZE
    for e in equity_curve:
        a = e['account']
        if a > peak:
            peak = a
        dd = a - peak
        if dd < max_dd_usd:
            max_dd_usd = dd
    max_dd_pct = abs(max_dd_usd) / ACCOUNT_SIZE * 100

    # Métriques risque (par trade, ajusté par nb contrats)
    sl_pts_list = [t['sl_pts'] for t in trades]
    avg_sl_pts  = float(np.mean(sl_pts_list)) if sl_pts_list else 0
    avg_sl_usd  = avg_sl_pts * POINT_VALUE * contracts
    max_sl_usd  = max(sl_pts_list, default=0) * POINT_VALUE * contracts

    # Apex : jours stoppés
    pct_days_stopped = 0.0
    n_daily_limit = 0
    if daily_stats:
        n_stopped = sum(1 for d in daily_stats if d['stopped'])
        n_daily_limit = n_stopped
        pct_days_stopped = n_stopped / len(daily_stats) * 100

    # Combien de trades perdants max avant daily limit ?
    if avg_sl_usd > 0:
        trades_before_limit = abs(DAILY_LOSS_LIMIT) / avg_sl_usd
    else:
        trades_before_limit = 0.0

    # Progression vers l'objectif Apex
    fa = final_account if final_account is not None else ACCOUNT_SIZE
    profit_toward_target = ((fa - ACCOUNT_SIZE) / PROFIT_TARGET * 100)

    return {
        'n_trades':                  len(trades),
        'win_rate':                  win_rate,
        'profit_factor':             pf,
        'sharpe':                    sharpe,
        'max_dd_usd':                max_dd_usd,
        'max_dd_pct':                max_dd_pct,
        'total_pnl_usd':             total_pnl,
        'avg_trade_usd':             avg_trade,
        'avg_bars':                  avg_bars,
        'trades_per_day':            avg_trades_day,
        'avg_pnl_per_day':           avg_pnl_per_day,
        'avg_sl_pts':                avg_sl_pts,
        'avg_sl_usd':                avg_sl_usd,
        'max_sl_usd':                max_sl_usd,
        'avg_risk_usd':              avg_sl_usd,
        'trades_before_daily_limit': trades_before_limit,
        'pct_days_stopped':          pct_days_stopped,
        'n_daily_limit':             n_daily_limit,
        'apex_halted':               halted,
        'final_account':             fa,
        'profit_toward_target_pct':  profit_toward_target,
    }


# ─────────────────────────────────────────────────────────────
# 9. OPTIMISATION
# ─────────────────────────────────────────────────────────────
def optimize(df: pd.DataFrame, conv: float = 1.0,
             param_grid: dict = None,
             contracts: int = 1) -> dict:
    """
    Grid search.
    Score = profit_factor × (win_rate/100) × freq_bonus
    freq_bonus = 1.0 si 4–6 signaux/jour, 0.85 si 3–8, 0.70 sinon
    """
    if param_grid is None:
        param_grid = PARAM_GRID_DEFAULT

    keys   = list(param_grid.keys())
    combos = list(product(*param_grid.values()))
    total  = len(combos)
    print(f"\n🔍 Optimisation ({contracts}c)... ({total} combinaisons)")

    results = []
    for idx, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        try:
            res = backtest(df, conv=conv, contracts=contracts, **params)
            s   = res['stats']
            if s['n_trades'] < 5:
                continue

            # Bonus fréquence
            tpd = s['trades_per_day']
            if 4 <= tpd <= 6:
                freq_bonus = 1.00
            elif 3 <= tpd <= 8:
                freq_bonus = 0.85
            else:
                freq_bonus = 0.70

            pf = max(s['profit_factor'], 0)
            wr = s['win_rate'] / 100
            score = pf * wr * freq_bonus

            results.append({**params, **s, 'score': score,
                             'freq_bonus': freq_bonus})
        except Exception:
            continue

        if (idx + 1) % 10 == 0 or idx + 1 == total:
            print(f"  ... {idx+1}/{total} ({(idx+1)/total*100:.0f}%)")

    if not results:
        print("  ⚠️  Aucun résultat valide (trop peu de trades ou conv trop stricte).")
        return {'best_params': {}, 'best_score': 0, 'ranking': pd.DataFrame()}

    ranking = (pd.DataFrame(results)
               .sort_values('score', ascending=False)
               .reset_index(drop=True))
    best        = ranking.iloc[0]
    best_params = {k: best[k] for k in keys}

    print(f"\n✅ Meilleurs paramètres ({contracts}c) :")
    print(f"   body_pct={best_params['body_pct']}, "
          f"rr_ratio={best_params['rr_ratio']}, "
          f"max_wait_bars={int(best_params['max_wait_bars'])}")
    print(f"   Win Rate: {best['win_rate']:.1f}% | "
          f"PF: {best['profit_factor']:.2f} | "
          f"Sharpe: {best['sharpe']:.2f} | "
          f"Trades/jour: {best['trades_per_day']:.1f}")
    print(f"   P&L moy/jour: ${best.get('avg_pnl_per_day',0):+.0f} | "
          f"Risque moy: ${best['avg_sl_usd']:.0f}/trade | "
          f"DD max: -${abs(best['max_dd_usd']):.0f}")

    return {'best_params': best_params,
            'best_score':  float(best['score']),
            'ranking':     ranking}


# ─────────────────────────────────────────────────────────────
# 10. WALK-FORWARD
# ─────────────────────────────────────────────────────────────
def walk_forward(df: pd.DataFrame, conv: float = 1.0,
                 train_days: int = 180, test_days: int = 30,
                 step_days: int = 30,
                 contracts: int = 1) -> dict:
    """
    Walk-forward (fenêtre glissante) :
    Train → optimise → Test → avance → recommence.
    """
    print(f"\n🧠 Walk-forward ({contracts}c)... train={train_days}j  test={test_days}j  pas={step_days}j")

    mini_grid = {
        "body_pct":      [0.25, 0.30],
        "rr_ratio":      [2.0, 3.0],
        "max_wait_bars": [3, 5],
    }

    dates      = df.index.normalize().unique()
    periods    = []
    wf_trades  = []
    wf_equity  = [{'time': df.index[0], 'account': ACCOUNT_SIZE}]
    wf_account = ACCOUNT_SIZE
    period_num = 0
    start_idx  = 0

    while True:
        if start_idx >= len(dates):
            break

        train_start = dates[start_idx]
        train_end   = train_start + timedelta(days=train_days)
        test_end    = train_end   + timedelta(days=test_days)

        df_train = df[(df.index.normalize() >= train_start) &
                      (df.index.normalize() <  train_end)]
        df_test  = df[(df.index.normalize() >= train_end) &
                      (df.index.normalize() <  test_end)]

        if len(df_train) < 60 or len(df_test) < 5:
            break

        opt = optimize(df_train, conv=conv, param_grid=mini_grid,
                       contracts=contracts)
        if not opt['best_params']:
            break

        bp          = opt['best_params']
        train_res   = backtest(df_train, conv=conv, contracts=contracts, **bp)
        test_res    = backtest(df_test,  conv=conv, contracts=contracts, **bp)
        ts, trs     = test_res['stats'], train_res['stats']

        # Rebaser le PnL test sur le compte WF courant
        if test_res['trades']:
            scale = wf_account / ACCOUNT_SIZE
            for t in test_res['trades']:
                tc              = t.copy()
                tc['pnl_usd']   = t['pnl_usd'] * scale
                wf_account     += tc['pnl_usd']
                tc['account']   = wf_account
                wf_trades.append(tc)
            wf_equity.append({'time': df_test.index[-1],
                               'account': wf_account})

        period_num += 1
        print(f"  P{period_num} ({train_start.strftime('%Y-%m')} → "
              f"{train_end.strftime('%Y-%m')}) : "
              f"Train WR={trs['win_rate']:.0f}% ({trs['n_trades']}T) | "
              f"Test WR={ts['win_rate']:.0f}% ({ts['n_trades']}T) | "
              f"Params bp={bp['body_pct']} rr={bp['rr_ratio']}")

        periods.append({
            'period': period_num, 'train_start': train_start,
            'train_end': train_end, 'params': bp,
            'train_wr': trs['win_rate'], 'test_wr': ts['win_rate'],
            'train_pf': trs['profit_factor'], 'test_pf': ts['profit_factor'],
            'train_n': trs['n_trades'], 'test_n': ts['n_trades'],
        })

        next_start = train_start + timedelta(days=step_days)
        try:
            ns64      = np.datetime64(next_start.date(), 'D')
            start_idx = int(np.searchsorted(dates.values, ns64))
        except Exception:
            start_idx += max(1, step_days // max(1, (dates[-1]-dates[0]).days//len(dates)))

    if not periods:
        print("  ⚠️  Pas assez de données pour le walk-forward.")
        return {'periods': [], 'wf_equity': pd.DataFrame(),
                'wf_trades': [], 'wf_stats': {}}

    wf_eq    = (pd.DataFrame(wf_equity).set_index('time')
                if wf_equity else pd.DataFrame())
    wf_stats = compute_stats(wf_trades,
                             [{'account': e['account']} for e in wf_equity],
                             contracts=contracts)
    wf_stats['final_account'] = wf_account

    print(f"\n  📊 WF global : WR={wf_stats['win_rate']:.1f}% | "
          f"PF={wf_stats['profit_factor']:.2f} | "
          f"Sharpe={wf_stats['sharpe']:.2f} | "
          f"Compte: ${wf_account:,.0f}")

    return {'periods': periods, 'wf_equity': wf_eq,
            'wf_trades': wf_trades, 'wf_stats': wf_stats}


# ─────────────────────────────────────────────────────────────
# 11. RAPPORT VISUEL (original)
# ─────────────────────────────────────────────────────────────
def generate_report(bt_result: dict, wf_result: dict = None,
                    output_path: str = "trading/nasdaq_report.png"):
    """
    Rapport 4 panneaux (thème sombre) :
    1. Equity curve (compte 100k$) + niveaux Apex
    2. Distribution PnL en $
    3. Walk-forward WR par période
    4. Tableau stats complet + métriques Apex
    """
    fig = plt.figure(figsize=(20, 14))
    fig.patch.set_facecolor('#0d1117')
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.42, wspace=0.35)

    C = dict(
        bg='#0d1117', panel='#161b22', text='#e6edf3',
        green='#3fb950', red='#f85149', blue='#58a6ff',
        gold='#d29922', grey='#8b949e', orange='#f0883e',
        purple='#bc8cff',
    )

    def style_ax(ax, title):
        ax.set_facecolor(C['panel'])
        ax.tick_params(colors=C['text'], labelsize=9)
        for spine in ax.spines.values():
            spine.set_color('#30363d')
        ax.xaxis.label.set_color(C['text'])
        ax.yaxis.label.set_color(C['text'])
        ax.set_title(title, fontsize=11, fontweight='bold',
                     pad=10, color=C['text'])

    # ── Panneau 1 : Equity Curve ─────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    style_ax(ax1, '📈 Compte Apex ($100k) — Equity Curve')

    eq = bt_result.get('equity_curve', pd.DataFrame())
    if not eq.empty and 'account' in eq.columns:
        ax1.plot(eq.index, eq['account'], color=C['blue'],
                 linewidth=1.5, label='Backtest', alpha=0.9)

    ax1.axhline(y=ACCOUNT_SIZE,
                color=C['grey'],  linestyle=':',  linewidth=1,
                label=f'Capital initial ${ACCOUNT_SIZE:,.0f}')
    ax1.axhline(y=ACCOUNT_SIZE + PROFIT_TARGET,
                color=C['green'], linestyle='--', linewidth=1.2,
                label=f'Objectif (${ACCOUNT_SIZE+PROFIT_TARGET:,.0f})')
    ax1.axhline(y=ACCOUNT_SIZE + MAX_DD_LIMIT,
                color=C['red'],   linestyle='--', linewidth=1.2,
                label=f'Max DD -8% (${ACCOUNT_SIZE+MAX_DD_LIMIT:,.0f})')

    ax1.set_ylabel('Compte (USD)', color=C['text'])
    ax1.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=7.5)
    ax1.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # ── Panneau 2 : Distribution PnL en $ ───────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    style_ax(ax2, '💰 Distribution PnL par trade ($)')

    trades = bt_result.get('trades', [])
    if trades:
        pnls   = [t['pnl_usd'] for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        bins   = min(40, max(8, len(pnls) // 3))

        if losses:
            ax2.hist(losses, bins=bins//2, color=C['red'],
                     alpha=0.75, label=f'Pertes ({len(losses)})')
        if wins:
            ax2.hist(wins,   bins=bins//2, color=C['green'],
                     alpha=0.75, label=f'Gains ({len(wins)})')

        ax2.axvline(x=0,            color='white',    linestyle='--', linewidth=1)
        ax2.axvline(x=np.mean(pnls), color=C['gold'], linewidth=1.5,
                    label=f"Moy: ${np.mean(pnls):.0f}")
        ax2.axvline(x=DAILY_LOSS_LIMIT, color=C['orange'], linestyle=':',
                    linewidth=1, label=f'Daily limit ${DAILY_LOSS_LIMIT:.0f}')

        ax2.set_xlabel('PnL par trade ($)', color=C['text'])
        ax2.set_ylabel('Fréquence', color=C['text'])
        ax2.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)

    # ── Panneau 3 : Walk-forward WR ──────────────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    style_ax(ax3, '🧠 Walk-Forward — Win Rate Train vs Test')

    if wf_result and wf_result.get('periods'):
        periods  = wf_result['periods']
        x_arr    = np.arange(len(periods))
        w = 0.35

        ax3.bar(x_arr - w/2, [p['train_wr'] for p in periods],
                w, color=C['blue'], alpha=0.8, label='Train WR')
        ax3.bar(x_arr + w/2, [p['test_wr']  for p in periods],
                w, color=C['gold'], alpha=0.8, label='Test WR')

        ax3.axhline(y=50, color=C['grey'], linestyle=':', linewidth=1)
        ax3.set_xticks(x_arr)
        ax3.set_xticklabels([f"P{p['period']}" for p in periods],
                             rotation=45, fontsize=8)
        ax3.set_ylabel('Win Rate (%)', color=C['text'])
        ax3.set_ylim(0, 110)
        ax3.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)
    else:
        ax3.text(0.5, 0.5, 'Walk-forward non exécuté',
                 ha='center', va='center', color=C['text'],
                 transform=ax3.transAxes)

    # ── Panneau 4 : Tableau stats ────────────────────────────
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.set_facecolor(C['panel'])
    ax4.axis('off')
    ax4.set_title('📊 Statistiques + Métriques Apex', fontsize=11,
                  fontweight='bold', pad=10, color=C['text'])

    s      = bt_result.get('stats', {})
    params = s.get('params', {})
    fa     = s.get('final_account', ACCOUNT_SIZE)
    pnl_t  = fa - ACCOUNT_SIZE

    rows = [
        ('— COMPTE APEX 100k —',       '',          C['blue']),
        ('Capital initial',            f"${ACCOUNT_SIZE:,.0f}",     None),
        ('Capital final',              f"${fa:,.0f}",                None),
        ('PnL total',                  f"${pnl_t:+,.0f}",           C['green'] if pnl_t>=0 else C['red']),
        ('P&L moy/jour',               f"${s.get('avg_pnl_per_day',0):+.0f}   (objectif: +${TARGET_DAILY_PNL:.0f})",
                                       C['green'] if s.get('avg_pnl_per_day',0) >= TARGET_DAILY_PNL else C['orange']),
        ('SEP', None, None),
        ('— TRADING —',                '',          C['blue']),
        ('Total trades',               f"{s.get('n_trades',0)}",    None),
        ('Win Rate',                   f"{s.get('win_rate',0):.1f}%", None),
        ('Profit Factor',              f"{s.get('profit_factor',0):.2f}", None),
        ('Sharpe (annualisé)',         f"{s.get('sharpe',0):.2f}",  None),
        ('Trades / jour',              f"{s.get('trades_per_day',0):.1f}", None),
        ('SEP', None, None),
        ('— RISQUE PAR TRADE —',       '',          C['orange']),
        ('SL moyen (pts NQ)',          f"{s.get('avg_sl_pts',0):.1f} pts", None),
        ('Risque moyen ($)',           f"${s.get('avg_sl_usd',0):.0f}", None),
        ('SEP', None, None),
        ('— APEX RÈGLES —',            '',          C['red']),
        ('Max DD ($)',                 f"${abs(s.get('max_dd_usd',0)):,.0f} ({s.get('max_dd_pct',0):.1f}%)",
                                       C['red'] if s.get('max_dd_pct',0) > 5 else None),
        ('Jours daily limit',          f"{s.get('n_daily_limit',0)}", None),
        ('DD limit dépassée',          '⛔ OUI' if s.get('apex_halted') else '✅ NON',
                                       C['red'] if s.get('apex_halted') else C['green']),
        ('SEP', None, None),
        ('— PARAMÈTRES —',             '',          C['purple']),
        ('body_pct',                   str(params.get('body_pct', '-')),  None),
        ('rr_ratio',                   str(params.get('rr_ratio', '-')),  None),
        ('Contrats',                   str(params.get('contracts', 1)),   None),
    ]

    y = 0.98
    for label, value, forced_color in rows:
        if label == 'SEP':
            ax4.plot([0.02, 0.98], [y+0.005, y+0.005], color='#30363d',
                     linewidth=0.5, transform=ax4.transAxes)
            y -= 0.022
            continue

        if value == '' and label.startswith('—'):
            ax4.text(0.05, y, label, transform=ax4.transAxes,
                     color=forced_color or C['blue'], fontsize=8,
                     fontweight='bold', va='top')
            y -= 0.040
            continue

        vc = forced_color if forced_color else C['text']
        ax4.text(0.05, y, label, transform=ax4.transAxes,
                 color=C['grey'], fontsize=8.5, va='top')
        ax4.text(0.62, y, str(value), transform=ax4.transAxes,
                 color=vc, fontsize=8.5, va='top', fontweight='bold')
        y -= 0.040

    fig.suptitle(
        f'NAS100 Reversal — NQ E-mini  |  Apex 100k$  |  '
        f'SL {SL_MIN_PTS}–{SL_MAX_PTS}pts  |  '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=12, fontweight='bold', color=C['text'], y=0.988,
    )

    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=C['bg'], edgecolor='none')
    plt.close()
    print(f"\n✅ Rapport sauvegardé : {output_path}")


# ─────────────────────────────────────────────────────────────
# 11b. RAPPORT COMPARATIF 1 vs 2 CONTRATS
# ─────────────────────────────────────────────────────────────
def generate_comparison_report(bt1: dict, bt2: dict,
                                output_path: str = "trading/nasdaq_comparison.png"):
    """
    Rapport côte à côte : 1 Contrat vs 2 Contrats
    3 lignes × 2 colonnes :
      Ligne 1 : Equity curves
      Ligne 2 : PnL journalier (barres)
      Ligne 3 : Tableau stats comparatif
    """
    fig = plt.figure(figsize=(22, 16))
    fig.patch.set_facecolor('#0d1117')
    gs  = gridspec.GridSpec(3, 2, figure=fig,
                            hspace=0.45, wspace=0.30,
                            height_ratios=[1.8, 1.4, 1.8])

    C = dict(
        bg='#0d1117', panel='#161b22', text='#e6edf3',
        green='#3fb950', red='#f85149', blue='#58a6ff',
        gold='#d29922', grey='#8b949e', orange='#f0883e',
        purple='#bc8cff', cyan='#39d353',
    )

    def style_ax(ax, title, col_color=None):
        ax.set_facecolor(C['panel'])
        ax.tick_params(colors=C['text'], labelsize=9)
        for spine in ax.spines.values():
            spine.set_color('#30363d')
        ax.xaxis.label.set_color(C['text'])
        ax.yaxis.label.set_color(C['text'])
        color = col_color or C['text']
        ax.set_title(title, fontsize=11, fontweight='bold',
                     pad=8, color=color)

    colors_col = [C['blue'], C['gold']]
    labels_col = ['1 Contrat', '2 Contrats']
    bts        = [bt1, bt2]

    # ────────────────────────────────────────────
    # LIGNE 1 : Equity curves
    # ────────────────────────────────────────────
    for col in range(2):
        ax = fig.add_subplot(gs[0, col])
        style_ax(ax, f'📈 Equity Curve — {labels_col[col]}', colors_col[col])

        eq = bts[col].get('equity_curve', pd.DataFrame())
        if not eq.empty and 'account' in eq.columns:
            ax.plot(eq.index, eq['account'], color=colors_col[col],
                    linewidth=1.5, alpha=0.92)

        ax.axhline(y=ACCOUNT_SIZE, color=C['grey'], linestyle=':',
                   linewidth=1, label=f'100k$')
        ax.axhline(y=ACCOUNT_SIZE + MAX_DD_LIMIT, color=C['red'],
                   linestyle='--', linewidth=1,
                   label=f'Max DD -8% ({ACCOUNT_SIZE+MAX_DD_LIMIT:,.0f}$)')
        ax.axhline(y=ACCOUNT_SIZE + TARGET_DAILY_PNL * 30,
                   color=C['green'], linestyle=':', linewidth=0.8, alpha=0.5,
                   label=f'Réf 1000$/j×30j')

        ax.set_ylabel('Compte (USD)', color=C['text'])
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
        ax.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=7.5,
                  loc='upper left')

    # ────────────────────────────────────────────
    # LIGNE 2 : PnL journalier
    # ────────────────────────────────────────────
    for col in range(2):
        ax = fig.add_subplot(gs[1, col])
        style_ax(ax, f'📊 PnL Journalier — {labels_col[col]}', colors_col[col])

        ds = bts[col].get('daily_stats', [])
        if ds:
            dates = [d['date'] for d in ds]
            pnls  = [d['pnl']  for d in ds]
            bar_colors = [C['green'] if p >= 0 else C['red'] for p in pnls]

            ax.bar(range(len(dates)), pnls, color=bar_colors, alpha=0.8, width=0.8)
            ax.axhline(y=0, color=C['grey'], linewidth=0.8)
            ax.axhline(y=TARGET_DAILY_PNL, color=C['cyan'], linestyle='--',
                       linewidth=1, alpha=0.7,
                       label=f'Objectif +${TARGET_DAILY_PNL:.0f}/j')
            ax.axhline(y=DAILY_LOSS_LIMIT, color=C['orange'], linestyle=':',
                       linewidth=1, label=f'Daily limit ${DAILY_LOSS_LIMIT:.0f}')

            ax.set_ylabel('PnL ($)', color=C['text'])
            ax.set_xlabel('Jours de trading', color=C['text'])
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
            ax.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=7.5)
        else:
            ax.text(0.5, 0.5, 'Aucun trade', ha='center', va='center',
                    color=C['text'], transform=ax.transAxes)

    # ────────────────────────────────────────────
    # LIGNE 3 : Tableau comparatif (2 colonnes)
    # ────────────────────────────────────────────
    for col in range(2):
        ax = fig.add_subplot(gs[2, col])
        ax.set_facecolor(C['panel'])
        ax.axis('off')
        style_ax(ax, f'📋 Statistiques — {labels_col[col]}', colors_col[col])

        s = bts[col].get('stats', {})
        p = s.get('params', {})
        fa = s.get('final_account', ACCOUNT_SIZE)
        pnl_t = fa - ACCOUNT_SIZE
        avg_day = s.get('avg_pnl_per_day', 0)
        ok_color = C['green'] if avg_day >= TARGET_DAILY_PNL else C['orange']

        rows_data = [
            ('Win Rate',             f"{s.get('win_rate',0):.1f}%",       None),
            ('Profit Factor',        f"{s.get('profit_factor',0):.2f}",   None),
            ('Sharpe',               f"{s.get('sharpe',0):.2f}",          None),
            ('P&L Total',            f"${pnl_t:+,.0f}",
                                     C['green'] if pnl_t >= 0 else C['red']),
            ('P&L Moy/Jour',         f"${avg_day:+.0f}  (obj: +${TARGET_DAILY_PNL:.0f})",
                                     ok_color),
            ('Max Drawdown',         f"-${abs(s.get('max_dd_usd',0)):,.0f} ({s.get('max_dd_pct',0):.1f}%)",
                                     C['red'] if s.get('max_dd_pct',0) > 5 else C['text']),
            ('Jours daily limit',    f"{s.get('n_daily_limit',0)}",       None),
            ('Signaux/jour',         f"{s.get('trades_per_day',0):.1f}",  None),
            ('Risque moy/trade',     f"${s.get('avg_risk_usd',0):.0f}",   None),
            ('Nb trades',            f"{s.get('n_trades',0)}",             None),
            ('—', None, None),
            ('PARAMÈTRES',           '',                                   colors_col[col]),
            ('body_pct',             str(p.get('body_pct', '-')),          None),
            ('rr_ratio',             str(p.get('rr_ratio', '-')),          None),
            ('max_wait_bars',        str(int(p.get('max_wait_bars', 0))), None),
            ('Contrats',             str(p.get('contracts', col+1)),       None),
        ]

        y = 0.96
        for label, value, forced_color in rows_data:
            if label == '—':
                ax.plot([0.02, 0.98], [y+0.01, y+0.01], color='#30363d',
                        linewidth=0.5, transform=ax.transAxes)
                y -= 0.03
                continue
            if value == '' and forced_color:
                ax.text(0.05, y, label, transform=ax.transAxes,
                        color=forced_color, fontsize=9, fontweight='bold', va='top')
                y -= 0.055
                continue

            vc = forced_color if forced_color else C['text']
            ax.text(0.05, y, label, transform=ax.transAxes,
                    color=C['grey'], fontsize=9, va='top')
            ax.text(0.55, y, str(value), transform=ax.transAxes,
                    color=vc, fontsize=9, va='top', fontweight='bold')
            y -= 0.055

    # Titre global
    fig.suptitle(
        f'NQ Futures — Comparaison 1 vs 2 Contrats  |  Apex 100k$  |  '
        f'SL {SL_MIN_PTS}–{SL_MAX_PTS}pts  |  Objectif: +${TARGET_DAILY_PNL:.0f}/jour  |  '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=12, fontweight='bold', color=C['text'], y=0.997,
    )

    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=C['bg'], edgecolor='none')
    plt.close()
    print(f"\n✅ Rapport comparatif sauvegardé : {output_path}")


# ─────────────────────────────────────────────────────────────
# 12. DIAGNOSTIC
# ─────────────────────────────────────────────────────────────
def diagnostic(df: pd.DataFrame, conv: float = 1.0, label: str = ""):
    """
    Affiche combien de signaux passent chaque filtre.
    """
    df2 = detect_exhaustion(df, conv=conv)
    n_total    = len(df2)
    n_bias     = (df2['bias'] != 0).sum()
    n_valid_sl = df2['signal_valid'].sum()
    bodies_pts = df2.loc[df2['bias'] != 0, 'signal_pts']

    try:
        n_days = max(1, (df.index[-1] - df.index[0]).days * 5 / 7)
    except Exception:
        n_days = 1

    lbl = f" [{label}]" if label else ""
    print(f"\n🔬 Diagnostic{lbl} :")
    print(f"  Barres totales           : {n_total}  (~{n_days:.0f} jours trading)")
    print(f"  Patterns d'épuisement    : {n_bias} "
          f"({n_bias/max(1,n_total)*100:.1f}%)"
          f"  ≈ {n_bias/n_days:.1f}/jour")
    if len(bodies_pts) > 0:
        print(f"  Corps signal (pts NQ)    : "
              f"min={bodies_pts.min():.1f}  "
              f"moy={bodies_pts.mean():.1f}  "
              f"med={bodies_pts.median():.1f}  "
              f"max={bodies_pts.max():.1f}")
        print(f"  Valides SL {SL_MIN_PTS}–{SL_MAX_PTS}pts         : "
              f"{n_valid_sl} ({n_valid_sl/max(1,n_bias)*100:.1f}%)"
              f"  ≈ {n_valid_sl/n_days:.1f}/jour")
        quantiles = bodies_pts.quantile([0.25, 0.50, 0.75])
        print(f"  Quartiles corps (pts NQ) : "
              f"Q25={quantiles[0.25]:.1f}  "
              f"Q50={quantiles[0.50]:.1f}  "
              f"Q75={quantiles[0.75]:.1f}")
    else:
        print("  Aucun pattern détecté.")


# ─────────────────────────────────────────────────────────────
# 13. MODE COMPARE — 1 vs 2 contrats
# ─────────────────────────────────────────────────────────────
def run_compare(df: pd.DataFrame, conv: float):
    """
    Lance 2 optimisations + backtests côte à côte (1c vs 2c).
    Génère le rapport comparatif et affiche le tableau console.
    """
    param_grid = {
        "body_pct":      [0.20, 0.25, 0.30, 0.35],
        "rr_ratio":      [1.5, 2.0, 3.0],
        "max_wait_bars": [2, 3, 5],
    }

    print("\n" + "=" * 60)
    print("  OPTIMISATION — 1 CONTRAT NQ")
    print("=" * 60)
    opt1 = optimize(df, conv=conv, param_grid=param_grid, contracts=1)
    best1 = opt1.get('best_params') or {'rr_ratio': 2.0, 'body_pct': 0.30, 'max_wait_bars': 3}

    print("\n" + "=" * 60)
    print("  OPTIMISATION — 2 CONTRATS NQ")
    print("=" * 60)
    opt2 = optimize(df, conv=conv, param_grid=param_grid, contracts=2)
    best2 = opt2.get('best_params') or {'rr_ratio': 2.0, 'body_pct': 0.30, 'max_wait_bars': 3}

    print("\n🔄 Backtest final — 1 contrat...")
    bt1 = backtest(df, conv=conv, contracts=1, **best1)

    print("🔄 Backtest final — 2 contrats...")
    bt2 = backtest(df, conv=conv, contracts=2, **best2)

    s1 = bt1['stats']
    s2 = bt2['stats']

    # ── Rapport PNG ──────────────────────────────────────────
    generate_comparison_report(bt1, bt2,
                                output_path="trading/nasdaq_comparison.png")

    # ── Verdict ──────────────────────────────────────────────
    def verdict(s):
        avg = s.get('avg_pnl_per_day', 0)
        if avg >= TARGET_DAILY_PNL:
            return "OBJECTIF ATTEINT ✅"
        elif avg >= TARGET_DAILY_PNL * 0.5:
            return "VIABLE ⚠️"
        else:
            return "INSUFFISANT ❌"

    # ── Tableau console ──────────────────────────────────────
    print()
    print("=" * 62)
    print("  RÉSULTATS COMPARATIFS — NQ Futures (Apex 100k$)")
    print("=" * 62)
    print(f"{'':25s}  {'1 CONTRAT':>12s}  {'2 CONTRATS':>12s}")
    print("-" * 62)
    print(f"{'Win Rate':25s}  {s1['win_rate']:>11.1f}%  {s2['win_rate']:>11.1f}%")
    print(f"{'Profit Factor':25s}  {s1['profit_factor']:>12.2f}  {s2['profit_factor']:>12.2f}")
    print(f"{'Sharpe':25s}  {s1['sharpe']:>12.2f}  {s2['sharpe']:>12.2f}")
    pnl1 = s1.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    pnl2 = s2.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    print(f"{'P&L Total':25s}  {pnl1:>+11,.0f}$  {pnl2:>+11,.0f}$")
    avg1 = s1.get('avg_pnl_per_day', 0)
    avg2 = s2.get('avg_pnl_per_day', 0)
    print(f"{'P&L Moy/Jour':25s}  {avg1:>+11,.0f}$  {avg2:>+11,.0f}$  ← objectif: +{TARGET_DAILY_PNL:.0f}$/j")
    dd1 = abs(s1.get('max_dd_usd', 0))
    dd2 = abs(s2.get('max_dd_usd', 0))
    print(f"{'Max Drawdown':25s}  {-dd1:>+11,.0f}$  {-dd2:>+11,.0f}$")
    print(f"{'Jours daily limit':25s}  {s1.get('n_daily_limit',0):>12d}  {s2.get('n_daily_limit',0):>12d}")
    print(f"{'Signaux/jour':25s}  {s1['trades_per_day']:>12.1f}  {s2['trades_per_day']:>12.1f}")
    risk1 = s1.get('avg_risk_usd', 0)
    risk2 = s2.get('avg_risk_usd', 0)
    print(f"{'Risque moy/trade':25s}  {risk1:>11,.0f}$  {risk2:>11,.0f}$")
    print("-" * 62)
    print(f"Best params (1c) : body_ratio={best1['body_pct']}, "
          f"rr={best1['rr_ratio']}, wait={int(best1['max_wait_bars'])}")
    print(f"Best params (2c) : body_ratio={best2['body_pct']}, "
          f"rr={best2['rr_ratio']}, wait={int(best2['max_wait_bars'])}")
    print()
    print("VERDICT :")
    print(f"  1 contrat  → {verdict(s1)}")
    print(f"  2 contrats → {verdict(s2)}")
    print("=" * 62)

    return bt1, bt2


# ─────────────────────────────────────────────────────────────
# 14. POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='NAS100 Reversal — Apex Funding NQ E-mini')
    parser.add_argument('--mode',
        choices=['backtest', 'optimize', 'walkforward', 'report', 'diag', 'compare', '5m', '5m_adx'],
        default='report')
    args = parser.parse_args()

    print("=" * 68)
    print("  NAS100 REVERSAL — NQ E-mini  |  Apex 100k$")
    print(f"  SL : {SL_MIN_PTS}–{SL_MAX_PTS} pts NQ  |  "
          f"Daily limit : ${abs(DAILY_LOSS_LIMIT):.0f}  |  "
          f"Max DD : ${abs(MAX_DD_LIMIT):.0f}  |  "
          f"Objectif/j : ${TARGET_DAILY_PNL:.0f}")
    print("=" * 68)

    # ── Données 1H (proxy long terme) ──────────────────────
    print("\n📦 Données 1H (2 ans) :")
    df_1h_raw, conv = download_data(interval='1h', period='2y')
    df_1h = filter_rth(df_1h_raw)

    # ── Données 10M (validation court terme) ───────────────
    df_10m = None
    conv2  = conv
    try:
        print("\n📦 Données 5M (60 jours) → 10M :")
        df_5m_raw, conv2 = download_data(interval='5m', period='60d')
        df_5m  = filter_rth(df_5m_raw)
        df_10m = resample_5m_to_10m(df_5m)
    except Exception as e:
        print(f"  ⚠️  5M indisponible ({e}).")

    # ── DIAG ───────────────────────────────────────────────
    if args.mode == 'diag':
        diagnostic(df_1h, conv=conv, label="1H (2 ans)")
        if df_10m is not None:
            diagnostic(df_10m, conv=conv2, label="10M (60j)")
        return

    # ── BACKTEST ────────────────────────────────────────────
    if args.mode == 'backtest':
        print("\n🔄 Backtest paramètres par défaut (1H)...")
        res = backtest(df_1h, conv=conv,
                       rr_ratio=2.0, body_pct=0.30, max_wait_bars=3,
                       contracts=1)
        s = res['stats']
        _print_summary(s)
        return

    # ── OPTIMIZE ────────────────────────────────────────────
    if args.mode == 'optimize':
        opt = optimize(df_1h, conv=conv, contracts=1)
        if df_10m is not None and opt['best_params']:
            print("\n🔄 Validation 10M (60j) :")
            val = backtest(df_10m, conv=conv2, contracts=1, **opt['best_params'])
            sv  = val['stats']
            print(f"  WR={sv['win_rate']:.1f}%  PF={sv['profit_factor']:.2f}  "
                  f"Trades={sv['n_trades']}  /jour={sv['trades_per_day']:.1f}")
        return

    # ── WALKFORWARD ─────────────────────────────────────────
    if args.mode == 'walkforward':
        walk_forward(df_1h, conv=conv,
                     train_days=180, test_days=30, step_days=30,
                     contracts=1)
        return

    # ── COMPARE — 1 vs 2 contrats ──────────────────────────
    if args.mode == 'compare':
        diagnostic(df_1h, conv=conv, label="1H (2 ans)")
        run_compare(df_1h, conv=conv)
        return

    # ── MODE 5M — NQ=F 5 minutes direct ────────────────────
    if args.mode == '5m':
        run_5m()
        return

    # ── MODE 5M_ADX — NQ=F 5M + filtre ADX ─────────────────
    if args.mode == '5m_adx':
        run_5m_adx()
        return

    # ── REPORT (complet) ────────────────────────────────────
    if args.mode == 'report':
        print("\n🔬 Diagnostic signaux :")
        diagnostic(df_1h,  conv=conv,  label="1H (2 ans)")
        if df_10m is not None:
            diagnostic(df_10m, conv=conv2, label="10M (60j)")

        print("\n🔍 Optimisation sur données 1H (2 ans)...")
        opt = optimize(df_1h, conv=conv, param_grid={
            "body_pct":      [0.25, 0.30, 0.35],
            "rr_ratio":      [1.5, 2.0, 3.0],
            "max_wait_bars": [3, 5],
        }, contracts=1)
        best = opt.get('best_params') or {
            'rr_ratio': 2.0, 'body_pct': 0.30, 'max_wait_bars': 3}

        if df_10m is not None and len(df_10m) >= 50:
            print(f"\n🔄 Backtest principal sur 10M (timeframe cible)...")
            bt_res = backtest(df_10m, conv=conv2, contracts=1, **best)
        else:
            print(f"\n🔄 Backtest principal sur 1H (fallback)...")
            bt_res = backtest(df_1h, conv=conv, contracts=1, **best)

        print("\n🧠 Walk-forward sur 1H (2 ans)...")
        wf_res = walk_forward(df_1h, conv=conv,
                              train_days=180, test_days=30, step_days=30,
                              contracts=1)

        generate_report(bt_res, wf_res,
                        output_path="trading/nasdaq_report.png")

        s = bt_res['stats']
        print("\n" + "=" * 68)
        print("  RÉSUMÉ FINAL")
        print("=" * 68)
        print(f"  Params : body_pct={best['body_pct']}  "
              f"rr_ratio={best['rr_ratio']}  "
              f"max_wait={int(best['max_wait_bars'])}")
        _print_summary(s)


def _print_summary(s: dict):
    """Affichage compact des stats dans la console."""
    fa  = s.get('final_account', ACCOUNT_SIZE)
    pnl = fa - ACCOUNT_SIZE
    avg_day = s.get('avg_pnl_per_day', 0)
    print(f"  Win Rate : {s['win_rate']:.1f}%  |  "
          f"PF : {s['profit_factor']:.2f}  |  "
          f"Sharpe : {s['sharpe']:.2f}")
    print(f"  Trades   : {s['n_trades']}  |  "
          f"/jour : {s['trades_per_day']:.1f}  |  "
          f"DD max : ${abs(s['max_dd_usd']):.0f} ({s['max_dd_pct']:.1f}%)")
    print(f"  Risque   : moy ${s['avg_sl_usd']:.0f}/trade  |  "
          f"~{s['trades_before_daily_limit']:.1f} trades avant daily limit")
    print(f"  PnL/jour : ${avg_day:+.0f}  (objectif: +${TARGET_DAILY_PNL:.0f})")
    print(f"  Compte   : ${ACCOUNT_SIZE:,.0f} → ${fa:,.0f}  ({pnl:+,.0f}$)")
    print(f"  Apex     : DD dépassée={'OUI ⛔' if s.get('apex_halted') else 'NON ✅'}  |  "
          f"Jours daily limit : {s.get('n_daily_limit', 0)}")


# ─────────────────────────────────────────────────────────────
# 15. MODE 5M — BACKTEST DIRECT NQ=F 5 MINUTES
# ─────────────────────────────────────────────────────────────

PARAM_GRID_5M = {
    "body_pct":      [0.15, 0.20, 0.25, 0.30],
    "rr_ratio":      [1.5, 2.0, 2.5, 3.0],
    "max_wait_bars": [2, 3, 5],
}


def backtest_5m(df: pd.DataFrame, conv: float = 1.0,
                rr_ratio: float = 2.0, body_pct: float = 0.25,
                max_wait_bars: int = 3,
                contracts: int = 1) -> dict:
    """
    Backtest 5M avec sortie forcée à 15h50 ET.
    """
    df = detect_exhaustion(df, conv=conv)
    df = df.dropna(subset=['bias']).copy()
    n = len(df)

    frais_rt = FRAIS_RT * contracts

    account = ACCOUNT_SIZE
    peak_acct = ACCOUNT_SIZE
    halted = False

    daily_pnl = 0.0
    daily_stopped = False
    current_date = None
    n_daily_limit = 0

    state = IDLE
    bias = 0
    sl_pts = 0.0
    wait_count = 0
    indc_high = 0.0
    indc_low = 0.0
    entry_price = 0.0
    sl_price = 0.0
    tp_price = 0.0
    entry_time = None
    entry_bar = 0

    trades = []
    equity_curve = []
    daily_stats = []

    try:
        idx = df.index
        if idx.tz is None:
            idx_et = idx.tz_localize('UTC').tz_convert('America/New_York')
        else:
            idx_et = idx.tz_convert('America/New_York')
    except Exception:
        idx_et = df.index

    for i in range(n):
        row = df.iloc[i]
        timestamp = df.index[i]

        try:
            bar_et = idx_et[i]
            bar_date = bar_et.date()
            bar_hour = bar_et.hour
            bar_minute = bar_et.minute
        except Exception:
            bar_date = timestamp.date()
            bar_hour = 15
            bar_minute = 59

        # Changement de journée
        if bar_date != current_date:
            if current_date is not None:
                if daily_stopped:
                    n_daily_limit += 1
                daily_stats.append({
                    'date': current_date,
                    'pnl': daily_pnl,
                    'stopped': daily_stopped,
                })
            current_date = bar_date
            daily_pnl = 0.0
            daily_stopped = False

        trading_allowed = (not halted) and (not daily_stopped)

        # Force close à 15h50 ET (10 min avant fermeture)
        force_close_time = (bar_hour == 15 and bar_minute >= 50) or bar_hour >= 16

        # Dernière barre de la session
        next_is_new_day = False
        if i + 1 < n:
            try:
                next_is_new_day = (idx_et[i + 1].date() != bar_date)
            except Exception:
                next_is_new_day = (df.index[i + 1].date() != timestamp.date())
        else:
            next_is_new_day = True

        # ── IN_POSITION ──
        if state == IN_POSITION:
            hit_sl = False
            hit_tp = False
            exit_price = row['close']
            raison = 'En cours'

            if bias == 1:
                if row['low'] <= sl_price:
                    hit_sl = True; exit_price = sl_price
                elif row['high'] >= tp_price:
                    hit_tp = True; exit_price = tp_price
            else:
                if row['high'] >= sl_price:
                    hit_sl = True; exit_price = sl_price
                elif row['low'] <= tp_price:
                    hit_tp = True; exit_price = tp_price

            opp = (bias == 1 and row['signal_valid'] and row['bias'] == -1) or \
                  (bias == -1 and row['signal_valid'] and row['bias'] == 1)
            if not hit_sl and not hit_tp and opp:
                exit_price = row['close']
                raison = 'Signal opposé'

            force_close = (not hit_sl and not hit_tp and
                           raison != 'Signal opposé' and
                           (force_close_time or next_is_new_day))
            if force_close:
                exit_price = row['close']
                raison = '15h50 ET' if force_close_time else 'Fin session'

            if hit_sl or hit_tp or opp or force_close:
                if hit_sl: raison = 'SL'
                elif hit_tp: raison = 'TP'

                if bias == 1:
                    pnl_pts = (exit_price - entry_price) * conv
                else:
                    pnl_pts = (entry_price - exit_price) * conv
                pnl_usd = pnl_pts * POINT_VALUE * contracts - frais_rt
                account += pnl_usd
                daily_pnl += pnl_usd

                if account > peak_acct:
                    peak_acct = account

                dd_from_peak = account - peak_acct
                if dd_from_peak <= MAX_DD_LIMIT:
                    halted = True

                if daily_pnl <= DAILY_LOSS_LIMIT:
                    daily_stopped = True

                trades.append({
                    'entry_time': entry_time,
                    'exit_time': timestamp,
                    'direction': 'LONG' if bias == 1 else 'SHORT',
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'sl_price': sl_price,
                    'tp_price': tp_price,
                    'sl_pts': sl_pts,
                    'sl_usd': sl_pts * POINT_VALUE * contracts,
                    'pnl_pts': pnl_pts,
                    'pnl_usd': pnl_usd,
                    'account': account,
                    'raison': raison,
                    'duree_bars': i - entry_bar,
                    'daily_pnl': daily_pnl,
                    'contracts': contracts,
                })

                state = IDLE
                bias = 0
                wait_count = 0

        # ── WAIT_BREAKOUT ──
        elif state == WAIT_BREAKOUT and trading_allowed and not force_close_time:
            entered = False

            if bias == -1 and row['close'] < indc_low:
                entry_price = row['close']
                sl_price = entry_price + (sl_pts / conv)
                tp_price = entry_price - (sl_pts * rr_ratio / conv)
                entered = True
            elif bias == 1 and row['close'] > indc_high:
                entry_price = row['close']
                sl_price = entry_price - (sl_pts / conv)
                tp_price = entry_price + (sl_pts * rr_ratio / conv)
                entered = True

            if entered:
                entry_time = timestamp
                entry_bar = i
                state = IN_POSITION
                wait_count = 0
            else:
                wait_count += 1
                if wait_count > max_wait_bars or next_is_new_day or force_close_time:
                    state = IDLE; bias = 0; wait_count = 0

        # ── WAIT_INDECISION ──
        elif state == WAIT_INDECISION and trading_allowed and not force_close_time:
            if is_indecision(row, body_pct):
                indc_high = row['high']
                indc_low = row['low']
                state = WAIT_BREAKOUT
                wait_count = 0
            else:
                wait_count += 1
                if wait_count > max_wait_bars or next_is_new_day or force_close_time:
                    state = IDLE; bias = 0; wait_count = 0

        # ── IDLE ──
        if state == IDLE and trading_allowed and not force_close_time:
            if row['signal_valid']:
                bias = int(row['bias'])
                sl_pts = float(row['signal_pts'])
                state = WAIT_INDECISION
                wait_count = 0

        equity_curve.append({'time': timestamp, 'account': account})

    # Fermer position ouverte en fin de données
    if state == IN_POSITION:
        exit_price = df.iloc[-1]['close']
        if bias == 1:
            pnl_pts = (exit_price - entry_price) * conv
        else:
            pnl_pts = (entry_price - exit_price) * conv
        pnl_usd = pnl_pts * POINT_VALUE * contracts - frais_rt / 2
        account += pnl_usd
        trades.append({
            'entry_time': entry_time, 'exit_time': df.index[-1],
            'direction': 'LONG' if bias == 1 else 'SHORT',
            'entry_price': entry_price, 'exit_price': exit_price,
            'sl_price': sl_price, 'tp_price': tp_price,
            'sl_pts': sl_pts, 'sl_usd': sl_pts * POINT_VALUE * contracts,
            'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd,
            'account': account, 'raison': 'Fin données',
            'duree_bars': n - entry_bar, 'daily_pnl': daily_pnl,
            'contracts': contracts,
        })

    if current_date is not None:
        if daily_stopped:
            n_daily_limit += 1
        daily_stats.append({'date': current_date, 'pnl': daily_pnl,
                            'stopped': daily_stopped})

    eq_df = (pd.DataFrame(equity_curve).set_index('time')
             if equity_curve else pd.DataFrame())
    stats = compute_stats(trades, equity_curve, daily_stats, halted, account,
                          contracts=contracts)
    stats['params'] = {
        'rr_ratio': rr_ratio, 'body_pct': body_pct,
        'max_wait_bars': max_wait_bars, 'contracts': contracts,
    }
    stats['n_daily_limit'] = n_daily_limit

    return {
        'trades': trades,
        'equity_curve': eq_df,
        'daily_stats': daily_stats,
        'stats': stats,
        'halted': halted,
    }


def optimize_5m(df: pd.DataFrame, conv: float = 1.0,
                contracts: int = 1) -> dict:
    """
    Grid search 5M.
    Score = profit_factor × win_rate_norm × freq_bonus
    freq_bonus = 1.2 si 4-8 signaux/j, 1.0 si 2-4, 0.7 si <2 ou >10
    """
    keys = list(PARAM_GRID_5M.keys())
    combos = list(product(*PARAM_GRID_5M.values()))
    total = len(combos)
    print(f"\n🔍 Optimisation 5M ({contracts}c)... ({total} combinaisons)")

    results = []
    for idx, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        try:
            res = backtest_5m(df, conv=conv, contracts=contracts, **params)
            s = res['stats']
            if s['n_trades'] < 3:
                continue

            tpd = s['trades_per_day']
            if 4 <= tpd <= 8:
                freq_bonus = 1.2
            elif 2 <= tpd < 4:
                freq_bonus = 1.0
            elif tpd > 10:
                freq_bonus = 0.7
            else:
                freq_bonus = 0.7

            pf = max(s['profit_factor'], 0)
            wr_norm = s['win_rate'] / 100
            score = pf * wr_norm * freq_bonus

            results.append({**params, **s, 'score': score,
                             'freq_bonus': freq_bonus})
        except Exception:
            continue

        if (idx + 1) % 12 == 0 or idx + 1 == total:
            print(f"  ... {idx+1}/{total} ({(idx+1)/total*100:.0f}%)")

    if not results:
        print("  ⚠️  Aucun résultat valide.")
        return {'best_params': {}, 'best_score': 0, 'ranking': pd.DataFrame()}

    ranking = (pd.DataFrame(results)
               .sort_values('score', ascending=False)
               .reset_index(drop=True))
    best = ranking.iloc[0]
    best_params = {k: best[k] for k in keys}

    print(f"\n✅ Meilleurs paramètres 5M ({contracts}c) :")
    print(f"   body_pct={best_params['body_pct']}, "
          f"rr_ratio={best_params['rr_ratio']}, "
          f"max_wait_bars={int(best_params['max_wait_bars'])}")
    print(f"   Win Rate: {best['win_rate']:.1f}% | "
          f"PF: {best['profit_factor']:.2f} | "
          f"Trades/jour: {best['trades_per_day']:.1f}")

    return {'best_params': best_params,
            'best_score': float(best['score']),
            'ranking': ranking}


def generate_5m_report(bt1: dict, bt2: dict,
                       output_path: str = "trading/nasdaq_5m_report.png"):
    """
    Rapport 4 panneaux (thème sombre) :
    1. Equity curve (1c bleu, 2c orange — même graphe)
    2. PnL journalier (barres)
    3. Distribution des trades (wins verts / losses rouges)
    4. Tableau stats comparatif 1c vs 2c
    """
    fig = plt.figure(figsize=(20, 14))
    fig.patch.set_facecolor('#0d1117')
    gs = gridspec.GridSpec(2, 2, figure=fig, hspace=0.42, wspace=0.35)

    C = dict(
        bg='#0d1117', panel='#161b22', text='#e6edf3',
        green='#3fb950', red='#f85149', blue='#58a6ff',
        gold='#d29922', grey='#8b949e', orange='#f0883e',
        purple='#bc8cff', cyan='#39d353',
    )

    def style_ax(ax, title):
        ax.set_facecolor(C['panel'])
        ax.tick_params(colors=C['text'], labelsize=9)
        for spine in ax.spines.values():
            spine.set_color('#30363d')
        ax.xaxis.label.set_color(C['text'])
        ax.yaxis.label.set_color(C['text'])
        ax.set_title(title, fontsize=11, fontweight='bold',
                     pad=10, color=C['text'])

    s1 = bt1['stats']
    s2 = bt2['stats']

    # ── Panneau 1 : Equity Curve (1c bleu + 2c orange) ──────
    ax1 = fig.add_subplot(gs[0, 0])
    style_ax(ax1, '📈 Equity Curve — 1 Contrat (bleu) vs 2 Contrats (orange)')

    eq1 = bt1.get('equity_curve', pd.DataFrame())
    eq2 = bt2.get('equity_curve', pd.DataFrame())

    if not eq1.empty and 'account' in eq1.columns:
        ax1.plot(eq1.index, eq1['account'], color=C['blue'],
                 linewidth=1.5, label='1 Contrat', alpha=0.9)
    if not eq2.empty and 'account' in eq2.columns:
        ax1.plot(eq2.index, eq2['account'], color=C['orange'],
                 linewidth=1.5, label='2 Contrats', alpha=0.9)

    ax1.axhline(y=ACCOUNT_SIZE, color=C['grey'], linestyle=':',
                linewidth=1, label='100k$ initial')
    ax1.axhline(y=ACCOUNT_SIZE + MAX_DD_LIMIT, color=C['red'],
                linestyle='--', linewidth=1,
                label=f'Max DD -8% ({ACCOUNT_SIZE + MAX_DD_LIMIT:,.0f}$)')
    ax1.set_ylabel('Compte (USD)', color=C['text'])
    ax1.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # ── Panneau 2 : PnL journalier ───────────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    style_ax(ax2, '📊 PnL Journalier (1 Contrat)')

    ds = bt1.get('daily_stats', [])
    if ds:
        pnls = [d['pnl'] for d in ds]
        bar_colors = [C['green'] if p >= 0 else C['red'] for p in pnls]
        ax2.bar(range(len(pnls)), pnls, color=bar_colors, alpha=0.8, width=0.8)
        ax2.axhline(y=0, color=C['grey'], linewidth=0.8)
        ax2.axhline(y=TARGET_DAILY_PNL, color=C['cyan'], linestyle='--',
                    linewidth=1, alpha=0.7, label=f'Objectif +${TARGET_DAILY_PNL:.0f}/j')
        ax2.axhline(y=DAILY_LOSS_LIMIT, color=C['orange'], linestyle=':',
                    linewidth=1, label=f'Daily limit ${DAILY_LOSS_LIMIT:.0f}')
        ax2.set_ylabel('PnL ($)', color=C['text'])
        ax2.set_xlabel('Jours de trading', color=C['text'])
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
        ax2.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)
    else:
        ax2.text(0.5, 0.5, 'Aucun trade', ha='center', va='center',
                 color=C['text'], transform=ax2.transAxes)

    # ── Panneau 3 : Distribution des trades ─────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    style_ax(ax3, '🎯 Distribution des Trades — Wins (vert) / Losses (rouge)')

    trades1 = bt1.get('trades', [])
    if trades1:
        pnls_all = [t['pnl_usd'] for t in trades1]
        wins = [p for p in pnls_all if p > 0]
        losses = [p for p in pnls_all if p <= 0]
        bins = min(30, max(8, len(pnls_all) // 3))

        if losses:
            ax3.hist(losses, bins=max(4, bins // 2), color=C['red'],
                     alpha=0.75, label=f'Pertes ({len(losses)})')
        if wins:
            ax3.hist(wins, bins=max(4, bins // 2), color=C['green'],
                     alpha=0.75, label=f'Gains ({len(wins)})')

        ax3.axvline(x=0, color='white', linestyle='--', linewidth=1)
        if pnls_all:
            ax3.axvline(x=float(np.mean(pnls_all)), color=C['gold'], linewidth=1.5,
                        label=f"Moy: ${float(np.mean(pnls_all)):.0f}")
        ax3.set_xlabel('PnL par trade ($)', color=C['text'])
        ax3.set_ylabel('Fréquence', color=C['text'])
        ax3.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)
    else:
        ax3.text(0.5, 0.5, 'Aucun trade', ha='center', va='center',
                 color=C['text'], transform=ax3.transAxes)

    # ── Panneau 4 : Tableau comparatif 1c vs 2c ─────────────
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.set_facecolor(C['panel'])
    ax4.axis('off')
    ax4.set_title('📋 Stats Comparatif — 1 Contrat vs 2 Contrats', fontsize=11,
                  fontweight='bold', pad=10, color=C['text'])

    p1 = s1.get('params', {})
    p2 = s2.get('params', {})
    pnl1 = s1.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    pnl2 = s2.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    avg1 = s1.get('avg_pnl_per_day', 0)
    avg2 = s2.get('avg_pnl_per_day', 0)

    table_rows = [
        ('INDICATEUR',           '1 CONTRAT',
                                 '2 CONTRATS',          True),
        ('Win Rate',             f"{s1['win_rate']:.1f}%",
                                 f"{s2['win_rate']:.1f}%", False),
        ('Profit Factor',        f"{s1['profit_factor']:.2f}",
                                 f"{s2['profit_factor']:.2f}", False),
        ('Sharpe',               f"{s1['sharpe']:.2f}",
                                 f"{s2['sharpe']:.2f}",   False),
        ('P&L Total',            f"${pnl1:+,.0f}",
                                 f"${pnl2:+,.0f}",        False),
        ('P&L Moy/Jour',         f"${avg1:+.0f}",
                                 f"${avg2:+.0f}",         False),
        ('Max Drawdown',         f"-${abs(s1.get('max_dd_usd', 0)):,.0f}",
                                 f"-${abs(s2.get('max_dd_usd', 0)):,.0f}", False),
        ('Jours daily limit',    str(s1.get('n_daily_limit', 0)),
                                 str(s2.get('n_daily_limit', 0)), False),
        ('Signaux/jour',         f"{s1['trades_per_day']:.1f}",
                                 f"{s2['trades_per_day']:.1f}", False),
        ('Risque moy/trade',     f"${s1.get('avg_risk_usd', 0):.0f}",
                                 f"${s2.get('avg_risk_usd', 0):.0f}", False),
        ('Nb trades',            str(s1['n_trades']),
                                 str(s2['n_trades']),     False),
        ('── Paramètres ──',     '',                      '',     True),
        ('body_pct',             str(p1.get('body_pct', '-')),
                                 str(p2.get('body_pct', '-')), False),
        ('rr_ratio',             str(p1.get('rr_ratio', '-')),
                                 str(p2.get('rr_ratio', '-')), False),
        ('max_wait',             str(int(p1.get('max_wait_bars', 0))),
                                 str(int(p2.get('max_wait_bars', 0))), False),
    ]

    y = 0.97
    line_h = 0.060
    for label, v1, v2, is_header in table_rows:
        if is_header:
            ax4.text(0.03, y, label, transform=ax4.transAxes,
                     color=C['blue'], fontsize=8.5, va='top', fontweight='bold')
            ax4.text(0.48, y, v1, transform=ax4.transAxes,
                     color=C['blue'], fontsize=8.5, va='top', fontweight='bold')
            ax4.text(0.74, y, v2, transform=ax4.transAxes,
                     color=C['orange'], fontsize=8.5, va='top', fontweight='bold')
        else:
            c1 = (C['green'] if pnl1 >= 0 else C['red']) if label == 'P&L Total' else \
                 (C['green'] if avg1 >= TARGET_DAILY_PNL else C['orange']) if label == 'P&L Moy/Jour' else \
                 C['text']
            c2 = (C['green'] if pnl2 >= 0 else C['red']) if label == 'P&L Total' else \
                 (C['green'] if avg2 >= TARGET_DAILY_PNL else C['orange']) if label == 'P&L Moy/Jour' else \
                 C['text']
            ax4.text(0.03, y, label, transform=ax4.transAxes,
                     color=C['grey'], fontsize=8.5, va='top')
            ax4.text(0.48, y, v1, transform=ax4.transAxes,
                     color=c1, fontsize=8.5, va='top', fontweight='bold')
            ax4.text(0.74, y, v2, transform=ax4.transAxes,
                     color=c2, fontsize=8.5, va='top', fontweight='bold')
        y -= line_h

    fig.suptitle(
        f'NQ Futures 5M — 1 vs 2 Contrats  |  Apex 100k$  |  '
        f'SL {SL_MIN_PTS}–{SL_MAX_PTS}pts  |  RTH 9h30–16h00 ET  |  '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=12, fontweight='bold', color=C['text'], y=0.988,
    )

    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=C['bg'], edgecolor='none')
    plt.close()
    print(f"\n✅ Rapport 5M sauvegardé : {output_path}")


def run_5m():
    """
    Mode 5M : télécharge NQ=F 5M, filtre RTH, optimise, compare 1c vs 2c.
    """
    print("=" * 60)
    print("NQ FUTURES 5M — BACKTEST (Apex 100k$)")
    print("=" * 60)

    # Télécharger NQ=F 5M (60j max yfinance)
    print("\n📥 Téléchargement NQ=F 5M (60j)...")
    try:
        df_raw, conv = download_data(interval='5m', period='60d')
    except Exception as e:
        print(f"❌ Erreur téléchargement : {e}")
        return

    # Filtre RTH 9h30–16h00 ET
    df = filter_rth(df_raw)

    if len(df) < 50:
        print("❌ Données insuffisantes après filtre RTH.")
        return

    # Nombre de jours de trading
    try:
        idx = df.index
        if idx.tz is None:
            idx_et = idx.tz_localize('UTC').tz_convert('America/New_York')
        else:
            idx_et = idx.tz_convert('America/New_York')
        n_days_trading = len(set(t.date() for t in idx_et))
    except Exception:
        n_days_trading = max(1, len(df) // 78)

    print(f"\nDonnées : NQ=F 5M | {n_days_trading} jours | {len(df)} barres | Heures RTH")

    # Diagnostic signaux bruts
    df_diag = detect_exhaustion(df, conv=conv)
    n_raw = int((df_diag['bias'] != 0).sum())
    n_filtered = int(df_diag['signal_valid'].sum())
    sigs_per_day = n_filtered / max(1, n_days_trading)
    print(f"Signaux bruts détectés : {n_raw}")
    print(f"Signaux filtrés (SL {SL_MIN_PTS}-{SL_MAX_PTS} pts) : {n_filtered} (~{sigs_per_day:.1f}/jour)")

    # Optimisation 1 contrat
    print("\n" + "─" * 40)
    print("  OPTIMISATION — 1 CONTRAT")
    print("─" * 40)
    opt1 = optimize_5m(df, conv=conv, contracts=1)
    best1 = opt1.get('best_params') or {
        'rr_ratio': 2.0, 'body_pct': 0.25, 'max_wait_bars': 3}

    # Optimisation 2 contrats
    print("\n" + "─" * 40)
    print("  OPTIMISATION — 2 CONTRATS")
    print("─" * 40)
    opt2 = optimize_5m(df, conv=conv, contracts=2)
    best2 = opt2.get('best_params') or {
        'rr_ratio': 2.0, 'body_pct': 0.25, 'max_wait_bars': 3}

    # Backtest final avec meilleurs paramètres
    print("\n🔄 Backtest final — 1 contrat...")
    bt1 = backtest_5m(df, conv=conv, contracts=1, **best1)

    print("🔄 Backtest final — 2 contrats...")
    bt2 = backtest_5m(df, conv=conv, contracts=2, **best2)

    s1 = bt1['stats']
    s2 = bt2['stats']

    # Générer rapport PNG
    generate_5m_report(bt1, bt2, output_path="trading/nasdaq_5m_report.png")

    # Verdict
    def verdict(s):
        avg = s.get('avg_pnl_per_day', 0)
        if avg >= TARGET_DAILY_PNL:
            return "OBJECTIF ATTEINT"
        elif avg >= TARGET_DAILY_PNL * 0.5:
            return "VIABLE"
        else:
            return "INSUFFISANT"

    pnl1 = s1.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    pnl2 = s2.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    avg1 = s1.get('avg_pnl_per_day', 0)
    avg2 = s2.get('avg_pnl_per_day', 0)

    # ── Sortie console requise ───────────────────────────────
    print()
    print("=" * 60)
    print("NQ FUTURES 5M — RÉSULTATS (Apex 100k$)")
    print("=" * 60)
    print(f"Données : NQ=F 5M | {n_days_trading} jours | {len(df)} barres | Heures RTH")
    print(f"Signaux bruts détectés : {n_raw}")
    print(f"Signaux filtrés (SL {SL_MIN_PTS}-{SL_MAX_PTS} pts) : {n_filtered} (~{sigs_per_day:.1f}/jour)")
    print()
    print(f"{'':24s}  {'1 CONTRAT':>12s}  {'2 CONTRATS':>12s}")
    print("-" * 54)
    print(f"{'Win Rate':24s}  {s1['win_rate']:>11.1f}%  {s2['win_rate']:>11.1f}%")
    print(f"{'Profit Factor':24s}  {s1['profit_factor']:>12.2f}  {s2['profit_factor']:>12.2f}")
    print(f"{'Sharpe':24s}  {s1['sharpe']:>12.2f}  {s2['sharpe']:>12.2f}")
    print(f"{'P&L Total':24s}  {pnl1:>+11,.0f}$  {pnl2:>+11,.0f}$")
    print(f"{'P&L Moyen/Jour':24s}  {avg1:>+11,.0f}$  {avg2:>+11,.0f}$"
          f"  ← objectif: {TARGET_DAILY_PNL:.0f}$/j")
    print(f"{'Max Drawdown':24s}  {-abs(s1.get('max_dd_usd', 0)):>+11,.0f}$"
          f"  {-abs(s2.get('max_dd_usd', 0)):>+11,.0f}$")
    print(f"{'Jours daily limit':24s}  {s1.get('n_daily_limit', 0):>12d}"
          f"  {s2.get('n_daily_limit', 0):>12d}")
    print(f"{'Signaux/jour':24s}  {s1['trades_per_day']:>12.1f}  {s2['trades_per_day']:>12.1f}")
    print(f"{'Risque moy/trade':24s}  {s1.get('avg_risk_usd', 0):>11,.0f}$"
          f"  {s2.get('avg_risk_usd', 0):>11,.0f}$")
    print()
    print(f"Best params (1c) : body_pct={best1['body_pct']}, "
          f"rr={best1['rr_ratio']}, max_wait={int(best1['max_wait_bars'])}")
    print(f"Best params (2c) : body_pct={best2['body_pct']}, "
          f"rr={best2['rr_ratio']}, max_wait={int(best2['max_wait_bars'])}")
    print()
    print("VERDICT :")
    print(f"1 contrat  → {verdict(s1)}")
    print(f"2 contrats → {verdict(s2)}")
    print("=" * 60)


# ─────────────────────────────────────────────────────────────
# 16. MODE 5M_ADX — FILTRE RÉGIME DE MARCHÉ ADX
# ─────────────────────────────────────────────────────────────

PARAM_GRID_5M_ADX = {
    "body_pct":      [0.15, 0.20, 0.25, 0.30],
    "rr_ratio":      [1.5, 2.0, 2.5, 3.0],
    "max_wait_bars": [2, 3, 5],
    "adx_threshold": [20, 25, 30, 35],
}


def backtest_5m_adx(df: pd.DataFrame, conv: float = 1.0,
                    rr_ratio: float = 2.0, body_pct: float = 0.25,
                    max_wait_bars: int = 3,
                    adx_threshold: float = 25,
                    contracts: int = 1) -> dict:
    """
    Backtest 5M avec filtre ADX.
    Signal accepté seulement si ADX(14) < adx_threshold
    (marché en range → retournement probable).
    """
    # Calcul ADX avant exhaustion
    adx_series = compute_adx(df, period=14)

    df = detect_exhaustion(df, conv=conv)
    df = df.dropna(subset=['bias']).copy()

    # Aligner l'ADX sur le DataFrame filtré
    adx_aligned = adx_series.reindex(df.index).fillna(0)
    df['adx'] = adx_aligned.values

    n = len(df)
    frais_rt = FRAIS_RT * contracts

    account   = ACCOUNT_SIZE
    peak_acct = ACCOUNT_SIZE
    halted    = False

    daily_pnl     = 0.0
    daily_stopped = False
    current_date  = None
    n_daily_limit = 0

    state      = IDLE
    bias       = 0
    sl_pts     = 0.0
    wait_count = 0
    indc_high  = 0.0
    indc_low   = 0.0
    entry_price = 0.0
    sl_price    = 0.0
    tp_price    = 0.0
    entry_time  = None
    entry_bar   = 0

    trades         = []
    equity_curve   = []
    daily_stats    = []
    adx_at_signal  = []   # ADX quand signal validé (pour stats)
    n_raw_signals  = 0    # Signaux avant filtre ADX
    n_adx_filtered = 0    # Signaux rejetés par ADX

    try:
        idx = df.index
        if idx.tz is None:
            idx_et = idx.tz_localize('UTC').tz_convert('America/New_York')
        else:
            idx_et = idx.tz_convert('America/New_York')
    except Exception:
        idx_et = df.index

    for i in range(n):
        row       = df.iloc[i]
        timestamp = df.index[i]
        adx_val   = float(df['adx'].iloc[i])

        try:
            bar_et     = idx_et[i]
            bar_date   = bar_et.date()
            bar_hour   = bar_et.hour
            bar_minute = bar_et.minute
        except Exception:
            bar_date   = timestamp.date()
            bar_hour   = 15
            bar_minute = 59

        if bar_date != current_date:
            if current_date is not None:
                if daily_stopped:
                    n_daily_limit += 1
                daily_stats.append({
                    'date':    current_date,
                    'pnl':     daily_pnl,
                    'stopped': daily_stopped,
                })
            current_date  = bar_date
            daily_pnl     = 0.0
            daily_stopped = False

        trading_allowed   = (not halted) and (not daily_stopped)
        force_close_time  = (bar_hour == 15 and bar_minute >= 50) or bar_hour >= 16

        next_is_new_day = False
        if i + 1 < n:
            try:
                next_is_new_day = (idx_et[i + 1].date() != bar_date)
            except Exception:
                next_is_new_day = (df.index[i + 1].date() != timestamp.date())
        else:
            next_is_new_day = True

        # ── IN_POSITION ──
        if state == IN_POSITION:
            hit_sl = False
            hit_tp = False
            exit_price = row['close']
            raison     = 'En cours'

            if bias == 1:
                if row['low']  <= sl_price:
                    hit_sl = True; exit_price = sl_price
                elif row['high'] >= tp_price:
                    hit_tp = True; exit_price = tp_price
            else:
                if row['high'] >= sl_price:
                    hit_sl = True; exit_price = sl_price
                elif row['low']  <= tp_price:
                    hit_tp = True; exit_price = tp_price

            opp = (bias == 1  and row['signal_valid'] and row['bias'] == -1) or \
                  (bias == -1 and row['signal_valid'] and row['bias'] == 1)
            if not hit_sl and not hit_tp and opp:
                exit_price = row['close']
                raison     = 'Signal opposé'

            force_close = (not hit_sl and not hit_tp and
                           raison != 'Signal opposé' and
                           (force_close_time or next_is_new_day))
            if force_close:
                exit_price = row['close']
                raison     = '15h50 ET' if force_close_time else 'Fin session'

            if hit_sl or hit_tp or opp or force_close:
                if hit_sl:  raison = 'SL'
                elif hit_tp: raison = 'TP'

                if bias == 1:
                    pnl_pts = (exit_price - entry_price) * conv
                else:
                    pnl_pts = (entry_price - exit_price) * conv
                pnl_usd    = pnl_pts * POINT_VALUE * contracts - frais_rt
                account   += pnl_usd
                daily_pnl += pnl_usd

                if account > peak_acct:
                    peak_acct = account
                if account - peak_acct <= MAX_DD_LIMIT:
                    halted = True
                if daily_pnl <= DAILY_LOSS_LIMIT:
                    daily_stopped = True

                trades.append({
                    'entry_time':  entry_time,
                    'exit_time':   timestamp,
                    'direction':   'LONG' if bias == 1 else 'SHORT',
                    'entry_price': entry_price,
                    'exit_price':  exit_price,
                    'sl_price':    sl_price,
                    'tp_price':    tp_price,
                    'sl_pts':      sl_pts,
                    'sl_usd':      sl_pts * POINT_VALUE * contracts,
                    'pnl_pts':     pnl_pts,
                    'pnl_usd':     pnl_usd,
                    'account':     account,
                    'raison':      raison,
                    'duree_bars':  i - entry_bar,
                    'daily_pnl':   daily_pnl,
                    'contracts':   contracts,
                    'adx_entry':   adx_val,
                })

                state      = IDLE
                bias       = 0
                wait_count = 0

        # ── WAIT_BREAKOUT ──
        elif state == WAIT_BREAKOUT and trading_allowed and not force_close_time:
            entered = False

            if bias == -1 and row['close'] < indc_low:
                entry_price = row['close']
                sl_price    = entry_price + (sl_pts / conv)
                tp_price    = entry_price - (sl_pts * rr_ratio / conv)
                entered     = True
            elif bias == 1 and row['close'] > indc_high:
                entry_price = row['close']
                sl_price    = entry_price - (sl_pts / conv)
                tp_price    = entry_price + (sl_pts * rr_ratio / conv)
                entered     = True

            if entered:
                entry_time = timestamp
                entry_bar  = i
                state      = IN_POSITION
                wait_count = 0
            else:
                wait_count += 1
                if wait_count > max_wait_bars or next_is_new_day or force_close_time:
                    state = IDLE; bias = 0; wait_count = 0

        # ── WAIT_INDECISION ──
        elif state == WAIT_INDECISION and trading_allowed and not force_close_time:
            if is_indecision(row, body_pct):
                indc_high  = row['high']
                indc_low   = row['low']
                state      = WAIT_BREAKOUT
                wait_count = 0
            else:
                wait_count += 1
                if wait_count > max_wait_bars or next_is_new_day or force_close_time:
                    state = IDLE; bias = 0; wait_count = 0

        # ── IDLE — chercher épuisement + filtre ADX ──
        if state == IDLE and trading_allowed and not force_close_time:
            if row['signal_valid']:
                n_raw_signals += 1
                if adx_val < adx_threshold:
                    # Marché en range → signal valide
                    adx_at_signal.append(adx_val)
                    bias       = int(row['bias'])
                    sl_pts     = float(row['signal_pts'])
                    state      = WAIT_INDECISION
                    wait_count = 0
                else:
                    # Tendance trop forte → signal ignoré
                    n_adx_filtered += 1

        equity_curve.append({
            'time':    timestamp,
            'account': account,
            'adx':     adx_val,
            'in_range': adx_val < adx_threshold,
        })

    # Fermer position ouverte en fin de données
    if state == IN_POSITION:
        exit_price = df.iloc[-1]['close']
        if bias == 1:
            pnl_pts = (exit_price - entry_price) * conv
        else:
            pnl_pts = (entry_price - exit_price) * conv
        pnl_usd = pnl_pts * POINT_VALUE * contracts - frais_rt / 2
        account += pnl_usd
        trades.append({
            'entry_time': entry_time, 'exit_time': df.index[-1],
            'direction':  'LONG' if bias == 1 else 'SHORT',
            'entry_price': entry_price, 'exit_price': exit_price,
            'sl_price': sl_price, 'tp_price': tp_price,
            'sl_pts': sl_pts, 'sl_usd': sl_pts * POINT_VALUE * contracts,
            'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd,
            'account': account, 'raison': 'Fin données',
            'duree_bars': n - entry_bar, 'daily_pnl': daily_pnl,
            'contracts': contracts, 'adx_entry': 0.0,
        })

    if current_date is not None:
        if daily_stopped:
            n_daily_limit += 1
        daily_stats.append({'date': current_date, 'pnl': daily_pnl,
                            'stopped': daily_stopped})

    eq_df  = (pd.DataFrame(equity_curve).set_index('time')
              if equity_curve else pd.DataFrame())
    stats  = compute_stats(trades, equity_curve, daily_stats, halted, account,
                           contracts=contracts)
    stats['params'] = {
        'rr_ratio': rr_ratio, 'body_pct': body_pct,
        'max_wait_bars': max_wait_bars, 'adx_threshold': adx_threshold,
        'contracts': contracts,
    }
    stats['n_daily_limit']   = n_daily_limit
    stats['n_raw_signals']   = n_raw_signals
    stats['n_adx_filtered']  = n_adx_filtered
    stats['adx_at_signal']   = adx_at_signal

    return {
        'trades':       trades,
        'equity_curve': eq_df,
        'daily_stats':  daily_stats,
        'stats':        stats,
        'halted':       halted,
        'adx_series':   adx_series,
    }


def optimize_5m_adx(df: pd.DataFrame, conv: float = 1.0,
                    contracts: int = 1) -> dict:
    """
    Grid search 5M avec filtre ADX.
    Score = profit_factor × win_rate_norm × freq_bonus
    freq_bonus = 1.2 si 4-8/j, 1.0 si 2-4/j, 0.7 si <2/j
    """
    keys   = list(PARAM_GRID_5M_ADX.keys())
    combos = list(product(*PARAM_GRID_5M_ADX.values()))
    total  = len(combos)
    print(f"\n🔍 Optimisation 5M+ADX ({contracts}c)... ({total} combinaisons)")

    results = []
    for idx, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        try:
            res = backtest_5m_adx(df, conv=conv, contracts=contracts, **params)
            s   = res['stats']
            if s['n_trades'] < 3:
                continue

            tpd = s['trades_per_day']
            if 4 <= tpd <= 8:
                freq_bonus = 1.2
            elif 2 <= tpd < 4:
                freq_bonus = 1.0
            else:
                freq_bonus = 0.7

            pf      = max(s['profit_factor'], 0)
            wr_norm = s['win_rate'] / 100
            score   = pf * wr_norm * freq_bonus

            results.append({**params, **s, 'score': score, 'freq_bonus': freq_bonus})
        except Exception:
            continue

        if (idx + 1) % 16 == 0 or idx + 1 == total:
            print(f"  ... {idx+1}/{total} ({(idx+1)/total*100:.0f}%)")

    if not results:
        print("  ⚠️  Aucun résultat valide.")
        return {'best_params': {}, 'best_score': 0, 'ranking': pd.DataFrame()}

    ranking = (pd.DataFrame(results)
               .sort_values('score', ascending=False)
               .reset_index(drop=True))
    best        = ranking.iloc[0]
    best_params = {k: best[k] for k in keys}

    print(f"\n✅ Meilleurs paramètres 5M+ADX ({contracts}c) :")
    print(f"   body_pct={best_params['body_pct']}, rr_ratio={best_params['rr_ratio']}, "
          f"max_wait_bars={int(best_params['max_wait_bars'])}, "
          f"adx_threshold={int(best_params['adx_threshold'])}")
    print(f"   Win Rate: {best['win_rate']:.1f}% | PF: {best['profit_factor']:.2f} | "
          f"Trades/jour: {best['trades_per_day']:.1f}")

    return {'best_params': best_params,
            'best_score':  float(best['score']),
            'ranking':     ranking}


def generate_5m_adx_report(bt1: dict, bt2: dict, adx_series: pd.Series,
                            adx_threshold: float = 25,
                            output_path: str = "trading/nasdaq_5m_adx_report.png"):
    """
    Rapport 5 panneaux :
    1. Equity curve 1c (bleu) vs 2c (orange)
    2. ADX au fil du temps avec zones colorées + signaux
    3. PnL journalier (barres)
    4. Distribution wins/losses
    5. Tableau stats comparatif + meilleurs params ADX
    """
    fig = plt.figure(figsize=(22, 20))
    fig.patch.set_facecolor('#0d1117')
    gs  = gridspec.GridSpec(3, 2, figure=fig,
                            hspace=0.48, wspace=0.32,
                            height_ratios=[1.4, 1.4, 1.6])

    C = dict(
        bg='#0d1117', panel='#161b22', text='#e6edf3',
        green='#3fb950', red='#f85149', blue='#58a6ff',
        gold='#d29922', grey='#8b949e', orange='#f0883e',
        purple='#bc8cff', cyan='#39d353',
    )

    def style_ax(ax, title):
        ax.set_facecolor(C['panel'])
        ax.tick_params(colors=C['text'], labelsize=8)
        for spine in ax.spines.values():
            spine.set_color('#30363d')
        ax.xaxis.label.set_color(C['text'])
        ax.yaxis.label.set_color(C['text'])
        ax.set_title(title, fontsize=10, fontweight='bold', pad=8, color=C['text'])

    s1 = bt1['stats']
    s2 = bt2['stats']
    eq1 = bt1.get('equity_curve', pd.DataFrame())
    eq2 = bt2.get('equity_curve', pd.DataFrame())
    trades1 = bt1.get('trades', [])
    trades2 = bt2.get('trades', [])

    # ── Panneau 1 : Equity Curve ─────────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    style_ax(ax1, '📈 Equity Curve — 1c (bleu) vs 2c (orange)')

    if not eq1.empty and 'account' in eq1.columns:
        ax1.plot(eq1.index, eq1['account'], color=C['blue'],
                 linewidth=1.5, label='1 Contrat', alpha=0.9)
    if not eq2.empty and 'account' in eq2.columns:
        ax1.plot(eq2.index, eq2['account'], color=C['orange'],
                 linewidth=1.5, label='2 Contrats', alpha=0.9)

    ax1.axhline(y=ACCOUNT_SIZE, color=C['grey'], linestyle=':', linewidth=1,
                label=f'100k$ initial')
    ax1.axhline(y=ACCOUNT_SIZE + MAX_DD_LIMIT, color=C['red'],
                linestyle='--', linewidth=1,
                label=f'Max DD -8% ({ACCOUNT_SIZE+MAX_DD_LIMIT:,.0f}$)')
    ax1.set_ylabel('Compte (USD)', color=C['text'])
    ax1.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=7.5)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # ── Panneau 2 : ADX + zones colorées ────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    style_ax(ax2, f'📊 ADX(14) — Range (vert<{adx_threshold:.0f}) vs Tendance (rouge≥{adx_threshold:.0f})')

    # Utiliser l'ADX de l'equity_curve du bt1 s'il existe, sinon adx_series
    adx_plot = adx_series
    if not eq1.empty and 'adx' in eq1.columns:
        adx_plot = eq1['adx'].dropna()

    if len(adx_plot) > 0:
        adx_idx   = adx_plot.index
        adx_vals  = adx_plot.values
        in_range  = adx_vals < adx_threshold

        # Zones colorées : fond vert (range) / rouge (tendance)
        for k in range(len(adx_idx) - 1):
            color = '#1a3a1a' if in_range[k] else '#3a1a1a'
            ax2.axvspan(adx_idx[k], adx_idx[k + 1], alpha=0.3, color=color, linewidth=0)

        ax2.plot(adx_idx, adx_vals, color=C['blue'], linewidth=1, alpha=0.9, label='ADX(14)')
        ax2.axhline(y=adx_threshold, color=C['gold'], linestyle='--', linewidth=1.2,
                    label=f'Seuil ADX={adx_threshold:.0f}')

        # Signaux pris (points verts = LONG / rouges = SHORT)
        if trades1:
            for t in trades1:
                try:
                    et = t['entry_time']
                    adx_t = adx_plot.asof(et) if hasattr(adx_plot, 'asof') else adx_threshold - 1
                    color_pt = C['green'] if t['direction'] == 'LONG' else C['red']
                    ax2.scatter(et, adx_t, color=color_pt, s=25, zorder=5, alpha=0.8)
                except Exception:
                    pass

        pct_range = (in_range.sum() / max(1, len(in_range))) * 100
        ax2.set_ylabel(f'ADX  ({pct_range:.0f}% du temps en range)', color=C['text'])
        ax2.set_ylim(0, max(60, float(np.nanmax(adx_vals)) * 1.1))
        ax2.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=7.5)

    # ── Panneau 3 : PnL journalier ───────────────────────────
    ax3 = fig.add_subplot(gs[1, 0])
    style_ax(ax3, '📊 PnL Journalier (1 Contrat)')

    ds = bt1.get('daily_stats', [])
    if ds:
        pnls_d     = [d['pnl'] for d in ds]
        bar_colors = [C['green'] if p >= 0 else C['red'] for p in pnls_d]
        ax3.bar(range(len(pnls_d)), pnls_d, color=bar_colors, alpha=0.8, width=0.8)
        ax3.axhline(y=0, color=C['grey'], linewidth=0.8)
        ax3.axhline(y=TARGET_DAILY_PNL, color=C['cyan'], linestyle='--',
                    linewidth=1, alpha=0.7, label=f'Objectif +${TARGET_DAILY_PNL:.0f}/j')
        ax3.axhline(y=DAILY_LOSS_LIMIT, color=C['orange'], linestyle=':',
                    linewidth=1, label=f'Daily limit ${DAILY_LOSS_LIMIT:.0f}')
        ax3.set_ylabel('PnL ($)', color=C['text'])
        ax3.set_xlabel('Jours de trading', color=C['text'])
        ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
        ax3.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=7.5)
    else:
        ax3.text(0.5, 0.5, 'Aucun trade', ha='center', va='center',
                 color=C['text'], transform=ax3.transAxes)

    # ── Panneau 4 : Distribution wins/losses ─────────────────
    ax4 = fig.add_subplot(gs[1, 1])
    style_ax(ax4, '🎯 Distribution Wins (vert) / Losses (rouge)')

    if trades1:
        pnls_all = [t['pnl_usd'] for t in trades1]
        wins_l   = [p for p in pnls_all if p > 0]
        losses_l = [p for p in pnls_all if p <= 0]
        bins     = min(25, max(8, len(pnls_all) // 3))

        if losses_l:
            ax4.hist(losses_l, bins=max(4, bins // 2), color=C['red'],
                     alpha=0.75, label=f'Pertes ({len(losses_l)})')
        if wins_l:
            ax4.hist(wins_l, bins=max(4, bins // 2), color=C['green'],
                     alpha=0.75, label=f'Gains ({len(wins_l)})')

        ax4.axvline(x=0, color='white', linestyle='--', linewidth=1)
        if pnls_all:
            ax4.axvline(x=float(np.mean(pnls_all)), color=C['gold'], linewidth=1.5,
                        label=f"Moy: ${float(np.mean(pnls_all)):.0f}")
        ax4.set_xlabel('PnL par trade ($)', color=C['text'])
        ax4.set_ylabel('Fréquence', color=C['text'])
        ax4.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)
    else:
        ax4.text(0.5, 0.5, 'Aucun trade', ha='center', va='center',
                 color=C['text'], transform=ax4.transAxes)

    # ── Panneau 5 : Tableau stats comparatif ─────────────────
    ax5 = fig.add_subplot(gs[2, :])
    ax5.set_facecolor(C['panel'])
    ax5.axis('off')
    ax5.set_title('📋 Statistiques Comparatives + Meilleurs Paramètres ADX',
                  fontsize=10, fontweight='bold', pad=8, color=C['text'])

    p1   = s1.get('params', {})
    p2   = s2.get('params', {})
    pnl1 = s1.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    pnl2 = s2.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    avg1 = s1.get('avg_pnl_per_day', 0)
    avg2 = s2.get('avg_pnl_per_day', 0)

    table_rows = [
        ('INDICATEUR',       '1 CONTRAT',    '2 CONTRATS',   True),
        ('Win Rate',         f"{s1['win_rate']:.1f}%",
                             f"{s2['win_rate']:.1f}%",        False),
        ('Profit Factor',    f"{s1['profit_factor']:.2f}",
                             f"{s2['profit_factor']:.2f}",    False),
        ('Sharpe',           f"{s1['sharpe']:.2f}",
                             f"{s2['sharpe']:.2f}",           False),
        ('P&L Total',        f"${pnl1:+,.0f}",
                             f"${pnl2:+,.0f}",                False),
        ('P&L Moy/Jour',     f"${avg1:+.0f}",
                             f"${avg2:+.0f}",                 False),
        ('Max Drawdown',     f"-${abs(s1.get('max_dd_usd',0)):,.0f}",
                             f"-${abs(s2.get('max_dd_usd',0)):,.0f}", False),
        ('Jours daily lim.', str(s1.get('n_daily_limit', 0)),
                             str(s2.get('n_daily_limit', 0)), False),
        ('Signaux/jour',     f"{s1['trades_per_day']:.1f}",
                             f"{s2['trades_per_day']:.1f}",   False),
        ('Nb trades',        str(s1['n_trades']),
                             str(s2['n_trades']),              False),
        ('── Params ──',     '',              '',              True),
        ('body_pct',         str(p1.get('body_pct', '-')),
                             str(p2.get('body_pct', '-')),    False),
        ('rr_ratio',         str(p1.get('rr_ratio', '-')),
                             str(p2.get('rr_ratio', '-')),    False),
        ('max_wait',         str(int(p1.get('max_wait_bars', 0))),
                             str(int(p2.get('max_wait_bars', 0))), False),
        ('adx_threshold',    str(int(p1.get('adx_threshold', adx_threshold))),
                             str(int(p2.get('adx_threshold', adx_threshold))), False),
    ]

    col_x = [0.02, 0.38, 0.62]
    y = 0.94
    lh = 0.055
    for label, v1, v2, is_header in table_rows:
        if is_header:
            for xi, txt, col in zip(col_x, [label, v1, v2],
                                    [C['blue'], C['blue'], C['orange']]):
                ax5.text(xi, y, txt, transform=ax5.transAxes,
                         color=col, fontsize=9, fontweight='bold', va='top')
        else:
            c1 = (C['green'] if pnl1 >= 0 else C['red']) if label == 'P&L Total' else \
                 (C['green'] if avg1 >= TARGET_DAILY_PNL else C['orange']) if label == 'P&L Moy/Jour' else \
                 C['text']
            c2 = (C['green'] if pnl2 >= 0 else C['red']) if label == 'P&L Total' else \
                 (C['green'] if avg2 >= TARGET_DAILY_PNL else C['orange']) if label == 'P&L Moy/Jour' else \
                 C['text']
            ax5.text(col_x[0], y, label, transform=ax5.transAxes,
                     color=C['grey'], fontsize=9, va='top')
            ax5.text(col_x[1], y, v1, transform=ax5.transAxes,
                     color=c1, fontsize=9, va='top', fontweight='bold')
            ax5.text(col_x[2], y, v2, transform=ax5.transAxes,
                     color=c2, fontsize=9, va='top', fontweight='bold')
        y -= lh

    fig.suptitle(
        f'NQ Futures 5M + FILTRE ADX — Apex 100k$  |  ADX threshold={adx_threshold:.0f}  |  '
        f'SL {SL_MIN_PTS}–{SL_MAX_PTS}pts  |  RTH 9h30–16h00 ET  |  '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=12, fontweight='bold', color=C['text'], y=0.998,
    )

    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=C['bg'], edgecolor='none')
    plt.close()
    print(f"\n✅ Rapport ADX sauvegardé : {output_path}")


def run_5m_adx():
    """
    Mode 5m_adx : NQ=F 5M + filtre ADX (régime de marché).
    Stratégie de retournement qui ne trade qu'en range (ADX < threshold).
    """
    print("=" * 60)
    print("NQ 5M + FILTRE ADX — BACKTEST (Apex 100k$)")
    print("=" * 60)

    print("\n📥 Téléchargement NQ=F 5M (60j)...")
    try:
        df_raw, conv = download_data(interval='5m', period='60d')
    except Exception as e:
        print(f"❌ Erreur téléchargement : {e}")
        return

    df = filter_rth(df_raw)

    if len(df) < 50:
        print("❌ Données insuffisantes après filtre RTH.")
        return

    try:
        idx = df.index
        if idx.tz is None:
            idx_et = idx.tz_localize('UTC').tz_convert('America/New_York')
        else:
            idx_et = idx.tz_convert('America/New_York')
        n_days_trading = len(set(t.date() for t in idx_et))
    except Exception:
        n_days_trading = max(1, len(df) // 78)

    print(f"\nDonnées : NQ=F 5M | {n_days_trading} jours RTH | {len(df)} barres")

    # ADX global pour stats
    adx_all = compute_adx(df, period=14)
    pct_range_25 = ((adx_all < 25).sum() / max(1, len(adx_all))) * 100

    # Diagnostic signaux bruts (avant ADX)
    df_diag    = detect_exhaustion(df, conv=conv)
    n_raw_patt = int((df_diag['bias'] != 0).sum())
    n_valid_sl = int(df_diag['signal_valid'].sum())
    print(f"Signaux bruts (pattern) : {n_raw_patt}")
    print(f"Après filtre SL {SL_MIN_PTS}-{SL_MAX_PTS}pts  : {n_valid_sl}")
    print(f"ADX moyen global : {adx_all.mean():.1f} | % temps en range (ADX<25) : {pct_range_25:.0f}%")

    # ── Optimisation 1 contrat ──
    print("\n" + "─" * 40)
    print("  OPTIMISATION — 1 CONTRAT + ADX")
    print("─" * 40)
    opt1 = optimize_5m_adx(df, conv=conv, contracts=1)
    best1 = opt1.get('best_params') or {
        'rr_ratio': 2.0, 'body_pct': 0.25, 'max_wait_bars': 3, 'adx_threshold': 25}

    # ── Optimisation 2 contrats ──
    print("\n" + "─" * 40)
    print("  OPTIMISATION — 2 CONTRATS + ADX")
    print("─" * 40)
    opt2 = optimize_5m_adx(df, conv=conv, contracts=2)
    best2 = opt2.get('best_params') or {
        'rr_ratio': 2.0, 'body_pct': 0.25, 'max_wait_bars': 3, 'adx_threshold': 25}

    # ── Backtest final ──
    print("\n🔄 Backtest final — 1 contrat...")
    bt1 = backtest_5m_adx(df, conv=conv, contracts=1, **best1)

    print("🔄 Backtest final — 2 contrats...")
    bt2 = backtest_5m_adx(df, conv=conv, contracts=2, **best2)

    s1 = bt1['stats']
    s2 = bt2['stats']

    # Stats ADX spécifiques
    adx_sigs1 = s1.get('adx_at_signal', [])
    adx_mean_sigs = float(np.mean(adx_sigs1)) if adx_sigs1 else 0.0
    n_raw_s1  = s1.get('n_raw_signals', n_valid_sl)
    n_adx_f1  = s1.get('n_adx_filtered', 0)
    n_after_adx = n_raw_s1 - n_adx_filtered1 if (n_adx_filtered1 := n_adx_f1) else n_raw_s1

    # Générer rapport PNG
    adx_thr_report = float(best1.get('adx_threshold', 25))
    generate_5m_adx_report(bt1, bt2, adx_all, adx_threshold=adx_thr_report,
                           output_path="trading/nasdaq_5m_adx_report.png")

    # Verdict
    def verdict(s):
        avg = s.get('avg_pnl_per_day', 0)
        if avg >= TARGET_DAILY_PNL:
            return "OBJECTIF ATTEINT"
        elif avg >= TARGET_DAILY_PNL * 0.5:
            return "VIABLE"
        else:
            return "INSUFFISANT"

    pnl1 = s1.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    pnl2 = s2.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
    avg1 = s1.get('avg_pnl_per_day', 0)
    avg2 = s2.get('avg_pnl_per_day', 0)

    # ── Sortie console ───────────────────────────────────────
    print()
    print("=" * 60)
    print("NQ 5M + FILTRE ADX — RÉSULTATS (Apex 100k$)")
    print("=" * 60)
    print(f"Données : NQ=F 5M | {n_days_trading} jours RTH")
    print(f"Signaux bruts : {n_raw_patt} → après filtre SL : {n_valid_sl} → "
          f"après filtre ADX : {n_after_adx} "
          f"(~{n_after_adx/max(1, n_days_trading):.1f}/jour)")
    print(f"ADX moyen pendant signaux : {adx_mean_sigs:.1f} | "
          f"% du temps en range (ADX<25) : {pct_range_25:.0f}%")
    print()
    print(f"{'':24s}  {'1 CONTRAT':>12s}  {'2 CONTRATS':>12s}")
    print("-" * 54)
    print(f"{'Win Rate':24s}  {s1['win_rate']:>11.1f}%  {s2['win_rate']:>11.1f}%")
    print(f"{'Profit Factor':24s}  {s1['profit_factor']:>12.2f}  {s2['profit_factor']:>12.2f}")
    print(f"{'Sharpe':24s}  {s1['sharpe']:>12.2f}  {s2['sharpe']:>12.2f}")
    print(f"{'P&L Total':24s}  {pnl1:>+11,.0f}$  {pnl2:>+11,.0f}$")
    print(f"{'P&L Moyen/Jour':24s}  {avg1:>+11,.0f}$  {avg2:>+11,.0f}$"
          f"  ← objectif: {TARGET_DAILY_PNL:.0f}$/j")
    print(f"{'Max Drawdown':24s}  {-abs(s1.get('max_dd_usd',0)):>+11,.0f}$"
          f"  {-abs(s2.get('max_dd_usd',0)):>+11,.0f}$")
    print(f"{'Jours daily limit':24s}  {s1.get('n_daily_limit',0):>12d}"
          f"  {s2.get('n_daily_limit',0):>12d}")
    print(f"{'Signaux/jour':24s}  {s1['trades_per_day']:>12.1f}  {s2['trades_per_day']:>12.1f}")
    print()
    p1 = best1
    p2 = best2
    print(f"Best params (1c) : body_pct={p1['body_pct']}, rr={p1['rr_ratio']}, "
          f"max_wait={int(p1['max_wait_bars'])}, adx_threshold={int(p1['adx_threshold'])}")
    print(f"Best params (2c) : body_pct={p2['body_pct']}, rr={p2['rr_ratio']}, "
          f"max_wait={int(p2['max_wait_bars'])}, adx_threshold={int(p2['adx_threshold'])}")
    print()
    print("VERDICT :")
    print(f"1 contrat  → {verdict(s1)}")
    print(f"2 contrats → {verdict(s2)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
