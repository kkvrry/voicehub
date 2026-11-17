"""生成一套演示音色库（``demo_library/``）——正弦波 wav + 说话内容 + 分类结构。

用法：``.venv/Scripts/python.exe scripts/make_demo.py``，然后在界面「扫描目录」
里加入 ``<项目根>/demo_library``。

除了「分类 / 多样例 / 单样例」三种形态，这份数据还要让**筛选那几维真的筛得动**：
界面自检（``scripts/shot.mjs``）会选一次「粤语」、点一颗标签，这两样都得库里先有，
才谈得上"点下去列表跟着变"。所以每条音色带一份元信息（见 :data:`META`），
扫描时从 sidecar 读进来——不是在脚本里现编，走的正是用户改元信息那条路。

**另外两件事不在磁盘上**（都只在 ``VOICEHUB_DATA_DIR`` 指着的隔离数据目录下才写，
不设那个环境变量就跳过，绝不往你日常那份 ``hub.db`` 里塞东西）：

- 「引擎卸载了、音色还留在库里」那种残留条目（见 :func:`seed_leftovers`）——左栏
  「已卸载的引擎」那一段的唯一来源，而那段只有存在时才渲染；
- 一条"生成过东西的平台音色"（见 :func:`seed_generated`）——左栏那条**引擎保存目录**
  行、以及"引擎目录里的音色右侧要和平台音色同一套"这两条断言的来源。它的文件夹名与
  显示名**故意不一样**：那正是当初把生成样例弄丢的那种数据。
"""

from __future__ import annotations

import math
import os
import struct
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "demo_library"

#: 「引擎已经不在了」那条残留：``source`` → 它留下几条音色。
#:
#: 名字用 ASCII（``source`` 会出现在 ``?source=`` 查询串里），要不要显示成别的
#: 由界面决定——这里刻意只给一个**像卸载后的引擎**的名字。
LEFTOVERS: dict[str, tuple[str, ...]] = {
    "legacy_engine": ("旧引擎音色甲", "旧引擎音色乙"),
}

VOICES: dict[str, dict[str, list[tuple[str, str, float]]]] = {
    "原神": {
        "胡桃": [
            ("默认", "往生堂第七十七代堂主，就是我了。", 440),
            ("开心", "吃饱喝饱，一路走好！", 520),
            ("难过", "呜……生老病死，天地万象。", 330),
        ],
        "钟离": [
            ("默认", "欲买桂花同载酒，终不似，少年游。", 300),
            ("沉稳", "契约既成，食言者当受食岩之罚。", 260),
        ],
    },
    "明日方舟": {
        "阿米娅": [
            ("默认", "博士，您还有许多事情需要处理，现在还不能休息哦。", 480),
        ],
    },
    # 「语言」那一维至少要有一条粤语：筛选栏点下去要**真剩下人**，
    # 否则"筛得动"这条永远只能靠"列表变空了"来假装。
    "粤剧": {
        "红线女": [
            ("默认", "落花满天蔽月光，借一杯附荐凤台上。", 380),
        ],
    },
}


#: 每条音色写进 sidecar 的元信息（``voice.json``）。字段名与
#: ``app/infra/sidecar.Sidecar`` 一一对应——**界面能改的那几格，这里就是初值**。
#: 值的写法用**代号**（``female`` / ``young`` / ``yue``），中文只在词表那一处：
#: 代号落进 sidecar 与 SQLite，显示名由后端的词表翻。
META: dict[tuple[str, str], dict[str, object]] = {
    ("原神", "胡桃"): {
        "gender": "female",
        "age": "young",
        "language": "zh",
        "tags": ["活泼", "俏皮"],
        "description": "往生堂第七十七代堂主。",
    },
    ("原神", "钟离"): {
        "gender": "male",
        "age": "middle",
        "language": "zh",
        "tags": ["沉稳"],
        "description": "往生堂的客卿。",
    },
    ("明日方舟", "阿米娅"): {
        "gender": "female",
        "age": "teen",
        "language": "zh",
        "tags": ["认真"],
        "description": "罗德岛的公开领袖。",
    },
    ("粤剧", "红线女"): {
        "gender": "female",
        "age": "middle",
        "language": "yue",
        "tags": ["唱腔"],
        "description": "粤剧花旦。",
    },
}


#: 演示用的一条**生成物**：一条平台音色 + 它保存目录里的一段音频（见
#: :func:`seed_generated`）。
#:
#: 它刻意长成"当初坏掉的那个样子"：**文件夹名与显示名不一样**。写盘那会儿显示名还
#: 空着，用的是音色 id（``demo_gen``）；后来用户把显示名改成「演示生成音色」。
#: 从前读回时按显示名去找文件夹，于是这条音色的生成样例整段消失——这段数据就是
#: 那条断链的复现条件，改完之后它必须仍然认得出（靠 sidecar 里记的归属 id）。
GENERATED: dict[str, str] = {
    "engine": "minimax",
    "label": "MiniMax",
    "voice_id": "minimax:demo_gen",
    "voice_name": "demo_gen",
    "display": "演示生成音色",
    "text": "这一段是这台引擎生成出来的。",
    "file": "20260101-000000-01.wav",
}

