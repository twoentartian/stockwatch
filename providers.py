"""行情数据源：Yahoo Finance 公开接口（免费、无需 API Key）。

- 行情：https://query1.finance.yahoo.com/v8/finance/chart/<symbol>
- 搜索：https://query1.finance.yahoo.com/v1/finance/search?q=<关键字>

指数写法：^GSPC 标普500、^DJI 道指、^IXIC 纳斯达克、^RUT 罗素2000、^VIX 波动率。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote as urlquote

import requests

log = logging.getLogger(__name__)

HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
TIMEOUT = 15

_session = requests.Session()
_session.headers.update(HEADERS)


class QuoteError(RuntimeError):
    pass


def _get_json(path: str, params: dict) -> dict:
    last_err: Exception | None = None
    for host in HOSTS:
        url = f"https://{host}{path}"
        try:
            resp = _session.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code != 200:
                last_err = QuoteError(f"HTTP {resp.status_code}")
                continue
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
    raise QuoteError(str(last_err) if last_err else "请求失败")


def get_json(path: str, params: dict) -> dict:
    """给其它模块复用的 Yahoo 请求入口（带 UA、超时和双域名重试）。"""
    return _get_json(path, params)


def fetch_quote(symbol: str) -> dict:
    """抓取单个代码的快照行情。失败抛 QuoteError。"""
    data = _get_json(
        f"/v8/finance/chart/{urlquote(symbol, safe='')}",
        {"range": "1d", "interval": "1d"},
    )
    chart = (data or {}).get("chart") or {}
    if chart.get("error"):
        raise QuoteError(str(chart["error"].get("description") or chart["error"]))
    results = chart.get("result") or []
    if not results:
        raise QuoteError("没有返回数据（代码可能不存在）")

    result = results[0]
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        raise QuoteError("没有最新价")

    prev_close = meta.get("chartPreviousClose")
    if prev_close is None:
        prev_close = meta.get("previousClose")

    open_price = None
    try:
        opens = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("open") or []
        opens = [x for x in opens if x is not None]
        if opens:
            open_price = float(opens[-1])
    except (IndexError, TypeError, ValueError):
        open_price = None

    change = None
    change_pct = meta.get("regularMarketChangePercent")
    if prev_close:
        change = price - prev_close
        change_pct = change / prev_close * 100.0

    return {
        "symbol": (meta.get("symbol") or symbol).upper(),
        "name": meta.get("shortName") or meta.get("longName") or symbol,
        "price": float(price),
        "prev_close": float(prev_close) if prev_close is not None else None,
        "open": open_price,
        "change": change,
        "change_pct": float(change_pct) if change_pct is not None else None,
        "day_high": meta.get("regularMarketDayHigh"),
        "day_low": meta.get("regularMarketDayLow"),
        "volume": meta.get("regularMarketVolume"),
        "currency": meta.get("currency"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "market_time": meta.get("regularMarketTime"),
        "exchange_tz": meta.get("exchangeTimezoneName"),
        "fetched_at": int(time.time()),
        "ts": int(time.time()),
    }


def fetch_quotes(symbols: list[str], max_workers: int = 6) -> tuple[dict, dict]:
    """并发抓取多个代码，返回 ({symbol: quote}, {symbol: 错误信息})。"""
    symbols = [s for s in dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip())]
    quotes: dict[str, dict] = {}
    errors: dict[str, str] = {}
    if not symbols:
        return quotes, errors

    def one(sym: str):
        try:
            return sym, fetch_quote(sym), None
        except Exception as exc:  # 单个代码失败不应影响其它代码
            log.warning("抓取 %s 失败: %s", sym, exc)
            return sym, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(symbols))) as pool:
        for sym, quote, err in pool.map(one, symbols):
            if quote:
                quotes[sym] = quote
            else:
                errors[sym] = err or "未知错误"
    return quotes, errors


def fetch_daily_bars(symbol: str, start_ts: int, end_ts: int) -> list[dict]:
    """抓一段时间的日线。用于核对历史成交价，抓回来的会存进本地归档。"""
    data = _get_json(
        f"/v8/finance/chart/{urlquote(symbol, safe='')}",
        {"period1": int(start_ts), "period2": int(end_ts), "interval": "1d"},
    )
    chart = (data or {}).get("chart") or {}
    if chart.get("error"):
        raise QuoteError(str(chart["error"].get("description") or chart["error"]))
    results = chart.get("result") or []
    if not results:
        return []

    result = results[0]
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]

    bars = []
    for i, stamp in enumerate(stamps):
        close = (quote.get("close") or [None] * len(stamps))[i]
        if close is None:
            continue
        bars.append(
            {
                "symbol": (meta.get("symbol") or symbol).upper(),
                "date": time.strftime("%Y-%m-%d", time.gmtime(int(stamp))),
                "open": (quote.get("open") or [None] * len(stamps))[i],
                "high": (quote.get("high") or [None] * len(stamps))[i],
                "low": (quote.get("low") or [None] * len(stamps))[i],
                "close": close,
                "volume": (quote.get("volume") or [None] * len(stamps))[i],
                "currency": meta.get("currency"),
                "source": "history",
            }
        )
    return bars


def fetch_news(symbol: str, limit: int = 6) -> list[dict]:
    """抓该标的最近的新闻标题，给 LLM 当市场背景用。只取标题和来源，不抓正文。"""
    try:
        data = _get_json(
            "/v1/finance/search",
            {"q": symbol, "quotesCount": 0, "newsCount": limit, "listsCount": 0},
        )
    except QuoteError:
        return []
    out = []
    for item in (data or {}).get("news") or []:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title,
                "publisher": item.get("publisher") or "",
                "ts": int(item.get("providerPublishTime") or 0),
                "link": item.get("link") or "",
            }
        )
    return out[:limit]


def search_symbols(keyword: str, limit: int = 8) -> list[dict]:
    """按关键字/公司名搜索代码，用于前端添加股票时的候选提示。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    try:
        data = _get_json(
            "/v1/finance/search",
            {"q": keyword, "quotesCount": limit, "newsCount": 0, "listsCount": 0},
        )
    except QuoteError:
        return []
    out = []
    for item in (data or {}).get("quotes") or []:
        symbol = item.get("symbol")
        if not symbol:
            continue
        out.append(
            {
                "symbol": symbol,
                "name": item.get("longname") or item.get("shortname") or "",
                "exchange": item.get("exchDisp") or item.get("exchange") or "",
                "type": item.get("typeDisp") or item.get("quoteType") or "",
            }
        )
    return out[:limit]
