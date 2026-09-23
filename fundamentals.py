"""基本面数据：年度财报、分红历史、长期股价。

数据来自 Yahoo 的两个公开接口：
  - fundamentals-timeseries：年度营收/利润/现金流等，免费层只给最近 4 个财年
  - v8/finance/chart?events=div：完整的分红派发历史（IBM 能回溯到 1962 年）

注意 quoteSummary 那套（市值、PE 等）现在要 crumb 才能调，这里刻意没用。
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import quote as urlquote

import providers
import store

log = logging.getLogger(__name__)

CACHE_TTL = 12 * 3600
# payload 结构变了就把它 +1，旧缓存会自动失效重抓——
# 否则新增字段（比如 quarterly）在缓存过期前一直是空的
CACHE_VERSION = 3

# 左边是 Yahoo 的字段名，右边是我们内部用的名字
ANNUAL_TYPES = {
    "annualTotalRevenue": "revenue",
    "annualGrossProfit": "gross_profit",
    "annualOperatingIncome": "operating_income",
    "annualNetIncome": "net_income",
    "annualDilutedEPS": "eps",
    "annualOperatingCashFlow": "operating_cash_flow",
    "annualFreeCashFlow": "free_cash_flow",
    "annualCashDividendsPaid": "dividends_paid",
    "annualStockholdersEquity": "equity",
    "annualTotalDebt": "total_debt",
}

# 季报用同一套字段，只是前缀不同
QUARTERLY_TYPES = {
    key.replace("annual", "quarterly", 1): value for key, value in ANNUAL_TYPES.items()
}


def _add_margins(row: dict) -> None:
    """派生利润率，分母为 0 或缺字段时留空。"""
    revenue = row.get("revenue")
    if not revenue:
        return
    if row.get("net_income") is not None:
        row["net_margin"] = row["net_income"] / revenue * 100.0
    if row.get("gross_profit") is not None:
        row["gross_margin"] = row["gross_profit"] / revenue * 100.0
    if row.get("operating_income") is not None:
        row["operating_margin"] = row["operating_income"] / revenue * 100.0


def _fetch_series(symbol: str, type_map: dict, period2: int | None = None) -> tuple[dict, str | None]:
    """抓一组 fundamentals-timeseries 指标，按期末日聚合。

    period2 是"截止到哪天"——接口每次最多回 5 期，给个更早的截止日就能往前翻页。
    """
    data = providers.get_json(  # 复用带 UA 和双域名重试的会话
        f"/ws/fundamentals-timeseries/v1/finance/timeseries/{urlquote(symbol, safe='')}",
        {
            "symbol": symbol,
            "type": ",".join(type_map),
            "period1": 946684800,          # 2000-01-01，实际能给多少由 Yahoo 决定
            "period2": int(period2 or time.time() + 86400),
            "merge": "false",
        },
    )

    by_date: dict[str, dict] = defaultdict(dict)
    currency = None
    for series in ((data or {}).get("timeseries") or {}).get("result") or []:
        yahoo_type = (series.get("meta") or {}).get("type") or []
        if not yahoo_type:
            continue
        field = type_map.get(yahoo_type[0])
        if not field:
            continue
        for point in series.get(yahoo_type[0]) or []:
            if not point:
                continue
            as_of = point.get("asOfDate") or ""
            raw = (point.get("reportedValue") or {}).get("raw")
            if not as_of or raw is None:
                continue
            by_date[as_of]["as_of"] = as_of
            by_date[as_of][field] = float(raw)
            currency = currency or point.get("currencyCode")
    return by_date, currency


def _fetch_annual(symbol: str) -> tuple[list[dict], str | None]:
    """返回 (按年份升序的财报列表, 报表币种)。"""
    by_date, currency = _fetch_series(symbol, ANNUAL_TYPES)
    rows = []
    for as_of in sorted(by_date):
        row = by_date[as_of]
        row["year"] = int(as_of[:4])
        _add_margins(row)
        rows.append(row)
    return rows, currency


QUARTERLY_PAGES = 3     # 往前翻几次，每次最多多拿 5 期


def _fetch_quarterly(symbol: str) -> list[dict]:
    """最近若干个季度。

    接口**单次最多返回 5 期**，但库里存得更多（ARCC 实测到 7 期）。给一个更早的
    period2 就能往前翻一页，所以这里翻几次再合并——多拿两期意味着能多算两期同比。

    不去猜"第几财季"：各家财年起止不同，猜错比不写更糟，直接用期末日当标识。
    """
    merged: dict[str, dict] = {}
    period2: int | None = None

    for _ in range(QUARTERLY_PAGES):
        by_date, _ = _fetch_series(symbol, QUARTERLY_TYPES, period2=period2)
        fresh = [d for d in by_date if d not in merged]
        merged.update(by_date)
        if not fresh:
            break                       # 没有新东西了，再翻也是白翻
        oldest = min(by_date)
        try:
            # 下一页以本页最早那期为截止日，往前再要 5 期
            period2 = int(datetime.strptime(oldest, "%Y-%m-%d").timestamp())
        except ValueError:
            break

    rows = []
    for as_of in sorted(merged):
        row = merged[as_of]
        row["period_key"] = as_of
        row["year"] = int(as_of[:4])
        _add_margins(row)
        rows.append(row)
    return rows


def _fetch_dividends_and_price(symbol: str) -> tuple[list[dict], list[dict]]:
    """分红事件（全历史）和周线收盘价（近 5 年）。"""
    dividends: list[dict] = []
    try:
        # 别用 range=max：它会把中间整段派息记录丢掉（PFE 实测缺了 2014–2026 共 12 年），
        # 30y 返回的是完整序列，对个人持仓来说也够长了。
        data = providers.get_json(
            f"/v8/finance/chart/{urlquote(symbol, safe='')}",
            {"range": "30y", "interval": "1d", "events": "div"},
        )
        result = ((data or {}).get("chart") or {}).get("result") or [{}]
        events = (result[0].get("events") or {}).get("dividends") or {}
        dividends = sorted(
            (
                {"ts": int(item["date"]), "amount": float(item["amount"])}
                for item in events.values()
                if item.get("date") and item.get("amount") is not None
            ),
            key=lambda d: d["ts"],
        )
    except Exception as exc:
        log.warning("%s 分红历史抓取失败: %s", symbol, exc)

    prices: list[dict] = []
    try:
        data = providers.get_json(
            f"/v8/finance/chart/{urlquote(symbol, safe='')}",
            {"range": "5y", "interval": "1wk"},
        )
        result = ((data or {}).get("chart") or {}).get("result") or [{}]
        stamps = result[0].get("timestamp") or []
        closes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        prices = [
            {"ts": int(t), "close": float(c)}
            for t, c in zip(stamps, closes)
            if c is not None
        ]
    except Exception as exc:
        log.warning("%s 历史价格抓取失败: %s", symbol, exc)

    return dividends, prices


def _dividends_by_year(events: list[dict]) -> list[dict]:
    buckets: dict[int, dict] = {}
    for item in events:
        year = time.gmtime(item["ts"]).tm_year
        bucket = buckets.setdefault(year, {"year": year, "amount": 0.0, "count": 0})
        bucket["amount"] += item["amount"]
        bucket["count"] += 1
    # 当年还没派完，柱子天然比往年矮，不标出来会被误读成减配
    this_year = time.gmtime().tm_year
    for year, bucket in buckets.items():
        bucket["partial"] = year == this_year
    return [buckets[y] for y in sorted(buckets)]


def load(symbol: str, force: bool = False) -> dict:
    """取某个代码的完整基本面数据，默认走 12 小时缓存。"""
    symbol = symbol.strip().upper()
    if not force:
        cached = store.get_fundamentals(symbol, CACHE_TTL)
        if cached and cached.get("cache_version") == CACHE_VERSION:
            cached["cached"] = True
            return cached

    quote = None
    try:
        quote = providers.fetch_quote(symbol)
    except providers.QuoteError as exc:
        log.warning("%s 行情抓取失败: %s", symbol, exc)

    annual, report_currency = _fetch_annual(symbol)
    try:
        quarterly = _fetch_quarterly(symbol)
    except Exception as exc:
        log.warning("%s 季报抓取失败: %s", symbol, exc)
        quarterly = []
    dividend_events, prices = _fetch_dividends_and_price(symbol)
    by_year = _dividends_by_year(dividend_events)

    # 近 12 个月分红合计，用来算股息率
    cutoff = int(time.time()) - 365 * 86400
    ttm_dividend = sum(d["amount"] for d in dividend_events if d["ts"] >= cutoff)
    price = (quote or {}).get("price")
    dividend_yield = (ttm_dividend / price * 100.0) if (price and ttm_dividend) else None

    # 派息率：当年每股分红 / 当年每股收益
    eps_by_year = {row["year"]: row.get("eps") for row in annual}
    div_by_year = {row["year"]: row["amount"] for row in by_year if not row.get("partial")}
    payout = []
    for year in sorted(set(eps_by_year) & set(div_by_year)):
        eps, dividend = eps_by_year[year], div_by_year[year]
        if eps and eps > 0:
            payout.append({"year": year, "ratio": dividend / eps * 100.0, "eps": eps, "dividend": dividend})

    payload = {
        "symbol": symbol,
        "name": (quote or {}).get("name") or symbol,
        "exchange": (quote or {}).get("exchange"),
        "currency": (quote or {}).get("currency") or report_currency or "",
        "report_currency": report_currency or (quote or {}).get("currency") or "",
        "price": price,
        "change_pct": (quote or {}).get("change_pct"),
        "prev_close": (quote or {}).get("prev_close"),
        "annual": annual,
        "quarterly": quarterly,
        "dividends_by_year": by_year,
        "dividend_events": dividend_events[-200:],
        "payout_ratio": payout,
        "ttm_dividend": ttm_dividend or None,
        "dividend_yield": dividend_yield,
        "prices": prices,
        "fetched_at": int(time.time()),
        "cached": False,
        "cache_version": CACHE_VERSION,
    }
    store.put_fundamentals(symbol, payload)
    return payload
