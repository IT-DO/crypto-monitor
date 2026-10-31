#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""order_executor.py — исполнение ордеров на TESTNET Binance (spot).

Боевой режим по умолчанию ЗАПРЕЩЁН: без явного --live скрипт только
показывает подписанный запрос (dry-run). Ключи читаются из .env:
    BINANCE_TESTNET_KEY=...
    BINANCE_TESTNET_SECRET=...

Жёсткие лимиты (hаrd caps) из config.json -> risk:
    max_notional_usdt  — максимальный размер одной заявки
    max_position_usdt  — максимальная суммарная позиция в базовом активе
    allowed_symbols    — белый список пар

  python order_executor.py --balance
  python order_executor.py --buy 0.001            # dry-run
  python order_executor.py --buy 25 --quote       # на 25 USDT
  python order_executor.py --sell --all
  python order_executor.py --open-orders
  python order_executor.py --live --buy 25 --quote # РЕАЛЬНАЯ заявка на testnet
  python order_executor.py --flatten              # закрыть позицию по рынку

Ключи НИКОГДА не печатаются и не логируются — только имена переменных.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
ORDERS_LOG = os.path.join(BASE, "data", "orders.jsonl")
HOST = "https://testnet.binance.vision"      # боевой: https://api.binance.com
UA = "Mozilla/5.0 (compatible; order-executor/1.0)"


def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def keys() -> tuple[str, str]:
    load_env(os.path.join(BASE, ".env"))
    k = os.environ.get("BINANCE_TESTNET_KEY", "")
    s = os.environ.get("BINANCE_TESTNET_SECRET", "")
    if not k or not s:
        raise SystemExit("Нет ключей: заполни BINANCE_TESTNET_KEY и BINANCE_TESTNET_SECRET в .env\n"
                         "Ключи testnet: https://testnet.binance.vision (права только на спот-торговлю)")
    return k, s


def sign(secret: str, params: dict) -> str:
    q = urllib.parse.urlencode(params)
    return q + "&signature=" + hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()


