#!/usr/bin/env python3
"""
BACKTEST FINAL — NQ 5M | ADX=20 | 2 CONTRATS (Apex 100k$)
Configuration définitive JP — paramètres fixés, pas d'optimisation.

Usage :
  python trading/nasdaq_final_backtest.py

Génère :
  trading/nasdaq_final_2c.png
  trading/nasdaq_trades_final.csv
"""

import warnings
import sys
import os

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
import yfinance as yf

warnings.filterwarnings('ignore')

# ─────────────────────────────────────────────────────────────
# CONFIGURATION DÉFINITIVE JP
# ─────────────────────────────────────────────────────────────
TICKER          = "NQ=F"
INTERVAL        = "5m"
PERIOD          = "60d"
CONTRACTS       = 2
POINT_VALUE     = 20.0          # 20$/point NQ
ACCOUNT_SIZE    = 100_000.0     # Apex 100k$
DAILY_LOSS_LIMIT = -2_000.0
MAX_DD_LIMIT    = -8_000.0
SL_MIN_PTS      = 10
SL_MAX_PTS      = 30
FRAIS_RT        = 5.0 * CONTRACTS   # $/aller-retour × contrats

# Params optimaux confirmés
ADX_THRESHOLD   = 20
BODY_PCT        = 0.15
RR_RATIO        = 1.5
MAX_WAIT        = 2

# RTH
RTH_START_H, RTH_START_M = 9, 30
RTH_END_H,   RTH_END_M   = 16, 0

# ─────────────────────────────────────────────────────────────
# 1. TÉLÉCHARGEMENT
# ─────────────────────────────────────────────────────────────
def download_data():
    candidates = [("NQ=F", 1.0), ("^NDX", 1.0), ("QQQ", 40.0)]
    for ticker, conv in candidates:
        try:
            print(f"  📥 Téléchargement {ticker} [{INTERVAL}, {PERIOD}]...")
            df = yf.download(ticker, interval=INTERVAL, period=PERIOD,
                             progress=False, auto_adjust=True)
            if df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]
            df = df[['open', 'high', 'low', 'close', 'volume']].copy()
            df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
            df = df[df['close'] > 0].copy()
            print(f"  ✅ {ticker} : {len(df)} bougies ({df.index[0].date()} → {df.index[-1].date()})  [conv={conv}×]")
            return df, conv, ticker
        except Exception as e:
            print(f"  ❌ {ticker} : {e}")
    raise RuntimeError("Impossible de télécharger les données.")


# ─────────────────────────────────────────────────────────────
# 2. FILTRE RTH
# ─────────────────────────────────────────────────────────────
def filter_rth(df):
    try:
        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize('UTC')
        idx_et = idx.tz_convert('America/New_York')
        in_rth = (
            (idx_et.dayofweek < 5) &
            ((idx_et.hour > RTH_START_H) |
             ((idx_et.hour == RTH_START_H) & (idx_et.minute >= RTH_START_M))) &
            (idx_et.hour < RTH_END_H)
        )
        mask = np.asarray(in_rth, dtype=bool)
        df_rth = df.iloc[mask].copy()
        print(f"  🕐 Filtre RTH : {len(df_rth)} barres conservées ({len(df) - len(df_rth)} hors-session retirées)")
        return df_rth
    except Exception as e:
        print(f"  ⚠️  Filtre RTH impossible ({e}) — toutes barres conservées")
        return df


# ─────────────────────────────────────────────────────────────
# 3. ADX
# ─────────────────────────────────────────────────────────────
def compute_adx(df, period=14):
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
    dm_plus  = dm_plus.where((dm_plus > dm_minus) & (dm_plus > 0), 0.0)
    dm_minus = dm_minus.where((dm_minus > dm_plus) & (dm_minus > 0), 0.0)
    atr      = tr.ewm(span=period, adjust=False).mean()
    di_plus  = 100 * dm_plus.ewm(span=period, adjust=False).mean() / atr
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / atr
    denom    = (di_plus + di_minus).replace(0, np.nan)
    dx       = (100 * (di_plus - di_minus).abs() / denom).fillna(0)
    adx      = dx.ewm(span=period, adjust=False).mean()
    return adx


