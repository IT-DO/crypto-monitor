#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report.py — один HTML: живые котировки + кривые бэктеста (EMA 50/200).
  python report.py --out report.html
"""
from __future__ import annotations

import argparse
import json
import os

import monitor
import strategy as S

COLORS = ["#4f8cff", "#31c48d", "#f59e0b"]


def curve_svg(series: dict[str, list[float]], title: str) -> str:
    W, H, P = 880, 300, 46
    # прореживаем до ~500 точек: SVG легче, картинка не меняется
    screen = {}
    for name, c in series.items():
        step = max(1, len(c) // 500)
        th = c[::step] + ([c[-1]] if c[::step][-1] != c[-1] else [])
        screen[name] = th
    series = screen
    lo = min(min(c) for c in series.values())
    hi = max(max(c) for c in series.values())
    n = len(next(iter(series.values())))
    parts = []
    for i, (name, curve) in enumerate(series.items()):
        col = COLORS[i % len(COLORS)]
        pts = " ".join("%.1f,%.1f" % (P + j / (n - 1) * (W - 2 * P),
                                      H - P - (v - lo) / (hi - lo) * (H - 2 * P))
                       for j, v in enumerate(curve))
        parts.append('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' % (col, pts))
        parts.append('<text x="%d" y="%d" fill="%s" font-size="12">%s: x%.2f</text>'
                     % (P + 6, 20 + i * 16, col, name, curve[-1]))
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="var(--border)"/>' % (P, H - P, W - P, H - P))
    parts.append('<text x="%d" y="%d" fill="var(--muted-foreground)" font-size="11">%s</text>'
                 % (P, H - 14, title))
    return '<svg viewBox="0 0 %d %d" width="100%%">%s</svg>' % (W, H, "".join(parts))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="report.html")
    a = ap.parse_args()
    base = os.path.dirname(os.path.abspath(__file__))

    cfg = json.load(open(os.path.join(base, "config.json"), encoding="utf-8"))
    snap = monitor.snapshot(cfg)

    rows_html = "".join(
        "<tr><td style='font-family:ui-monospace,monospace'>%s</td>"
        "<td style='text-align:right'>%s</td><td style='text-align:right;color:%s'>%s</td>"
        "<td style='text-align:right'>%s</td><td style='text-align:right'>%s</td></tr>" % (
            r["symbol"], "{:,.2f}".format(r["price"]),
            "#16a34a" if (r["chg24_pct"] or 0) >= 0 else "#dc2626",
            "n/a" if r["chg24_pct"] is None else "%+.2f%%" % r["chg24_pct"],
            "n/a" if r["arb_edge_pct"] is None else "%+.3f%%" % r["arb_edge_pct"],
            "n/a" if r["dispersion_pct"] is None else "%.3f%%" % r["dispersion_pct"])
        for r in snap["rows"])

    series, stats, cache = {}, [], {}
    for sym in cfg["symbols"]:
        rows = cache[sym] = S.fetch_klines(sym, "4h", 3)
        pos = S.signals(rows, "ema", {"fast": 50, "slow": 200})
        r = S.run(rows, pos)
        bh = S.run(rows, [1] * len(rows))
        series[sym] = r["_equity"]
        stats.append((sym, r, bh))
    btc = cache.get("BTCUSDT") or S.fetch_klines("BTCUSDT", "4h", 3)
    series["buy & hold (BTC)"] = [x["c"] / btc[0]["c"] for x in btc]

    def row(sym, r, bh):
        return ("<tr><td>%s</td><td style='text-align:right'>%+.1f%%</td><td style='text-align:right'>%+.1f%%</td>"
                "<td style='text-align:right'>%.1f%%</td><td style='text-align:right'>%.2f</td>"
                "<td style='text-align:right'>%d</td><td style='text-align:right'>%+.1f%%</td>"
                "<td style='text-align:right'>%.1f%%</td></tr>" % (
                    sym, r["total_return_pct"], r["cagr_pct"], r["max_dd_pct"], r["sharpe"],
                    r["trades"], bh["buy_hold_pct"], bh["max_dd_pct"]))

    stats_html = "".join(row(*s) for s in stats)
    th = "style='text-align:right;font-weight:600'"
    html = """<div style="font-family:var(--app-font,inherit);color:var(--foreground)">
<h3 style="margin:0 0 2px">Крипто-мониторинг · %(ts)s</h3>
<p style="margin:0 0 10px;color:var(--muted-foreground);font-size:12px">5 бирж, медианная цена. Арбитраж &gt; 0 — покупка дешевле, чем продажа на другой бирже, до комиссий.</p>
<table style="border-collapse:collapse;width:100%%;font-size:13px;margin-bottom:16px">
<tr style="border-bottom:1px solid var(--border)"><th style="text-align:left">Пара</th><th %(th)s>Цена</th><th %(th)s>24ч</th><th %(th)s>Арбитраж</th><th %(th)s>Разброс</th></tr>
%(rows)s</table>
<h3 style="margin:0 0 2px">Бэктест: EMA 50/200, 4h, 3 года</h3>
<p style="margin:0 0 8px;color:var(--muted-foreground);font-size:12px">Реальные свечи Binance, комиссия 0.10%% + слиппедж 0.02%% на сторону, без плеча.</p>
%(curve)s
<table style="border-collapse:collapse;width:100%%;font-size:13px;margin-top:12px">
<tr style="border-bottom:1px solid var(--border)"><th style="text-align:left">Пара</th><th %(th)s>Доход</th><th %(th)s>CAGR</th><th %(th)s>MaxDD</th><th %(th)s>Sharpe</th><th %(th)s>Сделок</th><th %(th)s>B&amp;H</th><th %(th)s>DD B&amp;H</th></tr>
%(stats)s</table>
<p style="color:var(--muted-foreground);font-size:11px;margin-top:10px">Прошлые результаты не гарантируют будущих. Не инвестиционная рекомендация.</p>
</div>""" % {"ts": snap["ts"], "rows": rows_html, "curve": curve_svg(series, "кривая капитала, x — во сколько раз вырос депозит"),
            "stats": stats_html, "th": th}
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print("OK:", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
