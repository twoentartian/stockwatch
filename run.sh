#!/usr/bin/env bash
# StockWatch 启动脚本
#
#   ./run.sh                前台跑（Ctrl+C 停止），适合在终端里调试
#   ./run.sh --bg           后台跑，日志写 logs/stockwatch.log，脚本立刻返回
#   ./run.sh --restart      先停再启（改了模板或代码之后必须用，Flask 会缓存模板）
#   ./run.sh --restart --bg 后台重启
#   ./run.sh --stop         停止
#   ./run.sh --status       看它在不在跑
#   ./run.sh --log          跟着看日志（Ctrl+C 退出，不影响服务）
#
# 可用环境变量：CONDA_BASE / ENV_NAME / STOCKWATCH_PORT
set -euo pipefail

cd "$(dirname "$0")"
PORT="${STOCKWATCH_PORT:-8765}"
ENV_NAME="${ENV_NAME:-stockwatch}"
URL="http://127.0.0.1:${PORT}"
HERE="$PWD"
LOG="logs/stockwatch.log"
MAX_LOG_BYTES=5242880   # 5MB 就轮转一次，别让它无限长

die() { echo "✗ $*" >&2; exit 1; }

# 只认**本目录**启动的那个进程。以前用 pkill -f 吃过亏：模式串本身出现在
# 命令行里，会把执行命令的 shell 自己也杀掉；换个目录还可能误杀别的项目。
our_pids() {
  local pid
  for pid in $(pgrep -f "app[.]py" 2>/dev/null || true); do
    if [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)" = "$HERE" ]; then
      echo "$pid"
    fi
  done
}

# 查本机有没有人在监听这个端口。
#
# 不要用 bash 的 /dev/tcp 去试连——**那个写法没有超时**。WSL 的 localhost 转发一旦
# 留着上一个实例的残留，SYN 出去没人应答，bash 就一直等到 TCP 自己超时（约两分钟），
# 期间脚本一行输出都不打，看起来就是卡死了。ss 只查本地监听表，不发包，毫秒级返回。
port_busy() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q LISTEN
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -q ":$PORT "
  else
    return 1   # 查不了就别挡着启动，端口真被占 Flask 自己会报错
  fi
}

stop_running() {
  local pids
  pids="$(our_pids)"
  [ -z "$pids" ] && { echo "没有正在运行的实例"; return 0; }
  echo "停止旧实例：$pids"
  kill $pids 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -z "$(our_pids)" ] && { echo "已停止"; return 0; }
    sleep 0.5
  done
  echo "还没退出，强制结束"; kill -9 $(our_pids) 2>/dev/null || true
}

# ---------------------------------------------------------------- 参数
MODE="fg"; DO_STOP=0
for arg in "$@"; do
  case "$arg" in
    --bg)      MODE="bg" ;;
    --restart) DO_STOP=1 ;;
    --stop)    stop_running; exit 0 ;;
    --status)
      pids="$(our_pids | tr '\n' ' ')"
      if [ -n "$pids" ]; then echo "运行中（PID $pids）→ $URL"; else echo "没在跑"; fi
      exit 0 ;;
    --log)
      [ -f "$LOG" ] || die "还没有日志文件（$HERE/$LOG）"
      exec tail -n 100 -f "$LOG" ;;
    *) die "不认识的参数：$arg（可用 --bg / --restart / --stop / --status / --log）" ;;
  esac
done

[ "$DO_STOP" = 1 ] && stop_running

if [ "$DO_STOP" = 0 ] && [ -n "$(our_pids)" ]; then
  echo "StockWatch 已经在运行（PID $(our_pids | tr '\n' ' ')）→ $URL"
  echo "想重启用：./run.sh --restart --bg"
  exit 0
fi
# 刚 kill 掉的进程，监听 socket 会再挂一小会儿才释放——停完立刻检查会误判成
# "端口被别人占着"。所以这里等几秒，而不是一看到占用就退出。
for _ in 1 2 3 4 5 6 7 8 9 10; do
  port_busy || break
  sleep 0.5
done
port_busy && die "端口 $PORT 被别的程序占着。换一个：STOCKWATCH_PORT=8766 ./run.sh"

# ---------------------------------------------------------------- conda 环境
CONDA_SH=""
for base in "${CONDA_BASE:-}" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "/opt/conda"; do
  [ -n "$base" ] && [ -f "$base/etc/profile.d/conda.sh" ] && { CONDA_SH="$base/etc/profile.d/conda.sh"; break; }
done
echo "准备环境…"
[ -n "$CONDA_SH" ] || die "找不到 conda。装好后重试，或指定：CONDA_BASE=/path/to/conda ./run.sh"
# shellcheck disable=SC1090
source "$CONDA_SH"

if ! conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "环境 $ENV_NAME 不存在，正在创建（第一次会慢几分钟）…"
  conda create -y -n "$ENV_NAME" python=3.11
fi
conda activate "$ENV_NAME"

# 依赖缺了就补。只在缺的时候装，正常启动不会每次都跑 pip
if ! python -c "import flask, requests, apscheduler, reportlab" 2>/dev/null; then
  echo "安装依赖…"
  pip install -q -r requirements.txt
fi

export STOCKWATCH_PORT="$PORT"

# ---------------------------------------------------------------- 启动
if [ "$MODE" = "fg" ]; then
  echo "StockWatch 启动中 → $URL   （Ctrl+C 停止）"
  exec python app.py
fi

mkdir -p logs
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt "$MAX_LOG_BYTES" ]; then
  mv -f "$LOG" "$LOG.1"      # 只留一代旧日志，够查最近的问题了
fi
echo "=== $(date '+%F %T') 启动 ===" >> "$LOG"

# setsid 让它脱离当前会话：这样关掉启动它的那个窗口（WSL 窗口、终端）服务照样活着。
# stdin 接 /dev/null，否则后台进程读到终端会被 SIGTTIN 挂起。
setsid nohup python app.py >> "$LOG" 2>&1 < /dev/null &

for _ in $(seq 1 60); do
  port_busy && break
  sleep 1
done

if port_busy; then
  echo "StockWatch 已在后台启动（PID $(our_pids | tr '\n' ' ')）→ $URL"
  echo "日志：$HERE/$LOG"
  echo "停止：./run.sh --stop    看日志：./run.sh --log"
else
  echo "✗ 启动失败，日志最后几行："
  tail -n 20 "$LOG" || true
  exit 1
fi
