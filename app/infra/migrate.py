"""目录迁移：改"音色保存目录"后，把旧目录里的东西搬到新目录。

口径：

- **先算后动**：:func:`plan_move` 只描述不动，用户确认后 :func:`apply_move` 才真搬；
- 同盘 rename（原子、快），跨盘退回"复制 + 校验 + 删源"，中途失败宁可留两份；
- **可续**：目标已有同内容者视为上次搬过的同一份，删源继续，不重搬；
- **绝不覆盖**：目标重名时改名（``名字-2.xxx``）保留两份。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "MoveError",
    "MovePlan",
    "MoveResult",
    "apply_move",
    "plan_move",
]


class MoveError(Exception):
    """迁移失败（磁盘满、权限、盘掉了）——api 层翻成 400，``str(exc)`` 是可读说明。"""


@dataclass(slots=True)
class MovePlan:
    """"要搬什么"的摘要——**只描述，不动**（界面拿它问用户）。"""

    source: str = ""
    target: str = ""
    #: 顶层条目数（音色文件夹、散放的文件）。
    entries: int = 0
    #: 可播音频数（只用来告诉用户"这里面有几段音频"）。
    audio: int = 0
    #: 字节数。
    bytes: int = 0
    #: 一句话的风险提示（空 = 没什么要说的）。
    note: str = ""

    @property
    def empty(self) -> bool:
        return self.entries == 0


@dataclass(slots=True)
class MoveResult:
    """"搬完了什么"的摘要。"""

    source: str = ""
    target: str = ""
    moved: int = 0
    #: 搬到目标时发现重名、改名保留的条目（``旧名 → 新名``）。
    renamed: list[tuple[str, str]] = field(default_factory=list)
    #: 没能搬走的（``路径``, 原因）——**失败不中断**，把清单交回去让人看。
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


#: 算得出音频的那些后缀（迁不迁移都只是"给用户报个数"，认宽一点无妨）。
_AUDIO_SUFFIXES = (
    ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".pcm", ".aac", ".wma",
)


def _same_place(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _nested(a: Path, b: Path) -> bool:
    """``b`` 在 ``a`` 里面（或反之）——直接搬会套娃，必须先拒。"""
    try:
        a_res, b_res = a.resolve(), b.resolve()
    except OSError:
        return False
    return a_res in b_res.parents or b_res in a_res.parents


def _file_digest(path: Path) -> str:
    """内容指纹（同名不同内容时用来判断"是不是同一份"）。读不动 = 空串。"""
    try:
        with path.open("rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return ""


def plan_move(source: Path, target: Path) -> MovePlan:
    """照一眼要搬什么（**不建目录、不碰文件**）。

    源不存在 / 是空目录 → 返回空计划（界面据此说"那边没有东西要搬"）；
    这条路也是"用户改了又改回去"时的正常路径，不该报错。

    嵌套（新目录在旧目录里面）**直接拒**：搬的时候会把目标目录连同正在搬的东西
    一起搬进去，越搬越深。
    """
    if _same_place(source, target):
        return MovePlan(source=str(source), target=str(target), note="新旧目录是同一个，不用搬。")
    if not source.is_dir():
        return MovePlan(source=str(source), target=str(target))
    if _nested(source, target):
        raise MoveError(
            f"目标目录位于源目录内部（{target} ⊂ {source}），迁移会包含目标目录自身。"
            "请先拆分这两个目录。"
        )
    entries = audios = total = 0
    try:
        for item in source.iterdir():
            entries += 1
            if item.is_file():
                total += item.stat().st_size
                if item.suffix.lower() in _AUDIO_SUFFIXES:
                    audios += 1
                continue
            for sub in item.rglob("*"):
                if sub.is_file():
                    total += sub.stat().st_size
                    if sub.suffix.lower() in _AUDIO_SUFFIXES:
                        audios += 1
    except OSError as exc:
        raise MoveError(f"无法读取 {source}：{exc}") from exc
    note = ""
    if target.exists() and any(target.iterdir()):
        note = "新目录里已经有东西了——重名的会改名保留，两份都不丢。"
    return MovePlan(
        source=str(source),
        target=str(target),
        entries=entries,
        audio=audios,
        bytes=total,
        note=note,
    )


def _tree_digest(path: Path) -> str:
    """整棵目录的内容指纹（相对路径 + 文件字节，按名排序），用于中断续搬时
    识别"目标那份就是上次搬的半截"，避免攒出 ``胡桃-2``、``胡桃-3``。"""
    digest = hashlib.sha256()
    try:
        for item in sorted(path.rglob("*"), key=lambda p: str(p.relative_to(path))):
            digest.update(str(item.relative_to(path)).encode("utf-8", "replace"))
            if item.is_file():
                digest.update(_file_digest(item).encode())
    except OSError:
        return ""
    return digest.hexdigest()


def _same_content(source: Path, dest: Path) -> bool:
    """源的这一份与目标里那一份是不是**同一份内容**（文件比字节，目录比整棵树）。"""
    if source.is_file() != dest.is_file():
        return False
    if source.is_file():
        return _file_digest(source) == _file_digest(dest)
    return _tree_digest(source) == _tree_digest(dest)


def _move_one(item: Path, dest_dir: Path, renamed: list[tuple[str, str]]) -> None:
    """搬一个顶层条目；目标重名时**改名保留两份**（绝不覆盖）。"""
    dest = dest_dir / item.name
    if dest.exists():
        if _same_content(item, dest):
            # 内容一致：视为上次搬到一半留下的同一份，删源即可（可续的关键）。
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            return
        stem, suffix = item.stem, item.suffix
        for n in range(2, 1000):
            candidate = dest_dir / f"{stem}-{n}{suffix}"
            if not candidate.exists():
                dest = candidate
                renamed.append((item.name, candidate.name))
                break
        else:
            raise MoveError(f"{item} 重命名 1000 次仍冲突，请手工处理。")
    try:
        item.rename(dest)  # 同盘：原子
        return
    except OSError:
        pass  # 跨盘（或权限）失败：退回复制 + 校验 + 删源
    if item.is_file():
        shutil.copy2(item, dest)
        if item.stat().st_size != dest.stat().st_size:
            raise MoveError(f"{item} 复制后大小不一致，源文件已保留。")
        item.unlink()
        return
    shutil.copytree(item, dest)
    shutil.rmtree(item)


def apply_move(plan: MovePlan) -> MoveResult:
    """照 :func:`plan_move` 的计划真搬（失败不中断，逐条记下来）。

    搬完不删源目录本身：里面可能有没能搬走的东西（:attr:`MoveResult.failed`），
    留空目录让用户自行处理。
    """
    source, target = Path(plan.source), Path(plan.target)
    if _same_place(source, target):
        return MoveResult(source=str(source), target=str(target))
    if _nested(source, target):
        raise MoveError(f"目标目录位于源目录内部（{target} ⊂ {source}），请先拆分。")
    if not source.is_dir():
        return MoveResult(source=str(source), target=str(target))
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MoveError(f"无法创建 {target}：{exc}") from exc

    result = MoveResult(source=str(source), target=str(target))
    try:
        items = sorted(source.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise MoveError(f"无法读取 {source}：{exc}") from exc
    for item in items:
        try:
            _move_one(item, target, result.renamed)
        except (OSError, MoveError, shutil.Error) as exc:
            result.failed.append((str(item), str(exc)))
            continue
        result.moved += 1
    return result
