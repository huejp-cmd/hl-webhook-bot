#!/usr/bin/env python3
"""
NQ 5M — BREAKOUT RETOURNEMENT + FILTRE ADX ≤ 20
=================================================
Stratégie :
  LONG  : Candle ROUGE 10-30 pts NQ + ADX ≤ 20
          → Niveau cible = HIGH_rouge + 2 pts
          → Max 3 barres suivantes : si HIGH ≥ cible → ENTRÉE LONG
  SHORT : Candle VERT  10-30 pts NQ + ADX ≤ 20
          → Niveau cible = LOW_vert  - 2 pts
          → Max 3 barres suivantes : si LOW  ≤ cible → ENTRÉE SHORT

SL = corps (pts) × 20$ × contrats
TP = corps × rr_ratio × 20$ × contrats
Sortie forcée 15h50 ET | RTH uniquement 9h30-16h00 ET
Une seule position à la fois

Comparaison vs ancienne version (ADX+indécision) :
  WR=50.0% | PF=2.61 | P&L=+7840$ | DD=-2440$ | 16 trades/50j
"""

import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import yfinance as yf
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────
TICKER           = "NQ=F"
INTERVAL         = "5m"
PERIOD           = "60d"
POINT_VALUE      = 20        # $/point NQ E-mini
ACCOUNT_SIZE     = 100_000
DAILY_LOSS_LIMIT = -2_000
MAX_DD_LIMIT     = -8_000
ADX_THRESHOLD    = 20        # filtre : ADX ≤ 20
BREAKOUT_PTS     = 2         # pts au-delà du HIGH/LOW
MAX_WAIT_BARS    = 3         # max barres d'attente breakout
SL_MIN_PTS       = 10        # corps min signal (pts NQ)
SL_MAX_PTS       = 30        # corps max signal (pts NQ)
FRAIS_RT         = 5.0       # $ aller-retour par contrat

RR_RATIOS      = [1.0, 1.5, 2.0, 2.5, 3.0]
CONTRACTS_LIST = [1, 2]

# Valeurs référence ancienne version (ADX+indécision)
OLD_WR    = 50.0
OLD_PF    = 2.61
OLD_PNL   = 7_840
OLD_DD    = -2_440
OLD_TRADES = 16   # sur ~50j

