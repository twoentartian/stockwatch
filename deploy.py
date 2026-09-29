"""把当前代码部署到群晖。

    ./deploy.sh 192.168.1.20 admin          # 密码会提示输入（不回显）
    ./deploy.sh                             # IP 和用户名用上次记住的
    ./deploy.sh --dry-run                   # 只打包、列出要做的事，不连 NAS

流程：打包当前工作区的代码 → 连 NAS（首次核对主机指纹）→ 把 NAS 上的数据库
备份一份到本地 backups/ → 上传解压 → 重新构建并启动容器 → 等服务就绪。

**密码怎么处理的：**

* 只用于两件事：SSH 登录、以及 NAS 上 `sudo docker` 的提权（群晖上管理员
  用户默认不在 docker 组里，不提权跑不了 docker）。
* sudo 的密码**从 SSH 通道的标准输入喂进去**，不拼进命令行。拼进命令行的话
  NAS 上 `ps` 能看到、可能进日志。也不申请伪终端，否则密码会被回显到输出里。
* 不写进任何文件。`.deploy.json` 只记 IP、用户名、路径和端口。
* `--password` 参数也支持，但它会留在 shell 历史里；推荐直接回车让脚本提示，
  或者用环境变量 SYNO_PASSWORD。长期用的话，给 NAS 配 SSH 公钥更好。

**不会碰的东西：** NAS 上的 `data/`（数据库和 config.json）。部署包里本来就
没有它们。`docker-compose.yml` 如果 NAS 上已经有了也不覆盖——那里面的
PUID/PGID、时区是按那台机器配的；确实要覆盖加 `--overwrite-compose`。
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

import package

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, ".deploy.json")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
KEEP_BACKUPS = 5
KNOWN_HOSTS = os.path.expanduser("~/.ssh/known_hosts")

DEFAULTS = {"path": "/volume1/docker/stockwatch", "ssh_port": 22, "web_port": 8765}
HEALTH_TIMEOUT_S = 180
COMPOSE = "docker-compose.yml"


def step(n: int, total: int, text: str) -> None:
    print(f"\n[{n}/{total}] {text}", flush=True)


# --------------------------------------------------------------------------- 参数

def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            return {**DEFAULTS, **json.load(fh)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(cfg: dict) -> None:
    """只存连接信息。**密码永远不落盘。**"""
    keep = {k: cfg[k] for k in ("host", "user", "path", "ssh_port", "web_port") if cfg.get(k)}
    with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="把当前代码部署到群晖 NAS")
    ap.add_argument("host", nargs="?", help="NAS 的 IP 或主机名（不填就用上次的）")
    ap.add_argument("user", nargs="?", help="群晖用户名，需要是管理员（不填就用上次的）")
    ap.add_argument("--password", help="密码。不推荐：会留在 shell 历史里。不填会提示输入")
    ap.add_argument("--path", help=f"NAS 上的项目目录（默认 {DEFAULTS['path']}）")
    ap.add_argument("--ssh-port", type=int, help="SSH 端口（默认 22）")
    ap.add_argument("--web-port", type=int, help="StockWatch 的网页端口（默认 8765）")
    ap.add_argument("--overwrite-compose", action="store_true",
                    help="NAS 上已有 docker-compose.yml 时也用本地的覆盖")
    ap.add_argument("--no-backup", action="store_true", help="跳过部署前的数据库备份")
    ap.add_argument("--yes", action="store_true", help="首次连接时不询问、直接信任主机指纹")
    ap.add_argument("--dry-run", action="store_true", help="只打包并列出要做的事，不连 NAS")
    return ap.parse_args()


def resolve(args: argparse.Namespace) -> dict:
    saved = load_settings()
    cfg = {
        "host": args.host or saved.get("host"),
        "user": args.user or saved.get("user"),
        "path": (args.path or saved["path"]).rstrip("/"),
        "ssh_port": args.ssh_port or saved["ssh_port"],
        "web_port": args.web_port or saved["web_port"],
    }
    if not cfg["host"]:
        cfg["host"] = input("NAS 的 IP：").strip()
    if not cfg["user"]:
        cfg["user"] = input("群晖用户名：").strip()
    if not cfg["host"] or not cfg["user"]:
        raise SystemExit("✗ 需要 IP 和用户名")
    return cfg


# --------------------------------------------------------------------------- 打包

def version_info() -> str:
    """部署的是哪个版本：commit + 工作区是否有未提交的改动。"""
    def git(*a):
        try:
            return subprocess.run(["git", *a], cwd=BASE_DIR, capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    commit = git("rev-parse", "--short", "HEAD") or "未知"
    dirty = " + 未提交的改动" if git("status", "--porcelain") else ""
    return f"{commit}{dirty}"


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """统一权限和属主，不照搬本地的。

    /mnt/d 下的文件在 WSL 里一律是 777，照搬到 NAS 上就是任何用户都能改。
    而 entrypoint.sh 每次容器启动都以 root 执行——可写就等于谁都能拿 root。
    """
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if info.isdir() or info.name.endswith(".sh"):
        info.mode = 0o755
    else:
        info.mode = 0o644
    return info


def build_tarball(skip_compose: bool, version: str) -> tuple[bytes, list[str]]:
    """把当前工作区的代码打成 tar.gz（内存里）。用 tar 不用 zip：保留可执行位，
    群晖上也一定有 tar。"""
    files = [f for f in package.code_files() if not (skip_compose and f == COMPOSE)]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in files:
            tar.add(os.path.join(BASE_DIR, rel), arcname=rel.replace(os.sep, "/"),
                    filter=_normalize)
        # 在 NAS 上留个记号：现在跑的到底是哪个版本
        stamp = f"{version}\n部署于 {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode("utf-8")
        info = tarfile.TarInfo(".deployed")
        info.size, info.mtime = len(stamp), int(time.time())
        tar.addfile(_normalize(info), io.BytesIO(stamp))
    return buf.getvalue(), files


# --------------------------------------------------------------------------- SSH

def fingerprint(key) -> str:
    """和 ssh-keygen -lf 一样的 SHA256 指纹，方便到群晖上核对。"""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def make_policy(assume_yes: bool):
    import paramiko

    class ConfirmNewHost(paramiko.MissingHostKeyPolicy):
        """第一次连接这台 NAS：把指纹给人看，确认了才连，并记进 known_hosts。

        不能像很多脚本那样用 AutoAddPolicy 无脑信任——接下来要把管理员密码
        发过去，万一连到的是冒充的机器，密码就直接送出去了。
        指纹**变了**的情况 paramiko 会直接报 BadHostKeyException，不走这里。
        """
        def missing_host_key(self, client, hostname, key):
            print(f"  首次连接 {hostname}，主机指纹：{key.get_name()} {fingerprint(key)}")
            print("  （可以在群晖上运行 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` 核对）")
            if not assume_yes:
                answer = input("  确认这是你的 NAS？[y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    raise SystemExit("✗ 已取消")
            client.get_host_keys().add(hostname, key.get_name(), key)
            os.makedirs(os.path.dirname(KNOWN_HOSTS), mode=0o700, exist_ok=True)
            client.save_host_keys(KNOWN_HOSTS)
            print(f"  已记入 {KNOWN_HOSTS}，下次不再询问")

    return ConfirmNewHost()


def connect(cfg: dict, password: str, assume_yes: bool):
    import paramiko

    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS):
        client.load_host_keys(KNOWN_HOSTS)
    client.set_missing_host_key_policy(make_policy(assume_yes))
    try:
        client.connect(cfg["host"], port=cfg["ssh_port"], username=cfg["user"],
                       password=password, timeout=10, auth_timeout=15,
                       look_for_keys=True, allow_agent=True)
    except paramiko.BadHostKeyException:
        raise SystemExit(
            f"✗ {cfg['host']} 的主机指纹和上次记录的不一样！\n"
            f"  如果你刚重装过 DSM，这是正常的，删掉 {KNOWN_HOSTS} 里对应那一行再试；\n"
            f"  否则可能有人在冒充你的 NAS，不要继续。")
    except paramiko.AuthenticationException:
        raise SystemExit("✗ 用户名或密码不对")
    except (OSError, paramiko.SSHException) as exc:
        raise SystemExit(f"✗ 连不上 {cfg['host']}:{cfg['ssh_port']}（{exc}）\n"
                         f"  确认群晖「控制面板 → 终端机和 SNMP」里开了 SSH")
    return client


def run(client, command: str, stdin_data: bytes | None = None) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 输出)。输出不实时打印，适合短命令。"""
    chan = client.get_transport().open_session()
    chan.set_combine_stderr(True)
    chan.exec_command(command)
    if stdin_data is not None:
        chan.sendall(stdin_data)
    chan.shutdown_write()
    out = bytearray()
    while True:
        data = chan.recv(65536)
        if not data:
            break
        out += data
    return chan.recv_exit_status(), out.decode("utf-8", "replace")


