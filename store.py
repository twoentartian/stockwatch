"""SQLite 存储：行情历史、告警记录、告警去重状态。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("STOCKWATCH_DB", os.path.join(BASE_DIR, "stockwatch.db"))

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    price       REAL,
    prev_close  REAL,
    open        REAL,
    change_pct  REAL,
    day_high    REAL,
    day_low     REAL,
    volume      REAL,
    currency    TEXT,
    name        TEXT
);
CREATE INDEX IF NOT EXISTS idx_quotes_symbol_ts ON quotes(symbol, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    symbol       TEXT    NOT NULL,
    name         TEXT,
    direction    TEXT,
    change_pct   REAL,
    threshold    REAL,
    price        REAL,
    baseline     TEXT,
    email_status TEXT,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS alert_state (
    key        TEXT PRIMARY KEY,
    last_ts    INTEGER,
    trade_date TEXT,
    change_pct REAL
);

-- 交易流水：买入 / 卖出 / 收到股息。金额一律记欧元（DeGiro 实际进出账的数），
-- 这样换汇成本天然包含在成本里，不需要再去查历史汇率。
CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT    NOT NULL,
    kind           TEXT    NOT NULL,   -- BUY / SELL / DIV
    trade_date     TEXT    NOT NULL,   -- YYYY-MM-DD
    shares         REAL,               -- BUY / SELL
    price          REAL,               -- 每股成交价（成交币种）
    price_currency TEXT,               -- USD / EUR ...
    fee_eur        REAL DEFAULT 0,     -- 手续费（欧元）
    amount_eur     REAL,               -- BUY:实际支出 SELL:实际到手 DIV:到账股息（税后）
    note           TEXT,
    created_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tx_symbol ON transactions(symbol, trade_date);

-- 日线归档：每个代码每天一条，永久保留。
-- quotes 表存的是每轮轮询的瞬时点（会按保留天数清理），这张表是长期存档，不清理。
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol   TEXT NOT NULL,
    date     TEXT NOT NULL,          -- YYYY-MM-DD
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    currency TEXT,
    source   TEXT,                   -- poll（轮询快照）/ history（历史接口）
    PRIMARY KEY (symbol, date)
);

-- 汇率历史，同样永久保留；抓不到汇率时可以回退到最近一条
CREATE TABLE IF NOT EXISTS fx_rates (
    currency     TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    eur_per_unit REAL NOT NULL,
    PRIMARY KEY (currency, ts)
);

-- 账户：区分这笔是谁买的（我 / 妈妈 / …）
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    note       TEXT,
    created_at INTEGER
);

-- LLM 写的财报点评，一个报告期一条，永久保留（生成一次要花钱，别重复生成）
-- period_type: annual（年报）/ quarterly（季报）
-- period_key : 年报是财年 '2025'，季报是期末日 '2026-06-30'
CREATE TABLE IF NOT EXISTS report_summaries (
    symbol            TEXT NOT NULL,
    period_type       TEXT NOT NULL DEFAULT 'annual',
    period_key        TEXT NOT NULL,
    fiscal_year       INTEGER,
    as_of             TEXT,
    summary           TEXT,
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    created_at        INTEGER,
    PRIMARY KEY (symbol, period_type, period_key)
);

-- 基本面数据缓存，年报一年才变几次，没必要每次打开页面都去抓
CREATE TABLE IF NOT EXISTS fundamentals_cache (
    symbol  TEXT PRIMARY KEY,
    ts      INTEGER,
    payload TEXT
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:  # 放在 /mnt/d 之类的挂载盘上时 WAL 可能不可用，退回默认日志模式
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    return conn


DEFAULT_ACCOUNT = "我"


def _migrate_report_summaries(conn) -> None:
    """老表的主键是 (symbol, fiscal_year)，容不下同一年的四个季度。

    SQLite 改不了主键，只能新建一张表把数据搬过去。已有的记录全部当年报处理。
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(report_summaries)")}
    if not columns or "period_type" in columns:
        return
    log_msg = conn.execute("SELECT COUNT(*) FROM report_summaries").fetchone()[0]
    conn.execute("ALTER TABLE report_summaries RENAME TO report_summaries_old")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO report_summaries (symbol, period_type, period_key, fiscal_year,"
        " as_of, summary, model, prompt_tokens, completion_tokens, created_at)"
        " SELECT symbol, 'annual', CAST(fiscal_year AS TEXT), fiscal_year,"
        " as_of, summary, model, prompt_tokens, completion_tokens, created_at"
        " FROM report_summaries_old"
    )
    conn.execute("DROP TABLE report_summaries_old")
    print(f"[store] report_summaries 已迁移，{log_msg} 条旧记录标记为年报")


