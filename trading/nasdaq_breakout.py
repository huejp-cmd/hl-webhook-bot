"""
NQ 5M — Breakout Retournement (sans ADX)
Apex 100k$ | SL=TP fixe symétrique
"""

import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import FancyBboxPatch
import warnings
warnings.filterwarnings('ignore')

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
TICKER          = "NQ=F"
INTERVAL        = "5m"
PERIOD          = "60d"
POINT_VALUE     = 20        # $/point NQ
ACCOUNT_SIZE    = 100_000   # Apex 100k$
DAILY_LOSS_LIMIT = -2000
MAX_DD_LIMIT    = -8000
SL_MIN_PTS      = 10
SL_MAX_PTS      = 30
BREAKOUT_PTS    = 2         # pts de confirmation (fixe)
MAX_WAIT_BARS   = 3         # max candles d'attente après signal

SL_TP_VALUES    = [8, 10, 12, 15, 18, 20, 25]
CONTRACTS_LIST  = [1, 2]

RTH_START = "09:30"
RTH_END   = "15:50"
FORCE_EXIT_TIME = "15:50"

# ──────────────────────────────────────────────
# 1. DONNÉES
# ──────────────────────────────────────────────
print("Téléchargement des données NQ=F 5M...")
raw = yf.download(TICKER, period=PERIOD, interval=INTERVAL, auto_adjust=True, progress=False)

if raw.empty:
    raise RuntimeError("Impossible de télécharger les données NQ=F")

# Flatten colonnes si MultiIndex
if isinstance(raw.columns, pd.MultiIndex):
    raw.columns = raw.columns.get_level_values(0)

raw.index = pd.to_datetime(raw.index)
if raw.index.tz is None:
    raw.index = raw.index.tz_localize('UTC')
raw.index = raw.index.tz_convert('America/New_York')

# Filtrer RTH uniquement
rth_mask = (
    (raw.index.time >= pd.Timestamp(RTH_START).time()) &
    (raw.index.time <= pd.Timestamp(RTH_END).time()) &
    (raw.index.dayofweek < 5)
)
df = raw[rth_mask].copy()
df = df.dropna(subset=['Open', 'High', 'Low', 'Close'])

n_days = df.index.normalize().nunique()
n_bars = len(df)

print(f"Données RTH : {n_days} jours | {n_bars} barres")

# ──────────────────────────────────────────────
# 2. DÉTECTION DES SIGNAUX
# ──────────────────────────────────────────────
df['body'] = (df['Close'] - df['Open']).abs()
df['is_red']   = df['Close'] < df['Open']   # candle rouge → signal LONG
df['is_green'] = df['Close'] > df['Open']   # candle verte → signal SHORT

body_ok = (df['body'] >= 10) & (df['body'] <= 30)

red_total   = int(df['is_red'].sum())
green_total = int(df['is_green'].sum())
red_valid   = int((df['is_red'] & body_ok).sum())
green_valid = int((df['is_green'] & body_ok).sum())
total_valid = red_valid + green_valid

print(f"Signaux rouges  : {red_total} total | {red_valid} valides (corps 10-30 pts)")
print(f"Signaux verts   : {green_total} total | {green_valid} valides (corps 10-30 pts)")
print(f"Total valides   : {total_valid} (~{total_valid/n_days:.1f}/jour)")

