#!/usr/bin/env bash
# 打包成 zip。参数直接透传给 package.py：
#   ./package.sh                 只打包代码
#   ./package.sh --with-data     带数据库快照和脱敏配置
#   ./package.sh --with-secrets  连密钥一起打（谨慎）
set -euo pipefail
cd "$(dirname "$0")"

# 有 conda 环境就用它，没有就用系统 python3——打包只用标准库，两边都能跑
for base in "${CONDA_BASE:-}" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "/opt/conda"; do
  if [ -n "$base" ] && [ -x "$base/envs/${ENV_NAME:-stockwatch}/bin/python" ]; then
    exec "$base/envs/${ENV_NAME:-stockwatch}/bin/python" package.py "$@"
  fi
done
exec python3 package.py "$@"
