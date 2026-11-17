"""MiniMax ``get_voice`` 的响应解析——**拆信封只写一次**。

官方响应体（``platform.minimax.io/docs/api-reference/voice-management-get``）：

.. code-block:: json

    {
      "system_voice":     [ {voice_id, voice_name, description, category, tag_list, cover_url, file_id}, ... ],
      "voice_cloning":    [ ... ],
      "voice_generation": [ ... ],
      "base_resp": {"status_code": 0, "status_msg": "success"}
    }

三组是**平铺的数组**（不是"分组"）。有些中转网关会把它包成
``system_voice_list: [{voice_list: [...]}]``——两种都认，因为认错的表现是
"解析出 0 条"，而那看起来像"这个账号没有音色"，不会有人去怀疑解析。

**这里不内置兜底音色清单。** 从前那套做法（拉不到就用一份写死的常用音色）会让
"账号里一条音色都没有""密钥没配""这次网络抖了"三种情况长得一模一样，用户对着
一份**并不存在于他账号里**的清单挑音色，直到发请求才失败。现在拉不到就说清为什么，
用户点一次刷新即可——**宁可空着，不可编造**。

**语言、性别、年龄感、音色特点都是在这里认出来的**（:mod:`app.engines.traits`）：
这一家的清单里没有这四个字段，它们只写在 ``voice_id`` 的命名（``Cantonese_CuteGirl``、
``male-qn-qingse``）、``voice_name``（``温柔女声``）与 ``description`` 的中文句子
（"青年女性粤语声音"）里。

认出来之后**各有各的去处**——不是随便挑一个地方放：

- **语言、性别、年龄感**落进**它们本来就有的格子**，也就是界面筛选栏那三个下拉，
  于是"挑一个说粤语的女声"从"逐个点开看说明"变成"筛三下"；
- **音色特点**落成**标签**（``tags``）：它跟上面三样不同，一个音色可以同时挂着
  "温暖、善良、体贴"好几条，而那三个格子都只有一格，装不下。（这个平台的
  ``tag_list`` 实测**永远是空的**，所以这一格从前是空着的。）
"""

from __future__ import annotations

from typing import Any

from app.engines.base import VoiceMeta
from app.engines.traits import read_traits

__all__ = ["parse_voice_list", "voice_items"]


def _as_list(raw: Any) -> list[Any]:
    return list(raw) if isinstance(raw, (list, tuple)) else []


def _tags_of(raw: Any) -> tuple[str, ...]:
    """平台的 ``tag_list`` → 一串标签。

    文档里它是**字符串数组**，而同一平台的别的端点上偶有逗号串的形式（``"新闻,旁白"``）。
    两种都认——认错了的症状是标签里挂着 ``新闻,旁白`` 一整条，搜"旁白"搜不到，
    而界面上看不出哪里不对（它确实显示成了一行字）。
    """
    if isinstance(raw, str):
        parts = raw.replace("，", ",").split(",")
    elif isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        return ()
    return tuple(part.strip() for part in parts if part.strip())


def _text_of(raw: Any) -> str:
    """``description`` 可能是**字符串或字符串数组**（官方给的是数组形式），两种都认。

    认错的表现很隐蔽：``str([...])`` 会把整个 Python 列表的 ``repr`` 端到界面上，
    用户看到副行里挂着一串带方括号与引号的假文字——它**看着像数据**，只是难看，
    于是没人会去怀疑解析。多条时用换行接起来（它们在平台上本就是同一个音色的
    多句说明）。
    """
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)):
        return "\n".join(str(part).strip() for part in raw if str(part).strip())
    return ""


def _traits_tags(raw_tags: Any, characteristics: tuple[str, ...]) -> tuple[str, ...]:
    """平台的 ``tag_list`` + 认出来的音色特点（**认出来的排最前**）。

    特点排最前是因为它们是"这个声音听起来什么样"——挑音色时比平台自己的归类更
    先要看；平台原有的标签原样跟在后面，顺序与内容一个都不动。重复项去掉：平台若
    也写了同一个词，不该出现两枚一样的标签。

    **语言不再走这条路**。它从前也落在这里（混成一条叫"粤语"的标签），那样做的毛病是
    "界面上只能当一串字看，筛不了"——现在它落成 ``Voice.language`` 那一格，与性别、
    年龄感并排成为第三个筛选维度。
    """
    tags = _tags_of(raw_tags)
    if not characteristics:
        return tags
    extra = tuple(word for word in characteristics if word not in tags)
    return (*extra, *tags)


def voice_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从 ``get_voice`` 的响应体里挖出**全部音色对象**（两套形状都认）。"""
    out: list[dict[str, Any]] = []
    # 第 1 套（官方现行）：三组平铺数组。
    for key in ("system_voice", "voice_cloning", "voice_generation"):
        out.extend(item for item in _as_list(payload.get(key)) if isinstance(item, dict))
    # 第 2 套（部分中转网关）：分组包装。
    for group in _as_list(payload.get("system_voice_list")):
        if isinstance(group, dict):
            out.extend(item for item in _as_list(group.get("voice_list")) if isinstance(item, dict))
    return out


def parse_voice_list(payload: dict[str, Any]) -> tuple[VoiceMeta, ...]:
    """响应体 → 音色清单。

    **信封的检查留给调用方**（:mod:`client` 里统一拆 ``base_resp``），这里只管
    把音色数组变成 :class:`VoiceMeta`——两件事混在一起的话，加一个端点就要再抄
    一遍信封判断。

    ``voice_name`` 是给人看的显示名，``voice_id`` 是发给平台的值，**两者分开装**：
    只留 id 的症状是界面上只能显示 ``Chinese (Mandarin)_News_Anchor`` 这种。

    属性的读法（哪一项读哪几段字）写在 :func:`app.engines.traits.read_traits` 里
    ——**年龄不读 id**，因为这一家的 ``Gentle_Senior`` 指的是"温柔学姐"。
    """
    items: list[VoiceMeta] = []
    for item in voice_items(payload):
        voice_id = str(item.get("voice_id") or "").strip()
        if not voice_id:
            continue
        name = str(item.get("voice_name") or "").strip()
        description = _text_of(item.get("description"))
        traits = read_traits(voice_id=voice_id, name=name, description=description)
        items.append(
            VoiceMeta(
                id=voice_id,
                name=name,
                description=description,
                category=str(item.get("category") or "").strip(),
                tags=_traits_tags(item.get("tag_list"), traits.characteristics),
                gender=traits.gender,
                age=traits.age,
                language=traits.language,
            )
        )
    return tuple(items)
