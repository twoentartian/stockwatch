"""配置管理：JSON 文件持久化、默认值合并、密码脱敏。

配置文件默认为项目目录下的 config.json，可用环境变量 STOCKWATCH_CONFIG 覆盖。
所有写操作都在进程内加锁，避免 Web 请求和定时任务同时改写。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading

import websearch

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("STOCKWATCH_CONFIG", os.path.join(BASE_DIR, "config.json"))

# 前端拿到的配置里密码用它代替；回传时如果还是它，就保留原密码不动。
PASSWORD_MASK = "__UNCHANGED__"

DEFAULTS = {
    "poll_interval_minutes": 10,
    "history_keep_days": 30,
    "indices": [
        {"symbol": "^GSPC", "name": "标普500"},
        {"symbol": "^DJI", "name": "道琼斯"},
        {"symbol": "^IXIC", "name": "纳斯达克"},
        {"symbol": "^RUT", "name": "罗素2000"},
        {"symbol": "^VIX", "name": "恐慌指数"},
    ],
    "watchlist": [
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "enabled": True,
            "up_pct": 5.0,
            "down_pct": 5.0,
            "cooldown_minutes": 120,
            "once_per_day": True,
            "note": "",
        },
    ],
    "alert": {
        # prev_close = 相对昨收（即常说的“今天涨了/跌了百分之几”）
        # today_open = 相对今日开盘价
        "baseline": "prev_close",
        "market_hours_only": False,
        "default_up_pct": 5.0,
        "default_down_pct": 5.0,
        "default_cooldown_minutes": 120,
        # 持仓累计收益率每突破这个百分比提醒一次（100 = 翻倍时提醒，再到 200% 再提醒）；0 = 关闭
        "position_return_pct": 100.0,
    },
    # DeepSeek（OpenAI 兼容）。字段名跟 toy_llm_roleplay 那套保持一致。
    "llm": {
        "enabled": False,
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.3,       # 写财报总结要稳，不要发挥
        "max_tokens": 8000,     # 思考型模型会先花掉一大截额度，给小了答案会被拦腰截断
        "timeout_s": 120,
        "json_mode": False,
        "auto_summary": True,     # 发现新财年年报时自动生成
        "include_position": True, # 上下文里带上自己的持仓（只为让它理解关注点）
        "include_quarterly": True,# 季报也自动点评（年报一年一次，季报一年四次）
        "use_tools": True,        # 开函数调用：模型可按需查行情/财报/分红/持仓（全本地，不联网）
        "email_summary": True,    # 自动生成的点评发到邮箱（要邮件通知本身也启用）
        "check_interval_hours": 12,
    },
    # 联网检索（Tavily）。**整个项目里唯一会把数据发到外网的东西**，默认关闭。
    # 只在 llm.use_tools 也打开时才生效——它是以工具的形式给模型用的。
    "websearch": {
        "enabled": False,
        "provider": "tavily",
        "api_key": "",
        "max_results": 5,         # 一次检索取回几条
        "max_searches": 3,        # 一次点评最多联网几次（每次都要花钱）
        "search_depth": "basic",  # basic | advanced（advanced 更贵）
        "days": 30,               # topic=news 时默认往回搜多少天
        "timeout_s": 30,
        # 域名白名单。留空 = 搜全网，那样什么 SEO 垃圾站都可能进模型的上下文。
        "include_domains": list(websearch.DEFAULT_DOMAINS),
    },
    "email": {
        "enabled": False,
        "smtp_host": "",
        "smtp_port": 465,
        "security": "ssl",  # ssl | starttls | none
        "username": "",
        "password": "",
        "from_addr": "",
        "to_addrs": [],
        "subject_prefix": "[StockWatch]",
    },
}

_lock = threading.RLock()
_cache: dict | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    """用 override 覆盖 base，dict 递归合并，其余类型直接替换。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def normalize_watch_item(item: dict, alert_cfg: dict | None = None) -> dict:
    alert_cfg = alert_cfg or DEFAULTS["alert"]
    return {
        "symbol": normalize_symbol(item.get("symbol", "")),
        "name": (item.get("name") or "").strip(),
        "enabled": bool(item.get("enabled", True)),
        "up_pct": _to_float(item.get("up_pct"), alert_cfg.get("default_up_pct", 5.0)),
        "down_pct": _to_float(item.get("down_pct"), alert_cfg.get("default_down_pct", 5.0)),
        "cooldown_minutes": int(
            _to_float(item.get("cooldown_minutes"), alert_cfg.get("default_cooldown_minutes", 120))
        ),
        "once_per_day": bool(item.get("once_per_day", True)),
        "note": (item.get("note") or "").strip(),
    }


def _clean_password(password: str) -> str:
    """去掉粘贴时带进来的空白。

    Gmail 的应用专用密码显示成 "abcd efgh ijkl mnop" 四组，照抄会带空格导致认证失败，
    这种固定格式直接拼起来；其它密码只去首尾空白，不动中间内容。
    """
    password = password.strip()
    if re.fullmatch(r"[a-z]{4}(\s+[a-z]{4}){3}", password):
        return re.sub(r"\s+", "", password)
    return password


