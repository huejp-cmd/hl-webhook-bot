"""
optimizer.py — Optuna-based hyperparameter optimizer for JP v5.3.

Objective : maximize Sharpe ratio.
Saves best params + full results to backtest/results/<symbol>_<tf>_optuna.json.

Usage
-----
    from optimizer import optimize
    best = optimize("SOLUSDT", "30m", n_trials=150)
"""

import json
import warnings
from pathlib import Path

import optuna
import pandas as pd

from data_fetcher import fetch
from strategy_v53 import StrategyV53, DEFAULT_PARAMS

optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings("ignore")

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _suggest_params(trial: optuna.Trial) -> dict:
    """Define the search space — all overrides on top of DEFAULT_PARAMS."""
    return {
        # Range bars
        "atr_length":        trial.suggest_int  ("atr_length",        5,  20),
        "use_fixed_range":   trial.suggest_categorical("use_fixed_range", [False, True]),
        "fixed_range_size":  trial.suggest_float("fixed_range_size",  0.5, 3.0, step=0.1),
        # Indicators
        "adx_length":        trial.suggest_int  ("adx_length",        7,  21),
        "rsi_length":        trial.suggest_int  ("rsi_length",        50, 150),
        "bb_length":         trial.suggest_int  ("bb_length",         10, 40),
        "bb_mult":           trial.suggest_float("bb_mult",           1.0, 3.0, step=0.25),
        "vp_k":              trial.suggest_float("vp_k",              0.3, 1.5, step=0.05),
        # Risk
        "risk_perc":         trial.suggest_float("risk_perc",         0.005, 0.05, step=0.005),
        "leverage":          trial.suggest_float("leverage",          1.0,   4.0,  step=0.5),
        "sl_cap_pct":        trial.suggest_float("sl_cap_pct",        0.02,  0.10, step=0.01),
        # Volume
        "vol_mult":          trial.suggest_float("vol_mult",          1.0,   2.5,  step=0.1),
        # TP multipliers
        "tp_mult_trending":  trial.suggest_float("tp_mult_trending",  2.0,   6.0,  step=0.5),
        "tp_mult_ranging":   trial.suggest_float("tp_mult_ranging",   1.5,   4.0,  step=0.5),
        "tp_mult_explosive": trial.suggest_float("tp_mult_explosive", 2.0,   5.0,  step=0.5),
        # Trailing
        "trail_atr_mult":    trial.suggest_float("trail_atr_mult",    0.5,   3.0,  step=0.25),
    }


def optimize(symbol: str, timeframe: str,
             n_trials: int = 150,
             btc_1h_df: pd.DataFrame | None = None,
             show_progress: bool = True) -> dict:
    """
    Run Optuna optimization for the given symbol/timeframe.

    Parameters
    ----------
    symbol      : e.g. "SOLUSDT"
    timeframe   : e.g. "30m"
    n_trials    : number of Optuna trials
    btc_1h_df   : pre-loaded BTC 1H DataFrame (fetched automatically if None)
    show_progress : print a progress bar via rich if True

    Returns
    -------
    dict with keys: best_params, best_value (Sharpe), trials_df
    """
    ohlcv_df = fetch(symbol, timeframe)
    if btc_1h_df is None:
        btc_1h_df = fetch("BTCUSDT", "1h")

    def objective(trial: optuna.Trial) -> float:
        params = {**DEFAULT_PARAMS, **_suggest_params(trial)}
        strat  = StrategyV53(params)
        result = strat.run(ohlcv_df, btc_1h_df)
        m      = result["metrics"]
        # Penalise if too few trades (unreliable stats)
        if m["n_trades"] < 10:
            return -10.0
        # Primary objective: Sharpe; secondary tiebreak: Calmar
        return m["sharpe"] + 0.1 * m["calmar"]

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=20),
    )

    callbacks = []
    if show_progress:
        try:
            from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn
            _progress_state = {"progress": None, "task": None}

            def _rich_callback(study, trial):
                if _progress_state["progress"] is None:
                    return
                _progress_state["progress"].advance(_progress_state["task"])

            # We'll manage progress outside optuna callbacks for cleaner output
        except ImportError:
            pass

    study.optimize(objective, n_trials=n_trials, show_progress_bar=show_progress,
                   gc_after_trial=True)

    best_params = {**DEFAULT_PARAMS, **study.best_params}

    # Re-run with best params to get full metrics
    strat      = StrategyV53(best_params)
    result     = strat.run(ohlcv_df, btc_1h_df)
    metrics    = result["metrics"]
    n_trades   = metrics["n_trades"]

    # Persist results
    trials_df  = study.trials_dataframe()
    out = {
        "symbol":      symbol,
        "timeframe":   timeframe,
        "n_trials":    n_trials,
        "best_value":  round(study.best_value, 4),
        "best_params": best_params,
        "metrics":     metrics,
    }
    save_path = RESULTS_DIR / f"{symbol}_{timeframe}_optuna.json"
    with open(save_path, "w") as f:
        json.dump(out, f, indent=2, default=str)

    trials_csv = RESULTS_DIR / f"{symbol}_{timeframe}_trials.csv"
    trials_df.to_csv(trials_csv, index=False)

    print(f"\n✅  Optimization done: {symbol} {timeframe}")
    print(f"   Sharpe={out['best_value']:.3f}  "
          f"Trades={n_trades}  "
          f"Win={metrics['win_rate']:.1%}  "
          f"DD={metrics['max_dd']:.1%}")
    print(f"   Results saved → {save_path.name}")

    return out


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "SOLUSDT"
    tf  = sys.argv[2] if len(sys.argv) > 2 else "30m"
    n   = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    optimize(sym, tf, n_trials=n)