# ─────────────────────────────────────────────────────────────
# 4. DÉTECTION ÉPUISEMENT
# ─────────────────────────────────────────────────────────────
def detect_exhaustion(df, conv=1.0):
    df = df.copy()
    df['body']     = (df['close'] - df['open']).abs()
    df['is_green'] = df['close'] > df['open']
    df['is_red']   = df['close'] < df['open']
    ph, pl, pig, pir = df['high'].shift(1), df['low'].shift(1), df['is_green'].shift(1), df['is_red'].shift(1)
    cond_short = df['is_green'] & pir & (df['high'] > ph) & (df['low'] > pl)
    cond_long  = df['is_red']   & pig & (df['low'] < pl)  & (df['high'] < ph)
    df['bias'] = 0
    df.loc[cond_long,  'bias'] = 1
    df.loc[cond_short, 'bias'] = -1
    df['signal_pts']   = df['body'] * conv
    df['signal_valid'] = (
        (df['bias'] != 0) &
        (df['signal_pts'] >= SL_MIN_PTS) &
        (df['signal_pts'] <= SL_MAX_PTS)
    )
    return df


# ─────────────────────────────────────────────────────────────
# 5. INDÉCISION
# ─────────────────────────────────────────────────────────────
def is_indecision(row, body_pct=BODY_PCT):
    total_range = row['high'] - row['low']
    if total_range <= 0:
        return False
    body       = abs(row['close'] - row['open'])
    upper_wick = row['high'] - max(row['open'], row['close'])
    lower_wick = min(row['open'], row['close']) - row['low']
    return (body < body_pct * total_range) and (upper_wick > 0) and (lower_wick > 0)


# ─────────────────────────────────────────────────────────────
# 6. BACKTEST PRINCIPAL
# ─────────────────────────────────────────────────────────────
IDLE = 0; WAIT_INDECISION = 1; WAIT_BREAKOUT = 2; IN_POSITION = 3

