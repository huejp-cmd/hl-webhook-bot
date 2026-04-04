#!/usr/bin/env python3
"""
Performance Report — JP Autonomous Bot
========================================
Fetches trade data from the live Railway deployment and generates a
3-panel performance chart saved as trading/performance_report.png.

Usage:
    python trading/performance_report.py

Requirements:
    pip install matplotlib requests
"""

import os
import sys
import json
import requests
import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import FancyBboxPatch

# ==============================================================
#  CONFIG
# ==============================================================
BASE_URL   = "https://hl-webhook-bot-production.up.railway.app"
JOURNAL_EP = f"{BASE_URL}/journal"
STATS_EP   = f"{BASE_URL}/stats"
OUT_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "performance_report.png")

TIMEOUT = 15   # seconds

# ==============================================================
#  FETCH DATA
# ==============================================================
def fetch(url: str) -> dict | list | None:
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        print(f"❌  Cannot reach {url} — is the server running?")
        return None
    except Exception as e:
        print(f"❌  Error fetching {url}: {e}")
        return None


def main():
    print(f"📡  Fetching data from {BASE_URL} …")
    journal_resp = fetch(JOURNAL_EP)
    stats_resp   = fetch(STATS_EP)

    if journal_resp is None or stats_resp is None:
        print("❌  Failed to fetch data — aborting report")
        sys.exit(1)

    trades = journal_resp.get("trades", [])
    stats  = stats_resp

    closed_trades = [t for t in trades if t.get("status") == "CLOSED"
                     and t.get("pnl_usdc") is not None]

    print(f"✅  Journal: {len(trades)} trades total, {len(closed_trades)} closed")
    print(f"    Stats: win_rate={stats.get('win_rate', 0):.1f}%  "
          f"total_pnl={stats.get('total_pnl_usdc', 0):+.2f} USDC")

    # ==============================================================
    #  LAYOUT
    # ==============================================================
    fig = plt.figure(figsize=(14, 10), facecolor="#0d0d1a")
    gs  = gridspec.GridSpec(
        3, 1,
        height_ratios=[2, 2, 1.4],
        hspace=0.40,
        figure=fig,
    )

    ax_cum  = fig.add_subplot(gs[0])   # Panel 1: Cumulative PnL
    ax_bar  = fig.add_subplot(gs[1])   # Panel 2: PnL per trade
    ax_stat = fig.add_subplot(gs[2])   # Panel 3: Stats table

    # Common style
    for ax in (ax_cum, ax_bar, ax_stat):
        ax.set_facecolor("#13132a")
        ax.tick_params(colors="#aaaaaa", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#2a2a4a")

    now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    fig.suptitle(
        f"⚡ JP Autonomous Bot — Performance Report   ({now_str})",
        color="#7eb8f7", fontsize=13, fontweight="bold", y=0.98,
    )

    # ==============================================================
    #  PANEL 1 — Cumulative PnL
    # ==============================================================
    cum_pnl = stats.get("cumulative_pnl", [])

    if cum_pnl:
        xs = list(range(1, len(cum_pnl) + 1))
        color_line = "#3ddc84" if cum_pnl[-1] >= 0 else "#ff5c5c"
        ax_cum.plot(xs, cum_pnl, color=color_line, linewidth=2, zorder=3)
        ax_cum.fill_between(
            xs, cum_pnl, 0,
            color=color_line, alpha=0.15, zorder=2,
        )
        ax_cum.axhline(0, color="#555", linewidth=0.8, linestyle="--", zorder=1)

        # Annotate final value
        ax_cum.annotate(
            f"{cum_pnl[-1]:+.2f} USDC",
            xy=(xs[-1], cum_pnl[-1]),
            xytext=(-40, 12),
            textcoords="offset points",
            color=color_line,
            fontsize=9,
            fontweight="bold",
        )
    else:
        ax_cum.text(
            0.5, 0.5, "No closed trades yet",
            transform=ax_cum.transAxes,
            ha="center", va="center",
            color="#555", fontsize=11,
        )

    ax_cum.set_title("Cumulative PnL (USDC)", color="#e0e0e0", fontsize=10, pad=6)
    ax_cum.set_xlabel("Trade #", color="#aaaaaa", fontsize=8)
    ax_cum.set_ylabel("PnL USDC", color="#aaaaaa", fontsize=8)
    ax_cum.yaxis.label.set_color("#aaaaaa")
    ax_cum.xaxis.label.set_color("#aaaaaa")

    # ==============================================================
    #  PANEL 2 — PnL per trade (bar chart)
    # ==============================================================
    if closed_trades:
        pnls   = [t["pnl_usdc"] for t in closed_trades]
        colors = ["#3ddc84" if p > 0 else "#ff5c5c" for p in pnls]
        xs2    = list(range(1, len(pnls) + 1))
        ax_bar.bar(xs2, pnls, color=colors, width=0.7, zorder=3)
        ax_bar.axhline(0, color="#555", linewidth=0.8, linestyle="--", zorder=1)

        # Labels for extreme values
        if pnls:
            best_i  = pnls.index(max(pnls))
            worst_i = pnls.index(min(pnls))
            for idx in set([best_i, worst_i]):
                v    = pnls[idx]
                clr  = "#3ddc84" if v > 0 else "#ff5c5c"
                yoff = 4 if v >= 0 else -12
                ax_bar.annotate(
                    f"{v:+.1f}",
                    xy=(xs2[idx], v),
                    xytext=(0, yoff),
                    textcoords="offset points",
                    ha="center", fontsize=7, color=clr,
                )
    else:
        ax_bar.text(
            0.5, 0.5, "No closed trades yet",
            transform=ax_bar.transAxes,
            ha="center", va="center",
            color="#555", fontsize=11,
        )

    ax_bar.set_title("PnL per Trade (USDC)", color="#e0e0e0", fontsize=10, pad=6)
    ax_bar.set_xlabel("Trade #", color="#aaaaaa", fontsize=8)
    ax_bar.set_ylabel("PnL USDC", color="#aaaaaa", fontsize=8)

    # ==============================================================
    #  PANEL 3 — Stats table
    # ==============================================================
    ax_stat.axis("off")

    rows = [
        ("Total trades",     str(stats.get("total_trades", 0))),
        ("Open positions",   str(stats.get("open_trades", 0))),
        ("Closed trades",    str(stats.get("closed_trades", 0))),
        ("Wins / Losses",    f"{stats.get('wins', 0)} / {stats.get('losses', 0)}"),
        ("Win rate",         f"{stats.get('win_rate', 0):.1f} %"),
        ("Total PnL",        f"{stats.get('total_pnl_usdc', 0):+.2f} USDC"),
        ("Avg win",          f"{stats.get('avg_win', 0):+.2f} USDC"),
        ("Avg loss",         f"{stats.get('avg_loss', 0):+.2f} USDC"),
        ("Best trade",       f"{stats.get('best_trade', 0):+.2f} USDC"),
        ("Worst trade",      f"{stats.get('worst_trade', 0):+.2f} USDC"),
    ]

    # Two-column layout
    col_w = 0.48
    col_gap = 0.04
    n_rows  = (len(rows) + 1) // 2
    row_h   = 0.85 / max(n_rows, 1)

    for i, (label, value) in enumerate(rows):
        col  = i // n_rows
        row  = i % n_rows
        x0   = col * (col_w + col_gap)
        y0   = 0.92 - row * row_h

        # Row background
        rect = FancyBboxPatch(
            (x0, y0 - row_h * 0.85), col_w, row_h * 0.85,
            boxstyle="round,pad=0.01",
            linewidth=0,
            facecolor="#1a1a30",
            transform=ax_stat.transAxes,
            clip_on=False,
        )
        ax_stat.add_patch(rect)

        # Label
        ax_stat.text(
            x0 + 0.01, y0 - row_h * 0.42,
            label,
            transform=ax_stat.transAxes,
            ha="left", va="center",
            fontsize=8.5, color="#aaaaaa",
        )

        # Value colour
        val_color = "#e0e0e0"
        if "PnL" in label or "win" in label.lower() or "loss" in label.lower():
            try:
                num = float(value.replace(" USDC", "").replace(" %", ""))
                val_color = "#3ddc84" if num > 0 else ("#ff5c5c" if num < 0 else "#e0e0e0")
            except ValueError:
                pass
        if label == "Win rate":
            try:
                pct = float(value.replace(" %", ""))
                val_color = "#3ddc84" if pct >= 50 else "#ff5c5c"
            except ValueError:
                pass

        ax_stat.text(
            x0 + col_w - 0.01, y0 - row_h * 0.42,
            value,
            transform=ax_stat.transAxes,
            ha="right", va="center",
            fontsize=8.5, color=val_color, fontweight="bold",
        )

    ax_stat.set_title("Summary Statistics", color="#e0e0e0", fontsize=10, pad=6)

    # ==============================================================
    #  SAVE
    # ==============================================================
    plt.savefig(OUT_FILE, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"✅  Report saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
