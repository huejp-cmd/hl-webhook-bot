"""
strategy_v53.py — JP v5.3 backtesting engine.

Faithfully replicates the Pine Script SOL29_v5_stepped_compound.pine logic:
  - Range bars construction
  - HMA / ATR / RSI / DMI-ADX / BB / VWAP indicators
  - inRange filter (ADX < 20 + low-volatility + BTC 1H range < 1%)
  - Three market regimes: Trending / Ranging / Explosive
  - Long/Short entry conditions for Trend and Explosive regimes
  - SL + TP + trailing-stop exit (ATR × 1.6)
  - Fixed-capital risk management (2% risk, 5% SL cap, leverage cap)

Usage
-----
    from strategy_v53 import StrategyV53
    strat   = StrategyV53(params)
    results = strat.run(ohlcv_df, btc_1h_df)
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field, asdict
from typing import Optional

from indicators import (
    hma, calc_atr, calc_rsi, calc_dmi_adx, calc_bb,
    calc_vwap_vah_val, build_range_bars,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Default parameters (must match v5.3 exactly)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PARAMS: dict = {
    # Range bars
    "use_fixed_range":  False,
    "fixed_range_size": 1.2,
    "atr_length":       10,
    # Indicators
    "adx_length":       14,
    "rsi_length":       100,
    "bb_length":        20,
    "bb_mult":          2.0,
    "vp_k":             0.75,
    # Volume filter
    "vol_filter":       True,
    "vol_mult":         1.4,
    # Risk management
    "risk_perc":        0.02,    # 2%
    "leverage":         2.0,
    "sl_cap_pct":       0.05,    # 5%
    "start_capital":    500.0,
    # Commission (Binance taker, round-trip)
    "commission":       0.00035, # 0.035% per side → 0.07% round-trip
    # Trailing stop offset multiplier
    "trail_atr_mult":   1.6,
    # TP multipliers by regime
    "tp_mult_trending": 4.0,
    "tp_mult_ranging":  2.5,
    "tp_mult_explosive":3.3,
    # inRange vol lookback
    "vol_lookback":     100,
    "vol_pct":          0.25,    # 25th-percentile threshold
}


# ─────────────────────────────────────────────────────────────────────────────
#  Trade record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_time:   object
    exit_time:    object
    side:         str       # "long" | "short"
    regime:       str       # "trending" | "explosive"
    entry_price:  float
    exit_price:   float
    qty:          float
    sl:           float
    tp:           float
    pnl:          float     # net P&L in $
    pnl_pct:      float     # P&L / capital
    exit_reason:  str       # "tp" | "sl" | "trail" | "end"


# ─────────────────────────────────────────────────────────────────────────────
#  Metrics helper
# ─────────────────────────────────────────────────────────────────────────────

def _compute_metrics(trades: list[Trade], equity: pd.Series,
                     start_capital: float) -> dict:
    if not trades:
        return {k: 0.0 for k in [
            "total_return_pct", "sharpe", "sortino", "max_dd",
            "win_rate", "profit_factor", "n_trades",
            "avg_win", "avg_loss", "calmar",
        ]}

    n       = len(trades)
    pnls    = np.array([t.pnl for t in trades])
    wins    = pnls[pnls > 0]
    losses  = pnls[pnls < 0]

    win_rate      = len(wins) / n if n else 0.0
    avg_win       = float(wins.mean()) if len(wins) else 0.0
    avg_loss      = float(losses.mean()) if len(losses) else 0.0
    gross_profit  = wins.sum() if len(wins) else 0.0
    gross_loss    = abs(losses.sum()) if len(losses) else 1e-12
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf

    final_equity      = float(equity.iloc[-1])
    total_return_pct  = (final_equity - start_capital) / start_capital * 100

    # Drawdown (on equity curve)
    roll_max  = equity.cummax()
    dd_series = (roll_max - equity) / roll_max
    max_dd    = float(dd_series.max())

    # Daily returns for Sharpe / Sortino
    daily_eq    = equity.resample("1D").last().ffill()
    daily_ret   = daily_eq.pct_change().dropna()
    if len(daily_ret) > 1:
        sharpe  = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252)) \
                  if daily_ret.std() > 0 else 0.0
        neg     = daily_ret[daily_ret < 0]
        sortino = float(daily_ret.mean() / neg.std() * np.sqrt(252)) \
                  if len(neg) > 1 and neg.std() > 0 else 0.0
    else:
        sharpe  = 0.0
        sortino = 0.0

    calmar = (total_return_pct / 100) / max_dd if max_dd > 0 else 0.0

    return {
        "total_return_pct": round(total_return_pct, 2),
        "sharpe":           round(sharpe,    3),
        "sortino":          round(sortino,   3),
        "max_dd":           round(max_dd,    4),
        "win_rate":         round(win_rate,  4),
        "profit_factor":    round(profit_factor, 3),
        "n_trades":         n,
        "avg_win":          round(avg_win,  2),
        "avg_loss":         round(avg_loss, 2),
        "calmar":           round(calmar,   3),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Main strategy class
# ─────────────────────────────────────────────────────────────────────────────

class StrategyV53:
    """
    JP v5.3 strategy back-tester.

    Parameters
    ----------
    params : dict (optional)
        Overrides for DEFAULT_PARAMS.
    """

    def __init__(self, params: Optional[dict] = None):
        self.p = {**DEFAULT_PARAMS, **(params or {})}

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self, ohlcv_df: pd.DataFrame,
            btc_1h_df: pd.DataFrame) -> dict:
        """
        Back-test v5.3 on ohlcv_df (any native timeframe).

        Parameters
        ----------
        ohlcv_df  : DataFrame with columns open/high/low/close/volume,
                    DatetimeIndex (UTC).
        btc_1h_df : BTC 1H OHLCV DataFrame (used for inRange filter).

        Returns
        -------
        dict with keys: trades, equity_curve, metrics
        """
        p = self.p

        # 1. Build range bars
        rb = build_range_bars(
            ohlcv_df,
            atr_length=p["atr_length"],
            use_fixed=p["use_fixed_range"],
            fixed_size=p["fixed_range_size"],
        )
        if len(rb) < 200:
            return self._empty_result("Not enough range bars")

        h, l, c, vol = rb["high"], rb["low"], rb["close"], rb["volume"]
        o_rb = rb["open"]

        # 2. Indicators on range bars
        atr    = calc_atr(h, l, c, p["atr_length"])
        rsi    = calc_rsi(c, p["rsi_length"])
        ma20   = hma(c, 20)
        ma50   = hma(c, 50)
        diplus, diminus, adx = calc_dmi_adx(h, l, c, p["adx_length"])
        basis, upper_bb, lower_bb = calc_bb(c, p["bb_length"], p["bb_mult"])
        vwap, vah, val = calc_vwap_vah_val(h, l, c, vol, p["vp_k"], atr)

        vol_sma  = vol.rolling(20, min_periods=1).mean()
        vol_up   = vol > vol_sma * p["vol_mult"]

        # 3. inRange filter ─ volatility percentile
        log_hl = np.log((h / l).replace(0, np.nan)).fillna(0)
        vol_window = log_hl.rolling(p["vol_lookback"], min_periods=10)
        # 25th-percentile of recent volatility
        vol_p25 = vol_window.quantile(p["vol_pct"])
        cur_vol = log_hl

        # 4. BTC 1H range → align to range-bar timestamps
        btc_range_1h = self._btc_range(btc_1h_df, rb.index)

        # 5. Build signals as arrays (faster iteration)
        N    = len(rb)
        vals = {
            "h":           h.to_numpy(),
            "l":           l.to_numpy(),
            "c":           c.to_numpy(),
            "vol":         vol.to_numpy(),
            "atr":         atr.to_numpy(),
            "rsi":         rsi.to_numpy(),
            "ma20":        ma20.to_numpy(),
            "ma50":        ma50.to_numpy(),
            "diplus":      diplus.to_numpy(),
            "diminus":     diminus.to_numpy(),
            "adx":         adx.to_numpy(),
            "upper_bb":    upper_bb.to_numpy(),
            "lower_bb":    lower_bb.to_numpy(),
            "vwap":        vwap.to_numpy(),
            "vah":         vah.to_numpy(),
            "val":         val.to_numpy(),
            "vol_up":      vol_up.to_numpy(),
            "cur_vol":     cur_vol.to_numpy(),
            "vol_p25":     vol_p25.to_numpy(),
            "btc_range":   btc_range_1h,
            "atr_sma20":   pd.Series(atr.to_numpy()).rolling(20, min_periods=1).mean().to_numpy(),
        }

        # 6. Simulation loop
        capital = p["start_capital"]
        equity  = [capital]
        times   = [rb.index[0]]
        trades: list[Trade] = []
        position = None   # None or dict

        for i in range(50, N):   # warm-up: 50 bars
            ts  = rb.index[i]
            ci  = vals["c"][i]
            hi  = vals["h"][i]
            li  = vals["l"][i]
            atri = vals["atr"][i]

            # ── Check open position exit ──────────────────────────────────
            if position is not None:
                pos = position
                exit_price, exit_reason = self._check_exit(
                    pos, ci, hi, li, atri, ts)
                if exit_price is not None:
                    comm  = exit_price * pos["qty"] * p["commission"]
                    pnl   = (
                        (exit_price - pos["entry"]) * pos["qty"]
                        if pos["side"] == "long"
                        else (pos["entry"] - exit_price) * pos["qty"]
                    ) - comm - pos["entry_comm"]
                    capital += pnl
                    trades.append(Trade(
                        entry_time  = pos["entry_time"],
                        exit_time   = ts,
                        side        = pos["side"],
                        regime      = pos["regime"],
                        entry_price = pos["entry"],
                        exit_price  = exit_price,
                        qty         = pos["qty"],
                        sl          = pos["sl"],
                        tp          = pos["tp"],
                        pnl         = round(pnl, 4),
                        pnl_pct     = round(pnl / p["start_capital"], 6),
                        exit_reason = exit_reason,
                    ))
                    position = None

                else:
                    # Update trailing stop
                    position = self._update_trail(pos, ci, hi, li, atri)
                    equity.append(capital)
                    times.append(ts)
                    continue

            # ── Compute regime & signals ──────────────────────────────────
            v = {k: vals[k][i] for k in vals}

            in_range = (
                v["adx"] < 20
                and (np.isnan(v["vol_p25"]) or v["cur_vol"] < v["vol_p25"])
                and v["btc_range"] < 0.01
            )
            if in_range:
                equity.append(capital)
                times.append(ts)
                continue

            volatility_high = v["atr"] > v["atr_sma20"] * 1.5
            trending        = v["adx"] > 25
            ranging         = (v["adx"] < 20
                               and v["h"] <= v["upper_bb"]
                               and v["l"] >= v["lower_bb"])
            explosive_raw   = (volatility_high
                               and (v["rsi"] > 75 or v["rsi"] < 25)
                               and (not p["vol_filter"] or v["vol_up"]))
            is_explosive = explosive_raw and not (trending or ranging)
            is_trending  = trending and not explosive_raw
            # is_ranging = ranging and not (trending or explosive_raw)

            # Direction filters
            bull_trend  = v["diplus"] > v["diminus"] and ci > v["ma50"]
            bear_trend  = v["diminus"] > v["diplus"] and ci < v["ma50"]
            strong_bull = v["adx"] > 30 and v["diplus"] > v["diminus"] * 1.5
            strong_bear = v["adx"] > 30 and v["diminus"] > v["diplus"] * 1.5
            valid_long  = not strong_bear
            valid_short = not strong_bull

            # Entry: trend pullback
            pullback_long  = (li <= v["ma20"] and ci > v["ma20"]
                              and 40 < v["rsi"] < 65)
            pullback_short = (hi >= v["ma20"] and ci < v["ma20"]
                              and 35 < v["rsi"] < 60)

            # Entry: explosive breakout
            prev_c = vals["c"][i - 1] if i > 0 else ci
            breakout_up   = (ci > v["vah"]
                             and volatility_high
                             and (ci - prev_c) > v["atr"] * 0.8
                             and (not p["vol_filter"] or v["vol_up"]))
            breakout_down = (ci < v["val"]
                             and volatility_high
                             and (ci - prev_c) < -v["atr"] * 0.8
                             and (not p["vol_filter"] or v["vol_up"]))

            entry_long_trend     = is_trending  and bull_trend  and pullback_long
            entry_long_explosive = is_explosive and breakout_up
            entry_short_trend    = is_trending  and bear_trend  and pullback_short
            entry_short_explosive= is_explosive and breakout_down

            # ── Enter long ────────────────────────────────────────────────
            if (entry_long_trend or entry_long_explosive) and valid_long:
                sl, tp, qty, trail = self._calc_entry(
                    "long", ci, li, hi, atri, is_trending,
                    is_explosive, v["val"], i, vals, capital)
                if qty > 0:
                    entry_comm = ci * qty * p["commission"]
                    capital   -= entry_comm   # deduct commission at entry
                    position   = {
                        "side":        "long",
                        "regime":      "trending" if is_trending else "explosive",
                        "entry":       ci,
                        "entry_time":  ts,
                        "entry_comm":  entry_comm,
                        "sl":          sl,
                        "tp":          tp,
                        "qty":         qty,
                        "trail_active":trail > 0,
                        "trail_points":trail,
                        "trail_offset":atri * p["trail_atr_mult"],
                        "trail_sl":    sl,   # moves up as price rises
                        "best_price":  ci,
                    }

            # ── Enter short ───────────────────────────────────────────────
            elif (entry_short_trend or entry_short_explosive) and valid_short:
                sl, tp, qty, trail = self._calc_entry(
                    "short", ci, li, hi, atri, is_trending,
                    is_explosive, v["vah"], i, vals, capital)
                if qty > 0:
                    entry_comm = ci * qty * p["commission"]
                    capital   -= entry_comm
                    position   = {
                        "side":        "short",
                        "regime":      "trending" if is_trending else "explosive",
                        "entry":       ci,
                        "entry_time":  ts,
                        "entry_comm":  entry_comm,
                        "sl":          sl,
                        "tp":          tp,
                        "qty":         qty,
                        "trail_active":trail > 0,
                        "trail_points":trail,
                        "trail_offset":atri * p["trail_atr_mult"],
                        "trail_sl":    sl,
                        "best_price":  ci,
                    }

            equity.append(capital)
            times.append(ts)

        # Close any open position at last bar
        if position is not None:
            last_c    = vals["c"][-1]
            comm      = last_c * position["qty"] * p["commission"]
            pnl       = (
                (last_c - position["entry"]) * position["qty"]
                if position["side"] == "long"
                else (position["entry"] - last_c) * position["qty"]
            ) - comm - position["entry_comm"]
            capital  += pnl
            trades.append(Trade(
                entry_time  = position["entry_time"],
                exit_time   = rb.index[-1],
                side        = position["side"],
                regime      = position["regime"],
                entry_price = position["entry"],
                exit_price  = last_c,
                qty         = position["qty"],
                sl          = position["sl"],
                tp          = position["tp"],
                pnl         = round(pnl, 4),
                pnl_pct     = round(pnl / p["start_capital"], 6),
                exit_reason = "end",
            ))

        eq_series = pd.Series(equity, index=times[:len(equity)], name="equity")
        metrics   = _compute_metrics(trades, eq_series, p["start_capital"])

        return {
            "trades":       trades,
            "equity_curve": eq_series,
            "metrics":      metrics,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _calc_entry(self, side, ci, li, hi, atri,
                    is_trending, is_explosive, band_level,
                    i, vals, capital):
        """Compute SL, TP, quantity and trail for a new entry."""
        p = self.p
        step_capital = p["start_capital"]   # fixed capital (non-compound)

        if side == "long":
            # SL: highest of (lowest low last 15 bars * 0.995) and (close * 0.985)
            low_15 = min(vals["l"][max(0, i - 15): i + 1]) * 0.995
            sl_base = low_15
            sl = max(sl_base, ci * 0.985)
        else:
            high_15 = max(vals["h"][max(0, i - 15): i + 1]) * 1.005
            sl_base = high_15
            sl = min(sl_base, ci * 1.015)

        if is_trending:
            tp_mult = p["tp_mult_trending"]
        elif is_explosive:
            tp_mult = p["tp_mult_explosive"]
        else:
            tp_mult = p["tp_mult_ranging"]

        dist = abs(ci - sl)
        if dist < 1e-12:
            dist = 1e-12

        if side == "long":
            tp = ci + dist * tp_mult
        else:
            tp = ci - dist * tp_mult

        # Qty calculation (matches Pine calcQty + capQty)
        base_qty  = (step_capital * p["risk_perc"]) / dist
        raw_qty   = max(base_qty * p["leverage"], step_capital * 0.001)

        # Cap 1: max exposure = step_capital * leverage / entry_price
        cap_exposure = (step_capital * p["leverage"]) / ci
        # Cap 2: max loss at SL = sl_cap_pct * step_capital
        cap_sl       = (step_capital * p["sl_cap_pct"]) / dist

        qty = min(raw_qty, min(cap_exposure, cap_sl))

        trail = dist if dist > 0 else 0.0
        return sl, tp, qty, trail

    def _check_exit(self, pos, ci, hi, li, atri, ts):
        """Return (exit_price, reason) or (None, None) if still open."""
        if pos["side"] == "long":
            # Hit stop loss
            if li <= pos["trail_sl"]:
                return pos["trail_sl"], "sl" if pos["trail_sl"] == pos["sl"] else "trail"
            # Hit take profit
            if hi >= pos["tp"]:
                return pos["tp"], "tp"
        else:
            if hi >= pos["trail_sl"]:
                return pos["trail_sl"], "sl" if pos["trail_sl"] == pos["sl"] else "trail"
            if li <= pos["tp"]:
                return pos["tp"], "tp"
        return None, None

    def _update_trail(self, pos, ci, hi, li, atri):
        """Advance trailing stop if price moved favourably."""
        if not pos["trail_active"]:
            return pos

        p      = self.p
        offset = pos["trail_offset"]

        if pos["side"] == "long":
            if hi > pos["best_price"]:
                pos = {**pos, "best_price": hi}
                new_trail_sl = hi - offset
                if new_trail_sl > pos["trail_sl"]:
                    pos = {**pos, "trail_sl": new_trail_sl}
        else:
            if li < pos["best_price"]:
                pos = {**pos, "best_price": li}
                new_trail_sl = li + offset
                if new_trail_sl < pos["trail_sl"]:
                    pos = {**pos, "trail_sl": new_trail_sl}
        return pos

    def _btc_range(self, btc_1h_df: pd.DataFrame,
                   target_index: pd.DatetimeIndex) -> np.ndarray:
        """
        Compute BTC 1H (high-low)/prev_close for each range-bar timestamp.
        Uses forward-fill to align BTC 1H bars to range-bar timestamps.
        """
        btc = btc_1h_df.copy()
        btc.index = pd.to_datetime(btc.index, utc=True)
        btc["range_pct"] = (btc["high"] - btc["low"]) / btc["close"].shift(1)
        btc["range_pct"] = btc["range_pct"].fillna(0.0)

        # Reindex to range-bar timestamps, forward-fill
        aligned = btc["range_pct"].reindex(
            btc.index.union(target_index)).sort_index().ffill()
        result  = aligned.reindex(target_index).fillna(0.0)
        return result.to_numpy()

    def _empty_result(self, reason: str) -> dict:
        empty_eq = pd.Series([self.p["start_capital"]], name="equity")
        return {
            "trades":       [],
            "equity_curve": empty_eq,
            "metrics":      _compute_metrics([], empty_eq, self.p["start_capital"]),
            "error":        reason,
        }
