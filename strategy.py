#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Бэктест трендовых стратегий на реальных свечах Binance (публичный API).

  python strategy.py --symbol BTCUSDT --interval 4h --years 3
  python strategy.py --symbol BTCUSDT --strategy ema --html report.html

Стратегии (long-only, вход/выход по close):
  donchian : пробой максимума N баров -> лонг; выход по минимуму M баров (или ATR-стоп)
  ema      : EMA(fast) выше EMA(slow) -> лонг, иначе кэш

Издержки: комиссия 0.10% за сделку + 0.02% проскальзывание, на вход и на выход.
Плеча нет, шорт нет, весь капитал в одну позицию.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "data", "klines")
UA = "Mozilla/5.0 (compatible; crypto-backtest/1.0)"
FEE = 0.0010
SLIP = 0.0002


# ---------------- данные ----------------

def fetch_klines(symbol: str, interval: str, years: float) -> list[dict]:
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s_%s_%gy.json" % (symbol, interval, years))
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 3600:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    end = int(time.time() * 1000)
    need = int(years * 365 * 24 * 3600 * 1000 / _interval_ms(interval))
    out: list[dict] = []
    while need > 0:
        url = ("https://api.binance.com/api/v3/klines?" +
               urllib.parse.urlencode({"symbol": symbol, "interval": interval,
                                       "limit": min(1000, need), "endTime": end}))
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            batch = json.loads(r.read().decode())
        if not batch:
            break
        out = batch + out
        end = batch[0][0] - 1
        need -= len(batch)
        if len(batch) < 2:
            break
        time.sleep(0.15)
    rows = [{"t": b[0], "o": float(b[1]), "h": float(b[2]), "l": float(b[3]),
             "c": float(b[4]), "v": float(b[5])} for b in out]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    return rows


def _interval_ms(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1])
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}[unit]


# ---------------- индикаторы ----------------

def ema(vals: list[float], n: int) -> list[float | None]:
    k = 2 / (n + 1)
    out: list[float | None] = []
    prev = None
    for v in vals:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def atr(rows: list[dict], n: int = 14) -> list[float | None]:
    tr, out = [], []
    for i, r in enumerate(rows):
        if i == 0:
            tr.append(r["h"] - r["l"])
        else:
            pc = rows[i - 1]["c"]
            tr.append(max(r["h"] - r["l"], abs(r["h"] - pc), abs(r["l"] - pc)))
        out.append(None if i < n else sum(tr[i - n + 1:i + 1]) / n)
    return out


# ---------------- сигналы ----------------

def signals(rows: list[dict], name: str, params: dict) -> list[int]:
    """Возвращает список 0/1 (позиция держится со следующего бара)."""
    closes = [r["c"] for r in rows]
    pos = [0] * len(rows)
    if name == "ema":
        f, s = ema(closes, params["fast"]), ema(closes, params["slow"])
        for i in range(len(rows)):
            pos[i] = 1 if f[i] is not None and s[i] is not None and f[i] > s[i] else 0
        return pos
    if name == "donchian":
        up, dn = params["entry"], params["exit"]
        a = atr(rows, params.get("atr_n", 14))
        in_pos, stop = False, 0.0
        for i in range(len(rows)):
            if i < max(up, dn) + 1:
                pos[i] = 0
                continue
            hh = max(r["h"] for r in rows[i - up:i])      # максимум предыдущих up баров
            ll = min(r["l"] for r in rows[i - dn:i])
            if not in_pos:
                if rows[i]["c"] > hh:
                    in_pos, stop = True, rows[i]["c"] - params.get("atr_mult", 2.5) * (a[i] or 0)
            else:
                if rows[i]["c"] < ll or (a[i] and rows[i]["c"] < stop):
                    in_pos = False
            pos[i] = 1 if in_pos else 0
            if in_pos and a[i]:
                stop = max(stop, rows[i]["c"] - params.get("atr_mult", 2.5) * a[i])
        return pos
    raise ValueError("unknown strategy: " + name)


# ---------------- бэктест ----------------

