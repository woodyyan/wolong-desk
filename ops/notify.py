# -*- coding: utf-8 -*-
"""统一通知中枢 · 可插拔多通道（Telegram / Server酱 / 企业微信）。

成本：个人用量全部免费。
配置：ops/notify_config.json（含密钥，勿入库）。缺省 / 未启用通道 → 静默 no-op，绝不影响主流程。

v2.0（2026-09-16）：引入 source 路由 + 通用 notify() 入口，成为跨项目统一接入规范。
  此前为交易台专属；现任意项目均可一行式接入，不耦合交易字典结构。
  当前主通道：微信(Server酱) + Telegram；iMessage / Bark 等按需再加通道函数即可。

schema:
{
  "channels": {
    "telegram":  {"enabled": true,  "token": "...", "chat_id": "..."},
    "serverchan":{"enabled": true,  "sendkey": "..."},
    "wecom":     {"enabled": false, "corpid": "...", "corpsecret": "...", "agentid": "...", "touser": "@all"}
  }
}

──────── 接入规范（跨项目统一）────────
1) 接入方式：将本文件置于各项目 ops/ 目录（或 import 路径可达），共用同一份代码与配置。
2) 调用入口：notify(source, level, title, lines[, channels=None])
     source  : 项目/来源名（如 "交易台" / "网站日报" / "组合看板"），用于区分与日志
     level   : "info" | "alert" | "report"
               - info/report：常规汇总/状态，不冷却
               - alert：异常告警，建议改用 send_alert(kind, title, lines)（带 1h 冷却去重）
     title   : 中文标题
     lines   : 中文正文行（list）
     channels: 可选，显式限定通道（如 ["telegram","serverchan"]）；不传则发往全部已启用通道
   返回 [(cid, ok, err)]，失败静默，绝不影响主流程。
3) 通道启用：在 notify_config.json 把对应通道 enabled 置 true 并填全必填项即可，无需改代码。
4) 命名约定：source 用项目中文短名；多项目共用同一 chat 时靠 source 前缀区分来源。
5) 新增通道：在 CHANNELS 注册元信息 + 在 _SENDERS 加发送函数，调用方无感。
"""
import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "notify_config.json")

# 通道元信息（前端弹窗据此动态渲染，单一事实来源）
CHANNELS = {
    "telegram": {
        "label": "Telegram",
        "desc": "Bot API · 免费。需 bot token + chat_id（@BotFather 建 bot，给 @userinfobot 发消息取 chat_id）。",
        "fields": [
            {"key": "token", "label": "Bot Token", "placeholder": "123456:ABC-DEF...", "secret": True},
            {"key": "chat_id", "label": "Chat ID", "placeholder": "123456789", "secret": False},
        ],
        "required": ["token", "chat_id"],
    },
    "serverchan": {
        "label": "Server酱（微信）",
        "desc": "微信直接收消息 · 免费。需 SendKey（sctapi.ftqq.com 获取）。",
        "fields": [
            {"key": "sendkey", "label": "SendKey", "placeholder": "SCTxxxxx", "secret": True},
        ],
        "required": ["sendkey"],
    },
    "wecom": {
        "label": "企业微信",
        "desc": "企业微信应用消息 · 免费。需 corpid + corpsecret + agentid（接收人默认 @all）。",
        "fields": [
            {"key": "corpid", "label": "Corp ID", "placeholder": "wwxxxx", "secret": False},
            {"key": "corpsecret", "label": "Corp Secret", "placeholder": "xxxx", "secret": True},
            {"key": "agentid", "label": "Agent ID", "placeholder": "1000002", "secret": False},
            {"key": "touser", "label": "接收人 (默认 @all)", "placeholder": "@all", "secret": False},
        ],
        "required": ["corpid", "corpsecret", "agentid"],
    },
}


def _load_cfg():
    if not os.path.exists(CFG):
        return None
    try:
        c = json.load(open(CFG, encoding="utf-8"))
        if not isinstance(c, dict) or not c.get("channels"):
            return None
        return c
    except Exception:
        return None


