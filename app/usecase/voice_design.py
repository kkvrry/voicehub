"""音色描述（voice design prompt）：把一段需求写成可直接交给设计模型的描述。

描述就是设计音色的凭据——每次合成随请求发送。所以这里只产出一段话：
不建音色、不碰引擎。完整提示词与音色分配用的精简写法都只在这里，
前端不另写一份。

正文照平台方给的写法建议组织（四个关键维度、可选的丰富项、写法建议与
注意事项），用 markdown 摆成它原本的样子：维度、示例与范例都是原话，
不改写成自己的说法——这些话就是模型的判据。
"""

from __future__ import annotations

import re

from app.llm.base import ChatMessage, LlmError
from app.usecase import llm as llm_uc

__all__ = ["BRIEF_RULES", "MAX_CHARS", "SYSTEM", "TEMPLATE", "clean", "suggest"]

SYSTEM = "你是配音的音色设计师，只写音色描述本身。"

#: 一段描述要怎么写。``{brief}`` 是已有的描述或草稿，没有时整行消失。
TEMPLATE = """音色名：{name}
{brief}
写一段音色描述（voice design prompt），它会原样交给音色设计模型，是这条音色唯一的凭据。

## 关键维度

| 维度 | 示例 |
|---|---|
| 性别与年龄 | "young woman in her mid-20s"、"五十多岁的中年男性" |
| 音色/质感 | "deep and gravelly"、"丝滑醇厚、带着磁性" |
| 情绪/语气 | "warm and confident"、"温柔但带着一丝疲惫" |
| 语速/节奏 | "slow and deliberate"、"语速极快，像连珠炮" |

以下维度可选择性加入，增加丰富度：

- 角色/人设：narrator、podcast host、评书先生、深夜电台 DJ
- 说话风格：casual and colloquial、一本正经地、压低嗓音像在密谋
- 场景描写：narrating a nature documentary、在给投资人路演
- 年代参照：1940s film noir、八十年代译制片配音

## 写法建议

- 简洁描述型：用关键词或一句话快速勾勒声音轮廓。
  例：Heavy Russian accent, gruff middle-aged male, blunt and matter-of-fact.
- 专业描述型：通过场景、人设或多维度细节立体刻画声音。
  例：Young female, extreme close-up with a binaural, ear-to-ear ASMR feel. Audible breathing,
  subtle swallowing, and soft natural lip sounds. She speaks very slowly, creating a deeply
  relaxing and immersive experience.
- 例：一位年迈的老先生，说带北方口音的普通话，语速缓慢而沉稳，嗓音略带沙哑和沧桑感，
  仿佛一位饱经风霜的老爷爷在讲故事，充满岁月的智慧。

## 注意事项

- 长度：1-4 句即可，不需要写长文。核心特征描述清楚比堆砌维度更重要。
- 避免冲突：不要同时要求矛盾的特征（如「稚嫩的童声 + CEO 气场」）。
- 避免音质效果词：不要写混响、回声、EQ、压缩等后期处理相关描述。
- 避免模糊词：不要用「普通的」「正常的」「外国的」等缺乏具体指向的描述。
- 中英文均可：模型同时支持中英文音色描述，选择你最能精确表达的语言。

## 这次的要求

音色名与已写的描述说的是这次要给谁用的声音，以它们为准；上面的范例只借写法，不要照抄。
只输出这段描述本身：不加引号、不加标题、不加解释。"""

#: 模型偶尔带上的包装：引号与「音色描述：」这类前缀。
_QUOTES = "\"'“”‘’「」『』"
_PREFIXES = ("音色描述：", "描述：", "答案：")

#: 折行处的空白：中文句子直接相连，英文才留空格。
_TIGHT = re.compile(r"(?<=[\u4e00-\u9fff，。；！？、：）])\s+")

#: 输入框按 300 字收：四句中文写满不到 150 字，英文的长范例也在 300 以内。
MAX_CHARS = 300

#: 音色分配在同一次回复里写最终描述时用的精简写法：与下面的完整提示词同一口径。
BRIEF_RULES = (
    f"1~4 句、{MAX_CHARS} 字以内。以性别与年龄、音色/质感、情绪/语气、语速/节奏为主；"
    "可以加角色/人设、说话风格、场景。只写声音本身，不写角色姓名、剧情与书名；"
    "不写混响、回声、EQ、压缩等后期词；不要同时要求矛盾的特征；"
    "不用「普通的」「正常的」这类模糊词；不加引号、标题或解释。"
)


def _unwrap(raw: str) -> str:
    """一行 → 去掉「描述：」前缀与首尾引号。"""
    line = raw.strip()
    for prefix in _PREFIXES:
        if line.startswith(prefix):
            line = line[len(prefix) :].strip()
    return line.strip(_QUOTES).strip()


def clean(text: str) -> str:
    """模型回的一段话 → 可填进输入框的一段话。空串留给调用方报错。

    去掉引号与「描述：」前缀；折行接到一起（中文不留空格），其余原样保留——
    多句是正常的，不截成第一句。逐行剥一层：模型常把整段话连引号一起包起来。
    """
    lines = [_unwrap(part) for part in str(text or "").splitlines()]
    body = " ".join(line for line in lines if line)
    return _TIGHT.sub("", body).strip(_QUOTES).strip()[:MAX_CHARS]


async def suggest(
    name: str, brief: str = "", *, provider: str = "", model: str = ""
) -> dict[str, str]:
    """写一段描述。``name`` 与 ``brief`` 至少给一个，否则无从下笔。

    :param brief: 已有的描述或草稿，创建窗口传用户写的那句。
    :param provider: 指定通道；留空用默认那家。
    """
    name = (name or "").strip()
    brief = (brief or "").strip()
    hint = f"已写的描述：{brief}\n" if brief else ""
    prompt = TEMPLATE.replace("{name}", name or "（未命名）").replace("{brief}", hint)
    body: dict[str, object] = {
        "messages": (
            ChatMessage("system", SYSTEM),
            ChatMessage("user", prompt),
        ),
        "temperature": 0.9,
        "max_tokens": 600,
    }
    if provider:
        body["provider"] = provider
    if model:
        body["model"] = model
    result = await llm_uc.chat(body)
    text = clean(str(result.get("text") or ""))
    if not text:
        raise LlmError("模型没有给出描述，重试一次。")
    return {"text": text, "provider_label": str(result.get("provider_label") or "")}
