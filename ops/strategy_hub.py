# -*- coding: utf-8 -*-
"""策略中心 · 多策略注册/路由 + 通用 paper 账本推进器。

架构纠正（2026-09-08，师傅指正）：
- 交易台只有一个窗（ops/serve.py :8790）。多策略是它的「策略维度」，不是第二个窗口。
- 每个策略一份独立 paper 账本：ops/state/strategies/<slug>/{paper,state,trades,equity}.json
- 判定同源：BTC -> engine.ma120_signal；CTA -> research.phase7_wf.compute_target（同一个函数）
- 执行同源：通用内核 engine.strategy_plugin.simulate_equity
  （含再平衡栅 + 共享兜底闸；已证与遗留 ma120 step() 数学等价，BTC parity 4.34e-16）
- 数据源统一：data/kline_*.json（全宇宙 PIT，BTC 亦同源于此，3309 天，2017-08 起）

新策略接入 = engine/plugins/ 加一个 StrategyPlugin 子类 + @register，并在 SLUG 登记一行。
"""
import os
import sys
import json
import bisect
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import importlib
from engine.strategy_plugin import (  # noqa: E402
    DataStore, StrategyPlugin, Composite, simulate_equity, registry, FEE,
)
# 注意：切勿在模块级 import 插件 —— dual_gate_cta 会在 import 时 exec research/phase7_wf
# （拉起 430 币全宇宙，约 27s）。改为按 slug 懒加载，保证「只看 BTC」时不付这笔代价。

STATE_ROOT = os.path.join(HERE, "state")
STRAT_ROOT = os.path.join(STATE_ROOT, "strategies")
START_EQUITY = 100000.0

# 内核/插件逻辑版本号。当 engine/strategy_plugin.py 或某插件的「持仓/再平衡判定」发生变化时，
# 必须 bump 此号（与 config.json version 同步即可）。advance_paper 用它判断「历史 paper 是否该重算」：
# 行情刷新后 as_of 虽已是最新日，但若 compute_version 不一致（逻辑改过），仍整段重算，使历史空仓被修正。
COMPUTE_VERSION = "1.5.10"

# ── 策略注册表（meta 前置：不 import 插件即可知道元信息，避免跨策略副作用）──
STRATS = {
    "btc_ma120": {
        "name": "BTC主策略", "mod": "engine.plugins.btc_ma120",
        "multi_asset": False, "composite": False, "rebal": 1,
    },
    "dual_gate_cta": {
        "name": "双闸CTA", "mod": "engine.plugins.dual_gate_cta",
        "multi_asset": True, "composite": False, "rebal": 7,
    },
    "fusion8020": {
        "name": "融合80/20", "mod": "engine.plugins.fusion",
        "multi_asset": True, "composite": True, "rebal": 1,
    },
}
NAME = {slug: m["name"] for slug, m in STRATS.items()}
SLUG = {m["name"]: slug for slug, m in STRATS.items()}
DEFAULT_SLUG = "btc_ma120"

_LOADED = set()


class UnknownStrategy(ValueError):
    """策略标识无法识别（前端/调用方传了非法的 slug 或展示名）。"""
    pass


def resolve_slug(s):
    """接受 slug 或展示名，统一返回 slug；无法识别抛 UnknownStrategy（中文原因）。

    背景：曾用展示名「融合80/20」直接查 STRATS，抛 KeyError: '融合80/20'，
    经 serve.py 的 str(e) 变成一串带引号的英文抛给前端，师傅看不懂。
    """
    if not s:
        return DEFAULT_SLUG
    s = str(s).strip()
    if s in STRATS:
        return s
    if s in SLUG:
        return SLUG[s]
    known = "、".join("%s(%s)" % (k, v) for k, v in SLUG.items())
    raise UnknownStrategy("无法识别策略「%s」，可用策略：%s" % (s, known))


def _cls(slug):
    """懒加载插件模块（首次按 slug 访问时才 exec，之后用 registry 取类）。"""
    if slug not in _LOADED:
        importlib.import_module(STRATS[slug]["mod"])
        _LOADED.add(slug)
    return registry()[NAME[slug]]

# 融合权重（与 phase8 裁决一致：核心 BTC 80% + 卫星 CTA 20%）
FUSION_W = {"btc_ma120": 0.8, "dual_gate_cta": 0.2}


# ────────────────────────────────────────────────────────────────────
#  行情上下文（懒加载：CTA 需全宇宙 430 币，慢但只做一次）
# ────────────────────────────────────────────────────────────────────
_CTX_CACHE = {}


def _ctx_light():
    """只含 BTC 的轻量上下文（快），给 BTC 单资产策略用。"""
    if "light" not in _CTX_CACHE:
        _CTX_CACHE["light"] = DataStore(["BTC"])
    return _CTX_CACHE["light"]


def _ctx_full():
    """全宇宙上下文（430 币，首次较慢），给多资产 CTA / 融合用。"""
    if "full" not in _CTX_CACHE:
        univ = json.load(open(os.path.join(ROOT, "data", "universe_pit.json")))
        bases = [c["base"] for c in univ["coins"]]
        if "BTC" not in bases:
            bases.append("BTC")
        _CTX_CACHE["full"] = DataStore(bases)
    return _CTX_CACHE["full"]


def ctx_for(slug):
    slug = resolve_slug(slug)
    return _ctx_full() if _is_multi(slug) else _ctx_light()


def _is_multi(slug):
    return bool(STRATS[slug]["multi_asset"])


def _is_composite(slug):
    return bool(STRATS[slug]["composite"])


# ────────────────────────────────────────────────────────────────────
#  路径
# ────────────────────────────────────────────────────────────────────
def sdir(slug):
    p = os.path.join(STRAT_ROOT, slug)
    os.makedirs(p, exist_ok=True)
    return p


def paper_path(slug):
    return os.path.join(sdir(slug), "paper.json")


def state_path(slug):
    return os.path.join(sdir(slug), "state.json")


def equity_path(slug):
    return os.path.join(sdir(slug), "equity.json")


def trades_path(slug):
    return os.path.join(sdir(slug), "trades.json")


def _read(p, default=None):
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return default
    return default


def load_paper(slug):
    return _read(paper_path(slug))


def load_state(slug):
    return _read(state_path(slug))


# ────────────────────────────────────────────────────────────────────
#  策略列表（供前端选择器）
# ────────────────────────────────────────────────────────────────────
def list_strategies():
    out = []
    for slug, meta in STRATS.items():
        p = load_paper(slug)
        st = load_state(slug)
        out.append({
            "slug": slug,
            "name": meta["name"],
            "multi_asset": bool(meta["multi_asset"]),
            "composite": bool(meta["composite"]),
            "rebal": int(meta["rebal"]),
            "status": (p or {}).get("status", "idle"),
            "paper_nav": (p or {}).get("nav"),
            "paper_as_of": (p or {}).get("as_of"),
            "started": bool(p and p.get("series")),
            "bt_cagr": (st or {}).get("cagr"),
            "bt_mdd": (st or {}).get("mdd"),
            "bt_sharpe": (st or {}).get("sharpe"),
        })
    return out