# ──────────────────────────────────────────────
# 3. BACKTEST CORE
# ──────────────────────────────────────────────
def run_backtest(df, sl_tp_pts, n_contracts):
    """
    Retourne un dict avec métriques + equity curve + trades.
    """
    pv = POINT_VALUE * n_contracts
    bars = df.reset_index()
    # Rename the datetime index column (may be 'Datetime' or 'index' depending on pandas version)
    ts_col = bars.columns[0]  # first column after reset_index is the timestamp
    n = len(bars)

    trades = []
    equity = 0.0
    equity_curve = []
    daily_pnl = {}
    max_eq = 0.0
    max_dd = 0.0

    in_position = False
    wait_signal = None   # dict : {type, entry_level, sl, tp, bars_left}
    trade_open = None

    i = 0
    while i < n:
        row = bars.iloc[i]
        ts   = row[ts_col]
        high = row['High']
        low  = row['Low']
        close = row['Close']
        open_ = row['Open']
        body  = abs(close - open_)
        date  = ts.date()

        # ── Sortie forcée 15h50 ──────────────────
        t = ts.time()
        force_exit_t = pd.Timestamp(FORCE_EXIT_TIME).time()
        if in_position and t >= force_exit_t:
            # sortie au close de la barre
            pos = trade_open
            if pos['type'] == 'LONG':
                pnl_pts = close - pos['entry']
            else:
                pnl_pts = pos['entry'] - close
            pnl = pnl_pts * pv
            equity += pnl
            trades.append({**pos, 'exit': close, 'pnl_pts': pnl_pts,
                           'pnl': pnl, 'exit_reason': 'FORCE_EXIT', 'date': date})
            daily_pnl[date] = daily_pnl.get(date, 0) + pnl
            in_position = False
            trade_open = None
            wait_signal = None
            equity_curve.append(equity)
            i += 1
            continue

        # ── Vérifier si on est en attente de déclenchement ──
        if wait_signal and not in_position:
            ws = wait_signal
            triggered = False
            if ws['type'] == 'LONG' and high >= ws['entry_level']:
                # déclenchement LONG
                entry = ws['entry_level']
                sl = entry - sl_tp_pts
                tp = entry + sl_tp_pts
                trade_open = {'type': 'LONG', 'entry': entry, 'sl': sl, 'tp': tp,
                              'entry_bar': i, 'date': date}
                in_position = True
                triggered = True
                wait_signal = None
            elif ws['type'] == 'SHORT' and low <= ws['entry_level']:
                # déclenchement SHORT
                entry = ws['entry_level']
                sl = entry + sl_tp_pts
                tp = entry - sl_tp_pts
                trade_open = {'type': 'SHORT', 'entry': entry, 'sl': sl, 'tp': tp,
                              'entry_bar': i, 'date': date}
                in_position = True
                triggered = True
                wait_signal = None
            else:
                ws['bars_left'] -= 1
                if ws['bars_left'] <= 0:
                    wait_signal = None

        # ── Gérer la position ouverte ──────────────
        hit_tp = hit_sl = False
        exit_price = None
        # Ne pas vérifier SL/TP sur la barre d'entrée (entrée au HIGH/LOW ≈ extrême de barre)
        if in_position and i > trade_open.get('entry_bar', i):
            pos = trade_open
            hit_tp = hit_sl = False
            if pos['type'] == 'LONG':
                if low <= pos['sl']:
                    hit_sl = True
                    exit_price = pos['sl']
                elif high >= pos['tp']:
                    hit_tp = True
                    exit_price = pos['tp']
            else:  # SHORT
                if high >= pos['sl']:
                    hit_sl = True
                    exit_price = pos['sl']
                elif low <= pos['tp']:
                    hit_tp = True
                    exit_price = pos['tp']

            if hit_tp or hit_sl:
                if pos['type'] == 'LONG':
                    pnl_pts = exit_price - pos['entry']
                else:
                    pnl_pts = pos['entry'] - exit_price
                pnl = pnl_pts * pv
                equity += pnl
                reason = 'TP' if hit_tp else 'SL'
                trades.append({**pos, 'exit': exit_price, 'pnl_pts': pnl_pts,
                               'pnl': pnl, 'exit_reason': reason, 'date': date})
                daily_pnl[date] = daily_pnl.get(date, 0) + pnl
                in_position = False
                trade_open = None

        # ── Chercher nouveau signal (si pas en position et pas en attente) ──
        if not in_position and wait_signal is None:
            body_valid = 10 <= body <= 30
            if body_valid:
                is_red   = close < open_
                is_green = close > open_
                if is_red:
                    # Signal LONG : cible = HIGH + 2
                    target = high + BREAKOUT_PTS
                    wait_signal = {'type': 'LONG', 'entry_level': target,
                                   'bars_left': MAX_WAIT_BARS}
                elif is_green:
                    # Signal SHORT : cible = LOW - 2
                    target = low - BREAKOUT_PTS
                    wait_signal = {'type': 'SHORT', 'entry_level': target,
                                   'bars_left': MAX_WAIT_BARS}

        # ── Max DD ────────────────────────────────
        if equity > max_eq:
            max_eq = equity
        dd = equity - max_eq
        if dd < max_dd:
            max_dd = dd

        equity_curve.append(equity)
        i += 1

    # ── Métriques ─────────────────────────────
    if not trades:
        return None

    tdf = pd.DataFrame(trades)
    total_trades = len(tdf)
    winners = tdf[tdf['pnl'] > 0]
    losers  = tdf[tdf['pnl'] < 0]
    win_rate = len(winners) / total_trades * 100 if total_trades > 0 else 0
    gross_profit = winners['pnl'].sum() if len(winners) > 0 else 0
    gross_loss   = abs(losers['pnl'].sum()) if len(losers) > 0 else 1e-9
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

    signals_per_day = total_trades / n_days
    if 3 <= signals_per_day <= 8:
        freq_bonus = 1.2
    elif 1 <= signals_per_day < 3:
        freq_bonus = 1.0
    else:
        freq_bonus = 0.7

    score = profit_factor * (win_rate / 100) * freq_bonus

    # Daily loss breaches
    dl_breaches = sum(1 for v in daily_pnl.values() if v < DAILY_LOSS_LIMIT)
    dd_pct = abs(max_dd) / ACCOUNT_SIZE * 100

    return {
        'sl_tp': sl_tp_pts,
        'contracts': n_contracts,
        'total_trades': total_trades,
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'total_pnl': equity,
        'max_dd': max_dd,
        'dd_pct': dd_pct,
        'signals_per_day': signals_per_day,
        'freq_bonus': freq_bonus,
        'score': score,
        'dl_breaches': dl_breaches,
        'equity_curve': equity_curve,
        'trades': tdf,
    }