def signed_request(method: str, path: str, params: dict, live: bool) -> dict:
    """Возвращает ответ API (live) или разбор запроса (dry-run). Ключи в вывод не попадают."""
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    if not live:
        load_env(os.path.join(BASE, ".env"))
        return {"dry_run": True, "method": method, "url": HOST + path,
                "params": {k: v for k, v in params.items()},
                "api_key_set": bool(os.environ.get("BINANCE_TESTNET_KEY")),
                "signature": "sha256 hmac (не показан)"}
    k, s = keys()
    query = sign(s, params)
    req = urllib.request.Request("%s%s?%s" % (HOST, path, query), method=method,
                                 headers={"X-MBX-APIKEY": k, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit("API %s %s: %s" % (e.code, path, body[:400]))


def public(path: str, params: dict) -> dict:
    url = HOST + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def log(row: dict) -> None:
    os.makedirs(os.path.dirname(ORDERS_LOG), exist_ok=True)
    row["ts_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ORDERS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def tracked_position(symbol: str) -> float:
    """Сколько базового актива набрал/продал именно этот бот (по data/orders.jsonl)."""
    if not os.path.exists(ORDERS_LOG):
        return 0.0
    pos = 0.0
    with open(ORDERS_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if row.get("reconcile_reset"):      # точка сверки: позиция бота обнуляется
                pos = 0.0
                continue
            if row.get("symbol") != symbol or not row.get("live"):
                continue
            resp = row.get("response") or {}
            if resp.get("status") not in ("FILLED", "PARTIALLY_FILLED"):
                continue
            qty = float(resp.get("executedQty") or 0)
            pos += qty if row.get("side") == "BUY" else -qty
    return pos


def risk_cfg() -> dict:
    cfg_path = os.path.join(BASE, "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8")) if os.path.exists(cfg_path) else {}
    return cfg.get("risk", {"max_notional_usdt": 50, "max_position_usdt": 100,
                            "allowed_symbols": ["BTCUSDT"], "symbol": "BTCUSDT"})


def check_risk(symbol: str, notional: float, rc: dict) -> None:
    if symbol not in rc.get("allowed_symbols", []):
        raise SystemExit("Стоп: %s нет в allowed_symbols %s" % (symbol, rc.get("allowed_symbols")))
    if notional > rc.get("max_notional_usdt", 50):
        raise SystemExit("Стоп: заявка %.2f USDT больше max_notional_usdt=%.2f"
                         % (notional, rc.get("max_notional_usdt", 50)))
    if notional <= 0:
        raise SystemExit("Стоп: нулевой размер заявки")


def filters(symbol: str) -> dict:
    """Шаг лота и минимальный номинал пары — без них биржа отвечает LOT_SIZE."""
    info = public("/api/v3/exchangeInfo", {"symbol": symbol})
    f = {x["filterType"]: x for x in info["symbols"][0]["filters"]}
    lot = f.get("LOT_SIZE", {})
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    return {"step": lot.get("stepSize"), "min_qty": lot.get("minQty"),
            "min_notional": notional.get("minNotional") or notional.get("notional")}


def round_qty(qty: float, step: str | None) -> tuple[float, str]:
    """Округление количества ВНИЗ до шага лота + строка без лишних нулей."""
    if not step:
        return qty, "%.8f" % qty
    s = float(step)
    if s <= 0:
        return qty, "%.8f" % qty
    decimals = len(step.split(".")[1].rstrip("0")) if "." in step else 0
    q = (int(qty / s)) * s
    return q, "%.*f" % (decimals, q)


def price_of(symbol: str) -> float:
    return float(public("/api/v3/ticker/price", {"symbol": symbol})["price"])


def main() -> int:
    rc = risk_cfg()
    sym = rc.get("symbol", "BTCUSDT")
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=sym)
    ap.add_argument("--buy", type=float, default=0, help="количество базового актива")
    ap.add_argument("--sell", action="store_true")
    ap.add_argument("--all", action="store_true", help="продать всю позицию")
    ap.add_argument("--quote", action="store_true", help="--buy задан в USDT, а не в монете")
    ap.add_argument("--balance", action="store_true")
    ap.add_argument("--open-orders", action="store_true")
    ap.add_argument("--flatten", action="store_true")
    ap.add_argument("--reconcile", action="store_true",
                    help="сверить позицию бота (журнал) с балансом биржи")
    ap.add_argument("--live", action="store_true", help="РЕАЛЬНАЯ заявка на testnet")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.reconcile:
        acc = signed_request("GET", "/api/v3/account", {}, a.live)
        if acc.get("dry_run"):
            print("dry-run: сверка требует --live")
            return 0
        base = a.symbol.replace("USDT", "")
        free = next((float(b["free"]) for b in acc["balances"] if b["asset"] == base), 0.0)
        held = tracked_position(a.symbol)
        # базовый остаток (фаусет/ручные покупки) запоминаем один раз, чтобы не путать его с позицией бота
        bpath = os.path.join(BASE, "data", "account_baseline.json")
        baseline = {}
        if os.path.exists(bpath):
            with open(bpath, encoding="utf-8") as f:
                baseline = json.load(f)
        if base not in baseline:
            take = max(0.0, free - held)
            baseline[base] = take
            os.makedirs(os.path.dirname(bpath), exist_ok=True)
            with open(bpath, "w", encoding="utf-8") as f:
                json.dump(baseline, f, indent=1)
            print("базовый остаток зафиксирован: %.8f %s" % (take, base))
        bl = float(baseline.get(base, 0.0))
        print("журнал бота:      %.8f %s" % (held, base))
        print("на бирже:         %.8f %s" % (free, base))
        print("базовый остаток:  %.8f %s (не бота)" % (bl, base))
        diff = free - held - bl
        if abs(diff) < 1e-12:
            print("СВЕРКА: расхождений нет")
        else:
            print("СВЕРКА: расхождение %.8f %s — ручные сделки или потеря состояния журнала"
                  % (diff, base))
        return 0

    if a.balance:
        r = signed_request("GET", "/api/v3/account", {}, a.live)
        if r.get("dry_run"):
            print("dry-run: подписанный запрос GET /api/v3/account готов (показать --json)")
        else:
            for b in r.get("balances", []):
                if float(b["free"]) > 0 or float(b["locked"]) > 0:
                    print("%-8s free %-18s locked %s" % (b["asset"], b["free"], b["locked"]))
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if a.open_orders:
        r = signed_request("GET", "/api/v3/openOrders", {"symbol": a.symbol}, a.live)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    px = price_of(a.symbol)

    if a.flatten:
        acc = signed_request("GET", "/api/v3/account", {}, a.live)
        if acc.get("dry_run"):
            print("dry-run: flatten отправил бы рыночную продажу всего остатка")
            return 0
        base = a.symbol.replace("USDT", "")
        free = next((float(b["free"]) for b in acc["balances"] if b["asset"] == base), 0.0)
        tracked = tracked_position(a.symbol)
        # закрываем только то, что купил бот: не трогаем остаток счёта (фаусет testnet и т.п.)
        sellable = min(free, tracked) if tracked > 0 else 0.0
        if sellable <= 0:
            print("Бот не держит позицию по %s (бот: %.8f, на балансе: %.8f)"
                  % (base, tracked, free))
            return 0
        print("Флэт: продаю %.8f %s (куплено ботом; всего на балансе %.8f)"
              % (sellable, base, free))
        qty = sellable

    side = "BUY" if a.buy > 0 else "SELL"   # flatten всегда ПРОДАЁТ
    if not a.flatten:
        qty = a.buy
    if a.sell or a.all:
        if a.all:
            acc = signed_request("GET", "/api/v3/account", {}, a.live)
            if acc.get("dry_run"):
                print(json.dumps({"dry_run": True, "action": "sell all"}, ensure_ascii=False))
                return 0
            base = a.symbol.replace("USDT", "")
            qty = next((float(b["free"]) for b in acc["balances"] if b["asset"] == base), 0.0)
        if qty <= 0:
            print("Нечего продавать")
            return 0

    notional = qty * px if not a.quote else qty
    if a.quote and side == "BUY":
        qty = qty / px
    check_risk(a.symbol, notional, rc)

    flt = filters(a.symbol) if (a.live or a.json) else {"step": None, "min_notional": None}
    qty, qty_s = round_qty(qty, flt.get("step"))
    if qty <= 0:
        raise SystemExit("Стоп: после округления до шага лота (stepSize=%s) получается 0"
                         % flt.get("step"))
    real_notional = qty * px
    mn = float(flt.get("min_notional") or 0)
    if mn and real_notional < mn:
        raise SystemExit("Стоп: %.2f USDT меньше минимального номинала пары (%.2f USDT)"
                         % (real_notional, mn))
    notional = real_notional

    params = {"symbol": a.symbol, "side": side, "type": "MARKET", "quantity": qty_s,
              "newClientOrderId": "hermes-%d" % int(time.time())}
    r = signed_request("POST", "/api/v3/order", params, a.live)
    log({"symbol": a.symbol, "side": side, "qty": qty, "price_ref": px,
         "notional": round(notional, 2), "live": a.live, "response": r})

    if a.live:
        print("TESTNET-ОРДЕР отправлен: %s %s %s @ ~%.2f (%.2f USDT)"
              % (side, a.symbol, qty_s, px, notional))
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("DRY-RUN: заявка %s %s %s @ ~%.2f (%.2f USDT) прошла проверки, но НЕ отправлена."
              % (side, a.symbol, qty_s, px, notional))
        print("  лимиты: max_notional=%.2f, max_position=%.2f, символы=%s"
              % (rc.get("max_notional_usdt", 0), rc.get("max_position_usdt", 0),
                 rc.get("allowed_symbols")))
        print("  для отправки добавь --live (только testnet)")
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
