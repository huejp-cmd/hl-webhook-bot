"""
Trade Journal — JP Autonomous Bot
==================================
Persistent trade log for DRY_RUN simulation.
Attempts to write to /app/trades.json (Railway persistent volume),
falls back to /tmp/trades.json, and always keeps an in-memory copy.

Usage:
    import trade_journal
    tid = trade_journal.record_entry(coin, side, entry_price, qty, sl, tp,
                                     regime, capital, lab_mult)
    trade_journal.record_exit(tid, exit_price, exit_reason, pnl_usdc, pnl_pct)
    all_trades = trade_journal.get_all()
    stats = trade_journal.get_stats()
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("trade_journal")

# ==============================================================
#  STORAGE
# ==============================================================
_PATHS = ["/app/trades.json", "/tmp/trades.json"]
_lock  = threading.Lock()

# In-memory list of all trades
_trades: list = []


def _resolve_path() -> str:
    """Return first writable path from _PATHS."""
    for p in _PATHS:
        try:
            dirpath = os.path.dirname(p)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            # Test write
            with open(p, "a"):
                pass
            return p
        except Exception:
            continue
    return _PATHS[-1]  # fallback (may fail silently)


_storage_path: str = _resolve_path()


def _save():
    """Persist trades list to disk (best-effort)."""
    try:
        with open(_storage_path, "w") as f:
            json.dump(_trades, f, indent=2, default=str)
    except Exception as e:
        log.warning(f"[journal] Cannot write to {_storage_path}: {e}")


def _load():
    """Load trades from disk on startup."""
    global _trades
    for p in _PATHS:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    _trades = data
                    log.info(f"[journal] Loaded {len(_trades)} trades from {p}")
                    return
            except Exception as e:
                log.warning(f"[journal] Could not load {p}: {e}")
    log.info("[journal] No existing trade file found — starting fresh")


# Load on import
_load()


# ==============================================================
#  PUBLIC API
# ==============================================================

def record_entry(
    coin: str,
    side: str,
    entry_price: float,
    qty: float,
    sl: float,
    tp: float,
    regime: str,
    capital: float,
    lab_mult: float,
) -> str:
    """
    Record a new trade entry.

    Returns:
        trade_id (str) — store this in _positions[coin]['journal_id']
    """
    trade_id = str(uuid.uuid4())
    now      = datetime.now(timezone.utc).isoformat()

    trade = {
        "id":         trade_id,
        "coin":       coin,
        "side":       side,
        "entry_price": round(entry_price, 6),
        "qty":         round(qty, 6),
        "sl":          round(sl, 6),
        "tp":          round(tp, 6),
        "regime":      regime,
        "capital":     round(capital, 2),
        "lab_mult":    round(lab_mult, 4),
        "entry_ts":    now,
        "exit_price":  None,
        "exit_ts":     None,
        "exit_reason": None,
        "pnl_usdc":    None,
        "pnl_pct":     None,
        "status":      "OPEN",
    }

    with _lock:
        _trades.append(trade)
        _save()

    log.info(
        f"[journal] ENTRY recorded | {coin} {side.upper()} "
        f"qty={qty} @ {entry_price} | id={trade_id[:8]}"
    )
    return trade_id


def record_exit(
    trade_id: str,
    exit_price: float,
    exit_reason: str,
    pnl_usdc: float,
    pnl_pct: float,
) -> bool:
    """
    Update a trade with exit data.

    Args:
        trade_id    : ID returned by record_entry()
        exit_price  : price at which trade closed
        exit_reason : "SL" | "TP" | "manual" | etc.
        pnl_usdc    : profit/loss in USDC
        pnl_pct     : profit/loss as a percentage of capital

    Returns:
        True if trade was found and updated, False otherwise.
    """
    now = datetime.now(timezone.utc).isoformat()

    with _lock:
        for trade in _trades:
            if trade["id"] == trade_id:
                trade["exit_price"]  = round(exit_price, 6)
                trade["exit_ts"]     = now
                trade["exit_reason"] = exit_reason
                trade["pnl_usdc"]    = round(pnl_usdc, 4)
                trade["pnl_pct"]     = round(pnl_pct, 4)
                trade["status"]      = "CLOSED"
                _save()
                log.info(
                    f"[journal] EXIT recorded | {trade['coin']} {exit_reason} "
                    f"PnL={pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%) | id={trade_id[:8]}"
                )
                return True

    log.warning(f"[journal] record_exit: trade_id {trade_id[:8]} not found")
    return False


def get_all() -> list:
    """Return a copy of all trades (JSON-serialisable)."""
    with _lock:
        return list(_trades)


def get_stats() -> dict:
    """
    Compute performance statistics from all closed trades.

    Returns a dict with:
        total_trades, open_trades, wins, losses, win_rate,
        total_pnl_usdc, avg_win, avg_loss,
        best_trade, worst_trade, cumulative_pnl
    """
    with _lock:
        trades = list(_trades)

    closed  = [t for t in trades if t["status"] == "CLOSED" and t["pnl_usdc"] is not None]
    open_   = [t for t in trades if t["status"] == "OPEN"]

    wins    = [t for t in closed if t["pnl_usdc"] > 0]
    losses  = [t for t in closed if t["pnl_usdc"] <= 0]

    total_pnl   = sum(t["pnl_usdc"] for t in closed)
    avg_win     = sum(t["pnl_usdc"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss    = sum(t["pnl_usdc"] for t in losses) / len(losses) if losses else 0.0
    win_rate    = len(wins) / len(closed) * 100 if closed else 0.0

    best_trade  = max(closed, key=lambda t: t["pnl_usdc"])["pnl_usdc"] if closed else 0.0
    worst_trade = min(closed, key=lambda t: t["pnl_usdc"])["pnl_usdc"] if closed else 0.0

    # Cumulative PnL over time (list of running totals)
    cumulative = []
    running    = 0.0
    for t in closed:
        running += t["pnl_usdc"]
        cumulative.append(round(running, 4))

    return {
        "total_trades":   len(trades),
        "open_trades":    len(open_),
        "closed_trades":  len(closed),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(win_rate, 2),
        "total_pnl_usdc": round(total_pnl, 4),
        "avg_win":        round(avg_win, 4),
        "avg_loss":       round(avg_loss, 4),
        "best_trade":     round(best_trade, 4),
        "worst_trade":    round(worst_trade, 4),
        "cumulative_pnl": cumulative,
    }
