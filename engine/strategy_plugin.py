# -*- coding: utf-8 -*-
"""统一多策略交易台 · 策略插件契约与并行执行内核。

设计（见 docs/Paper交易台_原型设计.md 红线）：
- 判定函数与回测同源：BTC 插件直接 import engine.ma120_signal；CTA 插件移植 phase7 同一套
  指标与 compute_target，禁止各自实现。
- 净值循环 *= 累乘；费率 0.1%；无前视（decide 只用 i-1 及之前）。
- 每个策略独立权益流（隔离回测）；融合为 NAV 级组合，不占独立资金。
- deviation_from_backtest 漂移预警：引擎净值须逐点 == 回测净值。

插件只需实现 decide(i, ctx) -> {base: weight}；行情/执行/共享风控闸/观测全部由内核复用。
新增策略 = 新增一个 StrategyPlugin 子类 + 在 registry 注册，其余零改动。
"""
import os
import json
import numpy as np
from abc import ABC, abstractmethod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
FEE = 0.001  # 换仓费 0.1%


# ── 共享行情仓库（L1 统一多资产缓存消费层）──
class DataStore:
    """消费 data/kline_<BASE>.json（已回填的多资产缓存），对齐到全局交易日向量。"""

    def __init__(self, bases):
        self._mat = {}
        self._load(bases)

    def _load(self, bases):
        raw = {}
        for b in bases:
            p = os.path.join(DATA, f"kline_{b}.json")
            if not os.path.exists(p):
                continue
            d = json.load(open(p))
            days = [x["day"] for x in d]
            closes = np.array([float(x["close"]) for x in d], np.float64)
            raw[b] = (days, closes)
        allset = set()
        for days, _ in raw.values():
            allset.update(days)
        self._dates = sorted(allset)
        self._idx = {d: i for i, d in enumerate(self._dates)}
        self._n = len(self._dates)
        self._bases = list(raw.keys())
        for b, (days, closes) in raw.items():
            arr = np.full(self._n, np.nan, np.float64)
            for d, c in zip(days, closes):
                arr[self._idx[d]] = c
            self._mat[b] = arr

    @property
    def dates(self):
        return self._dates

    @property
    def n(self):
        return self._n

    @property
    def bases(self):
        return self._bases

    def has(self, base):
        return base in self._mat

    def close(self, base):
        return self._mat[base]

    def ma(self, base, w):
        a = self._mat[base]
        m = len(a)
        out = np.full(m, np.nan)
        s = 0.0
        for i in range(m):
            s += a[i]
            if i >= w:
                s -= a[i - w]
            if i >= w - 1:
                out[i] = s / w
        return out


# ── 策略插件契约 ──
class StrategyPlugin(ABC):
    name = "unnamed"
    multi_asset = False
    rebal = 1            # 决策刷新节奏（天）；引擎每日调 decide，插件自决何时真正换仓
    max_single = None    # 单币限仓（None=不限）
    min_coins = 1        # 合格数不足则空仓
    delist_thr = None    # 退市代理：持仓跌穿 -X%（None=关）
    hard_stop = None     # 硬止损（None=关）
    trail_stop = None    # 移动止盈（None=关）

    def prepare(self, ctx):
        """预计算（指标等）。引擎在首次 decide 前调用。默认空。"""
        pass

    @abstractmethod
    def decide(self, i, ctx) -> dict:
        """第 i 日(成交日)目标权重；决策只用 i-1 及之前信息。返回 {base: w}，权重和<=1。"""
        raise NotImplementedError


class Composite(ABC):
    """NAV 级组合（如融合 80/20）。不占独立资金，由引擎在基础策略 NAV 上派生。"""

    name = "composite"

    def __init__(self, weights):
        self.weights = weights  # {strategy_name: w}

    @abstractmethod
    def nav(self, navs):
        """navs: {name: np.ndarray} → 派生 np.ndarray（与基础 NAV 同长）。"""
        raise NotImplementedError


# ── 注册表 ──
_REGISTRY = {}


def register(plugin_cls):
    _REGISTRY[plugin_cls.name] = plugin_cls
    return plugin_cls


def registry():
    return dict(_REGISTRY)


# ── 通用并行执行内核 ──
def _sanitize(target, ctx, i, plugin):
    """共享兜底闸：剔除非有限价、单币限仓封顶、归一化、不足 min_coins 即空仓。"""
    out = {}
    for b, w in target.items():
        if not ctx.has(b):
            continue
        c = ctx.close(b)[i]
        if not np.isfinite(c):
            continue
        w = max(0.0, float(w))
        if plugin.max_single:
            w = min(w, plugin.max_single)
        if w > 0:
            out[b] = w
    s = sum(out.values())
    if s <= 0:
        return {}
    if abs(s - 1.0) > 1e-9:
        out = {b: w / s for b, w in out.items()}
    if len(out) < (plugin.min_coins or 1):
        return {}
    return out


