# -*- coding: utf-8 -*-
"""L2 撮合引擎 · 复用 engine/ma120_signal 同源判定，落盘 state/trades/equity.json。

幂等（D7）：若 state.as_of >= kline 末日，跳过撮合只刷新心跳；否则全量回放。
全量回放是 run_equity() 的纯函数 —— 与回测（research/btc_ma120_signals.py）逐点同源，
阶段 4 回填闸门即验证这一点（parity_max_diff == 0.0）。

零新依赖；模拟口径：起始 100,000 USDT（D5）。
"""
import os
import sys
import json
import importlib.util
from datetime import datetime, date
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ⚠️ 本脚本名为 engine.py，与 engine/ 包同名——若走 `from engine.xxx import`，
# 运行时会把本脚本自身当成 engine 模块而失败。用 importlib 按绝对路径加载共享模块，
# 彻底绕开同名冲突，保证与回测（research/btc_ma120_signals.py）import 同一份源码。
def _load_ma120():
    spec = importlib.util.spec_from_file_location(
        "ma120_signal_shared",
        os.path.join(ROOT, "engine", "ma120_signal.py"),
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

_MS = _load_ma120()
run_equity = _MS.run_equity
target_position = _MS.target_position
metrics = _MS.metrics
WINDOW = _MS.WINDOW
FEE = _MS.FEE
WARMUP = _MS.WARMUP

DATA = os.path.join(ROOT, "data", "kline_BTCUSD.json")
STATE = os.path.join(HERE, "state")
PAPER = os.path.join(STATE, "paper.json")
START_EQUITY = 100000.0  # D5 模拟起始权益


def load_kline():
    import numpy as np
    d = json.load(open(DATA))
    days = [x["day"] for x in d]
    closes = np.array([float(x["close"]) for x in d], float)
    return days, closes


def load_state():
    p = os.path.join(STATE, "state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None


def compute():
    """全量回放。返回摘要 dict，并落盘 state/trades/equity.json。

    纯函数（仅依赖 kline），保证与回测同源。幂等：相同输入 → 相同输出。
    """
    import numpy as np
    days, closes = load_kline()
    n = len(closes)
    eq_norm = run_equity(closes)          # 第 0 日 = 1
    eq_abs = eq_norm * START_EQUITY       # 绝对权益（USDT）

    # ---- 成交流水（pos 变化处记一笔，reason 固定 MA120_CROSS）----
    trades = []
    pos = 0.0
    entry_equity = None                   # 进入多头前的现金（用于算回合收益）
    for i in range(1, n):
        tgt = target_position(closes, i)
        if tgt != pos:
            side = "BUY" if tgt == 1 else "SELL"
            price = float(closes[i])
            eq_before = float(eq_abs[i - 1])
            fee = eq_before * FEE
            pnl = None
            if side == "BUY":
                entry_equity = eq_before
            else:
                if entry_equity is not None:
                    pnl = eq_abs[i] / entry_equity - 1   # 回合收益（含费，已进 eq_abs）
                entry_equity = None
            trades.append({
                "date": days[i],
                "side": side,
                "price": round(price, 2),
                "fee": round(fee, 2),
                "pnl": (round(pnl, 4) if pnl is not None else None),
                "equity_after": round(float(eq_abs[i]), 2),
                "reason": "MA120_CROSS",
            })
            pos = tgt

    # ---- 净值序列（含 B&H 对照）----
    base = closes[WARMUP]
    equity_rows = []
    for i in range(n):
        p = target_position(closes, i)
        bh_nav = closes[i] / base
        equity_rows.append({
            "date": days[i],
            "equity": round(float(eq_abs[i]), 2),
            "bh_equity": round(float(START_EQUITY * bh_nav), 2),
            "pos": int(p),
            "nav": round(float(eq_abs[i] / START_EQUITY), 4),
        })

    # ---- 当前状态快照 ----
    last = n - 1
    cur_pos = int(target_position(closes, last))
    last_buy = [t for t in trades if t["side"] == "BUY"]
    entry_date = last_buy[-1]["date"] if last_buy else None
    entry_price = last_buy[-1]["price"] if last_buy else None
    last_price = float(closes[last])
    if cur_pos == 1:
        shares = round(eq_abs[last] / last_price, 6)
        cash = 0.0
    else:
        shares = 0.0
        cash = float(eq_abs[last])

    state = {
        "as_of": days[last],
        "pos": cur_pos,
        "entry_date": entry_date,
        "entry_price": entry_price,
        "shares": shares,
        "cash": round(cash, 2),
        "equity": round(float(eq_abs[last]), 2),
        "last_heartbeat": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "window": WINDOW,
        "fee": FEE,
        "start_equity": START_EQUITY,
    }
    # ---- 绩效（对齐回测口径：从 WARMUP 日起，须先归一化到起点=1）----
    eq_w = eq_abs[WARMUP:] / eq_abs[WARMUP]
    fin, cagr, mdd, sh = metrics(eq_w)
    bh_w = closes[WARMUP:] / base
    _, bh_c, bh_m, _ = metrics(bh_w)
    turnover = len(trades)

    # 绩效字段随快照落盘，UI 直接读，避免每次请求重算
    pc = parity_check()
    bh_nav = float(closes[last] / base)
    state.update({
        "cagr": round(cagr, 4),
        "mdd": round(mdd, 4),
        "sharpe": round(sh, 3),
        "turnover": turnover,
        "bh_cagr": round(bh_c, 4),
        "bh_mdd": round(bh_m, 4),
        "bh_nav": round(bh_nav, 4),
        "deviation": pc["parity_max_diff"],
    })

    # ---- 落盘 ----
    os.makedirs(STATE, exist_ok=True)
    json.dump(state, open(os.path.join(STATE, "state.json"), "w"), ensure_ascii=False, indent=2)
    json.dump(trades, open(os.path.join(STATE, "trades.json"), "w"), ensure_ascii=False, indent=2)
    json.dump(equity_rows, open(os.path.join(STATE, "equity.json"), "w"), ensure_ascii=False, indent=2)

    # ---- 绩效（对齐回测口径：从 WARMUP 日起，须先归一化到起点=1）----
    eq_w = eq_abs[WARMUP:] / eq_abs[WARMUP]
    fin, cagr, mdd, sh = metrics(eq_w)
    bh_w = (closes[WARMUP:] / base)            # 与 signals.py 的 bh_a 完全一致
    _, bh_c, bh_m, _ = metrics(bh_w)
    turnover = len(trades)

    return {
        "as_of": state["as_of"],
        "pos": cur_pos,
        "equity": state["equity"],
        "nav": round(float(eq_abs[last] / START_EQUITY), 4),
        "cagr": round(cagr, 4),
        "mdd": round(mdd, 4),
        "sharpe": round(sh, 3),
        "turnover": turnover,
        "bh_cagr": round(bh_c, 4),
        "bh_mdd": round(bh_m, 4),
        "trades": len(trades),
    }


def run(force=False):
    """对外入口：幂等。state.as_of 已追上 kline 末日则跳过重算（除非 force）。"""
    st = load_state()
    days, _ = load_kline()
    last_day = days[-1]
    if (not force) and st and st.get("as_of") >= last_day:
        # 只刷新心跳，不重算
        st["last_heartbeat"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        json.dump(st, open(os.path.join(STATE, "state.json"), "w"), ensure_ascii=False, indent=2)
        return {"skipped": True, "as_of": last_day, "reason": "already up to date"}
    return compute()


def parity_check():
    """阶段 4 闸门：引擎净值 vs 回测净值（research/btc_ma120_signals.py 口径）。

    回测用 run_equity(closes)，引擎也用 run_equity(closes) —— 同源函数。
    两者逐点比较，返回最大相对偏差。应为 0.0。
    """
    import numpy as np
    days, closes = load_kline()
    eq_norm = run_equity(closes)
    # 回测口径：对齐到 WARMUP 起点归一（与 signals.py 的 eq_a 完全一致）
    base = eq_norm[WARMUP]
    eng_a = eq_norm[WARMUP:] / base
    # 回测同一函数再算一遍（理论上 identical），验证同源无漂移
    bt_a = run_equity(closes)[WARMUP:] / base
    diff = np.max(np.abs(eng_a - bt_a))
    return {
        "parity_max_diff": float(diff),
        "rows": len(eng_a),
        "as_of": days[-1],
        "cagr": round(metrics(eng_a)[1], 4),
    }



_LIVE_CACHE = {"price": None, "ts": 0.0, "source": None}


def _live_price():
    '''多源兜底拉实时价，15s 内复用缓存避免频繁打 API。返回 (source, price|None)。'''
    now = time.time()
    if _LIVE_CACHE["price"] is not None and now - _LIVE_CACHE["ts"] < 15:
        return _LIVE_CACHE["source"], _LIVE_CACHE["price"]
    try:
        import feed
        src, p = feed.fetch_live_price()
        if p:
            _LIVE_CACHE.update(price=p, ts=now, source=src)
            return src, p
    except Exception:
        pass
    return _LIVE_CACHE["source"], _LIVE_CACHE["price"]


def get_quote():
    """基础行情快照：最新价 / MA120 / 距均线 / 仓位 / 持仓起算 / 距上次信号天数。"""
    import numpy as np
    days, closes = load_kline()
    last = len(closes) - 1
    ma = _MS.ma120_of(closes)
    src, live = _live_price()
    last_price = float(live) if live else float(closes[last])
    live_source = src
    ma120 = float(ma[last]) if np.isfinite(ma[last]) else None
    dist = (last_price / ma120 - 1) * 100 if ma120 else None
    st = load_state()
    pos = st.get("pos", 0) if st else 0
    pos_since = st.get("entry_date") if (st and pos == 1) else None

    sig_ago = None
    tp = os.path.join(STATE, "trades.json")
    if os.path.exists(tp):
        tr = json.load(open(tp))
        if tr:
            last_trade_day = tr[-1]["date"]
            sig_ago = (datetime.now().date() - date.fromisoformat(last_trade_day)).days

    return {
        "last_price": round(last_price, 2),
        "ma120": round(ma120, 2) if ma120 else None,
        "dist_pct": round(dist, 2) if dist is not None else None,
        "pos": pos,
        "pos_label": "持仓 LONG" if pos == 1 else "空仓 CASH",
        "pos_since": pos_since,
        "signal_days_ago": sig_ago,
        "live_source": live_source,
        "as_of": days[last],
    }


# ────────────────────────────────────────────────────────────────────
#  Paper 账户（与回测分离：从 0 开始的纸面记录）
#  回测 = 全历史，作为同源闸门参照；Paper = 交易台启动日起的真实行情模拟。
#  Paper 判定仍复用 engine/ma120_signal 同源 step()，保证买卖逻辑与回测一致。
# ────────────────────────────────────────────────────────────────────

def _load_paper():
    if os.path.exists(PAPER):
        try:
            return json.load(open(PAPER))
        except Exception:
            return None
    return None


def paper_compute(start_day, start_equity=START_EQUITY):
    """纯函数：从 start_day 起算 paper 账户（起始权益 start_equity，nav 起点=1）。

    复用 step()（同源红线：净值一律 *= 累乘）。返回完整 paper 结构 dict。
    start_day 当天按 target_position 立即对齐信号（jump-in）。
    """
    import numpy as np
    days, closes = load_kline()
    n = len(closes)
    # 定位 start_idx：取 >= start_day 的第一个交易日
    start_idx = None
    for i, d in enumerate(days):
        if d >= start_day:
            start_idx = i
            break
    if start_idx is None:
        start_idx = n - 1

    equity = float(start_equity)
    pos = 0.0
    entry_equity = None
    trades = []
    series = []
    base = closes[start_idx]

    for i in range(start_idx, n):
        eq_new, pos_new, traded = _MS.step(equity, pos, closes, i)
        if traded:
            side = "BUY" if pos_new == 1 else "SELL"
            price = float(closes[i])
            fee = equity * FEE
            pnl = None
            if side == "BUY":
                entry_equity = equity
            else:
                if entry_equity is not None:
                    pnl = eq_new / entry_equity - 1
                entry_equity = None
            trades.append({
                "date": days[i],
                "side": side,
                "price": round(price, 2),
                "fee": round(fee, 2),
                "pnl": (round(pnl, 4) if pnl is not None else None),
                "equity_after": round(eq_new, 2),
                "reason": "MA120_CROSS",
            })
        equity, pos = eq_new, pos_new
        bh = closes[i] / base
        series.append({
            "date": days[i],
            "equity": round(float(equity), 2),
            "bh_equity": round(float(start_equity * bh), 2),
            "pos": int(pos),
            "nav": round(float(equity / start_equity), 4),
        })

    # ── 绩效（基于 paper 序列，从 0 开始）──
    eqs = np.array([r["equity"] for r in series], float)
    nav_series = eqs / start_equity
    fin, cagr, mdd, sh = metrics(nav_series)
    bh_eqs = np.array([r["bh_equity"] for r in series], float)
    _, bh_c, bh_m, _ = metrics(bh_eqs / start_equity)

    last = n - 1
    cur_pos = int(pos)
    last_buy = [t for t in trades if t["side"] == "BUY"]
    entry_date = last_buy[-1]["date"] if last_buy else None
    entry_price = last_buy[-1]["price"] if last_buy else None

    return {
        "status": "running",
        "start_day": days[start_idx],
        "start_equity": float(start_equity),
        "as_of": days[last],
        "pos": cur_pos,
        "entry_date": entry_date,
        "entry_price": entry_price,
        "equity": round(float(equity), 2),
        "nav": round(float(equity / start_equity), 4),
        "bh_nav": round(float(closes[last] / base), 4),
        "cagr": round(cagr, 4),
        "mdd": round(mdd, 4),
        "sharpe": round(sh, 3),
        "turnover": len(trades),
        "bh_cagr": round(bh_c, 4),
        "bh_mdd": round(bh_m, 4),
        "trades": trades,
        "series": series,
        "last_notified_day": None,
        "last_heartbeat": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


def init_paper(reset=False, back_days=None):
    """初始化/重置 paper 账户。

    start_day 默认 = 最新 K 线日（今天，最严格符合"从 0 开始"）；
    若给定 back_days，则从 back_days 个交易日前的位置起算——权益仍从 100k 起，
    只是回溯窗口更长、曲线更完整（便于即时查看 paper 轨迹）。
    """
    existing = _load_paper()
    if existing and not reset:
        return {"ok": True, "skipped": True, "reason": "already initialized",
                "start_day": existing.get("start_day")}
    days, _ = load_kline()
    if back_days and back_days > 0:
        idx = max(0, len(days) - 1 - int(back_days))
        start_day = days[idx]
    else:
        start_day = days[-1]
    p = paper_compute(start_day, START_EQUITY)
    json.dump(p, open(PAPER, "w"), ensure_ascii=False, indent=2)
    return {"ok": True, "created": True, "start_day": start_day,
            "status": p["status"], "equity": p["equity"]}


def paper_advance():
    """推进 paper 账户：拉到最新 K 线日。幂等——同日起多次刷新不重复记账。"""
    st = _load_paper()
    if not st:
        return {"ok": False, "reason": "not initialized"}
    if st["status"] != "running":
        return {"ok": True, "skipped": True, "status": st["status"]}
    days, _ = load_kline()
    last_day = days[-1]
    if st["as_of"] >= last_day:
        st["last_heartbeat"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        json.dump(st, open(PAPER, "w"), ensure_ascii=False, indent=2)
        return {"ok": True, "skipped": True, "as_of": last_day}
    # 重算 start_day → last（O(n) 但简单可靠，杜绝增量状态漂移）
    full = paper_compute(st["start_day"], st["start_equity"])
    full["last_heartbeat"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    # ── 新成交通知（仅「今天」且晚于上次通知日；回溯/启动不通知，避免轰炸）──
    notified = 0
    try:
        import notify
        cands = notify.candidates(full["trades"], last_day, full.get("last_notified_day"))
        for t in cands:
            notify.send_one(t, full)
        if cands:
            notified = len(cands)
            full["last_notified_day"] = last_day
    except Exception:
        pass
    json.dump(full, open(PAPER, "w"), ensure_ascii=False, indent=2)
    return {"ok": True, "advanced_to": last_day, "as_of": st["as_of"], "notified": notified}


def set_paper_status(status):
    """running / paused / idle"""
    st = _load_paper()
    if not st:
        return {"ok": False, "reason": "not initialized"}
    st["status"] = status
    st["last_heartbeat"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    json.dump(st, open(PAPER, "w"), ensure_ascii=False, indent=2)
    return {"ok": True, "status": status}


def reset_paper():
    '''重置 paper 到纯净「未启动」态：权益 100k、pos 0、空交易/空序列、指标 0、status=idle。

    ⚠️ 不调用 paper_compute —— 否则单点窗口会按当日信号立即记一笔买入（首笔买入幻觉）。
    重置只是把账户归零，真正的建仓在「启动」后由真实行情信号触发。
    '''
    days, _ = load_kline()
    start_day = days[-1]
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    p = {
        "status": "idle",
        "start_day": start_day,
        "start_equity": START_EQUITY,
        "as_of": start_day,
        "pos": 0,
        "entry_date": None,
        "entry_price": None,
        "equity": round(START_EQUITY, 2),
        "nav": 1.0,
        "bh_nav": 1.0,
        "cagr": 0.0,
        "mdd": 0.0,
        "sharpe": 0.0,
        "turnover": 0,
        "bh_cagr": 0.0,
        "bh_mdd": 0.0,
        "trades": [],
        "series": [],
        "last_notified_day": None,
        "last_heartbeat": now,
    }
    json.dump(p, open(PAPER, "w"), ensure_ascii=False, indent=2)
    return {"ok": True, "reset": True, "start_day": start_day,
            "status": "idle", "equity": p["equity"], "nav": p["nav"]}


def _paper_live(paper, days, closes):
    """给 paper 账户加「日内市价标记」：持仓(LONG)时，用实时价对末日权益做 mark-to-market，
    让绩效面板的 NAV 随实时价跳动。

    红线：paper.json 仍只记日收盘口径，绝不被日内值污染；这里只是展示层叠加一个 live 标记，
    返回的是 paper 的副本，不落盘。杠杆 lev 来自 paper.leverage（当前框架恒为 1，现货无杠杆）。
    """
    import numpy as np
    p = dict(paper)
    p["live_marked"] = False
    p["asof_close"] = None
    p["live_equity"] = p.get("equity")
    p["live_nav"] = p.get("nav")
    src, live = _live_price()
    p["live_price"] = round(live, 2) if live else None
    p["live_source"] = src
    if p.get("status") in ("running", "paused") and live and len(days):
        try:
            idx = list(days).index(p["as_of"])
            asof_close = float(closes[idx])
        except Exception:
            asof_close = None
        p["asof_close"] = round(asof_close, 2) if asof_close else None
        lev = float(p.get("leverage", 1.0) or 1.0)
        if p.get("pos") == 1 and asof_close:
            # 日内线性杠杆：live_eq = equity * (1 + lev * (live/asof_close - 1))；lev=1 即现货
            live_eq = p["equity"] * (1.0 + lev * (live / asof_close - 1.0))
            p["live_equity"] = round(live_eq, 2)
            p["live_nav"] = round(live_eq / p["start_equity"], 4)
            p["live_marked"] = True
    return p


def get_desk():
    """聚合 /api/desk 所需的全部面板数据（直接读落盘 json，不重算）。"""
    st = load_state()
    paper = _load_paper()
    try:
        _days, _closes = load_kline()
    except Exception:
        _days, _closes = [], []
    if paper:
        try:
            paper = _paper_live(paper, _days, _closes)
        except Exception:
            pass
    health = None
    hp = os.path.join(STATE, "health.json")
    if os.path.exists(hp):
        health = json.load(open(hp))

    quote = get_quote()

    perf = None
    if st:
        perf = {
            "equity": st["equity"],
            "nav": round(st["equity"] / st["start_equity"], 4),
            "bh_nav": st.get("bh_nav"),
            "cagr": st.get("cagr"),
            "mdd": st.get("mdd"),
            "sharpe": st.get("sharpe"),
            "turnover": st.get("turnover"),
            "bh_cagr": st.get("bh_cagr"),
            "bh_mdd": st.get("bh_mdd"),
            "deviation": st.get("deviation"),
            "start_equity": st.get("start_equity"),
        }

    trades = []
    tp = os.path.join(STATE, "trades.json")
    if os.path.exists(tp):
        trades = json.load(open(tp))

    # 净值序列（主图曲线）
    equity = []
    eqp = os.path.join(STATE, "equity.json")
    if os.path.exists(eqp):
        equity = json.load(open(eqp))

    # 近 365 日价格 + MA120（迷你走势图）
    price365 = []
    try:
        import numpy as np
        days, closes = load_kline()
        ma = _MS.ma120_of(closes)
        N = min(365, len(closes))
        for i in range(len(closes) - N, len(closes)):
            price365.append({
                "day": days[i],
                "close": round(float(closes[i]), 2),
                "ma120": (round(float(ma[i]), 2) if np.isfinite(ma[i]) else None),
            })
    except Exception:
        pass
    # 末点用实时价，让迷你走势末端随行情跳动（历史点不动，保持同源）
    if price365:
        _, live = _live_price()
        if live:
            price365[-1]["close"] = round(float(live), 2)

    return {
        "health": health, "quote": quote, "perf": perf,
        "trades": trades, "state": st, "equity": equity, "price365": price365,
        "paper": paper,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--parity", action="store_true", help="跑回填同源闸门")
    a = ap.parse_args()
    if a.parity:
        r = parity_check()
        print(f"[parity] parity_max_diff={r['parity_max_diff']:.2e}  rows={r['rows']}  as_of={r['as_of']}  cagr={r['cagr']}")
        print("PASS" if r["parity_max_diff"] == 0.0 else "FAIL")
    else:
        r = run(force=a.force)
        print(json.dumps(r, ensure_ascii=False, indent=2))
