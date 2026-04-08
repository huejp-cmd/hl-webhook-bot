#!/usr/bin/env python3
"""
price_divergence.py
Compare les prix entre Hyperliquid et Binance pour SOL et ETH sur 30 jours.
"""

import urllib.request
import json
import time
import statistics
from datetime import datetime, timezone

# ─── Helpers ────────────────────────────────────────────────────────────────

def fetch_hl(coin, interval, start_ms, end_ms):
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_binance_klines(symbol, interval, start_ms, end_ms, limit=1000, retries=3):
    url = (f"https://api.binance.com/api/v3/klines"
           f"?symbol={symbol}&interval={interval}"
           f"&startTime={start_ms}&endTime={end_ms}&limit={limit}")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt < retries - 1:
                print(f"  Binance erreur ({e}), retry dans 2s…")
                time.sleep(2)
            else:
                raise


def fetch_bybit_klines(symbol, interval_min, start_ms, end_ms, limit=1000):
    url = (f"https://api.bybit.com/v5/market/kline"
           f"?category=linear&symbol={symbol}&interval={interval_min}"
           f"&start={start_ms}&end={end_ms}&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.loads(r.read())
    # Bybit retourne desc, on inverse
    rows = result.get("result", {}).get("list", [])
    rows.reverse()
    # [ts, open, high, low, close, volume, turnover]
    return rows


def fetch_all_hl(coin, interval_str, start_ms, end_ms):
    """Pagine les données HL par fenêtres de 1440 barres (30 jours × 48 barres/jour)."""
    interval_ms = 30 * 60 * 1000  # 30 minutes en ms
    batch_size = 1440
    window_ms = batch_size * interval_ms

    all_candles = []
    cur = start_ms
    while cur < end_ms:
        batch_end = min(cur + window_ms, end_ms)
        candles = fetch_hl(coin, interval_str, cur, batch_end)
        if not candles:
            break
        all_candles.extend(candles)
        # HL retourne des dicts avec clé "t" (timestamp open)
        last_ts = candles[-1]["t"]
        cur = last_ts + interval_ms
        if len(candles) < batch_size:
            break
        time.sleep(0.3)
    return all_candles


def fetch_all_binance(symbol, interval_str, start_ms, end_ms, use_bybit=False, bybit_sym=None, bybit_interval=None):
    interval_ms = 30 * 60 * 1000
    batch_size = 1000
    window_ms = batch_size * interval_ms

    all_klines = []
    cur = start_ms
    while cur < end_ms:
        batch_end = min(cur + window_ms, end_ms)
        if use_bybit:
            rows = fetch_bybit_klines(bybit_sym, bybit_interval, cur, batch_end, limit=1000)
            # [ts_ms_str, open, high, low, close, ...]
            klines = [[int(r[0]), r[1], r[2], r[3], r[4]] for r in rows]
        else:
            rows = fetch_binance_klines(symbol, interval_str, cur, batch_end, limit=batch_size)
            # [open_time, open, high, low, close, ...]
            klines = [[r[0], r[1], r[2], r[3], r[4]] for r in rows]

        if not klines:
            break
        all_klines.extend(klines)
        last_ts = klines[-1][0]
        cur = last_ts + interval_ms
        if len(klines) < batch_size:
            break
        time.sleep(0.3)
    return all_klines


# ─── Main ───────────────────────────────────────────────────────────────────

def analyze_pair(coin_hl, binance_sym, bybit_sym, display_name, entry_price, sl_pct=0.02):
    print(f"\n⏳ Téléchargement données {display_name}…")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 30 * 24 * 3600 * 1000  # 30 jours

    # ── Hyperliquid ──
    print(f"  → Hyperliquid {coin_hl} …")
    hl_candles = fetch_all_hl(coin_hl, "30m", start_ms, now_ms)
    print(f"     {len(hl_candles)} barres récupérées")

    # Build HL dict: ts -> close
    hl_map = {}
    for c in hl_candles:
        ts = int(c["t"])
        close = float(c["c"])
        hl_map[ts] = close

    # ── Binance (fallback Bybit) ──
    use_bybit = False
    try:
        print(f"  → Binance {binance_sym} …")
        bn_klines = fetch_all_binance(binance_sym, "30m", start_ms, now_ms)
        if len(bn_klines) < 100:
            raise ValueError(f"Trop peu de barres Binance: {len(bn_klines)}")
        print(f"     {len(bn_klines)} barres récupérées")
    except Exception as e:
        print(f"  ⚠️  Binance échoué ({e}), bascule sur Bybit…")
        use_bybit = True
        bn_klines = fetch_all_binance(
            binance_sym, "30m", start_ms, now_ms,
            use_bybit=True, bybit_sym=bybit_sym, bybit_interval=30
        )
        print(f"     {len(bn_klines)} barres récupérées (Bybit)")

    # Build Binance/Bybit dict: ts -> close
    bn_map = {}
    for k in bn_klines:
        ts = int(k[0])
        close = float(k[4])
        bn_map[ts] = close

    # ── Alignement ──
    common_ts = sorted(set(hl_map.keys()) & set(bn_map.keys()))
    print(f"  ✓ {len(common_ts)} timestamps alignés")

    if not common_ts:
        return None, display_name, entry_price, sl_pct, use_bybit

    diffs = []
    for ts in common_ts:
        hl_c = hl_map[ts]
        bn_c = bn_map[ts]
        diff_pct = (hl_c - bn_c) / bn_c * 100
        diffs.append(diff_pct)

    n = len(diffs)
    mean_d = statistics.mean(diffs)
    median_d = statistics.median(diffs)
    std_d = statistics.stdev(diffs) if n > 1 else 0.0
    min_d = min(diffs)
    max_d = max(diffs)

    sorted_d = sorted(diffs)
    p5 = sorted_d[int(0.05 * n)]
    p95 = sorted_d[int(0.95 * n)]

    stats = {
        "n": n,
        "mean": mean_d,
        "median": median_d,
        "std": std_d,
        "min": min_d,
        "max": max_d,
        "p5": p5,
        "p95": p95,
        "entry_price": entry_price,
        "sl_pct": sl_pct,
        "display_name": display_name,
        "source": "Bybit" if use_bybit else "Binance",
    }
    return stats, display_name, entry_price, sl_pct, use_bybit


def format_report(eth_stats, sol_stats):
    lines = []
    lines.append("📊 DIVERGENCE PRIX HYPERLIQUID vs BINANCE — 30 jours")
    lines.append("=" * 55)
    lines.append("")

    for stats in [eth_stats, sol_stats]:
        if stats is None:
            lines.append("  ⚠️  Données insuffisantes pour ce pair.\n")
            continue
        src = stats["source"]
        name = stats["display_name"]
        n = stats["n"]
        lines.append(f"{name} 30M ({n} barres alignées) [vs {src}] :")
        lines.append(f"  Écart moyen    : {stats['mean']:+.4f}%")
        lines.append(f"  Écart médian   : {stats['median']:+.4f}%")
        lines.append(f"  Std deviation  : {stats['std']:.4f}%")
        lines.append(f"  Min/Max        : {stats['min']:+.4f}% / {stats['max']:+.4f}%")
        lines.append(f"  P5/P95         : {stats['p5']:+.4f}% / {stats['p95']:+.4f}%")
        lines.append("")

    lines.append("Impact sur trades :")
    for stats in [eth_stats, sol_stats]:
        if stats is None:
            continue
        ep = stats["entry_price"]
        sl_pct = stats["sl_pct"]
        sl_dollar = ep * sl_pct
        mean_abs = abs(stats["mean"])
        mean_dollar = mean_abs / 100 * ep
        pct_of_sl = mean_dollar / sl_dollar * 100 if sl_dollar else 0
        name = stats["display_name"]
        sign = "+" if stats["mean"] >= 0 else "-"
        lines.append(f"  → Sur un trade {name} (entry ~{ep}$, SL ~{sl_pct*100:.0f}% = {sl_dollar:.2f}$) :")
        lines.append(f"     Écart moyen en $ : {mean_abs:.4f}% × {ep} = {mean_dollar:.4f}$ ({pct_of_sl:.1f}% du SL)")

    lines.append("")

    # Conclusion
    max_pct_sl = 0
    for stats in [eth_stats, sol_stats]:
        if stats is None:
            continue
        ep = stats["entry_price"]
        sl_pct = stats["sl_pct"]
        sl_dollar = ep * sl_pct
        mean_dollar = abs(stats["mean"]) / 100 * ep
        pct_of_sl = mean_dollar / sl_dollar * 100 if sl_dollar else 0
        if pct_of_sl > max_pct_sl:
            max_pct_sl = pct_of_sl

    if max_pct_sl < 5:
        verdict = "négligeable"
    elif max_pct_sl < 15:
        verdict = "acceptable"
    else:
        verdict = "significatif"

    lines.append(f"Conclusion : écart {verdict} par rapport au SL de 2%")
    lines.append(f"  (impact max = {max_pct_sl:.1f}% du SL)")
    lines.append("")
    lines.append(f"Généré le : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    return "\n".join(lines)


def main():
    print("=" * 55)
    print("📊 DIVERGENCE HYPERLIQUID vs BINANCE — démarrage")
    print("=" * 55)

    # ETH : entry ref ~2100$
    eth_result, _, _, _, _ = analyze_pair(
        coin_hl="ETH",
        binance_sym="ETHUSDT",
        bybit_sym="ETHUSDT",
        display_name="ETH",
        entry_price=2100,
        sl_pct=0.02
    )

    # SOL : entry ref ~140$ (prix approximatif actuel)
    sol_result, _, _, _, _ = analyze_pair(
        coin_hl="SOL",
        binance_sym="SOLUSDT",
        bybit_sym="SOLUSDT",
        display_name="SOL",
        entry_price=140,
        sl_pct=0.02
    )

    report = format_report(eth_result, sol_result)

    print("\n")
    print(report)

    report_path = "/Users/huejeanpierre/.openclaw/workspace/trading/price_divergence_report.txt"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\n✅ Rapport sauvegardé : {report_path}")


if __name__ == "__main__":
    main()
