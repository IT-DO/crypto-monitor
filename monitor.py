#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мониторинг криптовалют по публичным API (без ключей).

Режимы:
  python monitor.py               # один снимок + проверка алертов
  python monitor.py --loop 60     # каждые 60 секунд
  python monitor.py --json        # машинный вывод

Конфиг: config.json (рядом со скриптом). История: data/snapshots.jsonl
Алерты печатаются строкой ALERT и (если задан webhook_url) уходят в вебхук.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; crypto-monitor/1.0)"
TIMEOUT = 12


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- источники: возвращают {symbol_norm: {"price": float, "chg24": float|None, "bid": float|None, "ask": float|None}} ----------

def fetch_binance(symbols: list[str]) -> dict:
    out = {}
    for s in symbols:
        d = http_json("https://api.binance.com/api/v3/ticker/24hr?symbol=" + urllib.parse.quote(s))
        last = float(d["lastPrice"])
        out[s] = {"price": last, "bid": float(d["bidPrice"]), "ask": float(d["askPrice"]),
                  "chg24": float(d["priceChangePercent"]), "vol24": float(d["quoteVolume"])}
    return out


def fetch_bybit(symbols: list[str]) -> dict:
    d = http_json("https://api.bybit.com/v5/market/tickers?category=spot")
    idx = {i["symbol"]: i for i in d["result"]["list"]}
    out = {}
    for s in symbols:
        i = idx.get(s)
        if not i:
            continue
        out[s] = {"price": float(i["lastPrice"]), "bid": float(i["bid1Price"]),
                  "ask": float(i["ask1Price"]), "chg24": float(i["price24hPcnt"]) * 100,
                  "vol24": float(i["turnover24h"])}
    return out


def fetch_okx(symbols: list[str]) -> dict:
    d = http_json("https://www.okx.com/api/v5/market/tickers?instType=SPOT")
    idx = {i["instId"]: i for i in d["data"]}
    out = {}
    for s in symbols:
        inst = {"BTCUSDT": "BTC-USDT", "ETHUSDT": "ETH-USDT",
                "SOLUSDT": "SOL-USDT"}.get(s, s.replace("USDT", "-USDT"))
        i = idx.get(inst)
        if not i:
            continue
        last = float(i["last"])
        out[s] = {"price": last, "bid": float(i["bidPx"] or 0) or None,
                  "ask": float(i["askPx"] or 0) or None, "chg24": float(i["open24h"]) and
                  (last / float(i["open24h"]) - 1) * 100, "vol24": float(i["volCcy24h"])}
    return out


def fetch_kraken(symbols: list[str]) -> dict:
    pairs = {"BTCUSDT": "XBTUSD", "ETHUSDT": "ETHUSD", "SOLUSDT": "SOLUSD"}
    out = {}
    for s, pair in pairs.items():
        if s not in symbols:
            continue
        d = http_json("https://api.kraken.com/0/public/Ticker?pair=" + pair)
        key = next(k for k in d["result"])
        i = d["result"][key]
        last = float(i["c"][0])
        out[s] = {"price": last, "bid": float(i["b"][0]), "ask": float(i["a"][0]),
                  "chg24": (last / float(i["o"]) - 1) * 100, "vol24": None}
    return out


def fetch_coinbase(symbols: list[str]) -> dict:
    mapping = {"BTCUSDT": "BTC-USD", "ETHUSDT": "ETH-USD", "SOLUSDT": "SOL-USD"}
    out = {}
    for s, pair in mapping.items():
        if s not in symbols:
            continue
        d = http_json("https://api.coinbase.com/v2/prices/%s/spot" % pair)
        out[s] = {"price": float(d["data"]["amount"]), "bid": None, "ask": None,
                  "chg24": None, "vol24": None}
    return out


SOURCES = {"binance": fetch_binance, "bybit": fetch_bybit, "okx": fetch_okx,
           "kraken": fetch_kraken, "coinbase": fetch_coinbase}


