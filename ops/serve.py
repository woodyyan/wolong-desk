# -*- coding: utf-8 -*-
"""L3 交易台 UI · HTTP 服务 :8790。

端点：
    /                → templates/desk.html
    /api/desk        → 聚合四面板数据（health/quote/perf/trades/equity/price365）
    /api/refresh      POST → 拉行情 → 撮合 → 落盘
    /api/backfill     POST → 回填同源闸门（parity_max_diff 须=0）
    /vendor/echarts.min.js → 本地 vendored

零新依赖：只用标准库。
"""
import os
import sys
import threading
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TPL = os.path.join(HERE, "templates", "desk.html")
VENDOR = os.path.join(ROOT, "vendor", "echarts.min.js")
PORT = 8790
# 盘中实时判定轮询间隔（秒）：每 3 分钟用实时最新成交价复核各持仓币的兜底闸，
# 触发即盘中平仓（模拟实盘 OCO / 跟踪止损）。部署在直连环境（新加坡/香港）才真正执行。
LIVE_CHECK_SEC = 180


def _version():
    """交易台版本号（config.json），改代码须升版本：行为+minor / bug+patch。"""
    try:
        return str(json.load(open(os.path.join(ROOT, "config.json"))).get("version", "?"))
    except Exception:
        return "?"

import strategy_hub as H  # 策略中心：多策略 = 交易台的「策略维度」，不是第二个窗口

# ⚠️ ops/engine.py 与 ROOT/engine 包同名 —— 绝不可 `import engine`：
# 它会把 "engine" 占成模块，令 strategy_hub 的 `from engine.strategy_plugin import`
# 报 "engine is not a package"。需要遗留引擎时按绝对路径以别名加载。
_LEGACY = None


