"""
HA+HMA × Labouchère — Apex v3  (traduction Python du Pine Script JP)
====================================================================
Génère des signaux LONG / SHORT / EXIT à partir de bougies OHLCV.
Utilisé par hl_webhook_server.py pour ETH 10M (et tout autre symbol).

Fonctionnement :
  - Signaux  : Heikin-Ashi (HA) + HMA50/HMA20 calculés sur HA Close
  - Exécution: prix des bougies classiques (close, high, low)
  - SL/TP    : ATR × multiplicateurs (prix classiques)
  - Breakeven: SL → entry dès que gain ≥ distance SL initiale
  - Labouchère identique au moteur SOL29 v6 (avec diviseur WIN)
  - Circuit breakers : perte jour / DD total
"""

import math
from dataclasses import dataclass, field
from typing import Optional, List
import logging

log = logging.getLogger("apex_v3")

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTES (identiques au Pine)
# ─────────────────────────────────────────────────────────────────────────────
UNIT_FACTOR = 0.5
LEV_MULT    = 2.0
MAX_MULT    = 6.0
MIN_MULT    = 0.5


# ─────────────────────────────────────────────────────────────────────────────
#  CALCUL HMA (Hull Moving Average)
#  hma(series, n) = wma(2*wma(n/2) - wma(n), sqrt(n))
# ─────────────────────────────────────────────────────────────────────────────
def _wma(series: List[float], period: int) -> List[float]:
    """Weighted Moving Average (poids linéaires)."""
    result = []
    for i in range(len(series)):
        if i < period - 1:
            result.append(float("nan"))
            continue
        weights = list(range(1, period + 1))
        vals    = series[i - period + 1 : i + 1]
        wsum    = sum(w * v for w, v in zip(weights, vals))
        result.append(wsum / sum(weights))
    return result


def hma(series: List[float], period: int) -> List[float]:
    """Hull Moving Average."""
    half     = max(2, period // 2)
    sqrt_p   = max(2, int(math.sqrt(period)))
    wma_half = _wma(series, half)
    wma_full = _wma(series, period)
    diff = []
    for h, f in zip(wma_half, wma_full):
        if math.isnan(h) or math.isnan(f):
            diff.append(float("nan"))
        else:
            diff.append(2.0 * h - f)
    return _wma(diff, sqrt_p)


# ─────────────────────────────────────────────────────────────────────────────
#  CALCUL ATR (Average True Range)
# ─────────────────────────────────────────────────────────────────────────────
def atr(highs: List[float], lows: List[float], closes: List[float], period: int) -> List[float]:
    trs = []
    for i in range(len(highs)):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]),
            )
            trs.append(tr)
    # RMA (Wilder's smoothing, comme Pine Script ta.atr)
    result = [float("nan")] * len(trs)
    for i in range(len(trs)):
        if i < period - 1:
            continue
        elif i == period - 1:
            result[i] = sum(trs[:period]) / period
        else:
            result[i] = (result[i - 1] * (period - 1) + trs[i]) / period
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  CALCUL HEIKIN ASHI
# ─────────────────────────────────────────────────────────────────────────────
def calc_ha(opens, highs, lows, closes):
    ha_close  = [(o + h + l + c) / 4.0 for o, h, l, c in zip(opens, highs, lows, closes)]
    ha_open   = [0.0] * len(opens)
    ha_open[0] = (opens[0] + closes[0]) / 2.0
    for i in range(1, len(opens)):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0
    ha_is_green = [hc >= ho for hc, ho in zip(ha_close, ha_open)]
    ha_is_red   = [not g for g in ha_is_green]
    return ha_close, ha_open, ha_is_green, ha_is_red