def run_backtest(df, conv=1.0):
    adx_series = compute_adx(df, period=14)
    df = detect_exhaustion(df, conv=conv)
    df = df.dropna(subset=['bias']).copy()
    adx_aligned = adx_series.reindex(df.index).fillna(0)
    df['adx'] = adx_aligned.values
    n = len(df)

    account   = ACCOUNT_SIZE
    peak_acct = ACCOUNT_SIZE
    halted    = False

    daily_pnl     = 0.0
    daily_stopped = False
    current_date  = None
    n_daily_limit = 0
    n_trailing_dd = 0

    state      = IDLE
    bias       = 0
    sl_pts     = 0.0
    wait_count = 0
    indc_high = indc_low = entry_price = sl_price = tp_price = 0.0
    entry_time = None
    entry_bar  = 0
    entry_adx  = 0.0

    trades       = []
    equity_curve = []
    daily_stats  = []
    n_no_trade_days  = 0  # Jours sans trade
    days_with_trades = set()

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
                daily_stats.append({'date': current_date, 'pnl': daily_pnl, 'stopped': daily_stopped})
            current_date  = bar_date
            daily_pnl     = 0.0
            daily_stopped = False

        trading_allowed  = (not halted) and (not daily_stopped)
        force_close_time = (bar_hour == 15 and bar_minute >= 50) or bar_hour >= 16

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
            hit_sl = hit_tp = False
            exit_price_cur = row['close']
            raison = 'En cours'

            if bias == 1:
                if row['low'] <= sl_price:
                    hit_sl = True; exit_price_cur = sl_price
                elif row['high'] >= tp_price:
                    hit_tp = True; exit_price_cur = tp_price
            else:
                if row['high'] >= sl_price:
                    hit_sl = True; exit_price_cur = sl_price
                elif row['low'] <= tp_price:
                    hit_tp = True; exit_price_cur = tp_price

            opp = ((bias == 1 and row['signal_valid'] and row['bias'] == -1) or
                   (bias == -1 and row['signal_valid'] and row['bias'] == 1))
            if not hit_sl and not hit_tp and opp:
                exit_price_cur = row['close']
                raison = 'Signal opposé'

            force_close = (not hit_sl and not hit_tp and raison != 'Signal opposé' and
                           (force_close_time or next_is_new_day))
            if force_close:
                exit_price_cur = row['close']
                raison = '15h50 ET' if force_close_time else 'Fin session'

            if hit_sl or hit_tp or opp or force_close:
                if hit_sl:  raison = 'SL'
                elif hit_tp: raison = 'TP'

                pnl_pts = ((exit_price_cur - entry_price) if bias == 1 else
                           (entry_price - exit_price_cur)) * conv
                pnl_usd = pnl_pts * POINT_VALUE * CONTRACTS - FRAIS_RT
                account   += pnl_usd
                daily_pnl += pnl_usd
                days_with_trades.add(bar_date)

                prev_peak = peak_acct
                if account > peak_acct:
                    peak_acct = account
                dd = account - peak_acct
                if dd <= MAX_DD_LIMIT and not halted:
                    halted = True
                    n_trailing_dd += 1
                if daily_pnl <= DAILY_LOSS_LIMIT:
                    daily_stopped = True

                trades.append({
                    'trade_id':     len(trades) + 1,
                    'entry_time':   entry_time,
                    'exit_time':    timestamp,
                    'direction':    'LONG' if bias == 1 else 'SHORT',
                    'entry_price':  entry_price,
                    'sl_price':     sl_price,
                    'tp_price':     tp_price,
                    'exit_price':   exit_price_cur,
                    'exit_reason':  raison,
                    'sl_pts':       sl_pts,
                    'pnl_pts':      pnl_pts,
                    'pnl_usd':      pnl_usd,
                    'account':      account,
                    'daily_pnl':    daily_pnl,
                    'adx_entry':    entry_adx,
                })
                state = IDLE; bias = 0; wait_count = 0

        # ── WAIT_BREAKOUT ──
        elif state == WAIT_BREAKOUT and trading_allowed and not force_close_time:
            entered = False
            if bias == -1 and row['close'] < indc_low:
                entry_price = row['close']
                sl_price    = entry_price + (sl_pts / conv)
                tp_price    = entry_price - (sl_pts * RR_RATIO / conv)
                entered     = True
            elif bias == 1 and row['close'] > indc_high:
                entry_price = row['close']
                sl_price    = entry_price - (sl_pts / conv)
                tp_price    = entry_price + (sl_pts * RR_RATIO / conv)
                entered     = True

            if entered:
                entry_time = timestamp; entry_bar = i; entry_adx = adx_val
                state = IN_POSITION; wait_count = 0
            else:
                wait_count += 1
                if wait_count > MAX_WAIT or next_is_new_day or force_close_time:
                    state = IDLE; bias = 0; wait_count = 0

        # ── WAIT_INDECISION ──
        elif state == WAIT_INDECISION and trading_allowed and not force_close_time:
            if is_indecision(row):
                indc_high = row['high']; indc_low = row['low']
                state = WAIT_BREAKOUT; wait_count = 0
            else:
                wait_count += 1
                if wait_count > MAX_WAIT or next_is_new_day or force_close_time:
                    state = IDLE; bias = 0; wait_count = 0

        # ── IDLE — chercher signal + filtre ADX ──
        if state == IDLE and trading_allowed and not force_close_time:
            if row['signal_valid'] and adx_val < ADX_THRESHOLD:
                bias = int(row['bias']); sl_pts = float(row['signal_pts'])
                state = WAIT_INDECISION; wait_count = 0

        equity_curve.append({'time': timestamp, 'account': account, 'adx': adx_val})

    # Fermer position ouverte en fin de données
    if state == IN_POSITION:
        exit_price_cur = df.iloc[-1]['close']
        pnl_pts = ((exit_price_cur - entry_price) if bias == 1 else
                   (entry_price - exit_price_cur)) * conv
        pnl_usd = pnl_pts * POINT_VALUE * CONTRACTS - FRAIS_RT / 2
        account += pnl_usd
        trades.append({
            'trade_id': len(trades) + 1,
            'entry_time': entry_time, 'exit_time': df.index[-1],
            'direction': 'LONG' if bias == 1 else 'SHORT',
            'entry_price': entry_price, 'sl_price': sl_price,
            'tp_price': tp_price, 'exit_price': exit_price_cur,
            'exit_reason': 'Fin données', 'sl_pts': sl_pts,
            'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd,
            'account': account, 'daily_pnl': daily_pnl, 'adx_entry': 0.0,
        })

    if current_date is not None:
        if daily_stopped:
            n_daily_limit += 1
        daily_stats.append({'date': current_date, 'pnl': daily_pnl, 'stopped': daily_stopped})

    eq_df = pd.DataFrame(equity_curve).set_index('time') if equity_curve else pd.DataFrame()

    return {
        'trades':        trades,
        'equity_curve':  eq_df,
        'daily_stats':   daily_stats,
        'n_daily_limit': n_daily_limit,
        'n_trailing_dd': n_trailing_dd,
        'halted':        halted,
        'adx_series':    adx_series,
        'final_account': account,
        'days_with_trades': days_with_trades,
    }


