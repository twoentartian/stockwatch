"""数据导出 / 导入。

两种格式，用途不一样：

* **`.db`（整库快照）** —— 一模一样的东西，包含行情历史、日线归档、基本面缓存。
  适合搬家、备份、把数据挪进 Docker。导入 = **整体替换**，当前库会被覆盖
  （替换前自动备份一份，见 store.replace_with）。
* **`.json`（可移植数据）** —— 只有**你自己录进去的东西**：账户、交易流水、
  追踪列表、指数、财报点评。没有行情缓存（那些随时能重抓），
  也**没有任何密钥**。导入是**合并**，不会删掉现有记录。

为什么要有 JSON 这一份：`.db` 绑死在某个 schema 版本上，跨版本导入要靠迁移兜着；
JSON 只认字段名，老导出的文件在新版本里照样能进来。

导入的文件是**外部输入**，一律当不可信处理：先验格式、验类型，再写库。
"""
from __future__ import annotations

import json
import logging
import time

import config
import store

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
KINDS = ("BUY", "SELL", "DIV")


# --------------------------------------------------------------------------- 导出

def export_payload() -> dict:
    """攒出可移植的 JSON。不含密钥、不含行情缓存。"""
    cfg = config.load()
    accounts = store.accounts()
    by_id = {a["id"]: a["name"] for a in accounts}

    txs = []
    for tx in store.transactions():
        row = {k: tx.get(k) for k in (
            "symbol", "kind", "trade_date", "shares", "price", "price_currency",
            "fee_eur", "amount_eur", "note", "created_at",
            "check_status", "check_detail", "checked_at")}
        # 用账户**名字**而不是 id：换台机器 id 对不上，名字才是人认得的东西
        row["account"] = by_id.get(tx.get("account_id")) or ""
        txs.append(row)

    return {
        "format": "stockwatch",
        "version": FORMAT_VERSION,
        "exported_at": int(time.time()),
        "accounts": [{"name": a["name"], "note": a.get("note") or ""} for a in accounts],
        "transactions": txs,
        "watchlist": cfg.get("watchlist") or [],
        "indices": cfg.get("indices") or [],
        "report_summaries": store.report_summaries(None, 10000),
    }


# --------------------------------------------------------------------------- 导入

def _num(value, fallback=None):
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return fallback


def _tx_key(tx: dict) -> tuple:
    """判重用的自然键。同一天、同一账户、同一只票、同样的股数和金额，视为同一笔。"""
    return (
        (tx.get("account") or "").strip(),
        (tx.get("symbol") or "").strip().upper(),
        (tx.get("kind") or "").strip().upper(),
        (tx.get("trade_date") or "").strip(),
        round(_num(tx.get("shares")) or 0, 4),
        round(_num(tx.get("amount_eur")) or 0, 2),
    )


def import_payload(data: dict, with_config: bool = True) -> dict:
    """把 JSON 合并进当前库。返回每一项加了多少、跳过多少。

    **只增不删**：重复的流水按自然键跳过，已存在的账户和点评不覆盖。
    想整体替换用 `.db` 那条路。
    """
    if not isinstance(data, dict) or data.get("format") != "stockwatch":
        raise ValueError("不是 StockWatch 的导出文件")
    if int(data.get("version") or 0) > FORMAT_VERSION:
        raise ValueError(f"文件版本 {data.get('version')} 比本程序新，升级后再导入")

    report = {"accounts": 0, "transactions": 0, "skipped": 0,
              "summaries": 0, "watchlist": 0, "indices": 0, "errors": []}

    # ---- 账户：按名字对齐，已有的不动
    existing = {a["name"]: a["id"] for a in store.accounts()}
    for item in data.get("accounts") or []:
        name = (item.get("name") or "").strip()
        if not name or name in existing:
            continue
        try:
            existing[name] = store.add_account(name, (item.get("note") or "").strip())
            report["accounts"] += 1
        except Exception as exc:
            report["errors"].append(f"账户「{name}」：{exc}")

    # ---- 流水：按自然键判重
    have = set()
    by_id = {a["id"]: a["name"] for a in store.accounts()}
    for tx in store.transactions():
        row = dict(tx)
        row["account"] = by_id.get(tx.get("account_id")) or ""
        have.add(_tx_key(row))

    default_account = next(iter(existing.values()), None)
    for tx in data.get("transactions") or []:
        if not isinstance(tx, dict):
            continue
        symbol = (tx.get("symbol") or "").strip().upper()
        kind = (tx.get("kind") or "").strip().upper()
        trade_date = (tx.get("trade_date") or "").strip()
        if not symbol or kind not in KINDS or not trade_date:
            report["errors"].append(f"跳过一条格式不对的流水：{symbol or '?'} {trade_date or '?'}")
            continue
        key = _tx_key(tx)
        if key in have:
            report["skipped"] += 1
            continue
        try:
            store.add_transaction({
                "symbol": symbol,
                "kind": kind,
                "trade_date": trade_date,
                "shares": _num(tx.get("shares")),
                "price": _num(tx.get("price")),
                "price_currency": (tx.get("price_currency") or "").strip().upper() or None,
                "fee_eur": _num(tx.get("fee_eur"), 0) or 0,
                "amount_eur": _num(tx.get("amount_eur")),
                "note": (tx.get("note") or "").strip(),
                "account_id": existing.get((tx.get("account") or "").strip(), default_account),
            })  # created_at 由 store 按导入时间写，那是"这条记录什么时候录的"，
                # 跟交易日期无关，不必也不该从文件里搬过来
            have.add(key)
            report["transactions"] += 1
        except Exception as exc:
            report["errors"].append(f"{symbol} {trade_date}：{exc}")

    # ---- 财报点评：主键冲突就跳过，不覆盖已有的
    seen = {(r["symbol"], r.get("period_type") or "annual", str(r.get("period_key")))
            for r in store.report_summaries(None, 10000)}
    for record in data.get("report_summaries") or []:
        if not isinstance(record, dict) or not record.get("symbol"):
            continue
        key = (record["symbol"], record.get("period_type") or "annual",
               str(record.get("period_key")))
        if key in seen:
            continue
        try:
            store.put_report_summary(record)
            seen.add(key)
            report["summaries"] += 1
        except Exception as exc:
            report["errors"].append(f"点评 {key[0]} {key[2]}：{exc}")

    # ---- 追踪列表和指数：合并进配置，已有的代码不动
    if with_config:
        def merge(cfg):
            have_watch = {w["symbol"] for w in cfg["watchlist"]}
            for item in data.get("watchlist") or []:
                symbol = config.normalize_symbol((item or {}).get("symbol", ""))
                if not symbol or symbol in have_watch:
                    continue
                cfg["watchlist"].append(config.normalize_watch_item(item, cfg.get("alert")))
                have_watch.add(symbol)
                report["watchlist"] += 1
            have_idx = {i["symbol"] for i in cfg["indices"]}
            for item in data.get("indices") or []:
                symbol = config.normalize_symbol((item or {}).get("symbol", ""))
                if not symbol or symbol in have_idx:
                    continue
                cfg["indices"].append({"symbol": symbol, "name": (item.get("name") or "").strip()})
                have_idx.add(symbol)
                report["indices"] += 1
        config.update(merge)

    log.info("JSON 导入完成：%s", {k: v for k, v in report.items() if k != "errors"})
    return report
