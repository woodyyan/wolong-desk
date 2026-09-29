#!/usr/bin/env bash
# ============================================================
#  卧龙 BTC 交易台 · 启动脚本（Linux / macOS 通用）
#  用法：
#    ./start.sh            # 前台运行（systemd / nohup 调用）
#    PYTHON=python3.11 ./start.sh
#    DESK_HOST=0.0.0.0 ./start.sh     # 直接对外暴露（不建议裸奔，走 nginx 更佳）
#  停止：Ctrl+C，或 kill 占用 8790 的进程
# ============================================================
set -e

# 脚本所在目录（无论何处调用都正确）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Python 解释器：默认 python3；可用环境变量覆盖
PYTHON="${PYTHON:-python3}"

echo "[start] 使用 Python: $($PYTHON --version 2>&1)"
echo "[start] 工作目录: $SCRIPT_DIR"

# 依赖自检：numpy 必须存在
if ! "$PYTHON" -c "import numpy" 2>/dev/null; then
  echo "[start] 错误：未检测到 numpy。请先执行： $PYTHON -m pip install -r requirements.txt" >&2
  exit 1
fi

# 若 8790 已被占用，只终止「本项目自己的旧实例」。
# 不再无条件 kill —— 否则 nginx / 其它服务占用 8790 时会被误杀。
PORT_PIDS=""
if command -v lsof >/dev/null 2>&1; then
  PORT_PIDS="$(lsof -ti :8790 2>/dev/null || true)"
elif command -v fuser >/dev/null 2>&1; then
  PORT_PIDS="$(fuser -n tcp 8790 2>/dev/null | tr -c '0-9' '\n' | grep -E '^[0-9]+$' || true)"
fi

for _pid in $PORT_PIDS; do
  # 取该进程的完整命令行（Linux procps 用 args=，BSD/macOS 兜底 command=）
  _cmd="$(ps -p "$_pid" -o args= 2>/dev/null || true)"
  if [ -z "$_cmd" ]; then
    _cmd="$(ps -p "$_pid" -o command= 2>/dev/null || true)"
  fi
  case "$_cmd" in
    *serve.py*)
      echo "[start] 端口 8790 被本项目旧实例占用（PID $_pid），先终止它"
      kill "$_pid" 2>/dev/null || true
      ;;
    *)
      echo "[start] 错误：端口 8790 被非本项目进程占用（PID $_pid → ${_cmd:-命令行不可读}）" >&2
      echo "[start] 为安全起见不自动终止。请先确认：lsof -nP -iTCP:8790 -sTCP:LISTEN" >&2
      echo "[start] 确认可清理后手动 kill，或改用其它端口再启动。" >&2
      exit 1
      ;;
  esac
done

if [ -n "$PORT_PIDS" ]; then
  sleep 1
fi

# 绑定地址：默认 127.0.0.1（仅本机/反代可达）；设 DESK_HOST=0.0.0.0 才对外
export DESK_HOST="${DESK_HOST:-127.0.0.1}"

echo "[start] 启动交易台 → http://${DESK_HOST}:8790  (DESK_HOST 可改)"
echo "[start] 预热中（首次装载 CTA 430 币约需 20~30s），日志见 stdout"

exec "$PYTHON" ops/serve.py