# ─────────────────────────────────────────────────────────────
# 7. RAPPORT CONSOLE
# ─────────────────────────────────────────────────────────────
def print_report(result, n_trading_days):
    trades       = result['trades']
    daily_stats  = result['daily_stats']
    n_dl         = result['n_daily_limit']
    n_trailing_dd = result['n_trailing_dd']
    halted       = result['halted']
    eq           = result['equity_curve']
    final_acct   = result['final_account']
    days_with    = result['days_with_trades']

    pnls      = [t['pnl_usd'] for t in trades]
    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate  = len(wins) / max(1, len(pnls)) * 100
    pf        = (sum(wins) / abs(sum(losses))) if losses and wins else (float('inf') if wins else 0)
    avg_trade = float(np.mean(pnls)) if pnls else 0
    best_t    = max(pnls) if pnls else 0
    worst_t   = min(pnls) if pnls else 0

    # Sharpe
    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252)
    else:
        sharpe = 0.0

    # Max drawdown
    max_dd = 0.0
    peak   = ACCOUNT_SIZE
    for e in (eq['account'].values if not eq.empty else []):
        if e > peak:
            peak = e
        dd = e - peak
        if dd < max_dd:
            max_dd = dd
    max_dd_pct = abs(max_dd) / ACCOUNT_SIZE * 100

    # Risque moyen
    sl_pts_list = [t['sl_pts'] for t in trades]
    avg_sl_pts  = float(np.mean(sl_pts_list)) if sl_pts_list else 0

    # Jours
    n_with  = len(days_with)
    n_without = n_trading_days - n_with

    # PnL par jour
    daily_pnls = [d['pnl'] for d in daily_stats if d['pnl'] != 0 or d['date'] in days_with]
    avg_pnl_day = total_pnl / max(1, n_with)

    # Meilleurs/pires jours
    ds_sorted = sorted(daily_stats, key=lambda x: x['pnl'], reverse=True)
    top3  = [d for d in ds_sorted[:3]]
    bot3  = [d for d in ds_sorted[-3:]]

    # Dates période
    if trades:
        t0 = trades[0]['entry_time']
        t1 = trades[-1]['exit_time']
        try:
            d0 = t0.strftime('%d/%m/%Y') if hasattr(t0, 'strftime') else str(t0)[:10]
            d1 = t1.strftime('%d/%m/%Y') if hasattr(t1, 'strftime') else str(t1)[:10]
        except Exception:
            d0 = str(t0)[:10]; d1 = str(t1)[:10]
    else:
        d0 = d1 = '?'

    # VERDICT
    if avg_pnl_day >= 1000:
        verdict = "STRATÉGIE VIABLE ✅"
        obj_atteint = "OUI"
    elif avg_pnl_day >= 500:
        verdict = "POTENTIELLEMENT VIABLE ⚠️"
        obj_atteint = "NON"
    else:
        verdict = "NON VIABLE ❌"
        obj_atteint = "NON"

    # ── AFFICHAGE ──
    print()
    print("=" * 60)
    print("BACKTEST FINAL — NQ 5M | ADX=20 | 2 CONTRATS (Apex 100k$)")
    print("=" * 60)
    print(f"Période     : {d0} → {d1} ({n_trading_days} jours de trading)")
    print(f"Paramètres  : body_pct={BODY_PCT} | rr={RR_RATIO} | max_wait={MAX_WAIT} | ADX≤{ADX_THRESHOLD}")
    print()
    print("📊 RÉSUMÉ PERFORMANCE")
    print(f"Total trades          : {len(trades)}")
    print(f"Wins / Losses         : {len(wins)} / {len(losses)}")
    print(f"Win Rate              : {win_rate:.1f}%")
    print(f"Profit Factor         : {pf:.2f}")
    print(f"Sharpe (annualisé)    : {sharpe:.2f}")
    sign = '+' if total_pnl >= 0 else ''
    print(f"P&L Total             : {sign}{total_pnl:,.0f}$")
    sign_day = '+' if avg_pnl_day >= 0 else ''
    print(f"P&L Moyen/jour        : {sign_day}{avg_pnl_day:.0f}$/jour")
    sign_tr = '+' if avg_trade >= 0 else ''
    print(f"P&L Moyen/trade       : {sign_tr}{avg_trade:.0f}$/trade")
    sign_b = '+' if best_t >= 0 else ''
    print(f"Meilleur trade        : {sign_b}{best_t:,.0f}$")
    print(f"Pire trade            : {worst_t:,.0f}$")
    print()
    print("📉 RISQUE")
    print(f"Risque moyen/trade    : {avg_sl_pts * POINT_VALUE * CONTRACTS:.0f}$ ({avg_sl_pts:.1f} pts NQ × 20$ × 2)")
    print(f"Risque max/trade      : {SL_MAX_PTS * POINT_VALUE * CONTRACTS:.0f}$ (30 pts × 20$ × 2 = 1 200$)")
    print(f"Max Drawdown          : {max_dd:,.0f}$")
    print(f"Max DD % compte       : {max_dd_pct:.1f}%")
    print()
    print("🏦 APEX 100k$ — RÈGLES RESPECTÉES")
    pct_dl = n_dl / max(1, n_trading_days) * 100
    print(f"Daily loss limit      : -2 000$ → Touché {n_dl} fois ({pct_dl:.0f}%)")
    print(f"Max drawdown trailing : -8 000$ → Touché {n_trailing_dd} fois")
    print(f"Jours sans trade      : {n_without} (ADX trop élevé / aucun signal)")
    print(f"Jours avec 1+ trade   : {n_with}")
    print()
    top3_str = " | ".join([f"{'+' if d['pnl']>=0 else ''}{d['pnl']:,.0f}$ ({d['date']})" for d in top3])
    bot3_str = " | ".join([f"{d['pnl']:,.0f}$ ({d['date']})" for d in bot3])
    print(f"📅 MEILLEURS JOURS : {top3_str}")
    print(f"📅 PIRES JOURS    : {bot3_str}")
    print("=" * 60)
    print(f"VERDICT : {verdict}")
    print(f"Objectif 1000$/j atteint : {obj_atteint} (moyenne actuelle : {sign_day}{avg_pnl_day:.0f}$/j)")
    print("=" * 60)

    return {
        'win_rate': win_rate, 'pf': pf, 'sharpe': sharpe,
        'total_pnl': total_pnl, 'avg_pnl_day': avg_pnl_day,
        'avg_trade': avg_trade, 'best_t': best_t, 'worst_t': worst_t,
        'avg_sl_pts': avg_sl_pts, 'max_dd': max_dd, 'max_dd_pct': max_dd_pct,
        'wins': len(wins), 'losses': len(losses), 'n_trades': len(trades),
        'verdict': verdict, 'obj_atteint': obj_atteint,
        'n_without': n_without, 'n_with': n_with,
        'd0': d0, 'd1': d1, 'top3': top3, 'bot3': bot3,
        'n_daily_limit': n_dl, 'n_trailing_dd': n_trailing_dd,
    }


