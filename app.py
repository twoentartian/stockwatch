"""StockWatch：定时抓取美股指数/个股行情，Web 端展示，异动时发邮件提醒。

运行：  python app.py            （默认 http://0.0.0.0:8765）
环境变量：STOCKWATCH_HOST / STOCKWATCH_PORT / STOCKWATCH_CONFIG / STOCKWATCH_DB
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from urllib.parse import quote
import time

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, jsonify, render_template, request

import alerts
import config
import dataio
import export_pdf
import fundamentals
import fx
import insights
import llm
import notifier
import portfolio
import providers
import store
import verify
import websearch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("stockwatch")

app = Flask(__name__)
app.json.ensure_ascii = False

scheduler = BackgroundScheduler(timezone="UTC")
POLL_JOB_ID = "poll"

_poll_lock = threading.Lock()
_state = {
    "quotes": {},        # symbol -> quote
    "errors": {},        # symbol -> 错误信息
    "last_poll": None,   # 时间戳
    "last_reason": None,
    "last_prune": 0,
}


# --------------------------------------------------------------------------- 轮询

def _quote_to_bar(quote: dict) -> dict:
    """把一次行情快照折成当天的日线，写进永久归档。

    盘中会被后续轮询不断覆盖，收盘后最后一次写入的就是当天收盘价。
    """
    stamp = quote.get("market_time") or quote.get("ts") or time.time()
    return {
        "symbol": quote["symbol"],
        "date": time.strftime("%Y-%m-%d", time.gmtime(int(stamp))),
        "open": quote.get("open"),
        "high": quote.get("day_high"),
        "low": quote.get("day_low"),
        "close": quote.get("price"),
        "volume": quote.get("volume"),
        "currency": quote.get("currency"),
        "source": "poll",
    }


def poll_once(reason: str = "scheduled") -> dict:
    """抓一轮行情、落库、判定告警。返回本轮摘要。"""
    if not _poll_lock.acquire(blocking=False):
        return {"skipped": True, "error": "上一轮抓取还没结束"}
    try:
        cfg = config.load()
        symbols = [i["symbol"] for i in cfg["indices"]] + [w["symbol"] for w in cfg["watchlist"]]
        symbols += store.held_symbols()  # 有持仓但没在追踪列表里的也要报价，否则算不出市值
        quotes, errors = providers.fetch_quotes(symbols)
        store.insert_quotes(list(quotes.values()))
        store.upsert_daily_bars([_quote_to_bar(q) for q in quotes.values()])
        fx.refresh(q.get("currency") for q in quotes.values())

        triggered = []
        try:
            triggered = alerts.notify(alerts.evaluate(quotes, cfg), cfg)
        except Exception:
            log.exception("告警处理失败")

        # 持仓收益里程碑（跟股价告警分开：一个看当天涨跌，一个看这笔投资赚了多少）
        try:
            book = portfolio.build_all(
                store.transactions(), _state["quotes"] | quotes,
                _names_map(cfg), _accounts_map())
            milestones = alerts.notify_positions(
                alerts.evaluate_positions(book["positions"], cfg), cfg)
            triggered = list(triggered) + list(milestones)
        except Exception:
            log.exception("持仓里程碑处理失败")

        now = int(time.time())
        _state["quotes"].update(quotes)
        _state["errors"] = errors
        _state["last_poll"] = now
        _state["last_reason"] = reason

        if now - _state["last_prune"] > 6 * 3600:
            _state["last_prune"] = now
            try:
                store.prune(cfg.get("history_keep_days", 30))
            except Exception:
                log.exception("清理历史数据失败")

        log.info(
            "抓取完成(%s)：成功 %d 个，失败 %d 个，触发告警 %d 条",
            reason, len(quotes), len(errors), len(triggered),
        )
        return {
            "ok": True,
            "fetched": len(quotes),
            "errors": errors,
            "triggered": len(triggered),
            "last_poll": now,
        }
    finally:
        _poll_lock.release()


def reschedule(minutes: int) -> None:
    minutes = max(1, int(minutes))
    if scheduler.get_job(POLL_JOB_ID):
        scheduler.reschedule_job(POLL_JOB_ID, trigger="interval", minutes=minutes)
    else:
        scheduler.add_job(
            poll_once,
            trigger="interval",
            minutes=minutes,
            id=POLL_JOB_ID,
            max_instances=1,
            coalesce=True,
            kwargs={"reason": "scheduled"},
        )
    log.info("轮询间隔已设为 %d 分钟", minutes)


REPORT_JOB_ID = "reports"


def reschedule_reports(hours: int) -> None:
    """新年报扫描是独立的慢节奏任务：年报一年才出一次，没必要跟着行情轮询跑。"""
    hours = max(1, int(hours))
    if scheduler.get_job(REPORT_JOB_ID):
        scheduler.reschedule_job(REPORT_JOB_ID, trigger="interval", hours=hours)
    else:
        scheduler.add_job(
            insights.check_new_reports,
            trigger="interval",
            hours=hours,
            id=REPORT_JOB_ID,
            max_instances=1,
            coalesce=True,
        )


def next_run_ts():
    job = scheduler.get_job(POLL_JOB_ID)
    if job and job.next_run_time:
        return int(job.next_run_time.timestamp())
    return None


# --------------------------------------------------------------------------- 页面 & API

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    cfg = config.load()
    quotes = _state["quotes"]
    baseline = cfg["alert"].get("baseline", "prev_close")

    def decorate(quote: dict) -> dict:
        out = dict(quote)
        out["baseline_change_pct"] = alerts.change_against_baseline(quote, baseline)
        return out

    indices = []
    for item in cfg["indices"]:
        quote = quotes.get(item["symbol"])
        indices.append(
            {
                "symbol": item["symbol"],
                "alias": item.get("name") or "",
                "quote": decorate(quote) if quote else None,
                "error": _state["errors"].get(item["symbol"]),
            }
        )

    watchlist = []
    for item in cfg["watchlist"]:
        quote = quotes.get(item["symbol"])
        watchlist.append(
            {
                **item,
                "quote": decorate(quote) if quote else None,
                "error": _state["errors"].get(item["symbol"]),
            }
        )

    email_cfg = cfg["email"]
    return jsonify(
        {
            "indices": indices,
            "watchlist": watchlist,
            "alerts": store.recent_alerts(30),
            "last_poll": _state["last_poll"],
            "next_run": next_run_ts(),
            "poll_interval_minutes": cfg["poll_interval_minutes"],
            "baseline": baseline,
            "market_open": alerts.is_market_open(),
            "market_time": alerts.market_now().strftime("%Y-%m-%d %H:%M:%S"),
            "email": {
                "enabled": bool(email_cfg.get("enabled")),
                "configured": notifier.validate(email_cfg) is None,
                "to_addrs": email_cfg.get("to_addrs", []),
            },
            "server_time": int(time.time()),
        }
    )


@app.get("/api/config")
def api_get_config():
    return jsonify(config.public_view(config.load()))


@app.post("/api/config")
def api_set_config():
    body = request.get_json(silent=True) or {}

    def mutate(cfg):
        if "poll_interval_minutes" in body:
            cfg["poll_interval_minutes"] = max(1, int(float(body["poll_interval_minutes"])))
        if isinstance(body.get("alert"), dict):
            cfg["alert"].update(body["alert"])
        if isinstance(body.get("email"), dict):
            email = dict(body["email"])
            # 前端拿到的是占位符，原样传回来表示“密码不变”
            if email.get("password") == config.PASSWORD_MASK:
                email.pop("password")
            cfg["email"].update(email)
        if isinstance(body.get("llm"), dict):
            llm_body = dict(body["llm"])
            if llm_body.get("api_key") == config.PASSWORD_MASK:
                llm_body.pop("api_key")
            cfg["llm"].update(llm_body)
        if isinstance(body.get("websearch"), dict):
            web_body = dict(body["websearch"])
            if web_body.get("api_key") == config.PASSWORD_MASK:
                web_body.pop("api_key")
            cfg["websearch"].update(web_body)
        if "history_keep_days" in body:
            cfg["history_keep_days"] = max(1, int(float(body["history_keep_days"])))

    cfg = config.update(mutate)
    reschedule(cfg["poll_interval_minutes"])
    reschedule_reports(cfg["llm"]["check_interval_hours"])
    return jsonify({"ok": True, "config": config.public_view(cfg)})


@app.post("/api/watchlist/add")
def api_watch_add():
    body = request.get_json(silent=True) or {}
    symbol = config.normalize_symbol(body.get("symbol", ""))
    if not symbol:
        return jsonify({"ok": False, "error": "请填写股票代码"}), 400

    cfg = config.load()
    if any(w["symbol"] == symbol for w in cfg["watchlist"]):
        return jsonify({"ok": False, "error": f"{symbol} 已经在追踪列表里"}), 400

    try:
        quote = providers.fetch_quote(symbol)
    except providers.QuoteError as exc:
        return jsonify({"ok": False, "error": f"查不到 {symbol}：{exc}"}), 400

    item = config.normalize_watch_item({**body, "symbol": symbol}, cfg["alert"])
    item["name"] = item["name"] or quote.get("name") or symbol

    def mutate(c):
        c["watchlist"].append(item)

    config.update(mutate)
    _state["quotes"][symbol] = quote
    store.insert_quotes([quote])
    return jsonify({"ok": True, "item": item})


@app.post("/api/watchlist/update")
def api_watch_update():
    body = request.get_json(silent=True) or {}
    symbol = config.normalize_symbol(body.get("symbol", ""))
    found = {"hit": False}

    def mutate(cfg):
        for idx, item in enumerate(cfg["watchlist"]):
            if item["symbol"] == symbol:
                merged = {**item, **body, "symbol": symbol}
                cfg["watchlist"][idx] = config.normalize_watch_item(merged, cfg["alert"])
                found["hit"] = True
                break

    cfg = config.update(mutate)
    if not found["hit"]:
        return jsonify({"ok": False, "error": f"{symbol} 不在追踪列表里"}), 404
    return jsonify({"ok": True, "watchlist": cfg["watchlist"]})


@app.post("/api/watchlist/remove")
def api_watch_remove():
    body = request.get_json(silent=True) or {}
    symbol = config.normalize_symbol(body.get("symbol", ""))

    def mutate(cfg):
        cfg["watchlist"] = [w for w in cfg["watchlist"] if w["symbol"] != symbol]

    config.update(mutate)
    store.clear_alert_state(symbol)
    return jsonify({"ok": True})


@app.post("/api/indices/add")
def api_index_add():
    body = request.get_json(silent=True) or {}
    symbol = config.normalize_symbol(body.get("symbol", ""))
    if not symbol:
        return jsonify({"ok": False, "error": "请填写代码"}), 400
    cfg = config.load()
    if any(i["symbol"] == symbol for i in cfg["indices"]):
        return jsonify({"ok": False, "error": f"{symbol} 已在列表里"}), 400
    try:
        quote = providers.fetch_quote(symbol)
    except providers.QuoteError as exc:
        return jsonify({"ok": False, "error": f"查不到 {symbol}：{exc}"}), 400

    name = (body.get("name") or "").strip() or quote.get("name") or ""

    def mutate(c):
        c["indices"].append({"symbol": symbol, "name": name})

    config.update(mutate)
    _state["quotes"][symbol] = quote
    return jsonify({"ok": True})


@app.post("/api/indices/remove")
def api_index_remove():
    body = request.get_json(silent=True) or {}
    symbol = config.normalize_symbol(body.get("symbol", ""))

    def mutate(cfg):
        cfg["indices"] = [i for i in cfg["indices"] if i["symbol"] != symbol]

    config.update(mutate)
    return jsonify({"ok": True})


@app.post("/api/refresh")
def api_refresh():
    return jsonify(poll_once(reason="manual"))


@app.post("/api/email/test")
def api_email_test():
    body = request.get_json(silent=True) or {}
    cfg = config.load()
    email_cfg = dict(cfg["email"])
    # 允许直接用页面上还没保存的设置试发
    override = body.get("email")
    if isinstance(override, dict):
        override = dict(override)
        if override.get("password") == config.PASSWORD_MASK:
            override.pop("password")
        email_cfg.update(override)
    to_addrs = body.get("to_addrs") or None
    ok, message = notifier.send_test(email_cfg, override_to=to_addrs)
    return jsonify({"ok": ok, "message": message})


# --------------------------------------------------------------------------- 持仓

def _names_map(cfg: dict) -> dict:
    return {w["symbol"]: w.get("name") or "" for w in cfg["watchlist"]}


def _accounts_map() -> dict:
    return {a["id"]: a for a in store.accounts()}


def _default_account_id() -> int | None:
    rows = store.accounts()
    return rows[0]["id"] if rows else None


@app.get("/api/accounts")
def api_accounts():
    return jsonify({"accounts": store.accounts()})


@app.post("/api/accounts/add")
def api_account_add():
    name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "请填写账户名"}), 400
    if any(a["name"] == name for a in store.accounts()):
        return jsonify({"ok": False, "error": f"账户「{name}」已存在"}), 400
    return jsonify({"ok": True, "id": store.add_account(name, note)})


@app.post("/api/accounts/update")
def api_account_update():
    body = request.get_json(silent=True) or {}
    account_id = int(body.get("id") or 0)
    name = (body.get("name") or "").strip()
    if not account_id or not name:
        return jsonify({"ok": False, "error": "缺少账户 id 或名称"}), 400
    if any(a["name"] == name and a["id"] != account_id for a in store.accounts()):
        return jsonify({"ok": False, "error": f"账户「{name}」已存在"}), 400
    if not store.rename_account(account_id, name, body.get("note")):
        return jsonify({"ok": False, "error": "账户不存在"}), 404
    return jsonify({"ok": True})


@app.post("/api/accounts/remove")
def api_account_remove():
    account_id = int((request.get_json(silent=True) or {}).get("id") or 0)
    count = store.account_tx_count(account_id)
    if count:
        return jsonify({
            "ok": False,
            "error": f"这个账户下还有 {count} 条流水，请先删除或改到别的账户",
        }), 400
    if len(store.accounts()) <= 1:
        return jsonify({"ok": False, "error": "至少要保留一个账户"}), 400
    if not store.delete_account(account_id):
        return jsonify({"ok": False, "error": "账户不存在"}), 404
    return jsonify({"ok": True})


def _parse_tx(body: dict, require_symbol: bool = True) -> tuple[dict | None, str | None]:
    """校验前端传来的一笔流水。返回 (规范化的记录, 错误信息)。"""
    symbol = config.normalize_symbol(body.get("symbol", ""))
    if require_symbol and not symbol:
        return None, "缺少股票代码"

    account_id = int(body.get("account_id") or 0) or _default_account_id()
    if not account_id:
        return None, "请先在「账户」页建一个账户"
    if account_id not in _accounts_map():
        return None, "账户不存在"

    kind = (body.get("kind") or "BUY").upper()
    if kind not in store.TX_KINDS:
        return None, f"类型只能是 {'/'.join(store.TX_KINDS)}"

    trade_date = (body.get("trade_date") or "").strip()[:10]
    if not portfolio.parse_date(trade_date):
        return None, "日期格式应为 YYYY-MM-DD"

    def num(key, default=None):
        value = body.get(key)
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    shares = num("shares")
    amount_eur = num("amount_eur")
    price = num("price")
    fee_eur = num("fee_eur", 0.0) or 0.0

    if kind in ("BUY", "SELL"):
        if not shares or shares <= 0:
            return None, "请填写股数"
        if amount_eur is None and price is not None:
            # 没填实际扣款就按 股数×单价(+手续费) 估一个，前提是单价本来就是欧元
            amount_eur = shares * price + (fee_eur if kind == "BUY" else -fee_eur)
        if amount_eur is None:
            return None, "请填写实际成交金额（欧元）"
    else:  # DIV
        if amount_eur is None:
            return None, "请填写到账股息金额（欧元）"
        shares = None

    return {
        "symbol": symbol,
        "kind": kind,
        "trade_date": trade_date,
        "shares": shares,
        "price": price,
        "price_currency": (body.get("price_currency") or "").strip().upper() or None,
        "fee_eur": fee_eur,
        "amount_eur": abs(amount_eur),
        "note": (body.get("note") or "").strip() or None,
        "account_id": account_id,
    }, None


def _ensure_quotable(symbol: str) -> str | None:
    """确认这个代码真的能取到价格，否则持仓里会永远显示不出市值。

    典型的坑：Intel 在 XETRA 的代码是 INL.DE，写成 INL 也能查到条目但没有最新价。
    """
    if symbol in _state["quotes"]:
        return None
    try:
        quote = providers.fetch_quote(symbol)
    except providers.QuoteError as exc:
        return f"{symbol} 取不到行情（{exc}）。请在「追踪与告警」里用搜索确认正确代码，比如 Intel 的德国上市是 INL.DE。"
    _state["quotes"][symbol] = quote
    fx.refresh([quote.get("currency")])
    return None


@app.get("/api/portfolio")
def api_portfolio():
    cfg = config.load()
    account_id = int(request.args.get("account_id") or 0) or None
    data = portfolio.build_all(
        store.transactions(account_id=account_id),
        _state["quotes"],
        _names_map(cfg),
        _accounts_map(),
    )
    data["fx"] = fx.snapshot()
    data["base_currency"] = fx.BASE
    data["accounts"] = store.accounts()
    data["account_id"] = account_id
    return jsonify(data)


@app.get("/api/portfolio/export.pdf")
def api_portfolio_export():
    """导出当前账户视图的持仓报告。account_id 为空就是「全部账户」。"""
    cfg = config.load()
    account_id = int(request.args.get("account_id") or 0) or None
    accounts = _accounts_map()
    account_name = (accounts.get(account_id) or {}).get("name") if account_id else None

    txs = store.transactions(account_id=account_id)
    data = portfolio.build_all(txs, _state["quotes"], _names_map(cfg), accounts)
    data["fx"] = fx.snapshot()

    try:
        summaries = store.latest_summaries_for([p["symbol"] for p in data["positions"]])
        pdf = export_pdf.build(data, txs, account_name, summaries)
    except Exception as exc:
        log.exception("导出 PDF 失败")
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    stamp = datetime.now().strftime("%Y-%m-%d")
    filename = f"持仓报告_{account_name or '全部账户'}_{stamp}.pdf"
    ascii_name = f"stockwatch-portfolio-{stamp}.pdf"
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={
            # 中文文件名要用 RFC 5987 的写法，同时留一个纯 ASCII 的兜底
            "Content-Disposition":
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(len(pdf)),
        },
    )


# --------------------------------------------------------------------------- 数据导出 / 导入

# SQLite 的文件头。写成拼接而不是带转义的字面量——转义写法在几层脚本之间
# 传递时很容易变成一个真的 NUL 字节塞进源码，Python 直接拒绝编译。
SQLITE_MAGIC = b"SQLite format 3" + bytes(1)

MAX_UPLOAD_BYTES = 256 * 1024 * 1024     # 整库快照可能不小，但也不能无上限


@app.get("/api/data/export.db")
def api_export_db():
    """整库快照。跑着也能导——走 sqlite3 在线备份，不是复制文件。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "snapshot.db")
        try:
            store.backup_to(path)
        except Exception as exc:
            log.exception("导出数据库失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        with open(path, "rb") as fh:
            blob = fh.read()
    return Response(
        blob,
        mimetype="application/x-sqlite3",
        headers={
            "Content-Disposition": f'attachment; filename="stockwatch-{stamp}.db"',
            "Content-Length": str(len(blob)),
        },
    )


@app.get("/api/data/export.json")
def api_export_json():
    """可移植数据：账户、流水、追踪列表、点评。不含行情缓存，也不含任何密钥。"""
    try:
        payload = dataio.export_payload()
    except Exception as exc:
        log.exception("导出 JSON 失败")
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    blob = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response(
        blob,
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="stockwatch-{stamp}.json"',
            "Content-Length": str(len(blob)),
        },
    )


