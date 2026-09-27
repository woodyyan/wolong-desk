# -*- coding: utf-8 -*-
"""L1 行情层 · 多源兜底拉 BTC/USDT 日线 + 健康探针。

多源兜底（D3）：Binance → OKX → CoinGecko。沙箱/断网时降级到本地缓存，
health.source 标 'local'，不报错，保证交易台任何环境可跑。

零新依赖：只用标准库（urllib / json / datetime）。
用法：
    python feed.py            # 探针探活 + 增量拉取 + 写 health.json
    python feed.py --once     # 同上（默认即一次）
    python feed.py --no-pull  # 只探活写 health，不拉行情
"""
import os
import re
import sys
import json
import time
import datetime as dt
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# ⚠️ 必须与引擎读取的 BTC 文件一致：strategy_hub / engine 的 DataStore 读的是
#    data/kline_BTC.json（3309 天全样本，含 volume）。此前此处误写成 kline_BTCUSD.json，
#    导致 feed 刷新写入的文件引擎从不读取 → BTC 行情永远停留在打包快照、刷新无效。
#    改回 kline_BTC.json 后，/api/refresh 才会真正推进 BTC 行情。
DATA = os.path.join(ROOT, "data", "kline_BTC.json")
STATE = os.path.join(HERE, "state")
HEALTH = os.path.join(STATE, "health.json")

SYMBOL = "BTCUSDT"
TIMEOUT = 4  # 秒（境外源常不可达，缩短避免拖慢面板请求）
SOURCES = ["binance", "okx", "coingecko"]


# ── 代理支持：境外加密 API 常需走代理。优先级 显式配置 > 环境变量 > 系统代理 ──
_PROXY_CACHE = {"v": None, "ts": 0.0}
CONFIG = os.path.join(HERE, "feed_config.json")
# 实时价失败冷却：连续失败时不再每个请求都重试（曾导致面板一次请求卡 22~45s）
_LIVE_FAIL = {"ts": 0.0, "cooldown": 0.0}
# 探测中标志：已有线程在打境外源时，后来的请求直接降级本地价，不排队干等 12s
_LIVE_BUSY = {"on": False}
LIVE_COOLDOWN_STEP = 60.0     # 每次失败后冷却 +60s
LIVE_COOLDOWN_MAX = 600.0     # 上限 10 分钟


def _load_cfg():
    if os.path.exists(CONFIG):
        try:
            return json.load(open(CONFIG))
        except Exception:
            pass
    return {}


def _detect_system_proxy():
    """从 macOS scutil --proxy 读系统代理（Clash Verge / Mihomo 等常驻 7897）。"""
    try:
        import subprocess
        out = subprocess.run(["scutil", "--proxy"], capture_output=True,
                             text=True, timeout=3).stdout or ""
        if "HTTPSEnable : 1" not in out:
            return None
        host = re.search(r"HTTPSProxy\s*:\s*(\S+)", out)
        port = re.search(r"HTTPSPort\s*:\s*(\d+)", out)
        if host and port:
            return "http://%s:%s" % (host.group(1), port.group(1))
    except Exception:
        pass
    return None


def get_proxy():
    """返回形如 'http://127.0.0.1:7897' 的代理串，或 None（30s 缓存）。"""
    now = time.time()
    if _PROXY_CACHE["v"] is not None and now - _PROXY_CACHE["ts"] < 30:
        return _PROXY_CACHE["v"] or None
    v = (_load_cfg().get("proxy") or "").strip() or None
    if not v:
        for k in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            if os.environ.get(k):
                v = os.environ[k].strip()
                break
    if not v:
        v = _detect_system_proxy()
    _PROXY_CACHE.update(v=v or "", ts=now)
    return v


def _opener():
    p = get_proxy()
    if p:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": p, "https": p}))
    return urllib.request.build_opener()


