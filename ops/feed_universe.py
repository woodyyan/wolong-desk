# -*- coding: utf-8 -*-
"""多资产宇宙 K线 全量回填（Phase 0 数据基建）。

只走 Binance klines（全历史最完整），冷却+分段+重试（对齐 data-fetch-risk 范式）。
零新依赖（标准库）。产物 data/kline_<BASE>.json，schema {day, close, volume(quote≈USD)}。
BTC 也走这里（落 kline_BTC.json），与 desk 现有 kline_BTCUSD.json 互不干扰、互不复用。

用法：
    python ops/feed_universe.py                 # 回填 universe_candidates.json 全部
    python ops/feed_universe.py --symbol=SOLUSDT # 只回填一个（重试/补拉）
    python ops/feed_universe.py --force          # 忽略已有、强制重拉
"""
import os
import sys
import json
import time
import datetime as dt
import urllib.request
import urllib.error
import feed  # 复用代理探测 + opener（本机走 Clash，服务器直连）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
UNIV = os.path.join(DATA, "universe_candidates.json")

TIMEOUT = 12
SLEEP = 0.25          # 调用间冷却（Binance keyless 1200权重/分，留足余量）
RETRY = 6             # 失败重试次数（指数退避）


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (wolong desk)"})
    with feed._opener().open(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _day_str(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def fetch_symbol(symbol, max_pages=400):
    """分页拉全量日线，返回 {day: (close, quote_vol)}。无前视、纯历史。"""
    out = {}
    start = 0
    pages = 0
    while pages < max_pages:
        url = (f"https://api.binance.com/api/v3/klines"
               f"?symbol={symbol}&interval=1d&limit=1000&startTime={start}")
        rows = _get_json(url)
        if not rows:
            break
        for r in rows:
            d = _day_str(int(r[0]))
            out[d] = (float(r[4]), float(r[7]))   # close, quote volume(≈USD)
        pages += 1
        if len(rows) < 1000:
            break
        start = int(rows[-1][0]) + 86400000       # 下一日 00:00 UTC，无重叠
        time.sleep(SLEEP)
    return out


def backfill_symbol(symbol, force=False):
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    path = os.path.join(DATA, f"kline_{base}.json")
    # 断点续拉：已存在且最新则跳过（除非 --force）
    if os.path.exists(path) and not force:
        try:
            k = json.load(open(path))
            if k and (dt.date.today() - dt.date.fromisoformat(k[-1]["day"])).days <= 2:
                print(f"[univ] {symbol} 已最新({k[-1]['day']}) 跳过")
                return True
        except Exception:
            pass
    last_err = None
    for attempt in range(RETRY):
        try:
            bars = fetch_symbol(symbol)
            if not bars:
                last_err = "空返回"
                time.sleep(SLEEP * 2)
                continue
            arr = [{"day": d, "close": c, "volume": v}
                   for d, (c, v) in sorted(bars.items())]
            json.dump(arr, open(path, "w"), ensure_ascii=False)
            print(f"[univ] {symbol:10s} 回填 {len(arr):4d} 根 "
                  f"({arr[0]['day']}→{arr[-1]['day']})")
            return True
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(SLEEP * (2 ** attempt))    # 指数退避
    print(f"[univ] {symbol:10s} 失败: {last_err}")
    return False


def main():
    force = "--force" in sys.argv
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--symbol="):
            only = a.split("=", 1)[1]
    coins = json.load(open(UNIV))["coins"]
    targets = [c["symbol"] for c in coins] if not only else [only]
    ok = 0
    for s in targets:
        if backfill_symbol(s, force=force):
            ok += 1
    print(f"[univ] 完成 {ok}/{len(targets)}")


if __name__ == "__main__":
    main()