def run(rows: list[dict], pos: list[int], fee: float = FEE, slip: float = SLIP) -> dict:
    eq, trades = [1.0], []
    entry_px, entry_eq = None, None
    cur = 1.0
    for i in range(1, len(rows)):
        held = pos[i - 1]                      # решение на баре i-1, исполнение на i (close)
        ret = rows[i]["c"] / rows[i - 1]["c"] - 1
        if held:
            cur *= 1 + ret
            if entry_px is None:
                entry_px, entry_eq = rows[i - 1]["c"], cur / (1 + ret)
        if held != pos[i]:
            cost = fee + slip
            cur *= 1 - cost
            if held and entry_px is not None:
                trades.append({"entry": entry_px, "exit": rows[i]["c"],
                               "ret_pct": (rows[i]["c"] / entry_px - 1 - 2 * cost) * 100,
                               "bars": i - 1})
                entry_px = None
            elif not held:
                entry_px = rows[i]["c"]
        eq.append(cur)
    if entry_px is not None:
        trades.append({"entry": entry_px, "exit": rows[-1]["c"],
                       "ret_pct": (rows[-1]["c"] / entry_px - 1 - 2 * (fee + slip)) * 100,
                       "bars": len(rows) - 1})

    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    years = (rows[-1]["t"] - rows[0]["t"]) / (365 * 24 * 3600 * 1000)
    total = eq[-1] - 1
    cagr = (eq[-1] ** (1 / years) - 1) if years > 0 and eq[-1] > 0 else 0
    bars_per_year = len(rows) / years if years else 0
    vol = statistics.pstdev(rets) * math.sqrt(bars_per_year) if len(rets) > 1 else 0
    sharpe = (statistics.fmean(rets) * bars_per_year) / vol if vol else 0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    wins = [t for t in trades if t["ret_pct"] > 0]
    bh = rows[-1]["c"] / rows[0]["c"] - 1
    return {
        "bars": len(rows), "years": round(years, 2),
        "from": datetime.fromtimestamp(rows[0]["t"] / 1000, timezone.utc).date().isoformat(),
        "to": datetime.fromtimestamp(rows[-1]["t"] / 1000, timezone.utc).date().isoformat(),
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_dd_pct": round(mdd * 100, 2),
        "sharpe": round(sharpe, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "avg_trade_pct": round(statistics.fmean([t["ret_pct"] for t in trades]), 2) if trades else None,
        "exposure_pct": round(sum(pos) / len(pos) * 100, 1),
        "buy_hold_pct": round(bh * 100, 2),
        "final_equity": round(eq[-1], 4),
        "_equity": [round(v, 6) for v in eq],
        "_trades": trades,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--strategy", default="donchian", choices=["donchian", "ema", "both"])
    ap.add_argument("--fast", type=int, default=20)
    ap.add_argument("--slow", type=int, default=50)
    ap.add_argument("--entry", type=int, default=20)
    ap.add_argument("--exit", type=int, default=10)
    ap.add_argument("--atr-mult", type=float, default=2.5)
    ap.add_argument("--html", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows = fetch_klines(a.symbol, a.interval, a.years)
    variants = []
    if a.strategy in ("donchian", "both"):
        variants.append(("donchian %d/%d ATRx%.1f" % (a.entry, a.exit, a.atr_mult),
                         signals(rows, "donchian", {"entry": a.entry, "exit": a.exit,
                                                    "atr_mult": a.atr_mult})))
    if a.strategy in ("ema", "both"):
        variants.append(("ema %d/%d" % (a.fast, a.slow),
                         signals(rows, "ema", {"fast": a.fast, "slow": a.slow})))

    res = {}
    print("%s %s, %d баров, %s .. %s\n" % (a.symbol, a.interval, len(rows),
          datetime.fromtimestamp(rows[0]["t"] / 1000, timezone.utc).date(),
          datetime.fromtimestamp(rows[-1]["t"] / 1000, timezone.utc).date()))
    hdr = "%-26s %10s %8s %9s %7s %6s %7s %8s" % (
        "Стратегия", "Доход %", "CAGR %", "MaxDD %", "Sharpe", "Сдел", "Win %", "В рынке")
    print(hdr); print("-" * len(hdr))
    for name, pos in variants:
        r = run(rows, pos)
        res[name] = r
        print("%-26s %+10.2f %+8.2f %9.2f %7.2f %6d %7s %7.1f%%" % (
            name, r["total_return_pct"], r["cagr_pct"], r["max_dd_pct"], r["sharpe"],
            r["trades"], "-" if r["win_rate_pct"] is None else "%.1f" % r["win_rate_pct"],
            r["exposure_pct"]))
    bh = variants and run(rows, [1] * len(rows))
    print("%-26s %+10.2f %+8.2f %9.2f %7s %6s %7s %7s" % (
        "buy & hold", bh["buy_hold_pct"], bh["cagr_pct"], bh["max_dd_pct"], "-", "-", "-", "100%"))

    if a.html:
        write_html(a.html, a.symbol, a.interval, rows, res)
        print("\nHTML-отчёт:", a.html)
    if a.json:
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                          for k, v in res.items()}, ensure_ascii=False, indent=2))
    return 0


def write_html(path: str, symbol: str, interval: str, rows: list[dict], res: dict) -> None:
    """Простая SVG-кривая капитала по каждой стратегии + buy & hold."""
    W, H, P = 900, 320, 44
    all_eq = [r["_equity"] for r in res.values()]
    bh_curve = [rows[i]["c"] / rows[0]["c"] for i in range(len(rows))]
    all_eq.append(bh_curve)
    lo = min(min(c) for c in all_eq)
    hi = max(max(c) for c in all_eq)
    n = len(rows)

    def pts(curve):
        return " ".join("%.1f,%.1f" % (P + i / (n - 1) * (W - 2 * P),
                                       H - P - (v - lo) / (hi - lo) * (H - 2 * P))
                        for i, v in enumerate(curve))

    colors = ["#4f8cff", "#31c48d", "#f59e0b", "#9ca3af"]
    paths = []
    for i, (name, r) in enumerate(res.items()):
        paths.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>'
                     % (colors[i % 4], pts(r["_equity"])))
        paths.append('<text x="%d" y="%d" fill="%s" font-size="13">%s: %+.1f%%</text>'
                     % (P + 8, 22 + i * 18, colors[i % 4], name, r["total_return_pct"]))
    paths.append('<polyline fill="none" stroke="%s" stroke-width="1.5" stroke-dasharray="5 4" points="%s"/>'
                 % (colors[-1], pts(bh_curve)))
    paths.append('<text x="%d" y="%d" fill="%s" font-size="13">buy &amp; hold: %+.1f%%</text>'
                 % (P + 8, 22 + len(res) * 18, colors[-1], (bh_curve[-1] - 1) * 100))
    paths.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#d0d7de"/>'
                 % (P, H - P, W - P, H - P))
    svg = ('<svg viewBox="0 0 %d %d" width="100%%" style="max-width:920px">%s</svg>'
           % (W, H, "".join(paths)))
    html = ("<h3>%s %s — кривая капитала (бэктест)</h3>%s"
            "<p style=\"font-size:12px;opacity:.6\">Реальные свечи Binance, комиссия 0.10%% + "
            "проскальзывание 0.02%% на сделку, без плеча. Прошлые результаты не гарантируют будущих.</p>"
            % (symbol, interval, svg))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    raise SystemExit(main())