@app.get("/api/data/stats")
def api_data_stats():
    """导出前给前端报个数，让人知道自己要导的是多少东西。"""
    info = store.inspect_db(store.DB_PATH)
    size = os.path.getsize(store.DB_PATH) if os.path.exists(store.DB_PATH) else 0
    return jsonify({"ok": True, "size_bytes": size, **info})


@app.post("/api/data/import")
def api_import_data():
    """导入一个文件。按内容判断是整库快照还是 JSON，不看扩展名——
    扩展名是用户随手改的，文件头不会骗人。"""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"ok": False, "error": "没有收到文件"}), 400

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "upload.bin")
        upload.save(path)
        size = os.path.getsize(path)
        if size == 0:
            return jsonify({"ok": False, "error": "文件是空的"}), 400
        if size > MAX_UPLOAD_BYTES:
            return jsonify({"ok": False, "error": f"文件太大（{size / 1e6:.0f} MB）"}), 400

        with open(path, "rb") as fh:
            head = fh.read(16)

        if head == SQLITE_MAGIC:
            check = store.inspect_db(path)
            if not check["ok"]:
                return jsonify({"ok": False, "error": check["error"]}), 400
            try:
                backup = store.replace_with(path)
            except Exception as exc:
                log.exception("替换数据库失败")
                return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
            _state["quotes"] = {}      # 旧库的行情缓存跟新库对不上，清掉重抓
            threading.Thread(target=poll_once, kwargs={"reason": "import"}, daemon=True).start()
            log.warning("数据库已被整体替换，替换前的备份：%s", backup)
            return jsonify({"ok": True, "mode": "db", "counts": check["counts"],
                            "backup": os.path.basename(backup)})

        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            return jsonify({"ok": False,
                            "error": f"既不是数据库文件也不是合法 JSON：{exc}"}), 400
        try:
            report = dataio.import_payload(payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            log.exception("导入 JSON 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        return jsonify({"ok": True, "mode": "json", **report})


@app.get("/api/transactions")
def api_transactions():
    symbol = config.normalize_symbol(request.args.get("symbol", ""))
    account_id = int(request.args.get("account_id") or 0) or None
    return jsonify({"transactions": store.transactions(symbol or None, account_id)})


@app.post("/api/transactions/add")
def api_tx_add():
    tx, error = _parse_tx(request.get_json(silent=True) or {})
    if error:
        return jsonify({"ok": False, "error": error}), 400
    # 代码写错就当场拦下来，不要等到持仓页发现市值是空的
    error = _ensure_quotable(tx["symbol"])
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "id": store.add_transaction(tx)})


