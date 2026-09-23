"""把项目打包成一个 zip。

    python package.py                  只打包代码（默认）
    python package.py --with-data      带上数据库快照和一份脱敏的配置样例
    python package.py --with-secrets   连真实的 config.json 一起打（含密码和 API Key）
    python package.py -o 某个路径.zip  指定输出文件

**默认不打包 config.json**——里面有 SMTP 密码、DeepSeek Key、Tavily Key 的明文。
**默认也不打包数据库**——那是你的交易记录和持仓成本。

数据库不是直接复制文件：开着 WAL 的时候 stockwatch.db 主文件可能落后近一兆
（数据还在 .db-wal 里），照抄会得到一个旧快照。这里用 sqlite3 的在线备份接口，
程序正在运行也能拿到一致的快照。
"""
from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import os
import sqlite3
import sys
import tempfile
import time
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NAME = "stockwatch"

# 目录级排除：整棵子树都不要
SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "dist", "logs", ".pytest_cache", ".ruff_cache"}
# 文件级排除（glob）
SKIP_FILES = [
    "*.pyc", "*.pyo", "*.log", "*.tmp", "*.zip",
    "config.json", "config.json.tmp",          # 有密钥，单独按开关处理
    "*.db", "*.db-wal", "*.db-shm",            # 有数据，单独按开关处理
    ".DS_Store", "_*.py",                      # _chk.py 之类的临时脚本
    "*.db.bak-*",                              # 导入前的自动备份
]

SECRET_FIELDS = (("email", "password"), ("llm", "api_key"), ("websearch", "api_key"))


def wanted(rel: str) -> bool:
    parts = rel.split(os.sep)
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    return not any(fnmatch.fnmatch(parts[-1], pattern) for pattern in SKIP_FILES)


def code_files() -> list[str]:
    out = []
    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), BASE_DIR)
            if wanted(rel):
                out.append(rel)
    return sorted(out)


def db_snapshot(src: str, dst: str) -> None:
    """在线备份：程序跑着也能拿到一致快照（含 WAL 里还没合并的部分）。"""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    with target:
        source.backup(target)
    source.close()
    target.close()


def sanitized_config(path: str) -> str | None:
    """配置样例：结构照旧，密码和 Key 清空。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    cfg = copy.deepcopy(cfg)
    for section, field in SECRET_FIELDS:
        if isinstance(cfg.get(section), dict) and cfg[section].get(field):
            cfg[section][field] = ""
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser(description="把 StockWatch 打包成 zip")
    ap.add_argument("--with-data", action="store_true", help="带上数据库快照和脱敏的配置样例")
    ap.add_argument("--with-secrets", action="store_true",
                    help="连真实 config.json 一起打包（含明文密码和 API Key）")
    ap.add_argument("-o", "--out", help="输出文件名")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M")
    suffix = "-带密钥" if args.with_secrets else ("-带数据" if args.with_data else "")
    out = args.out or os.path.join(BASE_DIR, "dist", f"{NAME}-{stamp}{suffix}.zip")
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)

    files = code_files()
    added = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in files:
            zf.write(os.path.join(BASE_DIR, rel), f"{NAME}/{rel}")
            added.append(rel)

        config_path = os.path.join(BASE_DIR, "config.json")
        if args.with_secrets and os.path.exists(config_path):
            zf.write(config_path, f"{NAME}/config.json")
            added.append("config.json（含明文密钥）")
        elif os.path.exists(config_path):
            # 叫 example 而不是 config.json：解压覆盖安装时不会把人家的配置冲掉
            text = sanitized_config(config_path)
            if text:
                zf.writestr(f"{NAME}/config.example.json", text)
                added.append("config.example.json（已脱敏）")

        db_path = os.path.join(BASE_DIR, f"{NAME}.db")
        if (args.with_data or args.with_secrets) and os.path.exists(db_path):
            with tempfile.TemporaryDirectory() as tmp:
                snap = os.path.join(tmp, "snapshot.db")
                db_snapshot(db_path, snap)
                zf.write(snap, f"{NAME}/{NAME}.db")
                added.append(f"{NAME}.db（一致性快照，{os.path.getsize(snap) / 1e6:.1f} MB）")

    size = os.path.getsize(out) / 1e6
    print(f"✓ {out}")
    print(f"  {len(added)} 个条目，{size:.2f} MB")
    if not (args.with_data or args.with_secrets):
        print("  只有代码：没有数据库、没有配置（要数据加 --with-data）")
    for note in added:
        if "（" in note:
            print(f"  · {note}")
    if args.with_secrets:
        print("\n⚠️  这个包里有 SMTP 密码和 API Key 的明文，别往外发、别传网盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
