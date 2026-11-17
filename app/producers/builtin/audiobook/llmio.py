"""分析请求的输入输出：解析回复、回显校验、重试、输出预算。

这一层只管「发出去的是什么、回来的怎么读」，不碰项目与行：
调用方把消息体交给 :func:`chat_with_retry`，把回复交给 :func:`parse_json`，
再由 :func:`rows_of` 取标注数组。

输出预算（:func:`output_tokens` / :func:`rows_per_request`）决定一次请求装几行：
先让回复上限从一开始就装得下，截断只是兜底，不是常规路径。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any

from app.llm.json_text import parse_json

__all__ = [
    "MIN_SPLIT_ROWS",
    "chat_with_retry",
    "echo_matches",
    "output_limit",
    "output_tokens",
    "parse_json",
    "rows_of",
    "rows_per_request",
]

#: 一行标注要输出的大致 token：`[0,0,"平静","你来了"]` 约 12–16，留余量。
_ROW_OUTPUT_TOKENS = 24
#: JSON 信封与**思考内容**的预留：思考模式开着时 reasoning 也吃 max_tokens。
_OUTPUT_RESERVE = 2048
#: 通道没声明回复上限时的兜底（DeepSeek 官方 8192）。
_DEFAULT_OUTPUT_TOKENS = 8192
#: 自动分段的下限：再少就不值得拆成两次请求。
MIN_SPLIT_ROWS = 8

#: 回显比对时不计的字符：引号与空白——模型常把「“你来了。”」抄成「你来了」。
_ECHO_JUNK = re.compile(r"[\s“”「」『』\"'‘’]")


def echo_matches(text: str, head: str) -> bool:
    """模型回显的原文开头与这一行对不对得上。

    行号是这一批的局部下标，模型串位时行号照样"合法"——回显是唯一能发现串位的
    东西。空回显不算错（旧格式与不听话的模型没有这一列）。
    """
    wanted = _ECHO_JUNK.sub("", head or "")
    return not wanted or _ECHO_JUNK.sub("", text or "").startswith(wanted)


def rows_of(data: Mapping[str, Any]) -> list[Any]:
    """回复里的 ``rows``；形状不对当空（截断与乱返回都走"对半分再发"）。"""
    rows = data.get("rows")
    return rows if isinstance(rows, list) else []


async def chat_with_retry(llm_uc: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    """可重试的对话调用：限流 / 超时才重试一次，密钥错与参数错不重试。

    重试的判据是插件自己给的 ``LlmError.retryable``——4xx 类错误重试只是再花一次钱。
    """
    from app.llm.base import LlmError

    try:
        return await llm_uc.chat(body)
    except LlmError as exc:
        if not exc.retryable:
            raise
        await asyncio.sleep(0.8)
        return await llm_uc.chat(body)


def output_tokens(rows: int, limit: int) -> int:
    """这批行大约要多少输出 token：信封 + 思考预留之外按行数加。"""
    return min(limit, rows * _ROW_OUTPUT_TOKENS + _OUTPUT_RESERVE)


def rows_per_request(limit: int) -> int:
    """一次请求最多几行：让回复上限从一开始就装得下，不靠事后补救。"""
    return max(MIN_SPLIT_ROWS, (limit - _OUTPUT_RESERVE) // _ROW_OUTPUT_TOKENS)


def output_limit(channel: str) -> int:
    """这条通道配置的回复上限；读不到就用兜底（设置读不出来不该让分析失败）。"""
    from app.usecase import llm as llm_uc

    try:
        value = int(llm_uc.output_limit(channel))
    except Exception:  # noqa: BLE001 - 配置坏掉时按兜底继续
        value = 0
    return value if value > 0 else _DEFAULT_OUTPUT_TOKENS