def _legacy():
    """按绝对路径加载 ops/engine.py（别名为 legacy_btc_engine，不占用包名）。"""
    global _LEGACY
    if _LEGACY is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "legacy_btc_engine", os.path.join(HERE, "engine.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _LEGACY = m
    return _LEGACY


def _int(v):
    """query string → int 或 None（解析失败返回 None）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _zh(e):
    """把内部异常翻成中文提示（保留 detail 原文便于排查）。

    背景：曾把 KeyError: '融合80/20' 原样抛给前端，师傅只看到一串带引号的
    英文、完全看不懂。这里统一给出中文原因 + 可操作建议。
    """
    raw = str(e)
    if isinstance(e, H.UnknownStrategy):
        return {"error": raw, "code": "unknown_strategy",
                "hint": "请在顶部策略条里选择策略后重试"}
    if isinstance(e, KeyError):
        return {"error": "内部数据缺少字段：%s" % raw.strip("'\""), "code": "missing_field",
                "detail": raw, "hint": "多为策略标识传错或状态文件版本不符，可先「重置」该策略"}
    if isinstance(e, FileNotFoundError):
        return {"error": "找不到数据文件：%s" % (getattr(e, "filename", None) or raw),
                "code": "file_missing", "detail": raw, "hint": "先点「刷新行情」重新拉取数据"}
    if isinstance(e, json.JSONDecodeError):
        return {"error": "状态文件损坏（JSON 解析失败）：%s" % raw, "code": "state_corrupt",
                "detail": raw, "hint": "点「重置」该策略可重建账本"}
    if isinstance(e, PermissionError):
        return {"error": "没有权限写入状态文件：%s" % raw, "code": "permission", "detail": raw}
    if isinstance(e, TimeoutError):
        return {"error": "处理超时，请稍后重试", "code": "timeout", "detail": raw}
    if isinstance(e, ValueError):
        return {"error": "参数不合法：%s" % raw, "code": "bad_param", "detail": raw}
    return {"error": "后台处理出错：%s" % raw, "code": type(e).__name__, "detail": raw}


def do_refresh(slug=None):
    """拉行情 → 重算回测(同源闸门) → 推进 paper 账户，返回撮合+推进摘要。

    slug 指定非 BTC 策略时走 strategy_hub（每个策略一份独立账本）。

    ⚠️ 关键修复（v1.4.0）：feed 更新的是【磁盘】kline，而 strategy_hub 对行情做了
    进程级缓存 `_CTX_CACHE`。不清缓存的话，advance_paper 看到的还是服务启动时缓存的
    旧 kline（如 09-07），会判定 `as_of >= dates[-1]`「已最新」而【跳过推进】——
    这正是「点刷新按钮曲线仍空白」的根因。故 feed 之后必须让缓存失效。
    此外刷新会【一次性推进所有 running 策略】，一次点击即可让全部曲线增长。

    v1.5.0 起：刷新链路在 BTC(feed.py) 之后追加【多币行情(feed_multi.py)】。
    dual_gate_cta 依赖 data/kline_<BASE>.json，只刷 BTC 会让多币数据滞后、
    候选池塌缩到只剩 BTC、永远选不出 ≥3 币而空仓——这正是「空仓不买」的另一半根因。
    feed_multi 增量追加末日之后的交易日（绝不覆盖历史），并复用 feed 的代理探测，
    本机经 Clash、服务器直连均可拉取。

    v1.5.11 起：① feed_multi 改为并行抓取（见 feed_multi.py），更快且降低 600s 超时风险；
    ② 多币抓取失败不再静默吞掉（显式记录+告警）；③ 推进后加自检兜底——核对账本 as_of
    是否追上数据源末日，落后则重试一次 advance，仍落后则告警，杜绝「数据新、账本旧」静默滞留。
    """
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "feed.py")],
                       capture_output=True, timeout=60)
    except Exception as e:
        return dict(_zh(e), ok=False, stage="feed")
    # 多币行情同样需刷新：dual_gate_cta/fusion 依赖 data/kline_<BASE>.json，只刷 BTC 会让
    # 多币数据滞后 → 候选池塌缩 → 空仓。feed_multi 增量+代理感知（v1.5.11 起并行抓取，更快），
    # 失败【不再静默吞掉】——显式记录并告警，避免「数据半截没刷完却误以为成功」。
    feed_multi_ok = True
    feed_multi_err = None
    try:
        r_multi = subprocess.run([sys.executable, os.path.join(HERE, "feed_multi.py")],
                                 capture_output=True, timeout=600)
        if r_multi.returncode != 0:
            feed_multi_ok = False
            feed_multi_err = (r_multi.stderr or b"").decode("utf-8", "ignore")[-600:]
    except subprocess.TimeoutExpired:
        feed_multi_ok = False
        feed_multi_err = "feed_multi 超过 600s 被中断，多币行情可能只刷了部分币（并行抓取应已大幅降低此风险）"
    except Exception as e:
        feed_multi_ok = False
        feed_multi_err = str(e)
    # 关键修复：feed 已更新磁盘 kline，必须让进程内行情缓存失效，否则 advance 看不到新交易日
    H._CTX_CACHE.clear()
    # 推进【所有 running 策略】（不只是当前 tab），一次刷新全活
    s = slug or H.DEFAULT_SLUG
    advances = []
    try:
        for meta in H.list_strategies():
            if meta.get("status") != "running":
                continue
            try:
                r = H.advance_paper(meta["slug"])
            except Exception as e:
                r = dict(_zh(e), ok=False)
            advances.append({"slug": meta["slug"], "result": r})
            # 推进失败属于「运行异常」——此前完全静默，现在主动告警
            if isinstance(r, dict) and r.get("ok") is False:
                try:
                    import notify
                    notify.send_alert(
                        "engine_error_" + meta["slug"],
                        "%s 账本推进失败" % H.NAME.get(meta["slug"], meta["slug"]),
                        [r.get("error") or "推进过程中出错。",
                         "建议：查看服务日志定位，或先「重置」该策略再启动。"])
                except Exception:
                    pass
    except Exception as e:
        return dict(_zh(e), ok=False, stage="advance")
    # ── 推进后自检兜底（v1.5.11）──
    # 此前出现过「数据源已到 9.22、但账本 as_of 仍卡 9.20」：根因是 advance 跑在 feed 写完之前。
    # 这里强制核对：账本末日是否 == 数据源末日（advance 用的同源 ctx.dates[-1]）；
    # 落后则自动重试一次 advance，仍落后则告警。数据新鲜度只在 do_refresh 内变化，
    # 故此不变量在每次刷新末都能闭合，杜绝「数据新、账本旧」静默滞留。
    lag_alerts = []
    for meta in H.list_strategies():
        if meta.get("status") != "running":
            continue
        slug_i = meta["slug"]
        try:
            data_last = H.ctx_for(slug_i).dates[-1]
        except Exception:
            continue
        p = H.load_paper(slug_i) or {}
        if (p.get("as_of") or "") < data_last:
            try:
                H.advance_paper(slug_i)
                p = H.load_paper(slug_i) or {}
            except Exception:
                pass
        if (p.get("as_of") or "") < data_last:
            lag_alerts.append(slug_i)
            try:
                import notify
                notify.send_alert(
                    "advance_lag_" + slug_i,
                    "%s 账本推进后仍落后数据源" % H.NAME.get(slug_i, slug_i),
                    ["账本 as_of=%s，数据源末日=%s。" % (p.get("as_of"), data_last),
                     "已自动重试一次 advance 仍未追上，请检查多币行情是否完整（feed_multi 是否有 fail），或手动再点一次刷新。"])
            except Exception:
                pass
    # 多币抓取本身失败也要告警
    if not feed_multi_ok:
        try:
            import notify
            notify.send_alert(
                "feed_multi_failed", "多币行情刷新失败/超时",
                [feed_multi_err or "未知原因",
                 "dual_gate_cta / fusion 的候选池与账本可能滞后，请重试刷新或检查代理/网络。"])
        except Exception:
            pass
    # 刷新后顺带巡检（数据停更/同源漂移/心跳超时等，按类冷却，不会轰炸）
    hc = None
    try:
        hc = H.healthcheck(s)
    except Exception as e:
        hc = dict(_zh(e), ok=False)
    return {"ok": True, "strategy": s, "advances": advances,
            "feed_multi_ok": feed_multi_ok, "advance_lag": lag_alerts, "health": hc}


def do_backfill():
    """回填同源闸门（BTC：run_equity 自比较，应 = 0）。"""
    return _legacy().parity_check()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _qs(self):
        from urllib.parse import urlparse, parse_qs
        out = {}
        for k, v in parse_qs(urlparse(self.path).query).items():
            out[k] = v[0]
        return out

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/desk"):
                html = open(TPL, encoding="utf-8").read()
                html = html.replace("__VERSION__", "v" + _version())
                self._send(200, html, "text/html")
            elif path == "/api/desk":
                # 统一走策略中心：交易台只有一个窗，BTC 也只是「策略之一」
                slug = (self._qs().get("strategy") or H.DEFAULT_SLUG)
                self._send(200, H.desk_payload(slug))
            elif path == "/api/strategies":
                self._send(200, H.list_strategies())
            elif path == "/api/backfill":
                self._send(200, do_backfill())
            elif path == "/api/quote":
                # 同样走策略中心：否则 30s 轮询会用已迁移的旧 state/ 覆盖面板（口径回退）
                slug = (self._qs().get("strategy") or H.DEFAULT_SLUG)
                d = H.desk_payload(slug)
                self._send(200, {"strategy": slug, "multi_asset": d.get("multi_asset"),
                                 "quote": d["quote"], "price365": d["price365"]})
            elif path == "/api/health":
                self._send(200, H.healthcheck(self._qs().get("strategy")))
            elif path == "/api/notify-config":
                try:
                    import notify
                    self._send(200, notify.get_config())
                except Exception as e:
                    self._send(200, _zh(e))
            elif path == "/vendor/echarts.min.js":
                data = open(VENDOR, "rb").read()
                self._send(200, data, "application/javascript")
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, _zh(e))

    def _slug(self):
        """从 query/body 取 strategy slug；空则走默认 BTC 路径（向后兼容）。"""
        s = self._qs().get("strategy") or ""
        if not s:
            s = (self._body() or {}).get("strategy") or ""
        return s or None

    def do_POST(self):
        path = self.path.split("?")[0]
        slug = self._slug()
        if path == "/api/refresh":
            self._send(200, do_refresh(slug))
        elif path == "/api/backfill":
            self._send(200, do_backfill())
        elif path == "/api/start":
            # 启动 = 按所选回溯窗口（重建）paper 账户；已存在则覆盖重建（保证下拉窗口生效）
            q = self._qs()
            bd = _int(q.get("days"))
            self._send(200, H.init_paper(slug or H.DEFAULT_SLUG, reset=True, back_days=bd))
        elif path == "/api/stop":
            self._send(200, H.set_paper_status(slug or H.DEFAULT_SLUG, "paused"))
        elif path == "/api/reset":
            # 重置 = 归零到纯净初始态（今日/100k/nav≈1/指标0），不承接回溯窗口
            self._send(200, H.reset_paper(slug or H.DEFAULT_SLUG))
        elif path == "/api/live-check":
            # 手动触发一次盘中实时判定（后台线程每 3 分钟也会自动跑）
            self._send(200, H.check_live_stops(slug or H.DEFAULT_SLUG))
        elif path == "/api/daily-report":
            # 手动触发一次运行日报（方便测试；不影响每晚21:00自动推送）
            try:
                sys.path.insert(0, HERE)
                import desk_daily_report as _dr
                title, lines = _dr.build_report()
                try:
                    from notify import send_report
                    res = send_report(title, lines)
                except Exception as e:
                    res = [{"ok": False, "error": str(e)}]
                self._send(200, {"ok": True, "result": res,
                                "title": title, "preview_lines": lines})
            except Exception as e:
                self._send(200, dict(_zh(e), ok=False))
        elif path == "/api/notify-test":
            # 通知通道自检（需先配 ops/notify_config.json）
            try:
                import notify
                self._send(200, notify.send_test())
            except Exception as e:
                self._send(200, dict(_zh(e), ok=False))
        elif path == "/api/notify-config":
            # 保存通知配置（多通道，可独立启用/关闭）
            body = self._body()
            try:
                import notify
                self._send(200, notify.save_config(body))
            except Exception as e:
                self._send(200, dict(_zh(e), ok=False))
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass  # 静默访问日志


def main():
    # 绑定地址：默认 127.0.0.1（仅本机/反向代理可达，最安全）。
    # 若服务器需直接对外暴露，启动时设环境变量 DESK_HOST=0.0.0.0
    # （建议仍走 nginx 反代 + 鉴权，不要裸奔 0.0.0.0）。
    HOST = os.environ.get("DESK_HOST", "127.0.0.1")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)

    # 后台预热：双闸 CTA 首次请求需装载 430 币全宇宙（~20s），
    # 提前在后台跑掉，避免师傅第一次切到 CTA 标签时干等。
    def _warmup():
        try:
            import strategy_hub as _H
            _H.desk_payload("btc_ma120")      # 触发实时价探测（失败则进冷却）
            _H.desk_payload("dual_gate_cta")  # 430 币全宇宙（最重）
            sys.stderr.write("[warmup] BTC + CTA 已就绪\n")
        except Exception as e:
            sys.stderr.write("[warmup] 跳过：%s\n" % e)
    threading.Thread(target=_warmup, daemon=True).start()

    # 后台盘中实时判定：每 LIVE_CHECK_SEC 秒对各 running 非融合策略复核实时价兜底闸。
    # 后台线程挂了也不影响主服务（daemon + try 包裹）。
    def _live_monitor():
        try:
            import strategy_hub as _H
            import time as _t
            while True:
                _t.sleep(LIVE_CHECK_SEC)
                try:
                    for meta in _H.list_strategies():
                        if meta.get("status") != "running":
                            continue
                        if _H.STRATS.get(meta["slug"], {}).get("composite"):
                            continue
                        _H.check_live_stops(meta["slug"])
                except Exception as e:
                    sys.stderr.write("[live-monitor] %s\n" % e)
        except Exception:
            pass
    threading.Thread(target=_live_monitor, daemon=True).start()

    # 后台每日运行日报：每晚 21:00（server 本地时区，松柏=UTC+8=北京时）生成并推送，
    # 按日去重（持久标记到 state/daily_report_sent.json），服务重启不会重复发。
    # 失败不影响主服务（daemon + try 包裹）；手动可在前端/API 触发 /api/daily-report。
    def _daily_report_monitor():
        try:
            import datetime as _dt
            _sent_path = os.path.join(HERE, "state", "daily_report_sent.json")

            def _last_sent():
                try:
                    return json.load(open(_sent_path, encoding="utf-8")).get("date")
                except Exception:
                    return None

            def _mark_sent():
                try:
                    json.dump({"date": _dt.date.today().isoformat(),
                               "at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                              open(_sent_path, "w", encoding="utf-8"))
                except Exception:
                    pass

            while True:
                _t.sleep(60)
                try:
                    now = _dt.datetime.now()
                    if now.hour == 21 and _last_sent() != now.date().isoformat():
                        sys.path.insert(0, HERE)
                        import desk_daily_report as _dr
                        _dr.build_and_send()
                        _mark_sent()
                except Exception as e:
                    sys.stderr.write("[daily-report] %s\n" % e)
        except Exception:
            pass
    threading.Thread(target=_daily_report_monitor, daemon=True).start()
    print(f"BTC 交易台已启动 → http://127.0.0.1:{PORT}")
    print("Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
        print("\n已停止")


if __name__ == "__main__":
    main()
