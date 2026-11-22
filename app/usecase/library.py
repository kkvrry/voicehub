"""音色库用例：扫描编排、元数据编辑、平台音色登记。

一致性规则（docs/design.md §3.4）：

- **读**：扫描结果 + sidecar 覆盖值合并进 SQLite 索引；
- **写**：编辑本地音色 → 先写 sidecar（原子），再更新索引——写失败整个接口
  报错，不出现"界面改了库里没改"；
- sidecar 的 samples 按键增量合并：文件删了的键在保存时清掉。
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.domain.models import (
    GENDER_CHOICES,
    GENDERS,
    SOURCE_LOCAL,
    Sample,
    Voice,
    platform_voice_id,
    voice_id_for,
)
from app.domain.scanner import main_sample_of, relative_category
from app.engines.base import KIND_LABELS, VoiceMeta
from app.engines.registry import list_engines
from app.infra import archive, sidecar
from app.infra.audioprobe import probe_duration_cached
from app.infra.config import load_config, save_config
from app.infra.db import HubDB, path_key
from app.infra.duration_cache import DurationCache
from app.infra.loghub import CHANNEL
from app.infra.sidecar import (
    Sidecar,
    read_sidecar,
    sample_duration,
    sample_params,
    write_sidecar,
)
from app.infra.trash import TrashError, move_to_trash
from app.infra.walker import FoundVoice, walk_root
from app.usecase.engines import EngineConfigError, engine_entries, require_engine

__all__ = [
    "LibraryError",
    "LibraryService",
]


class LibraryError(Exception):
    """用例层错误——``code`` 与 ``status`` 随异常一起给出，HTTP 层不再按文案判定。

    :param message: 中文说明（严格、简洁、专业）。
    :param code: 错误码（见 ``app.api.errors.CODES``）。
    :param status: 对应的 HTTP 状态码；缺省 400（请求语义有问题）。
    """

    def __init__(self, message: str, *, code: str = "invalid_value", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _category_nodes(rels: list[str]) -> list[dict[str, Any]]:
    """一组相对分类路径 → 嵌套树（左栏一个根下的目录结构）。

    ``count`` 是子树里的音色数（点节点筛出的就是这个范围）；``path`` 是回传的
    筛选值（整条相对路径）。节点只有目录——category 是磁盘上真实存在的路径，
    直接落在根下的音色没有分类层，挂在根那一行上。
    """
    tree: dict[str, list] = {}  # 名字 -> [子树音色数, 下一层]

    for rel in rels:
        node = tree
        for segment in rel.split("/"):
            entry = node.setdefault(segment, [0, {}])
            entry[0] += 1
            node = entry[1]

    def emit(level: dict[str, list], parent: str) -> list[dict[str, Any]]:
        nodes = []
        for name in sorted(level):
            count, subs = level[name]
            path = name if not parent else f"{parent}/{name}"
            nodes.append(
                {
                    "name": name,
                    "path": path,
                    "count": count,
                    "children": emit(subs, path),
                }
            )
        return nodes

    return emit(tree, "")


def _audio_files(folder: Path) -> list[Path]:
    """一个音色文件夹里的音频，新的在前（文件名是 ``时间戳-序号``）。读不动当空。"""
    try:
        return sorted(
            (
                path
                for path in folder.iterdir()
                if path.is_file() and path.suffix.lower() in archive.AUDIO_SUFFIXES
            ),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return []


#: 产物索引的最长存活时间（秒）。根目录 mtime 变了会立刻重建；往**已有**目录里
#: 添文件不动根目录，只能靠它兜底。写完产物本进程会主动失效，正常用不到。
_GENERATED_TTL = 10.0


@dataclass(frozen=True, slots=True)
class _GeneratedAudio:
    """一台引擎保存目录里的产物索引：按归属 id 认（正本），文件夹名只给老数据兜底。

    ``sides`` 顺手存着每个文件夹的 sidecar：挂样例时要读文字 / 情绪 / 参数，
    索引阶段已经读过一遍，再读一次就是白跑一趟磁盘。
    """

    by_id: dict[str, list[Path]]
    by_name: dict[str, list[Path]]
    sides: dict[Path, Sidecar]

    def for_voice(self, voice_id: str, *names: str) -> list[Path]:
        # 去重的是文件夹不是文件：一个文件夹里可能有多段生成结果。
        folders: dict[Path, Path] = {path.parent: path for path in self.by_id.get(voice_id) or []}
        for name in names:
            for path in self.by_name.get(archive.safe_segment(name)) or []:
                folders.setdefault(path.parent, path)
        files = [path for folder in folders for path in _audio_files(folder)]
        # 新的在前（文件名带时间戳）：主样例取第一条 = 最近生成的。
        return sorted(files, key=lambda path: path.name, reverse=True)


def _empty_generated() -> _GeneratedAudio:
    """没有产物：空索引。每次给新对象，别让调用方共享同一份可变 dict。"""
    return _GeneratedAudio({}, {}, {})


def _merge_generated(indexes: Iterable[_GeneratedAudio]) -> _GeneratedAudio:
    """几台引擎的产物索引并成一份——本地音色的生成样例可能落进任意一台。"""
    by_id: dict[str, list[Path]] = {}
    by_name: dict[str, list[Path]] = {}
    sides: dict[Path, Sidecar] = {}
    for index in indexes:
        for owner, paths in index.by_id.items():
            by_id.setdefault(owner, []).extend(paths)
        for name, paths in index.by_name.items():
            by_name.setdefault(name, []).extend(paths)
        sides.update(index.sides)
    return _GeneratedAudio(by_id, by_name, sides)


def _design_prompt_sync(voice: Voice, description: Any) -> dict[str, Any]:
    """设计音色的「描述」就是它的设计描述：改这一格等于改每次发出去的描述。

    凭据只在 ``payload["design_prompt"]`` 一处（``domain/credential.py`` 照它判定），
    所以这里必须一起写；只改显示用的 ``description`` 会让界面说着 A、实际发着 B。
    不是设计音色（payload 里没有这一项）就原样返回。
    """
    payload = dict(voice.payload or {})
    if description is None or "design_prompt" not in payload:
        return payload
    text = str(description or "").strip()
    if text != str(payload.get("design_prompt") or "").strip():
        payload["design_prompt"] = text
    return payload


def _linked_origin(side: Sidecar, identity: str) -> str:
    """sidecar 里记的归属 id；指着自己的一律当没有（本地音色不是谁的样例目录）。"""
    origin_id = (side.origin.get("voice_id") or "").strip()
    return "" if origin_id == voice_id_for(identity) else origin_id


@dataclass(slots=True)
class _ScanJob:
    """一次扫描的进度（**内存对象**，进程重启即丢）。

    扫描是秒级操作，进度是"给人看着别慌"的临时状态，没有留存价值——不进 SQLite。
    字段名与生产方任务（``producers/.../store.jobs``）对齐：``state`` / ``total`` /
    ``done`` / ``message``，界面可以用同一套渲染。
    """

    id: str
    state: str = "running"  # running | done | failed
    phase: str = "准备"
    total: int = 0
    done: int = 0
    voices: int = 0
    samples: int = 0
    message: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def progress(
        self,
        *,
        phase: str = "",
        total: int | None = None,
        done: int | None = None,
        voices: int | None = None,
        samples: int | None = None,
    ) -> None:
        """改几个计数（扫描线程写、请求线程读；单字段赋值，不需要锁）。"""
        if phase:
            self.phase = phase
        if total is not None:
            self.total = total
        if done is not None:
            self.done = done
        if voices is not None:
            self.voices = voices
        if samples is not None:
            self.samples = samples

    def snapshot(self) -> dict[str, Any]:
        end = self.finished_at or time.time()
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": "scan",
            "state": self.state,
            "phase": self.phase,
            "total": self.total,
            "done": self.done,
            "voices": self.voices,
            "samples": self.samples,
            "elapsed": round(max(0.0, end - self.started_at), 3),
        }
        if self.message:
            payload["message"] = self.message
        if self.result:
            payload["result"] = self.result
        if self.error:
            payload["error"] = self.error
        return payload


class LibraryService:
    """音色库的全部用例。**一个服务实例 = 一个数据目录**（测试各起各的）。"""

    def __init__(self, db: HubDB) -> None:
        self._db = db
        #: 扫描互斥：两次并行会把"对方刚写进去的行"当成"已经不存在的"删掉。
        self._scan_lock = threading.Lock()
        #: 最近一次（或正在跑的）后台扫描。``_job_lock`` 只护这一个指针与它的建立。
        self._job_lock = threading.Lock()
        self._job: _ScanJob | None = None
        #: 引擎产物索引缓存：``引擎 key → (根目录 mtime, 建表时刻, 索引)``。
        self._generated_cache: dict[str, tuple[int, int, float, _GeneratedAudio]] = {}
        #: 扫描成功后的维护任务（插件自愈等）；异常只记日志，不影响扫描结果。
        self._scan_hooks: list[Any] = []

    # 幂等键：HTTP 层不直接持有 HubDB（分层纪律），因此由服务转发两个动作。
    # 语义与有效期在 HTTP 层决定，这里只做存取。

    def idempotent_begin(
        self, key: str, fingerprint: str, *, ttl: float
    ) -> tuple[str, dict[str, Any] | None]:
        """抢占幂等键，返回 ``("new" | "replay" | "pending", 已存结果)``。

        键存在但指纹不同 → 抛 409 语义的错误，由调用方翻译。
        """
        state, response = self._db.idempotent_begin(key, fingerprint, ttl=ttl)
        if state == "conflict":
            raise LibraryError(
                "同一个幂等键提交了不同的请求内容。",
                code="idempotency_key_conflict",
                status=409,
            ) from None
        return state, response

    def idempotent_abandon(self, key: str, fingerprint: str) -> None:
        """失败 / 取消时释放占位，别把键锁到过期。"""
        self._db.idempotent_abandon(key, fingerprint)

    def idempotent_store(
        self, key: str, fingerprint: str, response: dict[str, Any], *, ttl: float
    ) -> None:
        self._db.idempotent_put(key, fingerprint, response, ttl=ttl)

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #

    def scan(self, *, progress: Any = None) -> dict[str, Any]:
        """重扫全部根目录（同一时刻只允许一次，并发触发直接 409）。

        编排在 :meth:`_scan`；这层只负责互斥——扫描要读 sidecar、写库、删掉已消失
        的行，两次并行会互相把对方的结果当成"过期数据"。

        :param progress: 可选回调，签名为 :meth:`_ScanJob.progress` 的关键字参数
            （``phase`` / ``total`` / ``done`` / ``voices`` / ``samples``）。
        """
        if not self._scan_lock.acquire(blocking=False):
            raise LibraryError(
                "扫描已在进行中，请等这次结束。", code="scan_in_progress", status=409
            )
        try:
            return self._scan(progress=progress)
        finally:
            self._scan_lock.release()

    # ------------------------------------------------------------------ #
    # 后台扫描（界面按进度轮询，不再把请求线程占几十秒）
    # ------------------------------------------------------------------ #

    def start_scan(self) -> dict[str, Any]:
        """起一次后台扫描，立刻返回进度快照。

        已经在扫就把**那一次**返回：界面上连点两下不该变成两次全量扫描。
        同时有同步扫描在跑（``PUT /folders`` 那条路）则直接 409——那种情况没有
        可复用的任务，硬起一个只会立刻撞锁。
        """
        with self._job_lock:
            current = self._job
            if current is not None and current.state == "running":
                return current.snapshot()
            if self._scan_lock.locked():
                raise LibraryError(
                    "扫描已在进行中，请等这次结束。", code="scan_in_progress", status=409
                )
            job = _ScanJob(id=uuid.uuid4().hex[:12], started_at=time.time())
            self._job = job
        threading.Thread(
            target=self._run_job, args=(job,), name="voicehub-scan", daemon=True
        ).start()
        return job.snapshot()

    def add_scan_hook(self, hook: Any) -> None:
        """登记一条扫描成功后的维护任务（插件按需重绑自己的音色引用）。"""
        self._scan_hooks.append(hook)

    def scan_status(self) -> dict[str, Any]:
        """最近一次后台扫描的进度；从没扫过 → ``state: idle``。"""
        with self._job_lock:
            job = self._job
        if job is None:
            return {
                "id": "",
                "kind": "scan",
                "state": "idle",
                "phase": "",
                "total": 0,
                "done": 0,
                "voices": 0,
                "samples": 0,
                "elapsed": 0.0,
            }
        return job.snapshot()

    def _run_job(self, job: _ScanJob) -> None:
        """后台线程的主体：扫描的成败都落在这个任务上，不往外抛。"""
        try:
            result = self.scan(progress=job.progress)
        except LibraryError as exc:
            job.state = "failed"
            job.phase = "失败"
            job.error = str(exc)
            CHANNEL.warning(f"扫描失败：{exc}", source="library", code=exc.code)
        except Exception as exc:  # noqa: BLE001 - 后台线程：异常只能记在任务上
            job.state = "failed"
            job.phase = "失败"
            job.error = str(exc) or exc.__class__.__name__
            CHANNEL.error(f"扫描失败：{job.error}", source="library")
        else:
            job.result = result
            job.state = "done"
            # 阶段停在"写索引"会让人以为还在写：收尾就换个说法。
            job.phase = "完成"
            job.voices = result["voices"]
            job.samples = result["samples"]
        finally:
            job.finished_at = time.time()

    def _run_scan_hooks(self) -> None:
        """跑登记过的扫描后置任务：一条失败只记日志，不让整次扫描失败。"""
        for hook in self._scan_hooks:
            try:
                hook()
            except Exception as exc:  # noqa: BLE001 - 维护任务是增强，不是扫描本身
                CHANNEL.warning(f"扫描后置任务失败：{exc}", source="library")

    def _scan(self, *, progress: Any = None) -> dict[str, Any]:
        """重扫全部根目录：walker 走目录 → 合并 sidecar → 增量重写本地索引。

        **扫描根只有用户配的那几个**。引擎的音色保存目录不是扫描根：里面生成的音频
        按 sidecar 的 ``origin`` 挂回它自己的音色（平台或本地，见 :meth:`_with_generated`），
        不再靠"扫成第二条"来让人看见。

        增量口径（``FoundVoice.stamp``）：walker 给每个音色算一枚目录指纹（这一层每个
        文件的名字 / 大小 / mtime_ns 的摘要）。指纹与库里上次记的一致，就整条复用库里
        那一行——不读 sidecar、不读清单与 txt、不重建 ``Voice``、也不写库；只有真的
        动过的文件夹才走完整流程。时长仍由 :class:`~app.infra.duration_cache.DurationCache`
        兜底（指纹变了但文件没变，照样不必重起 ffprobe）；上次没量出时长的（0.0）不
        复用，下次扫描再试一次。
        """
        config = load_config()
        cache = DurationCache.loaded(self._db.duration_cache())
        generated = self._generated_roots()
        # 上次扫描留下的行与指纹：同一次连接里各取一份，逐条比对。
        known = self._db.local_voices()
        stamps = self._db.local_stamps()
        voices: list[Voice] = []
        unchanged: set[str] = set()
        fresh: dict[str, str] = {}
        # 这次从 sidecar 读到"设过的"收藏/隐藏（id → (hidden, favorite)），写库后回填。
        prefs: dict[str, tuple[bool | None, bool | None]] = {}
        missing: list[str] = []
        total_roots = len(config.folders)
        if progress is not None:
            progress(total=total_roots, phase="扫描根目录")
        for index, root in enumerate(config.folders, start=1):
            if progress is not None:
                progress(phase=f"扫描 {index}/{total_roots}：{root}", done=index - 1)
            found = walk_root(
                root,
                ffprobe_path=config.ffprobe_path,
                cache=cache,
                # 单个根里有几千个音色时，光"每根报一次"等于没进度。
                on_voice=(
                    (lambda count: progress(voices=len(voices) + count))
                    if progress is not None
                    else None
                ),
            )
            # 引擎保存目录里的东西是**某条音色的样例**，不是本地音色：它可能被放在
            # 扫描根里面（用户把「生成结果目录」指进音色库时就是这样），再扫一遍会
            # 让同一条音色多出一个"本地"副本。归属由 `_with_generated` 按 sidecar 认。
            found = [item for item in found if not self._is_generated(item.folder, generated)]
            if not found and not Path(root).is_dir():
                missing.append(root)
            for item in found:
                identity = os.path.join(item.folder, item.name) if item.flat else item.folder
                voice_id = voice_id_for(identity)
                fresh[voice_id] = item.stamp
                previous = known.get(voice_id)
                if (
                    previous is not None
                    and stamps.get(voice_id) == item.stamp
                    # 上次没量出时长的（ffprobe 没装 / 文件被占用）不进复用：
                    # 那条路要留出"下次再试一次"的机会。
                    and all(sample.duration > 0 for sample in previous.samples)
                ):
                    voices.append(previous)
                    unchanged.add(voice_id)
                    continue
                voices.append(self._merge_sidecar(item))
                # 收藏/隐藏的正本在音色目录的 sidecar 里（跟着文件夹走）：只有它在
                # sidecar 里明确设过才回填数据库——没设过的那些仍以库里现有值为准。
                side = item.sidecar
                if side.favorite is not None or side.hidden is not None:
                    prefs[voice_id] = (side.hidden, side.favorite)
            if progress is not None:
                progress(done=index, voices=len(voices))
        if progress is not None:
            progress(phase="写索引", done=total_roots)
        # 缓存先写：它是"文件的事实"，与本地行增量重写互不相干。
        self._db.remember_durations(cache.entries())
        self._db.sync_local(voices, unchanged=unchanged, stamps=fresh)
        for voice_id, (hidden, favorite) in prefs.items():
            self._db.set_prefs(voice_id, hidden=hidden, favorite=favorite)
        self._run_scan_hooks()
        result = {
            "roots": len(config.folders),
            "voices": len(voices),
            "samples": sum(len(v.samples) for v in voices),
            "missing_roots": missing,
            # 没动过的音色数：界面/日志据此说清"这次真的做了什么"。
            "unchanged": len(unchanged),
        }
        # 汇总一条：扫描结论只活在这一条日志里；配错的根目录单列 warning。
        CHANNEL.info(
            f"已重扫：{result['roots']} 个目录、{result['voices']} 条音色、"
            f"{result['samples']} 个样例。",
            source="library",
            **result,
        )
        for root in missing:
            CHANNEL.warning(f"扫描根不存在或读不到：{root}", source="library", root=root)
        return result

    def engine_roots(self) -> list[tuple[str, str, str, Path]]:
        """(引擎 key, label, 形态说法, 音色保存目录)，装了的引擎各一条；目录不存在也给。"""
        return [
            (
                key,
                engine.label,
                KIND_LABELS.get(engine.capabilities().kind, ("", ""))[0],
                archive.engine_root(key, engine.label, create=False),
            )
            for key, engine in list_engines().items()
        ]

    def _generated_roots(self) -> list[Path]:
        """装了的引擎各自的音色保存目录（已折形）。扫描时要把它们从扫描根里摘掉：
        这些目录里是平台音色的样例，不是本地音色（见 :meth:`_is_generated`）。"""
        roots: list[Path] = []
        for _key, _label, _kind, path in self.engine_roots():
            if path.is_dir():
                roots.append(path)
        return roots

    @staticmethod
    def _is_generated(folder: str, roots: list[Path]) -> bool:
        """这个扫描结果是不是某台引擎的保存目录（含其子目录）。

        用户把「生成结果目录」指进音色库时，平台音色的样例就躺在扫描根底下；
        它们仍归平台音色（`_with_generated`），不能再当成本地音色扫一遍。

        比对用 :func:`path_key` 折形后的前缀，**不 resolve**：这个函数每条扫描结果
        都要过一次，``Path.resolve()`` 在 Windows 上要问一次文件系统，几百条就是
        上百毫秒。两边路径都源自配置与库，写法一致，折形足够。
        """
        if not roots:
            return False
        target = path_key(folder)
        for root in roots:
            prefix = path_key(str(root)).rstrip("\\")
            if target == prefix or target.startswith(prefix + "\\"):
                return True
        return False

    def _merge_sidecar(self, found: FoundVoice) -> Voice:
        """扫描结果 + sidecar → 一条 Voice（**sidecar 覆盖推导值**）。

        用的就是 :attr:`FoundVoice.sidecar`——遍历那一层时读的那一份，不在这里再读一遍；
        收藏/隐藏两栏由 :meth:`_scan` 从同一份里取，见那里。
        """
        side = found.sidecar
        identity = os.path.join(found.folder, found.name) if found.flat else found.folder
        files = [s["file"] for s in found.samples]        # sidecar 里指向已删文件的样例键，写库时清掉（不带到下一次）。
        kept = {k: v for k, v in side.samples.items() if k in files}
        samples = tuple(
            Sample(
                file=s["file"],
                text=kept.get(s["file"], {}).get("text", "") or s["text"],
                emotion=kept.get(s["file"], {}).get("emotion", "") or s["emotion"],
                # 时长以实测为准（音频换了它就变，备份没有校验手段）；只有量不出来
                # （没 ffprobe / 编码不认识）时才退回 sidecar 里那份备份。
                duration=s["duration"] or sample_duration(kept.get(s["file"])),
                # 样例自己的标签扫描推不出来，只能手填，sidecar 里没有就是没有。
                tags=tuple(kept.get(s["file"], {}).get("tags") or ()),
                # 生成出来的样例才有 params（见 infra/archive.record_sample）。
                params=sample_params(kept.get(s["file"])),
            )
            for s in found.samples
        )
        return Voice(
            id=voice_id_for(identity),
            source=SOURCE_LOCAL,
            name=found.name,
            display=side.display,
            category=found.category,
            gender=side.gender if side.gender in GENDERS else "",
            language=side.language,
            # 年龄感不套词表：词表外的值原样收下、原样显示（见 domain/models.AGE_CHOICES）。
            age=side.age,
            tags=tuple(side.tags),
            description=side.description,
            folder=found.folder,
            cover=found.cover,
            main_sample=main_sample_of(files, side.main_sample),
            samples=samples,
            # 平台归属：这个文件夹是某条平台音色的样例目录（生成时就写在 sidecar 里）。
            # 有值时它不单独列，按那条平台音色出现（见 _with_generated）。
            # 指着自己不算归属：本地音色的目录就是它自己的家，写坏的历史数据在这里洗净。
            origin_id=_linked_origin(side, identity),
        )

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def tree(self) -> dict[str, Any]:
        """左栏数据：用户配的扫描根（引擎保存目录不再是扫描根）。

        平台段的主语是插件而不是库里的行：装好引擎但没拉到音色时也必须出现。
        本地根下的分类是嵌套树，节点带 ``count``（子树音色数）与 ``path``（回传筛选值）；
        直接落在根下的音色由 ``total`` 算进来。
        """
        config = load_config()
        stats = self._db.tree()
        # 取 (目录, 分类) 就够，不构造整条 Voice（解 JSON、算样例数是这里最贵的一段）。
        # 折形只做一次：下面是"每根 × 每条"，再折就是 O(根数 × 音色数) 次字符串操作。
        local = [(path_key(folder), category or "") for folder, category in self._db.local_folders()]

        def root_row(path: str, engine: dict[str, Any] | None = None) -> dict[str, Any]:
            # 分类归属：folder 折形后按「根 + 分隔符」匹配（共用 path_key；
            # 不带分隔符会让 d:\lib 把 d:\library 的音色也数进来）。
            prefix = path_key(path).rstrip("\\")
            rels: list[str] = []
            total = 0
            for folded, category in local:
                if not (folded == prefix or folded.startswith(prefix + "\\")):
                    continue
                total += 1
                # 直接在根下的音色不造节点（点根那一行就看得到）。
                if category:
                    rels.append(category)
            return {
                "path": path,
                "exists": Path(path).is_dir(),
                # 引擎目录里音色就在根下，没有分类层。
                "children": [] if engine else _category_nodes(rels),
                "total": total,
                "engine": engine,
            }

        roots = [root_row(root) for root in config.folders]
        designed = int(stats.get("design", 0))
        return {
            "roots": roots,
            "engines": engine_entries(stats["platforms"]),
            # 设计音色：跨引擎的一组，左栏单列一行（有目录的形态，图标另设）。
            "design": designed,
            # 库里一共多少条：本地 + 各引擎 + 设计音色。设计音色不在任何一台引擎的
            # 计数里（它们只归自己那一组），漏加这一段，「全部音色」就会比列表少几条。
            "total": len(local) + sum(stats["platforms"].values()) + designed,
        }

    def voice_origins(self, voices: list[Voice]) -> dict[str, dict[str, Any]]:
        """``{音色 id: 引擎信息}``——只给落在某台引擎保存目录里的本地音色。

        界面据此挂徽标，并把 ``voice_id``（库里对应的那条引擎音色）用于
        「生成样例」合成；认不出留空，照本地音色画。根归属与 :meth:`tree`
        共用 :func:`path_key` 折形规则。
        """
        if not voices:
            return {}
        roots = [
            (key, label, kind_label, path_key(str(path)))
            for key, label, kind_label, path in self.engine_roots()
        ]
        if not roots:
            return {}
        # 存在性判断一次查全表（原来每条音色两次 get_voice，是列表请求里最钝的 N+1）；
        # sidecar 也复用产物索引里已读好的那份，不逐条再开文件。
        known = self._db.voice_ids()
        out: dict[str, dict[str, Any]] = {}
        tables: dict[str, dict[str, str]] = {}
        sides: dict[str, _GeneratedAudio] = {}
        for voice in voices:
            if not voice.folder or not voice.is_local:
                continue
            norm = path_key(voice.folder)
            for key, label, kind_label, prefix in roots:
                if not (norm == prefix or norm.startswith(prefix + "\\")):
                    continue
                if key not in tables:
                    tables[key] = self._engine_voice_names(key)
                    sides[key] = self._generated_index(key)
                side = sides[key].sides.get(Path(voice.folder))
                if side is None:
                    side = read_sidecar(Path(voice.folder))
                out[voice.id] = {
                    "key": key,
                    "label": label,
                    "kind_label": kind_label,
                    "voice_id": self._linked_voice_id(key, voice, tables[key], known=known, side=side),
                }
                break
        return out

    def _engine_voice_names(self, engine_key: str) -> dict[str, str]:
        """这台引擎在库里的条目 → "名字 → 音色 id"（显示名、音色名、原显示名都能查），
        仅供老数据无 ``origin`` 时兜底，按需建表。"""
        table: dict[str, str] = {}
        for row in self._db.list_voices(source=engine_key):
            for name in (row.shown_name, row.name, row.display):
                if name:
                    table.setdefault(name, row.id)
        return table

    def _linked_voice_id(
        self,
        engine_key: str,
        voice: Voice,
        table: dict[str, str],
        *,
        known: set[str],
        side: Sidecar,
    ) -> str:
        """这个文件夹里的音频属于库里哪条引擎音色，认不出返回空串。

        三步，从可靠到兜底：sidecar 记的归属（须确认音色还在库里）→
        文件夹名当平台音色 id → 按名字查该引擎的条目表。
        """
        recorded = side.origin.get("voice_id", "")
        if recorded and recorded in known:
            return recorded
        direct = platform_voice_id(engine_key, voice.name)
        if direct in known:
            return direct
        return table.get(voice.name, "")

    def list_voices(self, *, limit: int = 0, offset: int = 0, **filters: Any) -> list[Voice]:
        found = self._db.list_voices(
            **{k: v for k, v in filters.items() if v}, limit=limit, offset=offset
        )
        return self._with_generated(found)

    # ------------------------------------------------------------------ #
    # 生成出来的东西 → 平台音色自己的样例
    # ------------------------------------------------------------------ #

    def _with_generated(self, voices: list[Voice]) -> list[Voice]:
        """把生成的音频认成**它自己那条音色**的样例。

        三条路，同一个归属正本（sidecar ``origin``）：

        - 平台音色、样例就在用户的音色库里（扫描根下的样例目录，``origin.voice_id``
          指着它）：直接拿那份目录当它的家，见 :meth:`_attach_linked`；
        - 平台音色、样例在引擎保存目录里：按它自己的引擎目录找；id 优先，名字兜底（老数据）；
        - 本地音色：跨引擎按 ``origin.voice_id`` 找，样例**并进**原有列表，目录不变。

        挂上的音频能播、能当参考音频；音色本身不变（平台还是平台、本地还是本地）。
        引擎目录每台每次只走一遍；挂不上的原样返回。
        """
        if not voices:
            return voices
        out: list[Voice] = []
        by_engine: dict[str, _GeneratedAudio] = {}
        merged: _GeneratedAudio | None = None  # 本地音色：跨引擎的合并视图
        linked: dict[str, Voice] | None = None  # 平台音色：扫描根里的样例目录
        for voice in voices:
            if voice.is_local:
                if merged is None:
                    merged = _merge_generated(self._generated_index(key) for key in list_engines())
                files = merged.by_id.get(voice.id, [])
                if not files:
                    out.append(voice)
                else:
                    out.append(self._attach(voice, files, merge=True, sides=merged.sides))
                continue
            if not voice.source:
                out.append(voice)
                continue
            if linked is None:
                linked = self._db.linked_locals()
            owner = linked.get(voice.id)
            if owner is not None:
                attached = self._attach_linked(voice, owner)
                if attached is not None:
                    out.append(attached)
                    continue
            index = by_engine.get(voice.source)
            if index is None:
                index = by_engine[voice.source] = self._generated_index(voice.source)
            files = index.for_voice(voice.id, voice.shown_name, voice.name)
            out.append(self._attach(voice, files, sides=index.sides) if files else voice)
        return out

    @staticmethod
    def _attach_linked(voice: Voice, owner: Voice) -> Voice | None:
        """平台音色 → 它在扫描根里的那个样例文件夹（``owner``）。

        文件夹在、里面也有音频，就把它们当这条平台音色的样例，并把那个文件夹
        原样交回去：「打开目录」「改文字」「换主样例」「删样例」都还落回同一处——
        样例依旧按本地音色管，只是界面上由平台音色代表。

        文件夹没了 / 里面没有音频时返回 ``None``，让调用方退回引擎保存目录那条路。
        """
        folder = Path(owner.folder)
        files = _audio_files(folder)
        if not files:
            return None
        side = read_sidecar(folder)
        attached = LibraryService._attach(
            voice, files, sides={folder: side}, main_hint=side.main_sample
        )
        # 封面照本地那份：同一个文件夹，不该因为"按平台音色显示"就丢头像。
        return replace(attached, cover=owner.cover)

    def sample_folder(self, voice: Voice) -> Path | None:
        """这条音色的样例该写进哪个文件夹（``None`` = 走引擎保存目录那条老路）。

        平台音色已经有家（扫描根下的样例目录）时，样例就该跟它留在音色库里：
        写进引擎保存目录的话，下一批生成物又落在扫描范围之外，界面上就"没有可以播的"。
        """
        if voice.is_local or not voice.folder:
            return None
        folder = Path(voice.folder)
        return folder if folder.is_dir() else None

    def missing_transcripts(self, limit: int = 0) -> list[tuple[str, str]]:
        """还没有逐字稿、而且文件确实还在的样例（``(voice_id, file)``）。

        「连接时自动识别」的原料：库里的记录可能比磁盘旧（文件被移走、手工改过名），
        这里先把拼不出来的剔掉，识别循环不必再处理一遍"文件不存在"。
        """
        out: list[tuple[str, str]] = []
        for voice_id, file, folder in self._db.samples_without_text(limit):
            if (Path(folder) / file).is_file():
                out.append((voice_id, file))
        return out

    def generated_sample_path(self, voice_id: str, file: str) -> Path | None:
        """生成样例的绝对路径：按 sidecar 归属去引擎保存目录里找。

        音频接口在"音色目录里没有这个文件"时用它回退——本地音色挂上的生成样例
        不在它自己的扫描目录里，只按 ``voice.folder/file`` 会 404。
        """
        for key in list_engines():
            for path in self._generated_index(key).by_id.get(voice_id, []):
                if path.name == file:
                    return path
        return None

    def _generated_index(self, engine_key: str) -> _GeneratedAudio:
        """这台引擎保存目录里的产物索引（带缓存）。

        每次列表都 ``iterdir`` 全目录 + 逐文件夹读 sidecar，规模上来后就是主要成本。
        缓存认三个信号：

        - **根目录 mtime**：新建 / 删掉一个音色目录会改它，立刻重建；
        - **sidecar 写入次数**（:func:`app.infra.sidecar.revision`）：用户刚改的文字
          就在 sidecar 里，写完必须立刻看到，不能等过期；
        - **存活时间** :data:`_GENERATED_TTL`：往**已有**目录里加音频不动上述两者，
          靠它兜底；本进程写完音频会主动 :meth:`invalidate_generated`，正常用不到。
        """
        root = self._engine_root_of(engine_key)
        if root is None:
            return _empty_generated()
        try:
            mtime = root.stat().st_mtime_ns
        except OSError:
            return _empty_generated()
        rev = sidecar.revision()
        cached = self._generated_cache.get(engine_key)
        if cached is not None:
            stamp, rev_seen, built_at, index = cached
            if (
                stamp == mtime
                and rev_seen == rev
                and time.monotonic() - built_at < _GENERATED_TTL
            ):
                return index
        index = self._scan_generated(root)
        self._generated_cache[engine_key] = (mtime, rev, time.monotonic(), index)
        return index

    def invalidate_generated(self, engine_key: str = "") -> None:
        """丢掉产物索引缓存（写完音频立刻调；不传 key 清全部）。"""
        if engine_key:
            self._generated_cache.pop(engine_key, None)
        else:
            self._generated_cache.clear()

    @staticmethod
    def _engine_root_of(engine_key: str) -> Path | None:
        """这台引擎的音色保存目录；没装 / 目录不在 = ``None``。"""
        try:
            engine = require_engine(engine_key)
        except EngineConfigError:
            return None
        root = archive.engine_root(engine_key, engine.label, create=False)
        return root if root.is_dir() else None

    @staticmethod
    def _scan_generated(root: Path) -> _GeneratedAudio:
        """真的走一遍磁盘：文件夹里有音频才算产物，sidecar 一起带出来。"""
        try:
            folders = [path for path in root.iterdir() if path.is_dir()]
        except OSError:
            return _empty_generated()
        by_id: dict[str, list[Path]] = {}
        by_name: dict[str, list[Path]] = {}
        sides: dict[Path, Sidecar] = {}
        for folder in folders:
            files = _audio_files(folder)
            if not files:
                continue
            by_name[folder.name] = files
            side = read_sidecar(folder)
            sides[folder] = side
            owner = side.origin.get("voice_id", "")
            if owner:
                by_id.setdefault(owner, []).extend(files)
        return _GeneratedAudio(by_id, by_name, sides)

    @staticmethod
    def _attach(
        voice: Voice,
        files: list[Path],
        *,
        merge: bool = False,
        sides: dict[Path, Sidecar] | None = None,
        main_hint: str = "",
    ) -> Voice:
        """把这几段生成音频挂到这条音色上。

        平台音色：目录换成引擎保存目录，样例就是这些（它自己磁盘上没有目录）；
        本地音色（``merge=True``）：目录不动，生成的样例**并进**原有列表——
        整体替换会让用户自己录的那几条从界面上消失。

        ``sides`` 是产物索引里已经读好的 sidecar（按目录）。一条音色的生成样例
        可能散在几个目录里（本地音色跨引擎），所以按**文件自己所在的目录**取，
        而不是拿第一个文件的目录当全部——旧写法会把别的目录的文字套错。

        :param main_hint: 库里没指定主样例时用它（sidecar 里用户换过的那份）；
            都不认时取文件名最大的那段（时间戳命名 = 最新生成的）。
        """
        sides = sides or {}
        folder = files[0].parent
        loaded: dict[Path, Sidecar] = {}
        ffprobe = load_config().ffprobe_path
        generated: list[Sample] = []
        for path in files:
            side = sides.get(path.parent) or loaded.get(path.parent)
            if side is None:
                side = loaded[path.parent] = read_sidecar(path.parent)
            entry = side.samples.get(path.name, {})
            generated.append(
                Sample(
                    file=path.name,
                    text=entry.get("text", ""),
                    emotion=entry.get("emotion", ""),
                    # 同一个文件在每次列表 / 详情里都会被问到：走进程内备忘，不重起 ffprobe。
                    duration=probe_duration_cached(path, ffprobe_path=ffprobe),
                    tags=tuple(entry.get("tags") or ()),
                    # 生成时顺手登记的参数，这里只负责读回来显示。
                    params=sample_params(entry),
                )
            )
        samples = tuple(generated)
        if merge:
            existing = {sample.file for sample in voice.samples}
            merged = voice.samples + tuple(
                sample for sample in samples if sample.file not in existing
            )
            return replace(voice, samples=merged, sample_count=len(merged))
        names = {path.name for path in files}
        main = voice.main_sample
        if main not in names:
            # 库里没指定就用 sidecar 里那份（用户换过主样例）；都没有时取最新的一段。
            main = main_hint if main_hint in names else files[0].name
        return replace(
            voice,
            folder=str(folder),
            samples=samples,
            sample_count=len(samples),
            main_sample=main,
        )

    def tag_cloud(self, **filters: Any) -> list[tuple[str, int]]:
        """当前筛选下各标签还剩几条（**``tag`` 自己不参与**——理由在 infra/db）。"""
        return self._db.tag_cloud(**{k: v for k, v in filters.items() if v})

    def get_voice(self, voice_id: str) -> Voice:
        voice = self._db.get_voice(voice_id)
        if voice is None:
            raise LibraryError(
                f"音色 {voice_id} 不存在，可能在重新扫描后路径已变化。",
                code="voice_not_found",
                status=404,
            )
        # 详情页、试听、参考音频、识别共用 get_voice，在源头统一挂上生成样例。
        return self._with_generated([voice])[0]

    # ------------------------------------------------------------------ #
    # 编辑（本地音色 → sidecar + 索引）
    # ------------------------------------------------------------------ #

    def sync_local_folder(self, folder: Path) -> Voice | None:
        """把一个本地音色目录的磁盘现状（文件 + sidecar）即时写进索引，返回该行。

        入库这类"本进程刚写完磁盘"的写动作走它：写完立刻可见，不依赖下一次扫描。
        元数据与样例口径与 :meth:`_merge_sidecar` 一致（sidecar 为源）；封面沿用
        库里已有的（新目录还没有封面文件，扫描以后再认）。目录是某个扫描根本身
        （平铺布局，每个文件一条音色）时返回 ``None``——那里的新文件是新的独立
        音色，交给扫描去认。
        """
        folder = Path(folder)
        if not folder.is_dir():
            return None
        config = load_config()
        roots = [Path(str(item).strip()) for item in config.folders if str(item).strip()]
        if any(folder == root for root in roots):
            return None
        side = read_sidecar(folder)
        names = [path.name for path in _audio_files(folder)]
        kept = {key: value for key, value in side.samples.items() if key in names}
        ffprobe = config.ffprobe_path
        samples = tuple(
            Sample(
                file=name,
                text=str(kept.get(name, {}).get("text") or ""),
                emotion=str(kept.get(name, {}).get("emotion") or ""),
                duration=probe_duration_cached(folder / name, ffprobe_path=ffprobe),
                tags=tuple(kept.get(name, {}).get("tags") or ()),
                params=sample_params(kept.get(name)),
            )
            for name in names
        )
        voice_id = voice_id_for(str(folder))
        previous = self._db.get_voice(voice_id)
        category = previous.category if previous is not None else self._category_of(folder, roots)
        voice = Voice(
            id=voice_id,
            source=SOURCE_LOCAL,
            name=folder.name,
            display=side.display,
            category=category,
            gender=side.gender if side.gender in GENDERS else "",
            language=side.language,
            age=side.age,
            tags=tuple(side.tags),
            description=side.description,
            folder=str(folder),
            cover=previous.cover if previous is not None else "",
            main_sample=main_sample_of(names, side.main_sample),
            samples=samples,
            origin_id=_linked_origin(side, str(folder)),
            hidden=previous.hidden if previous is not None else False,
            favorite=previous.favorite if previous is not None else False,
        )
        self._db.upsert_voice(voice)
        return voice

    @staticmethod
    def _category_of(folder: Path, roots: list[Path]) -> str:
        """目录相对哪个扫描根的分类（不在任何根下 = 空串）。"""
        for root in roots:
            if root.is_dir() and folder != root and root in folder.parents:
                return relative_category(folder, root)
        return ""

    def update_voice(self, voice_id: str, patch: dict[str, Any]) -> Voice:
        """改音色元数据。**先写 sidecar、再更新索引**——sidecar 是源。"""
        voice = self.get_voice(voice_id)
        if not voice.is_local:
            return self._update_platform_voice(voice, patch)

        allowed = ("display", "gender", "age", "language", "tags", "description", "main_sample")
        clean = {k: v for k, v in patch.items() if k in allowed}
        if "gender" in clean and clean["gender"] not in (*GENDERS, ""):
            raise LibraryError(
                f"性别取值不合法：「{clean['gender']}」。"
                f"允许值：{'、'.join(label for _, label in GENDER_CHOICES)}。"
            )
        if clean.get("main_sample"):
            files = {s.file for s in voice.samples}
            if clean["main_sample"] not in files:
                raise LibraryError(f"「{clean['main_sample']}」不在该音色的目录中。")

        side = read_sidecar(Path(voice.folder))
        kept = {k: v for k, v in side.samples.items() if k in {s.file for s in voice.samples}}
        write_sidecar(
            Path(voice.folder),
            Sidecar(
                gender=str(clean.get("gender", side.gender) or "").strip(),
                age=str(clean.get("age", side.age) or "").strip(),
                language=str(clean.get("language", side.language) or "").strip(),
                tags=[str(t).strip() for t in clean.get("tags", side.tags) if str(t).strip()],
                description=str(clean.get("description", side.description) or "").strip(),
                display=str(clean.get("display", side.display) or "").strip(),
                main_sample=str(clean.get("main_sample", side.main_sample) or "").strip(),
                # 偏好两栏原样带着走：这里改的是元信息，不该动收藏/隐藏。
                favorite=side.favorite,
                hidden=side.hidden,
                samples=kept,
                # 归属原样带着走：这里是"改音色的元信息"，不是"改谁家的音频"。
                origin=side.origin,
            ),
        )
        updated = replace(
            voice,
            display=clean.get("display", voice.display),
            gender=clean.get("gender", voice.gender),
            age=clean.get("age", voice.age),
            language=clean.get("language", voice.language),
            tags=tuple(clean.get("tags", voice.tags)),
            description=clean.get("description", voice.description),
            main_sample=main_sample_of(
                [s.file for s in voice.samples],
                str(clean.get("main_sample", side.main_sample) or ""),
            ),
        )
        self._db.upsert_voice(updated)
        self._db.touch_recent(voice_id)
        return updated

    def update_sample(self, voice_id: str, file: str, patch: dict[str, Any]) -> Voice:
        """改一条样例的 text/emotion/tags（写 sidecar 的 samples 键）。

        平台音色也走这里：其保存目录里生成的几段就是它的样例。区别只在写库这一步——
        本地行顺手更新；平台行是平台的记录（folder 该空着），只落 sidecar，
        下次 :meth:`_attach` 读回。
        """
        voice = self.get_voice(voice_id)
        target = next((s for s in voice.samples if s.file == file), None)
        if target is None:
            raise LibraryError(
                f"音色中不存在样例「{file}」。", code="sample_not_found", status=404
            )
        # 没提到的字段保持原样（前端一次只改一格）。
        text = str(patch.get("text", target.text) or "").strip()
        emotion = str(patch.get("emotion", target.emotion) or "").strip()
        tags = tuple(str(t).strip() for t in patch.get("tags", target.tags) if str(t).strip())

        side = read_sidecar(Path(voice.folder))
        entries = {k: v for k, v in side.samples.items() if k in {s.file for s in voice.samples}}
        # 这一格的字节数重新读一次：它是时长的校验值，跟着这条记录一起写。
        try:
            size = (Path(voice.folder) / file).stat().st_size
        except OSError:
            size = 0
        entries[file] = {
            "text": text,
            "emotion": emotion,
            # 时长备份跟着走：这一格是从"念的是什么"重建的，键不带过来就丢了备份。
            "duration": target.duration,
            "bytes": size,
            # params 来路跟着走：这里改的是"念的是什么"，不能抹掉生成记录。
            "params": dict(target.params),
        }
        if tags:
            entries[file]["tags"] = list(tags)
        write_sidecar(
            Path(voice.folder),
            Sidecar(
                gender=side.gender,
                age=side.age,
                language=side.language,
                tags=side.tags,
                description=side.description,
                display=side.display,
                main_sample=side.main_sample,
                favorite=side.favorite,
                hidden=side.hidden,
                samples=entries,
                # 改样例文字不该抹掉归属。
                origin=side.origin,
            ),
        )
        samples = tuple(
            replace(s, text=text, emotion=emotion, tags=tags) if s.file == file else s
            for s in voice.samples
        )
        updated = replace(voice, samples=samples)
        if voice.is_local:
            self._db.upsert_voice(updated)  # 平台那几行是平台的记录，不写；见方法说明
        return updated

    def replace_sample_audio(
        self,
        voice_id: str,
        file: str,
        source: Path,
        *,
        params: dict[str, Any] | None = None,
        seconds: float = 0.0,
    ) -> Voice:
        """用处理过的音频替换一条样例：原件进回收站，记录与文件名保留。

        与"删一条再加一条"的区别在**文件名不变**：主样例、逐字稿、平台凭据
        ``payload.ref_audio`` 都按路径认这一条，换名字等于把它们全部作废。

        顺序是先落临时文件、再移原件、最后改名：任何一步失败，手上要么还是原件，
        要么原件在回收站且新文件已就位，不会出现"目录里什么都没有"。

        :param params: 处理链，写进 sidecar 的 ``samples[file].params``。
        :param seconds: 实测时长；``0`` 则自己量。时长决定库里的记录，不靠推断。
        """
        voice = self.get_voice(voice_id)
        target_sample = next((s for s in voice.samples if s.file == file), None)
        if target_sample is None:
            raise LibraryError(
                f"音色中不存在样例「{file}」。", code="sample_not_found", status=404
            )
        if not voice.folder:
            raise LibraryError(
                "平台音色没有可替换的本地文件。", code="invalid_operation", status=400
            )
        folder = Path(voice.folder)
        target = folder / file
        if not source.is_file():
            raise LibraryError(
                f"处理结果不存在（{source}）。", code="invalid_value", status=400
            )

        # 临时名不带音频后缀：扫描器只认音频扩展名，半份文件不会被认成一条样例。
        staged = folder / f".{file}.incoming"
        try:
            shutil.copyfile(source, staged)
        except OSError as exc:
            raise LibraryError(
                f"写入处理结果失败：{exc}。", code="invalid_operation", status=500
            ) from exc
        try:
            if target.is_file():
                move_to_trash(target)
        except TrashError as exc:
            staged.unlink(missing_ok=True)
            # 有的环境文件已进回收站却回错误码：只在原件确实还在时才算失败。
            if target.exists():
                raise LibraryError(str(exc), code="invalid_operation") from exc
        try:
            os.replace(staged, target)
        except OSError as exc:
            staged.unlink(missing_ok=True)
            raise LibraryError(
                f"替换音频失败：{exc}。", code="invalid_operation", status=500
            ) from exc

        measured = (
            round(float(seconds), 3)
            if seconds > 0
            else probe_duration_cached(target, ffprobe_path=load_config().ffprobe_path)
        )
        size = target.stat().st_size
        side = read_sidecar(folder)
        names = {s.file for s in voice.samples}
        entries = {key: value for key, value in side.samples.items() if key in names}
        entry = dict(entries.get(file) or {})
        entry["bytes"] = size
        entry["duration"] = measured
        merged: dict[str, str] = dict(sample_params(entry))
        if params:
            merged.update({str(key): str(value) for key, value in params.items() if str(value)})
            entry["params"] = merged
        entries[file] = entry
        write_sidecar(
            folder,
            Sidecar(
                gender=side.gender,
                age=side.age,
                language=side.language,
                tags=side.tags,
                description=side.description,
                display=side.display,
                main_sample=side.main_sample,
                favorite=side.favorite,
                hidden=side.hidden,
                samples=entries,
                origin=side.origin,
            ),
        )
        samples = tuple(
            replace(
                sample,
                duration=measured,
                params=tuple(merged.items()) if params else sample.params,
            )
            if sample.file == file
            else sample
            for sample in voice.samples
        )
        updated = replace(voice, samples=samples)
        if voice.is_local:
            self._db.upsert_voice(updated)
        return updated

    def set_main_sample(self, voice_id: str, file: str) -> Voice:
        """设主样例（空串 = 取消，回到"默认标记 → 升序第一条"）。"""
        return self.update_voice(voice_id, {"main_sample": file})

    def delete_sample(self, voice_id: str, file: str) -> Voice:
        """移除一条样例：文件进回收站（可还原），sidecar 与库里的记录同时摘掉。

        **两半必须一起动**：只摘记录，下次扫描会把文件认回来（用户以为没删掉）；
        只删文件，详情里就挂着一条点不开的音频。顺序是**先磁盘后记录**——
        文件移不走的时候，库里那条不该已经消失了。

        平台音色的生成物在引擎保存目录里，走同一条路（路径由
        :meth:`generated_sample_path` 认）；它那一行是平台的记录，不写库。
        删掉的正好是主样例时，主样例按"默认标记 → 升序第一条"重新落一条，
        免得 sidecar 里留一个指向空文件的名字。
        """
        voice = self.get_voice(voice_id)
        target = next((s for s in voice.samples if s.file == file), None)
        if target is None:
            raise LibraryError(
                f"音色中不存在样例「{file}」。", code="sample_not_found", status=404
            )
        path = Path(voice.folder) / file if voice.folder else None
        if path is None or not path.is_file():
            path = self.generated_sample_path(voice_id, file)
        if path is not None and path.is_file():
            try:
                move_to_trash(path)
            except TrashError as exc:
                # 有的环境（含部分沙箱/外壳扩展）**文件已经进了回收站却回一个错误码**。
                # 只要文件确实不在了就当它成功——否则会留下"库里有、磁盘上没有"的残样，
                # 那比多删一条记录难收拾得多。
                if path.exists():
                    raise LibraryError(str(exc), code="invalid_operation") from exc

        remaining = tuple(s for s in voice.samples if s.file != file)
        main = main_sample_of([s.file for s in remaining], voice.main_sample)
        folder = Path(voice.folder)
        if voice.folder and folder.is_dir():
            side = read_sidecar(folder)
            entries = {
                key: value for key, value in side.samples.items() if key in {s.file for s in remaining}
            }
            # 归属必须跟着走：删一段音频不该让这条音色跟它剩下的生成物失联。
            write_sidecar(
                folder,
                Sidecar(
                    gender=side.gender,
                    age=side.age,
                    language=side.language,
                    tags=side.tags,
                    description=side.description,
                    display=side.display,
                    main_sample=main,
                    favorite=side.favorite,
                    hidden=side.hidden,
                    samples=entries,
                    origin=side.origin,
                ),
            )
        updated = replace(voice, samples=remaining, main_sample=main)
        if voice.is_local:
            self._db.upsert_voice(updated)  # 平台那几行是平台的记录，不写；见 update_sample
        return updated

    def set_prefs(
        self, voice_id: str, *, hidden: bool | None = None, favorite: bool | None = None
    ) -> Voice:
        """收藏/隐藏：写数据库，**同时写进音色目录的 sidecar**。

        sidecar 在文件夹里，是"跟着这条音色走"的那一份：文件夹改名或搬家，
        ``voice_id`` 会变、数据库那两栏会随旧行消失，只有它留得住。
        平台音色没有自己的目录，只写库。
        """
        voice = self.get_voice(voice_id)
        self._db.set_prefs(voice_id, hidden=hidden, favorite=favorite)
        if voice.is_local and voice.folder:
            folder = Path(voice.folder)
            if folder.is_dir():
                side = read_sidecar(folder)
                write_sidecar(
                    folder,
                    Sidecar(
                        gender=side.gender,
                        age=side.age,
                        language=side.language,
                        tags=side.tags,
                        description=side.description,
                        display=side.display,
                        main_sample=side.main_sample,
                        favorite=side.favorite if favorite is None else favorite,
                        hidden=side.hidden if hidden is None else hidden,
                        samples=side.samples,
                        origin=side.origin,
                    ),
                )
        return self.get_voice(voice_id)

    # ------------------------------------------------------------------ #
    # 平台音色（手动登记；P2 起由引擎插件拉清单）
    # ------------------------------------------------------------------ #

    def add_platform_voice(
        self,
        platform: str,
        meta: VoiceMeta,
        *,
        previous: Voice | None = None,
        payload: dict | None = None,
    ) -> Voice:
        """写入/更新一条平台音色。

        :param previous: 库里那条旧的（刷新清单时传入）。
        :param payload: 引擎私有的"以后怎么生成"凭据（如 MiMo 复刻音色的参考样本）；
            不给就沿用旧值——刷新清单不该把复刻凭据抹掉。

        认出来的东西不许盖掉用户写过的值（语言、性别、年龄感、标签同规则）：
        这些是插件从平台命名与说明里猜的，已填过则保守保留——宁可漏一次识别更新，
        也不删用户手写的字。
        """
        platform = platform.strip()
        if not platform:
            raise LibraryError("平台名不能为空。")
        if not meta.id.strip():
            raise LibraryError("平台音色 id 不能为空。")
        voice = Voice(
            id=platform_voice_id(platform, meta.id),
            source=platform,
            name=meta.id.strip(),
            display=meta.name.strip(),
            category=meta.category.strip(),
            description=meta.description.strip(),
            tags=previous.tags if previous and previous.tags else tuple(meta.tags),
            gender=previous.gender if previous and previous.gender else meta.gender,
            age=previous.age if previous and previous.age else meta.age,
            language=previous.language if previous and previous.language else meta.language,
            payload=dict(payload) if payload else (dict(previous.payload) if previous else {}),
        )
        self._db.upsert_voice(voice)
        return voice

    def import_platform_voices(
        self, platform: str, items: tuple[VoiceMeta, ...]
    ) -> tuple[int, int]:
        """把拉到的平台音色清单写进库，返回 ``(新增, 更新)``。

        只增改、不删：清单里消失的条目可能是"这次没拉到"（网络/权限）而非
        "平台真的删了"，删错代价高。旧值交给 :meth:`add_platform_voice` 挡住猜测的格子。
        """
        existing = {v.id: v for v in self._db.list_voices(source=platform)}
        added = updated = 0
        for meta in items:
            voice_id = platform_voice_id(platform, meta.id)
            if voice_id in existing:
                updated += 1
            else:
                added += 1
            self.add_platform_voice(platform, meta, previous=existing.get(voice_id))
        return added, updated

    def _update_platform_voice(self, voice: Voice, patch: dict[str, Any]) -> Voice:
        """平台音色的元数据（显示名、标签、描述……）。

        写回库里那一行原文（重新 ``_db.get_voice``），不是挂过生成结果的版本——
        那个版本带 folder 与本地样例，写回平台行会造成两边不一致。
        本地样例字段走 :meth:`update_sample`，落 sidecar。
        """
        stored = self._db.get_voice(voice.id) or voice
        allowed = (
            "display",
            "gender",
            "age",
            "language",
            "tags",
            "description",
            "category",
            "main_sample",
        )
        clean = {k: v for k, v in patch.items() if k in allowed}
        if clean.get("main_sample"):
            files = {s.file for s in voice.samples}
            if clean["main_sample"] not in files:
                raise LibraryError(f"「{clean['main_sample']}」不在该音色的样例中。")
        updated = replace(
            stored,
            display=str(clean.get("display", stored.display) or "").strip(),
            gender=str(clean.get("gender", stored.gender) or "").strip(),
            age=str(clean.get("age", stored.age) or "").strip(),
            language=str(clean.get("language", stored.language) or "").strip(),
            tags=tuple(str(t).strip() for t in clean.get("tags", stored.tags) if str(t).strip()),
            description=str(clean.get("description", stored.description) or "").strip(),
            category=str(clean.get("category", stored.category) or "").strip(),
            main_sample=str(clean.get("main_sample", stored.main_sample) or "").strip(),
            payload=_design_prompt_sync(stored, clean.get("description")),
        )
        self._db.upsert_voice(updated)
        # 回给界面的那份要带上生成出来的样例（详情页正开着它）。
        return self.get_voice(voice.id)

    def delete_voice(self, voice_id: str) -> dict[str, Any]:
        """移除一条音色。本地音色连磁盘一起处理（只删库行的话重扫又回来）。

        文件进回收站（可还原），不是真删。

        :returns: ``{"removed": 名字, "trashed": [进了回收站的路径]}``。
        """
        voice = self.get_voice(voice_id)
        if not voice.is_local:
            self._db.delete_voice(voice_id)
            return {"removed": voice.shown_name, "trashed": []}
        targets = self._removal_targets(voice)
        trashed: list[str] = []
        try:
            for target in targets:
                move_to_trash(target)
                trashed.append(str(target))
        except TrashError as exc:
            # 动了几个就报几个：已进回收站的清单要说出来，不能一句"失败"。
            raise LibraryError(
                f"{exc}（已移入回收站：{'、'.join(trashed) or '无'}）",
                code="invalid_operation",
            ) from exc
        self._db.delete_voice(voice_id)
        return {"removed": voice.shown_name, "trashed": trashed}

    def _removal_targets(self, voice: Voice) -> list[Path]:
        """这条音色在磁盘上占着哪些东西。

        平铺布局（扫描根层直接放音频）中多个音色共用 folder，此时只删它那几个
        音频文件——判据是 folder 是否为某个扫描根目录本身（与 walker 一致）。
        """
        folder = Path(voice.folder)
        if not folder.is_dir():
            # 文件夹已不在（移动盘没挂载等），只清库里那条。
            return []
        if folder.parent == folder:
            raise LibraryError(
                f"{folder} 是磁盘根目录，已拒绝操作。请手工处理。", code="unsafe_path"
            )
        roots = {str(Path(root)) for root in load_config().folders}
        if str(folder) in roots:
            return [folder / s.file for s in voice.samples]
        return [folder]

    def delete_platform_voice(self, voice_id: str) -> None:
        voice = self.get_voice(voice_id)
        if voice.is_local:
            raise LibraryError(
                "本地音色的文件位于磁盘上，请使用移除操作（文件将移入回收站）。",
                code="invalid_operation",
            )
        self._db.delete_voice(voice_id)

    def clear_source_voices(self, source: str) -> int:
        """清除某个平台来源在库里的全部条目，返回条数。

        只删库：平台音色的条目本身就是音色 id 与元数据；已生成的样例躺在引擎
        保存目录里，仍留在磁盘上。本地音色不属于任何一个平台来源，不接受此操作。
        """
        source = (source or "").strip()
        if not source or source == SOURCE_LOCAL:
            raise LibraryError("只能清除平台来源的音色条目。", code="invalid_operation")
        return self._db.delete_voices_by_source(source)

    # ------------------------------------------------------------------ #
    # 扫描根目录
    # ------------------------------------------------------------------ #

    def folders(self) -> list[str]:
        return load_config().folders

    def save_folders(self, folders: list[str]) -> dict[str, Any]:
        """改扫描根目录并**立刻重扫**——界面不需要一颗单独的「保存后记得扫描」按钮。"""
        config = load_config()
        config.folders = [str(f).strip() for f in folders if str(f).strip()]
        save_config(config)
        return self.scan()

    # ------------------------------------------------------------------ #
    # 对接口（给 audiobook）
    # ------------------------------------------------------------------ #

    def reference_of(self, voice_id: str) -> dict[str, Any]:
        """主样例路径 + 说话内容 + 情绪——合成参考，一个请求拿全。

        本地音色给绝对路径；平台音色生成过之后同样有本机文件
        （判据是"有没有本地音频"，不是"是不是本地音色"）。
        """
        voice = self.get_voice(voice_id)
        self._db.touch_recent(voice_id)
        main = next((s for s in voice.samples if s.file == voice.main_sample), None)
        return {
            "voice": voice.shown_name,
            "platform": "" if voice.is_local else voice.source,
            "sample_path": str(Path(voice.folder) / main.file) if main and voice.folder else "",
            "text": main.text if main else "",
            "emotion": main.emotion if main else "",
            "note": "" if main else "该音色尚无可用样例。",
        }