@app.post("/api/transactions/update")
def api_tx_update():
    body = request.get_json(silent=True) or {}
    tx_id = body.get("id")
    if not tx_id:
        return jsonify({"ok": False, "error": "缺少记录 id"}), 400
    tx, error = _parse_tx(body)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    error = _ensure_quotable(tx["symbol"])
    if error:
        return jsonify({"ok": False, "error": error}), 400
    if not store.update_transaction(int(tx_id), tx):
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True})


@app.post("/api/transactions/verify")
def api_tx_verify():
    """用成交当天的实际行情核对记录的单价。核对通过的会标记为已核对。"""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({
            "ok": True,
            **verify.check_all(
                only_unchecked=not body.get("recheck"),
                tx_id=int(body["id"]) if body.get("id") else None,
            ),
        })
    except Exception as exc:
        log.exception("核对交易失败")
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@app.get("/api/archive/stats")
def api_archive_stats():
    """本地归档了多少数据——所有抓下来的东西都落在这个 SQLite 文件里。"""
    stats = store.archive_stats()
    stats["db_path"] = store.DB_PATH
    try:
        stats["db_size_mb"] = round(os.path.getsize(store.DB_PATH) / 1048576, 2)
    except OSError:
        stats["db_size_mb"] = None
    return jsonify(stats)