# ────────────────────────────────────────────────────────────────────
#  通用 paper 推进器（核心）
# ────────────────────────────────────────────────────────────────────
def _idx_of_day(days, start_day):
    i = bisect.bisect_left(days, start_day)
    if i >= len(days):
        i = len(days) - 1
    return i


def _flatten_trades(tr_log, closes_of_base, days, eq_by_day, start_equity):
    """把 trace 事件（OPEN/REBAL/STOP）摊平成前端表格用的行：date/side/symbol/price/fee/pnl/equity_after/reason。

    多资产时按「每个币一行」拆开，便于 Trade Log 展示「标的」列。
    """
    rows = []
    for ev in tr_log:
        day = ev["day"]
        try:
            di = days.index(day)
        except ValueError:
            di = None
        kind = ev["kind"]
        if kind == "STOP":
            for f in ev["fired"]:
                exit_px = round(float(closes_of_base(f["base"])[di]), 6) if (di is not None) else None
                entry_px = f.get("entry")
                pnl = None
                # 含费回合收益 = 退出/入场 − 1，再扣双边换仓费（2×FEE）。
                # NAN 强平退出价非有限值，pnl 留空；其余按入场价回算。
                if exit_px is not None and np.isfinite(exit_px) and entry_px and entry_px > 0:
                    pnl = round(exit_px / float(entry_px) - 1.0 - 2.0 * FEE, 6)
                rows.append({
                    "date": day, "side": "SELL", "symbol": f["base"],
                    "weight": round(float(f.get("w", 0.0)), 6),
                    "price": exit_px,
                    "fee": None, "pnl": pnl,
                    "equity_after": ev.get("equity_after"),
                    "reason": f["reason"],
                })
        else:
            for sym, dw in sorted(ev["delta"].items()):
                if abs(dw) < 1e-9:
                    continue
                rows.append({
                    "date": day,
                    "side": "BUY" if dw > 0 else "SELL",
                    "symbol": sym,
                    "weight": abs(dw),
                    "price": (round(float(closes_of_base(sym)[di]), 6) if di is not None else None),
                    "fee": round(float(ev.get("fee", 0.0)) * abs(dw) / max(sum(abs(v) for v in ev["delta"].values()), 1e-9)
                                * float(ev.get("equity_after") or start_equity), 2),
                    "pnl": None,
                    "equity_after": ev.get("equity_after"),
                    "reason": kind,
                })
    return rows