# ---------- снимок ----------

def snapshot(cfg: dict) -> dict:
    symbols = cfg.get("symbols", ["BTCUSDT"])
    enabled = [s for s in cfg.get("sources", list(SOURCES)) if s in SOURCES]
    data: dict[str, dict[str, dict]] = {}
    errors = {}
    for name in enabled:
        try:
            data[name] = SOURCES[name](symbols)
        except Exception as e:  # noqa: BLE001
            errors[name] = "%s: %s" % (type(e).__name__, e)

    rows = []
    for sym in symbols:
        quotes = {src: data[src][sym] for src in data if sym in data[src]}
        if not quotes:
            continue
        prices = [q["price"] for q in quotes.values()]
        mid = statistics.median(prices)
        # edge > 0 => на одной бирже можно купить по ask дешевле, чем продать по bid на другой
        edge_pct = None
        best_bid = max((q["bid"] for q in quotes.values() if q.get("bid")), default=None)
        best_ask = min((q["ask"] for q in quotes.values() if q.get("ask")), default=None)
        if best_bid and best_ask and mid:
            edge_pct = (best_bid - best_ask) / mid * 100
        chgs = [q["chg24"] for q in quotes.values() if q.get("chg24") is not None]
        rows.append({
            "symbol": sym,
            "price": round(mid, 6),
            "chg24_pct": round(statistics.median(chgs), 3) if chgs else None,
            "dispersion_pct": round((max(prices) - min(prices)) / mid * 100, 3) if mid else None,
            "arb_edge_pct": round(edge_pct, 4) if edge_pct is not None else None,
            "best_bid": best_bid, "best_ask": best_ask,
            "sources": {k: round(v["price"], 6) for k, v in quotes.items()},
            "min": round(min(prices), 6), "max": round(max(prices), 6),
        })
    return {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "rows": rows, "errors": errors}


def check_alerts(cfg: dict, snap: dict) -> list[dict]:
    """[{"key": ключ правила (для дедупликации), "text": текст}] — цена в ключ НЕ входит."""
    alerts = []
    by_sym = {r["symbol"]: r for r in snap["rows"]}
    for rule in cfg.get("alerts", []):
        sym = rule.get("symbol")
        r = by_sym.get(sym)
        if not r:
            continue
        p, chg = r["price"], r["chg24_pct"]
        if "above" in rule and p >= rule["above"]:
            alerts.append({"key": "%s|above|%s" % (sym, rule["above"]),
                           "text": "%s %.2f — выше порога %.2f" % (sym, p, rule["above"])})
        if "below" in rule and p <= rule["below"]:
            alerts.append({"key": "%s|below|%s" % (sym, rule["below"]),
                           "text": "%s %.2f — ниже порога %.2f" % (sym, p, rule["below"])})
        if "change_abs_pct" in rule and chg is not None and abs(chg) >= rule["change_abs_pct"]:
            alerts.append({"key": "%s|chg|%s" % (sym, rule["change_abs_pct"]),
                           "text": "%s изменение за 24ч %.2f%% (порог ±%.2f%%)"
                                   % (sym, chg, rule["change_abs_pct"])})
        if "dispersion_pct" in rule and r["dispersion_pct"] is not None \
                and r["dispersion_pct"] >= rule["dispersion_pct"]:
            alerts.append({"key": "%s|disp|%s" % (sym, rule["dispersion_pct"]),
                           "text": "%s расхождение цен между биржами %.3f%% (порог %.3f%%)"
                                   % (sym, r["dispersion_pct"], rule["dispersion_pct"])})
    return alerts


def send_webhook(url: str, text: str) -> None:
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except Exception as e:  # noqa: BLE001
        print("webhook error:", e, file=sys.stderr)