def run_sudo(client, script: str, password: str) -> int:
    """以 root 跑一段 shell，输出实时打印（构建镜像要好几分钟，得看得到进度）。

    密码走标准输入，不进命令行；不申请伪终端（有伪终端的话密码会被回显进输出）。
    -p '' 去掉 sudo 自己的提示语，免得混在构建输出里。
    """
    # 群晖的 docker 在 /usr/local/bin，sudo 的 secure_path 里不一定有
    full = "export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:$PATH; " + script
    command = f"sudo -S -p '' sh -c {shlex.quote(full)}"
    chan = client.get_transport().open_session()
    chan.set_combine_stderr(True)
    chan.exec_command(command)
    chan.sendall((password + "\n").encode("utf-8"))
    chan.shutdown_write()      # 密码错了 sudo 再读就是 EOF，直接失败，而不是卡住等输入
    tail = ""
    while True:
        data = chan.recv(4096)
        if not data:
            break
        text = data.decode("utf-8", "replace")
        tail = (tail + text)[-2000:]
        sys.stdout.write(text)
        sys.stdout.flush()
    code = chan.recv_exit_status()
    if code != 0 and ("incorrect password" in tail or "Sorry, try again" in tail):
        print("\n✗ sudo 密码不对（群晖上 sudo 用的就是登录密码）")
    elif code != 0 and "not in the sudoers" in tail:
        print("\n✗ 这个用户没有 sudo 权限，需要用管理员账号")
    return code


