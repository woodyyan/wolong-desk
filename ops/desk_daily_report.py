# -*- coding: utf-8 -*-
"""卧龙加密工作台每日运行日报，经 notify.send_report 推送。

内容：
  一、数据接口状态（各策略账本/数据源末日、实时价复核、健康检查）
  二、绩效表现（净值/累计收益/今日/持仓/CAGR·MDD·Sharpe）
  三、近2日成交摘要（止损/换仓）

触发：serve.py 后台线程每晚 21:00（server 本地时区，松柏=UTC+8=北京时）自动调用
      build_and_send()；也可 `python3 ops/desk_daily_report.py` 手动跑，或 POST
      /api/daily-report 触发。仿 ops/cf_daily_report.py 结构（notify v2 通用中枢）。
"""
import os
import sys
import json
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def _fmt_pct(v, d=2):
    return "—" if v is None else ("%+.2f%%" % (v * 100))


def _day_change(series):
    """序列末日 vs 前一交易日的净值变化（今日收益）。"""
    if not series or len(series) < 2:
        return None
    try:
        a, b = series[-2]["nav"], series[-1]["nav"]
        if not a:
            return None
        return b / a - 1
    except Exception:
        return None


def build_report():
    import strategy_hub as H
    now = datetime.datetime.now()
    today_s = now.strftime("%Y-%m-%d")
    lines = []

    # 一、数据接口状态
    lines.append("【一、数据接口状态】")
    hc = None
    try:
        hc = H.healthcheck(notify_on_issue=False)
    except Exception as e:
        hc = {"ok": False, "issues": [{"title": "健康检查失败", "lines": [str(e)[:120]]}]}
    for meta in H.list_strategies():
        slug = meta["slug"]
        name = H.NAME.get(slug, slug)
        try:
            p = H.load_paper(slug) or {}
            asof = p.get("as_of")
            try:
                data_last = H.ctx_for(slug).dates[-1]
            except Exception:
                data_last = "?"
            if isinstance(data_last, str) and asof:
                flag = "✓" if asof <= data_last else "⚠滞后"
            else:
                flag = ""
            llc = p.get("last_live_check")
            llc_s = (llc if llc else "—")
            lines.append("- %s：账本 %s / 数据源 %s %s｜实时复核 %s｜净值 %s" % (
                name, asof, data_last, flag, llc_s,
                _fmt_pct((p.get("nav") or 0) - 1)))
        except Exception as e:
            lines.append("- %s：状态读取失败 %s" % (name, str(e)[:60]))
    if hc and hc.get("ok"):
        lines.append("健康检查：✓ 无异常")
    else:
        issues = hc.get("issues") or []
        titles = "；".join(it.get("title", "") for it in issues) or "未知"
        lines.append("健康检查：⚠ %d 项待处理：%s" % (len(issues), titles))
    lines.append("")

    # 二、绩效表现
    lines.append("【二、绩效表现】")
    for meta in H.list_strategies():
        slug = meta["slug"]
        name = H.NAME.get(slug, slug)
        try:
            p = H.load_paper(slug) or {}
            if not p:
                continue
            nav = p.get("nav")
            series = p.get("series") or []
            dc = _day_change(series)
            cagr = p.get("cagr")
            mdd = p.get("mdd")
            sharpe = p.get("sharpe")
            holdings = p.get("holdings") or {}
            n_h = len(holdings)
            hsym = "/".join(list(holdings.keys())[:12])
            if n_h > 12:
                hsym += " 等%d币" % n_h
            comp = "（融合派生）" if H.STRATS.get(slug, {}).get("composite") else ""
            nav_s = "%.4f" % nav if nav is not None else "—"
            cum_s = _fmt_pct((nav - 1) if nav is not None else None)
            lines.append("%s%s：" % (name, comp))
            lines.append("  净值 %s（累计 %s）｜今日 %s｜CAGR %s｜MDD %s｜Sharpe %s" % (
                nav_s, cum_s, _fmt_pct(dc),
                _fmt_pct(cagr) if cagr is not None else "—",
                _fmt_pct(mdd) if mdd is not None else "—",
                ("%.2f" % sharpe) if sharpe is not None else "—"))
            if holdings:
                lines.append("  持仓 %d：%s" % (n_h, hsym))
        except Exception as e:
            lines.append("%s：读取失败 %s" % (name, str(e)[:60]))
    lines.append("")

    # 三、近2日成交摘要
    lines.append("【三、近2日成交摘要】")
    any_tx = False
    for meta in H.list_strategies():
        slug = meta["slug"]
        name = H.NAME.get(slug, slug)
        try:
            p = H.load_paper(slug) or {}
            ts = p.get("trades") or []
            cutoff = (now - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
            recent = [t for t in ts if (t.get("date") or "") >= cutoff]
            if not recent:
                continue
            any_tx = True
            stops = [t for t in recent if t.get("reason") == "STOP"]
            rebals = [t for t in recent if t.get("reason") == "REBAL"]
            buys = [t for t in recent if t.get("side") == "BUY"]
            sells = [t for t in recent if t.get("side") == "SELL"]
            lines.append("%s：近2日 %d 笔（买%d/卖%d，止损%d/换仓%d）" % (
                name, len(recent), len(buys), len(sells), len(stops), len(rebals)))
            for t in recent[:8]:
                pnl = t.get("pnl")
                pnls = "" if pnl is None else (" 盈亏%s" % _fmt_pct(pnl))
                lines.append("  %s %s %s%s" % (t.get("date"), t.get("side"), t.get("symbol"), pnls))
        except Exception as e:
            lines.append("%s：成交读取失败 %s" % (name, str(e)[:60]))
    if not any_tx:
        lines.append("近2日无成交（仅持有/观察）")
    lines.append("")
    lines.append("（本报告由交易台自动生成 · 每晚21:00 推送）")
    title = "卧龙加密工作台日报 · %s" % today_s
    return title, lines


def build_and_send():
    """生成并推送；返回 send_report 结果（无通道时静默返回 []）。"""
    title, lines = build_report()
    try:
        sys.path.insert(0, HERE)
        from notify import send_report
        return send_report(title, lines)
    except Exception as e:
        sys.stderr.write("[desk-daily-report] 推送失败：%s\n" % e)
        return [{"ok": False, "error": str(e)}]


def main():
    title, lines = build_report()
    try:
        from notify import send_report
        res = send_report(title, lines)
        print("推送：", res)
    except Exception as e:
        print("推送失败：", e)
        print("──── 报告预览 ────")
        print("\n".join([title, "────────"] + lines))


if __name__ == "__main__":
    main()
