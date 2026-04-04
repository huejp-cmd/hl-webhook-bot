#!/usr/bin/env python3
"""
NQ 5M — Comparaison seuils ADX (20/25/30/35)
Params fixes : body_pct=0.15, rr=1.5, max_wait=2, contracts=1
Génère : trading/nasdaq_adx_comparison.png
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from datetime import datetime

# Import from strategy
from trading.nasdaq_strategy import (
    download_data, filter_rth, backtest_5m_adx,
    ACCOUNT_SIZE
)

# ─── Params fixes ───────────────────────────────────────────
FIXED_BODY_PCT   = 0.15
FIXED_RR         = 1.5
FIXED_MAX_WAIT   = 2
FIXED_CONTRACTS  = 1

# Résultats connus ADX=20
ADX20_KNOWN = {
    'win_rate':       70.0,
    'profit_factor':  3.34,
    'sharpe':         9.53,
    'total_pnl':      2465.0,
    'avg_pnl_day':    246.0,
    'max_dd':        -425.0,
    'signals_day':    1.0,
    'n_daily_limit':  0,
}

THRESHOLDS = [25, 30, 35]


def get_verdict(wr, pf, max_dd_abs):
    """Retourne le verdict de sécurité."""
    if wr > 55 and pf > 1.5 and max_dd_abs < 3000:
        return "✅ Sécurisé"
    elif wr > 45 and pf > 1.2 and max_dd_abs < 5000:
        return "⚠️ Acceptable"
    else:
        return "❌ Risqué"


def run_comparison():
    print("=" * 60)
    print("NQ 5M — COMPARAISON SEUILS ADX")
    print("=" * 60)

    # Télécharger données
    print("\n📥 Téléchargement NQ=F 5M (60j)...")
    df_raw, conv = download_data(interval='5m', period='60d')
    df = filter_rth(df_raw)

    if len(df) < 50:
        print("❌ Données insuffisantes")
        return

    # Nombre de jours de trading
    try:
        idx = df.index
        if idx.tz is None:
            idx_et = idx.tz_localize('UTC').tz_convert('America/New_York')
        else:
            idx_et = idx.tz_convert('America/New_York')
        n_days = len(set(t.date() for t in idx_et))
    except Exception:
        n_days = max(1, len(df) // 78)

    print(f"Données : {n_days} jours | {len(df)} barres RTH")

    # ── Lancer les 3 backtests ───────────────────────────────
    results = {}
    for adx_thr in THRESHOLDS:
        print(f"\n🔄 Backtest ADX={adx_thr}...")
        bt = backtest_5m_adx(
            df, conv=conv,
            body_pct=FIXED_BODY_PCT,
            rr_ratio=FIXED_RR,
            max_wait_bars=FIXED_MAX_WAIT,
            adx_threshold=float(adx_thr),
            contracts=FIXED_CONTRACTS
        )
        s = bt['stats']
        pnl_total = s.get('final_account', ACCOUNT_SIZE) - ACCOUNT_SIZE
        results[adx_thr] = {
            'win_rate':       s['win_rate'],
            'profit_factor':  s['profit_factor'],
            'sharpe':         s['sharpe'],
            'total_pnl':      pnl_total,
            'avg_pnl_day':    s.get('avg_pnl_per_day', 0),
            'max_dd':         s.get('max_dd_usd', 0),
            'signals_day':    s['trades_per_day'],
            'n_daily_limit':  s.get('n_daily_limit', 0),
            'n_trades':       s['n_trades'],
        }
        print(f"  WR={s['win_rate']:.1f}% | PF={s['profit_factor']:.2f} | "
              f"Sharpe={s['sharpe']:.2f} | Trades/j={s['trades_per_day']:.1f}")

    # ── Affichage tableau console ────────────────────────────
    r20 = ADX20_KNOWN
    r25 = results[25]
    r30 = results[30]
    r35 = results[35]

    v20 = get_verdict(r20['win_rate'], r20['profit_factor'], abs(r20['max_dd']))
    v25 = get_verdict(r25['win_rate'], r25['profit_factor'], abs(r25['max_dd']))
    v30 = get_verdict(r30['win_rate'], r30['profit_factor'], abs(r30['max_dd']))
    v35 = get_verdict(r35['win_rate'], r35['profit_factor'], abs(r35['max_dd']))

    print()
    print("=" * 76)
    print("NQ 5M — COMPARAISON SEUILS ADX (1 contrat, sécurité prioritaire)")
    print("=" * 76)
    print(f"Params fixes : body_pct={FIXED_BODY_PCT}, rr={FIXED_RR}, max_wait={FIXED_MAX_WAIT}")
    print()
    print(f"{'ADX MAX':<14} | {'ADX=20':>10} | {'ADX=25':>10} | {'ADX=30':>10} | {'ADX=35':>10}")
    print("-" * 76)
    print(f"{'Win Rate':<14} | {r20['win_rate']:>9.1f}% | {r25['win_rate']:>9.1f}% | {r30['win_rate']:>9.1f}% | {r35['win_rate']:>9.1f}%")
    print(f"{'Profit Factor':<14} | {r20['profit_factor']:>10.2f} | {r25['profit_factor']:>10.2f} | {r30['profit_factor']:>10.2f} | {r35['profit_factor']:>10.2f}")
    print(f"{'Sharpe':<14} | {r20['sharpe']:>10.2f} | {r25['sharpe']:>10.2f} | {r30['sharpe']:>10.2f} | {r35['sharpe']:>10.2f}")
    print(f"{'P&L Total':<14} | {r20['total_pnl']:>+9.0f}$ | {r25['total_pnl']:>+9.0f}$ | {r30['total_pnl']:>+9.0f}$ | {r35['total_pnl']:>+9.0f}$")
    print(f"{'P&L Moy/jour':<14} | {r20['avg_pnl_day']:>+9.0f}$ | {r25['avg_pnl_day']:>+9.0f}$ | {r30['avg_pnl_day']:>+9.0f}$ | {r35['avg_pnl_day']:>+9.0f}$")
    print(f"{'Max DD':<14} | {r20['max_dd']:>+9.0f}$ | {r25['max_dd']:>+9.0f}$ | {r30['max_dd']:>+9.0f}$ | {r35['max_dd']:>+9.0f}$")
    print(f"{'Signaux/jour':<14} | {r20['signals_day']:>10.1f} | {r25['signals_day']:>10.1f} | {r30['signals_day']:>10.1f} | {r35['signals_day']:>10.1f}")
    print(f"{'Jours limit':<14} | {int(r20['n_daily_limit']):>10} | {int(r25['n_daily_limit']):>10} | {int(r30['n_daily_limit']):>10} | {int(r35['n_daily_limit']):>10}")
    print("-" * 76)
    print(f"{'VERDICT':<14} | {v20:>10} | {v25:>10} | {v30:>10} | {v35:>10}")
    print("=" * 76)

    # ── Recommandation ───────────────────────────────────────
    print("\n📊 ANALYSE :")
    print(f"  ADX=20 : {v20} — excellent mais ~1 signal/jour (trop rare)")

    best_thr = None
    best_score = -1
    for thr in THRESHOLDS:
        r = results[thr]
        # Score combiné : WR * PF / max(1, abs(DD/1000)) avec bonus fréquence
        freq_bonus = 1.2 if r['signals_day'] >= 2 else 1.0
        score = (r['win_rate'] / 100) * min(r['profit_factor'], 5) * freq_bonus / max(1, abs(r['max_dd']) / 500)
        if get_verdict(r['win_rate'], r['profit_factor'], abs(r['max_dd'])) in ("✅ Sécurisé", "⚠️ Acceptable"):
            if score > best_score:
                best_score = score
                best_thr = thr

    if best_thr:
        r_best = results[best_thr]
        print(f"\n🏆 RECOMMANDATION JP : ADX={best_thr}")
        print(f"   WR={r_best['win_rate']:.1f}% | PF={r_best['profit_factor']:.2f} | "
              f"DD={r_best['max_dd']:+.0f}$ | {r_best['signals_day']:.1f} sig/j")
        print(f"   → Meilleur équilibre sécurité / fréquence")
    else:
        print(f"\n⚠️  Aucun seuil parfait — ADX=20 reste le plus sûr (WR=70%, PF=3.34)")

    # ── Générer PNG ──────────────────────────────────────────
    generate_adx_comparison_chart(results, ADX20_KNOWN, verdicts={
        20: v20, 25: v25, 30: v30, 35: v35
    })

    return results


def generate_adx_comparison_chart(results: dict, r20: dict, verdicts: dict):
    """
    4 panneaux horizontaux — 1 ligne par métrique, 4 barres une par ADX.
    1. Win Rate
    2. Profit Factor
    3. P&L moyen/jour
    4. Max Drawdown
    """
    thresholds = [20, 25, 30, 35]
    labels     = ['ADX=20', 'ADX=25', 'ADX=30', 'ADX=35']

    # Collecter les données
    win_rates   = [r20['win_rate']]   + [results[t]['win_rate']   for t in [25, 30, 35]]
    pf_values   = [r20['profit_factor']] + [results[t]['profit_factor'] for t in [25, 30, 35]]
    avg_pnl     = [r20['avg_pnl_day']] + [results[t]['avg_pnl_day']  for t in [25, 30, 35]]
    max_dds     = [abs(r20['max_dd'])] + [abs(results[t]['max_dd'])   for t in [25, 30, 35]]
    sigs_day    = [r20['signals_day']] + [results[t]['signals_day']   for t in [25, 30, 35]]

    C = dict(
        bg='#0d1117', panel='#161b22', text='#e6edf3',
        green='#3fb950', red='#f85149', blue='#58a6ff',
        gold='#d29922', grey='#8b949e', orange='#f0883e',
        cyan='#39d353',
    )

    fig, axes = plt.subplots(4, 1, figsize=(14, 18))
    fig.patch.set_facecolor(C['bg'])
    fig.suptitle(
        f'NQ 5M — Comparaison Seuils ADX  |  1 Contrat, Sécurité Prioritaire\n'
        f'Params fixes : body_pct={FIXED_BODY_PCT}, rr={FIXED_RR}, max_wait={FIXED_MAX_WAIT}  |  '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M")}',
        fontsize=13, fontweight='bold', color=C['text'], y=0.99
    )

    x = np.arange(len(thresholds))
    bar_w = 0.55

    # ── Panneau 1 : Win Rate ─────────────────────────────────
    ax1 = axes[0]
    ax1.set_facecolor(C['panel'])
    wr_colors = []
    for wr in win_rates:
        if wr > 55:
            wr_colors.append(C['green'])
        elif wr >= 45:
            wr_colors.append(C['orange'])
        else:
            wr_colors.append(C['red'])

    bars = ax1.bar(x, win_rates, width=bar_w, color=wr_colors, alpha=0.85,
                   edgecolor='#30363d', linewidth=0.8)
    ax1.axhline(y=55, color=C['green'],  linestyle='--', linewidth=1.0, alpha=0.7,
                label='Seuil sécurisé (55%)')
    ax1.axhline(y=45, color=C['orange'], linestyle=':', linewidth=1.0, alpha=0.7,
                label='Seuil acceptable (45%)')
    ax1.set_ylabel('Win Rate (%)', color=C['text'])
    ax1.set_ylim(0, max(100, max(win_rates) * 1.15))
    ax1.set_xticks(x)
    ax1.set_xticklabels([f"{lb}\n{verdicts[t]}" for lb, t in zip(labels, thresholds)],
                         fontsize=9)
    ax1.set_title('📊 Win Rate par seuil ADX', color=C['text'], fontsize=11,
                  fontweight='bold', pad=8)
    ax1.tick_params(colors=C['text'])
    ax1.yaxis.label.set_color(C['text'])
    for spine in ax1.spines.values():
        spine.set_color('#30363d')
    ax1.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8, loc='upper right')
    for bar, val in zip(bars, win_rates):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                 f'{val:.1f}%', ha='center', va='bottom',
                 color=C['text'], fontsize=10, fontweight='bold')

    # ── Panneau 2 : Profit Factor ────────────────────────────
    ax2 = axes[1]
    ax2.set_facecolor(C['panel'])
    pf_colors = [C['green'] if pf > 1.5 else C['orange'] if pf > 1.2 else C['red']
                 for pf in pf_values]
    bars2 = ax2.bar(x, pf_values, width=bar_w, color=pf_colors, alpha=0.85,
                    edgecolor='#30363d', linewidth=0.8)
    ax2.axhline(y=1.5, color=C['green'],  linestyle='--', linewidth=1.0, alpha=0.7,
                label='Seuil sécurisé (1.5)')
    ax2.axhline(y=1.2, color=C['orange'], linestyle=':', linewidth=1.0, alpha=0.7,
                label='Seuil acceptable (1.2)')
    ax2.axhline(y=1.0, color=C['red'],    linestyle='-',  linewidth=0.8, alpha=0.5)
    ax2.set_ylabel('Profit Factor', color=C['text'])
    ax2.set_ylim(0, max(5, max(pf_values) * 1.15))
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=9)
    ax2.set_title('💰 Profit Factor par seuil ADX', color=C['text'], fontsize=11,
                  fontweight='bold', pad=8)
    ax2.tick_params(colors=C['text'])
    ax2.yaxis.label.set_color(C['text'])
    for spine in ax2.spines.values():
        spine.set_color('#30363d')
    ax2.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8, loc='upper right')
    for bar, val in zip(bars2, pf_values):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.02,
                 f'{val:.2f}', ha='center', va='bottom',
                 color=C['text'], fontsize=10, fontweight='bold')

    # ── Panneau 3 : P&L moyen/jour ───────────────────────────
    ax3 = axes[2]
    ax3.set_facecolor(C['panel'])
    pnl_colors = [C['green'] if p >= 0 else C['red'] for p in avg_pnl]
    bars3 = ax3.bar(x, avg_pnl, width=bar_w, color=pnl_colors, alpha=0.85,
                    edgecolor='#30363d', linewidth=0.8)
    ax3.axhline(y=1000, color=C['cyan'], linestyle='--', linewidth=1.0, alpha=0.7,
                label='Objectif +1000$/jour')
    ax3.axhline(y=0, color=C['grey'], linewidth=0.8)
    ax3.set_ylabel('P&L Moy/Jour ($)', color=C['text'])
    ax3.set_xticks(x)
    ax3.set_xticklabels([f"{lb}\n{s:.1f} sig/j" for lb, s in zip(labels, sigs_day)],
                         fontsize=9)
    ax3.set_title('📈 P&L Moyen/Jour par seuil ADX', color=C['text'], fontsize=11,
                  fontweight='bold', pad=8)
    ax3.tick_params(colors=C['text'])
    ax3.yaxis.label.set_color(C['text'])
    ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'${v:,.0f}'))
    for spine in ax3.spines.values():
        spine.set_color('#30363d')
    ax3.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8, loc='upper right')
    for bar, val in zip(bars3, avg_pnl):
        ypos = bar.get_height() + (abs(max(avg_pnl) - min(avg_pnl)) * 0.01) if val >= 0 else bar.get_height() - (abs(max(avg_pnl) - min(avg_pnl)) * 0.04)
        ax3.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 5,
                 f'${val:+.0f}', ha='center', va='bottom',
                 color=C['text'], fontsize=10, fontweight='bold')

    # ── Panneau 4 : Max Drawdown ─────────────────────────────
    ax4 = axes[3]
    ax4.set_facecolor(C['panel'])
    # Barres rouges — plus court = mieux (valeurs positives pour affichage)
    dd_plot = max_dds
    dd_colors = [C['red'] if dd >= 3000 else C['orange'] if dd >= 1000 else '#88cc88'
                 for dd in dd_plot]
    bars4 = ax4.bar(x, dd_plot, width=bar_w, color=dd_colors, alpha=0.85,
                    edgecolor='#30363d', linewidth=0.8)
    ax4.axhline(y=3000, color=C['orange'], linestyle='--', linewidth=1.0, alpha=0.8,
                label='Limite sécurisé (3 000$)')
    ax4.axhline(y=5000, color=C['red'],    linestyle=':', linewidth=1.0, alpha=0.8,
                label='Limite acceptable (5 000$)')
    ax4.set_ylabel('Max Drawdown ($) — plus bas = mieux', color=C['text'])
    ax4.set_xticks(x)
    ax4.set_xticklabels(labels, fontsize=9)
    ax4.set_title('⚠️ Max Drawdown par seuil ADX (barre rouge = plus petit = mieux)',
                  color=C['text'], fontsize=11, fontweight='bold', pad=8)
    ax4.tick_params(colors=C['text'])
    ax4.yaxis.label.set_color(C['text'])
    ax4.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'-${v:,.0f}'))
    for spine in ax4.spines.values():
        spine.set_color('#30363d')
    ax4.legend(facecolor=C['panel'], labelcolor=C['text'], fontsize=8, loc='upper right')
    for bar, val in zip(bars4, dd_plot):
        ax4.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(max_dds) * 0.01,
                 f'-${val:,.0f}', ha='center', va='bottom',
                 color=C['text'], fontsize=10, fontweight='bold')

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    out = 'trading/nasdaq_adx_comparison.png'
    plt.savefig(out, dpi=150, bbox_inches='tight',
                facecolor=C['bg'], edgecolor='none')
    plt.close()
    print(f"\n✅ Rapport comparatif sauvegardé : {out}")


if __name__ == '__main__':
    run_comparison()