def load_env(path: str) -> None:
    """Мини-загрузчик .env: KEY=VALUE, значения не перетирают уже заданные в системе."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send_telegram(text: str, cfg: dict) -> bool:
    tg = cfg.get("telegram") or {}
    if not tg.get("enabled"):
        return False
    token = os.environ.get(tg.get("token_env", "TELEGRAM_BOT_TOKEN"))
    chat = os.environ.get(tg.get("chat_env", "TELEGRAM_CHAT_ID"))
    if not token or not chat:
        print("telegram: не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (.env)", file=sys.stderr)
        return False
    body = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                  "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=body)
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read().decode())
        return bool(r.get("ok"))
    except Exception as e:  # noqa: BLE001
        print("telegram error: %s" % e, file=sys.stderr)
        return False


def filter_alerts(alerts: list[dict], cooldown_min: int, state_path: str) -> list[dict]:
    """Не повторять один и тот же алерт чаще, чем раз в cooldown_min минут."""
    now = time.time()
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:  # noqa: BLE001
        state = {}
    fresh = []
    for a in alerts:
        if now - state.get(a["key"], 0) >= cooldown_min * 60:
            fresh.append(a)
            state[a["key"]] = now
    state = {k: v for k, v in state.items() if now - v < 7 * 86400}
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    return fresh


def render(snap: dict) -> str:
    out = ["Снимок %s" % snap["ts"], ""]
    head = "%-9s %12s %9s %10s %10s" % ("Пара", "Цена", "24ч %", "Арбитраж %", "Разброс %")
    out += [head, "-" * len(head)]
    for r in snap["rows"]:
        chg = "n/a" if r["chg24_pct"] is None else "%+.2f" % r["chg24_pct"]
        spr = "n/a" if r["arb_edge_pct"] is None else "%+.3f" % r["arb_edge_pct"]
        dis = "n/a" if r["dispersion_pct"] is None else "%.3f" % r["dispersion_pct"]
        out.append("%-9s %12.2f %9s %10s %10s  [%s]"
                   % (r["symbol"], r["price"], chg, spr, dis,
                      " ".join("%s=%.2f" % (k, v) for k, v in r["sources"].items())))
    if snap["errors"]:
        out += ["", "Ошибки источников: " + "; ".join("%s(%s)" % (k, v) for k, v in snap["errors"].items())]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="интервал в секундах (0 = один снимок)")
    ap.add_argument("--json", action="store_true", help="только JSON")
    ap.add_argument("--no-history", action="store_true", help="не писать в data/snapshots.jsonl")
    ap.add_argument("--quiet", action="store_true", help="печатать только алерты (для cron)")
    ap.add_argument("--config", default="config.json", help="путь к конфигу")
    ap.add_argument("--repeat-after", type=int, default=0,
                    help="минут до повтора того же алерта (0 = из config.json)")
    args = ap.parse_args()

    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(BASE, args.config)
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    hist_dir = os.path.join(BASE, "data")
    os.makedirs(hist_dir, exist_ok=True)
    hist = os.path.join(hist_dir, "snapshots.jsonl")
    state_path = os.path.join(hist_dir, "alert_state.json")
    load_env(os.path.join(BASE, ".env"))
    cooldown = args.repeat_after or cfg.get("alert_cooldown_min", 60)

    while True:
        snap = snapshot(cfg)
        alerts = check_alerts(cfg, snap)
        fresh = filter_alerts(alerts, cooldown, state_path)
        snap["alerts"] = [a["text"] for a in alerts]
        if not args.no_history:
            with open(hist, "a", encoding="utf-8") as f:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            if not args.quiet:
                print(render(snap))
            for a in fresh:
                print("ALERT:", a["text"])
            if args.quiet and not fresh and snap["errors"]:
                print("WARN: источники недоступны: " + "; ".join(snap["errors"]))
        if fresh and cfg.get("webhook_url"):
            send_webhook(cfg["webhook_url"], "\n".join(a["text"] for a in fresh))
        if fresh:
            send_telegram("Крипто-алерт\n" + "\n".join(a["text"] for a in fresh)
                          + "\n" + snap["ts"], cfg)
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