#: 另一条平台音色：**一段音频都没有**。它在这儿只为让"有生成样例的排在前面"这条
#: 验得到——只造一条带样例的话，顺序是看不出来的（它排第几都一样）。
PLAIN: dict[str, str] = {
    "engine": "minimax",
    "voice_id": "minimax:demo_plain",
    "voice_name": "demo_plain",
    "display": "演示素音色",
    "description": "演示用的平台音色（还没有生成过）。",
}

#: 文件名里的结构标记：这个名字的音频 = 这个音色的主样例（见
#: ``app/domain/scanner.py`` 的 ``DEFAULT_MARKS`` 与 ``main_sample_of``）。
#: 它**不是情绪**，sidecar 里也不给它写 emotion。
MARK_STEMS: frozenset[str] = frozenset({"默认", "default", "主"})


def sample_entry(name: str, text: str) -> dict[str, str]:
    """一条样例在 sidecar 里的样子（说话内容 + 情绪）。

    「默认」那一类只写说话内容：把 "默认" 当情绪写进去的话，库里会多出一条
    标签叫「默认」的音频——而主样例是靠**文件名**登记的，不需要在样例上再打
    一个"默认"的标签。
    """
    entry = {"text": text}
    if name not in MARK_STEMS:
        entry["emotion"] = name
    return entry


def make_wav(path: Path, *, seconds: float = 1.2, freq: float = 440.0) -> None:
    """一段**有音节起伏**的占位音频。

    从前这里是一条等幅正弦：文件小、跑得快，但详情页那条波形就是**一根直线**——
    看起来像画坏了，而它其实是在老实反映文件。演示库的作用是让界面自检与截图
    看得见真东西，所以给这条占位音一个接近说话的形状：每 0.19 秒一个音节、
    音节之间留一口气，音节内幅度起落，再叠两层泛音把"一根正弦"变成"一团声音"。

    它仍然不是人声——只是**形状**上像一句话。这样"波形有没有跟着音频走"这件事，
    肉眼就能分得开：等幅信号的波形本来就该是平的。
    """
    rate = 22050
    total = int(rate * seconds)
    #: 音节表：**长度与轻重都不一样**。等长的半正弦画出来就是五个一模一样的鼓包，
    #: 一看就是画上去的；真实的一句话，音节有长有短、有重有轻。
    syllables = (
        (0.17, 1.0),
        (0.10, 0.66),
        (0.21, 0.88),
        (0.08, 0.48),
        (0.16, 0.95),
        (0.12, 0.58),
        (0.19, 0.82),
    )
    gap = 0.045
    plan: list[tuple[float, float, float]] = []
    cursor = 0.02
    for length, weight in syllables:
        plan.append((cursor, cursor + length, weight))
        cursor += length + gap
    frames = bytearray()
    for i in range(total):
        t = i / rate
        # 气口留一点底噪：真实录音不是绝对静音，波形上也不该是一刀切平的零。
        env = 0.015
        for start, end, weight in plan:
            if start <= t < end:
                local = (t - start) / (end - start)
                env = weight * (math.sin(math.pi * local) ** 0.6)
                # 音节内部还有一层快一点的起伏：真实说话的能量不是一条光滑的钟形，
                # 少了这一层，波形边缘会干净得像打印出来的。
                env *= 0.74 + 0.26 * math.sin(2 * math.pi * 7.3 * t + weight * 3)
                break
        # 头尾淡入淡出，避免爆音
        env *= min(1.0, i / 400, (total - i) / 400)
        # 轻微音高游移：真实说话不会钉在一个频率上。
        wobble = 1.0 + 0.004 * math.sin(2 * math.pi * 3.1 * t)
        voice = (
            math.sin(2 * math.pi * freq * wobble * t)
            + 0.35 * math.sin(2 * math.pi * freq * 2 * wobble * t)
            + 0.16 * math.sin(2 * math.pi * freq * 3 * wobble * t)
        )
        frames += struct.pack("<h", int(9000 * env * voice / 1.51))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))


