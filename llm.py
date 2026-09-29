"""DeepSeek（OpenAI 兼容）客户端。

配置字段沿用 toy_llm_roleplay 那套：base_url / api_key / model / temperature /
max_tokens / timeout_s / json_mode。区别只有传输层——那边是 async 用 httpx，
这边 Flask 是同步的，直接用项目已有的 requests，不多引一个依赖。

模型**不联网**。所有事实（年报数字、分红历史、股价、持仓）都由我们从本地库里
取好、拼进提示词。理由见 insights.py 顶部。
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"


class LLMError(RuntimeError):
    pass


def is_configured(llm_cfg: dict) -> bool:
    return bool((llm_cfg or {}).get("api_key") and (llm_cfg or {}).get("model"))


def chat(
    llm_cfg: dict,
    messages: list[dict],
    json_mode: bool | None = None,
    max_tokens: int | None = None,
    tool_schemas: list[dict] | None = None,
) -> dict:
    """发一轮对话，返回 {text, message, tool_calls, usage, ...}。失败抛 LLMError。

    传了 tool_schemas 时模型可能不返回正文而是返回 tool_calls，调用方要负责执行
    并把结果喂回来（见 chat_with_tools）。
    """
    if not is_configured(llm_cfg):
        raise LLMError("没有配置 DeepSeek 的 API Key 或模型")

    base = str(llm_cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    url = f"{base}/chat/completions"
    payload = {
        "model": llm_cfg["model"],
        "messages": messages,
        "temperature": float(llm_cfg.get("temperature", 0.3)),
        "max_tokens": int(max_tokens or llm_cfg.get("max_tokens", 2000)),
    }
    if tool_schemas:
        payload["tools"] = tool_schemas
    if json_mode if json_mode is not None else llm_cfg.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm_cfg['api_key']}",
    }
    timeout = float(llm_cfg.get("timeout_s", 120))

    started = time.time()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise LLMError(f"请求失败：{type(exc).__name__}: {exc}") from exc

    if resp.status_code != 200:
        detail = resp.text[:300]
        if resp.status_code == 401:
            raise LLMError("API Key 无效（401）")
        if resp.status_code == 402:
            raise LLMError("账户余额不足（402）")
        if resp.status_code == 429:
            raise LLMError("请求过于频繁，稍后再试（429）")
        raise LLMError(f"HTTP {resp.status_code}：{detail}")

    try:
        data = resp.json()
        message = (data["choices"][0] or {}).get("message") or {}
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"返回格式无法解析：{exc}") from exc

    finish_reason = (data["choices"][0] or {}).get("finish_reason") or ""
    tool_calls = message.get("tool_calls") or []
    text = (message.get("content") or "").strip()
    # 思考型模型偶尔把答案写进思维链、content 留空
    reasoning = (message.get("reasoning_content") or "").strip()
    leaked = False
    if not text and reasoning and not tool_calls:
        log.warning("模型把内容写进了 reasoning_content，取思维链兜底")
        text = reasoning
        leaked = True

    # 要调工具时 content 本来就是空的，这不算失败
    if not text and not tool_calls:
        raise LLMError(f"模型返回了空内容（finish_reason={finish_reason}）")

    return {
        "text": text,
        "message": message,
        "tool_calls": tool_calls,
        "reasoning": reasoning if reasoning != text else "",
        # 撞上 max_tokens 上限 —— 内容会被拦腰截断，调用方必须处理
        "truncated": finish_reason == "length",
        "leaked_reasoning": leaked,
        "finish_reason": finish_reason,
        "usage": data.get("usage") or {},
        "model": data.get("model") or llm_cfg["model"],
        "elapsed_s": round(time.time() - started, 1),
    }


def chat_with_tools(
    llm_cfg: dict,
    messages: list[dict],
    tool_schemas: list[dict],
    execute,
    max_rounds: int = 4,
    max_calls: int = 8,
    max_tokens: int | None = None,
) -> dict:
    """带工具的多轮循环：模型要什么，我们执行完喂回去，直到它给出正文。

    ``execute(name, arguments) -> str`` 由调用方提供，负责真正干活。
    轮数和调用次数都有上限——模型偶尔会陷进"反复查同一个东西"的循环里，
    没有上限就是在烧钱。

    返回值跟 chat() 一样，另外带 tool_log（调用了哪些工具）和累计 usage。
    """
    conversation = list(messages)
    tool_log: list[dict] = []
    seen: dict[str, bool] = {}          # 本次生成里已经查过什么
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    result: dict = {}

    for round_index in range(max_rounds):
        # 到了最后一轮就把工具撤掉，逼它直接作答，否则可能一直要工具要到超限
        remaining = max_calls - len(tool_log)
        allow_tools = remaining > 0 and round_index < max_rounds - 1
        result = chat(
            llm_cfg, conversation,
            json_mode=False,
            max_tokens=max_tokens,
            tool_schemas=tool_schemas if allow_tools else None,
        )

        for key in total_usage:
            total_usage[key] += (result.get("usage") or {}).get(key, 0) or 0

        calls = result.get("tool_calls") or []
        if not calls:
            result["usage"] = total_usage
            result["tool_log"] = tool_log
            result["rounds"] = round_index + 1
            # 包含全部工具调用和返回结果（不含最终答案）。需要重写答案时从这里接着来，
            # 而不是从头再查一遍——检索额度已经花掉了，重查只会什么都查不到
            result["conversation"] = conversation
            return result

        # 模型那条带 tool_calls 的消息必须原样回填，否则下一轮对不上 id
        conversation.append(result["message"])
        for call in calls[:remaining]:
            function = call.get("function") or {}
            name = function.get("name") or ""
            arguments = function.get("arguments") or "{}"

            # 同一次生成里重复查同样的东西很常见，把结果再塞一遍纯属浪费 token
            cache_key = f"{name}::{arguments}"
            if cache_key in seen:
                output = ("这次查询你在本次对话中已经做过，结果就在前面的消息里，"
                          "请直接使用，不要重复调用。")
                repeated = True
            else:
                output = execute(name, arguments)
                seen[cache_key] = True
                repeated = False

            tool_log.append({"name": name, "arguments": arguments,
                             "chars": len(output), "repeated": repeated})
            conversation.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": output,
            })

    result["usage"] = total_usage
    result["tool_log"] = tool_log
    result["rounds"] = max_rounds
    result["conversation"] = conversation
    return result


def test_connection(llm_cfg: dict) -> tuple[bool, str]:
    """设置页那个「测试连接」按钮用的。不抛异常。"""
    try:
        # 给 512 而不是几十：会先思考再作答的模型（deepseek-flash 就是）
        # 额度太小会在思考阶段就耗尽，测试结果看起来像是模型在胡言乱语
        result = chat(
            llm_cfg,
            [{"role": "user", "content": "只回复两个字：连通"}],
            json_mode=False,
            max_tokens=512,
        )
    except LLMError as exc:
        return False, str(exc)
    usage = result.get("usage") or {}
    note = "（注意：这个模型会先思考再作答，max_tokens 要留够）" if result.get("leaked_reasoning") else ""
    return True, (
        f"连接正常（{result['model']}，{result['elapsed_s']}s，"
        f"消耗 {usage.get('total_tokens', '?')} tokens）：{result['text'][:40]}{note}"
    )
