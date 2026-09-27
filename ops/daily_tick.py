# -*- coding: utf-8 -*-
"""交易台每日推进：刷新行情 + 推进纸面账户序列。

每日应在交易台运行期执行一次（建议交易日 08:30 前后）：
  1) feed.py        —— 多源兜底拉 BTC 日线，增量追加进 data/kline_BTC.json（不覆盖历史）。
  2) feed_multi.py   —— 增量+代理感知拉多币日线，追加进 data/kline_<BASE>.json。
                         dual_gate_cta 依赖多币行情，滞后会让候选池塌缩致空仓。
  3) advance_paper(slug) —— 把每个 running 策略的 paper.series 延展到最新交易日，
     使净值曲线随真实行情增长（曲线只在 series>=2 时才绘制）。

退出码 0 = 成功推进；非 0 = 有策略推进失败（供自动化判定是否告警）。
"""
import os
import sys
import json
import subprocess
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)  # 让 strategy_hub / notify 可被 import
os.chdir(HERE)

VENPY = sys.executable  # 应由带 numpy 的 venv python 调用
STRATS = ["btc_ma120", "dual_gate_cta", "fusion8020"]


def step_feed():
    print("[daily_tick] 1) feed.py 刷新行情 ...")
    try:
        r = subprocess.run([VENPY, os.path.join(HERE, "feed.py")],
                           capture_output=True, text=True, timeout=120)
        line = (r.stdout or "").strip().splitlines()
        last = line[-1] if line else (r.stderr or "").strip().splitlines()[-1:]
        last = last[0] if isinstance(last, list) else last
        print("   ", last)
        return True
    except Exception as e:
        print("   feed 失败:", e)
        return False


def step_advance():
    print("[daily_tick] 2) advance_paper 推进纸面序列 ...")
    import strategy_hub as h
    summary = {}
    ok = True
    for s in STRATS:
        try:
            res = h.advance_paper(s)
            summary[s] = res
            print(f"   {s}: advanced_to={res.get('advanced_to')} nav={res.get('nav')} "
                  f"notified={res.get('notified')}")
            if not res.get("ok"):
                ok = False
        except Exception as e:
            summary[s] = {"error": str(e)}
            ok = False
            print(f"   {s}: ERROR {e}")
    return ok, summary


def main():
    t0 = datetime.datetime.now()
    step_feed()
    step_feed_multi()
    ok, summary = step_advance()
    print("[daily_tick] done @", t0.isoformat(), "->", datetime.datetime.now().isoformat())
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