def seed_leftovers() -> int:
    """往**隔离数据目录**的 ``hub.db`` 里塞几条"引擎已卸载"的残留音色。

    **为什么非要有它**：左栏「已卸载的引擎」那一段（以及"点它不白屏"那条断言）
    只有库里真的存在这种行时才验得到；而它凭空造不出来——那需要先装一台引擎、
    拉到音色、再把插件卸掉。所以演示环境直接把**结果**写进库。

    安全阀：``VOICEHUB_DATA_DIR`` 没设就**直接跳过**。不设的时候 ``data_dir()``
    会落到 ``%LOCALAPPDATA%\\voicehub``，那正是日常用的那一份——一个演示脚本
    不该往里面塞东西。
    """
    root = os.environ.get("VOICEHUB_DATA_DIR", "").strip()
    if not root:
        print("（跳过「卸载残留」：没设 VOICEHUB_DATA_DIR——不碰日常那份库）")
        return 0

    import sqlite3
    import time

    db = Path(root) / "hub.db"
    if not db.exists():
        print(f"（跳过「卸载残留」：{db} 还不存在——先起一次服务让它建库）")
        return 0

    # id 的形态与 ``app.domain.models.platform_voice_id`` 一致（``平台:音色id``）。
    # 直接写字符串而不 import 那个函数：这是个一次性的演示脚本，不该牵着 app 走。
    now = time.time()
    written = 0
    conn = sqlite3.connect(str(db))
    try:
        for source, names in LEFTOVERS.items():
            for index, name in enumerate(names, start=1):
                conn.execute(
                    "insert or replace into voices "
                    "(id, source, name, display, category, description, updated_at) "
                    "values (?, ?, ?, ?, '', ?, ?)",
                    (
                        f"{source}:{index}",
                        source,
                        name,
                        name,
                        "这台引擎的插件已经不在了，音色还留在库里。",
                        now,
                    ),
                )
                written += 1
        conn.commit()
    finally:
        conn.close()
    return written


def seed_generated() -> str:
    """在**隔离数据目录**里造一条"生成过东西的平台音色"，返回它的音色 id。

    三件事一起造，缺一件就验不到：

    1. **一条平台音色**（写进 ``hub.db``，``source`` 是装了的那台引擎）——"这台引擎
       自己的音色"那一半；
    2. **它保存目录里的一段音频**（``generated/<引擎>/<音色名>/…``）——"生成物"那一半，
       同时也是左栏那条**引擎保存目录**行的来源；
    3. **sidecar 里记的归属**（``origin.voice_id``）——把两半认成同一个东西的那根线。
       没有它，读回只能靠名字，而这段数据的文件夹名（``demo_gen``）与显示名
       （「演示生成音色」）**故意不一样**：名字那条路必然断。

    安全阀与 :func:`seed_leftovers` 同一条：``VOICEHUB_DATA_DIR`` 没设就跳过，
    绝不往日常那份库里塞东西。
    """
    root = os.environ.get("VOICEHUB_DATA_DIR", "").strip()
    if not root:
        print("（跳过「生成物」：没设 VOICEHUB_DATA_DIR——不碰日常那份库）")
        return ""

    import json
    import sqlite3
    import time

    db = Path(root) / "hub.db"
    if not db.exists():
        print(f"（跳过「生成物」：{db} 还不存在——先起一次服务让它建库）")
        return ""

    item = GENERATED
    folder = Path(root) / "generated" / item["label"] / item["voice_name"]
    # 真的是一段音频（正弦波），不是空文件：卡片上那颗 ▶ 与试听走的都是它。
    make_wav(folder / item["file"], seconds=0.6, freq=560)
    (folder / "voice.json").write_text(
        json.dumps(
            {
                "version": 1,
                # **归属**：这些音频属于库里哪条音色。读回时按它认，名字只作兜底。
                "origin": {"voice_id": item["voice_id"], "engine": item["engine"]},
                "samples": {
                    item["file"]: {
                        "text": item["text"],
                        "emotion": "开心",
                        # 生成这一段时用的参数（形状照 archive.record_sample）。
                        "params": {"speed": "1.1"},
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    now = time.time()
    conn = sqlite3.connect(str(db))
    try:
        for entry in (item, PLAIN):
            conn.execute(
                "insert or replace into voices "
                "(id, source, name, display, category, description, updated_at) "
                "values (?, ?, ?, ?, '', ?, ?)",
                (
                    entry["voice_id"],
                    entry["engine"],
                    entry["voice_name"],
                    entry["display"],
                    entry.get("description", "演示用的平台音色。"),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return item["voice_id"]


def main() -> None:
    count = 0
    for category, voices in VOICES.items():
        for name, samples in voices.items():
            folder = ROOT / category / name
            for emotion, text, freq in samples:
                make_wav(folder / f"{emotion}.wav", freq=freq)
                count += 1
            # 说话内容放进 sidecar（正本存储），不生成散落的 .txt。
            import json

            payload: dict[str, object] = {"version": 1}
            payload.update(META.get((category, name), {}))
            payload["samples"] = {f"{e}.wav": sample_entry(e, t) for e, t, _ in samples}
            (folder / "voice.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    print(f"演示音色库已生成：{ROOT}（{count} 条样例）")
    leftovers = seed_leftovers()
    if leftovers:
        print(f"演示用的「卸载残留」已写入：{leftovers} 条（source={','.join(LEFTOVERS)}）")
    generated = seed_generated()
    if generated:
        print(f"演示用的「生成物」已写入：{generated}")


if __name__ == "__main__":
    main()