def _post_json(url, payload=None, timeout=10):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _post_form(url, data, timeout=10):
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _fmt_pct(v, d=2):
    return "—" if v is None else ("%+.2f%%" % (v * 100))


def _fmt_num(v, d=2):
    return "—" if v is None else ("{:,.2f}".format(float(v)))


def _perf_block(ctx):
    """绩效表现段：让成交通知带上「这笔操作发生在什么状态下」。

    只有价格/方向的裸信号无法判断该不该跟，附上 NAV、区间、CAGR/MDD/Sharpe、
    基准对比与当前持仓，才能一眼看出这次调仓是在顺风还是逆风里做的。
    """
    ctx = ctx or {}
    lines = []
    nav = ctx.get("nav")
    if nav is not None:
        lines.append("净值 NAV：%.4f（累计 %+.2f%%）" % (float(nav), (float(nav) - 1.0) * 100))
    sd, ao = ctx.get("start_day"), ctx.get("as_of")
    if sd:
        n = ctx.get("n_days")
        run = ("，已运行 %d 个交易日" % int(n)) if n else ""
        lines.append("区间：%s → %s%s" % (sd, ao or "—", run))
    cagr, mdd, sh = ctx.get("cagr"), ctx.get("mdd"), ctx.get("sharpe")
    if any(x is not None for x in (cagr, mdd, sh)):
        tail = ""
        try:
            # 样本太短时年化是外推出来的，极易误导（3 天 -0.24% 会年化成 -22.8%）
            if int(ctx.get("n_days") or 0) < 30:
                tail = "（样本不足 30 日，年化仅供参考）"
        except Exception:
            pass
        lines.append("年化 %s ｜ 最大回撤 %s ｜ Sharpe %s%s" % (
            _fmt_pct(cagr), _fmt_pct(mdd), ("%.2f" % float(sh)) if sh is not None else "—", tail))
    bh = ctx.get("bh_nav")
    if bh is not None:
        lines.append("同期基准（买入持有）：%s" % _fmt_pct(float(bh) - 1.0))
    if ctx.get("turnover"):
        lines.append("累计换手：%d 次" % int(ctx["turnover"]))
    hold = ctx.get("holdings") or {}
    if hold:
        try:
            top = sorted(hold.items(), key=lambda kv: -float(kv[1]))[:3]
            lines.append("当前持仓：%s" % "、".join("%s %.1f%%" % (k, float(v) * 100) for k, v in top))
        except Exception:
            pass
    return lines


def _format(trade, ctx):
    ctx = ctx or {}
    side = "买入 BUY 🟢" if trade["side"] == "BUY" else "卖出 SELL 🔴"
    pnl = trade.get("pnl")
    pnl_txt = "—" if pnl is None else ("+%.2f%%" % (pnl * 100))
    eq = trade.get("equity_after")
    pos = "持仓 LONG" if ctx.get("pos") == 1 else "空仓 CASH"
    head = "%s · 成交通知" % (ctx.get("strategy_name") or "交易台")
    body = [head, "────────", side]
    if trade.get("symbol"):
        body.append("标的：%s" % trade["symbol"])
    body += [
        "日期：%s" % trade.get("date"),
        "价格：%s USDT" % _fmt_num(trade.get("price")),
        "回合收益：%s" % pnl_txt,
        "后权益：%s USDT" % _fmt_num(eq),
        "当前仓位：%s" % pos,
    ]
    perf = _perf_block(ctx)
    if perf:
        body += ["────────", "📊 当前表现"] + perf
    return "\n".join(body)


def _send_telegram(ch, text):
    token = ch.get("token")
    chat_id = ch.get("chat_id")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    _post_json(url, {"chat_id": chat_id, "text": text})
    return True


def _send_serverchan(ch, text):
    key = ch.get("sendkey")
    if not key:
        return False
    url = f"https://sctapi.ftqq.com/{key}.send"
    _post_form(url, {"title": "BTC 交易台信号", "desp": text})
    return True


