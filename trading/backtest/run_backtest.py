"""
run_backtest.py — Main entry point for JP v5.3 back-testing system.

Menu
----
  1. Download / update data
  2. Single back-test with default params
  3. Optimize (choose symbol + timeframe)
  4. Full multi-timeframe report (SOL + ETH × 7 timeframes)
  5. Exit
"""

import sys
from pathlib import Path

# ── Attempt rich import (graceful fallback) ───────────────────────────────────
try:
    from rich.console import Console
    from rich.prompt  import Prompt, IntPrompt
    from rich.panel   import Panel
    from rich         import print as rprint
    _RICH = True
    console = Console()
except ImportError:
    _RICH = False
    console = None

def _print(msg, style=""):
    if _RICH and console:
        console.print(msg, style=style)
    else:
        # strip basic rich markup for plain output
        import re
        plain = re.sub(r"\[.*?\]", "", msg)
        print(plain)

def _input(prompt):
    if _RICH:
        return Prompt.ask(prompt)
    return input(prompt + " ")

# ── Local imports ─────────────────────────────────────────────────────────────
from data_fetcher import (
    fetch, fetch_all,
    SYMBOLS, ALL_TIMEFRAMES, NATIVE_TFS, DERIVED_TFS,
)
from strategy_v53 import StrategyV53, DEFAULT_PARAMS
from report import (
    print_metrics_table, save_equity_chart,
    print_trades_summary, print_multi_table,
)

# Symbols and timeframes covered by option 4
REPORT_SYMBOLS     = ["SOLUSDT", "ETHUSDT"]
REPORT_TIMEFRAMES  = ALL_TIMEFRAMES   # 15m, 30m, 45m, 1h, 2h, 3h, 4h


# ─────────────────────────────────────────────────────────────────────────────
#  Option 1 — Download data
# ─────────────────────────────────────────────────────────────────────────────

def menu_download():
    _print("\n[bold cyan]⬇  Downloading / updating data…[/bold cyan]")
    force = _input("Force full refresh? (y/N)").strip().lower() == "y"
    # Always include BTCUSDT for the inRange filter
    all_syms = list(set(REPORT_SYMBOLS + ["BTCUSDT"]))
    fetch_all(symbols=all_syms, timeframes=ALL_TIMEFRAMES, force_refresh=force)
    _print("[green]✅  Data ready.[/green]")


# ─────────────────────────────────────────────────────────────────────────────
#  Option 2 — Single back-test
# ─────────────────────────────────────────────────────────────────────────────

