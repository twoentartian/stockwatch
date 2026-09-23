"""告警判定：按每只股票的阈值判断涨跌幅，去重后汇总成一封邮件发出。"""
from __future__ import annotations

import logging
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import notifier
import store

log = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

BASELINE_LABEL = {"prev_close": "昨收", "today_open": "今开"}


def market_now() -> datetime:
    return datetime.now(MARKET_TZ)


def trade_date(ts: int | None = None) -> str:
    dt = datetime.fromtimestamp(ts, MARKET_TZ) if ts else market_now()
    return dt.strftime("%Y-%m-%d")


def is_market_open(now: datetime | None = None) -> bool:
    """美东时间的常规交易时段（不含节假日判断）。"""
    now = now or market_now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def baseline_value(quote: dict, baseline: str) -> float | None:
    value = quote.get("open") if baseline == "today_open" else quote.get("prev_close")
    if not value:  # 拿不到今开就退回昨收，拿不到昨收就没法算
        value = quote.get("prev_close")
    return float(value) if value else None


def change_against_baseline(quote: dict, baseline: str) -> float | None:
    base = baseline_value(quote, baseline)
    price = quote.get("price")
    if not base or price is None:
        return None
    return (float(price) - base) / base * 100.0


def evaluate(quotes: dict, cfg: dict, now_ts: int | None = None) -> list[dict]:
    """找出本轮需要告警的条目（已按冷却/每日一次去重），但还没发邮件。"""
    now_ts = int(now_ts or time.time())
    alert_cfg = cfg.get("alert", {})
    baseline = alert_cfg.get("baseline", "prev_close")

    if alert_cfg.get("market_hours_only") and not is_market_open():
        return []

    today = trade_date()
    triggered = []

    for item in cfg.get("watchlist", []):
        if not item.get("enabled", True):
            continue
        symbol = item["symbol"]
        quote = quotes.get(symbol)
        if not quote:
            continue
        change_pct = change_against_baseline(quote, baseline)
        if change_pct is None:
            continue

        up_pct = float(item.get("up_pct") or 0)
        down_pct = float(item.get("down_pct") or 0)
        direction = None
        threshold = None
        if up_pct > 0 and change_pct >= up_pct:
            direction, threshold = "up", up_pct
        elif down_pct > 0 and change_pct <= -down_pct:
            direction, threshold = "down", down_pct
        if not direction:
            continue

        key = f"{symbol}:{direction}"
        state = store.get_alert_state(key)
        if state:
            same_day = state.get("trade_date") == today
            if item.get("once_per_day", True) and same_day:
                continue
            cooldown = int(item.get("cooldown_minutes") or 0) * 60
            if same_day and cooldown and now_ts - int(state.get("last_ts") or 0) < cooldown:
                continue

        triggered.append(
            {
                "ts": now_ts,
                "symbol": symbol,
                "name": item.get("name") or quote.get("name") or symbol,
                "direction": direction,
                "change_pct": change_pct,
                "threshold": threshold,
                "price": quote.get("price"),
                "prev_close": quote.get("prev_close"),
                "open": quote.get("open"),
                "baseline": baseline,
                "currency": quote.get("currency") or "USD",
            }
        )
    return triggered


def _format_subject(triggered: list[dict]) -> str:
    if len(triggered) == 1:
        a = triggered[0]
        arrow = "涨" if a["direction"] == "up" else "跌"
        return f"{a['symbol']} {arrow} {abs(a['change_pct']):.2f}%（{a['price']:.2f}）"
    parts = [f"{a['symbol']} {a['change_pct']:+.2f}%" for a in triggered[:4]]
    more = "" if len(triggered) <= 4 else f" 等 {len(triggered)} 只"
    return "股价异动：" + "、".join(parts) + more


