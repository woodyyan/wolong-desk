# -*- coding: utf-8 -*-
"""MA120 趋势闸门 · 判定与撮合的唯一真源。

回测脚本（research/btc_ma120_signals.py）与 paper 引擎（ops/engine.py）
必须 import 本模块，禁止各自实现。改这里 = 同时改两边。

铁律（见 卧龙台/.workbuddy/memory/MEMORY.md）：
- 无前视：决策只用 i-1 日及之前的信息，成交发生在第 i 日收盘。
- 净值循环里任何中间赋值必须用 *= 累乘，用 = 会静默吞掉前面乘子。
- 纯多头：pos ∈ {0,1}，无 −1。无止损/无目标价，只看 MA120 穿越。
- CAGR 须除以窗口起点权益（防 68.6% 虚高 bug）。
"""
import numpy as np

WINDOW = 120
FEE = 0.001          # 换仓费 0.1%
WARMUP = WINDOW      # 需要 120 根 K 线才有第一个 MA120


def sma(a, w):
    """简单移动平均。前 w-1 个为 nan。O(n) 滚动实现。"""
    a = np.asarray(a, float)
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


def ma120_of(closes):
    """返回 MA120 序列（前 119 个为 nan）。"""
    return sma(closes, WINDOW)


def target_position(closes, i):
    """第 i 日的目标仓位（无前视）。

    只用 i-1 日及之前的信息决策，成交发生在第 i 日收盘。
    返回 1（满仓）/ 0（空仓）；MA120 未热时返回 0。
    """
    if i < 1:
        return 0
    ma = ma120_of(closes)[i - 1]          # ← 关键：用 i-1 日的 MA120
    if ma is None or not np.isfinite(ma):
        return 0                          # 预热期空仓
    return 1 if closes[i - 1] >= ma else 0


def step(equity_prev, pos_prev, closes, i):
    """推进一个交易日。返回 (equity_new, pos_new, traded: bool)。

    ⚠️ 红线：所有乘子必须 *= 累乘，禁止 = 覆盖。
    """
    tgt = target_position(closes, i)
    eq = equity_prev
    traded = False
    if tgt != pos_prev:
        eq *= (1.0 - FEE)                  # 换仓费（累乘！）
        traded = True
    if tgt == 1:
        eq *= (closes[i] / closes[i - 1])  # 持仓收益（累乘！）
    return eq, tgt, traded


def run_equity(closes):
    """返回完整净值序列（与回测同口径）。

    第 0 日 equity=1；第 i 日按 step() 推进。
    返回 np.array 长度=len(closes)，第 0 日=1。
    """
    closes = np.asarray(closes, float)
    n = len(closes)
    eq = np.ones(n)
    pos = 0.0
    for i in range(1, n):
        eq[i], pos, _ = step(eq[i - 1], pos, closes, i)
    return eq


def metrics(eq_warm):
    """对「已对齐到预热起点的净值序列」算 CAGR/MDD/Sharpe。

    eq_warm: 从 WARMUP 日起的净值（可为原始 equity 切片，比例不变）。
    与 research/btc_ma120_signals.py 的 metrics() 完全一致。
    """
    eq_warm = np.asarray(eq_warm, float)
    n = len(eq_warm)
    if n < 2:
        return 0.0, 0.0, 0.0, 0.0
    yrs = n / 365.0
    fin = eq_warm[-1]
    cagr = fin ** (1 / yrs) - 1
    runmax = np.maximum.accumulate(eq_warm)
    mdd = float((eq_warm / runmax - 1).min())
    rt = eq_warm[1:] / eq_warm[:-1] - 1
    sd = rt.std()
    sh = rt.mean() / sd * np.sqrt(365) if sd > 0 else 0.0
    return float(fin), float(cagr), float(mdd), float(sh)