class StorageError(RuntimeError):
    """数据库打不开/写不进。消息里带着排查所需的全部信息。"""


def _access_hint() -> str:
    """SQLite 只会说 "unable to open database file"，这里把真正有用的信息补上：
    文件在哪、目录存不存在、目录归谁、当前进程是谁、到底能不能写。"""
    folder = os.path.dirname(os.path.abspath(DB_PATH)) or "."
    uid = getattr(os, "getuid", lambda: "?")()
    gid = getattr(os, "getgid", lambda: "?")()
    parts = [f"数据库：{DB_PATH}", f"当前进程 uid={uid} gid={gid}"]
    if not os.path.isdir(folder):
        parts.append(f"目录 {folder} 不存在")
    else:
        st = os.stat(folder)
        parts.append(f"目录属主 uid={st.st_uid} gid={st.st_gid}，权限位 {oct(st.st_mode & 0o777)}")
        writable = os.access(folder, os.W_OK)
        parts.append("目录可写" if writable else "目录不可写")
        if not writable and st.st_uid == uid:
            parts.append("属主就是当前进程却写不进去——是权限位或 ACL 的问题（群晖共享文件夹常见），"
                         "在宿主机上 chmod -R u+rwX 该目录")
    if os.path.exists(DB_PATH) and not os.access(DB_PATH, os.W_OK):
        parts.append("数据库文件本身不可写")
    return "；".join(parts)


def init() -> None:
    try:
        _init()
    except sqlite3.OperationalError as exc:
        text = str(exc)
        if "unable to open" in text or "readonly" in text or "read-only" in text:
            raise StorageError(
                f"打不开数据库（{text}）——多半是权限问题。{_access_hint()}。"
                f"Docker 部署请用 PUID/PGID 环境变量，不要用 user:。"
            ) from exc
        raise


def _init() -> None:
    with _lock, _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_report_summaries(conn)

        # 迁移：早期版本的流水表没有这些列
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
        for name, ddl in (
            ("account_id", "INTEGER"),
            ("check_status", "TEXT"),     # ok / warn / bad / skip
            ("check_detail", "TEXT"),
            ("checked_at", "INTEGER"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {ddl}")

        # 至少保证有一个账户，并把没归属的流水挂到它名下
        row = conn.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO accounts (name, note, created_at) VALUES (?,?,?)",
                (DEFAULT_ACCOUNT, "", int(time.time())),
            )
            default_id = int(cur.lastrowid)
        else:
            default_id = int(row["id"])
        conn.execute(
            "UPDATE transactions SET account_id = ? WHERE account_id IS NULL", (default_id,)
        )


def accounts() -> list[dict]:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id) AS tx_count"
            " FROM accounts a ORDER BY a.id"
        )
        return [dict(r) for r in cur.fetchall()]


def add_account(name: str, note: str = "") -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (name, note, created_at) VALUES (?,?,?)",
            (name, note, int(time.time())),
        )
        return int(cur.lastrowid)


def rename_account(account_id: int, name: str, note: str | None = None) -> bool:
    with _lock, _connect() as conn:
        if note is None:
            cur = conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, int(account_id)))
        else:
            cur = conn.execute(
                "UPDATE accounts SET name = ?, note = ? WHERE id = ?",
                (name, note, int(account_id)),
            )
        return cur.rowcount > 0


def delete_account(account_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM accounts WHERE id = ?", (int(account_id),))
        return cur.rowcount > 0


def account_tx_count(account_id: int) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE account_id = ?", (int(account_id),)
        )
        return int(cur.fetchone()["n"])