# ──────────────────────────────────────────────
# 4. LANCER TOUS LES BACKTESTS
# ──────────────────────────────────────────────
results = {}
triggered_total = None

print("\nOptimisation en cours...")
for n_c in CONTRACTS_LIST:
    results[n_c] = []
    for sl_tp in SL_TP_VALUES:
        r = run_backtest(df, sl_tp, n_c)
        if r:
            results[n_c].append(r)
            if triggered_total is None:
                triggered_total = r['total_trades']  # approx avec 1c sl=premier

# Calcul signaux déclenchés (indépendant du SL/TP, utilisons les trades 1c sl=10)
r_ref = next((r for r in results[1] if r['sl_tp'] == 10), None)
triggered_est = r_ref['total_trades'] if r_ref else 0

print(f"Signaux déclenchés (~breakout atteint) : {triggered_est} (~{triggered_est/n_days:.1f}/jour)")

# ──────────────────────────────────────────────
# 5. AFFICHAGE CONSOLE
# ──────────────────────────────────────────────
print()
print("=" * 60)
print("NQ 5M — BREAKOUT RETOURNEMENT (sans ADX)")
print(f"Apex 100k$ | SL=TP fixe | {TICKER} | {n_days} jours RTH")
print("=" * 60)
print(f"Données : {n_days} jours | {n_bars} barres RTH")
print(f"Signaux rouges détectés : {red_total:3d} | validés (corps 10-30 pts) : {red_valid}")
print(f"Signaux verts  détectés : {green_total:3d} | validés (corps 10-30 pts) : {green_valid}")
print(f"Total signaux valides   : {total_valid} (~{total_valid/n_days:.1f}/jour)")
print(f"Signaux déclenchés (breakout +{BREAKOUT_PTS} pts atteint) : {triggered_est} (~{triggered_est/n_days:.1f}/jour)")

best_results = {}
for n_c in CONTRACTS_LIST:
    rows = sorted(results[n_c], key=lambda x: x['score'], reverse=True)
    best = rows[0] if rows else None
    best_results[n_c] = best

    print(f"\nTOP COMBINAISONS ({n_c} contrat{'s' if n_c > 1 else ''}) :")
    print(f"{'SL=TP':>6} | {'WR%':>6} | {'PF':>5} | {'P&L$':>8} | {'DD$':>8} | {'Trades':>7} | {'Signal/j':>9}")
    print("-" * 65)
    for r in sorted(results[n_c], key=lambda x: x['sl_tp']):
        pnl_str = f"+{r['total_pnl']:,.0f}" if r['total_pnl'] >= 0 else f"{r['total_pnl']:,.0f}"
        dd_str  = f"-{abs(r['max_dd']):,.0f}"
        print(f"  {r['sl_tp']:>4} | {r['win_rate']:>5.1f} | {r['profit_factor']:>4.2f} | {pnl_str:>8} | {dd_str:>8} | {r['total_trades']:>7} | {r['signals_per_day']:>9.1f}")

    if best:
        pnl_str = f"+{best['total_pnl']:,.0f}" if best['total_pnl'] >= 0 else f"{best['total_pnl']:,.0f}"
        print(f"\n🏆 MEILLEURE CONFIG {n_c} CONTRAT{'S' if n_c > 1 else ''} :")
        print(f"SL=TP={best['sl_tp']} pts | WR={best['win_rate']:.1f}% | PF={best['profit_factor']:.2f} | P&L={pnl_str}$ | DD=-{abs(best['max_dd']):,.0f}$")

