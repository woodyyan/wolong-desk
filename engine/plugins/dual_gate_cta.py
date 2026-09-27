# -*- coding: utf-8 -*-
"""双闸多币 CTA 插件 · 决策同源 research/phase7_wf.compute_target（B+T 闸门 on）。

判定函数直接 import research.phase7_wf.compute_target —— 与回测(phase7_wf.run)完全相同，
禁止各自实现。改 research/phase7_wf.py = 同时改回测与交易台（红线：判定与回测同源）。

执行（换仓费/退市代理/硬止损/移动止盈/单币限仓）由统一内核 simulate_equity 完成，
其机制与 phase7.run() 逐行一致（NaN→全损、delist代理、硬/移动止损、w·(c/ci−1) 日收益）。

周频再平衡 R=7（持仓期间），与回测同源的信号选择；空仓时由内核改为每日检查开仓
（见 engine/strategy_plugin.simulate_equity 的再平衡栅说明）。
"""
import os
import sys
import importlib.util
import numpy as np
from engine.strategy_plugin import StrategyPlugin, register

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    "phase7_wf_shared", os.path.join(ROOT, "research", "phase7_wf.py")
)
_P7 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_P7)
sys.modules.setdefault("phase7_wf_shared", _P7)  # 单例：multi_engine 复用同一份

# 落地配置：与 phase7 固定参数对照组一致（commit B+T，闸门 on）
FIXED_CFG = {"sig": "B+T", "max_w": 0.10, "min_coins": 3, "gate": True}
R = _P7.R


@register
class DualGateCta(StrategyPlugin):
    name = "双闸CTA"
    multi_asset = True
    rebal = R
    max_single = 0.10
    min_coins = 3
    delist_thr = 0.95
    hard_stop = 0.10
    trail_stop = 0.30
    sig = "B+T"
    gate = True

    def prepare(self, ctx):
        # 调度（空仓日检 / 持仓周期复核）已交给内核 simulate_equity 统一处理，
        # 插件只做信号选择，保持与回测 compute_target 同源。
        pass

    def decide(self, i, ctx):
        # 只做信号选择：返回第 i 日的目标权重（{base: w}）。
        # 周期调度（何时真正换仓）由内核负责，此处不再自设周期闸。
        tgt = _P7.compute_target(FIXED_CFG, i)  # {ci(列索引): w}
        return {_P7.BASES[ci]: w for ci, w in tgt.items()}