def insert_quotes(quotes: list[dict]) -> None:
    if not quotes:
        return
    rows = [
        (
            q["symbol"],
            int(q.get("ts") or time.time()),
            q.get("price"),
            q.get("prev_close"),
            q.get("open"),
            q.get("change_pct"),
            q.get("day_high"),
            q.get("day_low"),
            q.get("volume"),
            q.get("currency"),
            q.get("name"),
        )
        for q in quotes
        if q.get("price") is not None
    ]
    if not rows:
        return
    with _lock, _connect() as conn:
        conn.executemany(
            "INSERT INTO quotes (symbol, ts, price, prev_close, open, change_pct,"
            " day_high, day_low, volume, currency, name)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def history(symbol: str, hours: int = 48, limit: int = 2000) -> list[dict]:
    since = int(time.time()) - hours * 3600
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT ts, price, change_pct FROM quotes WHERE symbol = ? AND ts >= ?"
            " ORDER BY ts ASC LIMIT ?",
            (symbol, since, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def insert_alert(alert: dict) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO alerts (ts, symbol, name, direction, change_pct, threshold,"
            " price, baseline, email_status, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                int(alert.get("ts") or time.time()),
                alert["symbol"],
                alert.get("name"),
                alert.get("direction"),
                alert.get("change_pct"),
                alert.get("threshold"),
                alert.get("price"),
                alert.get("baseline"),
                alert.get("email_status"),
                alert.get("detail"),
            ),
        )


def recent_alerts(limit: int = 50) -> list[dict]:
    with _lock, _connect() as conn:
        cur = conn.execute("SELECT * FROM alerts ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_alert_state(key: str) -> dict | None:
    with _lock, _connect() as conn:
        cur = conn.execute("SELECT * FROM alert_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_alert_state(key: str, ts: int, trade_date: str, change_pct: float) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO alert_state (key, last_ts, trade_date, change_pct) VALUES (?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET last_ts=excluded.last_ts,"
            " trade_date=excluded.trade_date, change_pct=excluded.change_pct",
            (key, int(ts), trade_date, change_pct),
        )


def clear_alert_state(symbol: str | None = None) -> None:
    with _lock, _connect() as conn:
        if symbol:
            conn.execute("DELETE FROM alert_state WHERE key LIKE ?", (f"{symbol}:%",))
        else:
            conn.execute("DELETE FROM alert_state")


TX_KINDS = ("BUY", "SELL", "DIV")


def add_transaction(tx: dict) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO transactions (symbol, kind, trade_date, shares, price,"
            " price_currency, fee_eur, amount_eur, note, account_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                tx["symbol"],
                tx["kind"],
                tx["trade_date"],
                tx.get("shares"),
                tx.get("price"),
                tx.get("price_currency"),
                tx.get("fee_eur") or 0.0,
                tx.get("amount_eur"),
                tx.get("note"),
                tx.get("account_id"),
                int(time.time()),
            ),
        )
        return int(cur.lastrowid)


def update_transaction(tx_id: int, fields: dict) -> bool:
    allowed = (
        "symbol", "kind", "trade_date", "shares", "price",
        "price_currency", "fee_eur", "amount_eur", "note", "account_id",
    )
    sets = [(k, v) for k, v in fields.items() if k in allowed]
    if not sets:
        return False
    clause = ", ".join(f"{k} = ?" for k, _ in sets)
    with _lock, _connect() as conn:
        cur = conn.execute(
            f"UPDATE transactions SET {clause} WHERE id = ?",
            [v for _, v in sets] + [int(tx_id)],
        )
        return cur.rowcount > 0


def delete_transaction(tx_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM transactions WHERE id = ?", (int(tx_id),))
        return cur.rowcount > 0


def transactions(symbol: str | None = None, account_id: int | None = None) -> list[dict]:
    where, params = [], []
    if symbol:
        where.append("t.symbol = ?")
        params.append(symbol)
    if account_id:
        where.append("t.account_id = ?")
        params.append(int(account_id))
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT t.*, a.name AS account_name FROM transactions t"
            f" LEFT JOIN accounts a ON a.id = t.account_id{clause}"
            " ORDER BY t.trade_date ASC, t.id ASC",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def held_symbols() -> list[str]:
    with _lock, _connect() as conn:
        cur = conn.execute("SELECT DISTINCT symbol FROM transactions ORDER BY symbol")
        return [r["symbol"] for r in cur.fetchall()]


def set_tx_check(tx_id: int, status: str, detail: str, checked_at: int | None) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE transactions SET check_status = ?, check_detail = ?, checked_at = ? WHERE id = ?",
            (status, detail, checked_at, int(tx_id)),
        )


# --------------------------------------------------------------------------- 本地归档

def upsert_daily_bars(bars: list[dict]) -> int:
    """写入日线归档。同一天重复写会覆盖（盘中会随最新价变化，收盘后即为收盘价）。"""
    rows = [
        (
            b["symbol"], b["date"], b.get("open"), b.get("high"), b.get("low"),
            b.get("close"), b.get("volume"), b.get("currency"), b.get("source") or "poll",
        )
        for b in bars
        if b.get("symbol") and b.get("date") and b.get("close") is not None
    ]
    if not rows:
        return 0
    with _lock, _connect() as conn:
        conn.executemany(
            "INSERT INTO daily_bars (symbol, date, open, high, low, close, volume, currency, source)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(symbol, date) DO UPDATE SET open=COALESCE(excluded.open, open),"
            " high=COALESCE(excluded.high, high), low=COALESCE(excluded.low, low),"
            " close=excluded.close, volume=COALESCE(excluded.volume, volume),"
            " currency=COALESCE(excluded.currency, currency), source=excluded.source",
            rows,
        )
    return len(rows)


def daily_bar_near(symbol: str, date: str, tolerance_days: int = 5) -> dict | None:
    """取某天的日线；当天没有（周末/假期）就取最近的一条。"""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT *, ABS(julianday(date) - julianday(?)) AS gap FROM daily_bars"
            " WHERE symbol = ? AND ABS(julianday(date) - julianday(?)) <= ?"
            " ORDER BY gap ASC LIMIT 1",
            (date, symbol, date, tolerance_days),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def daily_bars(symbol: str, limit: int = 400) -> list[dict]:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM daily_bars WHERE symbol = ? ORDER BY date DESC LIMIT ?", (symbol, limit)
        )
        return [dict(r) for r in reversed(cur.fetchall())]


