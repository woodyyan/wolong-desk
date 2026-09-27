# -*- coding: utf-8 -*-
"""PIT 宇宙 K线全量回填：按 data/universe_pit.json（Binance 上市 USDT）回填全历史日线。
复用 feed_universe 的分页/冷却/重试范式；多 host 兜底。产物 data/kline_<BASE>.json，断点续拉。
"""
import os, sys, json, time, datetime as dt, urllib.request
import feed  # 复用代理探测 + opener（本机走 Clash，服务器直连）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
UNIV = os.path.join(DATA, "universe_pit.json")

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
TIMEOUT = 12
SLEEP = 0.25
RETRY = 6


def _get_json(url):
    last = None
    for h in HOSTS:
        try:
            req = urllib.request.Request(h + url, headers={"User-Agent": "Mozilla/5.0 (wolong pit)"})
            with feed._opener().open(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"all hosts failed: {last}")


def _day_str(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def fetch_symbol(symbol, max_pages=400):
    out = {}
    start = 0
    pages = 0
    while pages < max_pages:
        rows = _get_json(f"/api/v3/klines?symbol={symbol}&interval=1d&limit=1000&startTime={start}")
        if not rows:
            break
        for r in rows:
            d = _day_str(int(r[0]))
            out[d] = (float(r[4]), float(r[7]))
        pages += 1
        if len(rows) < 1000:
            break
        start = int(rows[-1][0]) + 86400000
        time.sleep(SLEEP)
    return out


def backfill_symbol(symbol, force=False):
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    path = os.path.join(DATA, f"kline_{base}.json")
    if os.path.exists(path) and not force:
        try:
            k = json.load(open(path))
            if k and (dt.date.today() - dt.date.fromisoformat(k[-1]["day"])).days <= 2:
                return "skip"
        except Exception:
            pass
    last_err = None
    for attempt in range(RETRY):
        try:
            bars = fetch_symbol(symbol)
            if not bars:
                last_err = "空返回"; time.sleep(SLEEP * 2); continue
            arr = [{"day": d, "close": c, "volume": v} for d, (c, v) in sorted(bars.items())]
            json.dump(arr, open(path, "w"), ensure_ascii=False)
            return f"ok {len(arr)}"
        except Exception as e:
            last_err = e
            time.sleep(SLEEP * (2 ** attempt))
    return f"FAIL {last_err}"


def main():
    force = "--force" in sys.argv
    coins = json.load(open(UNIV))["coins"]
    ok = skip = fail = 0
    for i, c in enumerate(coins):
        r = backfill_symbol(c["symbol"], force=force)
        if r == "skip": skip += 1
        elif r.startswith("ok"): ok += 1
        else: fail += 1
        if (i + 1) % 25 == 0:
            print(f"[pit] {i+1}/{len(coins)} ok={ok} skip={skip} fail={fail}")
    print(f"[pit] 完成 ok={ok} skip={skip} fail={fail} / {len(coins)}")


if __name__ == "__main__":
    main()