def _send_wecom(ch, text):
    corpid = ch.get("corpid")
    secret = ch.get("corpsecret")
    agentid = ch.get("agentid")
    touser = ch.get("touser", "@all")
    if not (corpid and secret and agentid):
        return False
    tk_url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
    tok = json.loads(_post_json(tk_url) or "{}").get("access_token")
    if not tok:
        return False
    send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={tok}"
    _post_json(send_url, {
        "touser": touser, "msgtype": "text",
        "agentid": int(agentid), "text": {"content": text},
    })
    return True


_SENDERS = {"telegram": _send_telegram, "serverchan": _send_serverchan, "wecom": _send_wecom}


def _enabled_channels(cfg):
    """返回 [(cid, ch)]：已启用且必填项齐全的通道。"""
    out = []
    for cid, meta in CHANNELS.items():
        ch = (((cfg or {}).get("channels", {}) or {}).get(cid, {}) if cfg else {})
        if not ch.get("enabled"):
            continue
        if not all(ch.get(k) for k in meta["required"]):
            continue
        out.append((cid, ch))
    return out


def send_one(trade, ctx):
    """发送单笔成交通知到所有「已启用且已配全」的通道。失败静默，不影响主流程。"""
    cfg = _load_cfg()
    if not cfg:
        return []
    results = []
    for cid, ch in _enabled_channels(cfg):
        try:
            _SENDERS[cid](ch, _format(trade, ctx))
            results.append((cid, True, None))
        except Exception as e:
            results.append((cid, False, str(e)))
    return results


def candidates(trades, today, last_notified_day):
    """返回「今天」且晚于 last_notified_day 的待通知交易（去重，避免重发）。"""
    cfg = _load_cfg()
    if not cfg:
        return []
    out = []
    for t in trades or []:
        if t.get("date") == today and (last_notified_day is None or t["date"] > last_notified_day):
            out.append(t)
    return out


def get_config():
    """返回当前配置 + 通道 schema，供前端弹窗渲染与回填。"""
    cfg = _load_cfg()
    config = {}
    for cid, meta in CHANNELS.items():
        ch = (((cfg or {}).get("channels", {}) or {}).get(cid, {}) if cfg else {})
        entry = {"enabled": bool(ch.get("enabled"))}
        for f in meta["fields"]:
            entry[f["key"]] = ch.get(f["key"], "") or ""
        config[cid] = entry
    return {"config": config, "schema": {
        cid: {"label": meta["label"], "desc": meta["desc"], "fields": meta["fields"], "required": meta["required"]}
        for cid, meta in CHANNELS.items()
    }}


def get_status():
    """各通道启用/配置状态（精简，供状态展示）。"""
    cfg = _load_cfg()
    out = {}
    for cid, meta in CHANNELS.items():
        ch = (((cfg or {}).get("channels", {}) or {}).get(cid, {}) if cfg else {})
        out[cid] = {
            "enabled": bool(ch.get("enabled")),
            "configured": all(ch.get(k) for k in meta["required"]),
            "label": meta["label"],
        }
    return out


def save_config(body):
    """写入配置。body = {"channels": {cid: {enabled, field...}}}。只保留已知通道与字段。"""
    src_channels = ((body or {}).get("channels", {}) if isinstance(body, dict) else {})
    clean = {"channels": {}}
    for cid, meta in CHANNELS.items():
        src = src_channels.get(cid, {})
        ch = {"enabled": bool(src.get("enabled"))}
        for f in meta["fields"]:
            ch[f["key"]] = src.get(f["key"], "") or ""
        clean["channels"][cid] = ch
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return {"ok": True, "status": get_status()}


# 告警冷却：同类告警在窗口内只发一次，避免每次刷新/轮询都轰炸。
# 落盘保存 —— 服务重启后仍然有效。
ALERT_COOLDOWN_SEC = 3600
_DEDUP = os.path.join(HERE, "state", "_alert_dedup.json")


