"""汇率换算：把各币种的市值折算成欧元。

行情币种由 Yahoo 返回（美股 USD、法兰克福 EUR、伦敦是 GBp 便士）。
成本这边不需要历史汇率——买入时记的是 DeGiro 实际扣的欧元数，换汇成本已经含在里面了。
"""
from __future__ import annotations

import logging
import threading
import time

import providers
import store

log = logging.getLogger(__name__)

BASE = "EUR"
CACHE_TTL = 1800  # 汇率缓存半小时足够，本来就是给持仓估值用的

_lock = threading.Lock()
_cache: dict[str, tuple[float, int]] = {}


# 部分市场报价用的是辅币：伦敦是便士(GBp)、约堡是分(ZAc)、特拉维夫是阿高洛(ILA)。
# 注意 "GBp" 和 "GBP" 只差大小写但含义差 100 倍，必须按原样匹配，不能先转大写。
SUBUNITS = {
    "GBp": ("GBP", 0.01),
    "GBX": ("GBP", 0.01),
    "ZAc": ("ZAR", 0.01),
    "ILA": ("ILS", 0.01),
}


def _normalize(currency: str) -> tuple[str, float]:
    """返回 (标准币种, 换算系数)。"""
    raw = (currency or BASE).strip()
    if raw in SUBUNITS:
        return SUBUNITS[raw]
    return raw.upper(), 1.0


def rate_to_eur(currency: str) -> float | None:
    """1 单位该币种值多少欧元。拿不到汇率返回 None。"""
    code, factor = _normalize(currency)
    if code == BASE:
        return factor

    now = int(time.time())
    with _lock:
        hit = _cache.get(code)
        if hit and now - hit[1] < CACHE_TTL:
            return hit[0] * factor

    # Yahoo 的报价对是 EURUSD=X（1 欧元 = 多少美元），所以取倒数
    try:
        quote = providers.fetch_quote(f"{BASE}{code}=X")
        price = float(quote.get("price") or 0)
    except Exception as exc:
        log.warning("获取 %s 汇率失败: %s", code, exc)
        price = 0.0

    if price <= 0:
        # 抓不到就退回内存缓存，再不行就用磁盘上存的最后一条，总比没有强
        with _lock:
            stale = _cache.get(code)
        if stale:
            return stale[0] * factor
        stored = store.latest_fx_rate(code)
        if stored:
            log.info("%s 用磁盘上 %s 的汇率兜底", code, time.strftime("%Y-%m-%d %H:%M", time.localtime(stored["ts"])))
            with _lock:
                _cache[code] = (stored["eur_per_unit"], now)
            return stored["eur_per_unit"] * factor
        return None

    value = 1.0 / price
    with _lock:
        _cache[code] = (value, now)
    store.put_fx_rate(code, value, now)   # 落盘归档
    return value * factor


def to_eur(amount: float | None, currency: str) -> float | None:
    if amount is None:
        return None
    rate = rate_to_eur(currency)
    return None if rate is None else float(amount) * rate


def refresh(currencies) -> None:
    """轮询时顺手把用到的币种汇率刷一遍，页面就不用现抓了。"""
    for currency in {c for c in currencies if c}:
        rate_to_eur(currency)


def snapshot() -> dict:
    """当前缓存的汇率，给前端显示用。"""
    with _lock:
        return {code: {"eur_per_unit": value, "ts": ts} for code, (value, ts) in _cache.items()}