def _turnover(pos, target):
    keys = set(pos.keys()) | set(target.keys())
    return sum(abs(target.get(k, 0.0) - pos.get(k, {}).get("w", 0.0)) for k in keys)


def simulate_equity(plugin, ctx, start_i=0, end_i=None, init_eq=1.0, trace=False,
                    jump_in=False, init_pos=None, next_rebal_i=None):
    """通用权益模拟：每个策略独立权益流，与回测同口径。

    红线：净值 *= 累乘；费率 0.1%；无前视（decide 内部保证）。
    共享兜底闸：NaN 即清仓、退市代理、硬止损、移动止盈、单币限仓。

    再平衡栅（与回测同源，关键）：仅「再平衡日」才依 decide() 重设仓位；其余交易日
    只执行共享兜底闸（止损/退市），book 只缩不扩——被止损的币不会在两次再平衡之间被
    静默回补（否则会抹掉回测里的止损收益）。再平衡日由 plugin.rebal 决定（BTC=1 即每日，
    CTA=7 即周频）。

    trace=True 时额外返回 {"trades": [...], "holdings": [...]}，用于 paper 账本/流水。
    ⚠️ trace 只做「记录」，绝不参与净值计算 —— trace=False 与 True 的净值必须逐点相等。
    """
    n = ctx.n
    if end_i is None:
        end_i = n - 1
    plugin.prepare(ctx)
    R = plugin.rebal or 1
    first_rebal = ((start_i + R - 1) // R) * R
    # 增量续算模式（init_pos 存在）：再平衡锚点由调用方基于「开仓日固定栅格」算好并传入，
    # 此处不得重算/覆盖（否则每次增量续算都把栅格重置为 asof_i+R，使再平衡日无限后推、永不在
    # 已发生区间触发——v1.5.7 的回归 bug）。仅当调用方未传时才退回全局栅格。
    next_rebal_i = (first_rebal if next_rebal_i is None else next_rebal_i)
    pos = {}  # base -> {w, entry, peak}
    if init_pos is not None:
        # 增量续算模式：承接上一段的持仓状态（含 entry/peak），不再 jump_in
        pos = {b: dict(v) for b, v in init_pos.items()}
        jump_in = False
    eq = [init_eq]
    tr_log = [] if trace else None
    hold_log = [] if trace else None
    if jump_in:
        # 账户启动日：按 decide() 立即建仓（对齐 legacy paper_compute 的 start_day 立即交易语义）
        # ⚠️ 只此一次，之后仍走正常再平衡栅 —— 不影响 rebal_day 判定逻辑。
        target = _sanitize(plugin.decide(start_i, ctx), ctx, start_i, plugin)
        if _turnover(pos, target) > 1e-9:
            f0 = _turnover(pos, target) * FEE
            if trace:
                tr_log.append({
                    "day": ctx.dates[start_i],
                    "kind": "OPEN",
                    "delta": {b: round(float(target.get(b, 0.0)), 6) for b in sorted(target.keys())},
                    "turnover": round(float(_turnover(pos, target)), 6),
                    "fee": round(float(f0), 6),
                    "equity_after": round(float(eq[0] * (1.0 - f0)), 2),
                })
            newpos = {}
            for b, w in target.items():
                c = ctx.close(b)[start_i]
                newpos[b] = {"w": w, "entry": c, "peak": c}
            pos = newpos
            eq[0] = eq[0] * (1.0 - f0)
        if trace:
            # ⚠️ 关键：1 天 jump_in 模拟的日循环为空，hold_log 不会被循环补写，
            # 否则 tr["holdings"]=[] → paper_compute 误判 pos=0 / 空仓。
            # 这里把「启动日(含 jump_in)建仓后」的持仓快照补录，使 hold_log 与 eq 等长对齐。
            hold_log.append({
                "day": ctx.dates[start_i],
                "n": len(pos),
                "w": {b: round(float(v["w"]), 6) for b, v in sorted(pos.items())},
            })
    # 开仓后把再平衡锚点钉到「开仓日 + R」，使首次再平衡恰好距建仓 R 天（真正周频 from entry），
    # 而非落到全局栅格点 first_rebal（可能仅距建仓 1~6 天，造成「周频变 3 天」的错觉）。
    # 空仓则不钉 —— 保持每日检查开仓。
    # ⚠️ 增量续算模式（init_pos 存在）不在此处重设：锚点已由调用方传入，避免覆盖（见上方说明）。
    if init_pos is None and len(pos) > 0:
        next_rebal_i = start_i + R
    for i in range(start_i + 1, end_i + 1):
        flat = (len(pos) == 0)
        # 空仓时每日都视为再平衡日检查开仓（避免固定周期之间空等）；
        # 持仓时仅在动态锚定的 next_rebal_i 复核。非再平衡日 book 只缩不扩（红线）。
        rebal_day = flat or (i >= next_rebal_i)
        fee = 0.0
        fired = []
        if rebal_day:
            target = _sanitize(plugin.decide(i, ctx), ctx, i, plugin)
            if _turnover(pos, target) > 1e-9:
                fee = _turnover(pos, target) * FEE
                if trace:
                    keys = set(list(pos.keys()) + list(target.keys()))
                    tr_log.append({
                        "day": ctx.dates[i],
                        "kind": "REBAL",
                        "delta": {b: round(float(target.get(b, 0.0) - pos.get(b, {}).get("w", 0.0)), 6)
                                  for b in sorted(keys)},
                        "turnover": round(float(_turnover(pos, target)), 6),
                        "fee": round(float(fee), 6),
                        "equity_after": round(float(eq[-1] * (1.0 - fee)), 2),
                    })
                newpos = {}
                for b, w in target.items():
                    if b in pos:
                        newpos[b] = {**pos[b], "w": w}
                    else:
                        c = ctx.close(b)[i]
                        newpos[b] = {"w": w, "entry": c, "peak": c}
                pos = newpos
            if len(pos) > 0:
                next_rebal_i = i + R  # 持仓中：锚定下一再平衡日 = 今日 + R
        # ── 共享兜底闸 + 日收益（每个交易日执行；非再平衡日 book 只缩不扩）──
        daily = 0.0
        for b in list(pos.keys()):
            c = ctx.close(b)[i]
            ci = ctx.close(b)[i - 1]
            if not np.isfinite(c) or not np.isfinite(ci) or ci <= 0:
                daily -= pos[b]["w"]
                if trace:
                    fired.append({"base": b, "reason": "NAN", "w": round(float(pos[b]["w"]), 6),
                                  "entry": round(float(pos[b]["entry"]), 6)})
                del pos[b]
                continue
            if c > pos[b]["peak"]:
                pos[b]["peak"] = c
            dt = plugin.delist_thr
            if dt and c < pos[b]["entry"] * (1 - dt):
                daily -= pos[b]["w"]
                if trace:
                    fired.append({"base": b, "reason": "DELIST", "w": round(float(pos[b]["w"]), 6),
                                  "entry": round(float(pos[b]["entry"]), 6)})
                del pos[b]
                continue
            hs = plugin.hard_stop
            ts = plugin.trail_stop
            if (hs and c < pos[b]["entry"] * (1 - hs)) or (ts and c < pos[b]["peak"] * (1 - ts)):
                if trace:
                    fired.append({"base": b, "reason": "STOP", "w": round(float(pos[b]["w"]), 6),
                                  "entry": round(float(pos[b]["entry"]), 6)})
                del pos[b]
                continue
            daily += pos[b]["w"] * (c / ci - 1.0)
        new_eq = eq[-1] * (1.0 + daily) * (1.0 - fee)
        if trace and fired:
            tr_log.append({
                "day": ctx.dates[i],
                "kind": "STOP",
                "fired": fired,
                "equity_after": round(float(new_eq), 2),
            })
        eq.append(new_eq)
        if trace:
            hold_log.append({
                "day": ctx.dates[i],
                "n": len(pos),
                "w": {b: round(float(v["w"]), 6) for b, v in sorted(pos.items())},
            })
    out = np.array(eq, np.float64)
    if trace:
        # final_pos 透出末态持仓（含 entry/peak），供增量续算 / 盘中实时判定承接
        return out, {"trades": tr_log, "holdings": hold_log,
                     "final_pos": {b: dict(v) for b, v in pos.items()}}
    return np.array(eq, np.float64)


def parity_diff(eq_engine, eq_backtest):
    """引擎净值 vs 回测净值 的最大相对偏差（同源校验）。"""
    a = np.asarray(eq_engine, float)
    b = np.asarray(eq_backtest, float)
    m = min(len(a), len(b))
    a, b = a[:m], b[:m]
    if m == 0:
        return 0.0
    denom = np.maximum(np.abs(b), 1e-9)
    return float(np.max(np.abs(a - b) / denom))
