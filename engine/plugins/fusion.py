# -*- coding: utf-8 -*-
"""融合元策略 · NAV 级组合（不占独立资金，引擎在基础策略 NAV 上派生）。

定稿方案（见 phase8）：核心 BTC 主策略 80% + 卫星 双闸CTA 20%，月度再平衡视角下同源派生。
这里做"派生视图"而非独立交易账户：融合 NAV = Σ w·基础NAV，与 phase8 combine() 口径一致。
"""
import numpy as np
from engine.strategy_plugin import Composite, register


@register
class Fusion8020(Composite):
    name = "融合80/20"

    def __init__(self, weights=None):
        super().__init__(weights or {"BTC主策略": 0.8, "双闸CTA": 0.2})

    def nav(self, navs):
        out = None
        for name, w in self.weights.items():
            if name not in navs:
                continue
            a = np.asarray(navs[name], float)
            out = a * w if out is None else out + a * w
        if out is None:
            return np.array([1.0])
        return out
