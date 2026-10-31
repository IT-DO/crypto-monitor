#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paper_trader.py — торговля ВИРТУАЛЬНЫМ депозитом по сигналам EMA 50/200 (4h).
Реальные ордера не отправляются, API-ключи не нужны: цены берутся из публичного API.

  python paper_trader.py --init 1000   # обнулить счёт (виртуальные 1000 USDT)
  python paper_trader.py               # один тик: проверить последнюю ЗАКРЫТУЮ свечу
  python paper_trader.py --status      # состояние и статистика
  python paper_trader.py --quiet       # тик без вывода, если сделки не было (для cron)

Сделка исполняется по close закрытой свечи + комиссия 0.10% + слиппедж 0.02%.
Один тик идемпотентен: повторный запуск на той же свече ничего не делает.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
PAPER = os.path.join(BASE, "data", "paper")
STATE = os.path.join(PAPER, "state.json")
TRADES = os.path.join(PAPER, "trades.jsonl")
EQUITY = os.path.join(PAPER, "equity.jsonl")
UA = "Mozilla/5.0 (compatible; paper-trader/1.0)"
FEE, SLIP = 0.0010, 0.0002
SYMBOL, INTERVAL, FAST, SLOW = "BTCUSDT", "4h", 50, 200


def candles(limit: int = 400) -> list[dict]:
    url = ("https://api.binance.com/api/v3/klines?" +
           urllib.parse.urlencode({"symbol": SYMBOL, "interval": INTERVAL, "limit": limit}))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.loads(r.read().decode())
    # последняя свеча ещё не закрыта -> отбрасываем её
    return [{"t": b[0], "c": float(b[4]), "h": float(b[2]), "l": float(b[3])} for b in raw[:-1]]


def ema_series(vals: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def load_state() -> dict:
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"cash": 1000.0, "qty": 0.0, "entry": None, "entry_ts": None,
            "last_bar": 0, "start_equity": 1000.0, "start_price": None, "trades": 0}


def save_state(st: dict) -> None:
    os.makedirs(PAPER, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def log(path: str, row: dict) -> None:
    os.makedirs(PAPER, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def tick(quiet: bool) -> int:
    bars = candles()
    closes = [b["c"] for b in bars]
    f, s = ema_series(closes, FAST), ema_series(closes, SLOW)
    last = bars[-1]
    price = last["c"]
    want_long = f[-1] > s[-1]
    st = load_state()
    if st["start_price"] is None:
        st["start_price"] = price
        st["start_equity"] = st["cash"] if st["qty"] == 0 else st["cash"] + st["qty"] * price
    ts = datetime.fromtimestamp(last["t"] / 1000, timezone.utc)

    if st["last_bar"] == last["t"]:
        if not quiet:
            print("Свеча %s уже обработана — пропуск (идемпотентно)." % ts.isoformat())
        return 0

    equity = st["cash"] + st["qty"] * price
    acted = ""
    if want_long and st["qty"] == 0:
        px = price * (1 + SLIP)
        qty = (st["cash"] / (1 + FEE)) / px
        if qty > 0:
            st["cash"] -= qty * px * (1 + FEE)
            st["qty"], st["entry"], st["entry_ts"] = qty, price, last["t"]
            st["trades"] += 1
            acted = "BUY %s по %.2f (%s)" % (SYMBOL, price, ts.date())
            log(TRADES, {"ts": last["t"], "side": "buy", "price": price, "qty": round(qty, 8),
                         "equity": round(equity, 2)})
    elif not want_long and st["qty"] > 0:
        px = price * (1 - SLIP)
        proceeds = st["qty"] * px * (1 - FEE)
        pnl = proceeds - st["qty"] * (st["entry"] or px)
        st["cash"] = proceeds
        acted = "SELL %s по %.2f (%s), PnL %+.2f USDT" % (SYMBOL, price, ts.date(), pnl)
        log(TRADES, {"ts": last["t"], "side": "sell", "price": price,
                     "qty": round(st["qty"], 8), "pnl": round(pnl, 2),
                     "equity": round(proceeds, 2)})
        st["qty"], st["entry"], st["entry_ts"] = 0.0, None, None
        st["last_pnl"] = round(pnl, 2)

    st["last_bar"] = last["t"]
    save_state(st)
    equity = st["cash"] + st["qty"] * price
    log(EQUITY, {"ts": last["t"], "price": price, "equity": round(equity, 2),
                 "in_pos": 1 if st["qty"] else 0, "signal": 1 if want_long else 0})

    if acted:
        print("PAPER: " + acted)
        print("PAPER: капитал %.2f USDT, сделок %d" % (equity, st["trades"]))
    elif not quiet:
        status(st, price, want_long, eq=equity)
    return 0


def status(st: dict, price: float, want_long: bool, eq: float | None = None) -> None:
    eq = eq if eq is not None else st["cash"] + st["qty"] * price
    ret = (eq / st["start_equity"] - 1) * 100 if st["start_equity"] else 0
    bh = (price / st["start_price"] - 1) * 100 if st.get("start_price") else 0
    peak = eq
    mdd = 0.0
    if os.path.exists(EQUITY):
        curve = [json.loads(l)["equity"] for l in open(EQUITY, encoding="utf-8") if l.strip()]
        if curve:
            peak = max(curve)
            for v in curve:
                peak = max(peak, v)
                mdd = min(mdd, v / peak - 1)
    print("Состояние paper-счёта (%s %s, EMA %d/%d)" % (SYMBOL, INTERVAL, FAST, SLOW))
    print("  цена           %10.2f USDT" % price)
    print("  сигнал         %s" % ("ЛОНГ" if want_long else "ВНЕ РЫНКА (кэш)"))
    print("  позиция        %s" % ("%.8f @ %.2f" % (st["qty"], st["entry"]) if st["qty"] else "нет"))
    print("  капитал        %10.2f USDT" % eq)
    print("  доход          %+9.2f%%" % ret)
    print("  buy & hold     %+9.2f%%" % bh)
    print("  макс. просадка %9.2f%%" % (mdd * 100))
    print("  сделок         %10d" % st["trades"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", type=float, default=0, help="сбросить счёт на N USDT")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.init:
        os.makedirs(PAPER, exist_ok=True)
        for f in (TRADES, EQUITY):
            if os.path.exists(f):
                os.remove(f)
        save_state({"cash": a.init, "qty": 0.0, "entry": None, "entry_ts": None,
                    "last_bar": 0, "start_equity": a.init, "start_price": None, "trades": 0})
        print("Paper-счёт инициализирован: %.2f USDT" % a.init)
        return 0

    if a.status:
        bars = candles()
        closes = [b["c"] for b in bars]
        want = ema_series(closes, FAST)[-1] > ema_series(closes, SLOW)[-1]
        status(load_state(), closes[-1], want)
        return 0
    return tick(a.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
