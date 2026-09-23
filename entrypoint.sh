#!/bin/sh
# 容器入口：把数据目录交给运行用户，再降权启动。
#
# 为什么要这一步：挂载进来的目录属于宿主机上的某个用户（群晖上是你的账号，
# Docker 自动建的是 root），跟镜像里的用户对不上就写不进去，SQLite 报
# "unable to open database file"。这里以 root 身份先把属主改对，再用 gosu 降权，
# 进程本身始终不以 root 跑。
#
# PUID / PGID 决定数据文件在宿主机上归谁。群晖一般是 1026 / 100，
# 普通 Linux 桌面一般是 1000 / 1000。填错也能跑，只是文件属主不是你。
set -e

PUID="${PUID:-10001}"
PGID="${PGID:-10001}"
DATA_DIR="$(dirname "${STOCKWATCH_DB:-/data/stockwatch.db}")"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    if ! chown -R "$PUID:$PGID" "$DATA_DIR" 2>/dev/null; then
        echo "警告：无法修改 $DATA_DIR 的属主（只读挂载？），继续尝试启动" >&2
    fi
    # 光改属主不够。群晖共享文件夹走的是它自己的 ACL，Linux 权限位常常是全空的
    # （d---------+）；你在 File Station 里的权限多半来自 administrators 组，
    # 而降权后的进程只带 PGID 这一个组，ACL 判下来就是不可写。
    # 所以把属主的读写位显式设上，不再依赖 ACL。
    chmod -R u+rwX "$DATA_DIR" 2>/dev/null || true

    # 以降权后的身份真的试一次，写不进去就现在说清楚，别等 SQLite 报一句含糊的错
    if ! gosu "$PUID:$PGID" sh -c 'test -w "$1"' _ "$DATA_DIR"; then
        echo "错误：以 uid=$PUID gid=$PGID 仍然无法写入 $DATA_DIR" >&2
        echo "  目录状态：$(ls -ld "$DATA_DIR" 2>/dev/null)" >&2
        echo "  群晖上请在宿主机执行：sudo chmod -R u+rwX <你的 data 目录>" >&2
        echo "  （或 控制面板 → 任务计划 → 以 root 运行上面这条命令）" >&2
        exit 1
    fi
    exec gosu "$PUID:$PGID" "$@"
fi

# 走到这里说明 compose 里用 user: 指定了非 root 用户——没权限改属主，只能检查
if [ ! -w "$DATA_DIR" ]; then
    echo "错误：数据目录 $DATA_DIR 对当前用户（uid=$(id -u) gid=$(id -g)）不可写。" >&2
    echo "  目录属主：$(stat -c '%u:%g' "$DATA_DIR" 2>/dev/null || echo 未知)" >&2
    echo "  解决办法：把 compose 里的 user: 这一行删掉，改用 PUID / PGID 环境变量，" >&2
    echo "  容器会自己把目录权限改好。" >&2
    exit 1
fi
exec "$@"