@app.post("/api/transactions/note")
def api_tx_note():
    """只改备注。走单独的入口，不必像整条更新那样重新校验金额和行情。"""
    body = request.get_json(silent=True) or {}
    tx_id = int(body.get("id") or 0)
    if not tx_id:
        return jsonify({"ok": False, "error": "缺少记录 id"}), 400
    note = (body.get("note") or "").strip() or None
    if not store.update_transaction(tx_id, {"note": note}):
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True})


@app.post("/api/transactions/remove")
def api_tx_remove():
    body = request.get_json(silent=True) or {}
    if not store.delete_transaction(int(body.get("id") or 0)):
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- 基本面

@app.get("/stock/<path:symbol>")
def stock_page(symbol: str):
    return render_template("stock.html", symbol=config.normalize_symbol(symbol))


@app.get("/api/fundamentals")
def api_fundamentals():
    symbol = config.normalize_symbol(request.args.get("symbol", ""))
    if not symbol:
        return jsonify({"ok": False, "error": "缺少股票代码"}), 400
    force = request.args.get("force") in ("1", "true", "yes")
    try:
        return jsonify(fundamentals.load(symbol, force=force))
    except providers.QuoteError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:  # 数据源结构变化时不要把整页拖垮
        log.exception("基本面数据加载失败 %s", symbol)
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