# ─────────────────────────────────────────────────────────────────────────────
#  ÉTAT APEX V3 (par symbol)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ApexState:
    symbol:        str
    start_capital: float = 50_000.0
    risk_pct:      float = 0.01         # 1%
    seq_init_val:  int   = 1
    leverage_lab:  float = 1.0
    base_divisor:  int   = 4
    sl_cap_pct:    float = 0.05
    hma_slow_len:  int   = 50
    hma_fast_len:  int   = 20
    atr_len:       int   = 14
    sl_atr_mult:   float = 1.5
    tp_atr_mult:   float = 2.0
    dd_pause_pct:  float = 0.04
    dd_reduce1_pct:float = 0.02
    dd_reduce2_pct:float = 0.03
    apex_daily_lim:float = 900.0
    apex_dd_lim:   float = 1800.0

    # Labouchère
    lab_seq:       List[int] = field(default_factory=lambda: [1, 1, 1, 1])
    lab_wins:      int   = 0
    lab_losses:    int   = 0
    last_divisor:  int   = 1

    # Position ouverte
    in_long:       bool  = False
    in_short:      bool  = False
    entry_price:   float = 0.0
    sl:            float = 0.0
    tp:            Optional[float] = None
    sl_init:       float = 0.0
    breakeven:     bool  = False

    # Capital
    equity:        float = 0.0
    day_start_eq:  float = 0.0
    last_day:      int   = -1

    def __post_init__(self):
        self.equity        = self.start_capital
        self.day_start_eq  = self.start_capital
        self.lab_seq       = [self.seq_init_val] * 4

    # ── Labouchère helpers ──────────────────────────────────────────────────
    @property
    def lab_bet(self) -> int:
        if len(self.lab_seq) == 0:
            return self.seq_init_val * 2
        if len(self.lab_seq) == 1:
            return self.lab_seq[0] * 2
        return self.lab_seq[0] + self.lab_seq[-1]

    @property
    def lab_mult(self) -> float:
        return max(MIN_MULT, min(MAX_MULT, float(self.lab_bet) * UNIT_FACTOR * LEV_MULT))

    @property
    def compound_cap(self) -> float:
        return max(self.equity, self.start_capital * 0.5)

    # ── Circuit breakers ────────────────────────────────────────────────────
    def current_dd(self) -> float:
        if self.equity < self.start_capital:
            return (self.start_capital - self.equity) / self.start_capital
        return 0.0

    def size_factor(self) -> float:
        dd = self.current_dd()
        if dd >= self.dd_pause_pct:
            return 0.0
        if dd >= self.dd_reduce2_pct:
            return 0.25
        if dd >= self.dd_reduce1_pct:
            return 0.5
        return 1.0

    def trading_allowed(self, daily_pnl: float) -> bool:
        total_dd = self.start_capital - self.equity
        return (daily_pnl > -self.apex_daily_lim
                and total_dd < self.apex_dd_lim
                and self.size_factor() > 0.0)

    # ── Calcul quantité ─────────────────────────────────────────────────────
    def calc_qty(self, entry_px: float, sl_px: float) -> float:
        dist     = max(abs(entry_px - sl_px), 1e-12)
        base_qty = (self.compound_cap * self.risk_pct) / dist
        raw_qty  = base_qty * self.leverage_lab * self.lab_mult
        raw_qty  = raw_qty if raw_qty > 0 else self.compound_cap * 0.001
        qty_cap_sl  = (self.compound_cap * self.sl_cap_pct) / dist
        qty_cap_abs = self.equity / entry_px
        qty_cap_lev = (self.compound_cap * self.leverage_lab) / entry_px
        return min(raw_qty, qty_cap_sl, qty_cap_abs, qty_cap_lev) * self.size_factor()

    # ── Mise à jour Labouchère après clôture ────────────────────────────────
    def on_trade_closed(self, pnl: float, position_notional: float):
        if pnl > 0:
            if position_notional >= self.start_capital:
                divisor_f   = max(float(self.base_divisor),
                                  math.floor(position_notional / self.start_capital) + float(self.base_divisor - 1))
                add_val     = max(1, round(float(self.lab_bet) / divisor_f))
                self.last_divisor = int(divisor_f)
            else:
                add_val = self.lab_bet
                self.last_divisor = 1
            self.lab_seq.append(add_val)
            self.lab_wins += 1
            log.info(f"[Apex v3] {self.symbol} WIN  pnl={pnl:+.2f} | +[{add_val}] | seq={self.lab_seq}")
        else:
            if len(self.lab_seq) >= 2:
                self.lab_seq.pop(-1)
                self.lab_seq.pop(0)
            elif len(self.lab_seq) == 1:
                self.lab_seq.clear()
            if not self.lab_seq:
                self.lab_seq = [self.seq_init_val] * 4
            self.lab_losses += 1
            log.info(f"[Apex v3] {self.symbol} LOSS pnl={pnl:+.2f} | seq={self.lab_seq}")
        self.equity += pnl


