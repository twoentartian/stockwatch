"""持仓与收益计算：成本、市值、盈亏、年化（XIRR）。

年化用的是资金加权收益率（XIRR）：把每笔买入当作现金流出、卖出和股息当作流入、
当前市值当作今天一次性收回，求让净现值为零的年利率。这样分多次买入、
中途收过股息的情况都能算对，等价于 Excel 的 XIRR 函数。
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import fx

log = logging.getLogger(__name__)

DAYS_PER_YEAR = 365.0
# 持有不足这个天数时，年化是把几天的波动放大到一年，参考意义很低，前端会标注
MIN_RELIABLE_DAYS = 30


def parse_date(value: str) -> date | None:
    try:
        return datetime.strptime((value or "").strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _npv(rate: float, flows: list[tuple[date, float]], t0: date) -> float:
    total = 0.0
    for when, amount in flows:
        years = (when - t0).days / DAYS_PER_YEAR
        total += amount / ((1.0 + rate) ** years)
    return total


def xirr(flows: list[tuple[date, float]], guess_lo: float = -0.9999, guess_hi: float = 10.0):
    """求内部收益率（年化）。无解或输入不合法时返回 None。"""
    flows = [(d, float(a)) for d, a in flows if d and a]
    if len(flows) < 2:
        return None
    if not (any(a > 0 for _, a in flows) and any(a < 0 for _, a in flows)):
        return None  # 全是流入或全是流出，没有收益率可言

    t0 = min(d for d, _ in flows)
    lo, hi = guess_lo, guess_hi
    f_lo, f_hi = _npv(lo, flows, t0), _npv(hi, flows, t0)

    # 收益率极高时（短期暴涨）默认上界不够，往上扩几次
    tries = 0
    while f_lo * f_hi > 0 and tries < 8:
        hi *= 4
        f_hi = _npv(hi, flows, t0)
        tries += 1
    if f_lo * f_hi > 0:
        return None

    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = _npv(mid, flows, t0)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def _price_eur(quote: dict | None) -> tuple[float | None, float | None]:
    """返回 (原币种价格, 折合欧元价格)。"""
    if not quote or quote.get("price") is None:
        return None, None
    price = float(quote["price"])
    return price, fx.to_eur(price, quote.get("currency") or "EUR")


def build_position(
    symbol: str,
    txs: list[dict],
    quote: dict | None,
    name: str = "",
    account: dict | None = None,
) -> dict:
    """把某个账户下某个代码的所有流水汇总成一条持仓记录。

    拿不到行情时，市值和收益一律留空（None），绝不当成市值 0 —— 那会算出 -100% 的假亏损。
    """
    bought_shares = sold_shares = 0.0
    invested_eur = proceeds_eur = dividends_eur = fees_eur = 0.0
    first_buy: date | None = None
    last_activity: date | None = None
    flows: list[tuple[date, float]] = []

    for tx in txs:
        when = parse_date(tx.get("trade_date"))
        if when and (last_activity is None or when > last_activity):
            last_activity = when
        amount = float(tx.get("amount_eur") or 0.0)
        fees_eur += float(tx.get("fee_eur") or 0.0)
        kind = (tx.get("kind") or "").upper()

        if kind == "BUY":
            bought_shares += float(tx.get("shares") or 0.0)
            invested_eur += amount
            if when and (first_buy is None or when < first_buy):
                first_buy = when
            if when:
                flows.append((when, -amount))
        elif kind == "SELL":
            sold_shares += float(tx.get("shares") or 0.0)
            proceeds_eur += amount
            if when:
                flows.append((when, amount))
        elif kind == "DIV":
            dividends_eur += amount
            if when:
                flows.append((when, amount))

    shares_held = bought_shares - sold_shares
    price, price_eur = _price_eur(quote)
    market_value_eur = price_eur * shares_held if price_eur is not None else None

    # 总收益 = 现在手上的市值 + 已卖出收回的钱 + 收到的股息 - 总共投进去的钱
    total_return_eur = None
    if market_value_eur is not None:
        total_return_eur = market_value_eur + proceeds_eur + dividends_eur - invested_eur
    return_pct = (total_return_eur / invested_eur * 100.0) if (total_return_eur is not None and invested_eur) else None

    today = date.today()
    # 已清仓的持仓，持有天数要停在最后一笔交易那天，不能继续往上涨
    closed = shares_held <= 1e-9 and bought_shares > 0
    end_day = last_activity if (closed and last_activity) else today
    holding_days = (end_day - first_buy).days if first_buy else None

    annualized = None
    if market_value_eur is not None and flows:
        annualized = xirr(flows + [(today, market_value_eur)])
    elif flows:
        annualized = xirr(flows)

    avg_cost_eur = (invested_eur / bought_shares) if bought_shares else None
    # 摊薄成本：把已收股息和卖出回款从成本里扣掉，剩下的分摊到还持有的股数上
    net_cost_eur = invested_eur - dividends_eur - proceeds_eur
    breakeven_eur = (net_cost_eur / shares_held) if shares_held > 0 else None

    unrealized_eur = None
    if market_value_eur is not None and avg_cost_eur is not None:
        unrealized_eur = market_value_eur - avg_cost_eur * shares_held

    return {
        "symbol": symbol,
        "name": name or (quote or {}).get("name") or symbol,
        "account_id": (account or {}).get("id"),
        "account": (account or {}).get("name") or "",
        "has_quote": price is not None,
        "currency": (quote or {}).get("currency") or "",
        "shares_held": shares_held,
        "bought_shares": bought_shares,
        "sold_shares": sold_shares,
        "price": price,
        "price_eur": price_eur,
        "market_value_eur": market_value_eur,
        "invested_eur": invested_eur,
        "proceeds_eur": proceeds_eur,
        "dividends_eur": dividends_eur,
        "fees_eur": fees_eur,
        "avg_cost_eur": avg_cost_eur,
        "breakeven_eur": breakeven_eur,
        "unrealized_eur": unrealized_eur,
        "total_return_eur": total_return_eur,
        "return_pct": return_pct,
        "annualized_pct": annualized * 100.0 if annualized is not None else None,
        "annualized_reliable": bool(holding_days and holding_days >= MIN_RELIABLE_DAYS),
        "holding_days": holding_days,
        "closed": closed,
        "first_buy": first_buy.isoformat() if first_buy else None,
        "last_activity": last_activity.isoformat() if last_activity else None,
        "tx_count": len(txs),
    }


def build_all(
    all_txs: list[dict],
    quotes: dict,
    names: dict | None = None,
    accounts: dict | None = None,
) -> dict:
    """按「账户 + 代码」汇总持仓，再算一个整体合计。"""
    names = names or {}
    accounts = accounts or {}

    grouped: dict[tuple, list[dict]] = {}
    for tx in all_txs:
        grouped.setdefault((tx.get("account_id"), tx["symbol"]), []).append(tx)

    positions = []
    for (account_id, symbol), txs in sorted(
        grouped.items(), key=lambda kv: (accounts.get(kv[0][0], {}).get("name", ""), kv[0][1])
    ):
        positions.append(
            build_position(
                symbol, txs, quotes.get(symbol), names.get(symbol, ""), accounts.get(account_id)
            )
        )

    # 有行情的才计入市值和收益，没行情的单独列出来提醒，避免算出假的亏损
    priced = [p for p in positions if p["has_quote"]]
    missing = [p["symbol"] for p in positions if not p["has_quote"]]

    totals = {
        "invested_eur": sum(p["invested_eur"] for p in positions),
        "proceeds_eur": sum(p["proceeds_eur"] for p in positions),
        "dividends_eur": sum(p["dividends_eur"] for p in positions),
        "fees_eur": sum(p["fees_eur"] for p in positions),
        "market_value_eur": sum(p["market_value_eur"] for p in priced),
        "priced_invested_eur": sum(p["invested_eur"] for p in priced),
        "positions": len(positions),
        "missing_quotes": sorted(set(missing)),
    }

    if missing:
        # 只要有一只算不出市值，整体收益就是不完整的，宁可不给数
        totals["total_return_eur"] = None
        totals["return_pct"] = None
        totals["annualized_pct"] = None
        return {"positions": positions, "totals": totals}

    totals["total_return_eur"] = (
        totals["market_value_eur"] + totals["proceeds_eur"] + totals["dividends_eur"] - totals["invested_eur"]
    )
    totals["return_pct"] = (
        totals["total_return_eur"] / totals["invested_eur"] * 100.0 if totals["invested_eur"] else None
    )

    # 组合年化：把所有股票的现金流合在一起算一个 XIRR
    flows: list[tuple[date, float]] = []
    for tx in all_txs:
        when = parse_date(tx.get("trade_date"))
        amount = float(tx.get("amount_eur") or 0.0)
        kind = (tx.get("kind") or "").upper()
        if not when or not amount:
            continue
        flows.append((when, -amount if kind == "BUY" else amount))
    if flows and totals["market_value_eur"]:
        rate = xirr(flows + [(date.today(), totals["market_value_eur"])])
        totals["annualized_pct"] = rate * 100.0 if rate is not None else None
    else:
        totals["annualized_pct"] = None

    return {"positions": positions, "totals": totals}
