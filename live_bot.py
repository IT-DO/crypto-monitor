#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_bot.py — авто-контур: сигнал EMA 50/200 (4h) -> рыночный ордер на TESTNET Binance.

По умолчанию НЕ торгует: печатает, что сделал бы. Реальную заявку отправляет только
с флагом --live. Хост всегда testnet — боевой адрес в коде не прописан.

  python live_bot.py                 # симуляция: показывает решение по последней закрытой свече
  python live_bot.py --live          # исполнить решение на testnet
  python live_bot.py --live --quiet  # режим cron: молчит, если сделки не было
  python live_bot.py --status        # позиция, капитал, история
  python live_bot.py --live --force buy   # ТЕСТ: принудительная покупка мимо сигнала
  python live_bot.py --live --reset       # сбросить состояние бота

Состояние: data/live/state.json · Сделки: data/live/trades.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import order_executor as oe

BASE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(BASE, "data", "live")
STATE = os.path.join(LIVE, "state.json")
TRADES = os.path.join(LIVE, "trades.jsonl")
SYMBOL, INTERVAL, FAST, SLOW = "BTCUSDT", "4h", 50, 200


def candles(limit: int = 400) -> list[dict]:
    url = "https://api.binance.com/api/v3/klines?" + urllib.parse.urlencode(
        {"symbol": SYMBOL, "interval": INTERVAL, "limit": limit})
    req = urllib.request.Request(url, headers={"User-Agent": oe.UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = json.loads(r.read().decode())
    return [{"t": b[0], "c": float(b[4])} for b in raw[:-1]]      # последняя свеча не закрыта


def ema(vals: list[float], n: int) -> list[float]:
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
    return {"in_pos": False, "qty": 0.0, "entry_px": 0.0, "last_bar": 0,
            "trades": 0, "realized_usdt": 0.0, "alloc_usdt": 0.0}


def save_state(st: dict) -> None:
    os.makedirs(LIVE, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def log_trade(row: dict) -> None:
    os.makedirs(LIVE, exist_ok=True)
    row["ts_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(TRADES, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def market_order(side: str, qty: float, live: bool) -> dict:
    """Рыночная заявка с округлением по шагу лота пары."""
    flt = oe.filters(SYMBOL)
    q, q_s = oe.round_qty(qty, flt["step"])
    if q <= 0:
        raise SystemExit("Стоп: количество после округления по stepSize=%s равно нулю" % flt["step"])
    px = oe.price_of(SYMBOL)
    notional = q * px
    mn = float(flt.get("min_notional") or 0)
    if mn and notional < mn:
        raise SystemExit("Стоп: %.2f USDT меньше MIN_NOTIONAL %.2f" % (notional, mn))
    params = {"symbol": SYMBOL, "side": side, "type": "MARKET", "quantity": q_s,
              "newClientOrderId": "hermes-live-%d" % int(time.time())}
    resp = oe.signed_request("POST", "/api/v3/order", params, live)
    # единый журнал исполненных заявок: по нему order_executor --flatten считает позицию
    oe.log({"symbol": SYMBOL, "side": side, "qty": q, "price_ref": px,
            "notional": round(notional, 2), "live": live, "source": "live_bot",
            "response": resp})
    return {"qty": q, "qty_s": q_s, "price": px, "notional": notional, "resp": resp, "live": live}


def status(st: dict, price: float, want_long: bool) -> None:
    eq = st["realized_usdt"]
    if st["in_pos"]:
        eq += st["qty"] * price - st["alloc_usdt"]
    print("Авто-бот testnet (%s %s, EMA %d/%d)" % (SYMBOL, INTERVAL, FAST, SLOW))
    print("  цена            %10.2f" % price)
    print("  сигнал          %s" % ("ЛОНГ" if want_long else "ВНЕ РЫНКА"))
    print("  позиция         %s" % ("%.8f @ %.2f" % (st["qty"], st["entry_px"]) if st["in_pos"] else "нет"))
    print("  реализовано     %+10.2f USDT" % st["realized_usdt"])
    print("  сделок          %10d" % st["trades"])
    print("  последняя свеча %s" % (datetime.fromtimestamp(st["last_bar"] / 1000, timezone.utc)
                                   if st["last_bar"] else "-"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="отправить заявку на testnet")
    ap.add_argument("--quiet", action="store_true", help="молчать, если сделки не было (cron)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--force", choices=["buy", "sell"], help="принудительная сделка для проверки контура")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()

    if a.reset:
        held = oe.tracked_position(SYMBOL)
        if held > 0:
            raise SystemExit("Стоп: на testnet открыта позиция %.8f %s. Сначала закрой её:\n"
                             "  python order_executor.py --live --flatten\n"
                             "потом сбрасывай состояние — иначе позиция останется без учёта."
                             % (held, SYMBOL.replace("USDT", "")))
        for f in (STATE, TRADES):
            if os.path.exists(f):
                os.remove(f)
        save_state(load_state())
        print("Состояние бота сброшено.")
        return 0

    bars = candles()
    px = bars[-1]["c"]
    want_long = ema([b["c"] for b in bars], FAST)[-1] > ema([b["c"] for b in bars], SLOW)[-1]
    st = load_state()

    if a.status:
        status(st, px, want_long)
        return 0

    rc = oe.risk_cfg()
    alloc = float(rc.get("alloc_usdt", 20))
    max_notional = float(rc.get("max_notional_usdt", 50))
    if alloc > max_notional:
        raise SystemExit("Стоп: alloc_usdt=%.2f больше max_notional_usdt=%.2f" % (alloc, max_notional))

    if a.force:
        want_side = a.force
    else:
        if st["last_bar"] == bars[-1]["t"]:
            if not a.quiet:
                print("Свеча %s уже обработана — пропуск (идемпотентно)."
                      % datetime.fromtimestamp(bars[-1]["t"] / 1000, timezone.utc))
            return 0
        want_side = "buy" if (want_long and not st["in_pos"]) else \
                    "sell" if (not want_long and st["in_pos"]) else None

    acted = None
    if want_side == "buy":
        res = market_order("BUY", alloc / px, a.live)
        fill_qty = float(res["resp"].get("executedQty") or res["qty"]) if a.live else res["qty"]
        spent = float(res["resp"].get("cummulativeQuoteQty") or 0) if a.live else res["notional"]
        st.update({"in_pos": True, "qty": fill_qty, "entry_px": px, "alloc_usdt": spent,
                   "trades": st["trades"] + 1})
        acted = ("КУПИЛ %s BTC по ~%.2f (%.2f USDT)%s"
                 % (res["qty_s"], px, spent or res["notional"],
                    "" if a.live else " [СИМУЛЯЦИЯ, ордер не отправлен]"))
        log_trade({"side": "buy", "qty": res["qty"], "price": px,
                   "notional": spent or res["notional"], "live": a.live,
                   "order_id": (res["resp"] or {}).get("orderId")})
    elif want_side == "sell":
        qty = st["qty"] if st["qty"] > 0 else oe.tracked_position(SYMBOL)
        if qty <= 0:
            print("Нечего продавать: позиция пустая.")
            return 0
        res = market_order("SELL", qty, a.live)
        received = float(res["resp"].get("cummulativeQuoteQty") or 0) if a.live else res["qty"] * px
        pnl = (received - st["alloc_usdt"]) if a.live else (qty * px - st["alloc_usdt"])
        st.update({"in_pos": False, "qty": 0.0, "entry_px": 0.0,
                   "realized_usdt": st["realized_usdt"] + pnl,
                   "trades": st["trades"] + 1})
        acted = ("ПРОДАЛ %s BTC по ~%.2f, PnL %+.2f USDT%s"
                 % (res["qty_s"], px, pnl, "" if a.live else " [СИМУЛЯЦИЯ]"))
        log_trade({"side": "sell", "qty": res["qty"], "price": px,
                   "pnl_usdt": round(pnl, 4), "live": a.live,
                   "order_id": (res["resp"] or {}).get("orderId")})

    if not a.force:
        st["last_bar"] = bars[-1]["t"]
    save_state(st)

    if acted:
        print("BOT: " + acted)
        print("BOT: сделок %d, реализовано %+.2f USDT" % (st["trades"], st["realized_usdt"]))
    elif not a.quiet:
        status(st, px, want_long)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