def _now_iso():
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _get_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (wolong desk)"})
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _day_str(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def fetch_binance():
    """返回 {day: close}。失败抛异常。"""
    url = (f"https://api.binance.com/api/v3/klines"
           f"?symbol={SYMBOL}&interval=1d&limit=1000")
    rows = _get_json(url)
    out = {}
    for r in rows:
        out[_day_str(int(r[0]))] = float(r[4])
    return out


def fetch_okx():
    url = ("https://www.okx.com/api/v5/market/candles"
           "?instId=BTC-USDT&bar=1D&limit=1000")
    data = _get_json(url).get("data", [])
    out = {}
    for r in data:
        # OKX 返回最新在前，r[0]=ts(ms) r[4]=close
        out[_day_str(int(r[0]))] = float(r[4])
    return out


def fetch_coingecko():
    url = ("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
           "?vs_currency=usd&days=max&interval=daily")
    prices = _get_json(url).get("prices", [])
    out = {}
    for ts, px in prices:
        out[_day_str(int(ts))] = float(px)
    return out


_FETCHERS = {
    "binance": fetch_binance,
    "okx": fetch_okx,
    "coingecko": fetch_coingecko,
}


def pull_once():
    """按兜底顺序试源，返回 (source, {day:close}) 或 (None, None)。"""
    for name in SOURCES:
        try:
            t0 = time.time()
            bars = _FETCHERS[name]()
            if bars:
                return name, bars, int((time.time() - t0) * 1000)
        except Exception as e:
            sys.stderr.write(f"[feed] {name} 失败: {e}\n")
    return None, None, 0


def fetch_live_price(force=False):
    '''实时价多源兜底：Binance ticker → OKX ticker → CoinGecko simple。

    返回 (source, price) 或 (None, None)。
    ⚠️ 境外源常不可达；连续失败时进入冷却（默认 60s 起、上限 10min），
       冷却期内直接返回 (None, None) 不再打网络 —— 否则每次面板请求
       都会同步重试 3 个源（各 4s 超时），把页面卡到 20~45s。
    '''
    now = time.time()
    if not force and now - _LIVE_FAIL["ts"] < _LIVE_FAIL["cooldown"]:
        return None, None
    if _LIVE_BUSY["on"]:
        return None, None          # 正有人探测 —— 别排队，直接用本地收盘价
    _LIVE_BUSY["on"] = True
    try:
        return _probe_live(force, now)
    finally:
        _LIVE_BUSY["on"] = False


def _probe_live(force, now):
    for name, fn in (
        ("binance", lambda: float(_get_json(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT").get("price", 0))),
        ("okx", lambda: float(_get_json(
            "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT")
            .get("data", [{}])[0].get("last", 0))),
        ("coingecko", lambda: float(_get_json(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
            .get("bitcoin", {}).get("usd", 0))),
    ):
        try:
            p = fn()
            if p and p > 0:
                _LIVE_FAIL.update(ts=0.0, cooldown=0.0)   # 成功 → 清冷却
                return name, p
        except Exception:
            continue
    # 全失败 → 进入/加深冷却（写盘，重启后不立刻重试，避免首屏卡 12s）
    cd = min(_LIVE_FAIL["cooldown"] + LIVE_COOLDOWN_STEP, LIVE_COOLDOWN_MAX) or LIVE_COOLDOWN_STEP
    _LIVE_FAIL.update(ts=now, cooldown=cd)
    _save_live_fail()
    return None, None


# ── 任意币种实时价：供交易台多资产持仓篮取各标的实时价（与 BTC 主策略一致）──
import threading as _th
_SYM_LIVE = {}            # SYM -> {price, ts, src}
_SYM_FAIL = {}            # SYM -> {ts, cooldown}
_SYM_BUSY = _th.Lock()
_SYM_LIVE_TTL = 60.0      # 进程内缓存时长（秒）


def _probe_symbol_live(sym):
    """单币实时价：Binance ticker → OKX ticker。返回 (price, src) 或 (None, None)。"""
    for name, fn in (
        ("binance", lambda: float(_get_json(
            "https://api.binance.com/api/v3/ticker/price?symbol=%sUSDT" % sym).get("price", 0))),
        ("okx", lambda: float(_get_json(
            "https://www.okx.com/api/v5/market/ticker?instId=%s-USDT" % sym)
            .get("data", [{}])[0].get("last", 0))),
    ):
        try:
            p = fn()
            if p and p > 0:
                return p, name
        except Exception:
            continue
    return None, None


def fetch_symbol_live_prices(symbols, force=False):
    """并发取多个币种实时价，返回 {SYM: (src, price)}。供 _holdings_with_price 一次取全篮。

    性能：线程池并发(≤8)、超时受 TIMEOUT 控制；命中 per-symbol 缓存(TTL 60s)直接返回不飘网。
    风险：境外源不可达 → 返回空 dict → 调用方降级本地收盘价（_kline_tail 兜底），不卡面板请求。
    """
    now = time.time()
    out = {}
    need = []
    for s in symbols:
        su = s.upper()
        c = _SYM_LIVE.get(su)
        if not force and c and now - c["ts"] < _SYM_LIVE_TTL:
            out[su] = (c["src"], c["price"])
        else:
            f = _SYM_FAIL.get(su)
            if not force and f and now - f["ts"] < f["cooldown"]:
                continue
            need.append(su)
    if not need:
        return out
    if _SYM_BUSY.locked():        # 已有探测在跑，不重入，用已缓存/降级
        return out
    try:
        with _SYM_BUSY:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            def work(su):
                return su, _probe_symbol_live(su)
            with ThreadPoolExecutor(max_workers=min(8, len(need))) as ex:
                futs = {ex.submit(work, su): su for su in need}
                for fut in as_completed(futs):
                    su, (p, src) = fut.result()
                    if p and p > 0:
                        out[su] = (src, p)
                        _SYM_LIVE[su] = {"price": p, "ts": now, "src": src}
                        _SYM_FAIL.pop(su, None)
                    else:
                        cd = min((_SYM_FAIL.get(su, {}) or {}).get("cooldown", 0.0) + LIVE_COOLDOWN_STEP, LIVE_COOLDOWN_MAX) or LIVE_COOLDOWN_STEP
                        _SYM_FAIL[su] = {"ts": now, "cooldown": cd}
    except Exception:
        pass
    return out


def _live_fail_path():
    return os.path.join(STATE, "_live_fail.json")


def _load_live_fail():
    """读回冷却状态：服务重启后不会立刻重试失败的境外源。"""
    try:
        p = _live_fail_path()
        if os.path.exists(p):
            d = json.load(open(p))
            _LIVE_FAIL["ts"] = float(d.get("ts", 0.0))
            _LIVE_FAIL["cooldown"] = float(d.get("cooldown", 0.0))
    except Exception:
        pass


def _save_live_fail():
    try:
        os.makedirs(STATE, exist_ok=True)
        json.dump({"ts": _LIVE_FAIL["ts"], "cooldown": _LIVE_FAIL["cooldown"]},
                  open(_live_fail_path(), "w"))
    except Exception:
        pass


_load_live_fail()


def live_status():
    """实时价通道状态，供前端区分「真异常」与「境外源不可达/本地兜底」。"""
    now = time.time()
    cooling = (now - _LIVE_FAIL["ts"]) < _LIVE_FAIL["cooldown"]
    return {
        "cooling": bool(cooling),
        "retry_in": int(max(0.0, _LIVE_FAIL["ts"] + _LIVE_FAIL["cooldown"] - now)) if cooling else 0,
        "proxy": get_proxy(),
    }


def merge_to_local(bars):
    """把 {day:close} 增量追加进本地 kline（仅追加新日期，绝不覆盖历史收盘价）。

    ⚠️ 只追加 day 严格大于现有末日的记录 —— 覆盖历史收盘价会让引擎回放与
    回测原始数据产生偏差，破坏阶段 4 同源闸门。
    """
    klines = []
    if os.path.exists(DATA):
        klines = json.load(open(DATA))
    existing = {k["day"] for k in klines}
    last_day = max(existing) if existing else ""
    added = 0
    for day, close in bars.items():
        if day > last_day and day not in existing:
            klines.append({"day": day, "close": close})
            existing.add(day)
            added += 1
    klines.sort(key=lambda k: k["day"])
    json.dump(klines, open(DATA, "w"), ensure_ascii=False)
    return added


def load_health():
    if os.path.exists(HEALTH):
        try:
            return json.load(open(HEALTH))
        except Exception:
            pass
    return {"fail_streak": 0}


def write_health(source, latency_ms, api_ok, kline_last_day, kline_fresh):
    h = load_health()
    if api_ok:
        h["fail_streak"] = 0
        h["last_ok_ts"] = _now_iso()
    else:
        h["fail_streak"] = int(h.get("fail_streak", 0)) + 1
    h.update({
        "api_ok": api_ok,
        "source": source,
        "latency_ms": latency_ms,
        "kline_last_day": kline_last_day,
        "kline_fresh": kline_fresh,
        "checked_at": _now_iso(),
    })
    os.makedirs(STATE, exist_ok=True)
    json.dump(h, open(HEALTH, "w"), ensure_ascii=False, indent=2)
    return h


def last_local_day():
    if not os.path.exists(DATA):
        return None
    klines = json.load(open(DATA))
    return klines[-1]["day"] if klines else None


def kline_is_fresh(last_day):
    if not last_day:
        return False
    today = dt.date.today()
    ld = dt.date.fromisoformat(last_day)
    return (today - ld).days <= 2


def main():
    no_pull = "--no-pull" in sys.argv
    os.makedirs(STATE, exist_ok=True)
    last_day = last_local_day()
    fresh = kline_is_fresh(last_day)

    if no_pull:
        # 只探活：用轻量请求判断可达，不拉全量
        source, bars, lat = pull_once()
        api_ok = source is not None
        # 不合并（--no-pull）
        h = write_health(source or "local", lat, api_ok, last_day, fresh)
        print(f"[feed] 探活完成 source={h['source']} api_ok={api_ok} fail_streak={h['fail_streak']}")
        return

    source, bars, lat = pull_once()
    if source is None:
        # 全失败 → 降级本地
        h = write_health("local", 0, False, last_day, fresh)
        print(f"[feed] 所有远程源失败，降级 local。kline 末日={last_day} 新鲜={fresh}")
        return
    added = merge_to_local(bars)
    last_day = last_local_day()
    fresh = kline_is_fresh(last_day)
    h = write_health(source, lat, True, last_day, fresh)
    print(f"[feed] 拉取成功 source={source} 新增/更新={added} 末日={last_day} 新鲜={fresh}")


if __name__ == "__main__":
    main()