# ─────────────────────────────────────────────────────────────
# ADX (14) — repris de nasdaq_strategy.py
# ─────────────────────────────────────────────────────────────
def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average Directional Index (ADX).
    ADX ≤ 20 → marché en range → retournement probable.
    """
    high  = df['high']
    low   = df['low']
    close = df['close']

    tr = pd.DataFrame({
        'hl': high - low,
        'hc': (high - close.shift(1)).abs(),
        'lc': (low  - close.shift(1)).abs(),
    }).max(axis=1)

    dm_plus  = high.diff()
    dm_minus = -low.diff()
    dm_plus  = dm_plus.where((dm_plus  > dm_minus) & (dm_plus  > 0), 0.0)
    dm_minus = dm_minus.where((dm_minus > dm_plus)  & (dm_minus > 0), 0.0)

    atr      = tr.ewm(span=period, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm(span=period,  adjust=False).mean() / atr
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / atr

    denom = (di_plus + di_minus).replace(0, np.nan)
    dx    = (100 * (di_plus - di_minus).abs() / denom).fillna(0)
    adx   = dx.ewm(span=period, adjust=False).mean()
    return adx


# ─────────────────────────────────────────────────────────────
# DONNÉES
# ─────────────────────────────────────────────────────────────
def download_data() -> pd.DataFrame:
    print(f"  📥 Téléchargement {TICKER} [{INTERVAL}, {PERIOD}]...")
    df = yf.download(TICKER, interval=INTERVAL, period=PERIOD,
                     progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"Données vides pour {TICKER}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
    df = df[df['close'] > 0].copy()
    print(f"  ✅ {len(df)} barres ({df.index[0].date()} → {df.index[-1].date()})")
    return df


def filter_rth(df: pd.DataFrame) -> pd.DataFrame:
    """Filtre RTH 9h30-16h00 ET, lundi-vendredi."""
    try:
        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize('UTC')
        idx_et = idx.tz_convert('America/New_York')
        mask = (
            (idx_et.dayofweek < 5) &
            ((idx_et.hour > 9) | ((idx_et.hour == 9) & (idx_et.minute >= 30))) &
            (idx_et.hour < 16)
        )
        df_rth = df.iloc[np.asarray(mask, dtype=bool)].copy()
        print(f"  🕐 Filtre RTH : {len(df_rth)} barres conservées "
              f"({len(df) - len(df_rth)} retirées)")
        return df_rth
    except Exception as e:
        print(f"  ⚠️  Filtre RTH impossible ({e}) — toutes les barres conservées")
        return df


def get_et_index(df: pd.DataFrame):
    try:
        idx = df.index
        if idx.tz is None:
            return idx.tz_localize('UTC').tz_convert('America/New_York')
        return idx.tz_convert('America/New_York')
    except Exception:
        return df.index


# ─────────────────────────────────────────────────────────────
# BACKTEST BREAKOUT + ADX
# ─────────────────────────────────────────────────────────────
def backtest_breakout_adx(df: pd.DataFrame, adx: pd.Series,
                          rr_ratio: float = 2.0,
                          contracts: int = 1) -> dict:
    """
    Stratégie breakout retournement + filtre ADX ≤ 20.

    LONG  : candle ROUGE 10-30 pts + ADX ≤ 20
            → cible = HIGH_rouge + BREAKOUT_PTS
            → Entrée dès qu'une des 3 prochaines barres atteint la cible
    SHORT : candle VERT  10-30 pts + ADX ≤ 20
            → cible = LOW_vert  - BREAKOUT_PTS
            → Entrée dès qu'une des 3 prochaines barres casse la cible

    SL  = corps (pts) × POINT_VALUE × contracts
    TP  = corps × rr_ratio × POINT_VALUE × contracts
    """
    idx_et  = get_et_index(df)
    n       = len(df)
    frais   = FRAIS_RT * contracts

    account    = float(ACCOUNT_SIZE)
    peak_acct  = float(ACCOUNT_SIZE)
    halted     = False

    daily_pnl     = 0.0
    daily_stopped = False
    current_date  = None
    n_daily_limit = 0

    # États : IDLE=0, WAIT_BREAKOUT=1, IN_POSITION=2
    IDLE, WAIT, IN_POS = 0, 1, 2
    state      = IDLE

    # Signal courant
    sig_bias      = 0        # +1 LONG / -1 SHORT
    sig_body_pts  = 0.0      # corps du signal en pts NQ
    sig_entry_lvl = 0.0      # niveau d'entrée (breakout trigger)
    sig_sl_pts    = 0.0      # SL en pts NQ = corps
    sig_tp_pts    = 0.0      # TP en pts NQ = corps × rr
    wait_count    = 0

    # Position ouverte
    entry_price = 0.0
    sl_price    = 0.0
    tp_price    = 0.0
    entry_time  = None
    entry_bar   = 0
    pos_bias    = 0

    trades       = []
    equity_curve = []
    daily_stats  = []

    # Statistiques signaux
    n_body_signals = 0   # candles avec corps 10-30 pts
    n_adx_signals  = 0   # après filtre ADX ≤ 20
    n_triggered    = 0   # effectivement déclenchés (breakout atteint)

    for i in range(n):
        row       = df.iloc[i]
        timestamp = df.index[i]

        try:
            bar_et     = idx_et[i]
            bar_date   = bar_et.date()
            bar_hour   = bar_et.hour
            bar_minute = bar_et.minute
        except Exception:
            bar_date   = timestamp.date()
            bar_hour   = 15
            bar_minute = 59

        adx_val = float(adx.iloc[i]) if i < len(adx) else 100.0

        # ── Changement de journée ──
        if bar_date != current_date:
            if current_date is not None:
                if daily_stopped:
                    n_daily_limit += 1
                daily_stats.append({'date': current_date, 'pnl': daily_pnl,
                                    'stopped': daily_stopped})
            current_date  = bar_date
            daily_pnl     = 0.0
            daily_stopped = False

        trading_allowed  = (not halted) and (not daily_stopped)
        force_close_time = (bar_hour == 15 and bar_minute >= 50) or (bar_hour >= 16)

        next_is_new_day = False
        if i + 1 < n:
            try:
                next_is_new_day = (idx_et[i + 1].date() != bar_date)
            except Exception:
                next_is_new_day = (df.index[i + 1].date() != timestamp.date())
        else:
            next_is_new_day = True

        # ═══════════════════════════════════════
        # ÉTAT : IN_POSITION
        # ═══════════════════════════════════════
        if state == IN_POS:
            hit_sl = False
            hit_tp = False
            exit_price = row['close']
            raison = 'En cours'

            if pos_bias == 1:   # LONG
                if row['low'] <= sl_price:
                    hit_sl = True; exit_price = sl_price
                elif row['high'] >= tp_price:
                    hit_tp = True; exit_price = tp_price
            else:               # SHORT
                if row['high'] >= sl_price:
                    hit_sl = True; exit_price = sl_price
                elif row['low'] <= tp_price:
                    hit_tp = True; exit_price = tp_price

            # Sortie forcée 15h50 ET ou fin session
            if not hit_sl and not hit_tp and (force_close_time or next_is_new_day):
                exit_price = row['close']
                raison = '15h50 ET' if force_close_time else 'Fin session'

            if hit_sl or hit_tp or raison in ('15h50 ET', 'Fin session'):
                if hit_sl:   raison = 'SL'
                elif hit_tp: raison = 'TP'

                if pos_bias == 1:
                    pnl_pts = exit_price - entry_price
                else:
                    pnl_pts = entry_price - exit_price

                pnl_usd    = pnl_pts * POINT_VALUE * contracts - frais
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
                    'direction':   'LONG' if pos_bias == 1 else 'SHORT',
                    'entry_price': entry_price,
                    'exit_price':  exit_price,
                    'sl_price':    sl_price,
                    'tp_price':    tp_price,
                    'sl_pts':      sig_sl_pts,
                    'tp_pts':      sig_tp_pts,
                    'pnl_pts':     pnl_pts,
                    'pnl_usd':     pnl_usd,
                    'account':     account,
                    'raison':      raison,
                    'duree_bars':  i - entry_bar,
                    'daily_pnl':   daily_pnl,
                    'contracts':   contracts,
                    'rr_ratio':    rr_ratio,
                })

                state      = IDLE
                pos_bias   = 0
                wait_count = 0

        # ═══════════════════════════════════════
        # ÉTAT : WAIT_BREAKOUT
        # ═══════════════════════════════════════
        elif state == WAIT and trading_allowed and not force_close_time:
            triggered = False

            if sig_bias == 1:   # LONG : attendre HIGH ≥ sig_entry_lvl
                if row['high'] >= sig_entry_lvl:
                    triggered   = True
                    entry_price = sig_entry_lvl   # entrée au niveau exact
                    sl_price    = entry_price - sig_sl_pts
                    tp_price    = entry_price + sig_tp_pts
            else:               # SHORT : attendre LOW ≤ sig_entry_lvl
                if row['low'] <= sig_entry_lvl:
                    triggered   = True
                    entry_price = sig_entry_lvl
                    sl_price    = entry_price + sig_sl_pts
                    tp_price    = entry_price - sig_tp_pts

            if triggered:
                n_triggered += 1
                entry_time   = timestamp
                entry_bar    = i
                pos_bias     = sig_bias
                state        = IN_POS
                wait_count   = 0
            else:
                wait_count += 1
                if wait_count >= MAX_WAIT_BARS or next_is_new_day:
                    state      = IDLE
                    sig_bias   = 0
                    wait_count = 0

        # ═══════════════════════════════════════
        # ÉTAT : IDLE — chercher signal
        # ═══════════════════════════════════════
        if state == IDLE and trading_allowed and not force_close_time:
            body = abs(row['close'] - row['open'])

            if SL_MIN_PTS <= body <= SL_MAX_PTS:
                n_body_signals += 1

                if adx_val <= ADX_THRESHOLD:
                    n_adx_signals += 1

                    if row['close'] < row['open']:   # Candle ROUGE → LONG
                        sig_bias      =  1
                        sig_body_pts  =  body
                        sig_entry_lvl =  row['high'] + BREAKOUT_PTS
                        sig_sl_pts    =  body
                        sig_tp_pts    =  body * rr_ratio
                        state         = WAIT
                        wait_count    = 0

                    elif row['close'] > row['open']:  # Candle VERT → SHORT
                        sig_bias      = -1
                        sig_body_pts  =  body
                        sig_entry_lvl =  row['low'] - BREAKOUT_PTS
                        sig_sl_pts    =  body
                        sig_tp_pts    =  body * rr_ratio
                        state         = WAIT
                        wait_count    = 0

        equity_curve.append({'time': timestamp, 'account': account})

    # Fermer position ouverte en fin de données
    if state == IN_POS:
        exit_price = df.iloc[-1]['close']
        if pos_bias == 1:
            pnl_pts = exit_price - entry_price
        else:
            pnl_pts = entry_price - exit_price
        pnl_usd = pnl_pts * POINT_VALUE * contracts - frais / 2
        account += pnl_usd
        trades.append({
            'entry_time': entry_time, 'exit_time': df.index[-1],
            'direction': 'LONG' if pos_bias == 1 else 'SHORT',
            'entry_price': entry_price, 'exit_price': exit_price,
            'sl_price': sl_price, 'tp_price': tp_price,
            'sl_pts': sig_sl_pts, 'tp_pts': sig_tp_pts,
            'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd,
            'account': account, 'raison': 'Fin données',
            'duree_bars': n - entry_bar, 'daily_pnl': daily_pnl,
            'contracts': contracts, 'rr_ratio': rr_ratio,
        })

    if current_date is not None:
        if daily_stopped:
            n_daily_limit += 1
        daily_stats.append({'date': current_date, 'pnl': daily_pnl,
                            'stopped': daily_stopped})

    eq_df = pd.DataFrame(equity_curve).set_index('time') if equity_curve else pd.DataFrame()

    return {
        'trades':         trades,
        'equity_curve':   eq_df,
        'daily_stats':    daily_stats,
        'halted':         halted,
        'n_daily_limit':  n_daily_limit,
        'n_body_signals': n_body_signals,
        'n_adx_signals':  n_adx_signals,
        'n_triggered':    n_triggered,
    }


# ─────────────────────────────────────────────────────────────
# CALCUL STATISTIQUES
# ─────────────────────────────────────────────────────────────
def compute_stats(result: dict, n_days: int) -> dict:
    trades = result['trades']
    if not trades:
        return {
            'win_rate': 0, 'profit_factor': 0, 'total_pnl': 0,
            'max_dd': 0, 'n_trades': 0, 'trades_per_day': 0,
        }

    pnls  = [t['pnl_usd'] for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) * 100
    pf = (sum(wins) / abs(sum(losses))) if losses else float('inf')
    total_pnl = sum(pnls)

    # Max drawdown
    eq = result['equity_curve']
    max_dd = 0.0
    if not eq.empty and 'account' in eq.columns:
        acct_vals = eq['account'].values
        peak = ACCOUNT_SIZE
        for a in acct_vals:
            if a > peak:
                peak = a
            dd = a - peak
            if dd < max_dd:
                max_dd = dd

    trades_per_day = len(trades) / max(1, n_days)

    return {
        'win_rate':       win_rate,
        'profit_factor':  pf,
        'total_pnl':      total_pnl,
        'max_dd':         max_dd,
        'n_trades':       len(trades),
        'trades_per_day': trades_per_day,
        'n_daily_limit':  result['n_daily_limit'],
        'halted':         result['halted'],
    }


# ─────────────────────────────────────────────────────────────
# SCORE OPTIMISATION
# ─────────────────────────────────────────────────────────────
def score_fn(s: dict) -> float:
    """Score = profit_factor × win_rate_norm × freq_bonus"""
    if s['n_trades'] < 3:
        return 0.0
    tpd = s['trades_per_day']
    if 3 <= tpd <= 8:
        freq_bonus = 1.2
    elif 1 <= tpd < 3:
        freq_bonus = 1.0
    else:
        freq_bonus = 0.7
    pf = max(s['profit_factor'], 0)
    wr = s['win_rate'] / 100
    return pf * wr * freq_bonus


# ─────────────────────────────────────────────────────────────
# RAPPORT PNG
# ─────────────────────────────────────────────────────────────
def generate_report(results_1c: dict, results_2c: dict,
                    best_1c_rr: float, best_2c_rr: float,
                    n_days: int,
                    output_path: str = "trading/nasdaq_breakout_adx_report.png"):
    """
    4 panneaux :
    1. Equity curve meilleure config 1c vs 2c
    2. WR et PF par RR ratio (barres)
    3. Comparaison ancienne vs nouvelle version
    4. Journal trades + stats finales
    """
    fig = plt.figure(figsize=(20, 14))
    fig.patch.set_facecolor('#0d1117')
    gs  = gridspec.GridSpec(2, 2, figure=fig, hspace=0.45, wspace=0.35)

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
        ax.set_title(title, fontsize=11, fontweight='bold', pad=10, color=C['text'])

    # ── Panneau 1 : Equity Curve meilleure config 1c vs 2c ──
    ax1 = fig.add_subplot(gs[0, 0])
    style_ax(ax1, f'📈 Equity Curve — Meilleure config (RR={best_1c_rr})')

    eq1 = results_1c[best_1c_rr]['result']['equity_curve']
    eq2 = results_2c[best_2c_rr]['result']['equity_curve']

    if not eq1.empty and 'account' in eq1.columns:
        ax1.plot(eq1.index, eq1['account'], color=C['blue'],
                 linewidth=1.5, label=f'1 Contrat (RR={best_1c_rr})', alpha=0.9)
    if not eq2.empty and 'account' in eq2.columns:
        ax1.plot(eq2.index, eq2['account'], color=C['orange'],
                 linewidth=1.5, label=f'2 Contrats (RR={best_2c_rr})', alpha=0.9)

    ax1.axhline(y=ACCOUNT_SIZE, color=C['grey'], linestyle=':', linewidth=1,
                label='Capital initial 100k$')
    ax1.axhline(y=ACCOUNT_SIZE + MAX_DD_LIMIT, color=C['red'],
                linestyle='--', linewidth=1,
                label=f'Max DD Apex -8%')
    ax1.set_ylabel('Compte (USD)', color=C['text'])
    ax1.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # ── Panneau 2 : WR et PF par RR ratio ───────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    style_ax(ax2, '📊 Win Rate & Profit Factor par RR (1 contrat)')

    rrs   = sorted(results_1c.keys())
    wrs   = [results_1c[rr]['stats']['win_rate']      for rr in rrs]
    pfs   = [results_1c[rr]['stats']['profit_factor'] for rr in rrs]
    x     = np.arange(len(rrs))
    width = 0.35

    ax2b = ax2.twinx()
    bars1 = ax2.bar(x - width/2, wrs, width, color=C['blue'],  alpha=0.8, label='Win Rate %')
    bars2 = ax2b.bar(x + width/2, pfs, width, color=C['gold'], alpha=0.8, label='Profit Factor')

    ax2.set_xticks(x)
    ax2.set_xticklabels([f'RR={r}' for r in rrs], color=C['text'])
    ax2.set_ylabel('Win Rate (%)', color=C['blue'])
    ax2b.set_ylabel('Profit Factor', color=C['gold'])
    ax2b.tick_params(colors=C['text'])
    ax2.set_facecolor(C['panel'])
    ax2b.set_facecolor(C['panel'])

    ax2.axhline(y=50, color=C['grey'], linestyle=':', linewidth=0.8, alpha=0.5)
    ax2b.axhline(y=1.0, color=C['orange'], linestyle=':', linewidth=0.8, alpha=0.5)

    lines1, labels1 = ax2.get_legend_handles_labels()
    lines2, labels2 = ax2b.get_legend_handles_labels()
    ax2.legend(lines1 + lines2, labels1 + labels2,
               facecolor=C['panel'], labelcolor=C['text'], fontsize=8)

    # Annoter les valeurs
    for bar, val in zip(bars1, wrs):
        ax2.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.5,
                 f'{val:.1f}%', ha='center', va='bottom',
                 color=C['text'], fontsize=7.5, fontweight='bold')
    for bar, val in zip(bars2, pfs):
        ax2b.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.01,
                  f'{val:.2f}', ha='center', va='bottom',
                  color=C['text'], fontsize=7.5, fontweight='bold')

    # ── Panneau 3 : Comparaison ancienne vs nouvelle ─────────
    ax3 = fig.add_subplot(gs[1, 0])
    style_ax(ax3, '⚔️  Comparaison — Ancienne (ADX+indécision) vs Nouvelle (ADX+breakout)')

    s_best_1c = results_1c[best_1c_rr]['stats']
    new_wr    = s_best_1c['win_rate']
    new_pf    = s_best_1c['profit_factor']
    new_pnl   = s_best_1c['total_pnl']
    new_dd    = abs(s_best_1c['max_dd'])

    metrics   = ['Win Rate (%)', 'Profit Factor', 'P&L ($)', 'Max DD ($)']
    old_vals  = [OLD_WR, OLD_PF, OLD_PNL, abs(OLD_DD)]
    new_vals  = [new_wr, new_pf, max(0, new_pnl), new_dd]

    # Normaliser pour l'affichage (max de chaque métrique = 1)
    max_vals_norm = [max(o, n, 0.01) for o, n in zip(old_vals, new_vals)]
    old_norm = [o / m for o, m in zip(old_vals, max_vals_norm)]
    new_norm = [n / m for n, m in zip(new_vals, max_vals_norm)]

    x_cmp = np.arange(len(metrics))
    w_cmp = 0.35

    bars_old = ax3.bar(x_cmp - w_cmp/2, old_norm, w_cmp,
                       color=C['grey'], alpha=0.8, label='Ancienne version')
    bars_new = ax3.bar(x_cmp + w_cmp/2, new_norm, w_cmp,
                       color=C['green'], alpha=0.8, label='Nouvelle version')

    ax3.set_xticks(x_cmp)
    ax3.set_xticklabels(metrics, color=C['text'], fontsize=9)
    ax3.set_ylabel('Score normalisé', color=C['text'])
    ax3.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)
    ax3.set_ylim(0, 1.3)

    # Annoter les vraies valeurs
    for bar, val, label in zip(bars_old, old_vals, metrics):
        if label == 'P&L ($)':
            txt = f'+{val:,.0f}$'
        elif label == 'Max DD ($)':
            txt = f'-{val:,.0f}$'
        elif label == 'Profit Factor':
            txt = f'{val:.2f}'
        else:
            txt = f'{val:.1f}%'
        ax3.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.02,
                 txt, ha='center', va='bottom',
                 color=C['grey'], fontsize=7.5, fontweight='bold')

    for bar, val, label in zip(bars_new, new_vals, metrics):
        if label == 'P&L ($)':
            txt = f'+{val:,.0f}$'
        elif label == 'Max DD ($)':
            txt = f'-{val:,.0f}$'
        elif label == 'Profit Factor':
            txt = f'{val:.2f}'
        else:
            txt = f'{val:.1f}%'
        ax3.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.02,
                 txt, ha='center', va='bottom',
                 color=C['green'], fontsize=7.5, fontweight='bold')

    # ── Panneau 4 : Journal trades + stats finales ───────────
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.set_facecolor(C['panel'])
    ax4.axis('off')
    style_ax(ax4, '📋 Stats finales — Meilleure config (1 contrat)')

    s1  = results_1c[best_1c_rr]['stats']
    s2  = results_2c[best_2c_rr]['stats']
    r1  = results_1c[best_1c_rr]['result']

    rows_data = [
        ('── RR Optimal (1c) ──',    '',                  C['blue']),
        ('RR ratio',                 f"{best_1c_rr}",     None),
        ('Win Rate',                 f"{s1['win_rate']:.1f}%", None),
        ('Profit Factor',            f"{s1['profit_factor']:.2f}", None),
        ('P&L Total',                f"${s1['total_pnl']:+,.0f}",
            C['green'] if s1['total_pnl'] >= 0 else C['red']),
        ('Max Drawdown',             f"${s1['max_dd']:,.0f}",    C['red']),
        ('Trades total',             f"{s1['n_trades']}",         None),
        ('Trades/jour',              f"{s1['trades_per_day']:.1f}", None),
        ('Daily limit touché',       f"{s1['n_daily_limit']} fois", None),
        ('Max DD Apex dépassé',      '⛔ OUI' if s1['halted'] else '✅ NON',
            C['red'] if s1['halted'] else C['green']),
        ('SEP', None, None),
        ('── RR Optimal (2c) ──',    '',                  C['orange']),
        ('RR ratio',                 f"{best_2c_rr}",     None),
        ('Win Rate',                 f"{s2['win_rate']:.1f}%", None),
        ('Profit Factor',            f"{s2['profit_factor']:.2f}", None),
        ('P&L Total',                f"${s2['total_pnl']:+,.0f}",
            C['green'] if s2['total_pnl'] >= 0 else C['red']),
        ('Max Drawdown',             f"${s2['max_dd']:,.0f}",    C['red']),
        ('SEP', None, None),
        ('── Signaux ──',            '',                  C['purple']),
        ('Corps 10-30 pts',          f"{r1.get('n_body_signals', '?')}",  None),
        ('Après ADX ≤ 20',           f"{r1.get('n_adx_signals', '?')}",   None),
        ('Déclenchés (breakout)',     f"{r1.get('n_triggered', '?')}",     None),
    ]

    y = 0.97
    for label, value, forced_color in rows_data:
        if label == 'SEP':
            ax4.plot([0.02, 0.98], [y + 0.005, y + 0.005],
                     color='#30363d', linewidth=0.5, transform=ax4.transAxes)
            y -= 0.022
            continue
        if value == '' and forced_color:
            ax4.text(0.05, y, label, transform=ax4.transAxes,
                     color=forced_color, fontsize=8.5, fontweight='bold', va='top')
            y -= 0.040
            continue
        vc = forced_color if forced_color else C['text']
        ax4.text(0.05, y, label, transform=ax4.transAxes,
                 color=C['grey'], fontsize=8.5, va='top')
        ax4.text(0.60, y, str(value), transform=ax4.transAxes,
                 color=vc, fontsize=8.5, va='top', fontweight='bold')
        y -= 0.040

    fig.suptitle(
        f'NQ 5M — BREAKOUT + ADX≤{ADX_THRESHOLD}  |  Apex 100k$  |  '
        f'SL={SL_MIN_PTS}-{SL_MAX_PTS}pts corps  |  TP=corps×RR  |  '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=12, fontweight='bold', color=C['text'], y=0.988,
    )

    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=C['bg'], edgecolor='none')
    plt.close()
    print(f"\n✅ Rapport sauvegardé : {output_path}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("NQ 5M — BREAKOUT + ADX≤20 (SL=corps, TP=corps×RR)")
    print("=" * 60)

    # ── 1. Données ──────────────────────────────────────────
    print("\n📥 Données :")
    df_raw = download_data()
    df     = filter_rth(df_raw)

    if len(df) < 50:
        print("❌ Données insuffisantes après filtre RTH.")
        return

    # Nombre de jours de trading
    idx_et = get_et_index(df)
    try:
        trading_dates = sorted(set(t.date() for t in idx_et))
        n_days        = len(trading_dates)
    except Exception:
        n_days = max(1, len(df) // 78)

    # ── 2. ADX ──────────────────────────────────────────────
    adx = compute_adx(df, period=14)
    adx_aligned = adx.reindex(df.index).ffill().fillna(50.0)

    # ── 3. Diagnostic signaux ───────────────────────────────
    body     = (df['close'] - df['open']).abs()
    n_body   = int(((body >= SL_MIN_PTS) & (body <= SL_MAX_PTS)).sum())
    adx_vals = adx_aligned.values
    n_adx    = int(((body >= SL_MIN_PTS) & (body <= SL_MAX_PTS) &
                    (adx_vals <= ADX_THRESHOLD)).sum())

    # ── 4. Boucle RR × contrats ─────────────────────────────
    results_1c = {}
    results_2c = {}

    for rr in RR_RATIOS:
        for contracts in CONTRACTS_LIST:
            res   = backtest_breakout_adx(df, adx_aligned, rr_ratio=rr,
                                          contracts=contracts)
            stats = compute_stats(res, n_days)

            if contracts == 1:
                results_1c[rr] = {'result': res, 'stats': stats}
            else:
                results_2c[rr] = {'result': res, 'stats': stats}

    # ── 5. Meilleur RR par nb contrats ──────────────────────
    best_1c_rr = max(RR_RATIOS, key=lambda rr: score_fn(results_1c[rr]['stats']))
    best_2c_rr = max(RR_RATIOS, key=lambda rr: score_fn(results_2c[rr]['stats']))

    # ── 6. Statistiques déclenchements ──────────────────────
    n_triggered = results_1c[best_1c_rr]['result']['n_triggered']

    # ── 7. SÉCURITÉ APEX ────────────────────────────────────
    best_s1 = results_1c[best_1c_rr]['stats']
    max_dd_pct = abs(best_s1['max_dd']) / ACCOUNT_SIZE * 100

    # ── 8. SORTIE CONSOLE ───────────────────────────────────
    print()
    print("=" * 60)
    print("NQ 5M — BREAKOUT + ADX≤20 (SL=corps, TP=corps×RR)")
    print("=" * 60)
    print(f"Données : {TICKER} {INTERVAL} | {n_days} jours RTH")
    print(f"Signaux corps {SL_MIN_PTS}-{SL_MAX_PTS} pts : {n_body}")
    print(f"Signaux après ADX≤{ADX_THRESHOLD}   : {n_adx} "
          f"(~{n_adx/n_days:.1f}/jour)")
    print(f"Déclenchés (breakout)  : {n_triggered} "
          f"(~{n_triggered/n_days:.1f}/jour)")
    print()
    print("RÉSULTATS PAR RR (1 contrat) :")
    print(f"{'RR':>5} | {'WR%':>6} | {'PF':>6} | {'P&L$':>8} | {'DD$':>8} | {'Trades':>6} | {'Sig/j':>5}")
    print("-" * 60)
    for rr in RR_RATIOS:
        s = results_1c[rr]['stats']
        r = results_1c[rr]['result']
        sig_j = r['n_triggered'] / n_days
        print(f"{rr:>5.1f} | {s['win_rate']:>5.1f}% | {s['profit_factor']:>6.2f} | "
              f"{s['total_pnl']:>+8,.0f} | {s['max_dd']:>+8,.0f} | "
              f"{s['n_trades']:>6} | {sig_j:>5.1f}")

    s1_best = results_1c[best_1c_rr]['stats']
    print(f"\n🏆 MEILLEUR RR (1c) : RR={best_1c_rr} | "
          f"WR={s1_best['win_rate']:.0f}% | "
          f"PF={s1_best['profit_factor']:.2f} | "
          f"P&L={s1_best['total_pnl']:+,.0f}$")

    print()
    print("RÉSULTATS PAR RR (2 contrats) :")
    print(f"{'RR':>5} | {'WR%':>6} | {'PF':>6} | {'P&L$':>8} | {'DD$':>8} | {'Trades':>6} | {'Sig/j':>5}")
    print("-" * 60)
    for rr in RR_RATIOS:
        s = results_2c[rr]['stats']
        r = results_2c[rr]['result']
        sig_j = r['n_triggered'] / n_days
        print(f"{rr:>5.1f} | {s['win_rate']:>5.1f}% | {s['profit_factor']:>6.2f} | "
              f"{s['total_pnl']:>+8,.0f} | {s['max_dd']:>+8,.0f} | "
              f"{s['n_trades']:>6} | {sig_j:>5.1f}")

    s2_best = results_2c[best_2c_rr]['stats']
    print(f"\n🏆 MEILLEUR RR (2c) : RR={best_2c_rr} | "
          f"WR={s2_best['win_rate']:.0f}% | "
          f"PF={s2_best['profit_factor']:.2f} | "
          f"P&L={s2_best['total_pnl']:+,.0f}$")

    print()
    print("SÉCURITÉ APEX :")
    print(f"Daily limit touché : {s1_best['n_daily_limit']} fois | "
          f"Max DD : {max_dd_pct:.1f}%")

    # Trades/50j (normalisation pour comparaison)
    new_trades_50j = int(round(s1_best['n_trades'] / n_days * 50))
    new_pnl_50j    = s1_best['total_pnl'] / n_days * 50 if n_days > 0 else s1_best['total_pnl']
    new_dd         = s1_best['max_dd']

    print()
    print("COMPARAISON FINALE :")
    print(f"{'':22s}  {'Ancienne version':>18s}  {'Nouvelle version':>18s}")
    print(f"{'':22s}  {'(ADX+indécision)':>18s}  {'(ADX+breakout)':>18s}")
    print(f"{'Win Rate':22s}: {'':>10s}{OLD_WR:.1f}%    "
          f"{'':>10s}{s1_best['win_rate']:.1f}%")
    print(f"{'Profit Factor':22s}: {'':>13s}{OLD_PF:.2f}    "
          f"{'':>13s}{s1_best['profit_factor']:.2f}")
    print(f"{'P&L Total':22s}: {'':>9s}+{OLD_PNL:,}$    "
          f"{'':>8s}{s1_best['total_pnl']:+,.0f}$")
    print(f"{'Max DD':22s}: {'':>9s}{OLD_DD:,}$    "
          f"{'':>9s}{new_dd:+,.0f}$")
    print(f"{'Trades/50j':22s}: {'':>14s}{OLD_TRADES}    "
          f"{'':>14s}{new_trades_50j}")
    print("=" * 60)

    # ── 9. Rapport PNG ──────────────────────────────────────
    generate_report(
        results_1c=results_1c,
        results_2c=results_2c,
        best_1c_rr=best_1c_rr,
        best_2c_rr=best_2c_rr,
        n_days=n_days,
        output_path="trading/nasdaq_breakout_adx_report.png",
    )


if __name__ == '__main__':
    main()
