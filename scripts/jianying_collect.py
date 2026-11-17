"""剪映「文本朗读」音色试听采集器——点一条，收一条，直接落成 VoiceHub 音色库。

**为什么是这个形状**（实测结论，见 ``_catalog/jianying_tones.json`` 的来历）：
剪映的八百多条音色全部在云端，本地清单里**没有任何试听音频地址**
（``download_info.url`` 全空、没有 preview 字段、安装包 ``Resources/tts`` 只有一张
40 行的兜底表）。唯一的音频来源是：在「文本朗读」面板点一张音色卡，客户端拿
当前编辑区的文字去云端合成一次，结果落进
``User Data/Cache/tryInputText/<md5>/<md5>_0.mp3``（多数其实是 24kHz WAV）。
目录名是 md5，日志里查不到它与音色的对应关系，所以**只能靠"刚点的就是刚落地的"**
来配对——本脚本据此把「下一个该点谁」打在终端上，看到新文件就认领、归档、再报下一个。

用法（剪映开着、面板停在某一类音色上）::

    python scripts/jianying_collect.py --harvest               # 先收已点过的：认不回名字，按序号入库
    python scripts/jianying_collect.py --favorites             # 用「收藏时间戳」给序号音色的那批补真名
    python scripts/jianying_collect.py --category 解说          # 按面板顺序逐条点
    python scripts/jianying_collect.py --category 解说 --limit 20  # 先收 20 条试试
    python scripts/jianying_collect.py --categories 女声,男声   # 一次排几类
    python scripts/jianying_collect.py --status                 # 看进度，不采集
    python scripts/jianying_collect.py --verify                 # 库 / map / sidecar 三方对账（只读）

采集过程中按键：**n** 这条不要了（跳过）· **u** 收回上一条（点错了）·
**空格** 暂停/继续 · **q** 保存退出。退出后重跑同一条命令会从断点接着走。

**已点过的还能不能追回名字**：能，但只限你**收藏过**的那几条。收藏是服务器打时间的
（面板缓存里的 ``favorite_time``，秒），试听落盘是本地时间（``lru_cache.json``，毫秒），
两者是同一个动作触发的，正常只差 1~2 秒——一配就中。所以顺手点个收藏，回头就能定名；
没收藏的只能听声音自己改。差超过 ``--trust``（默认 3 秒）的那些不当真，只写进
``_catalog/favorite_match.json`` 当候选：实测差 5 秒以上的里有六条基频和剪映自己的性别
标签反号，是硬凑的。收藏状态得让面板重拉一次才会进缓存（切走分类再切回来）。

产出：``<库>/<剪映>/<音色名>/默认.wav`` + ``voice.json``。平铺一层，一条音色一个文件夹
（不是一个"试听"大文件夹——VoiceHub 里音色 id 就是路径的散列，堆一起改一条会连着动一堆）。
sidecar 只写能如实给的：性别/年龄/语言（剪映自带标签，认不出留空）+ 逐条听写的文本，
**不写描述和标签**——那种东西听着碍事，还得手动删。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
import wave
from collections import Counter
from datetime import datetime
from pathlib import Path

# 音色名/分类名里带 🔥 这类 emoji，GBK 控制台打印会崩，先把输出换成 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
TRY_DIR = Path("D:/JianyingPro/User Data/Cache/tryInputText")
LIB_ROOT = Path("F:/ai/语音模型/voice-models/剪映")
CATALOG = LIB_ROOT / "_catalog/jianying_tones.json"
ASR_URL = "http://127.0.0.1:18999"
#: 音色目录里算"有音"的后缀（VoiceHub 认的就是这几个）。
AUDIO_EXT = frozenset({".wav", ".mp3", ".m4a", ".ogg", ".flac"})
#: 偶有试听是真 MP3，解不了 WAV 头时用它转一下再测基频。
FFMPEG = shutil.which("ffmpeg") or str(
    Path.home()
    / "AppData/Local/Programs/Python/Python310/ffmpeg"
    / "ffmpeg-2022-07-04-git-dba7376d59-essentials_build/bin/ffmpeg.exe"
)
#: 一次点出多条试听时，认不出来的那些先存这儿（在音色库之外，免得扫进库）。
UNCLAIMED = Path.home() / ".cache" / "jianying_collect" / "未认领"

#: 一条音色属于多个分类时，文件夹放哪个。越靠前越优先——「热门/最新」是运营位，
#: 不是音色属性，永远不该成为目录名。
CATEGORY_PRIORITY = [
    "解说",
    "影视动漫",
    "角色演绎",
    "情感",
    "新闻",
    "营销",
    "娱乐",
    "多风格",
    "女声",
    "男声",
    "童声",
    "方言",
    "外语",
    "游戏",
    "趣味歌唱",
    "特邀大咖",
    "🔥超仿真",
    "热门",
    "最新",
]

#: 剪映标签 → VoiceHub 词表代号（值必须是 app/domain/models.py 里那张表的值）。
GENDER_TAG = {"女": "female", "女声": "female", "男": "male", "男声": "male"}
AGE_TAG = {
    "儿童": "child",
    "宝宝": "child",
    "童声": "child",
    "少年": "teen",
    "青少年": "teen",
    "少女": "teen",
    "青年": "young",
    "成年": "young",
    "中年": "middle",
    "大叔": "middle",
    "老年": "senior",
    "奶奶": "senior",
}
DIALECT = {
    "粤语": "yue",
    "四川话": "zh",
    "东北话": "zh",
    "上海话": "zh",
    "河南话": "zh",
    "湖南话": "zh",
    "潮汕话": "zh",
    "台湾腔": "zh",
    "闽南语": "zh",
    "西安话": "zh",
}
FOREIGN = {
    "英文": "en",
    "英语": "en",
    "日语": "ja",
    "韩语": "ko",
    "泰语": "th",
    "越南语": "vi",
    "印尼语": "id",
    "西班牙语": "es",
    "法语": "fr",
    "德语": "de",
    "俄语": "ru",
    "葡萄牙语": "pt",
    "意大利语": "it",
    "阿拉伯语": "ar",
    "土耳其语": "tr",
    "印地语": "hi",
}

BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# --------------------------------------------------------------------------- 清单


def primary_category(tones: list[dict]) -> None:
    """给每条音色挑一个目录分类（就地写 ``folder_category``）。"""
    rank = {name: i for i, name in enumerate(CATEGORY_PRIORITY)}
    for t in tones:
        cats = [c for c in t.get("categories", []) if c in rank]
        t["folder_category"] = (
            min(cats, key=lambda c: rank[c]) if cats else (t.get("categories") or ["其他"])[0]
        )


def meta_of(tone: dict) -> dict:
    """剪映标签 → sidecar 字段。认不出的一律留空，让扫描/用户去补。

    **只填这三项，不写 tags / description**：分类和 speaker 那串都在清单
    （``_catalog/jianying_tones.csv``）里，写进 sidecar 只是给用户添"要删的废话"。
    """
    gender = age = language = ""
    for tag in tone.get("tags") or []:
        if not gender and tag in GENDER_TAG:
            gender = GENDER_TAG[tag]
            continue
        if not age and tag in AGE_TAG:
            age = AGE_TAG[tag]
            continue
        if not language and (tag in DIALECT or tag in FOREIGN):
            language = (DIALECT if tag in DIALECT else FOREIGN)[tag]
    if not language and "中文" in (tone.get("tags") or []):
        language = "zh"  # 「中文」是语言，不是音色标签——没有更具体的方言/外语时落到 language
    return {"gender": gender, "age": age, "language": language}


def load_queue(args, tones: list[dict]) -> list[dict]:
    wanted = (
        [c.strip() for c in re.split(r"[,，]", args.categories or "") if c.strip()]
        if args.categories
        else []
    )
    if args.category:
        wanted = [args.category] + wanted
    queue = []
    seen = set()
    for t in tones:
        cats = set(t.get("categories", [])) | {t.get("folder_category", "")}
        if wanted and not (cats & set(wanted)):
            continue
        if t["effect_id"] in seen:
            continue
        seen.add(t["effect_id"])
        queue.append(t)
    if wanted:
        order = {name: i for i, name in enumerate(wanted)}
        queue.sort(
            key=lambda t: (
                min(
                    (order[c] for c in set(t["categories"]) | {t["folder_category"]} if c in order),
                    default=99,
                ),
                t["folder_category"],
                t["title"],
            )
        )
    assign_names(queue)
    if args.skip_done:
        queue = [t for t in queue if not done_folder(t)]
    return queue[: args.limit] if args.limit else queue


def assign_names(queue: list[dict]) -> None:
    """给队列里的音色定文件夹名：重名的尾缀 effect_id 后四位。

    同名不同人在剪映里确实有（两条「TVB女声」）：尾缀只是为了两条音频别堆进同一个
    目录。只看队列、不看磁盘，所以两次跑出来的名字一致。
    """
    taken: set[str] = set()
    for t in queue:
        name = safe_name(t["title"])
        if name in taken:
            name = f"{name}·{re.sub(r'[^0-9A-Za-z]', '', t['effect_id'])[-4:] or 'x'}"
        taken.add(name)
        t["folder_name"] = name


def safe_name(raw: str) -> str:
    name = BAD_NAME.sub(" ", (raw or "").strip()).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return (name or "未命名")[:48]


def target_folder(tone: dict) -> Path:
    """音色落盘位置：``<库>/<音色名>/``——**平铺**，不再套分类目录。

    分类不进路径：VoiceHub 拿整段相对路径当分类，套一层以后同一个音色在
    "剪映" 和 "剪映/男声" 之间换分类，音色 id 跟着换人。分类留在清单里够用了，
    库目录只按名字排，好找也好改（重名才加 effect 尾缀）。
    """
    name = tone.get("folder_name") or safe_name(tone["title"])
    return LIB_ROOT / name


def paired_effects() -> dict[str, str]:
    """``--favorites`` 已经认过名的音色：``effect_id → 库里的文件夹名``。

    名字会撞（清单里真有两条「TVB女声」），**id 不会**——所以"这条音色库里是不是已经有了"
    最终得问 id，别问文件夹名。明细只记已改名（``apply``）的那些。
    """
    path = LIB_ROOT / "_catalog" / "favorite_match.json"
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(r["effect_id"]): str(r["folder"]) for r in rows if r.get("apply") and r.get("effect_id")
    }


def _has_audio(folder: Path) -> bool:
    return folder.is_dir() and any(
        f.stat().st_size > 1024 for f in folder.iterdir() if f.suffix.lower() in AUDIO_EXT
    )


def done_folder(tone: dict) -> Path | None:
    """这条音色库里是否已经有音：按路径认，或按 ``--favorites`` 记过的 id 认。"""
    for folder in (
        target_folder(tone),
        LIB_ROOT / (tone.get("folder_name") or safe_name(tone["title"])),
    ):
        if _has_audio(folder):
            return folder
    hit = paired_effects().get(str(tone.get("effect_id", "")))
    if hit and _has_audio(LIB_ROOT / hit):
        return LIB_ROOT / hit
    return None


# ----------------------------------------------------------------------- 试听监听


def collected_dirs() -> set[Path]:
    """库里已经有音频的音色目录。

    两层是点了名的（``剪映/<分类>/<音色名>/``），一层是 --harvest 回收的
    （``剪映/剪映试听NN/``）——都是每条音色一个文件夹，不再套「试听待命名」。
    音频名按后缀认（不按 ``默认.*``）：改名脚本出过时 ``默认 (2).wav`` 这种残留，
    只认 ``默认.*`` 会把那条整个漏掉、计数少 1。
    """
    out: set[Path] = set()
    for pattern in ("*/*", "*/*/*"):
        for p in LIB_ROOT.glob(pattern):
            hidden = any(part.startswith("_") for part in p.relative_to(LIB_ROOT).parts)
            if (
                p.is_file()
                and not hidden
                and p.suffix.lower() in AUDIO_EXT
                and p.stat().st_size > 1024
            ):
                out.add(p.parent)
    return out


def snapshot(try_dir: Path) -> set[Path]:
    return {p for p in try_dir.glob("*") if p.is_dir()} if try_dir.exists() else set()


def newest_audio(dir_path: Path) -> Path | None:
    files = [p for p in dir_path.iterdir() if p.is_file() and p.stat().st_size > 0]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def wait_stable(dir_path: Path, timeout: float, settle: float = 0.6) -> Path | None:
    """等这个目录里出现一个写完的文件（大小连续两次不变）。"""
    deadline = time.time() + timeout
    last, last_at = -1, 0.0
    while time.time() < deadline:
        f = newest_audio(dir_path)
        if f:
            size = f.stat().st_size
            now = time.time()
            if size == last and now - last_at >= settle:
                return f
            last, last_at = size, now
        time.sleep(0.15)
    return newest_audio(dir_path)


def is_riff(path: Path) -> bool:
    try:
        return path.open("rb").read(4) == b"RIFF"
    except OSError:
        return False


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0


# --------------------------------------------------------------------------- ASR


def asr_text(sample: Path) -> str:
    """听写第一段，作为本批试听文本；听不了就留空，不拖累采集。"""
    if not sample.exists():
        return ""
    try:
        sys.path.insert(0, str(REPO))
        from app.engines.builtin.qwen3_asr import client
        from app.engines.builtin.qwen3_asr.settings import Qwen3AsrSettings
    except Exception:  # noqa: BLE001
        return ""
    tmp = Path.home() / ".cache" / "jianying_collect"
    tmp.mkdir(parents=True, exist_ok=True)
    wav = tmp / f"asr_{int(time.time())}.wav"
    try:
        wav.write_bytes(sample.read_bytes())
        tr = client.transcribe(Qwen3AsrSettings(base_url=ASR_URL), wav)
        return (tr.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    finally:
        wav.unlink(missing_ok=True)


# ------------------------------------------------------------------------ 归档


def clean_tags(tone: dict) -> list[str]:
    """剪映自带的标签里挑出能用来筛的：去掉内部标记、场景前缀和表情。

    「场景_角色演绎」「in-house」「UGC」「热门/最新」这类是给面板自己分组用的，
    落到音色库里只会碍事；「女/青年/开心/解说/小说朗读」这些才是人想筛的。
    """
    skip = {
        "UGC",
        "in-house",
        "热门",
        "最新",
        "通用",
        "人类",
        "非人类",
        "其他",
        "中文",  # 语言，已进 language
        "角色演绎",  # 分类/玩法，不是音色特质
        "多风格",
        "多情绪",
        "IP",
        "多元性别/其他",
        "无年龄设定",
        "外部引入-微软",
    }
    # 性别/年龄已经有 sidecar 的 gender、age 字段，再留在 tags 里就是重复的；
    # 方言/外语标签反而要留——language 只写词表代号（四川话也记 zh），标签才是细节。
    taken = set(GENDER_TAG) | set(AGE_TAG)
    out: list[str] = []
    for raw in tone.get("tags") or []:
        tag = str(raw).strip()
        if not tag or tag in skip or tag in taken or tag.startswith("场景_"):
            continue
        if any(ord(ch) > 0xFFFF for ch in tag):  # 表情
            continue
        if tag not in out:
            out.append(tag)
    return out[:10]


def register_capture(src: Path, out: Path) -> None:
    """把实时收到的这一段记进 harvest_map（键沿用缓存音频的内容 md5）。

    不记的话 ``--verify`` 会把这条当成"目录没被 map 记录"，而 ``--harvest`` 将来
    还会把同一段再收一遍。
    """
    hm_path = LIB_ROOT / "_catalog" / "harvest_map.json"
    hm = json.loads(hm_path.read_text(encoding="utf-8")) if hm_path.exists() else {}
    digest = hashlib.md5(src.read_bytes()).hexdigest()
    seconds = wav_seconds(out) if out.suffix == ".wav" else 0.0
    hm[digest] = {
        "folder": out.parent.name,
        "clip": src.name,
        "seconds": round(seconds, 2),
        "f0": round(median_f0(out), 1),
        "clicked": datetime.fromtimestamp(src.stat().st_mtime).strftime("%m-%d %H:%M:%S.%f")[:-3],
    }
    hm_path.parent.mkdir(parents=True, exist_ok=True)
    hm_path.write_text(json.dumps(hm, ensure_ascii=False, indent=1), encoding="utf-8")


def archive(src: Path, tone: dict, text: str) -> Path:
    folder = target_folder(tone)
    folder.mkdir(parents=True, exist_ok=True)
    if is_riff(src):
        out = folder / "默认.wav"
        shutil.copyfile(src, out)
    else:
        out = folder / "默认.wav"
        if not to_wav(src, out):
            out = folder / "默认.mp3"
            shutil.copyfile(src, out)
    sidecar = folder / "voice.json"
    meta = meta_of(tone)
    body: dict = {"version": 1}
    for key in ("gender", "age", "language"):
        if meta[key]:
            body[key] = meta[key]
    tags = clean_tags(tone)
    if tags:
        body["tags"] = tags
    body["samples"] = {out.name: {"text": text}}
    sidecar.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    register_capture(src, out)
    return out


# ------------------------------------------------------------------ 回收已点过的

HARVEST_PREFIX = "剪映试听"


def pcm16k(path: Path):
    """解成 16kHz 单声道 float，够算基频用。剪映的试听多数是 WAV，偶尔是 MP3。"""
    import numpy as np

    if is_riff(path):
        with wave.open(str(path), "rb") as w:
            raw = w.readframes(w.getnframes())
            sr = w.getframerate()
            data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if w.getnchannels() > 1:
                data = data.reshape(-1, w.getnchannels()).mean(axis=1)
    else:
        proc = subprocess.run(
            [
                FFMPEG,
                "-v",
                "error",
                "-i",
                str(path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                "-",
            ],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None, 0
        data = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
        sr = 16000
    if sr != 16000 and len(data):
        keep = int(len(data) * 16000 / sr)
        data = data[:keep]
    return data, 16000


def median_f0(path: Path) -> float:
    """整段的中位基频（Hz），认不出来给 0。只做粗判：成人男声 <160 <女声。"""
    import numpy as np

    samples, sr = pcm16k(path)
    if samples is None or len(samples) < sr:
        return 0.0
    win, hop = 1024, 512
    floor = max(0.01, float(np.abs(samples).max()) * 0.05)
    lo, hi = int(sr / 400.0), int(sr / 70.0)
    vals: list[float] = []
    for start in range(0, len(samples) - win, hop):
        frame = samples[start : start + win]
        rms = float(np.sqrt(np.mean(frame * frame)))
        if rms < floor:
            continue
        frame = frame - frame.mean()
        corr = np.fft.irfft(np.abs(np.fft.rfft(frame, 2 * win)) ** 2)[: hi + 1]
        if corr[lo] <= 0:
            continue
        peak = int(np.argmax(corr[lo : hi + 1])) + lo
        vals.append(sr / peak)
    return float(np.median(vals)) if len(vals) >= 4 else 0.0


def to_wav(src: Path, dst: Path) -> bool:
    """真 MP3 的试听转成 WAV：VoiceHub 本机没带 ffprobe，MP3 的时长读不出来。"""
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-i", str(src), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 44


def harvest(args) -> int:
    """把已经点过的试听全部搬进音色库：每条一个文件夹，直接放在「剪映」分类下。

    点过的东西认不回音色名（md5 目录、日志里都查不到对应），所以名字只能是序号；
    sidecar 里只写能如实给的：基频推的性别、语言、逐条听写的文本——
    先在界面上听着改名，比对着剪映一条条点要快得多。
    """
    clips = sorted(
        (p for p in args.try_dir.glob("*/*") if p.is_file() and p.stat().st_size > 4096),
        key=lambda p: p.stat().st_mtime,
    )
    if not clips:
        print(f"{args.try_dir} 里没有音频。")
        return 1
    map_path = LIB_ROOT / "_catalog" / "harvest_map.json"
    done = {}
    if map_path.exists():
        try:
            done = json.loads(map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            done = {}
    fresh = []
    for c in clips:
        digest = hashlib.md5(c.read_bytes()).hexdigest()
        if digest not in done:
            fresh.append((digest, c))
    print(f"试听缓存 {len(clips)} 条，库里还没收过的 {len(fresh)} 条。")
    if not fresh:
        return 0

    base = len(done)
    width = max(2, len(str(base + len(fresh))))
    for i, (digest, clip) in enumerate(fresh, 1):
        seq = base + i
        folder = LIB_ROOT / f"{HARVEST_PREFIX}{seq:0{width}d}"
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / "默认.wav"
        if is_riff(clip):
            shutil.copyfile(clip, out)
        elif not to_wav(clip, out):
            out = folder / "默认.mp3"
            shutil.copyfile(clip, out)
        seconds = wav_seconds(out) if out.suffix == ".wav" else 0.0
        f0 = median_f0(out)
        text = "" if args.no_asr else asr_text(out)
        gender = "male" if 0 < f0 < 160 else ("female" if f0 >= 185 else "")
        heard = f"{f0:.0f}Hz" if f0 else "基频没测出"
        body: dict = {"version": 1}
        if gender:
            body["gender"] = gender
        if text and re.search(r"[一-鿿]", text):
            body["language"] = "zh"
        body["samples"] = {out.name: {"text": text}}
        (folder / "voice.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        done[digest] = {
            "folder": str(folder.relative_to(LIB_ROOT)),
            "clip": clip.name,
            "seconds": round(seconds, 2),
            "f0": round(f0, 1),
        }
        map_path.parent.mkdir(parents=True, exist_ok=True)
        map_path.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
        print(
            f"  [{i}/{len(fresh)}] {folder.relative_to(LIB_ROOT)} "
            f"{seconds:5.1f}s {heard:>9} {'男' if gender == 'male' else '女' if gender == 'female' else '?'} "
            f"{text[:24]}"
        )
    print(f"\n收了 {len(fresh)} 条 → {LIB_ROOT}")
    print("剪映那份缓存没动（它自己会按 LRU 清）；确认库里有音了再删不迟。")
    return 0


# ------------------------------------------------------------------ 按收藏时间戳配对

PANEL_DB = Path("D:/JianyingPro/User Data/Cache/ressdk_db/12690222373077278714/rp.db")
PANEL_URL = "24A486D98227817A32049E4D52BEFAEF"  # 「文本朗读」面板的固定 hash


def favorite_times() -> dict[str, int]:
    """面板缓存里被收藏过的音色：``effect_id → favorite_time``（服务器秒级时间戳）。

    收藏状态只有重新拉面板才刷新——在剪映里点了收藏，得让「文本朗读」面板重新加载一次
    （切走分类再切回来也行），缓存里才会有新的 ``favorite_time``。
    """
    import sqlite3

    if not PANEL_DB.exists():
        raise SystemExit(f"找不到剪映的面板缓存：{PANEL_DB}")
    conn = sqlite3.connect(f"file:{PANEL_DB.as_posix()}?mode=ro", uri=True)
    row = conn.execute(
        "select response_body from http_cache where url like ? order by id desc limit 1",
        (f"%get_panel_info_{PANEL_URL}%",),
    ).fetchone()
    if row is None:
        raise SystemExit("缓存里没有「文本朗读」面板——先在剪映打开该面板，让它重拉一次清单。")
    body = row[0]
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    out: dict[str, int] = {}
    for block in json.loads(body)["data"]["category_resources"].values():
        for it in block.get("effect_item_list") or []:
            ca = it.get("common_attr") or {}
            stamp = ca.get("favorite_time")
            eid = ca.get("effect_id")
            if eid and stamp:
                out.setdefault(eid, int(stamp))
    return out


def click_times(args) -> list[tuple[float, str]]:
    """已收进库的试听：``(落盘时刻, 库里的文件夹名)``，按时间排序。

    时刻优先取 ``lru_cache.json`` 的毫秒键（试听写进缓存的那一刻），缓存已被 LRU
    清掉的退回 harvest_map 里记的 ``clicked``。
    """
    map_path = LIB_ROOT / "_catalog" / "harvest_map.json"
    if not map_path.exists():
        return []
    hm = json.loads(map_path.read_text(encoding="utf-8"))
    lru: dict[str, float] = {}
    lru_path = args.try_dir / "lru_cache.json"
    if lru_path.exists():
        for key, target in json.loads(lru_path.read_text(encoding="utf-8")).items():
            lru[Path(target).name] = int(key) / 1000.0
    rows = []
    for entry in hm.values():
        if entry.get("discarded"):
            continue  # 用户按"认不出来就丢掉"删掉的：别拿它去认领收藏
        folder = entry["folder"]
        # 缓存目录名是 clip 文件名的**前一段**（``<md5>_0.mp3``）——直接用 stem 会把
        # ``_0`` 一起带上，lru 永远查不中，配对只能退回秒级时刻（差值全成整数）。
        digest = Path(entry["clip"]).name.split("_")[0]
        when = lru.get(digest)
        if when is None and entry.get("clicked"):
            # 退回 map 里记的时刻（剪映缓存被 LRU 清掉之后只剩这个）：带毫秒、不带毫秒都可能。
            text = str(entry["clicked"])
            fmt = "%m-%d %H:%M:%S.%f" if "." in text else "%m-%d %H:%M:%S"
            when = datetime.strptime(text, fmt).replace(year=2026).timestamp()
        if when is None:
            continue
        rows.append((when, folder))
    rows.sort()
    return rows


def favorites(args) -> int:
    """把「收藏时间戳」和「试听落盘时间戳」一对一配上，给已收的试听补真名。

    剪映不给我们试听音频地址，也不记你点过谁，**但收藏是服务器打时间的**：
    ``favorite_time``（秒）↔ 试听写盘（毫秒）通常只差 1~2 秒，配对即可定名。
    代价是只有**收藏过**的音色能这么补——没收藏的那几条没有第二个时间来源。
    性别/年龄/语言一律以剪映自己的标签为准（基频只是猜，不做否决）。

    **分两档**：差值 ≤ ``--trust``（默认 3 秒）的直接改名；再大的只列进明细当候选，
    不落到库上。理由：收藏和试听写盘是同一个动作触发的，正常只差 1~2 秒；差到 5~10 秒
    的那些多半是"近邻被更贴的试听抢走"后硬凑的——实测这类里六条基频和剪映自己的性别
    标签反号（例如 320Hz 的试听配给「沧桑史书」）。宁可留序号让你听，也不要错名字。
    """
    favs = favorite_times()
    tones = json.loads(CATALOG.read_text(encoding="utf-8"))
    by_id = {t["effect_id"]: t for t in tones}
    clicks = click_times(args)
    if not clicks:
        print("库里还没有已收的试听（harvest_map 是空的）。先跑 --harvest。")
        return 1
    pairs = []
    for eid, stamp in favs.items():
        if eid not in by_id:
            continue
        for when, folder in clicks:
            signed = stamp - when
            delta = abs(signed)
            if args.star_after:
                # 自由点：先试听、后收藏。允许 3 秒的"星星早于写盘"（同一个动作），
                # 最晚 --max-delta 秒；按离星星的远近排序，谁最近归谁。
                if not (-3.0 <= signed <= args.max_delta):
                    continue
            elif delta > args.max_delta:
                continue
            pairs.append((delta, stamp, eid, when, folder))
    pairs.sort()
    claim_f, claim_e, rows = set(), set(), []
    for delta, stamp, eid, when, folder in pairs:
        if folder in claim_f or eid in claim_e:
            continue  # 一条音频只认一个名字，一个名字只认一条音频
        claim_f.add(folder)
        claim_e.add(eid)
        tone = by_id[eid]
        rows.append(
            {
                "delta": round(delta, 2),
                "apply": True if args.star_after else delta <= args.trust,
                "favorited": datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M:%S"),
                "clicked": datetime.fromtimestamp(when).strftime("%m-%d %H:%M:%S.%f")[:-3],
                "folder": folder,
                "title": tone.get("title", ""),
                "effect_id": eid,
                "voice_type": tone.get("voice_type", ""),
                "gender": tone.get("gender", ""),
                "age": tone.get("age", ""),
                "language": tone.get("language", ""),
            }
        )
    if args.dry_run:
        # 两个时间都摆出来：收藏是服务器秒级时刻，试听是写盘毫秒——正常只差 1~2 秒。
        for r in sorted(rows, key=lambda row: row["favorited"]):
            mark = " " if r["apply"] else "?"
            print(
                f"  {mark}收藏 {r['favorited']} · 试听 {r['clicked']} · "
                f"差 {r['delta']:>5.1f}s  {r['title']}  ←  {r['folder']}"
            )
        paired = {r["effect_id"] for r in rows}
        loose = [
            (stamp, eid)
            for eid, stamp in sorted(favs.items(), key=lambda kv: kv[1])
            if eid in by_id and eid not in paired
        ]
        if loose:
            print(f"  —— 收藏了但 {args.max_delta:g}s 内没有试听写盘的 {len(loose)} 条：")
            for stamp, eid in loose:
                t = by_id[eid]
                print(
                    f"     收藏 {datetime.fromtimestamp(stamp).strftime('%m-%d %H:%M:%S')}  "
                    f"{t.get('title', '')}  [{t.get('voice_type', '')}]"
                )
        sure = sum(1 for r in rows if r["apply"])
        print(f"（只看不改：配上 {len(rows)} 条，其中 {sure} 条差 ≤{args.trust:g}s 会改名）")
        return 0

    hm_path = LIB_ROOT / "_catalog" / "harvest_map.json"
    hm = json.loads(hm_path.read_text(encoding="utf-8"))
    renamed = 0
    for r in rows:
        if not r["apply"]:
            continue  # 差太远的只进明细，不碰库
        src = LIB_ROOT / r["folder"]
        if not src.is_dir():
            # 上一轮已经改过名了（map 里可能还是旧序号）：认出真身，把 map 补齐。
            guess = LIB_ROOT / safe_name(r["title"])
            if guess.is_dir():
                for entry in hm.values():
                    if entry["folder"] == r["folder"]:
                        entry["folder"] = guess.name
                r["folder"] = guess.name
            continue
        name = safe_name(r["title"])
        if (LIB_ROOT / name).exists() and (LIB_ROOT / name / "voice.json").exists():
            name = f"{name}·{re.sub(r'[^0-9A-Za-z]', '', r['effect_id'])[-4:]}"
        dst = LIB_ROOT / name
        side = src / "voice.json"
        body = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
        out: dict = {"version": 1}
        for key in ("gender", "age", "language"):
            if r[key]:
                out[key] = r[key]
        if body.get("samples"):
            out["samples"] = body["samples"]
        side.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        if dst != src:
            if dst.exists():
                print(f"  ! 目标已存在，没动：{name}")
                continue
            shutil.move(str(src), str(dst))
            renamed += 1
            for entry in hm.values():
                if entry["folder"] == r["folder"]:
                    entry["folder"] = name
        r["folder"] = name
    hm_path.write_text(json.dumps(hm, ensure_ascii=False, indent=1), encoding="utf-8")
    (LIB_ROOT / "_catalog" / "favorite_match.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    loose = len({d for d in collected_dirs() if d.name.startswith(HARVEST_PREFIX)})
    sure = [r for r in rows if r["apply"]]
    far = [r for r in rows if not r["apply"]]
    print(
        f"收藏 {len(favs)} 条 · 配上 {len(rows)} 条 · 差 ≤{args.trust:g}s 的 {len(sure)} 条已改名（实际移动 {renamed} 个）"
    )
    if far:
        print(f"  另 {len(far)} 条差 >{args.trust:g}s，只进明细不动库（想认就 --trust 加大）：")
        for r in far:
            print(
                f"    {r['delta']:>5.1f}s  {r['folder']:<12} ≈ {r['title']}  {r['gender'] or '?'}"
            )
    print(f"库里还是序号名的 {loose} 条 = 你没收藏过的（或收藏时没重拉面板）")
    print(f"明细：{LIB_ROOT / '_catalog' / 'favorite_match.json'}")
    return 0


# ------------------------------------------------------------ 收藏里还差哪些音色


def missing_favorites() -> list[dict]:
    """收藏过、库里却没有名字的音色（按**收藏时间**排，照着剪映收藏夹的顺序点就行）。

    "有名字"只认 ``--favorites`` 里差 ≤3s 落库的那批；差更大的本来就只当候选、没进库。
    这些音色是"点了星但没留下试听"的——只点收藏没试听，或者重播走了缓存、没写新文件。
    补点一遍就有了：采集器按队列顺序逐条报名字，收到一条就归档一条。
    """
    path = LIB_ROOT / "_catalog" / "favorite_match.json"
    rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    have = {r["effect_id"] for r in rows if r.get("apply")}
    tones = json.loads(CATALOG.read_text(encoding="utf-8"))
    primary_category(tones)
    by_id = {t["effect_id"]: t for t in tones}
    favs = favorite_times()
    return [
        by_id[eid]
        for eid, _ in sorted(favs.items(), key=lambda kv: kv[1])
        if eid in by_id and eid not in have
    ]


def favorites_queue(oldest_first: bool) -> list[dict]:
    """收藏过的**全部**音色，按收藏时间排队（默认从新到旧 = 剪映收藏夹从上到下）。

    推倒重来时用它：你照收藏夹的顺序点，采集器照同一顺序命名，一条对一条。
    方向拿不准就加 ``--oldest-first`` 反一下。
    """
    tones = json.loads(CATALOG.read_text(encoding="utf-8"))
    primary_category(tones)
    by_id = {t["effect_id"]: t for t in tones}
    items = [(stamp, by_id[eid]) for eid, stamp in favorite_times().items() if eid in by_id]
    items.sort(key=lambda kv: kv[0], reverse=not oldest_first)
    return [tone for _, tone in items]


# ------------------------------------------------------- 同一嗓子对账（重点时用）


def _mono16k(path: Path):
    """读成 16kHz 单声道 float；采样率不对就线性插值——对齐要靠真实时间。"""
    import numpy as np

    if not is_riff(path):
        return pcm16k(path)
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
        sr = w.getframerate()
        ch = w.getnchannels()
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1)
    if sr == 16000 or not len(data):
        return data, sr
    n = int(len(data) * 16000 / sr)
    idx = np.linspace(0, len(data) - 1, n)
    return np.interp(idx, np.arange(len(data)), data).astype(np.float32), 16000


def voice_score(a: Path, b: Path) -> float:
    """两段音频的波形相关（取 ±0.3s 内的最大 |r|，都能对齐）。

    实测依据：89 条试听两两相比（同一段文字、不同音色）最大相关只有 0.022——
    所以"同一个音色 + 同一段文字"重新点一次，相关会贴近 1，中间几乎是空的。
    前提是**文字一样**：文字变了，波形就完全不同，这个分数没有意义。
    """
    import numpy as np

    sa, sra = _mono16k(a)
    sb, srb = _mono16k(b)
    if sa is None or sb is None or sra != srb or not len(sa) or not len(sb):
        return 0.0
    n = min(len(sa), len(sb))
    sa = sa[:n].astype(np.float64)
    sb = sb[:n].astype(np.float64)
    sa -= sa.mean()
    sb -= sb.mean()
    denom = float(np.linalg.norm(sa) * np.linalg.norm(sb))
    if denom <= 0:
        return 0.0
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    corr = np.fft.irfft(np.fft.rfft(sa, nfft) * np.conj(np.fft.rfft(sb, nfft)), nfft)
    corr = np.concatenate([corr[-(n - 1) :], corr[:n]])
    half = min(len(corr) // 2, int(0.3 * 16000))
    mid = len(corr) // 2
    return float(np.max(np.abs(corr[mid - half : mid + half + 1])) / denom)


def _clip_of(folder: Path) -> Path | None:
    for f in sorted(folder.iterdir()) if folder.is_dir() else []:
        if f.suffix.lower() in AUDIO_EXT and f.stat().st_size > 1024:
            return f
    return None


def match(args) -> int:
    """把序号音色和已命名音色按"同一个嗓子"对上。

    用法：库里还有序号音色时，用**同一段文字**在剪映里重点一遍（采集器会逐条命名），
    然后跑 ``--match`` 看哪条序号音频其实和新收的某个音色是同一个人。默认只看不改，
    ``--apply`` 才动手（删掉重复的序号目录，并把 harvest_map 里对应记录一起清掉）。
    """
    named = [d for d in collected_dirs() if not d.name.startswith(HARVEST_PREFIX)]
    loose = [d for d in collected_dirs() if d.name.startswith(HARVEST_PREFIX)]
    if not named or not loose:
        print(f"不用对：已命名 {len(named)} 条，序号 {len(loose)} 条。")
        return 0
    print(f"已命名 {len(named)} 条 · 序号 {len(loose)} 条，逐条比波形…")
    named_clips = [(d.name, _clip_of(d)) for d in sorted(named)]
    named_clips = [(n, p) for n, p in named_clips if p]
    hits = []
    for i, d in enumerate(sorted(loose), 1):
        clip = _clip_of(d)
        if clip is None:
            continue
        best = (0.0, "")
        for name, other in named_clips:
            score = voice_score(clip, other)
            if score > best[0]:
                best = (score, name)
        if best[0] >= args.threshold:
            hits.append((d.name, best[1], best[0]))
        if i % 10 == 0:
            print(f"  比到 {i}/{len(loose)}…（已中 {len(hits)}）")
    if not hits:
        print(f"没有相关 ≥{args.threshold:g} 的——序号那批不是这些名字。")
        return 0
    print(f"\n对上的 {len(hits)} 条（同一段文字、同一嗓子）：")
    for folder, name, score in sorted(hits, key=lambda x: -x[2]):
        print(f"  {folder:<14} ≈ {name}   相关 {score:.3f}")
    if not args.apply:
        print(
            "\n只看了没动。确认无误再加 --apply：会删掉这些序号目录（它们是同一个音色的重复录音）。"
        )
        return 0
    hm_path = LIB_ROOT / "_catalog" / "harvest_map.json"
    hm = json.loads(hm_path.read_text(encoding="utf-8")) if hm_path.exists() else {}
    for folder, _name, _score in hits:
        target = LIB_ROOT / folder
        shutil.rmtree(target)
        hm = {k: v for k, v in hm.items() if v.get("folder") != folder}
        print(f"  删了重复的 {folder}")
    hm_path.write_text(json.dumps(hm, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n库里现在 {len(collected_dirs())} 条音色。")
    return 0


# ------------------------------------------------------------------------- 对账


def verify(args) -> int:
    """库 ↔ ``harvest_map`` ↔ ``voice.json`` 三方对账，一条音频 = 一个文件夹 = 一条记录。

    改名/回收这两个动作在库里挪的是**路径**，而 ``voice_id_for()`` 就是路径的散列、
    sidecar 的 ``samples`` 键又是文件名——任何一步漏写，症状都是"音频凭空少一条"或
    "界面上看不到文字"。所以每轮折腾完跑这个，别靠眼睛数。
    """
    map_path = LIB_ROOT / "_catalog" / "harvest_map.json"
    hm = json.loads(map_path.read_text(encoding="utf-8")) if map_path.exists() else {}
    dirs = collected_dirs()
    problems: list[str] = []
    named = sum(1 for d in dirs if not d.name.startswith(HARVEST_PREFIX))
    print(f"对账 {len(dirs)} 个音色目录（带真名 {named} · 序号 {len(dirs) - named}）…")

    # 1) 目录里只能有一段音频，且叫默认.*
    for d in dirs:
        auds = sorted(f for f in d.iterdir() if f.suffix.lower() in AUDIO_EXT)
        if len(auds) != 1:
            problems.append(
                f"{d.name}：里有 {len(auds)} 段音频（{'、'.join(a.name for a in auds)}）"
            )
        elif auds[0].name != f"默认{auds[0].suffix}":
            problems.append(f"{d.name}：音频叫「{auds[0].name}」，不是默认{auds[0].suffix}")
        side = d / "voice.json"
        if not side.exists():
            problems.append(f"{d.name}：没有 voice.json")
            continue
        try:
            body = json.loads(side.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{d.name}：voice.json 读不了（{exc}）")
            continue
        keys = set(body.get("samples") or {})
        want = {a.name for a in auds}
        if keys != want:
            problems.append(
                f"{d.name}：sidecar 的 samples 键 {sorted(keys)} ≠ 实际音频 {sorted(want)}"
            )

    # 2) map ↔ 磁盘 一一对应，键 = 缓存音频的内容 md5，时刻不重复
    # 标了 discarded 的是用户主动"认不出来就丢掉"的：条目留着（--harvest 别再收回来），
    # 但目录已经不在了，不能算"map 指向不存在的目录"。
    live = {k: v for k, v in hm.items() if not v.get("discarded")}
    refs = [v["folder"] for v in live.values()]
    on_disk = {d.name for d in dirs}
    for miss in sorted({r for r in refs if r not in on_disk}):
        problems.append(f"map 指向不存在的目录：{miss}")
    for extra in sorted(on_disk - set(refs)):
        problems.append(f"目录没被 map 记录：{extra}")
    dup = [n for n, c in Counter(refs).items() if c > 1]
    for n in dup:
        problems.append(f"map 里 {n} 被两条记录指向（一段音频配了两个人）")
    times = [v.get("clicked") for v in live.values() if v.get("clicked")]
    for t, c in Counter(times).items():
        if c > 1:
            problems.append(f"点击时刻 {t} 重复 {c} 次")
    gone = 0
    for entry in live.values():
        clip = args.try_dir / Path(entry["clip"]).name.split("_")[0] / entry["clip"]
        if not clip.exists():
            gone += 1
    if gone:
        print(f"  剪映缓存里已清掉 {gone} 段（不影响库，只是不能再核对来路）")

    # 3) 记的时长/基频和文件现状一致（改了名但挪错人的那条就是这么露出来的）
    for i, entry in enumerate(live.values(), 1):
        d = LIB_ROOT / entry["folder"]
        auds = [f for f in d.iterdir() if f.suffix.lower() in AUDIO_EXT] if d.is_dir() else []
        if not auds:
            continue
        f = auds[0]
        if f.suffix.lower() != ".wav":
            continue
        sec = wav_seconds(f)
        if entry.get("seconds") and abs(sec - float(entry["seconds"])) > 0.25:
            problems.append(f"{entry['folder']}：记 {entry['seconds']}s，实际 {sec:.2f}s")
        if i % 20 == 0:
            print(f"  测到 {i}/{len(live)}…")
    if problems:
        print(f"\n发现 {len(problems)} 处对不上：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"\n没问题：{len(dirs)} 个目录 · map {len(hm)} 条 · 每条一段默认.wav + 一份文字。")
    return 0


# --------------------------------------------------------------------- 按键 / 进度


class Keys:
    """Windows 控制台单键读取；读不到就退化成"等超时"。"""

    def __init__(self) -> None:
        self._fn = None
        try:
            import msvcrt

            self._fn = msvcrt.kbhit, msvcrt.getwch
        except ImportError:
            pass

    def read(self) -> str:
        if not self._fn:
            return ""
        kbhit, getwch = self._fn
        if kbhit():
            return getwch().lower()
        return ""


def load_progress(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"collected": [], "missed": [], "durations": [], "session_text": ""}


def save_progress(path: Path, prog: dict) -> None:
    prog["updated"] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prog, ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------------- main


def main() -> int:
    global LIB_ROOT
    ap = argparse.ArgumentParser(description="剪映音色试听采集器")
    ap.add_argument("--category", default="", help="只采这一类（按面板顺序点）")
    ap.add_argument("--categories", default="", help="逗号分隔的多个分类")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多采几条")
    ap.add_argument("--timeout", type=float, default=45.0, help="等一条试听落盘的秒数")
    ap.add_argument("--skip-done", action="store_true", help="跳过库里已有音频的音色")
    ap.add_argument("--status", action="store_true", help="只看进度")
    ap.add_argument("--dry-run", action="store_true", help="只打印队列，不采集")
    ap.add_argument("--try-dir", type=Path, default=TRY_DIR, help="试听缓存目录（自测用）")
    ap.add_argument("--lib", type=Path, default=LIB_ROOT, help="音色库根目录（自测用）")
    ap.add_argument("--into", default="", help="本轮全归到这个文件夹（覆盖按分类挑的目录）")
    ap.add_argument(
        "--harvest",
        action="store_true",
        help="把已经点过的试听全部收进库（认不回名字，按序号 + 实测信息命名）",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="库 / harvest_map / voice.json 三方对账（只读，改完名字都跑一次）",
    )
    ap.add_argument(
        "--favorites",
        action="store_true",
        help="按面板缓存里的收藏时间戳，给已收的试听配音色名（只认收藏过的）",
    )
    ap.add_argument(
        "--max-delta",
        type=float,
        default=12.0,
        help="收藏与试听相差几秒内还值得列进明细（默认 12）",
    )
    ap.add_argument(
        "--trust",
        type=float,
        default=3.0,
        help="差这么秒内才真改名；再大的只当候选（默认 3，试听写盘一般落后收藏 1~2s）",
    )
    ap.add_argument(
        "--star-after",
        action="store_true",
        help="自由点（不按顺序）：收藏在试听之后，允许差到 --max-delta 秒，按最近一次试听认",
    )
    ap.add_argument("--no-asr", action="store_true", help="不听写试听文本")
    ap.add_argument(
        "--missing-favorites",
        action="store_true",
        help="本轮只点「收藏过、库里还没收到」的那几条（按收藏顺序排，照剪映收藏夹点）",
    )
    ap.add_argument(
        "--sticky",
        action="store_true",
        help="等不到音频就一直等这一条、不跳过（顺序点的时候用；跳过会让后面的名字全错位）",
    )
    ap.add_argument(
        "--all-favorites",
        action="store_true",
        help="本轮把收藏过的全部音色按收藏夹顺序排一遍（默认从新到旧）",
    )
    ap.add_argument("--oldest-first", action="store_true", help="收藏夹顺序反过来（从旧到新）")
    ap.add_argument(
        "--skip-first",
        type=int,
        default=0,
        help="跳过队列前 N 条（某条一直出不来时，从这条之后接着点）",
    )
    ap.add_argument(
        "--match",
        action="store_true",
        help="把序号音色和已命名音色按波形对一遍（同一段文字重点之后才有意义）",
    )
    ap.add_argument("--apply", action="store_true", help="--match 时真动手：删掉重复的序号目录")
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="--match 认作同一嗓子的相关阈值（默认 0.8；不同音色实测最高只有 0.02）",
    )
    args = ap.parse_args()
    LIB_ROOT = args.lib
    if args.match:
        return match(args)
    if args.verify:
        return verify(args)
    if args.favorites:
        return favorites(args)
    if args.harvest:
        return harvest(args)

    tones = json.loads(CATALOG.read_text(encoding="utf-8"))
    primary_category(tones)
    by_id = {t["effect_id"]: t for t in tones}

    if args.status:
        prog = load_progress(LIB_ROOT / "_catalog" / "collect_progress.json")
        dirs = collected_dirs()
        matched = paired_effects()
        named = {d for d in dirs if not d.name.startswith(HARVEST_PREFIX)}
        loose = len(dirs) - len(named)
        print(
            f"库里已有音频的音色目录：{len(dirs)} 个（有真名的 {len(named)} · "
            f"还是序号的 {loose}）· 清单共 {len(by_id)} 条"
        )
        print(f"本轮采集：{len(prog['collected'])} 条 · 漏采 {len(prog['missed'])} 条")
        if prog["durations"]:
            print(
                f"平均时长 {statistics.mean(prog['durations']):.1f}s · "
                f"合计 {sum(prog['durations']) / 60:.1f} 分钟"
            )
        # 分类进度按"清单里这条是不是已经有了"数：先认收藏配对过的 id（准），
        # 再退回到同名的目录（手动改过名的那些）。目录全平铺在「剪映/」下，
        # 所以不能再用"父目录名"当分类计数——那样永远是 0。
        todo = Counter(t["folder_category"] for t in tones)
        on_disk = {d.name for d in dirs}
        got = Counter(
            t["folder_category"]
            for t in tones
            if str(t["effect_id"]) in matched or safe_name(t["title"]) in on_disk
        )
        print(f"\n各分类 已有/清单（按 id 确认的 {len(matched)} 条）：")
        for cat in CATEGORY_PRIORITY:
            if todo.get(cat):
                print(f"  {cat:<8} {got.get(cat, 0):>3} / {todo[cat]}")
        for line in prog["missed"][-12:]:
            print("  漏采:", line)
        return 0

    if args.all_favorites:
        queue = favorites_queue(args.oldest_first)
        assign_names(queue)
        order = "从旧到新" if args.oldest_first else "从新到旧"
        print(
            f"收藏过的 {len(queue)} 条，按收藏时间**{order}**排（跟剪映收藏夹里从上往下对一遍）："
        )
    else:
        queue = missing_favorites() if args.missing_favorites else load_queue(args, tones)
        if args.missing_favorites:
            assign_names(queue)
            print(f"收藏过、库里还没收到的 {len(queue)} 条（按收藏顺序；剪映里打开收藏夹顺着点）：")
    if args.into:
        for t in queue:
            t["folder_category"] = args.into
    if args.skip_first:
        if 0 < args.skip_first < len(queue):
            print(f"跳过前 {args.skip_first} 条：{queue[args.skip_first]['title']} 开始。")
            queue = queue[args.skip_first :]
        elif args.skip_first >= len(queue):
            print("--skip-first 比队列还长，没得点了。")
            return 1
    if not queue:
        print("队列是空的——分类名写错了？看 _catalog/jianying_tones.csv 的分类列。")
        return 1

    print(f"本轮队列 {len(queue)} 条（清单共 {len(by_id)} 条）")
    for i, t in enumerate(queue[:12], 1):
        print(
            f"  {i:>2}. {t['folder_category']} / {t['title']}  "
            f"[{t['voice_type']}]  {'VIP' if t.get('is_vip') else '免费'}"
        )
    if len(queue) > 12:
        print(f"  … 还有 {len(queue) - 12} 条")
    if args.dry_run:
        return 0
    if not args.try_dir.exists():
        print(f"找不到试听缓存：{args.try_dir}（剪映装在哪？）")
        return 1

    prog_path = LIB_ROOT / "_catalog" / "collect_progress.json"
    prog = load_progress(prog_path)
    seen = snapshot(args.try_dir)
    keys = Keys()
    read_key = keys.read
    text = prog.get("session_text") or ""
    history: list[tuple[Path, dict]] = []
    pending: list[Path] = []  # 已落地、还没分给名字的试听（按落盘先后排队）
    started, collected = time.time(), 0
    print("\n开始在剪映里逐条点「试听」。终端会一直显示该点谁。\n")
    try:
        for idx, tone in enumerate(queue, 1):
            print(
                f"[{idx}/{len(queue)}] 点这条 ▶ {tone['folder_category']} / {tone['title']}  \a",
                flush=True,
            )
            deadline = time.time() + args.timeout
            picked = None
            while time.time() < deadline or args.sticky:
                cmd = read_key()
                if cmd in {"q", "\x03"}:
                    raise KeyboardInterrupt
                if cmd == "u" and history:
                    folder, back = history.pop()
                    for f in folder.glob("默认.*"):
                        f.unlink()
                    (folder / "voice.json").unlink(missing_ok=True)
                    try:
                        folder.rmdir()
                        folder.parent.rmdir()
                    except OSError:
                        pass
                    prog["collected"] = [c for c in prog["collected"] if c != back["effect_id"]]
                    print(f"  ↩ 已收回上一条：{back['title']}")
                    deadline = time.time() + args.timeout
                    continue
                if cmd in {"n", " "}:
                    break
                now = snapshot(args.try_dir)
                fresh = sorted(now - seen, key=lambda p: p.stat().st_mtime)
                if fresh:
                    seen |= set(fresh)
                    # 同时落地几条（手点快了）：**全部排队**，一条名字消费一条。
                    # 以前多出来的被挪去暂存、名字照样往前走——那就是错位的根源。
                    pending.extend(fresh)
                    if len(fresh) > 1:
                        print(f"  ↓ 一次落地 {len(fresh)} 条，排队逐条归档")
                if pending:
                    claim = pending.pop(0) if args.sticky else pending.pop()
                    picked = wait_stable(claim, 8.0)
                    break
                seen = now
                time.sleep(0.2)
                if args.sticky and time.time() >= deadline:
                    # 一条一条对着点的时候，**绝不能跳**：跳过一条，后面所有名字都会错位。
                    print(f"  … 还没等到音频，继续等这条：{tone['title']}", flush=True)
                    deadline = time.time() + args.timeout
            if picked is None:
                prog["missed"].append(f"{tone['folder_category']}/{tone['title']}")
                print(f"  ✗ 没等到音频，记为漏采：{tone['title']}")
                save_progress(prog_path, prog)
                continue
            if not is_riff(picked):
                pass  # 真 MP3 也照收（VoiceHub 认 .mp3）
            out = archive(picked, tone, text)
            seconds = wav_seconds(out) if out.suffix == ".wav" else 0.0
            if not text and not args.no_asr:
                text = asr_text(out)
                prog["session_text"] = text
                if text:
                    archive(picked, tone, text)
                    print(f"  试听文本：{text[:40]}")
            history.append((out.parent, tone))
            collected += 1
            prog["collected"].append(tone["effect_id"])
            if seconds:
                prog["durations"].append(round(seconds, 2))
            save_progress(prog_path, prog)
            rate = (time.time() - started) / max(collected, 1)
            print(
                f"  ✓ {out.relative_to(LIB_ROOT)}  "
                f"{seconds:.1f}s · 均 {rate:.1f}s/条 · 剩余约 "
                f"{(len(queue) - idx) * rate / 60:.0f} 分钟"
            )
    except KeyboardInterrupt:
        print("\n已中断——进度存好了，重跑同一条命令接着采。")
    print(
        f"\n本轮收了 {collected} 条，漏采 {len(prog['missed'])} 条。"
        f"库里现在 {len(collected_dirs())} 条音色。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