# --------------------------------------------------------------------------- 年报点评

@app.post("/api/llm/test")
def api_llm_test():
    """设置页的「测试连接」。允许用页面上还没保存的配置试。"""
    body = request.get_json(silent=True) or {}
    llm_cfg = dict(config.load().get("llm") or {})
    override = body.get("llm")
    if isinstance(override, dict):
        override = dict(override)
        if override.get("api_key") == config.PASSWORD_MASK:
            override.pop("api_key")
        llm_cfg.update(override)
    ok, message = llm.test_connection(llm_cfg)
    return jsonify({"ok": ok, "message": message})


@app.post("/api/websearch/test")
def api_websearch_test():
    """设置页的「测试检索」。真的会发一次请求出去，会算 Tavily 的额度。"""
    body = request.get_json(silent=True) or {}
    web_cfg = dict(config.load().get("websearch") or {})
    override = body.get("websearch")
    if isinstance(override, dict):
        override = dict(override)
        if override.get("api_key") == config.PASSWORD_MASK:
            override.pop("api_key")
        web_cfg.update(override)
    web_cfg["enabled"] = True   # 测试时不看开关，否则没法先测后开
    web_cfg["include_domains"] = websearch.normalize_domains(web_cfg.get("include_domains"))
    ok, message = websearch.test_connection(web_cfg)
    return jsonify({"ok": ok, "message": message})


