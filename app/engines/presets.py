"""「引导指令」那一格的现成说法——**读插件目录下的 ``instructions.json``，不写在代码里**。

为什么是数据文件而不是一个 Python 常量
--------------------------------------

预设**专门是用来改的**：``.json`` 摆在那儿，改一句话、删一条、加五条，都不用碰 Python，
也不会因为改了一个引号让整个插件起不来（读失败最坏是"这次没有候选"，见下）。
写死在代码里的话，改一次就要动一次插件源码——而这一类"文案级"的改动会一直发生。

读失败怎么办
------------

**一句话都不抛**，返回空表：这个文件是给人手改的，手改就会有写错的格式、漏掉的逗号。
而「引导指令」那一格的用途只是"给按一下就有现成句子的方便"——为它让 ``/api/v1/engines``
整个 500，等于把整个生成页（以及所有别家引擎）一起弄挂。
代价是**改错了界面上候选会静默消失**，所以格式写在文件自己的 ``note`` 里。

与 ``unsupported()`` / ``mark_map`` 的分工
----------------------------------------

- ``unsupported()``：这一家**认哪些参数、不认哪些**（引擎面板里那张表）；
- ``mark_map``：正文里的**语气词标签**（``[[笑]]``）它认哪几个；
- 这里：**引导指令**那一格的现成说法（``instruction``）——它不落进任何"认不认"的
  判断，那一格本来就是自由文本，这些只是**写好的例子**。

**哪些话这一家听得懂，只有它自己知道**：界面上攒一份通用清单，等于替所有引擎一起
吹牛。所以每台引擎带自己的 ``instructions.json``（内容存成数据文件，加载器只有这一份）。
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["load_presets"]


def load_presets(path: Path) -> tuple[tuple[str, str], ...]:
    """读一份 ``instructions.json``，返回 ``((短名, 完整句子), ...)``（顺序即界面上候选的顺序）。

    **每次调用都重新读**（不缓存）：这是"改一下刷新一下就见效"的那类数据，
    而它只有几 KB——为了省这点 IO 加一层缓存，换来的是"改了没反应，得重启"。

    :param path: 数据文件的位置（各插件自己的目录下）。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 文件没了 / JSON 写坏了：当作"这次没有候选"。**不是异常**，见模块顶部。
        return ()
    items = raw.get("presets") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return ()
    found: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        text = str(item.get("text") or "").strip()
        # 短名与句子都要有：只有句子的话菜单里没法认，只有短名则按下去框还是空的。
        if label and text:
            found.append((label, text))
    return tuple(found)
