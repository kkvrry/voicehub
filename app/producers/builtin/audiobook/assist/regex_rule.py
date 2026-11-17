"""需求转正则：用户输入一句描述或一条文本样例，模型给出可复用的规则。

校验只守确定性的部分：正则语法、是否匹配空串、替换串是否合法，并保证
``pattern`` 是纯正则（去掉代码围栏等包装）。**不再要求它命中输入**——输入
可能是一句「删除行首的 11、12 这种编号」的描述，而不是待匹配的文本。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.llm.base import ChatMessage
from app.producers.base import ProducerError
from app.producers.builtin.audiobook import ai_prompts
from app.usecase.assist import AssistContext, AssistError, AssistInvalid

__all__ = ["RegexTask"]

_MAX_PATTERN = 500
_MAX_INPUT = 600
_FENCE = re.compile(r"^```[A-Za-z0-9_+-]*[ \t]*\r?\n?(.*?)\r?\n?```$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Prepared:
    service: Any
    project_id: str
    kind: str
    #: 用户输入：可以是一句需求描述，也可以是一条文本样例。
    sample: str


def _pure_pattern(raw: Any) -> str:
    """只留正则本身：剥掉模型偶尔包在 ``pattern`` 外层的代码围栏或反引号。"""
    text = str(raw or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`" and "`" not in text[1:-1]:
        text = text[1:-1].strip()
    return text


class RegexTask:
    key = "regex_rule"
    label = "文本转正则"
    max_tokens = 1200

    def build(self, ctx: AssistContext) -> Prepared:
        kind = str(ctx.body.get("kind") or "dialog").strip()
        if kind not in {"dialog", "bracket"}:
            raise AssistError("正则任务的类型只能是 dialog 或 bracket。")
        request = str(ctx.body.get("sample") or "").strip()
        if not request:
            raise AssistError("先在自定义规则输入框里输入需求或一条文本样例。")
        if len(request) > _MAX_INPUT:
            raise AssistError(f"输入太长了，最多 {_MAX_INPUT} 字；截取有代表性的一段再试。")
        return Prepared(
            service=ctx.service,
            project_id=ctx.project_id,
            kind=kind,
            sample=request,
        )

    def messages(self, prepared: Prepared) -> tuple[ChatMessage, ...]:
        action = (
            "这是查找替换规则：pattern 匹配要处理的片段，需要保留的内容用捕获组。"
            if prepared.kind == "dialog"
            else "这是删除规则：pattern 匹配到的内容会被删除。"
        )
        system, template = ai_prompts.assist_messages("regex_rule")
        prompt = template.replace("{action}", action).replace("{input}", prepared.sample)
        return (ChatMessage("system", system), ChatMessage("user", prompt))

    def validate(self, prepared: Prepared, data: Mapping[str, Any]) -> dict[str, Any]:
        pattern = _pure_pattern(data.get("pattern"))
        replacement = str(data.get("replacement") or "") if prepared.kind == "dialog" else ""
        explanation = str(data.get("explanation") or "").strip()[:80]
        if not pattern:
            raise AssistInvalid("没有给出正则。")
        if len(pattern) > _MAX_PATTERN:
            raise AssistInvalid(f"正则太长（超过 {_MAX_PATTERN} 字符）。")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise AssistInvalid(f"正则写错了：{exc}") from None
        if compiled.search("") is not None:
            raise AssistInvalid("生成的正则会匹配空字符串，请收紧匹配范围。")
        if prepared.kind == "dialog":
            try:
                # 替换串里的 \1 等组引用要在真正试跑前就报错；空串足够触发解析。
                compiled.sub(replacement, "")
            except re.error as exc:
                raise AssistInvalid(f"替换串写错了：{exc}") from None
        return {
            "pattern": pattern,
            "replacement": replacement,
            "explanation": explanation,
        }

    def preview(self, prepared: Prepared, proposal: Mapping[str, Any]) -> dict[str, Any]:
        pattern = str(proposal.get("pattern") or "")
        replacement = str(proposal.get("replacement") or "")
        body: dict[str, Any] = {
            "dialog": [],
            "bracket": [],
            "custom_dialog": None,
            "custom_bracket": "",
        }
        if prepared.kind == "dialog":
            body["custom_dialog"] = {"pattern": pattern, "replacement": replacement}
        else:
            body["custom_bracket"] = pattern

        try:
            result = prepared.service.preview_text(prepared.project_id, body)
        except ProducerError as exc:
            text = str(exc)
            if "超过" in text or "先分章" in text:
                raise AssistError(text) from None
            raise AssistInvalid(f"试跑失败：{text}") from None

        hits = list(result.get("hits") or [])
        warnings: list[str] = []
        if not hits:
            warnings.append("当前正文没有命中，应用前请确认规则范围。")
        if len(hits) >= 50:
            warnings.append("命中较多，预览只列前 50 处；应用前请用完整预览确认范围。")
        if prepared.kind == "dialog" and not replacement:
            warnings.append("替换串为空，命中内容会被删除。")
        if hits and not result.get("changed"):
            warnings.append("规则命中了，但替换后文本没有变化。")
        if any(len(str(hit.get("before") or "")) > 2000 for hit in hits):
            warnings.append("有一处命中跨度很大，确认不会误删整段。")

        return {
            "kind": prepared.kind,
            "pattern": pattern,
            "replacement": replacement,
            "explanation": str(proposal.get("explanation") or ""),
            "changed": bool(result.get("changed")),
            "scope": str(result.get("scope") or ""),
            "chars_before": int(result.get("chars_before") or 0),
            "chars_after": int(result.get("chars_after") or 0),
            "hits": hits,
            "total": len(hits),
            "truncated": len(hits) >= 50,
            "warnings": warnings,
        }