def archive_stats() -> dict:
    with _lock, _connect() as conn:
        def one(sql):
            return conn.execute(sql).fetchone()[0]
        return {
            "daily_bars": one("SELECT COUNT(*) FROM daily_bars"),
            "daily_bar_symbols": one("SELECT COUNT(DISTINCT symbol) FROM daily_bars"),
            "quotes": one("SELECT COUNT(*) FROM quotes"),
            "fx_rates": one("SELECT COUNT(*) FROM fx_rates"),
            "fundamentals": one("SELECT COUNT(*) FROM fundamentals_cache"),
            "transactions": one("SELECT COUNT(*) FROM transactions"),
        }


def put_fx_rate(currency: str, eur_per_unit: float, ts: int) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fx_rates (currency, ts, eur_per_unit) VALUES (?,?,?)",
            (currency, int(ts), float(eur_per_unit)),
        )


def latest_fx_rate(currency: str) -> dict | None:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM fx_rates WHERE currency = ? ORDER BY ts DESC LIMIT 1", (currency,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_fundamentals(symbol: str, max_age: int) -> dict | None:
    with _lock, _connect() as conn:
        cur = conn.execute("SELECT ts, payload FROM fundamentals_cache WHERE symbol = ?", (symbol,))
        row = cur.fetchone()
    if not row or int(time.time()) - int(row["ts"]) > max_age:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def get_report_summary(symbol: str, period_key: str, period_type: str = "annual") -> dict | None:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM report_summaries WHERE symbol = ? AND period_type = ?"
            " AND period_key = ?",
            (symbol, period_type, str(period_key)),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def report_summaries(symbol: str | None = None, limit: int = 50,
                     period_type: str | None = None) -> list[dict]:
    where, params = [], []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if period_type:
        where.append("period_type = ?")
        params.append(period_type)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    order = "period_key DESC" if symbol else "created_at DESC"
    params.append(limit)
    with _lock, _connect() as conn:
        cur = conn.execute(f"SELECT * FROM report_summaries{clause} ORDER BY {order} LIMIT ?", params)
        return [dict(r) for r in cur.fetchall()]


def latest_summaries_for(symbols: list[str]) -> dict:
    """每个代码取最新的一份年报点评和一份季报点评，给 PDF 附录用。"""
    out: dict[str, dict] = {}
    if not symbols:
        return out
    with _lock, _connect() as conn:
        for symbol in symbols:
            entry = {}
            for period_type in ("annual", "quarterly"):
                cur = conn.execute(
                    "SELECT * FROM report_summaries WHERE symbol = ? AND period_type = ?"
                    " ORDER BY period_key DESC LIMIT 1", (symbol, period_type))
                row = cur.fetchone()
                if row:
                    entry[period_type] = dict(row)
            if entry:
                out[symbol] = entry
    return out