def paper_compute(slug, start_day, start_equity=START_EQUITY):
    """从 start_day 起算该策略的 paper 账户（含首日 jump-in 建仓）。

    返回与遗留 paper.json 同构的 dict（前端字段不变），多资产时额外带 holdings。
    红线：净值一律 *= 累乘（由内核保证）；判定/执行均与回测同源。
    """
    if _is_composite(slug):
        return _fusion_paper(start_equity)

    ctx = ctx_for(slug)
    days = ctx.dates
    n = len(days)
    idx = _idx_of_day(days, start_day)

    plugin = _cls(slug)()
    # init_eq = 本金 -> eq 直接是「元」单位，series/trades 无需再换算
    eq, tr = simulate_equity(plugin, ctx, idx, n - 1, float(start_equity), trace=True, jump_in=True)

    # BH 基准统一用 BTC 买入持有（两个策略同一参照，便于横向比较）
    btc = ctx.close("BTC")
    base_close = btc[idx]
    series = []
    for k in range(len(eq)):
        equity_v = float(eq[k])          # 元
        nav = equity_v / float(start_equity)
        di = idx + k
        if np.isfinite(btc[di]) and np.isfinite(base_close) and base_close > 0:
            bh = float(btc[di]) / float(base_close)
        else:
            bh = nav
        series.append({
            "date": days[di],
            "equity": round(equity_v, 2),
            "bh_equity": round(float(bh) * float(start_equity), 2),
            "nav": round(float(nav), 4),
            "bh_nav": round(float(bh), 4),
            "n_coins": (tr["holdings"][k]["n"] if k < len(tr["holdings"]) else 0),
        })

    nav_arr = np.array([r["nav"] for r in series], float)
    bh_arr = np.array([r["bh_nav"] for r in series], float)
    cagr, mdd, sh = _nav_metrics(nav_arr, len(series))
    bh_c, bh_m, _ = _nav_metrics(bh_arr, len(series))

    last_hold = tr["holdings"][-1] if tr["holdings"] else {"n": 0, "w": {}}
    closes_of_base = ctx.close
    trades = _flatten_trades(tr["trades"], closes_of_base, days, None, start_equity)

    return {
        "status": "running",
        "start_day": days[idx],
        "start_equity": float(start_equity),
        "as_of": days[-1],
        "pos": (len(last_hold["w"]) if _is_multi(slug) else (1 if any(v > 0 for v in last_hold["w"].values()) else 0)),
        "entry_date": (trades[0]["date"] if trades else None),
        "entry_price": (trades[0]["price"] if trades else None),
        "equity": round(float(eq[-1]), 2),
        "nav": round(float(nav_arr[-1]), 4),
        "bh_nav": round(float(bh_arr[-1]), 4),
        "cagr": round(cagr, 4),
        "mdd": round(mdd, 4),
        "sharpe": round(sh, 3),
        "turnover": len(trades),
        "bh_cagr": round(bh_c, 4),
        "bh_mdd": round(bh_m, 4),
        "trades": trades,
        "series": series,
        "holdings": last_hold.get("w", {}),
        "multi_asset": _is_multi(slug),
        # 增量续算 / 盘中实时判定承接字段（单一真相源，避免两套账目互相覆盖）
        "pos_state": tr.get("final_pos", {}),
        "as_of_i": n - 1,
        "eq_state": round(float(eq[-1]), 2),
        "live_equity": round(float(eq[-1]), 2),
        "live_trading": True,
        "last_notified_day": None,
        "last_heartbeat": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _fusion_holdings():
    """融合持仓 = 各子策略当前持仓按资金权重合并（0.8×BTC + 0.2×CTA）。

    融合是 NAV 级派生、不占独立资金，所以没有自己的持仓——必须向下穿透到
    子账本，否则面板会显示「0 币」，与「融合明明有仓位」矛盾。
    """
    merged = {}
    for s_, w in FUSION_W.items():
        p = load_paper(s_)
        if not p:
            continue
        for sym, sw in (p.get("holdings") or {}).items():
            merged[sym] = merged.get(sym, 0.0) + float(w) * float(sw)
    return {k: v for k, v in merged.items() if v > 1e-9}


def _fusion_trades():
    """融合派生持仓的真实成交来自子策略(BTC主策略 / 双闸CTA)。

    融合自身不下单，但其「持有 BTC 0.8」的仓位由子策略 NAV 加权穿透而来，
    真实 BUY/SELL 发生在子策略。此处合并子策略自启动日起的纸面成交、逐笔标注来源，
    作为融合交易记录面板的主源，使「持有 BTC 0.8」有可追溯的建仓成交支撑。
    """
    out = []
    for s, _w in FUSION_W.items():
        p = load_paper(s)
        for t in (p or {}).get("trades") or []:
            tt = dict(t)
            tt["src"] = NAME.get(s, s)
            out.append(tt)
    out.sort(key=lambda t: t.get("date", ""))
    return out


def _fusion_paper(start_equity=START_EQUITY):
    """融合 80/20：不是独立策略，由两个基础策略的 paper NAV 加权派生。

    返回 (data, reason)：data 为 None 时 reason 是**中文**失败原因。
    旧实现统一返回 None，上层一律报「需先分别启动」——但真实原因常是
    「子策略已启动、共同交易日却不足 2 天」（如 CTA 刚启动只有 1 天），
    提示严重误导。此处拆开诊断；并允许仅 1 天时先建账本（warming_up）。
    """
    nav_by_slug = {}
    missing = []
    for s, w in FUSION_W.items():
        p = load_paper(s)
        if not p or not p.get("series"):
            missing.append(NAME.get(s, s))
            continue
        nav_by_slug[s] = {r["date"]: r["nav"] for r in p["series"]}
    if missing:
        return None, "以下子策略尚未启动（无 paper 账本）：" + "、".join(missing) + "。请先分别启动它们。"
    common = None
    for d in nav_by_slug.values():
        common = set(d) if common is None else (common & set(d))
    common = sorted(common or [])
    if not common:
        detail = "；".join(
            "%s 账本区间 %s~%s" % (NAME.get(s_, s_),
                                   (load_paper(s_) or {}).get("start_day"),
                                   (load_paper(s_) or {}).get("as_of"))
            for s_ in FUSION_W)
        return None, "子策略账本没有共同交易日，无法派生。当前：" + detail
    series = []
    for day in common:
        nav = sum(FUSION_W[s] * nav_by_slug[s][day] for s in FUSION_W)
        series.append({"date": day, "nav": round(nav, 4), "equity": round(nav * start_equity, 2)})
    nav_arr = np.array([r["nav"] for r in series], float)
    warm = len(series) < 2
    if warm:
        cagr = mdd = sh = None
    else:
        cagr, mdd, sh = _nav_metrics(nav_arr, len(series))
        cagr, mdd, sh = round(cagr, 4), round(mdd, 4), round(sh, 3)
    return {
        "status": "running",
        "derived": True,
        "warming_up": warm,
        "note": ("共同交易日仅 %d 天，绩效指标待数据积累后再展示" % len(series)) if warm
                else "由 BTC主策略 80% + 双闸CTA 20% 的 paper 净值派生，无独立账本",
        "start_day": common[0],
        "start_equity": float(start_equity),
        "as_of": common[-1],
        "pos": 1,
        "equity": round(float(nav_arr[-1]) * start_equity, 2),
        "nav": round(float(nav_arr[-1]), 4),
        "cagr": cagr, "mdd": mdd, "sharpe": sh,
        "turnover": 0,
        "trades": [],
        "series": series,
        "multi_asset": True,
        "holdings": _fusion_holdings(),
        "last_notified_day": None,
        "last_heartbeat": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }, None


def _nav_metrics(nav, n_days):
    """对起点=1 的 NAV 序列算 CAGR/MDD/Sharpe（与 ma120 metrics 同口径）。"""
    nav = np.asarray(nav, float)
    if len(nav) < 2:
        return 0.0, 0.0, 0.0
    yrs = len(nav) / 365.0
    cagr = float(nav[-1]) ** (1 / yrs) - 1 if nav[-1] > 0 else 0.0
    runmax = np.maximum.accumulate(nav)
    mdd = float((nav / runmax - 1).min())
    rt = nav[1:] / np.where(nav[:-1] > 0, nav[:-1], np.nan) - 1
    rt = rt[np.isfinite(rt)]
    sd = rt.std() if len(rt) else 0.0
    sh = float(rt.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0
    return cagr, mdd, sh


# ────────────────────────────────────────────────────────────────────
#  账本生命周期：启 / 停 / 推进 / 重置
# ────────────────────────────────────────────────────────────────────
def init_paper(slug, reset=False, back_days=None):
    slug = resolve_slug(slug)
    existing = load_paper(slug)
    if existing and not reset:
        return {"ok": True, "skipped": True, "reason": "already initialized",
                "start_day": (existing or {}).get("start_day")}
    if _is_composite(slug):
        d, reason = _fusion_paper()
        if d is None:
            return {"ok": False, "reason": reason}
        json.dump(d, open(paper_path(slug), "w"), ensure_ascii=False, indent=2)
        return {"ok": True, "created": True, "derived": True, "warming_up": bool(d.get("warming_up")),
                "start_day": d["start_day"], "nav": d["nav"]}
    ctx = ctx_for(slug)
    days = ctx.dates
    idx = len(days) - 1 if not back_days else max(0, len(days) - 1 - int(back_days))
    p = paper_compute(slug, days[idx], START_EQUITY)
    # 启动/回溯属于「补历史」，不应把过去几天的成交挨个推送一遍 —— 锚定到当前日
    p["last_notified_day"] = p.get("as_of")
    json.dump(p, open(paper_path(slug), "w"), ensure_ascii=False, indent=2)
    return {"ok": True, "created": True, "start_day": p["start_day"], "nav": p["nav"]}


def paper_continue(slug, p):
    """增量续算：承接上一段的 pos_state / equity，只跑 as_of_i 之后的新交易日。

    与 paper_compute 全量重算的区别：不重跑历史、不破坏已发生的盘中实时平仓（live overlay），
    从而消除「刷新后把盘中已止损的币复活」的风险。compute_version 变更或缺失 as_of_i 时
    不走此路径（由 advance_paper 改为全量重算，安全迁移）。
    """
    ctx = ctx_for(slug)
    days = ctx.dates
    n = len(days)
    asof_i = int(p.get("as_of_i") or (n - 1))
    pos_state = p.get("pos_state") or {}
    eq_state = float(p.get("equity") if p.get("equity") is not None else p.get("eq_state", START_EQUITY))
    plugin = _cls(slug)()
    base_i = _idx_of_day(days, p.get("start_day"))
    R = plugin.rebal or 1
    # 基于「开仓日固定栅格」算下一个再平衡日（第一个 >= asof_i+1 的 base_i + k*R）。
    # ⚠️ 这是修复 v1.5.7 回归 bug 的关键：增量续算必须承接原再平衡栅，不能让内核用 asof_i+R 重置。
    # 持仓中（pos_state 非空）才有意义；空仓时内核走每日检查开仓，不必传。
    next_rebal_i = None
    if pos_state and R:
        k = (asof_i + 1 - base_i + R - 1) // R
        if k < 1:
            k = 1
        next_rebal_i = base_i + k * R
    # 从 asof_i 起（asof_i 当天不再重放，避免与既有 trades 重复），处理 asof_i+1 .. 末日
    eq, tr = simulate_equity(plugin, ctx, asof_i, n - 1, float(eq_state),
                             trace=True, init_pos=pos_state, next_rebal_i=next_rebal_i)
    if len(eq) < 2:
        return None
    btc = ctx.close("BTC")
    base_btc = float(btc[base_i]) if (0 <= base_i < len(btc)) else None
    se = float(p.get("start_equity", START_EQUITY))
    new_series = []
    for k in range(1, len(eq)):
        di = asof_i + k
        ev = float(eq[k])
        nav = ev / se
        if base_btc and np.isfinite(btc[di]) and base_btc > 0:
            bh = float(btc[di]) / base_btc
        else:
            bh = nav
        new_series.append({
            "date": days[di], "equity": round(ev, 2),
            "bh_equity": round(float(bh) * se, 2),
            "nav": round(float(nav), 4), "bh_nav": round(float(bh), 4),
            "n_coins": (tr["holdings"][k - 1]["n"] if (k - 1) < len(tr["holdings"]) else 0),
        })
    full = dict(p)
    full["series"] = (p.get("series") or []) + new_series
    # ⚠️ 续算追加的成交必须「摊平」成与初始 paper_compute 同构的行（date/side/symbol/...），
    # 否则内核原始 REBAL/STOP 事件以 day/delta/kind 字段混入 trades，交易记录面板会渲染成
    # 空白/乱码的「卖出 —」行（前端只读 t.date/t.side/t.symbol/t.reason）。
    cont_flat = _flatten_trades(tr["trades"], ctx.close, days, None,
                                p.get("start_equity", START_EQUITY))
    full["trades"] = (p.get("trades") or []) + cont_flat
    last_hold = tr["holdings"][-1] if tr["holdings"] else {"n": 0, "w": {}}
    full["holdings"] = last_hold.get("w", {})
    full["pos_state"] = tr.get("final_pos", {})
    full["as_of_i"] = n - 1
    full["as_of"] = days[-1]
    full["equity"] = round(float(eq[-1]), 2)
    full["nav"] = round(float(eq[-1]) / se, 4)
    full["pos"] = (len(full["holdings"]) if _is_multi(slug)
                   else (1 if any(v > 0 for v in full["holdings"].values()) else 0))
    nav_arr = np.array([r["nav"] for r in full["series"]], float)
    bh_arr = np.array([r["bh_nav"] for r in full["series"]], float)
    cagr, mdd, sh = _nav_metrics(nav_arr, len(nav_arr))
    bh_c, bh_m, _ = _nav_metrics(bh_arr, len(nav_arr))
    full["cagr"] = round(cagr, 4); full["mdd"] = round(mdd, 4); full["sharpe"] = round(sh, 3)
    full["bh_cagr"] = round(bh_c, 4); full["bh_mdd"] = round(bh_m, 4)
    full["turnover"] = len(full["trades"])
    full["live_trading"] = True
    return full


def _notify_new(slug, full, last_day, last_notified_day):
    """抽取「截至 last_day 且晚于 last_notified_day」的新成交通知。"""
    notified = 0
    notify_err = None
    try:
        import notify
        cands = notify.candidates(full["trades"], last_day, last_notified_day)
        nctx = dict(full)
        nctx["strategy_name"] = NAME.get(slug, slug)
        nctx["n_days"] = len(full.get("series") or [])
        for t in cands:
            notify.send_one(t, nctx)
        if cands:
            notified = len(cands)
        full["last_notified_day"] = last_day
    except Exception as e:
        notify_err = str(e)
    return notified, notify_err


def advance_paper(slug):
    slug = resolve_slug(slug)
    p = load_paper(slug)
    if not p:
        return {"ok": False, "reason": "not initialized"}
    if p.get("status") != "running":
        return {"ok": True, "skipped": True, "status": p.get("status")}
    if _is_composite(slug):
        d, reason = _fusion_paper(p.get("start_equity", START_EQUITY))
        if d is None:
            return {"ok": True, "skipped": True, "reason": reason}
        d["last_heartbeat"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        d["compute_version"] = COMPUTE_VERSION
        d["live_trading"] = False  # 融合为派生视图，无独立持仓，不做盘中实时判定
        json.dump(d, open(paper_path(slug), "w"), ensure_ascii=False, indent=2)
        return {"ok": True, "advanced_to": d["as_of"], "nav": d["nav"]}
    ctx = ctx_for(slug)
    # 重算判定：
    # ① compute_version 不一致（逻辑改过）② 缺 as_of_i（旧版账本迁移）→ 全量重算
    # ③ as_of 落后或 as_of_i 未到末日 → 增量续算（保留盘中实时平仓 overlay）
    recompute_full = (p.get("compute_version") != COMPUTE_VERSION) or (p.get("as_of_i") is None) or (p.get("as_of") is None)
    if recompute_full:
        full = paper_compute(slug, p["start_day"], p.get("start_equity", START_EQUITY))
    elif (p.get("as_of") < ctx.dates[-1]) or ((p.get("as_of_i") or 0) < len(ctx.dates) - 1):
        full = paper_continue(slug, p)
    else:
        # 无新交易日：仅心跳（不重算，保留盘中 overlay）
        p["last_heartbeat"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        json.dump(p, open(paper_path(slug), "w"), ensure_ascii=False, indent=2)
        return {"ok": True, "skipped": True, "as_of": p.get("as_of")}
    if full is None:
        return {"ok": False, "reason": "continuation failed"}
    full["last_heartbeat"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    full["compute_version"] = COMPUTE_VERSION
    full["live_trading"] = True
    notified, notify_err = _notify_new(slug, full, ctx.dates[-1], p.get("last_notified_day"))
    json.dump(full, open(paper_path(slug), "w"), ensure_ascii=False, indent=2)
    out = {"ok": True, "advanced_to": full["as_of"], "nav": full["nav"], "notified": notified}
    if notify_err:
        out["notify_error"] = notify_err
    return out


def check_live_stops(slug, now=None):
    """盘中实时判定（模拟实盘 OCO / 跟踪止损）：用实时最新成交价对每个持仓币复核兜底闸。

    触发 hard_stop / trail_stop / delist / NAN → 立即盘中平仓，写回 paper.json（单一真相源），
    并推送通知。trail_stop 的峰值用实时最高价维护（贴合实盘 trailing）。

    安全：实时源不可达 → 静默跳过（不假触发、不崩）。本机境外源不可达时自动降级，
    故仅部署在直连环境（如新加坡/香港）才会真正执行盘中触发。
    """
    slug = resolve_slug(slug)
    p = load_paper(slug)
    if not p or p.get("status") != "running":
        return {"ok": False, "reason": "not running"}
    if _is_composite(slug):
        return {"ok": False, "reason": "融合为派生视图，无独立持仓"}
    plugin = _cls(slug)()
    pos_state = p.get("pos_state") or {
        s: {"w": w, "entry": None, "peak": None} for s, w in (p.get("holdings") or {}).items()
    }
    if not pos_state:
        return {"ok": True, "checked": 0, "triggered": 0}
    syms = list(pos_state.keys())
    try:
        import feed
        live = feed.fetch_symbol_live_prices(syms) or {}
    except Exception:
        live = {}
    if not live:
        return {"ok": True, "checked": len(syms), "triggered": 0, "skipped_source": True}
    now = now or datetime.now()
    cb = _cost_basis(p.get("trades") or [])
    equity = float(p.get("equity") if p.get("equity") is not None else p.get("eq_state", START_EQUITY))
    live_equity = float(p.get("live_equity") if p.get("live_equity") is not None else equity)
    # 先整簿标记到实时价（live_equity）
    for sym, st in pos_state.items():
        lr = live.get(sym.upper()) or live.get(sym)
        if not lr:
            continue
        px = float(lr[1])
        if not np.isfinite(px) or px <= 0:
            continue
        last, _ = _kline_tail(sym)
        if last and last > 0:
            live_equity = live_equity * (1.0 + st["w"] * (px / last - 1.0))
    events = []
    for sym, st in list(pos_state.items()):
        lr = live.get(sym.upper()) or live.get(sym)
        if not lr:
            continue
        px = float(lr[1])
        bad = (not np.isfinite(px)) or (px <= 0)
        if bad:
            reason = "NAN"
        else:
            entry = st.get("entry") or cb.get(sym)
            if entry is None or entry <= 0:
                continue
            st["peak"] = max(st.get("peak") or px, px)
            if plugin.delist_thr and px < entry * (1.0 - plugin.delist_thr):
                reason = "DELIST"
            elif plugin.hard_stop and px < entry * (1.0 - plugin.hard_stop):
                reason = "STOP"
            elif plugin.trail_stop and st["peak"] and px < st["peak"] * (1.0 - plugin.trail_stop):
                reason = "STOP"
            else:
                continue
        w = float(st["w"])
        entry = st.get("entry") or cb.get(sym)
        exit_px = px if (np.isfinite(px) and px > 0) else None
        pnl = (exit_px / entry - 1.0 - 2.0 * FEE) if (exit_px and entry and entry > 0) else None
        # close-based equity 减仓（移除该币权重，与每日重算口径一致）；实时盈亏由 live_equity 承载
        equity = equity * (1.0 - w)
        ev = {
            "date": now.strftime("%Y-%m-%d"), "side": "SELL", "symbol": sym,
            "weight": round(w, 6),
            "price": round(exit_px, 6) if exit_px else None,
            "fee": None, "pnl": round(pnl, 6) if pnl is not None else None,
            "equity_after": round(equity, 2), "reason": reason, "live": True,
        }
        events.append(ev)
        del pos_state[sym]
    if events:
        p["trades"] = (p.get("trades") or []) + events
        p["holdings"] = {s: v["w"] for s, v in pos_state.items()}
        p["pos_state"] = pos_state
        p["equity"] = round(equity, 2)
        p["live_equity"] = round(live_equity, 2)
        p["pos"] = len(p["holdings"])
        p["last_live_check"] = now.strftime("%Y-%m-%dT%H:%M:%S")
        p["last_heartbeat"] = p["last_live_check"]
        p["live_trading"] = True
        json.dump(p, open(paper_path(slug), "w"), ensure_ascii=False, indent=2)
        try:
            import notify
            nctx = dict(p)
            nctx["strategy_name"] = NAME.get(slug, slug)
            nctx["n_days"] = len(p.get("series") or [])
            for t in events:
                notify.send_one(t, nctx)
        except Exception:
            pass
        return {"ok": True, "checked": len(syms), "triggered": len(events), "events": events}
    p["live_equity"] = round(live_equity, 2)
    p["pos_state"] = pos_state
    p["last_live_check"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    p["last_heartbeat"] = p["last_live_check"]
    p["live_trading"] = True
    json.dump(p, open(paper_path(slug), "w"), ensure_ascii=False, indent=2)
    return {"ok": True, "checked": len(syms), "triggered": 0}


def set_paper_status(slug, status):
    slug = resolve_slug(slug)
    p = load_paper(slug)
    if not p:
        return {"ok": False, "reason": "not initialized"}
    if _is_composite(slug) and status == "running":
        return {"ok": False, "reason": "融合为派生视图，无独立账本"}
    p["status"] = status
    p["last_heartbeat"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    json.dump(p, open(paper_path(slug), "w"), ensure_ascii=False, indent=2)
    return {"ok": True, "status": status}


def reset_paper(slug):
    """归零到纯净未启动态（不调用 paper_compute —— 避免首笔买入幻觉）。"""
    slug = resolve_slug(slug)
    ctx = ctx_for(slug)
    start_day = ctx.dates[-1]
    p = {
        "status": "idle", "start_day": start_day, "start_equity": START_EQUITY,
        "as_of": start_day, "pos": 0, "entry_date": None, "entry_price": None,
        "equity": START_EQUITY, "nav": 1.0, "bh_nav": 1.0,
        "cagr": 0.0, "mdd": 0.0, "sharpe": 0.0, "turnover": 0,
        "bh_cagr": 0.0, "bh_mdd": 0.0,
        "trades": [], "series": [], "holdings": {},
        "multi_asset": _is_multi(slug),
        "last_notified_day": None,
        "compute_version": None,
        "last_heartbeat": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    json.dump(p, open(paper_path(slug), "w"), ensure_ascii=False, indent=2)
    return {"ok": True, "reset": True, "start_day": start_day, "nav": 1.0}


# ────────────────────────────────────────────────────────────────────
#  L3 聚合层：给 :8790 交易台产出统一面板数据
#  （交易台只有一个窗 —— 多策略是「策略维度」，不是第二个窗口）
# ────────────────────────────────────────────────────────────────────
def _ms():
    """按绝对路径加载 ma120_signal（绕开 ops/engine.py 与 engine/ 包同名冲突）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ma120_signal_shared", os.path.join(ROOT, "engine", "ma120_signal.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_LIVE = {"src": None, "price": None, "ts": 0.0, "tried": 0.0}
_LIVE_TTL = 15.0      # 成功值复用 15s（避免每个请求都打 API）


def _live_price(force=False):
    """带进程缓存的实时价。失败不再每次重试 —— feed 内有失败冷却。"""
    import time as _t
    now = _t.time()
    if not force and _LIVE["price"] is not None and now - _LIVE["ts"] < _LIVE_TTL:
        return _LIVE["src"], _LIVE["price"]
    try:
        import feed
        src, p = feed.fetch_live_price(force=force)
    except Exception:
        src, p = None, None
    if p:
        _LIVE.update(src=src, price=p, ts=now)
    else:
        # 拿不到价：短时间内不重复打网络（feed 冷却已保证），仅更新尝试时间
        _LIVE.update(tried=now)
    return src, p


def live_status():
    """实时价通道状态：区分「正常 / 境外源不可达(本地收盘价兜底) / 冷却中」。"""
    try:
        import feed
        st = feed.live_status()
    except Exception:
        st = {}
    import time as _t
    st["has_price"] = _LIVE["price"] is not None
    st["source"] = _LIVE["src"]
    st["age_sec"] = int(_t.time() - _LIVE["ts"]) if _LIVE["price"] is not None else None
    return st


# ── 持仓篮：每个标的补最新价（本地 K 线末收盘；BTC 有实时价则用实时价）──
_KLINE_TAIL = {}


def _kline_tail(sym):
    """返回 (last_close, prev_close)。读 data/kline_<SYM>.json，带缓存。"""
    if sym in _KLINE_TAIL:
        return _KLINE_TAIL[sym]
    out = (None, None)
    path = os.path.join(ROOT, "data", "kline_%s.json" % sym)
    try:
        if os.path.exists(path):
            rows = json.load(open(path))
            if rows:
                last = float(rows[-1].get("close") or 0) or None
                prev = float(rows[-2].get("close") or 0) if len(rows) > 1 else None
                out = (last, prev)
    except Exception:
        out = (None, None)
    _KLINE_TAIL[sym] = out
    return out


def _holdings_with_price(hold, slug=None):
    """给持仓篮每只币补 last_price / chg_pct（日涨跌）。

    多资产没有单一报价，所以「最新价」= 各持仓标的的最新价，诚实分列展示，
    绝不用 BTC 价冒充篮子报价。每只标的尽力取实时价（Binance/OKX ticker，
    带 TTL 缓存+失败冷却），取不到则降级本地 K 线末日收盘价——与 BTC 主策略一致。
    """
    items = sorted((hold or {}).items(), key=lambda kv: -kv[1])
    live_map = {}
    if items:
        try:
            import feed
            live_map = feed.fetch_symbol_live_prices([s for s, _ in items]) or {}
        except Exception:
            live_map = {}
    out = []
    n_live = 0
    for sym, w in items:
        last, prev = _kline_tail(sym)
        lr = live_map.get(sym.upper())
        if lr and lr[1] and lr[1] > 0:
            last = float(lr[1])
            n_live += 1
        chg = (last / prev - 1.0) if (last and prev) else None
        out.append({
            "symbol": sym,
            "w": round(float(w), 4),
            "last_price": round(float(last), 6) if last else None,
            "chg_pct": round(chg * 100, 2) if chg is not None else None,
            "live": bool(lr and lr[1] and lr[1] > 0),
        })
    src = ("live(%d/%d)" % (n_live, len(items))) if n_live else "close"
    return {"items": out, "price_source": src}


def _cost_basis(trades):
    """从成交流反推当前持仓各币的加权平均买入价（成本基准），供投资组合浮盈亏展示。

    逐笔回放：BUY 累加权重与加权成本，SELL 按比例减成本。最终仅保留净持仓为正的标的，
    返回 {symbol: 平均买入价}。与 holdings 对齐——已清仓的标的不应出现在浮盈亏表。
    """
    pos = {}  # sym -> [net_weight, weighted_cost]
    for t in trades or []:
        sym = t.get("symbol")
        if not sym:
            continue
        w = float(t.get("weight") or 0.0)
        price = float(t.get("price") or 0.0)
        cur = pos.get(sym, [0.0, 0.0])
        if t.get("side") == "BUY":
            cur[0] += w
            cur[1] += w * price
        elif t.get("side") == "SELL":
            if cur[0] > 1e-9:
                cur[1] -= (w / cur[0]) * cur[1]
            cur[0] -= w
            if cur[0] <= 1e-9:
                cur = [0.0, 0.0]
        pos[sym] = cur
    out = {}
    for sym, (net_w, cost) in pos.items():
        if net_w > 1e-9:
            out[sym] = round(cost / net_w, 6)
    return out


def _paper_live(paper, slug):
    """日内市价标记。

    BTC 单资产：用实时价 mark-to-market。
    多资产篮（CTA/融合）：实时标记由 check_live_stops（盘中实时判定）维护到
    live_equity 字段（整簿按实时价标记 + 盘中平仓），此处直接采用；取不到则兜底收盘权益，
    绝不用 BTC 价冒充篮子报价。
    """
    p = dict(paper or {})
    p["live_marked"] = False
    p["live_equity"] = p.get("equity")
    p["live_nav"] = p.get("nav")
    p["live_price"] = None
    if not p:
        return p
    if _is_multi(slug):
        le = p.get("live_equity")
        if le is not None:
            p["live_equity"] = round(float(le), 2)
            p["live_marked"] = True
            p["live_nav"] = round(float(le) / float(p.get("start_equity", START_EQUITY)), 4)
        else:
            p["live_equity"] = p.get("equity")
            p["live_marked"] = False
            p["live_nav"] = p.get("nav")
        return p
    src, live = _live_price()
    p["live_price"] = round(float(live), 2) if live else None
    p["live_source"] = src
    if p.get("status") in ("running", "paused") and live and p.get("pos") == 1:
        ctx = _ctx_light()
        days = ctx.dates
        try:
            asof_close = float(ctx.close("BTC")[days.index(p["as_of"])])
        except Exception:
            asof_close = None
        if asof_close:
            le = float(p["equity"]) * (1.0 + (float(live) / asof_close - 1.0))
            p["live_equity"] = round(le, 2)
            p["live_nav"] = round(le / float(p.get("start_equity", START_EQUITY)), 4)
            p["live_marked"] = True
    return p


def _btc_quote(paper):
    import numpy as np
    ctx = _ctx_light()
    days, closes = ctx.dates, ctx.close("BTC")
    last = len(days) - 1
    ma = _ms().ma120_of(closes)
    src, live = _live_price()
    last_price = float(live) if live else float(closes[last])
    ma120 = float(ma[last]) if np.isfinite(ma[last]) else None
    dist = (last_price / ma120 - 1) * 100 if ma120 else None
    pos = int((paper or {}).get("pos", 0) or 0)
    sig_ago = None
    tr = (paper or {}).get("trades") or []
    if tr:
        from datetime import date
        sig_ago = (datetime.now().date() - date.fromisoformat(tr[-1]["date"])).days
    return {
        "last_price": round(last_price, 2),
        "ma120": round(ma120, 2) if ma120 else None,
        "dist_pct": round(dist, 2) if dist is not None else None,
        "pos": pos,
        "pos_label": "持仓 LONG" if pos == 1 else "空仓 CASH",
        "pos_since": (paper or {}).get("entry_date"),
        "signal_days_ago": sig_ago,
        "live_source": src,
        "as_of": days[last],
    }


def _symbol_price365(symbol):
    """近 365 日某标的的价格 + MA120（迷你走势图）。多资产主导持仓(如融合的 BTC 主仓)复用。"""
    import numpy as np
    try:
        ctx = _ctx_light()
        days, closes = ctx.dates, ctx.close(symbol)
        ma = _ms().ma120_of(closes)
        N = min(365, len(days))
        out = []
        for i in range(len(days) - N, len(days)):
            out.append({
                "day": days[i],
                "close": round(float(closes[i]), 2) if np.isfinite(closes[i]) else None,
                "ma120": round(float(ma[i]), 2) if np.isfinite(ma[i]) else None,
            })
        # 仅 BTC 有实时价源，末点用实时价覆盖；其他标的用收盘价
        if symbol == "BTC":
            _, live = _live_price()
            if out and live:
                out[-1]["close"] = round(float(live), 2)
        return out
    except Exception:
        return []


def _btc_price365():
    """近 365 日 BTC 价格 + MA120（迷你走势图）。仅 BTC 策略有意义。"""
    return _symbol_price365("BTC")


def _top_holdings(holdings, n=6):
    items = sorted((holdings or {}).items(), key=lambda kv: -kv[1])[:n]
    return [{"symbol": k, "w": round(float(v), 4)} for k, v in items]


def desk_payload(slug):
    """:8790 交易台统一数据源。返回结构与原 engine.get_desk() 兼容，多了策略维度信息。"""
    slug = resolve_slug(slug)
    p = load_paper(slug)
    p = _paper_live(p, slug)
    # 融合是派生策略：自身不下单，持仓由子策略 NAV 加权穿透；真实成交发生在子策略，
    # 在此合并子策略纸面成交作为交易记录主源，使「持有 BTC 0.8」有可追溯的建仓成交支撑。
    # 必须在多资产分支之前合并——成本基准(_cost_basis)与交易面板都依赖这份合并成交。
    if _is_composite(slug) and p is not None:
        ft = _fusion_trades()
        p = dict(p)
        p["trades"] = ft
        p["turnover"] = len(ft)
    st = load_state(slug)
    perf = None
    if st:
        perf = {
            "equity": st.get("equity"), "nav": st.get("nav"), "bh_nav": st.get("bh_nav"),
            "cagr": st.get("cagr"), "mdd": st.get("mdd"), "sharpe": st.get("sharpe"),
            "turnover": st.get("turnover"), "bh_cagr": st.get("bh_cagr"),
            "bh_mdd": st.get("bh_mdd"), "deviation": st.get("deviation"),
            "start_equity": st.get("start_equity"),
        }
    multi = _is_multi(slug)
    if multi:
        hold = (p or {}).get("holdings") or {}
        hp = _holdings_with_price(hold, slug)
        # 反推各持仓币加权平均买入价 → 浮盈亏（与 holdings 对齐）
        cb = _cost_basis((p or {}).get("trades") or [])
        be = (p or {}).get("equity")
        for it in hp["items"]:
            sym = it["symbol"]
            entry = cb.get(sym)
            it["entry"] = entry
            if entry and entry > 0 and it.get("last_price"):
                it["unreal_pct"] = round((it["last_price"] / entry - 1) * 100, 2)
                if be:
                    it["unreal_usdt"] = round(it["w"] * float(be) * (it["last_price"] / entry - 1), 2)
        quote = {
            "multi": True,
            "pos": len(hold),
            "n_coins": len(hold),
            "holdings": hp["items"],
            "price_source": hp["price_source"],
            "basket_equity": be,
            "as_of": (p or {}).get("as_of"),
            "note": ("投资组合 %d 只 · 买入价/浮盈亏见下表 · 最新价取各标的实时价(源不可达时降级收盘价) · 止损/止盈以盘中实时判定为准" % len(hold)) if hold
                    else "当前空仓 · 无持仓标的",
        }
        # 多资产无单一篮子报价，但若有主导持仓(如融合的 BTC 主仓 80%)，展示其「个股价」走势，
        # 并标注为「主仓标的」价格，绝不用它冒充篮子报价。
        price365 = []
        price_symbol = None
        if hold:
            dom = max(hold.items(), key=lambda kv: kv[1])[0]
            price365 = _symbol_price365(dom)
            price_symbol = dom
    else:
        quote = _btc_quote(p)
        price365 = _btc_price365()
        price_symbol = "BTC"

    return {
        "strategy": slug,
        "name": NAME[slug],
        "multi_asset": multi,
        "composite": _is_composite(slug),
        "live_trading": (p or {}).get("live_trading", False),
        "strategies": list_strategies(),
        "health": _read(os.path.join(STATE_ROOT, "health.json")),
        "live_status": live_status(),
        "quote": quote,
        "perf": perf,
        "trades": (p or {}).get("trades") or [],
        # 纸面账户自启动日起的真实成交，是交易记录面板的唯一数据源；
        # 不展示任何全样本回测成交/净值（用户明确：交易台=手动开启后的纸面模拟，非回测展示器）。
        "state": st,
        "price365": price365,
        "price_symbol": price_symbol,
        "paper": p,
    }


# ────────────────────────────────────────────────────────────────────
#  全样本回测（Paper 之外的「同源闸门」参照）
# ────────────────────────────────────────────────────────────────────
def _parity(slug, my_eq):
    """与回测原口径对比的最大相对偏差（红线：应 ≈ 0）。"""
    try:
        import numpy as np
        ref = None
        if slug == "btc_ma120":
            ctx = _ctx_light()
            ref = _ms().run_equity(ctx.close("BTC")) * float(START_EQUITY)
        elif slug == "dual_gate_cta":
            mod = sys.modules.get("phase7_wf_shared")
            if mod is not None:
                ref = mod.run({"sig": "B+T", "max_w": 0.10, "min_coins": 3, "gate": True},
                              0, len(my_eq) - 1) * float(START_EQUITY)
        if ref is None:
            return None
        m = min(len(ref), len(my_eq))
        denom = np.maximum(np.abs(np.asarray(ref[:m], float)), 1e-9)
        return float(np.max(np.abs(np.asarray(my_eq[:m], float) - ref[:m]) / denom))
    except Exception:
        return None


def full_backtest(slug, start_equity=START_EQUITY):
    slug = resolve_slug(slug)
    """全样本回测（2017→今）落盘到 strategies/<slug>/{state,equity,trades}.json。

    用的就是 simulate_equity + 插件 decide —— 与 research 回测同源（同一内核/同一判定），
    所以面板上显示的 CAGR/MDD/Sharpe 就是回测口径，不是另算一套。
    """
    if _is_composite(slug):
        return _fusion_backtest(start_equity)

    ctx = ctx_for(slug)
    days = ctx.dates
    n = len(days)
    plugin = _cls(slug)()
    eq, tr = simulate_equity(plugin, ctx, 0, n - 1, float(start_equity), trace=True)

    nav_arr = np.asarray(eq, float) / float(start_equity)
    cagr, mdd, sh = _nav_metrics(nav_arr, n)

    btc = np.asarray(ctx.close("BTC"), float)
    base = float(btc[0]) if np.isfinite(btc[0]) and btc[0] > 0 else 1.0
    bh_arr = np.where(np.isfinite(btc), btc / base, 1.0)
    bh_c, bh_m, _ = _nav_metrics(bh_arr, n)

    trades = _flatten_trades(tr["trades"], ctx.close, days, None, start_equity)
    last_hold = tr["holdings"][-1] if tr["holdings"] else {"n": 0, "w": {}}

    equity_series = [{"day": days[i],
                      "equity": round(float(eq[i]), 2),
                      "nav": round(float(nav_arr[i]), 4)} for i in range(n)]

    st = {
        "as_of": days[-1], "n_days": n,
        "equity": round(float(eq[-1]), 2),
        "nav": round(float(nav_arr[-1]), 4),
        "bh_nav": round(float(bh_arr[-1]), 4),
        "start_equity": float(start_equity),
        "cagr": round(cagr, 4), "mdd": round(mdd, 4), "sharpe": round(sh, 3),
        "turnover": len(trades),
        "bh_cagr": round(bh_c, 4), "bh_mdd": round(bh_m, 4),
        "deviation": _parity(slug, eq),
        "n_coins": int(last_hold.get("n", 0)),
    }
    json.dump(st, open(state_path(slug), "w"), ensure_ascii=False, indent=2)
    json.dump(equity_series, open(equity_path(slug), "w"), ensure_ascii=False)
    json.dump(trades, open(trades_path(slug), "w"), ensure_ascii=False)
    return st


def _fusion_backtest(start_equity=START_EQUITY):
    """融合：全样本 NAV 由两个子策略的回测净值加权派生。"""
    navs = {}
    dates = None
    for s, w in FUSION_W.items():
        p = os.path.join(sdir(s), "equity.json")
        if not os.path.exists(p):
            return {"ok": False, "reason": f"{s} 尚未跑全样本回测"}
        series = json.load(open(p))
        navs[s] = {r["day"]: r["nav"] for r in series}
        ds = set(navs[s])
        dates = ds if dates is None else (dates & ds)
    dates = sorted(dates or [])
    if len(dates) < 2:
        return {"ok": False, "reason": "无共同日期"}
    nav_arr = np.array([sum(FUSION_W[s] * navs[s][d] for s in FUSION_W) for d in dates], float)
    cagr, mdd, sh = _nav_metrics(nav_arr, len(dates))
    st = {
        "as_of": dates[-1], "n_days": len(dates),
        "equity": round(float(nav_arr[-1]) * start_equity, 2),
        "nav": round(float(nav_arr[-1]), 4),
        "start_equity": float(start_equity),
        "cagr": round(cagr, 4), "mdd": round(mdd, 4), "sharpe": round(sh, 3),
        "turnover": 0, "deviation": None, "derived": True,
    }
    json.dump(st, open(state_path("fusion8020"), "w"), ensure_ascii=False, indent=2)
    json.dump([{"day": dates[i], "nav": round(float(nav_arr[i]), 4),
                "equity": round(float(nav_arr[i]) * start_equity, 2)} for i in range(len(dates))],
              open(equity_path("fusion8020"), "w"), ensure_ascii=False)
    json.dump([], open(trades_path("fusion8020"), "w"), ensure_ascii=False)
    return st


# ────────────────────────────────────────────────────────────────────
#  运行健康巡检：异常主动告警（此前只有成交通知，异常静默）
# ────────────────────────────────────────────────────────────────────
def _day_gap(a, b):
    """两个 YYYY-MM-DD 相差天数；解析失败返回 None。"""
    try:
        return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
    except Exception:
        return None


def _hb_age_sec(p):
    """账本心跳距今秒数；无心跳返回 None。"""
    hb = (p or {}).get("last_heartbeat")
    if not hb:
        return None
    try:
        return (datetime.now() - datetime.strptime(hb, "%Y-%m-%dT%H:%M:%S")).total_seconds()
    except Exception:
        return None


def healthcheck(slug=None, notify_on_issue=True):
    """巡检策略运行状态，返回问题清单；有异常时经 notify 发中文告警（按类冷却）。

    覆盖：
      1. 数据停更   —— 账本 as_of 明显落后于数据最新交易日
      2. 同源漂移   —— 引擎净值 vs 回测净值 deviation 超过 1e-2
      3. 心跳超时   —— status=running 但超过 48h 没有推进/心跳
      4. 融合断供   —— 融合已启动，但子策略账本缺失或无共同交易日
      5. 通知未配置 —— 没有任何可用通道，异常送不出去
    """
    issues = []
    slugs = [resolve_slug(slug)] if slug else list(STRATS.keys())

    for s_ in slugs:
        name = NAME.get(s_, s_)
        p = load_paper(s_)
        st = load_state(s_)

        # 2) 同源漂移（不依赖账本是否存在）
        if st and st.get("deviation") is not None:
            try:
                dev = float(st["deviation"])
            except Exception:
                dev = None
            if dev is not None and abs(dev) > 1e-2:
                issues.append({
                    "kind": "parity_" + s_, "strategy": s_,
                    "title": "%s 同源校验漂移超标" % name,
                    "lines": ["漂移值：%.3e（告警阈值 1.0e-02）" % dev,
                              "引擎净值与回测净值已不再逐点一致，决策可能已不同源。",
                              "建议：点「回填同源闸门」重算，或检查插件是否改过判定逻辑。"]})

        if not p:
            continue

        # 1) 数据停更
        if p.get("status") == "running":
            try:
                last_day = ctx_for(s_).dates[-1]
                gap = _day_gap(p.get("as_of"), last_day) if p.get("as_of") else None
                if gap is not None and gap >= 2:
                    issues.append({
                        "kind": "data_stale_" + s_, "strategy": s_,
                        "title": "%s 数据已停更" % name,
                        "lines": ["账本最新日 %s，数据最新日 %s，落后 %d 个交易日。"
                                  % (p.get("as_of"), last_day, gap),
                                  "建议：点「刷新行情」拉最新数据。"]})
            except Exception:
                pass

            # 3) 心跳超时
            age = _hb_age_sec(p)
            if age is not None and age > 48 * 3600:
                issues.append({
                    "kind": "heartbeat_" + s_, "strategy": s_,
                    "title": "%s 账本长时间未推进" % name,
                    "lines": ["距上次心跳已 %.1f 小时（阈值 48 小时）。" % (age / 3600.0),
                              "策略状态仍为「运行中」，但一直没有推进。",
                              "建议：确认刷新流程是否在跑，或先「停止」该策略。"]})

        # 4) 融合断供
        if _is_composite(s_):
            d, reason = _fusion_paper(p.get("start_equity", START_EQUITY))
            if d is None:
                issues.append({
                    "kind": "fusion_broken", "strategy": s_,
                    "title": "融合80/20 无法派生", "lines": [reason or "子策略账本不可用。",
                                                        "建议：重新启动对应子策略。"]})

    # 5) 通知通道未配置
    try:
        import notify
        stt = notify.get_status() or {}
        if not any(v.get("enabled") and v.get("configured") for v in stt.values()):
            issues.append({
                "kind": "notify_unconfigured", "strategy": "-",
                "title": "通知通道未配置",
                "lines": ["当前没有任何「已启用且配置完整」的通知通道，异常与成交通知都送不出去。",
                          "建议：在交易台「通知设置」里配置通道后点「测试推送」。"]})
    except Exception:
        pass

    if notify_on_issue and issues:
        try:
            import notify
            for it in issues:
                notify.send_alert(it["kind"], it["title"], it["lines"])
        except Exception:
            pass

    return {"ok": not issues, "issues": issues, "checked": slugs,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