# --------------------------------------------------------------------------- 备份与探活

def backup_remote_db(cfg: dict) -> str | None:
    """部署前把 NAS 上的库拉一份到本地。新版本启动时可能跑数据库迁移，万一出问题能回退。

    走程序自己的导出接口而不是 SSH 拷文件：直接拷开着 WAL 的库会拿到不一致的快照，
    导出接口用的是 sqlite3 在线备份。服务没在跑（比如第一次部署）就跳过。
    """
    url = f"http://{cfg['host']}:{cfg['web_port']}/api/data/export.db"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            blob = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"  服务没有响应（{exc}），跳过备份——第一次部署的话这是正常的")
        return None
    if not blob.startswith(b"SQLite format 3"):
        print("  返回的不是数据库文件，跳过备份")
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"nas-{cfg['host']}-{time.strftime('%Y%m%d-%H%M%S')}.db")
    with open(path, "wb") as fh:
        fh.write(blob)
    olds = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(f"nas-{cfg['host']}-"))
    for name in olds[:-KEEP_BACKUPS]:
        os.remove(os.path.join(BACKUP_DIR, name))
    print(f"  已备份 {len(blob) / 1e6:.1f} MB → {os.path.relpath(path, BASE_DIR)}（只留最近 {KEEP_BACKUPS} 份）")
    return path


