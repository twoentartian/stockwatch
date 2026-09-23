"""交易记录核对：拿成交当天的实际股价跟你记的单价比一比。

只做粗略核对——落在当天最高/最低价区间内就算对得上，偏离超过阈值才提醒。
核对通过的流水会在数据库里标记「已核对」，下次不再重复抓数据。

典型能抓出来的错误：股数和单价填反了、单价填成了总额、日期写错了月份、币种选错。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import fundamentals
import providers
import store

log = logging.getLogger(__name__)

# 落在当天区间内就算通过；超出区间时按偏离收盘价的比例判定
OK_TOLERANCE = 0.02      # 2% 以内认为是正常的（盘中价差、手续费摊入等）
WARN_TOLERANCE = 0.08    # 8% 以内只是提醒，再多就当有问题

# 股息预扣税的合理区间。美股对欧盟居民按税收协定通常预扣 15%（没交 W-8BEN 则是 30%），
# 德国本土股息预扣 26.375%。这里不判断你该交多少税，只看隐含税率是否落在常识范围内——
# 超出范围基本说明股数、日期或标的记错了，而不是税的问题。
# 实付金额与「股数 × 单价 × 汇率」的允许偏差。汇率点差、盘中成交价与收盘价的差异
# 都会造成小幅出入，但忘了换汇会造成 15%~25% 的系统性偏差。
AMOUNT_OK = 0.03
AMOUNT_WARN = 0.10

TYPICAL_TAX_PCT = 15.0
MIN_TAX_PCT = -2.0       # 实收比毛额还多 → 肯定哪里不对
WARN_TAX_PCT = 35.0      # 超过 35% 提醒一下
MAX_TAX_PCT = 60.0       # 超过 60% 当作记错

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_BAD = "bad"
STATUS_SKIP = "skip"


def _ensure_bars(symbol: str, date: str) -> dict | None:
    """先查本地归档，没有再去抓一段回来存上。"""
    bar = store.daily_bar_near(symbol, date)
    if bar:
        return bar
    try:
        target = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None

    start = int((target - timedelta(days=8)).timestamp())
    end = int((target + timedelta(days=8)).timestamp())
    try:
        bars = providers.fetch_daily_bars(symbol, start, end)
    except providers.QuoteError as exc:
        log.warning("%s %s 历史日线抓取失败: %s", symbol, date, exc)
        return None
    if bars:
        store.upsert_daily_bars(bars)
    return store.daily_bar_near(symbol, date)


def _rate_on(day: str, from_ccy: str, to_ccy: str) -> float | None:
    """当天 1 单位 from_ccy 值多少 to_ccy。历史汇率同样走日线接口并落盘缓存。"""
    if from_ccy == to_ccy:
        return 1.0
    bar = _ensure_bars(f"{from_ccy}{to_ccy}=X", day)
    if bar and bar.get("close"):
        return float(bar["close"])
    inverse = _ensure_bars(f"{to_ccy}{from_ccy}=X", day)
    if inverse and inverse.get("close"):
        return 1.0 / float(inverse["close"])
    return None


def _transposed_date(day: str) -> str | None:
    """把 YYYY-MM-DD 的月和日对调，用来提示常见的手滑（06-12 打成 12-06）。"""
    try:
        year, month, date_ = day.split("-")
        swapped = f"{year}-{date_}-{month}"
        datetime.strptime(swapped, "%Y-%m-%d")
        return swapped if swapped != day else None
    except ValueError:
        return None


def _shares_on(symbol: str, account_id, on_date: str) -> float:
    """某账户在某天收盘时持有多少股（按除息日算应得的分红）。"""
    total = 0.0
    for tx in store.transactions(symbol, account_id):
        if tx["trade_date"] > on_date:
            continue
        kind = (tx.get("kind") or "").upper()
        if kind == "BUY":
            total += float(tx.get("shares") or 0)
        elif kind == "SELL":
            total -= float(tx.get("shares") or 0)
    return total


def _dividend_before(symbol: str, pay_date: str, lookback_days: int = 100) -> dict | None:
    """找这笔到账最可能对应的那次派息。

    Yahoo 给的是**除息日**，钱一般在除息日之后两周到两个月才到账，所以往前找。
    """
    try:
        data = fundamentals.load(symbol)
    except Exception as exc:
        log.warning("%s 分红历史加载失败: %s", symbol, exc)
        return None

    try:
        pay = datetime.strptime(pay_date, "%Y-%m-%d")
    except ValueError:
        return None

    best = None
    for event in data.get("dividend_events") or []:
        ex = datetime.utcfromtimestamp(event["ts"])
        gap = (pay - ex).days
        # 允许 3 天的负数容差：有人按除息日而不是到账日记账
        if -3 <= gap <= lookback_days:
            if best is None or gap < best[0]:
                best = (gap, event, ex)
    if not best:
        return None
    gap, event, ex = best
    return {
        "ex_date": ex.strftime("%Y-%m-%d"),
        "amount": float(event["amount"]),
        "gap_days": gap,
        "currency": data.get("currency") or "",
    }


def check_dividend(tx: dict) -> dict:
    """核对一笔股息：用官方每股派息额 × 除息日持股数算出毛额，再看实收差多少。"""
    net_eur = float(tx.get("amount_eur") or 0)
    if net_eur <= 0:
        return {"status": STATUS_SKIP, "detail": "没有到账金额"}

    event = _dividend_before(tx["symbol"], tx["trade_date"])
    if not event:
        return {
            "status": STATUS_BAD,
            "detail": f"{tx['trade_date']} 往前 100 天内查不到 {tx['symbol']} 的派息记录",
        }

    shares = _shares_on(tx["symbol"], tx.get("account_id"), event["ex_date"])
    if shares <= 0:
        return {
            "status": STATUS_BAD,
            "detail": f"除息日 {event['ex_date']} 时这个账户没有 {tx['symbol']} 持仓，"
                      f"对不上这笔 €{net_eur:.2f}",
        }

    currency = (event["currency"] or "EUR").upper()
    gross = shares * event["amount"]
    rate = _rate_on(tx["trade_date"], currency, "EUR") if currency != "EUR" else 1.0
    if rate is None:
        return {"status": STATUS_SKIP, "detail": f"取不到 {tx['trade_date']} 的 {currency}/EUR 汇率"}

    gross_eur = gross * rate
    if gross_eur <= 0:
        return {"status": STATUS_SKIP, "detail": "算不出毛额"}

    tax_pct = (1 - net_eur / gross_eur) * 100
    base = (
        f"{shares:g} 股 × 每股 {event['amount']:.4f} {currency}"
        f"（除息日 {event['ex_date']}，{event['gap_days']} 天后到账）= 毛额 €{gross_eur:.2f}，"
        f"实收 €{net_eur:.2f}，隐含预扣税 {tax_pct:.1f}%"
    )

    if tax_pct < MIN_TAX_PCT:
        detail = base + f"——实收比毛额还多 €{net_eur - gross_eur:.2f}，股数或日期可能不对"
        # 多账户时常见的记法问题：一笔到账其实包含了全家的股数，却只记在一个账户下
        household = _shares_on(tx["symbol"], None, event["ex_date"])
        if household > shares:
            whole = household * event["amount"] * rate
            whole_tax = (1 - net_eur / whole) * 100 if whole > 0 else None
            if whole_tax is not None and MIN_TAX_PCT <= whole_tax <= WARN_TAX_PCT:
                detail += (
                    f"；但按所有账户合计 {household:g} 股算，毛额 €{whole:.2f}、"
                    f"隐含税率 {whole_tax:.1f}%——这笔是不是把几个账户的股息记在一起了？"
                )
        return {"status": STATUS_BAD, "detail": detail}
    if tax_pct > MAX_TAX_PCT:
        return {"status": STATUS_BAD, "detail": base + "——扣得太多，核对一下是不是漏记了股数或记串了标的"}
    if tax_pct > WARN_TAX_PCT:
        return {"status": STATUS_WARN, "detail": base + f"——高于常见的 {TYPICAL_TAX_PCT:.0f}% 预扣",
                "tax_pct": tax_pct}
    return {"status": STATUS_OK, "detail": base, "tax_pct": tax_pct}


def check_amount(tx: dict) -> dict | None:
    """核对「实付欧元」跟「股数 × 单价 × 当天汇率」对不对得上。

    价格核对只能证明单价没写错，证明不了总额没写错。最常见的错法是**忘了换汇**：
    买美股时直接把 股数 × 美元单价 当成欧元填进去，金额会偏高 15%~25%（取决于汇率），
    而单价本身完全正确、照样能通过价格核对。
    """
    shares = tx.get("shares")
    price = tx.get("price")
    amount = tx.get("amount_eur")
    if not shares or not price or not amount:
        return None

    currency = (tx.get("price_currency") or "EUR").upper()
    rate = _rate_on(tx["trade_date"], currency, "EUR") if currency != "EUR" else 1.0
    if rate is None:
        return None

    fee = float(tx.get("fee_eur") or 0)
    gross_eur = float(shares) * float(price) * rate
    kind = (tx.get("kind") or "").upper()
    expected = gross_eur + fee if kind == "BUY" else gross_eur - fee
    if expected <= 0:
        return None

    deviation = abs(float(amount) - expected) / expected
    base = (f"实付 €{amount:.2f}，按 {shares:g} × {price:.4f} {currency}"
            f"{'' if currency == 'EUR' else f' × {rate:.4f}'} "
            f"{'+' if kind == 'BUY' else '-'} 手续费 €{fee:.2f} 应为 €{expected:.2f}")

    if deviation <= AMOUNT_OK:
        return {"status": STATUS_OK, "detail": base + "，吻合"}

    # 典型错法：把外币金额当欧元填了，差额刚好等于汇率
    if currency != "EUR":
        no_fx = float(shares) * float(price) + (fee if kind == "BUY" else -fee)
        if abs(float(amount) - no_fx) / no_fx <= 0.01:
            return {
                "status": STATUS_BAD,
                "detail": base + f"——你填的 €{amount:.2f} 正好等于 {shares:g} × {price:.4f} "
                                 f"**{currency}** + 手续费，**漏了换汇**。"
                                 f"按当天汇率应该是 €{expected:.2f}，多记了 €{amount - expected:.2f}",
            }

    detail = base + f"，相差 {deviation * 100:.1f}%"
    if deviation <= AMOUNT_WARN:
        return {"status": STATUS_WARN, "detail": detail + "（汇率点差和费用可能造成小幅出入）"}
    return {"status": STATUS_BAD, "detail": detail}


def _merge(*results) -> dict:
    """几项检查合并成一条结论：取最差的状态，说明拼在一起。"""
    order = {STATUS_OK: 0, STATUS_SKIP: 1, STATUS_WARN: 2, STATUS_BAD: 3}
    items = [r for r in results if r]
    if not items:
        return {"status": STATUS_SKIP, "detail": "没有可核对的内容"}
    worst = max(items, key=lambda r: order.get(r["status"], 0))
    details = "；".join(r["detail"] for r in items)
    out = {"status": worst["status"], "detail": details}
    if worst.get("tax_pct") is not None:
        out["tax_pct"] = worst["tax_pct"]
    return out


def check_price(tx: dict) -> dict:
    """核对「每股价」跟成交当天的实际行情区间。"""
    price = tx.get("price")
    if not price:
        return {"status": STATUS_SKIP, "detail": "没有填每股价，无法核对"}

    today = datetime.now().strftime("%Y-%m-%d")
    if tx["trade_date"] > today:
        hint = _transposed_date(tx["trade_date"])
        detail = f"成交日 {tx['trade_date']} 在未来"
        if hint:
            detail += f"，是不是想写 {hint}？"
        return {"status": STATUS_BAD, "detail": detail}

    bar = _ensure_bars(tx["symbol"], tx["trade_date"])
    if not bar:
        detail = f"{tx['symbol']} 在 {tx['trade_date']} 附近没有行情数据（可能当时还没上市）"
        hint = _transposed_date(tx["trade_date"])
        if hint and hint <= today and _ensure_bars(tx["symbol"], hint):
            detail += f"；但 {hint} 有数据，是不是月和日写反了？"
        return {"status": STATUS_BAD, "detail": detail}

    bar_currency = (bar.get("currency") or "").upper()
    tx_currency = (tx.get("price_currency") or "").upper()

    # 币种不同不代表记错了——DeGiro 给你看的就是折算后的欧元价，按当天汇率换过来再比
    fx_note = ""
    if tx_currency and bar_currency and tx_currency != bar_currency:
        rate = _rate_on(bar["date"], tx_currency, bar_currency)
        if rate is None:
            return {
                "status": STATUS_SKIP,
                "detail": f"取不到 {bar['date']} 的 {tx_currency}/{bar_currency} 汇率，没法比",
            }
        price = price * rate
        fx_note = f"（{tx['price']:.2f} {tx_currency} × {rate:.4f} = {price:.2f} {bar_currency}）"

    low, high, close = bar.get("low"), bar.get("high"), bar.get("close")
    day = bar["date"]
    same_day = day == tx["trade_date"]
    day_note = f"{day} 区间 {low:.2f}–{high:.2f}" + ("" if same_day else "（最近交易日）")

    if low is not None and high is not None and low <= price <= high:
        return {"status": STATUS_OK, "detail": f"{price:.2f} 落在 {day_note} 内{fx_note}"}

    if not close:
        return {"status": STATUS_SKIP, "detail": "当天数据不完整"}

    # 区间外：看离最近的那一端差多少
    bound = high if (high is not None and price > high) else low
    deviation = abs(price - bound) / close if bound else abs(price - close) / close
    detail = f"{price:.2f} 偏离 {day_note} 约 {deviation * 100:.1f}%{fx_note}"

    if deviation <= OK_TOLERANCE:
        return {"status": STATUS_OK, "detail": detail}
    if deviation <= WARN_TOLERANCE:
        return {"status": STATUS_WARN, "detail": detail}

    ratio = price / close if close else 0
    hints = []
    if tx.get("shares") and abs(ratio - float(tx["shares"])) / max(float(tx["shares"]), 1) < 0.1:
        hints.append("看着像把总金额填成了每股价")
    if 0.9 < ratio * 100 < 1.1:
        hints.append("看着像差了 100 倍（便士/元的单位问题）")
    if hints:
        detail += "；" + "、".join(hints)
    return {"status": STATUS_BAD, "detail": detail}


def check_one(tx: dict) -> dict:
    """核对一条流水。买卖要同时过「单价」和「总额」两关——单价对不代表总额对。"""
    if (tx.get("kind") or "").upper() == "DIV":
        return check_dividend(tx)
    return _merge(check_price(tx), check_amount(tx))


def check_all(only_unchecked: bool = True, tx_id: int | None = None) -> dict:
    """批量核对。默认跳过已经核对通过的，避免重复抓数据。"""
    rows = store.transactions()
    if tx_id:
        rows = [t for t in rows if t["id"] == int(tx_id)]
    elif only_unchecked:
        rows = [t for t in rows if t.get("check_status") != STATUS_OK]

    summary = {STATUS_OK: 0, STATUS_WARN: 0, STATUS_BAD: 0, STATUS_SKIP: 0}
    problems = []
    tax_rates = []
    now = int(time.time())

    for tx in rows:
        result = check_one(tx)
        summary[result["status"]] = summary.get(result["status"], 0) + 1
        if result.get("tax_pct") is not None:
            tax_rates.append(result["tax_pct"])
        # 核对通过的才算「已核对」，有问题的留着下次再查
        checked_at = now if result["status"] == STATUS_OK else None
        store.set_tx_check(tx["id"], result["status"], result["detail"], checked_at)
        if result["status"] in (STATUS_WARN, STATUS_BAD):
            problems.append(
                {
                    "id": tx["id"],
                    "symbol": tx["symbol"],
                    "trade_date": tx["trade_date"],
                    "status": result["status"],
                    "detail": result["detail"],
                }
            )

    result = {"checked": len(rows), "summary": summary, "problems": problems}
    if tax_rates:
        ordered = sorted(tax_rates)
        result["tax"] = {
            "count": len(ordered),
            "median_pct": ordered[len(ordered) // 2],
            "min_pct": ordered[0],
            "max_pct": ordered[-1],
        }
    return result
