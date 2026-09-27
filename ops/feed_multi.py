# -*- coding: utf-8 -*-
"""多币 K线 增量刷新（交易台刷新链路专用）。

与 ops/feed.py(BTC) 同范式：只增量追加「本地末日之后」的新交易日，绝不覆盖历史收盘价
（避免与回测同源闸门偏离）。代理感知（复用 feed.get_proxy / feed._opener），
故本机/境外服务器均可在无凭证情况下拉取（本机走 Clash，服务器直连）。

产物：data/kline_<BASE>.json（与 DataStore / phase7_wf 读取口径一致，
schema {day, close, volume}）。仅供 dual_gate_cta 等多币策略消费。

用法：
    python ops/feed_multi.py            # 增量刷新 universe_pit.json 全部币到最新
    python ops/feed_multi.py --dry      # 只打印哪些币滞后、需刷新，不拉取
"""
import os
import sys
import json
import time
import threading
import datetime as dt
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# 确保本脚本所在目录（ops/）在 sys.path 上，使 `import feed` 无论以何种方式调用都能解析
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feed  # 复用代理探测 + opener

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
UNIV = os.path.join(DATA, "universe_pit.json")

TIMEOUT = 10
SLEEP = 0.12
RETRY = 4
HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]


def _day_str(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def _get_klines(symbol, start_ms):
    last = None
    for h in HOSTS:
        try:
            url = (f"{h}/api/v3/klines?symbol={symbol}&interval=1d"
                   f"&limit=1000&startTime={start_ms}")
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (wolong desk multi)"})
            with feed._opener().open(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"all hosts failed: {last}")


def append_tail(base):
    """把 base 本地 K线追加到最新；返回 'fresh'/'missing'/'empty'/'fail'/int(新增天数)。"""
    path = os.path.join(DATA, f"kline_{base}.json")
    if not os.path.exists(path):
        return "missing"
    try:
        arr = json.load(open(path))
    except Exception:
        return "corrupt"
    if not arr:
        return "empty"
    last = arr[-1]["day"]
    # 仅当本地末日已是「今天」才视为新鲜跳过；昨天及更早都必须补拉（含今天这根 bar），
    # 否则 2 天容差会让「末日=昨天」的币永远跳过今天 → 多币行情滞后 → 候选池塌缩/NaN 强平。
    if last == dt.date.today().isoformat():
        return "fresh"
    start_ms = int(dt.datetime.strptime(last, "%Y-%m-%d")
                   .replace(tzinfo=dt.timezone.utc).timestamp() * 1000) + 86400000
    bars = None
    for attempt in range(RETRY):
        try:
            bars = _get_klines(f"{base}USDT", start_ms)
            break
        except Exception:  # noqa: BLE001
            time.sleep(SLEEP * (2 ** attempt))
    if not bars:
        return "fail"
    existing = {x["day"] for x in arr}
    added = 0
    for r in bars:
        d = _day_str(int(r[0]))
        if d > last and d not in existing:
            arr.append({"day": d, "close": float(r[4]), "volume": float(r[7])})
            existing.add(d)
            added += 1
    if added:
        arr.sort(key=lambda x: x["day"])
        json.dump(arr, open(path, "w"), ensure_ascii=False)
    return added


def _base_of(c):
    return c["symbol"][:-4] if c["symbol"].endswith("USDT") else c["symbol"]


def main():
    dry = "--dry" in sys.argv
    coins = json.load(open(UNIV))["coins"]
    if dry:
        for c in coins:
            base = _base_of(c)
            p = os.path.join(DATA, f"kline_{base}.json")
            if not os.path.exists(p):
                continue
            try:
                arr = json.load(open(p))
            except Exception:
                continue
            if not arr:
                continue
            last = arr[-1]["day"]
            lag = (dt.date.today() - dt.date.fromisoformat(last)).days
            if lag > 2:
                print(f"[multi] {base:8s} 滞后 {lag}d (末日 {last}) 需刷新")
        print("[multi] dry-run 完成")
        return
    # 并行抓取：每只币写独立 kline_<BASE>.json 文件，无共享写竞争；
    # 430 次串行 HTTP（经代理+重试退避）是刷新「转很久」的主因，并行可将健康场景从分钟级压到十几秒，
    # 同时降低撞上 serve.py 600s 硬超时、导致多币行情半截被掐死的概率。
    workers = int(os.environ.get("MULTI_WORKERS", "12"))
    bases = [_base_of(c) for c in coins]
    lock = threading.Lock()
    st = {"ok": 0, "skip": 0, "fail": 0, "done": 0}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(append_tail, b): b for b in bases}
        for fut in as_completed(futs):
            r = fut.result()
            with lock:
                if r in ("fresh", "missing", "empty"):
                    st["skip"] += 1
                elif isinstance(r, int) and r >= 0:
                    st["ok"] += 1
                else:
                    st["fail"] += 1
                st["done"] += 1
                if st["done"] % 25 == 0:
                    print(f"[multi] {st['done']}/{len(bases)} ok={st['ok']} skip={st['skip']} fail={st['fail']}")
    print(f"[multi] 完成 ok={st['ok']} skip={st['skip']} fail={st['fail']} / {len(bases)}")


if __name__ == "__main__":
    main()