print("\nVERDICT SÉCURITÉ APEX :")
dl_1 = best_results[1]['dl_breaches'] if best_results[1] else 0
dl_2 = best_results[2]['dl_breaches'] if best_results[2] else 0
dd1  = best_results[1]['dd_pct'] if best_results[1] else 0
dd2  = best_results[2]['dd_pct'] if best_results[2] else 0
print(f"Daily limit touché : {dl_1} fois (1c) | {dl_2} fois (2c)")
print(f"Max DD : {dd1:.1f}% (1c) | {dd2:.1f}% (2c)")
print("=" * 60)

# ──────────────────────────────────────────────
# 6. RAPPORT VISUEL
# ──────────────────────────────────────────────
fig = plt.figure(figsize=(16, 14), facecolor='#0d1117')
gs = gridspec.GridSpec(2, 2, figure=fig, hspace=0.4, wspace=0.35)
ax1 = fig.add_subplot(gs[0, 0])   # Equity curves
ax2 = fig.add_subplot(gs[0, 1])   # Heatmap WR × PF
ax3 = fig.add_subplot(gs[1, 0])   # Signaux/jour
ax4 = fig.add_subplot(gs[1, 1])   # Tableau final

dark_bg = '#0d1117'
card_bg = '#161b22'
text_col = '#e6edf3'
blue_col = '#58a6ff'
orange_col = '#f78166'
green_col = '#3fb950'
yellow_col = '#d29922'

for ax in [ax1, ax2, ax3, ax4]:
    ax.set_facecolor(card_bg)
    ax.tick_params(colors=text_col)
    ax.spines['bottom'].set_color('#30363d')
    ax.spines['top'].set_color('#30363d')
    ax.spines['left'].set_color('#30363d')
    ax.spines['right'].set_color('#30363d')
    ax.xaxis.label.set_color(text_col)
    ax.yaxis.label.set_color(text_col)
    ax.title.set_color(text_col)

# ── Panel 1 : Equity Curves ─────────────────
b1 = best_results[1]
b2 = best_results[2]
if b1 and b1['equity_curve']:
    ax1.plot(b1['equity_curve'], color=blue_col, linewidth=1.5,
             label=f"1c SL=TP={b1['sl_tp']}pts")
if b2 and b2['equity_curve']:
    ax2_curve = b2['equity_curve']
    ax1.plot(ax2_curve, color=orange_col, linewidth=1.5,
             label=f"2c SL=TP={b2['sl_tp']}pts")
ax1.axhline(0, color='#444', linewidth=0.8, linestyle='--')
ax1.set_title('📈 Equity Curves — Meilleures Configs', fontsize=11, fontweight='bold')
ax1.set_xlabel('Barres')
ax1.set_ylabel('P&L ($)')
ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
ax1.legend(facecolor=card_bg, edgecolor='#30363d', labelcolor=text_col, fontsize=9)
ax1.grid(True, alpha=0.2, color='#30363d')

# ── Panel 2 : Heatmap WR × PF ───────────────
sl_tp_labels = [str(v) for v in SL_TP_VALUES]
wr_vals_1  = [next((r['win_rate'] for r in results[1] if r['sl_tp'] == v), 0) for v in SL_TP_VALUES]
pf_vals_1  = [next((r['profit_factor'] for r in results[1] if r['sl_tp'] == v), 0) for v in SL_TP_VALUES]
scores_1   = [next((r['score'] for r in results[1] if r['sl_tp'] == v), 0) for v in SL_TP_VALUES]

x = np.arange(len(SL_TP_VALUES))
w = 0.35
bars_wr = ax2.bar(x - w/2, wr_vals_1, w, label='WR% (1c)', color=blue_col, alpha=0.85)
bars_pf = ax2.bar(x + w/2, [p * 20 for p in pf_vals_1], w, label='PF×20 (1c)', color=green_col, alpha=0.85)

# Colorer en fonction du score
for bar, sc in zip(bars_wr, scores_1):
    if sc == max(scores_1):
        bar.set_edgecolor(yellow_col)
        bar.set_linewidth(2.5)

ax2.set_xticks(x)
ax2.set_xticklabels(sl_tp_labels, color=text_col)
ax2.set_title('📊 WR% & PF par SL=TP (1 contrat)', fontsize=11, fontweight='bold')
ax2.set_xlabel('SL=TP (pts)')
ax2.legend(facecolor=card_bg, edgecolor='#30363d', labelcolor=text_col, fontsize=9)
ax2.grid(True, alpha=0.2, axis='y', color='#30363d')