# ─────────────────────────────────────────────────────────────
# 8. JOURNAL DES TRADES (console)
# ─────────────────────────────────────────────────────────────
def print_trade_journal(trades):
    print()
    print("─" * 95)
    print(f"{'#':>3} | {'Date':10} | {'Heure':5} | {'Dir':5} | {'Entry':>8} | {'SL':>8} | {'TP':>8} | {'Exit':>8} | {'PnL$':>8} | Résultat")
    print("─" * 95)
    for t in trades:
        et = t['entry_time']
        try:
            if hasattr(et, 'tz_convert'):
                et_ny = et.tz_convert('America/New_York')
            else:
                et_ny = et
            date_str = et_ny.strftime('%Y-%m-%d')
            hour_str = et_ny.strftime('%H:%M')
        except Exception:
            date_str = str(et)[:10]
            hour_str = str(et)[11:16]

        pnl = t['pnl_usd']
        sign = '+' if pnl >= 0 else ''
        result = 'TP ✅' if t['exit_reason'] == 'TP' else ('SL ❌' if t['exit_reason'] == 'SL' else t['exit_reason'])
        dir_str = t['direction'][:5]
        print(f"{t['trade_id']:>3} | {date_str:10} | {hour_str:5} | {dir_str:5} | "
              f"{t['entry_price']:>8.0f} | {t['sl_price']:>8.0f} | {t['tp_price']:>8.0f} | "
              f"{t['exit_price']:>8.0f} | {sign}{pnl:>7,.0f}$ | {result}")
    print("─" * 95)


# ─────────────────────────────────────────────────────────────
# 9. EXPORT CSV
# ─────────────────────────────────────────────────────────────
def export_csv(trades, output_path):
    rows = []
    for t in trades:
        et = t['entry_time']
        try:
            if hasattr(et, 'tz_convert'):
                et_ny = et.tz_convert('America/New_York')
            else:
                et_ny = et
            date_str = et_ny.strftime('%Y-%m-%d')
            time_str = et_ny.strftime('%H:%M')
        except Exception:
            date_str = str(et)[:10]
            time_str = str(et)[11:16]

        rows.append({
            'trade_id':    t['trade_id'],
            'date':        date_str,
            'time':        time_str,
            'direction':   t['direction'],
            'entry':       round(t['entry_price'], 2),
            'sl':          round(t['sl_price'], 2),
            'tp':          round(t['tp_price'], 2),
            'exit_price':  round(t['exit_price'], 2),
            'exit_reason': t['exit_reason'],
            'pnl_usd':     round(t['pnl_usd'], 2),
            'pnl_pts':     round(t['pnl_pts'], 2),
            'sl_pts':      round(t['sl_pts'], 2),
            'adx_at_entry': round(t.get('adx_entry', 0), 2),
        })
    df_csv = pd.DataFrame(rows)
    df_csv.to_csv(output_path, index=False)
    print(f"\n✅ CSV sauvegardé : {output_path} ({len(rows)} trades)")