def menu_single():
    _print("\n[bold cyan]📊  Single Back-test[/bold cyan]")
    sym = _input(f"Symbol [{'/'.join(REPORT_SYMBOLS)}]").strip().upper() or "SOLUSDT"
    tf  = _input(f"Timeframe [{'/'.join(ALL_TIMEFRAMES)}]").strip() or "30m"

    _print(f"  Loading {sym} {tf}…")
    ohlcv_df  = fetch(sym, tf)
    btc_1h_df = fetch("BTCUSDT", "1h")

    _print("  Running strategy…")
    strat  = StrategyV53()
    result = strat.run(ohlcv_df, btc_1h_df)

    print_metrics_table(result, sym, tf)
    print_trades_summary(result["trades"], n=10)
    save_equity_chart(
        result["equity_curve"], sym, tf,
        trades=result["trades"],
        start_capital=DEFAULT_PARAMS["start_capital"],
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Option 3 — Optimize
# ─────────────────────────────────────────────────────────────────────────────

def menu_optimize():
    from optimizer import optimize

    _print("\n[bold cyan]🔬  Optimization[/bold cyan]")
    sym     = _input(f"Symbol [{'/'.join(REPORT_SYMBOLS)}]").strip().upper() or "SOLUSDT"
    tf      = _input(f"Timeframe [{'/'.join(ALL_TIMEFRAMES)}]").strip() or "30m"
    n_str   = _input("Number of trials [150]").strip()
    n_trials= int(n_str) if n_str.isdigit() else 150

    _print(f"  Starting {n_trials} trials for {sym} {tf}…")
    best = optimize(sym, tf, n_trials=n_trials)

    _print(f"\n[green]Best Sharpe: {best['best_value']:.4f}[/green]")
    _print("Best params:")
    for k, v in best["best_params"].items():
        if k in DEFAULT_PARAMS and best["best_params"][k] != DEFAULT_PARAMS[k]:
            _print(f"  [yellow]{k:<25}[/yellow]: {v}  (default: {DEFAULT_PARAMS[k]})")

    # Show metrics for best params
    ohlcv_df  = fetch(sym, tf)
    btc_1h_df = fetch("BTCUSDT", "1h")
    strat  = StrategyV53(best["best_params"])
    result = strat.run(ohlcv_df, btc_1h_df)
    print_metrics_table(result, sym, tf)
    save_equity_chart(
        result["equity_curve"], sym, f"{tf}_optimized",
        trades=result["trades"],
        start_capital=best["best_params"]["start_capital"],
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Option 4 — Full multi-timeframe report
# ─────────────────────────────────────────────────────────────────────────────

def menu_full_report():
    _print("\n[bold cyan]📋  Full Multi-Timeframe Report[/bold cyan]")
    _print(f"  Symbols   : {', '.join(REPORT_SYMBOLS)}")
    _print(f"  Timeframes: {', '.join(REPORT_TIMEFRAMES)}")
    _print("  (45m = resampled 3×15m | 2h = resampled 2×1h | 3h = resampled 3×1h)\n")

    # Pre-load BTC 1H once
    _print("  Loading BTC 1H for inRange filter…")
    btc_1h_df = fetch("BTCUSDT", "1h")

    all_results = {}
    total = len(REPORT_SYMBOLS) * len(REPORT_TIMEFRAMES)
    done  = 0

    for sym in REPORT_SYMBOLS:
        for tf in REPORT_TIMEFRAMES:
            done += 1
            _print(f"  [{done}/{total}] {sym} {tf}…")
            try:
                ohlcv_df = fetch(sym, tf)
                strat    = StrategyV53()
                result   = strat.run(ohlcv_df, btc_1h_df)
                all_results[(sym, tf)] = result

                m = result["metrics"]
                _print(
                    f"        → Sharpe={m['sharpe']:.3f}  "
                    f"Return={m['total_return_pct']:.1f}%  "
                    f"Trades={m['n_trades']}  "
                    f"DD={m['max_dd']:.1%}"
                )

                # Save individual equity chart
                save_equity_chart(
                    result["equity_curve"], sym, tf,
                    trades=result["trades"],
                    start_capital=DEFAULT_PARAMS["start_capital"],
                )

            except Exception as e:
                _print(f"  [red]Error {sym} {tf}: {e}[/red]")
                all_results[(sym, tf)] = {
                    "metrics": {k: 0.0 for k in [
                        "total_return_pct", "sharpe", "sortino", "max_dd",
                        "win_rate", "profit_factor", "n_trades",
                        "avg_win", "avg_loss", "calmar",
                    ]},
                    "trades": [],
                    "equity_curve": None,
                    "error": str(e),
                }

    _print("\n")
    print_multi_table(all_results)

    # Best configuration
    valid = {k: v for k, v in all_results.items()
             if v["metrics"]["n_trades"] >= 10}
    if valid:
        best_key = max(valid, key=lambda k: valid[k]["metrics"]["sharpe"])
        bm       = valid[best_key]["metrics"]
        _print(
            f"\n[bold green]🏆  Best config: {best_key[0]} {best_key[1]}[/bold green]  "
            f"Sharpe={bm['sharpe']:.3f}  Return={bm['total_return_pct']:.1f}%"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Main menu
# ─────────────────────────────────────────────────────────────────────────────

MENU_ITEMS = {
    "1": ("Download / update data",                 menu_download),
    "2": ("Single back-test (default params)",      menu_single),
    "3": ("Optimize (Optuna)",                      menu_optimize),
    "4": ("Full multi-TF report (SOL+ETH, 7 TFs)", menu_full_report),
    "5": ("Exit",                                   None),
}


def main():
    banner = (
        "[bold cyan]"
        "╔══════════════════════════════════════╗\n"
        "║   JP v5.3 — Back-test & Optimizer   ║\n"
        "╚══════════════════════════════════════╝"
        "[/bold cyan]"
    )
    _print(banner)

    # Non-interactive mode: pass menu option as CLI argument
    if len(sys.argv) > 1:
        choice = sys.argv[1].strip()
        if choice in MENU_ITEMS:
            label, fn = MENU_ITEMS[choice]
            _print(f"\n[yellow]→ {label}[/yellow]")
            if fn:
                fn()
        else:
            _print(f"[red]Unknown option: {choice}[/red]")
        return

    while True:
        _print("\n[bold]Choose an option:[/bold]")
        for key, (label, _) in MENU_ITEMS.items():
            _print(f"  [cyan]{key}[/cyan]  {label}")

        choice = _input("\nOption").strip()
        if choice not in MENU_ITEMS:
            _print("[red]Invalid choice.[/red]")
            continue

        label, fn = MENU_ITEMS[choice]
        if fn is None:
            _print("[dim]Goodbye.[/dim]")
            break

        try:
            fn()
        except KeyboardInterrupt:
            _print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:
            _print(f"\n[red]Error: {e}[/red]")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
