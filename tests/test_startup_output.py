"""启动横幅必须**当场**出现在终端上，不能攒着。

``start.bat`` 用 ``uv run`` 起服务，子进程的 stdout 接到的是管道而不是控制台；管道上
Python 走块缓冲（攒够 4KB 才冲一次），而 ``[VoiceHub] 运行中：…`` 只有几十个字节。
症状是：服务其实已经起来了，终端却停在 ``[VoiceHub] 正在启动服务……`` 一动不动，看着
像卡死（实测修复前 57 秒零输出，加上 flush 后 2 秒出现）。

``scripts/ensure_web.py`` 的话一直能看见，就因为它每处 print 都带 ``flush=True`` ——
那是对照样本。所以这里钉的不是"有没有 print"，而是"这行 print 会不会被管道吞掉"。
"""

from __future__ import annotations

import ast
from pathlib import Path

_MAIN = Path(__file__).resolve().parent.parent / "app" / "main.py"


def _unflushed(tree: ast.AST) -> list[int]:
    """找出所有**没写** ``flush=True`` 的 ``print(...)`` 调用，返回行号。

    只认显式的 ``flush=True``：写 ``flush=sys.stdout.flush`` 那种花活解析不了真假，
    宁可让它报出来让人改直白。
    """
    lines = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "print":
            continue
        flushed = any(
            keyword.arg == "flush"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
        if not flushed:
            lines.append(node.lineno)
    return lines


def test_every_startup_banner_is_flushed() -> None:
    """``app/main.py`` 里的每一句话都得能被立刻看见——它们全是"服务现在怎么样"。"""
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    missing = _unflushed(tree)
    assert not missing, (
        f"app/main.py 第 {missing} 行的 print 少了 flush=True："
        "在 uv run 的管道下会被块缓冲吞掉，表现为启动卡死"
    )


def test_the_check_catches_a_bare_print() -> None:
    """判据自己得有合成样本：一个不冲、一个冲，只该报前者的行号。"""
    tree = ast.parse('print("不冲")\nprint("冲了", flush=True)\n')
    assert _unflushed(tree) == [1]