# ─────────────────────────────────────────────────────────────────────────────
#  MOTEUR DE SIGNAUX
#  Prend les N dernières bougies OHLCV et retourne le signal pour la dernière
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Signal:
    action:   str   # "long" | "short" | "exit_long" | "exit_short" | "sl_hit" | "tp_hit" | "breakeven" | "none"
    symbol:   str
    entry_px: float = 0.0
    sl:       float = 0.0
    tp:       Optional[float] = None
    qty:      float = 0.0
    atr:      float = 0.0
    comment:  str   = ""


def compute_signal(
    state:  ApexState,
    opens:  List[float],
    highs:  List[float],
    lows:   List[float],
    closes: List[float],
    day_of_bar: int = 0,   # entier YYYYMMDD pour circuit breaker jour
) -> Signal:
    """
    Calcule le signal Apex v3 pour la dernière bougie confirmée.
    Minimum de barres nécessaires : max(hma_slow_len*2, atr_len) + quelques barres.
    """
    n = len(closes)
    assert n == len(opens) == len(highs) == len(lows), "OHLCV length mismatch"
    min_bars = max(state.hma_slow_len * 2 + int(math.sqrt(state.hma_slow_len)),
                   state.atr_len) + 5
    if n < min_bars:
        return Signal("none", state.symbol, comment=f"not enough bars ({n}<{min_bars})")

    # ── Calculs HA ──────────────────────────────────────────────────────────
    ha_close, ha_open, ha_green, ha_red = calc_ha(opens, highs, lows, closes)

    # ── HMA sur HA Close ────────────────────────────────────────────────────
    hma_slow = hma(ha_close, state.hma_slow_len)
    hma_fast = hma(ha_close, state.hma_fast_len)

    # ── ATR ─────────────────────────────────────────────────────────────────
    atr_vals = atr(highs, lows, closes, state.atr_len)

    i  = n - 1  # dernière bougie confirmée
    i1 = n - 2  # bougie précédente

    hms  = hma_slow[i]
    hmf  = hma_fast[i]
    atrv = atr_vals[i]

    if math.isnan(hms) or math.isnan(hmf) or math.isnan(atrv):
        return Signal("none", state.symbol, comment="indicators not ready")

    # ── Circuit breaker jour ─────────────────────────────────────────────────
    if day_of_bar != state.last_day:
        state.day_start_eq = state.equity
        state.last_day     = day_of_bar
    daily_pnl = state.equity - state.day_start_eq

    allowed = state.trading_allowed(daily_pnl)

    c    = closes[i]
    hi   = highs[i]
    lo   = lows[i]

    hag  = ha_green[i]
    har  = ha_red[i]
    hag1 = ha_green[i1]
    har1 = ha_red[i1]

    # ── Breakeven check ─────────────────────────────────────────────────────
    if state.in_long and not state.breakeven and state.entry_price > 0:
        if c >= state.entry_price + state.sl_init:
            state.sl        = state.entry_price
            state.breakeven = True
            log.info(f"[Apex v3] {state.symbol} BREAKEVEN LONG @ {state.entry_price:.4f}")

    if state.in_short and not state.breakeven and state.entry_price > 0:
        if c <= state.entry_price - state.sl_init:
            state.sl        = state.entry_price
            state.breakeven = True
            log.info(f"[Apex v3] {state.symbol} BREAKEVEN SHORT @ {state.entry_price:.4f}")

    # ── SL hit ───────────────────────────────────────────────────────────────
    if state.in_short and state.sl > 0 and hi >= state.sl:
        comment = "BE ✗" if state.breakeven else "SL ✗"
        return Signal("sl_hit", state.symbol, entry_px=state.sl, comment=comment)

    if state.in_long and state.sl > 0 and lo <= state.sl:
        comment = "BE ✗" if state.breakeven else "SL ✗"
        return Signal("sl_hit", state.symbol, entry_px=state.sl, comment=comment)

    # ── TP hit (ATR fixe) ────────────────────────────────────────────────────
    if state.in_short and state.tp is not None and state.tp_atr_mult > 0:
        if lo <= state.tp:
            return Signal("tp_hit", state.symbol, entry_px=state.tp, comment="TP-ATR ✓")

    if state.in_long and state.tp is not None and state.tp_atr_mult > 0:
        if hi >= state.tp:
            return Signal("tp_hit", state.symbol, entry_px=state.tp, comment="TP-ATR ✓")

    # ── Exit signal (HA inversion) ───────────────────────────────────────────
    exit_short = (hag and har1 and ha_close[i] > hmf and state.in_short)
    exit_long  = (har and hag1 and ha_close[i] < hmf and state.in_long)

    if exit_short:
        return Signal("exit_short", state.symbol, entry_px=c, comment="TP-HA ✓")
    if exit_long:
        return Signal("exit_long",  state.symbol, entry_px=c, comment="TP-HA ✓")

    # ── Entrées ──────────────────────────────────────────────────────────────
    if not allowed:
        return Signal("none", state.symbol, comment="circuit_breaker")

    long_signal  = hag and ha_close[i] > hms and not state.in_long
    short_signal = har and ha_close[i] < hms and not state.in_short

    if long_signal:
        sl_px = c - atrv * state.sl_atr_mult
        tp_px = c + atrv * state.tp_atr_mult if state.tp_atr_mult > 0 else None
        qty   = state.calc_qty(c, sl_px)
        if qty <= 0:
            return Signal("none", state.symbol, comment="qty=0")
        return Signal("long", state.symbol,
                      entry_px=c, sl=sl_px, tp=tp_px, qty=qty, atr=atrv,
                      comment="HA↑ + HMA50")

    if short_signal:
        sl_px = c + atrv * state.sl_atr_mult
        tp_px = c - atrv * state.tp_atr_mult if state.tp_atr_mult > 0 else None
        qty   = state.calc_qty(c, sl_px)
        if qty <= 0:
            return Signal("none", state.symbol, comment="qty=0")
        return Signal("short", state.symbol,
                      entry_px=c, sl=sl_px, tp=tp_px, qty=qty, atr=atrv,
                      comment="HA↓ + HMA50")

    return Signal("none", state.symbol)


# ─────────────────────────────────────────────────────────────────────────────
#  TEST RAPIDE
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import random
    random.seed(42)
    logging.basicConfig(level=logging.INFO)

    # Génère des bougies synthétiques
    N = 300
    price = 2000.0
    opens, highs, lows, closes = [], [], [], []
    for _ in range(N):
        o  = price
        c  = o + random.uniform(-20, 22)
        h  = max(o, c) + random.uniform(0, 10)
        l  = min(o, c) - random.uniform(0, 10)
        opens.append(o); highs.append(h); lows.append(l); closes.append(c)
        price = c

    state = ApexState(symbol="ETH", start_capital=50_000)
    sig   = compute_signal(state, opens, highs, lows, closes)
    print(f"\n✅ Signal Apex v3 sur {N} bougies synthétiques ETH :")
    print(f"   action   : {sig.action}")
    print(f"   entry_px : {sig.entry_px:.2f}")
    print(f"   sl       : {sig.sl:.2f}")
    print(f"   tp       : {sig.tp}")
    print(f"   qty      : {sig.qty:.4f}")
    print(f"   comment  : {sig.comment}")
    print(f"   lab_mult : {state.lab_mult:.2f}×")
    print(f"   lab_seq  : {state.lab_seq}")