# ─────────────────────────────────────────────────────────────
# 10. RAPPORT VISUEL 5 PANNEAUX
# ─────────────────────────────────────────────────────────────
def generate_final_report(result, stats, n_trading_days, output_path):
    C = dict(
        bg='#0d1117', panel='#161b22', text='#e6edf3',
        green='#3fb950', red='#f85149', blue='#58a6ff',
        gold='#d29922', grey='#8b949e', orange='#f0883e',
        purple='#bc8cff', cyan='#39d353',
    )

    trades      = result['trades']
    eq          = result['equity_curve']
    daily_stats = result['daily_stats']
    adx_series  = result['adx_series']

    fig = plt.figure(figsize=(22, 22))
    fig.patch.set_facecolor(C['bg'])
    gs  = gridspec.GridSpec(5, 1, figure=fig, hspace=0.55,
                            height_ratios=[2, 1.5, 1.5, 2, 1.8])

    def style_ax(ax, title):
        ax.set_facecolor(C['panel'])
        ax.tick_params(colors=C['text'], labelsize=8)
        for spine in ax.spines.values():
            spine.set_color('#30363d')
        ax.xaxis.label.set_color(C['text'])
        ax.yaxis.label.set_color(C['text'])
        ax.set_title(title, fontsize=11, fontweight='bold', pad=8, color=C['text'])

    # ── Panneau 1 : Equity Curve ─────────────────────────────
    ax1 = fig.add_subplot(gs[0])
    style_ax(ax1, '📈 Equity Curve — 100 000$ de départ (Apex 100k$)')

    if not eq.empty and 'account' in eq.columns:
        accs = eq['account'].values
        times = eq.index

        # Zone verte/rouge par rapport à 100k
        for k in range(len(times) - 1):
            color = '#0d2b0d' if accs[k] >= ACCOUNT_SIZE else '#2b0d0d'
            ax1.axvspan(times[k], times[k+1], alpha=0.3, color=color, linewidth=0)

        ax1.plot(times, accs, color=C['blue'], linewidth=2, label='Equity', zorder=3)
        ax1.fill_between(times, ACCOUNT_SIZE, accs,
                         where=(accs >= ACCOUNT_SIZE), alpha=0.15, color=C['green'])
        ax1.fill_between(times, ACCOUNT_SIZE, accs,
                         where=(accs < ACCOUNT_SIZE), alpha=0.15, color=C['red'])

    ax1.axhline(y=ACCOUNT_SIZE,           color=C['grey'],  linestyle=':',  linewidth=1, label='100k$ initial')
    ax1.axhline(y=ACCOUNT_SIZE + MAX_DD_LIMIT, color=C['red'], linestyle='--', linewidth=1.2,
                label=f'Max DD -8% ({ACCOUNT_SIZE+MAX_DD_LIMIT:,.0f}$)')
    ax1.axhline(y=ACCOUNT_SIZE + 10000,   color=C['green'], linestyle='--', linewidth=1.0,
                label='Objectif Apex +10% (110k$)')
    ax1.set_ylabel('Compte (USD)', color=C['text'])
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
    ax1.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8, loc='upper left')

    # ── Panneau 2 : PnL par jour ─────────────────────────────
    ax2 = fig.add_subplot(gs[1])
    style_ax(ax2, '📊 PnL par Jour (barres vertes = profit, rouges = perte)')

    if daily_stats:
        pnls_d = [d['pnl'] for d in daily_stats]
        colors_d = [C['green'] if p >= 0 else C['red'] for p in pnls_d]
        ax2.bar(range(len(pnls_d)), pnls_d, color=colors_d, alpha=0.85, width=0.8)
        ax2.axhline(y=0, color=C['grey'], linewidth=0.8)
        ax2.axhline(y=1000, color=C['cyan'], linestyle='--', linewidth=1, alpha=0.8,
                    label='Objectif +1000$/j')
        ax2.axhline(y=DAILY_LOSS_LIMIT, color=C['orange'], linestyle=':', linewidth=1,
                    label='Daily limit -2000$')
        ax2.set_ylabel('PnL ($)', color=C['text'])
        ax2.set_xlabel('Jours de trading', color=C['text'])
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))
        ax2.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)

    # ── Panneau 3 : Distribution PnL par trade ───────────────
    ax3 = fig.add_subplot(gs[2])
    style_ax(ax3, '🎯 Distribution des PnL par Trade (histogramme)')

    if trades:
        pnls_t = [t['pnl_usd'] for t in trades]
        wins_t = [p for p in pnls_t if p > 0]
        loss_t = [p for p in pnls_t if p <= 0]
        bins   = min(30, max(8, len(pnls_t) // 3))

        if loss_t:
            ax3.hist(loss_t, bins=max(4, bins//2), color=C['red'], alpha=0.75, label=f'Pertes ({len(loss_t)})')
        if wins_t:
            ax3.hist(wins_t, bins=max(4, bins//2), color=C['green'], alpha=0.75, label=f'Gains ({len(wins_t)})')

        ax3.axvline(x=0, color='white', linestyle='--', linewidth=1)
        if pnls_t:
            ax3.axvline(x=float(np.mean(pnls_t)), color=C['gold'], linewidth=1.5,
                        label=f"Moyenne: ${float(np.mean(pnls_t)):.0f}")
        ax3.set_xlabel('PnL par trade ($)', color=C['text'])
        ax3.set_ylabel('Fréquence', color=C['text'])
        ax3.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8)

    # ── Panneau 4 : ADX + points d'entrée ───────────────────
    ax4 = fig.add_subplot(gs[3])
    style_ax(ax4, f'📉 ADX(14) au fil du temps | Seuil ADX={ADX_THRESHOLD} | vert=LONG, rouge=SHORT')

    adx_plot = adx_series
    if not eq.empty and 'adx' in eq.columns:
        adx_plot = eq['adx'].dropna()

    if len(adx_plot) > 0:
        adx_idx  = adx_plot.index
        adx_vals = adx_plot.values
        in_range = adx_vals < ADX_THRESHOLD

        for k in range(len(adx_idx) - 1):
            color = '#1a3a1a' if in_range[k] else '#3a1a1a'
            ax4.axvspan(adx_idx[k], adx_idx[k+1], alpha=0.25, color=color, linewidth=0)

        ax4.plot(adx_idx, adx_vals, color=C['blue'], linewidth=1, alpha=0.9, label='ADX(14)')
        ax4.axhline(y=ADX_THRESHOLD, color=C['gold'], linestyle='--', linewidth=1.2,
                    label=f'Seuil ADX={ADX_THRESHOLD} (range)')

        # Points d'entrée
        for t in trades:
            try:
                et    = t['entry_time']
                adx_t = float(adx_plot.asof(et)) if hasattr(adx_plot, 'asof') else ADX_THRESHOLD - 1
                col   = C['green'] if t['direction'] == 'LONG' else C['red']
                ax4.scatter(et, adx_t, color=col, s=30, zorder=5, alpha=0.9)
            except Exception:
                pass

        pct_range = (in_range.sum() / max(1, len(in_range))) * 100
        ax4.set_ylabel(f'ADX  ({pct_range:.0f}% temps en range)', color=C['text'])
        ax4.set_ylim(0, max(60, float(np.nanmax(adx_vals)) * 1.1))
        green_patch = mpatches.Patch(color=C['green'], label='Entrée LONG')
        red_patch   = mpatches.Patch(color=C['red'],   label='Entrée SHORT')
        ax4.legend(handles=[ax4.lines[0], ax4.lines[1], green_patch, red_patch],
                   facecolor=C['panel'], labelcolor=C['text'], fontsize=7.5)

    # ── Panneau 5 : Tableau récapitulatif avec VERDICT ───────
    ax5 = fig.add_subplot(gs[4])
    ax5.set_facecolor(C['panel'])
    ax5.axis('off')
    ax5.set_title('📋 Tableau Récapitulatif — VERDICT FINAL', fontsize=11,
                  fontweight='bold', pad=8, color=C['text'])

    # Organiser les données en 2 colonnes
    s = stats
    col1 = [
        ('PERFORMANCE',              '',                          C['blue']),
        ('Total trades',             str(s['n_trades']),         None),
        ('Wins / Losses',            f"{s['wins']} / {s['losses']}", None),
        ('Win Rate',                 f"{s['win_rate']:.1f}%",   None),
        ('Profit Factor',            f"{s['pf']:.2f}",          None),
        ('Sharpe annualisé',         f"{s['sharpe']:.2f}",      None),
        ('P&L Total',                f"${s['total_pnl']:+,.0f}", C['green'] if s['total_pnl'] >= 0 else C['red']),
        ('P&L Moyen/jour',           f"${s['avg_pnl_day']:+.0f}",
                                     C['green'] if s['avg_pnl_day'] >= 1000 else C['orange']),
        ('P&L Moyen/trade',          f"${s['avg_trade']:+.0f}", None),
        ('Meilleur trade',           f"${s['best_t']:+,.0f}",   C['green']),
        ('Pire trade',               f"${s['worst_t']:,.0f}",   C['red']),
    ]
    col2 = [
        ('RISQUE & APEX',            '',                         C['orange']),
        ('Risque moyen/trade',       f"${s['avg_sl_pts'] * POINT_VALUE * CONTRACTS:.0f}$ ({s['avg_sl_pts']:.1f} pts × 2)", None),
        ('Risque max/trade',         f"$1 200 (30 pts × 20$ × 2)", None),
        ('Max Drawdown',             f"${s['max_dd']:,.0f}",    C['red'] if abs(s['max_dd']) > 5000 else C['text']),
        ('Max DD %',                 f"{s['max_dd_pct']:.1f}%", None),
        ('Daily limit touché',       f"{s['n_daily_limit']} fois", None),
        ('Trailing DD touché',       f"{s['n_trailing_dd']} fois", None),
        ('Jours sans trade',         str(s['n_without']),       None),
        ('Jours avec trade',         str(s['n_with']),          None),
        ('PARAMÈTRES',               '',                        C['purple']),
        ('ADX threshold',            str(ADX_THRESHOLD),        None),
        ('body_pct / rr / wait',     f"{BODY_PCT} / {RR_RATIO} / {MAX_WAIT}", None),
    ]

    def draw_col(ax, rows, x_label, x_val, start_y, line_h):
        y = start_y
        for label, value, forced_color in rows:
            if value == '':
                ax.text(x_label, y, label, transform=ax.transAxes,
                        color=forced_color or C['blue'], fontsize=9,
                        fontweight='bold', va='top')
                y -= line_h * 0.7
                ax.plot([x_label, x_label + 0.45], [y + line_h * 0.35, y + line_h * 0.35],
                        color='#30363d', linewidth=0.5, transform=ax.transAxes)
                continue
            vc = forced_color if forced_color else C['text']
            ax.text(x_label, y, label, transform=ax.transAxes,
                    color=C['grey'], fontsize=8.5, va='top')
            ax.text(x_val, y, str(value), transform=ax.transAxes,
                    color=vc, fontsize=8.5, va='top', fontweight='bold')
            y -= line_h

    lh = 0.062
    draw_col(ax5, col1, 0.02, 0.30, 0.96, lh)
    draw_col(ax5, col2, 0.52, 0.80, 0.96, lh)

    # VERDICT en grand
    verdict_col = C['green'] if '✅' in s['verdict'] else (C['orange'] if '⚠️' in s['verdict'] else C['red'])
    ax5.text(0.5, 0.05, f"VERDICT : {s['verdict']}", transform=ax5.transAxes,
             color=verdict_col, fontsize=14, fontweight='bold',
             ha='center', va='bottom',
             bbox=dict(boxstyle='round,pad=0.5', facecolor='#0d1117',
                       edgecolor=verdict_col, linewidth=2))

    from datetime import datetime
    fig.suptitle(
        f'BACKTEST FINAL NQ 5M | ADX={ADX_THRESHOLD} | 2 CONTRATS | Apex 100k$  |  '
        f'{s["d0"]} → {s["d1"]}  |  '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=12, fontweight='bold', color=C['text'], y=0.995,
    )

    plt.savefig(output_path, dpi=150, bbox_inches='tight',
                facecolor=C['bg'], edgecolor='none')
    plt.close()
    print(f"\n✅ Rapport visuel sauvegardé : {output_path}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("NQ FUTURES 5M — BACKTEST FINAL (Configuration JP)")
    print(f"ADX={ADX_THRESHOLD} | body_pct={BODY_PCT} | rr={RR_RATIO} | max_wait={MAX_WAIT} | {CONTRACTS} contrats")
    print("=" * 60)

    # 1. Téléchargement
    print("\n📥 Téléchargement des données...")
    df_raw, conv, ticker_used = download_data()

    # 2. Filtre RTH
    df = filter_rth(df_raw)

    if len(df) < 50:
        print("❌ Données insuffisantes après filtre RTH.")
        sys.exit(1)

    # Nombre de jours de trading
    try:
        idx = df.index
        if idx.tz is None:
            idx_et = idx.tz_localize('UTC').tz_convert('America/New_York')
        else:
            idx_et = idx.tz_convert('America/New_York')
        n_trading_days = len(set(t.date() for t in idx_et))
    except Exception:
        n_trading_days = max(1, len(df) // 78)

    print(f"\nDonnées : {ticker_used} 5M | {n_trading_days} jours RTH | {len(df)} barres")

    # 3. Backtest
    print(f"\n🔄 Backtest en cours avec ADX≤{ADX_THRESHOLD}, {CONTRACTS} contrats...")
    result = run_backtest(df, conv=conv)

    if not result['trades']:
        print("❌ Aucun trade généré. Vérifiez les données.")
        sys.exit(1)

    # 4. Rapport console
    stats = print_report(result, n_trading_days)

    # 5. Journal des trades
    print_trade_journal(result['trades'])

    # 6. Export CSV
    os.makedirs('trading', exist_ok=True)
    export_csv(result['trades'], 'trading/nasdaq_trades_final.csv')

    # 7. Rapport visuel
    print("\n🖼  Génération du rapport visuel...")
    generate_final_report(result, stats, n_trading_days, 'trading/nasdaq_final_2c.png')


if __name__ == '__main__':
    main()
