"""联网检索（Tavily）。

这是整个项目里**唯一一个会把数据发到外网、并把外网内容带回来**的模块，
所以单独放一个文件，边界画清楚：

* **出去的只有一个字符串**：模型生成的查询词。它有长度上限、会被清洗成单行，
  并且每一条都写进日志（``log.info("联网检索: %s")``），事后可查。
  风险是真实存在的——模型先调 ``get_position`` 看到你的持仓金额，
  再把它写进查询词，那这段私密数据就出门了。所以查询词必须可审计，
  提示词里也明确禁止把持仓、金额、账户名写进查询。
* **回来的全是不可信内容**。网页是谁都能写的，里面完全可能埋着
  "忽略之前的指令"。所以结果一律打上警示包装后再交给模型，
  且同一轮里不存在任何能写数据或发邮件的工具（见 tools.py 顶部）。

默认只搜白名单里那些财经/监管站点。想搜全网就把白名单清空，
但那意味着随便哪个 SEO 垃圾站都能进模型的上下文。
"""
from __future__ import annotations

import logging
import re
import time

import requests

log = logging.getLogger(__name__)

ENDPOINT = "https://api.tavily.com/search"

# 查询词长度上限。正常的公司检索十几二十个字就够，给这么长已经很宽松；
# 限长本身也是一道防线——不让模型把一大段持仓明细塞进查询词发出去。
MAX_QUERY_CHARS = 160
MAX_SNIPPET_CHARS = 700

# 默认白名单：通讯社、主流财经媒体、监管机构、公司公告发布渠道。
DEFAULT_DOMAINS = [
    "reuters.com", "apnews.com", "bloomberg.com", "wsj.com", "ft.com",
    "cnbc.com", "marketwatch.com", "barrons.com", "investors.com",
    "sec.gov", "nasdaq.com", "finance.yahoo.com", "morningstar.com",
    "seekingalpha.com", "fool.com", "businesswire.com", "prnewswire.com",
    "globenewswire.com",
]


class SearchError(RuntimeError):
    pass


def is_configured(cfg: dict | None) -> bool:
    cfg = cfg or {}
    return bool(cfg.get("enabled") and cfg.get("api_key"))


def clean_query(query: str) -> str:
    """压成单行、去掉控制字符、截断。返回空串表示这条查询不能用。"""
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    query = "".join(ch for ch in query if ch.isprintable())
    return query[:MAX_QUERY_CHARS]


def normalize_domains(value) -> list[str]:
    """把用户填的域名列表规整成 Tavily 认的样子（去协议、去路径、小写）。"""
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    out, seen = [], set()
    for item in value or []:
        item = str(item).strip().lower()
        item = re.sub(r"^https?://", "", item)
        item = item.split("/")[0].strip(".")
        item = re.sub(r"^www\.", "", item)   # www.sec.gov 和 sec.gov 是同一个，别算两份
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def search(cfg: dict, query: str, topic: str = "general", days: int | None = None) -> dict:
    """搜一次，返回 {query, results:[{title,url,published,snippet}], ...}。失败抛 SearchError。"""
    if not is_configured(cfg):
        raise SearchError("联网检索未启用或没有配置 Tavily API Key")

    query = clean_query(query)
    if len(query) < 3:
        raise SearchError("查询词太短")

    topic = "news" if str(topic).lower().startswith("n") else "general"
    payload = {
        "query": query,
        "topic": topic,
        "search_depth": cfg.get("search_depth") or "basic",
        "max_results": max(1, min(int(cfg.get("max_results") or 5), 10)),
        "include_answer": False,
        "include_raw_content": False,   # 只要摘要：正文全量既费 token 又是更大的注入面
        "include_images": False,
    }
    if topic == "news":
        payload["days"] = max(1, min(int(days or cfg.get("days") or 30), 365))
    domains = normalize_domains(cfg.get("include_domains"))
    if domains:
        payload["include_domains"] = domains[:100]

    # 出门的东西全部留痕，事后能对账
    log.info("联网检索: %r（topic=%s，白名单 %d 个域名）", query, topic, len(domains))

    started = time.time()
    try:
        resp = requests.post(
            ENDPOINT,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg['api_key']}"},
            json=payload,
            timeout=float(cfg.get("timeout_s") or 30),
        )
    except requests.RequestException as exc:
        raise SearchError(f"请求失败：{type(exc).__name__}: {exc}") from exc

    if resp.status_code != 200:
        if resp.status_code in (401, 403):
            raise SearchError("Tavily API Key 无效或没有权限")
        if resp.status_code == 429:
            raise SearchError("Tavily 请求过于频繁（429）")
        if resp.status_code in (432, 433):
            raise SearchError("Tavily 额度用完了（免费额度每月 1000 次）")
        raise SearchError(f"HTTP {resp.status_code}：{resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise SearchError(f"返回格式无法解析：{exc}") from exc

    results = []
    for item in (data.get("results") or []):
        snippet = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        results.append({
            "title": (item.get("title") or "").strip()[:200],
            "url": item.get("url") or "",
            "published": (item.get("published_date") or "")[:10],
            "snippet": snippet[:MAX_SNIPPET_CHARS],
        })

    return {
        "query": query,
        "topic": topic,
        "results": results,
        "elapsed_s": round(time.time() - started, 1),
    }


def test_connection(cfg: dict) -> tuple[bool, str]:
    """设置页那个「测试检索」按钮用的。不抛异常。"""
    try:
        result = search(cfg, "S&P 500 market today", topic="news", days=7)
    except SearchError as exc:
        return False, str(exc)
    if not result["results"]:
        domains = normalize_domains(cfg.get("include_domains"))
        return False, ("连接成功但没搜到结果" +
                       (f"——白名单里那 {len(domains)} 个域名可能太窄了" if domains else ""))
    first = result["results"][0]
    return True, (f"检索正常（{result['elapsed_s']}s，{len(result['results'])} 条）："
                  f"{first['title'][:40]}…（{first['url'].split('/')[2] if '/' in first['url'] else ''}）")
