#!/usr/bin/env bash
# 部署到群晖。参数直接透传给 deploy.py：
#   ./deploy.sh 192.168.1.20 admin      密码会提示输入
#   ./deploy.sh                         用上次记住的 IP 和用户名
#   ./deploy.sh --dry-run               只打包、列出要做的事
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for base in "${CONDA_BASE:-}" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "/opt/conda"; do
  if [ -n "$base" ] && [ -x "$base/envs/${ENV_NAME:-stockwatch}/bin/python" ]; then
    PY="$base/envs/${ENV_NAME:-stockwatch}/bin/python"; break
  fi
done
PY="${PY:-python3}"

# paramiko 只有部署要用，不进运行时依赖；缺了就装
if ! "$PY" -c "import paramiko" 2>/dev/null; then
  echo "安装部署依赖（paramiko）…"
  "$PY" -m pip install -q -r requirements-deploy.txt
fi
exec "$PY" deploy.py "$@"