@app.get("/api/insights")
def api_insights():
    symbol = config.normalize_symbol(request.args.get("symbol", ""))
    return jsonify({
        "summaries": store.report_summaries(symbol or None, 50),
        "configured": llm.is_configured(config.load().get("llm") or {}),
    })


@app.post("/api/insights/generate")
def api_insights_generate():
    body = request.get_json(silent=True) or {}
    symbol = config.normalize_symbol(body.get("symbol", ""))
    if not symbol:
        return jsonify({"ok": False, "error": "缺少股票代码"}), 400
    period_type = body.get("period_type") or "annual"
    try:
        record = insights.generate(symbol, force=bool(body.get("force")),
                                   period_type=period_type)
        return jsonify({"ok": True, **record})
    except llm.LLMError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:
        log.exception("生成年报总结失败 %s", symbol)
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@app.post("/api/insights/email")
def api_insights_email():
    """把已生成的点评手动发一封邮件。不带 symbol 就发最近生成的那几份。"""
    body = request.get_json(silent=True) or {}
    symbol = config.normalize_symbol(body.get("symbol", ""))
    period_key = body.get("period_key") or body.get("fiscal_year")
    period_type = body.get("period_type") or "annual"

    if symbol and period_key:
        record = store.get_report_summary(symbol, str(period_key), period_type)
        records = [record] if record else []
    elif symbol:
        records = store.report_summaries(symbol, 2)
    else:
        records = store.report_summaries(None, int(body.get("limit") or 5))

    if not records:
        return jsonify({"ok": False, "error": "没有找到可发送的点评"}), 404
    ok, message = insights.email_summaries(records)
    return jsonify({"ok": ok, "message": message, "count": len(records)})


