#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""walkforward.py — честная проверка: параметры подбираются на прошлом,
проверяются на будущем (expanding window, без заглядывания вперёд).

  python walkforward.py --symbol BTCUSDT --folds 5 --interval 4h --years 3

Каждый фолд:
  IS  = все бары до начала фолда  -> перебор сетки, выбор по Sharpe
  OOS = сам фолд (по умолчанию ~1/5 истории) -> считаем доход
Доход фолдов перемножается: получаем «честную» кривую, где параметры
каждый раз выбирались только на данных, доступных ДО фолда.
"""
from __future__ import annotations

import argparse
import itertools
import statistics

import strategy as S

GRID_DONCHIAN = [{"entry": e, "exit": x, "atr_mult": m}
                 for e, x in [(20, 10), (30, 15), (55, 20)] for m in (1.5, 2.5, 3.5)]
GRID_EMA = [{"fast": f, "slow": s} for f, s in [(10, 30), (20, 50), (50, 200)]]


def variants(name: str):
    grid = GRID_DONCHIAN if name == "donchian" else GRID_EMA
    for p in grid:
        label = ("don %d/%d ATRx%.1f" % (p["entry"], p["exit"], p["atr_mult"])
                 if name == "donchian" else "ema %d/%d" % (p["fast"], p["slow"]))
        yield label, p


def equity_of(rows, pos):
    r = S.run(rows, pos)
    return r["_equity"], r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="4h")
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--strategy", default="both", choices=["donchian", "ema", "both"])
    a = ap.parse_args()

    rows = S.fetch_klines(a.symbol, a.interval, a.years)
    n = len(rows)
    names = ["donchian", "ema"] if a.strategy == "both" else [a.strategy]
    min_is = int(n * 0.4)                     # первый фолд не раньше 40% истории
    bounds = [min_is + int((n - min_is) * i / a.folds) for i in range(a.folds + 1)]

    print("%s %s: %d баров, фолдов %d, IS начинается с 40%% истории\n"
          % (a.symbol, a.interval, n, a.folds))
    hdr = "%-4s %-22s %-6s %9s %9s %8s %7s" % ("#", "выбрано на IS", "IS Sh", "IS %", "OOS %", "OOS DD", "OOS Sh")
    print(hdr)
    print("-" * len(hdr))

    total = 1.0
    picks, oos_rets = [], []
    for k in range(a.folds):
        s0, s1 = bounds[k], bounds[k + 1]
        is_rows = rows[:s0]
        best = None
        for nm in names:
            for label, params in variants(nm):
                pos = S.signals(is_rows, nm, params)
                r = S.run(is_rows, pos)
                if best is None or r["sharpe"] > best[2]["sharpe"]:
                    best = (nm, label, r, params)
        nm, label, ris, params = best
        # сигналы считаем на всём окне (нужен прогрев), оцениваем ТОЛЬКО хвост фолда
        pos = S.signals(rows[:s1], nm, params)
        eq, full = equity_of(rows[:s1], pos)
        s1 = min(s1, len(eq) - 1)          # eq короче rows[:s1] на один элемент
        base = eq[s0] if s0 < len(eq) else 1.0
        oos = eq[s1] / base - 1
        seg = eq[s0:s1 + 1]
        peak, dd = seg[0], 0.0
        for v in seg:
            peak = max(peak, v)
            dd = min(dd, v / peak - 1)
        oos_sh = 0.0
        seg_rets = [seg[i] / seg[i - 1] - 1 for i in range(1, len(seg)) if seg[i - 1] > 0]
        if len(seg_rets) > 1:
            sd = statistics.pstdev(seg_rets)
            bars_per_year = len(rows) / full["years"] if full["years"] else 0
            oos_sh = (statistics.fmean(seg_rets) * bars_per_year / (sd * (bars_per_year ** 0.5))
                      if sd else 0.0)
        total *= 1 + oos
        picks.append(label)
        oos_rets.append(oos)
        print("%-4d %-22s %-6s %+9.1f %+9.2f %8.1f %7.2f"
              % (k + 1, label, "%.2f" % ris["sharpe"], ris["total_return_pct"],
                 oos * 100, dd * 100, oos_sh))

    bh_eq, bh = equity_of(rows, [1] * len(rows))
    bh_total = bh_eq[-1] / bh_eq[min_is] - 1 if min_is < len(bh_eq) else 0
    print("-" * len(hdr))
    print("Склеенный OOS-доход (%-d фолд.): %+.2f%%  |  buy & hold на том же участке: %+.2f%%"
          % (a.folds, (total - 1) * 100, bh_total * 100))
    print("Фолдов с плюсом: %d из %d. Параметры менялись: %s"
          % (sum(1 for r in oos_rets if r > 0), len(oos_rets),
             "да" if len(set(picks)) > 1 else "нет (%s)" % picks[0]))
    print("\nВажно: это вневыборочная проверка, но всё равно на одном активе и одной истории.")
    print("Прибыльность в прошлом не гарантирует прибыльность в будущем.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