def wait_healthy(cfg: dict) -> bool:
    url = f"http://{cfg['host']}:{cfg['web_port']}/api/state"
    deadline = time.time() + HEALTH_TIMEOUT_S
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    return False


# --------------------------------------------------------------------------- 主流程

BUILD_SCRIPT = (
    'cd {path} && '
    # DSM 7.2 的 Container Manager 自带 compose v2；更老的只有 docker-compose
    'if docker compose version >/dev/null 2>&1; then docker compose up -d --build; '
    'else docker-compose up -d --build; fi'
)


def main() -> int:
    args = parse_args()
    cfg = resolve(args)
    version = version_info()
    path_q = shlex.quote(cfg["path"])
    total = 6

    print(f"部署 StockWatch {version} → {cfg['user']}@{cfg['host']}:{cfg['path']}")

    if args.dry_run:
        step(1, 1, "打包（dry run，不连接 NAS）")
        blob, files = build_tarball(skip_compose=not args.overwrite_compose, version=version)
        print(f"  {len(files)} 个文件，{len(blob) / 1024:.0f} KB")
        for f in files:
            print(f"    {f}")
        print("\n  将要在 NAS 上执行：")
        print(f"    mkdir -p {path_q} && tar -xzf - -C {path_q}")
        print(f"    sudo -S -p '' sh -c '… {BUILD_SCRIPT.format(path=path_q)}'   （密码走标准输入）")
        if not args.overwrite_compose:
            print(f"  （NAS 上已有 {COMPOSE} 时不会覆盖；这里按「已有」列的文件清单）")
        return 0

    password = args.password or os.environ.get("SYNO_PASSWORD") or \
        getpass.getpass(f"{cfg['user']}@{cfg['host']} 的密码：")
    if not password:
        raise SystemExit("✗ 需要密码")

    step(1, total, "连接 NAS")
    client = connect(cfg, password, args.yes)
    print("  已连接")
    save_settings(cfg)   # 连上了才记住，免得把输错的 IP 存下来

    try:
        code, out = run(client, f"mkdir -p {path_q} && test -f {path_q}/{COMPOSE} && echo has || echo none")
        if code != 0:
            raise SystemExit(f"✗ 没法在 NAS 上创建 {cfg['path']}：{out.strip()}")
        has_compose = out.strip().endswith("has")

        step(2, total, "打包当前代码")
        skip_compose = has_compose and not args.overwrite_compose
        blob, files = build_tarball(skip_compose=skip_compose, version=version)
        print(f"  {len(files)} 个文件，{len(blob) / 1024:.0f} KB")
        if skip_compose:
            print(f"  NAS 上已有 {COMPOSE}，保留不动（要覆盖加 --overwrite-compose）")

        step(3, total, "备份 NAS 上的数据库到本地")
        if args.no_backup:
            print("  已跳过（--no-backup）")
        else:
            backup_remote_db(cfg)

        step(4, total, "上传并解压")
        code, out = run(client, f"tar -xzf - -C {path_q}", stdin_data=blob)
        if code != 0:
            raise SystemExit(f"✗ 解压失败：{out.strip()}")
        print(f"  已更新 {cfg['path']}（data/ 没动）")

        step(5, total, "重新构建镜像并重启容器（第一次会比较久）")
        code = run_sudo(client, BUILD_SCRIPT.format(path=path_q), password)
        if code != 0:
            print(f"\n✗ 构建或启动失败（退出码 {code}），容器最近的日志：")
            run_sudo(client, "docker logs --tail 40 stockwatch 2>&1 || true", password)
            return 1
    finally:
        client.close()

    step(6, total, "等服务就绪")
    if wait_healthy(cfg):
        print(f"\n✓ 部署完成：http://{cfg['host']}:{cfg['web_port']}/  （版本 {version}）")
        return 0
    print(f"\n✗ {HEALTH_TIMEOUT_S} 秒内服务没有响应。到 Container Manager 里看容器日志，"
          f"或者 SSH 上去运行：sudo docker logs --tail 50 stockwatch")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