def _load_dedup():
    try:
        return json.load(open(_DEDUP, encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_dedup(d):
    try:
        os.makedirs(os.path.dirname(_DEDUP), exist_ok=True)
        json.dump(d, open(_DEDUP, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


def send_alert(kind, title, lines, cooldown=ALERT_COOLDOWN_SEC, force=False):
    """运行状态异常告警（中文），走与成交通知相同的通道。

    kind     : 告警类别（用于冷却去重，如 "data_stale" / "parity" / "engine_error"）
    title    : 中文标题
    lines    : 中文正文行（list）
    cooldown : 同类告警冷却秒数，默认 1 小时
    未配置通知通道时静默返回 []，绝不影响主流程。
    """
    cfg = _load_cfg()
    if not cfg:
        return []
    ded = _load_dedup()
    now = time.time()
    last = float(ded.get(kind, 0) or 0)
    if not force and (now - last) < cooldown:
        return [{"channel": "-", "ok": False, "error": "冷却中（%s 内已告警过），本次跳过" % kind}]
    ded[kind] = now
    _save_dedup(ded)
    text = "⚠️ 交易台异常告警 · %s\n────────\n%s" % (title, "\n".join(lines or []))
    if not lines:
        text = "⚠️ 交易台异常告警 · %s" % title
    results = []
    for cid, ch in _enabled_channels(cfg):
        try:
            _SENDERS[cid](ch, text)
            results.append({"channel": cid, "ok": True})
        except Exception as e:
            results.append({"channel": cid, "ok": False, "error": str(e)})
    return results


def send_test():
    """通道自检：向所有「已启用且已配全」的通道发测试消息。"""
    cfg = _load_cfg()
    if not cfg:
        return {"ok": False, "reason": "no config (未配置 notify_config.json)"}
    results = []
    for cid, ch in _enabled_channels(cfg):
        text = "🔔 BTC 交易台通知测试 · 通道正常（此消息由「测试推送」触发）"
        try:
            _SENDERS[cid](ch, text)
            results.append({"channel": cid, "ok": True})
        except Exception as e:
            results.append({"channel": cid, "ok": False, "error": str(e)})
    return {"ok": any(r["ok"] for r in results), "results": results}


def send_report(title, lines):
    """每日运行报告（中文），走与成交通知相同的通道，不带告警前缀/冷却。

    适用于「每天推送一次站点流量/运行状态」这类常规汇总（区别于 send_alert 的异常告警）。
    title : 中文标题（如 "yangxiaa.cc 站点日报 · 2026-09-15"）
    lines : 中文正文行（list），按显示顺序拼接
    无配置通道时静默返回 []，绝不影响主流程。
    """
    cfg = _load_cfg()
    if not cfg:
        return []
    body = "\n".join(lines or [])
    text = ("%s\n────────\n%s" % (title, body)) if body else title
    results = []
    for cid, ch in _enabled_channels(cfg):
        try:
            _SENDERS[cid](ch, text)
            results.append({"channel": cid, "ok": True})
        except Exception as e:
            results.append({"channel": cid, "ok": False, "error": str(e)})
    return results


def notify(source, level, title, lines, channels=None):
    """统一通知入口（渠道无关），任意项目一行式接入；跨项目统一规范。

    source  : 项目/来源名（如 "交易台" / "网站日报" / "组合看板"），用于区分与日志
    level   : "info" | "alert" | "report"
              - info/report：常规汇总/状态，不冷却
              - alert：异常告警，建议改用 send_alert(kind, title, lines)（带 1h 冷却去重）
    title   : 中文标题
    lines   : 中文正文行（list）
    channels: 可选，显式限定通道（如 ["telegram","serverchan"]）；不传则发往全部已启用通道
    失败静默，绝不影响主流程。返回 [(cid, ok, err)]。
    """
    cfg = _load_cfg()
    if not cfg:
        return []
    body = "\n".join(lines or [])
    text = ("%s · %s\n────────\n%s" % (source, title, body)) if body else ("%s · %s" % (source, title))
    targets = _enabled_channels(cfg)
    if channels:
        targets = [(cid, ch) for (cid, ch) in targets if cid in channels]
    results = []
    for cid, ch in targets:
        try:
            _SENDERS[cid](ch, text)
            results.append((cid, True, None))
        except Exception as e:
            results.append((cid, False, str(e)))
    return results