# ── Panel 3 : Signaux/jour comparaison ──────
categories = ['Rouges\ndétectés', 'Rouges\nvalides', 'Verts\ndétectés', 'Verts\nvalides',
              'Total\nvalides', 'Déclenchés']
values_abs = [red_total, red_valid, green_total, green_valid, total_valid, triggered_est]
values_day = [v / n_days for v in values_abs]
colors_bar = [orange_col, blue_col, green_col, '#7ee787', yellow_col, '#f0883e']

bars3 = ax3.bar(categories, values_day, color=colors_bar, alpha=0.85, edgecolor='#30363d')
for bar, val in zip(bars3, values_day):
    ax3.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.05,
             f'{val:.1f}', ha='center', va='bottom', color=text_col, fontsize=8)
ax3.set_title('📡 Signaux par jour', fontsize=11, fontweight='bold')
ax3.set_ylabel('Signaux / jour')
ax3.tick_params(axis='x', labelsize=7.5)
ax3.grid(True, alpha=0.2, axis='y', color='#30363d')

# ── Panel 4 : Tableau récap ─────────────────
ax4.axis('off')
ax4.set_title('🏆 Résumé & Verdict Apex', fontsize=11, fontweight='bold')

def safe_pnl(r):
    if r and r['total_pnl'] >= 0:
        return f"+${r['total_pnl']:,.0f}"
    elif r:
        return f"-${abs(r['total_pnl']):,.0f}"
    return "N/A"

lines = []
lines.append(("Meilleure config 1 contrat :", ""))
if b1:
    lines.append((f"  SL=TP = {b1['sl_tp']} pts", ""))
    lines.append((f"  Win Rate = {b1['win_rate']:.1f}%", ""))
    lines.append((f"  Profit Factor = {b1['profit_factor']:.2f}", ""))
    lines.append((f"  P&L total = {safe_pnl(b1)}", ""))
    lines.append((f"  Max DD = -${abs(b1['max_dd']):,.0f} ({b1['dd_pct']:.1f}%)", ""))
    lines.append((f"  Trades = {b1['total_trades']} ({b1['signals_per_day']:.1f}/jour)", ""))
lines.append(("", ""))
lines.append(("Meilleure config 2 contrats :", ""))
if b2:
    lines.append((f"  SL=TP = {b2['sl_tp']} pts", ""))
    lines.append((f"  Win Rate = {b2['win_rate']:.1f}%", ""))
    lines.append((f"  Profit Factor = {b2['profit_factor']:.2f}", ""))
    lines.append((f"  P&L total = {safe_pnl(b2)}", ""))
    lines.append((f"  Max DD = -${abs(b2['max_dd']):,.0f} ({b2['dd_pct']:.1f}%)", ""))
    lines.append((f"  Trades = {b2['total_trades']} ({b2['signals_per_day']:.1f}/jour)", ""))
lines.append(("", ""))
lines.append(("VERDICT APEX :", ""))
lines.append((f"  Daily limit : {dl_1}x (1c) | {dl_2}x (2c)", ""))
lines.append((f"  Max DD : {dd1:.1f}% (1c) | {dd2:.1f}% (2c)", ""))

# Verdict couleur
safe_1c = dd1 < 8 and dl_1 == 0
safe_2c = dd2 < 8 and dl_2 == 0
if safe_1c:
    verdict = "✅ 1c : SAFE APEX"
    vcol = green_col
else:
    verdict = "⚠️ 1c : RISQUÉ"
    vcol = orange_col
lines.append((verdict, vcol))

y_pos = 0.97
for line, col in lines:
    color = col if col else text_col
    ax4.text(0.05, y_pos, line, transform=ax4.transAxes,
             color=color, fontsize=9, verticalalignment='top',
             fontfamily='monospace')
    y_pos -= 0.057

fig.suptitle(f'NQ 5M — Breakout Retournement (sans ADX) | {TICKER} | {n_days}j RTH',
             color=text_col, fontsize=13, fontweight='bold', y=0.98)

out_path = 'trading/nasdaq_breakout_report.png'
plt.savefig(out_path, dpi=150, bbox_inches='tight', facecolor=dark_bg)
plt.close()
print(f"\n✅ Rapport visuel sauvegardé : {out_path}")
print("\nTerminé.")
