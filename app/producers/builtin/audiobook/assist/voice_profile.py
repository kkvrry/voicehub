"""角色声音画像：让对话模型说清「这个角色该是什么声音」。

为什么不给对白：对白只说明**说了什么**，不说明声音；而模型对作品本身知道得比
几句截断的台词多得多。所以这一步只发书名（带作者）与角色名（带行数，用于判断
戏份），不发对白、不发候选音色。

作者随书名一起给出：导入时书目信息里就有作者，同名书很多，作者是模型认人的关键。
作者不需要模型去查，开头已经写明。

**搜索交给模型自己决定**：对话里声明平台的服务端联网搜索，认不准的角色它自己去查、
查完直接写画像。搜索与整合都在平台侧完成，宿主不执行工具循环，也不替它拼 query。
搜索结果只用于这一步，不会进分配提示词。

产出的画像有两个用户：

1. 交给向量检索当查询文本——性别、年龄、质感、语气都是音色标签同族的词，
   才比得动（对白是叙事文本，和标签不是一个语言）。
2. 原样放进提示词，让模型知道自己是按什么标准挑的，写 reason 时也有一致依据。

查完还是不认识，就如实标 ``known: false``：宁可退回按名字匹配，也不让它按名字
硬编一份画像——编出来的画像比没有更坏，因为向量会当真。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from app.infra.atomic import write_json
from app.infra.config import data_dir
from app.infra.loghub import CHANNEL
from app.llm.base import ChatMessage
from app.llm.json_text import parse_json
from app.producers.builtin.audiobook import ai_prompts
from app.usecase import llm as llm_uc

__all__ = ["PROMPT_VERSION", "ensure", "summary", "text"]

#: 字段含义改了就 +1：旧缓存随之失效。提示词正文的版本另由 ai_prompts.revision 计入键。
PROMPT_VERSION = 5

GENDERS = ("男", "女")
AGES = ("童声", "少年", "青年", "中年", "老年")
PACES = ("偏慢", "适中", "偏快")

#: 一次最多问 40 个角色：再多输出容易截断，分批发也比一整条长回复稳。
_CHUNK = 40
_MAX_TOKENS = 120

_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_LOADED = False


def _cache_path():
    return data_dir() / "cache" / "voice_profiles.json"


def _load() -> None:
    global _LOADED
    with _CACHE_LOCK:
        if _LOADED:
            return
        _LOADED = True
        try:
            stored = json.loads(_cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(stored, dict):
            _CACHE.update(
                {str(key): value for key, value in stored.items() if isinstance(value, dict)}
            )


def _save() -> None:
    try:
        write_json(_cache_path(), dict(_CACHE), indent=None)
    except OSError as exc:
        # 缓存写不进去只影响下次多花一次调用，不该让分配失败。
        CHANNEL.warning(f"角色画像缓存写入失败：{exc}", source="assist")


def _key(book: str, author: str, speaker: str) -> str:
    # 设置页改了提示词，画像口径就变了：版本指纹一起进键，旧缓存不再冒充。
    prompt_version = ai_prompts.revision("voice_profile")
    raw = f"{PROMPT_VERSION}:{prompt_version}:{book}:{author}:{speaker}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _words(value: Any, limit: int, width: int) -> list[str]:
    """模型偶尔给字符串或超长数组：收窄成去重后的短词表。"""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return []
    out: list[str] = []
    for item in items:
        word = str(item or "").strip()
        if word and word not in out:
            out.append(word[:width])
        if len(out) >= limit:
            break
    return out


def _pick(value: Any, allowed: Sequence[str]) -> str:
    word = str(value or "").strip()
    return word if word in allowed else ""


def _normalize(raw: Any) -> dict[str, Any]:
    """一条模型输出 → 固定字段。认不出来的字段一律留空，不往画像里塞猜测。"""
    if not isinstance(raw, dict):
        return {"known": False}
    known = bool(raw.get("known", True))
    profile = {
        "known": known,
        "gender": _pick(raw.get("gender"), GENDERS),
        "age": _pick(raw.get("age"), AGES),
        "timbre": _words(raw.get("timbre"), 6, 12),
        "tone": _words(raw.get("tone"), 4, 12),
        "pace": _pick(raw.get("pace"), PACES),
        "note": str(raw.get("note") or "").strip()[:60],
    }
    if not known:
        return {"known": False}
    return profile


def text(profile: dict[str, Any] | None) -> str:
    """画像 → 检索用的查询文本。只放能对上音色标签的词，不放剧情与角色名。"""
    if not profile or not profile.get("known", True):
        return ""
    parts: list[str] = []
    sex_age = "，".join(
        part for part in (profile.get("gender") or "", profile.get("age") or "") if part
    )
    if sex_age:
        parts.append(sex_age)
    for label, key in (("音色", "timbre"), ("语气", "tone")):
        words = "、".join(profile.get(key) or [])
        if words:
            parts.append(f"{label}：{words}")
    if profile.get("pace"):
        parts.append(f"语速{profile['pace']}")
    return "；".join(parts)


def summary(profile: dict[str, Any] | None) -> str:
    """界面上那一行：性别 · 年龄 · 音色词。未知时给一句能直接看的说明。"""
    if not profile:
        return ""
    if not profile.get("known", True):
        return "模型不认识这个角色"
    bits = [
        part
        for part in (
            profile.get("gender") or "",
            profile.get("age") or "",
            "、".join((profile.get("timbre") or [])[:3]),
        )
        if part
    ]
    return " · ".join(bits) or profile.get("note") or ""


def _parse_roles(text: str, allowed: set[str]) -> dict[str, dict[str, Any]]:
    data = parse_json(str(text or ""))
    rows = data.get("roles") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        speaker = str(item.get("speaker") or "").strip()
        if speaker in allowed and speaker not in out:
            out[speaker] = _normalize(item)
    return out


def _author_from_name(raw: str) -> str:
    """从导入的文件名里捞作者：``(川端康成作品集)`` → ``川端康成``。

    用户没在书目信息里填作者时，这是唯一的线索——清洗书名会把括号里的东西
    一起切掉，而里面往往正好是作者。只在括号组里找：去「；译者」尾巴、去
    「作品集」，排除版本、卷册、出版社这类不是人名的段。
    """
    for group in re.findall(r"[（(]([^（）()]*)[）)]", str(raw or "")):
        text = group.strip(" ·-_—,，、;；")
        if not text or not re.search(r"[\u4e00-\u9fff]", text):
            continue
        text = text.split("；")[0].split(";")[0].strip()
        for suffix in ("作品集", "文集", "全集", "著", "编著", "译"):
            text = text.removesuffix(suffix)
        text = text.strip(" ·-_—,，、")
        if not 2 <= len(text) <= 10:
            continue
        if re.search(r"\d|版|卷|册|辑|出版|全集|文集|作品集|小说|文库", text):
            continue
        return text
    return ""


def _book_title(raw: str) -> str:
    """书名去掉版本、丛书与来源标记：``山音（2021年版修订版） (Z-Library)`` → ``山音``。

    项目名多半来自导入的文件名，模型认不出带一串括号的书名，搜也搜不准。
    只切第一个括号之前的部分；切完不足两个字就保留原样（书名本身可能以括号开头）。
    """
    text = str(raw or "").strip()
    cut = len(text)
    for mark in ("（", "("):
        index = text.find(mark)
        if index > 0:
            cut = min(cut, index)
    title = text[:cut].strip(" ·-_—,，、")
    return title if len(title) >= 2 else text


def _author(raw: str) -> str:
    """作者去掉续注与来源标记：``川端康成（日）`` → ``川端康成``。

    用户在书目信息里手填的多半是干净的人名；这里只挡住导入时带进来的括号尾巴。
    多作者用顿号连着，原样留着——那是有效信息，不该被切掉。
    """
    text = str(raw or "").strip()
    cut = len(text)
    for mark in ("（", "("):
        index = text.find(mark)
        if index > 0:
            cut = min(cut, index)
    return text[:cut].strip(" ·-_—,，、;；")


def _title(book: str, author: str) -> str:
    """提示词里的书名：有作者就一起给——同名书很多，作者是模型认人的关键。"""
    return f"《{book}》（{author} 著）" if author else f"《{book}》"


async def _ask(
    book: str,
    roles: Sequence[tuple[str, int]],
    *,
    author: str = "",
    provider: str,
    model: str,
) -> dict[str, dict[str, Any]]:
    """问一轮画像：书名带作者、只发角色名与行数；认不准时可以联网查证。

    联网由平台的服务端工具执行，宿主不执行工具循环；平台只返回纯文本，
    正文不是 JSON 时再发一次纯 JSON 抽取，不再联网。
    """
    listing = "\n".join(f"- {name}（{rows} 行）" if rows else f"- {name}" for name, rows in roles)
    system, template = ai_prompts.assist_messages("voice_profile")
    prompt = template.replace("{title}", _title(book, author)).replace("{roles}", listing)
    allowed = {name for name, _ in roles}
    max_tokens = min(4000, _MAX_TOKENS * len(roles) + 200)

    def payload(
        messages: Sequence[ChatMessage], *, web_search: bool, json_object: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "messages": tuple(messages),
            "temperature": 0,
            "thinking": "off",
            "max_tokens": max_tokens,
        }
        if web_search:
            body["web_search"] = True
        if json_object:
            body["json_object"] = True
        if provider:
            body["provider"] = provider
        if model:
            body["model"] = model
        return body

    sent: list[ChatMessage] = [ChatMessage("system", system), ChatMessage("user", prompt)]
    text = ""
    try:
        result = await llm_uc.research(payload(sent, web_search=True, json_object=False))
        text = str(result.get("text") or "")
        done = _parse_roles(text, allowed)
        if done:
            return done
        if text.strip():
            # 联网回答只给纯文本：把正文交给下一轮抽取，不再联网。
            sent.append(ChatMessage("assistant", text))
            sent.append(
                ChatMessage(
                    "user",
                    "按上述资料只输出 JSON，不要解释；查不到的角色写 known:false。",
                )
            )
    except Exception as exc:  # noqa: BLE001 - 联网失败退回普通对话，不让画像整段丢掉
        CHANNEL.warning(
            f"角色画像联网失败，退回普通对话：{exc}", source="assist", task="voice_assign"
        )

    # 收尾：只要一次纯 JSON。联网没拿到资料时就是对原提示词直接作答。
    result = await llm_uc.chat(payload(sent, web_search=False, json_object=True))
    done = _parse_roles(str(result.get("text") or ""), allowed)
    if not done:
        raise RuntimeError("回复里没有 roles 数组。")
    return done


async def ensure(
    book: str,
    roles: Sequence[tuple[str, int]],
    *,
    author: str = "",
    provider: str = "",
    model: str = "",
) -> tuple[dict[str, dict[str, Any]], str]:
    """取这些角色的画像：缓存优先，缺的一次问清。

    :param author: 书目信息里的作者。同名书很多（《金阁寺》就有两本），
        作者是模型认人的关键；用户没填就只给书名。
    :return: ``(画像表, 失败说明)``。失败说明为空表示全部拿到；非空时画像表
        可能只有一部分——调用方照样能按名字匹配，不必让整次分配失败。
    """
    raw_book = str(book or "").strip()
    book = _book_title(raw_book) or "这本书"
    author = _author(author) or _author_from_name(raw_book)
    wanted = [(str(name).strip(), int(rows or 0)) for name, rows in roles if str(name).strip()]
    if not wanted:
        return {}, ""
    _load()
    with _CACHE_LOCK:
        cached = {
            name: _CACHE[_key(book, author, name)]
            for name, _ in wanted
            if _key(book, author, name) in _CACHE
        }
    missing = [(name, rows) for name, rows in wanted if name not in cached]
    if not missing:
        return cached, ""

    fetched: dict[str, dict[str, Any]] = {}
    errors = 0
    for start in range(0, len(missing), _CHUNK):
        chunk = missing[start : start + _CHUNK]
        try:
            fetched.update(
                await _ask(book, chunk, author=author, provider=provider, model=model)
            )
        except Exception as exc:  # noqa: BLE001 - 画像拿不到只是降级，不是失败
            errors += 1
            CHANNEL.warning(f"角色画像没拿到：{exc}", source="assist", task="voice_assign")
            continue
    if fetched:
        with _CACHE_LOCK:
            for name, profile in fetched.items():
                _CACHE[_key(book, author, name)] = profile
        _save()
    for name, _ in missing:
        # 没拿到（不是「不认识」）：留空表，界面不写「模型不认识这个角色」，
        # 免得把一次请求失败说成一次判断。
        fetched.setdefault(name, {})
    note = "角色画像没拿到，按角色名匹配。" if errors else ""
    return {**cached, **fetched}, note