def _format_bodies(triggered: list[dict]) -> tuple[str, str]:
    stamp = market_now().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [f"触发时间（美东）：{stamp}", ""]
    rows = []
    for a in triggered:
        label = BASELINE_LABEL.get(a["baseline"], a["baseline"])
        base = a.get("prev_close") if a["baseline"] == "prev_close" else (a.get("open") or a.get("prev_close"))
        base_txt = f"{base:.2f}" if base else "-"
        lines.append(
            f"{a['symbol']}（{a['name']}）  现价 {a['price']:.2f} {a['currency']}，"
            f"相对{label} {base_txt} 变动 {a['change_pct']:+.2f}%，"
            f"触发阈值 {'涨' if a['direction'] == 'up' else '跌'} {a['threshold']:.2f}%"
        )
        color = "#d93025" if a["direction"] == "up" else "#137333"
        rows.append(
            f"<tr><td style='padding:6px 10px'><b>{a['symbol']}</b><br>"
            f"<span style='color:#666;font-size:12px'>{a['name']}</span></td>"
            f"<td style='padding:6px 10px;text-align:right'>{a['price']:.2f} {a['currency']}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:{color};font-weight:bold'>"
            f"{a['change_pct']:+.2f}%</td>"
            f"<td style='padding:6px 10px;text-align:right;color:#666'>{label} {base_txt}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:#666'>"
            f"{'涨' if a['direction'] == 'up' else '跌'} {a['threshold']:.2f}%</td></tr>"
        )
    lines.append("")
    lines.append("—— StockWatch 自动发送")

    html = (
        "<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif\">"
        f"<h3 style='margin:0 0 4px'>股价异动提醒</h3>"
        f"<div style='color:#666;font-size:12px;margin-bottom:12px'>触发时间（美东）：{stamp}</div>"
        "<table style='border-collapse:collapse;font-size:14px' border='0'>"
        "<tr style='background:#f2f4f7'>"
        "<th style='padding:6px 10px;text-align:left'>代码</th>"
        "<th style='padding:6px 10px;text-align:right'>现价</th>"
        "<th style='padding:6px 10px;text-align:right'>涨跌幅</th>"
        "<th style='padding:6px 10px;text-align:right'>基准</th>"
        "<th style='padding:6px 10px;text-align:right'>阈值</th></tr>"
        + "".join(rows)
        + "</table>"
        "<p style='color:#888;font-size:12px;margin-top:14px'>—— StockWatch 自动发送</p></div>"
    )
    return "\n".join(lines), html


# --------------------------------------------------------------------------- 持仓收益里程碑