def put_report_summary(record: dict) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO report_summaries (symbol, period_type, period_key, fiscal_year,"
            " as_of, summary, model, prompt_tokens, completion_tokens, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(symbol, period_type, period_key) DO UPDATE SET"
            " fiscal_year=excluded.fiscal_year, as_of=excluded.as_of,"
            " summary=excluded.summary, model=excluded.model,"
            " prompt_tokens=excluded.prompt_tokens,"
            " completion_tokens=excluded.completion_tokens, created_at=excluded.created_at",
            (
                record["symbol"], record.get("period_type") or "annual",
                str(record["period_key"]),
                record.get("fiscal_year"), record.get("as_of"),
                record.get("summary"), record.get("model"),
                record.get("prompt_tokens"), record.get("completion_tokens"),
                int(record.get("created_at") or time.time()),
            ),
        )


def put_fundamentals(symbol: str, payload: dict) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO fundamentals_cache (symbol, ts, payload) VALUES (?,?,?)"
            " ON CONFLICT(symbol) DO UPDATE SET ts=excluded.ts, payload=excluded.payload",
            (symbol, int(time.time()), json.dumps(payload, ensure_ascii=False)),
        )


def prune(keep_days: int = 30) -> None:
    cutoff = int(time.time()) - keep_days * 86400
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM quotes WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))


# --------------------------------------------------------------------------- 备份与整体替换

CORE_TABLES = ("transactions", "accounts", "report_summaries")


def backup_to(path: str) -> None:
    """把当前库一致性快照到 path。

    不是复制文件——开着 WAL 的时候主文件可能落后一大截，数据还在 .db-wal 里，
    照抄会得到一个旧快照。用 sqlite3 的在线备份接口，程序正在跑也安全。
    """
    with _lock:
        src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        dst = sqlite3.connect(path)
        try:
            with dst:
                src.backup(dst)
        finally:
            src.close()
            dst.close()


def inspect_db(path: str) -> dict:
    """看看这个文件是不是一个能用的 StockWatch 库。不抛异常。"""
    if not os.path.exists(path):
        return {"ok": False, "error": "文件不存在"}
    try:
        with open(path, "rb") as fh:
            if fh.read(16) != b"SQLite format 3\x00":
                return {"ok": False, "error": "不是 SQLite 数据库文件"}
    except OSError as exc:
        return {"ok": False, "error": f"读不了：{exc}"}

    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        # 只查第一条：整库 integrity_check 在大库上很慢，而我们只想排除明显损坏
        result = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        if result != "ok":
            return {"ok": False, "error": f"文件已损坏：{result}"}
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in CORE_TABLES if t not in names]
        if missing:
            return {"ok": False, "error": f"缺少必要的表：{'、'.join(missing)}"}
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in CORE_TABLES}
        return {"ok": True, "counts": counts, "tables": len(names)}
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"打不开：{exc}"}
    finally:
        if conn is not None:
            conn.close()


def replace_with(path: str) -> str:
    """用 path 处的库整体替换当前库，返回替换前的备份路径。

    调用方必须先用 inspect_db() 验过。替换会**丢掉当前库的全部内容**，
    所以先自动备份一份到 DB_PATH.bak-<时间戳>——这一步不要省，
    导入错文件是很容易发生的事。
    """
    # 先把新库复制到**数据库旁边**再改名。不能直接 os.replace(上传的临时文件)：
    # 临时目录和数据库常常不在一个文件系统上（WSL 的 /tmp 对 /mnt/d、
    # 容器的 /tmp 对挂载卷），那样会直接 EXDEV 失败。
    # 同一个文件系统内的 rename 才是原子的，中途断电也不会留下半个库。
    staging = f"{DB_PATH}.incoming"
    try:
        shutil.copyfile(path, staging)
    except OSError as exc:
        _quiet_remove(staging)
        raise RuntimeError(f"写入失败（磁盘满了？）：{exc}") from exc

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = f"{DB_PATH}.bak-{stamp}"
    backup_to(backup_path)

    with _lock:
        os.replace(staging, DB_PATH)
        # -wal / -shm 里是旧库的东西，留着会和新文件对不上，必须一起删
        for suffix in ("-wal", "-shm"):
            _quiet_remove(DB_PATH + suffix)

    init()            # 新库可能来自老版本，跑一遍迁移
    prune_backups()
    return backup_path


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def prune_backups(keep: int = 3) -> None:
    """导入前的自动备份留最近几份就够了，不然导几次就堆一堆一兆多的文件。"""
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    prefix = os.path.basename(DB_PATH) + ".bak-"
    try:
        backups = sorted(f for f in os.listdir(folder) if f.startswith(prefix))
    except OSError:
        return
    for name in backups[:-keep] if keep > 0 else backups:
        _quiet_remove(os.path.join(folder, name))
