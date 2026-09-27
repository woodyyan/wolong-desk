# -*- coding: utf-8 -*-
"""BTC 主策略插件 · MA120 日线趋势闸门（纯多头）。

判定函数直接 import engine.ma120_signal.target_position —— 与回测（research/btc_ma120_signals.py）
及现有单策略交易台（ops/engine.py）同源，禁止各自实现。改 engine/ma120_signal.py = 同时改两边。
"""
import os
import sys
import importlib.util
import numpy as np
from engine.strategy_plugin import StrategyPlugin, register

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    "ma120_signal_shared", os.path.join(ROOT, "engine", "ma120_signal.py")
)
_MS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_MS)


@register
class BtcMa120(StrategyPlugin):
    name = "BTC主策略"
    multi_asset = False
    rebal = 1
    max_single = None
    min_coins = 1
    delist_thr = None
    hard_stop = None
    trail_stop = None

    def decide(self, i, ctx):
        if not ctx.has("BTC"):
            return {}
        closes = ctx.close("BTC")
        pos = _MS.target_position(closes, i)
        return {"BTC": 1.0} if pos == 1 else {}
