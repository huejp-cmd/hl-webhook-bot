"""
report.py — Rich terminal table + equity curve PNG for JP v5.3 back-test results.

Usage
-----
    from report import print_metrics_table, save_equity_chart, print_trades_summary

    print_metrics_table(results_dict, symbol, timeframe)
    save_equity_chart(equity_series, symbol, timeframe)
"""

from pathlib import Path
import matplotlib
matplotlib.use("Agg")   # non-interactive backend (safe for scripts)
import matplotlib.pyplot as plt
import matplotlib.dates  as mdates
import pandas as pd
import numpy  as np

try:
    from rich.console import Console
    from rich.table   import Table
    from rich         import box
    _RICH = True
except ImportError:
    _RICH = False

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

console = Console() if _RICH else None


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pct(v: float) -> str:
    return f"{v:.2%}"

def _usd(v: float) -> str:
    return f"${v:,.2f}"

def _f(v: float, decimals: int = 3) -> str:
    return f"{v:.{decimals}f}"


# ─────────────────────────────────────────────────────────────────────────────
#  Single-result table
# ─────────────────────────────────────────────────────────────────────────────

def print_metrics_table(result: dict, symbol: str, timeframe: str) -> None:
    """Print a Rich summary table for one back-test result."""
    m = result["metrics"]
    trades = result.get("trades", [])

    if _RICH:
        t = Table(
            title=f"[bold cyan]JP v5.3 — {symbol} {timeframe}[/bold cyan]",
            box=box.ROUNDED, show_header=True,
            header_style="bold magenta",
        )
        t.add_column("Metric",  style="dim",   width=22)
        t.add_column("Value",   justify="right", width=16)

        def _color(val, good_above=None, bad_below=None):
            if good_above is not None and val >= good_above:
                return f"[green]{val}[/green]"
            if bad_below is not None and val <= bad_below:
                return f"[red]{val}[/red]"
            return str(val)

        t.add_row("Return",          _color(_pct(m["total_return_pct"]/100), good_above=None))
        t.add_row("Sharpe",          _color(_f(m["sharpe"]), good_above="1.0", bad_below="0"))
        t.add_row("Sortino",         _f(m["sortino"]))
        t.add_row("Calmar",          _f(m["calmar"]))
        t.add_row("Max Drawdown",    f"[red]{_pct(m['max_dd'])}[/red]")
        t.add_row("Win Rate",        _pct(m["win_rate"]))
        t.add_row("Profit Factor",   _f(m["profit_factor"]))
        t.add_row("# Trades",        str(m["n_trades"]))
        t.add_row("Avg Win",         _usd(m["avg_win"]))
        t.add_row("Avg Loss",        _usd(m["avg_loss"]))

        if trades:
            long_trades  = [t_ for t_ in trades if t_.side == "long"]
            short_trades = [t_ for t_ in trades if t_.side == "short"]
            t.add_row("Long trades",  str(len(long_trades)))
            t.add_row("Short trades", str(len(short_trades)))

            by_regime = {}
            for tr in trades:
                by_regime.setdefault(tr.regime, []).append(tr.pnl)
            for reg, pnls in by_regime.items():
                t.add_row(
                    f"  {reg.capitalize()} trades",
                    f"{len(pnls)} | {_usd(sum(pnls))}"
                )

        console.print(t)
    else:
        # Fallback plain print
        print(f"\n=== JP v5.3  {symbol} {timeframe} ===")
        for k, v in m.items():
            print(f"  {k:<22}: {v}")


# ─────────────────────────────────────────────────────────────────────────────
#  Multi-timeframe comparison table
# ─────────────────────────────────────────────────────────────────────────────