@app.post("/api/insights/check")
def api_insights_check():
    """手动触发一次「有没有新年报」的扫描。"""
    try:
        return jsonify({"ok": True, **insights.check_new_reports()})
    except Exception as exc:
        log.exception("扫描新年报失败")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/search")
def api_search():
    return jsonify({"results": providers.search_symbols(request.args.get("q", ""))})


@app.get("/api/history")
def api_history():
    symbol = config.normalize_symbol(request.args.get("symbol", ""))
    hours = max(1, min(24 * 30, int(float(request.args.get("hours", 48)))))
    return jsonify({"symbol": symbol, "points": store.history(symbol, hours)})


@app.get("/api/alerts")
def api_alerts():
    limit = max(1, min(500, int(float(request.args.get("limit", 100)))))
    return jsonify({"alerts": store.recent_alerts(limit)})


@app.post("/api/alerts/reset")
def api_alerts_reset():
    """清掉告警去重状态，让同样的行情可以再次触发（调试或误报后重置用）。"""
    body = request.get_json(silent=True) or {}
    store.clear_alert_state(config.normalize_symbol(body.get("symbol", "")) or None)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- 启动

def bootstrap() -> None:
    try:
        store.init()
    except store.StorageError as exc:
        # 一行说清楚就退出。放任它抛出去的话，容器会不断重启、
        # 每次刷一屏 traceback，真正有用的那句话反而被淹掉
        log.critical("%s", exc)
        raise SystemExit(1) from None
    cfg = config.load()
    if not scheduler.running:
        scheduler.start()
    reschedule(cfg["poll_interval_minutes"])
    reschedule_reports(cfg["llm"]["check_interval_hours"])
    threading.Thread(target=poll_once, kwargs={"reason": "startup"}, daemon=True).start()
    log.info("配置文件：%s", config.CONFIG_PATH)
    log.info("数据库：%s", store.DB_PATH)


bootstrap()

if __name__ == "__main__":
    host = os.environ.get("STOCKWATCH_HOST", "0.0.0.0")
    port = int(os.environ.get("STOCKWATCH_PORT", "8765"))
    log.info("Web 界面： http://127.0.0.1:%d", port)
    # 关掉 reloader，否则调度器会被启动两次
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
