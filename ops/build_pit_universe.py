# -*- coding: utf-8 -*-
"""构建 Binance 全量 PIT 候选宇宙（命实盘可行性）。

与 universe_candidates.json（终点流动性头部 107 只，自带幸存者偏差）不同，
本脚本取 **当前仍在 Binance 上市** 的全部 USDT 现货对（剔除稳定币/杠杆代币/法币），
只要当前 24h 报价额 ≥ 10万 USD（仅排除 dust，不做事前"头部"筛选）。
配合引擎内的 PIT 滚动流动性闸门（每再平衡日用截至当日的 trailing 30d 成交量判定合格），
即可在回测中自然剔除"当时没流动性/未上市"的币，且纳入"曾经热过、后来凉了"的币——
大幅压缩原宇宙的幸存者偏差。

注：已退市(完全下架)的币不在 exchangeInfo 中，本宇宙仍不含它们（残余偏差，实盘也无法交易已退市币，
属可接受且已披露）。若未来要纳入退市币，需另取社区退市清单补充。

产物：data/universe_pit.json
"""
import os, sys, json, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "universe_pit.json")

HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]
STABLES = {"USDT","USDC","BUSD","TUSD","DAI","FDUSD","USDP","PYUSD","USD1",
            "EUR","GBP","AUD","BRL","TRY","RUB","UAH","NGN","ZAR","BIDR","IDRT","RUPX",
            "EURUSDT","GBPUSDT"}  # 冗余保险
MIN_QV = 100_000  # 当前24h报价额下限（USD），仅排 dust


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "wolong-pit"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))


def fetch_json(path):
    last = None
    for h in HOSTS:
        try:
            return _get(h + path)
        except Exception as e:
            last = e
    raise RuntimeError(f"all hosts failed: {last}")


def is_leverage_token(base):
    return (base.endswith(("UP","DOWN","BULL","BEAR","3L","3S","5L","5S"))
            or "LEVER" in base or base in ("BTCDOM","DEFI"))


def main():
    ei = fetch_json("/api/v3/exchangeInfo")
    tk = fetch_json("/api/v3/ticker/24hr")
    qv = {t["symbol"]: float(t["quoteVolume"]) for t in tk if t["symbol"].endswith("USDT")}

    syms = [s for s in ei["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
            and s.get("isSpotTradingAllowed")]
    out = []
    for s in syms:
        b = s["baseAsset"]
        if b in STABLES or is_leverage_token(b):
            continue
        q = qv.get(s["symbol"], 0.0)
        if q < MIN_QV:
            continue
        out.append({"symbol": s["symbol"], "base": b, "quoteVol24hUsd": q})

    out.sort(key=lambda x: -x["quoteVol24hUsd"])
    meta = {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "source": "Binance exchangeInfo + ticker/24hr",
        "filter": f"USDT spot TRADING, 剔除稳定币/杠杆代币/法币, 当前24h报价额>={MIN_QV/1000:.0f}k USD",
        "note": "PIT 滚动流动性闸门在引擎内按每日 trailing 30d 成交量判定合格；本列表仅为候选全集。",
        "n": len(out),
    }
    json.dump({"meta": meta, "coins": out}, open(OUT, "w"), ensure_ascii=False, indent=1)
    # 统计：当前<200万(曾经热过) 占比
    cooled = sum(1 for c in out if c["quoteVol24hUsd"] < 2_000_000)
    print(f"[pit] 候选全集: {len(out)} 只")
    print(f"[pit] 其中当前24h报价额<200万USD(曾热后凉, 修正幸存者偏差主体): {cooled} 只 ({cooled/len(out)*100:.0f}%)")
    print(f"[pit] written {OUT}")


if __name__ == "__main__":
    main()