def print_multi_table(all_results: dict) -> None:
    """
    Print a comparison table across symbols and timeframes.

    all_results: { (symbol, tf): result_dict }
    """
    rows = []
    for (sym, tf), res in all_results.items():
        m = res["metrics"]
        rows.append({
            "Symbol":   sym,
            "TF":       tf,
            "Return %": round(m["total_return_pct"], 2),
            "Sharpe":   round(m["sharpe"],    3),
            "Sortino":  round(m["sortino"],   3),
            "Max DD %": round(m["max_dd"] * 100, 2),
            "Win Rate": round(m["win_rate"] * 100, 1),
            "PF":       round(m["profit_factor"], 2),
            "Trades":   m["n_trades"],
            "Calmar":   round(m["calmar"], 3),
        })

    df = pd.DataFrame(rows)

    if _RICH:
        t = Table(
            title="[bold cyan]JP v5.3 — Multi-Timeframe Report[/bold cyan]",
            box=box.SIMPLE_HEAVY, show_header=True,
            header_style="bold blue",
        )
        for col in df.columns:
            justify = "right" if col not in ("Symbol", "TF") else "left"
            t.add_column(col, justify=justify)

        for _, row in df.iterrows():
            ret_col = (
                f"[green]{row['Return %']}%[/green]"
                if row["Return %"] > 0 else
                f"[red]{row['Return %']}%[/red]"
            )
            sharpe_col = (
                f"[green]{row['Sharpe']}[/green]"
                if row["Sharpe"] > 1.0 else str(row["Sharpe"])
            )
            t.add_row(
                row["Symbol"], row["TF"],
                ret_col, sharpe_col,
                str(row["Sortino"]),
                f"[red]{row['Max DD %']}%[/red]",
                f"{row['Win Rate']}%",
                str(row["PF"]),
                str(row["Trades"]),
                str(row["Calmar"]),
            )
        console.print(t)
    else:
        print(df.to_string(index=False))

    # Save CSV summary
    csv_path = RESULTS_DIR / "multi_tf_report.csv"
    df.to_csv(csv_path, index=False)
    print(f"\n📄  Summary saved → {csv_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  Equity curve chart
# ─────────────────────────────────────────────────────────────────────────────

def save_equity_chart(equity: pd.Series, symbol: str, timeframe: str,
                      trades=None, start_capital: float = 500.0) -> Path:
    """
    Save a PNG equity-curve chart.

    Parameters
    ----------
    equity       : pd.Series with DatetimeIndex
    symbol       : e.g. "SOLUSDT"
    timeframe    : e.g. "30m"
    trades       : list of Trade objects (optional, for win/loss markers)
    start_capital: initial capital for reference line

    Returns
    -------
    Path to saved PNG.
    """
    fig, axes = plt.subplots(2, 1, figsize=(14, 8),
                              gridspec_kw={"height_ratios": [3, 1]},
                              facecolor="#0e1117")
    ax1, ax2 = axes

    # ── Equity curve ─────────────────────────────────────────────────────────
    ax1.set_facecolor("#0e1117")
    ax1.plot(equity.index, equity.values,
             color="#00d4ff", linewidth=1.5, label="Equity")
    ax1.axhline(start_capital, color="#ffffff44", linewidth=0.8,
                linestyle="--", label=f"Start: ${start_capital:,.0f}")

    # Drawdown fill
    roll_max = equity.cummax()
    ax1.fill_between(equity.index, equity.values, roll_max.values,
                     alpha=0.25, color="#ff4444", label="Drawdown")

    # Trade markers
    if trades:
        win_times  = [t.exit_time for t in trades if t.pnl > 0]
        loss_times = [t.exit_time for t in trades if t.pnl <= 0]
        if win_times:
            win_eq = equity.reindex(win_times, method="ffill")
            ax1.scatter(win_times, win_eq.values,
                        marker="^", color="#00ff88", s=25, zorder=5, label="Win")
        if loss_times:
            loss_eq = equity.reindex(loss_times, method="ffill")
            ax1.scatter(loss_times, loss_eq.values,
                        marker="v", color="#ff4444", s=25, zorder=5, label="Loss")

    ax1.set_title(f"JP v5.3 — {symbol} {timeframe}",
                  color="white", fontsize=14, pad=10)
    ax1.set_ylabel("Equity ($)", color="white")
    ax1.tick_params(colors="white")
    ax1.legend(loc="upper left", fontsize=8, facecolor="#1a1a2e", labelcolor="white")
    ax1.grid(True, alpha=0.15, color="white")
    for spine in ax1.spines.values():
        spine.set_edgecolor("#333")

    # ── Drawdown % ───────────────────────────────────────────────────────────
    ax2.set_facecolor("#0e1117")
    dd_pct = ((roll_max - equity) / roll_max * 100)
    ax2.fill_between(equity.index, dd_pct.values, color="#ff4444", alpha=0.6)
    ax2.set_ylabel("DD %", color="white")
    ax2.set_xlabel("Date", color="white")
    ax2.tick_params(colors="white")
    ax2.invert_yaxis()
    ax2.grid(True, alpha=0.15, color="white")
    for spine in ax2.spines.values():
        spine.set_edgecolor("#333")

    # Date formatting
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    fig.autofmt_xdate()

    plt.tight_layout(pad=1.5)
    out_path = RESULTS_DIR / f"{symbol}_{timeframe}_equity.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="#0e1117")
    plt.close(fig)
    print(f"📈  Chart saved → {out_path.name}")
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
#  Trade summary
# ─────────────────────────────────────────────────────────────────────────────

def print_trades_summary(trades, n: int = 15) -> None:
    """Print the last N trades in a rich table."""
    if not trades:
        print("No trades.")
        return

    last = trades[-n:]
    if _RICH:
        t = Table(title=f"Last {len(last)} trades",
                  box=box.MINIMAL_DOUBLE_HEAD, show_header=True)
        for col in ("Entry", "Exit", "Side", "Regime",
                    "Entry $", "Exit $", "PnL $", "Reason"):
            t.add_column(col)

        for tr in last:
            pnl_str = (
                f"[green]+{tr.pnl:.2f}[/green]"
                if tr.pnl >= 0 else
                f"[red]{tr.pnl:.2f}[/red]"
            )
            t.add_row(
                str(tr.entry_time)[:16],
                str(tr.exit_time)[:16],
                tr.side, tr.regime,
                f"{tr.entry_price:.4f}",
                f"{tr.exit_price:.4f}",
                pnl_str,
                tr.exit_reason,
            )
        console.print(t)
    else:
        for tr in last:
            print(f"  {tr.side:5} {tr.regime:9} "
                  f"in={tr.entry_price:.4f} out={tr.exit_price:.4f} "
                  f"pnl={tr.pnl:+.2f} ({tr.exit_reason})")