def _to_float(value, fallback):
    try:
        if value is None or value == "":
            return float(fallback)
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def load() -> dict:
    """读取配置（带缓存）。文件不存在或损坏时回落到默认配置。"""
    global _cache
    with _lock:
        if _cache is not None:
            return copy.deepcopy(_cache)
        raw = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except json.JSONDecodeError as exc:
                log.warning("配置文件 %s 格式损坏（%s），先用默认配置", CONFIG_PATH, exc)
                raw = {}
            except OSError as exc:
                # 以前这里是悄悄回落到默认值——权限不对时，看起来就像
                # "设置全丢了"，其实文件好好的只是读不了。必须大声说出来
                log.error("读不了配置文件 %s（%s），先用默认配置。"
                          "Docker 部署检查 PUID/PGID；本机检查文件权限", CONFIG_PATH, exc)
                raw = {}
        cfg = _deep_merge(DEFAULTS, raw)
        cfg = _sanitize(cfg)
        _cache = cfg
        if not os.path.exists(CONFIG_PATH):
            _write(cfg)
        return copy.deepcopy(cfg)


def _sanitize(cfg: dict) -> dict:
    cfg["poll_interval_minutes"] = max(1, int(_to_float(cfg.get("poll_interval_minutes"), 10)))
    cfg["history_keep_days"] = max(1, int(_to_float(cfg.get("history_keep_days"), 30)))

    indices = []
    seen = set()
    for item in cfg.get("indices") or []:
        if isinstance(item, str):
            item = {"symbol": item, "name": ""}
        symbol = normalize_symbol(item.get("symbol", ""))
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        indices.append({"symbol": symbol, "name": (item.get("name") or "").strip()})
    cfg["indices"] = indices

    watchlist = []
    seen = set()
    for item in cfg.get("watchlist") or []:
        if isinstance(item, str):
            item = {"symbol": item}
        norm = normalize_watch_item(item, cfg.get("alert"))
        if not norm["symbol"] or norm["symbol"] in seen:
            continue
        seen.add(norm["symbol"])
        watchlist.append(norm)
    cfg["watchlist"] = watchlist

    email = cfg.get("email", {})
    email["smtp_port"] = int(_to_float(email.get("smtp_port"), 465))
    email["password"] = _clean_password(email.get("password") or "")
    if email.get("security") not in ("ssl", "starttls", "none"):
        email["security"] = "ssl"
    to_addrs = email.get("to_addrs")
    if isinstance(to_addrs, str):
        to_addrs = [x.strip() for x in to_addrs.replace(";", ",").split(",")]
    email["to_addrs"] = [x.strip() for x in (to_addrs or []) if x and x.strip()]
    email["enabled"] = bool(email.get("enabled", False))
    cfg["email"] = email

    llm_cfg = cfg.get("llm", {})
    llm_cfg["api_key"] = _clean_password(llm_cfg.get("api_key") or "")
    llm_cfg["timeout_s"] = max(10, int(_to_float(llm_cfg.get("timeout_s"), 120)))
    llm_cfg["max_tokens"] = max(1000, int(_to_float(llm_cfg.get("max_tokens"), 8000)))
    llm_cfg["check_interval_hours"] = max(1, int(_to_float(llm_cfg.get("check_interval_hours"), 12)))
    cfg["llm"] = llm_cfg

    web = cfg.get("websearch", {})
    web["api_key"] = (web.get("api_key") or "").strip()
    web["enabled"] = bool(web.get("enabled", False))
    web["max_results"] = max(1, min(int(_to_float(web.get("max_results"), 5)), 10))
    web["max_searches"] = max(1, min(int(_to_float(web.get("max_searches"), 3)), 6))
    web["days"] = max(1, min(int(_to_float(web.get("days"), 30)), 365))
    web["timeout_s"] = max(5, int(_to_float(web.get("timeout_s"), 30)))
    if web.get("search_depth") not in ("basic", "advanced"):
        web["search_depth"] = "basic"
    # 前端是个多行文本框，存进来可能是字符串；统一成规整过的域名列表
    web["include_domains"] = websearch.normalize_domains(web.get("include_domains"))
    cfg["websearch"] = web

    alert = cfg.get("alert", {})
    if alert.get("baseline") not in ("prev_close", "today_open"):
        alert["baseline"] = "prev_close"
    alert["market_hours_only"] = bool(alert.get("market_hours_only", False))
    cfg["alert"] = alert
    return cfg


def _write(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:  # 里面有 SMTP 密码，尽量收紧权限（Windows 上会静默失败，无妨）
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def save(cfg: dict) -> dict:
    global _cache
    with _lock:
        cfg = _sanitize(_deep_merge(DEFAULTS, cfg))
        _write(cfg)
        _cache = cfg
        return copy.deepcopy(cfg)


def update(mutator) -> dict:
    """在锁内读改写：mutator(cfg) 直接修改传入的 dict。"""
    with _lock:
        cfg = load()
        mutator(cfg)
        return save(cfg)


def public_view(cfg: dict) -> dict:
    """给前端的配置副本：密码和 API Key 换成占位符，明文永不回传浏览器。"""
    out = copy.deepcopy(cfg)
    out["email"]["password"] = PASSWORD_MASK if out["email"].get("password") else ""
    out["llm"]["api_key"] = PASSWORD_MASK if out["llm"].get("api_key") else ""
    out["websearch"]["api_key"] = PASSWORD_MASK if out["websearch"].get("api_key") else ""
    return out