def evaluate_positions(positions: list[dict], cfg: dict, now_ts: int | None = None) -> list[dict]:
    """持仓累计收益率突破里程碑（默认每 100%）时提醒一次。

    跟股价告警不是一回事：那个看的是当天涨跌，这个看的是**这笔投资从买入到现在**
    赚了多少。按里程碑去重——翻倍提醒一次，再翻到 200% 再提醒一次，
    中间来回波动不会反复发。
    """
    now_ts = int(now_ts or time.time())
    threshold = float((cfg.get("alert") or {}).get("position_return_pct") or 0)
    if threshold <= 0:
        return []

    triggered = []
    for pos in positions:
        if not pos.get("has_quote") or pos.get("return_pct") is None:
            continue
        if pos.get("shares_held", 0) <= 0:      # 已清仓的不再提醒
            continue
        milestone = int(pos["return_pct"] // threshold) * int(threshold)
        if milestone < threshold:
            continue

        key = f"pos:{pos.get('account_id')}:{pos['symbol']}"
        state = store.get_alert_state(key)
        reached = float((state or {}).get("change_pct") or 0)
        if milestone <= reached:
            continue

        triggered.append({
            "ts": now_ts,
            "key": key,
            "symbol": pos["symbol"],
            "name": pos.get("name") or pos["symbol"],
            "account": pos.get("account") or "",
            "milestone": milestone,
            "return_pct": pos["return_pct"],
            "invested_eur": pos["invested_eur"],
            "market_value_eur": pos["market_value_eur"],
            "dividends_eur": pos["dividends_eur"],
            "total_return_eur": pos["total_return_eur"],
            "holding_days": pos.get("holding_days"),
            "annualized_pct": pos.get("annualized_pct"),
        })
    return triggered


def notify_positions(triggered: list[dict], cfg: dict) -> list[dict]:
    """把里程碑汇总成一封邮件发出，并记下已达到的档位。"""
    if not triggered:
        return []

    email_cfg = cfg.get("email", {})
    if not email_cfg.get("enabled"):
        status, detail = "skipped", "邮件通知未启用"
    else:
        if len(triggered) == 1:
            a = triggered[0]
            subject = f"{a['symbol']} 收益率已达 {a['return_pct']:.0f}%（{a['account']}）"
        else:
            subject = "持仓收益里程碑：" + "、".join(
                f"{a['symbol']} {a['return_pct']:.0f}%" for a in triggered[:4])

        lines = [f"触发时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
        rows = []
        for a in triggered:
            annual = "—" if a["annualized_pct"] is None else f"{a['annualized_pct']:+.1f}%"
            lines.append(
                f"{a['symbol']}（{a['name']}）· {a['account']}\n"
                f"  累计收益率 {a['return_pct']:+.1f}%，突破 {a['milestone']}% 门槛\n"
                f"  投入 €{a['invested_eur']:.2f} → 市值 €{a['market_value_eur']:.2f}"
                f"，已收股息 €{a['dividends_eur']:.2f}，合计赚 €{a['total_return_eur']:.2f}\n"
                f"  持有 {a['holding_days']} 天，年化 {annual}"
            )
            rows.append(
                f"<tr><td style='padding:6px 10px'><b>{a['symbol']}</b><br>"
                f"<span style='color:#666;font-size:12px'>{a['name']} · {a['account']}</span></td>"
                f"<td style='padding:6px 10px;text-align:right;color:#d93025;font-weight:bold'>"
                f"{a['return_pct']:+.1f}%</td>"
                f"<td style='padding:6px 10px;text-align:right'>€{a['invested_eur']:,.2f}</td>"
                f"<td style='padding:6px 10px;text-align:right'>€{a['market_value_eur']:,.2f}</td>"
                f"<td style='padding:6px 10px;text-align:right'>€{a['total_return_eur']:,.2f}</td>"
                f"<td style='padding:6px 10px;text-align:right;color:#666'>{a['holding_days']} 天</td></tr>"
            )

        html = (
            "<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif\">"
            "<h3 style='margin:0 0 10px'>持仓收益里程碑</h3>"
            "<table style='border-collapse:collapse;font-size:14px'>"
            "<tr style='background:#f2f4f7'>"
            "<th style='padding:6px 10px;text-align:left'>标的</th>"
            "<th style='padding:6px 10px;text-align:right'>累计收益率</th>"
            "<th style='padding:6px 10px;text-align:right'>投入</th>"
            "<th style='padding:6px 10px;text-align:right'>市值</th>"
            "<th style='padding:6px 10px;text-align:right'>总收益</th>"
            "<th style='padding:6px 10px;text-align:right'>持有</th></tr>"
            + "".join(rows) +
            "</table><p style='color:#888;font-size:12px;margin-top:14px'>"
            "收益率 =（市值 + 已收股息 + 卖出回款 − 总投入）÷ 总投入。—— StockWatch 自动发送</p></div>"
        )
        ok, message = notifier.send_mail(email_cfg, subject, "\n".join(lines), html)
        status, detail = ("sent" if ok else "failed"), message

    for alert in triggered:
        store.insert_alert({
            "ts": alert["ts"],
            "symbol": alert["symbol"],
            "name": f"{alert['name']} · {alert['account']}",
            "direction": "milestone",
            "change_pct": alert["return_pct"],
            "threshold": alert["milestone"],
            "price": alert["market_value_eur"],
            "baseline": "position",
            "email_status": status,
            "detail": detail,
        })
        store.set_alert_state(alert["key"], alert["ts"], trade_date(alert["ts"]), alert["milestone"])
        log.info("持仓里程碑 %s %s %.1f%% -> 邮件 %s",
                 alert["account"], alert["symbol"], alert["return_pct"], status)
    return triggered


def notify(triggered: list[dict], cfg: dict) -> list[dict]:
    """把触发项汇总成一封邮件发出，并写入数据库。返回带发送结果的记录。"""
    if not triggered:
        return []

    email_cfg = cfg.get("email", {})
    if not email_cfg.get("enabled"):
        status, detail = "skipped", "邮件通知未启用"
    else:
        subject = _format_subject(triggered)
        text, html = _format_bodies(triggered)
        ok, message = notifier.send_mail(email_cfg, subject, text, html)
        status, detail = ("sent" if ok else "failed"), message

    for alert in triggered:
        alert["email_status"] = status
        alert["detail"] = detail
        store.insert_alert(alert)
        store.set_alert_state(
            f"{alert['symbol']}:{alert['direction']}",
            alert["ts"],
            trade_date(alert["ts"]),
            alert["change_pct"],
        )
        log.info(
            "告警 %s %s %.2f%% -> 邮件 %s",
            alert["symbol"], alert["direction"], alert["change_pct"], status,
        )
    return triggered
