"""audio.cpp 工作区的状态机：接入哪台服务、跑哪些实例、模型装到哪。

三层各管一件事，界面只读结果：

- **实例**：端口与模型 id 取自**引擎设置里的服务地址**（``base_url`` / ``model``），
  插件照着它拉起进程；手加的实例另存一份，两边不重复记账。
- **服务**：``endpoint`` 有值就用用户填的地址（外部实例，只读）；
  为空则用实例表。
- **运行包**：插件 ``bin/`` 下的 ``audiocpp_server``，或用户自己指定的可执行文件；
  下载、校验、解压见 ``runtimes``。
- **模型**：清单来自官方 ``model_specs``（见 ``catalog``），安装/停止/删除全部转发
  实例自己的 ``/v1/ui/models/*``（见 ``manager``），不再自建下载器。

所有文件落 ``<data>/producers/audiocpp/``；不写宿主配置，也不往别人的安装目录里放东西。
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.infra.atomic import write_json
from app.infra.loghub import CHANNEL
from app.infra.trash import TrashError, move_to_trash
from app.producers.base import ProducerError
from app.producers.builtin.audiocpp import catalog, manager, runtimes
from app.producers.builtin.audiocpp.processes import Instance, ProcessPool, url_of

__all__ = ["FAMILY_ALIASES", "MIRRORS", "RuntimeService"]

#: 引擎 key 与官方 ``model_specs`` 族名不同名的那几个；其余按「去掉非字母数字后同名」匹配。
FAMILY_ALIASES = {"indextts2": "index_tts2"}

#: 实例模式覆盖：SenseVoice 的 offline 路径要 Silero VAD
#: （``assets/framework/models/silero_vad``），内置运行包不带这份资产；
#: streaming 用固定窗口，不依赖 VAD。
MODE_OVERRIDES = {"sense_asr": "streaming"}

#: 只认回环地址：远程服务没法在这里拉起进程，那种用「填地址接入」。
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

#: 模型下载镜像；写进实例进程的 ``AUDIOCPP_HF_BASE_URL``。
#: 实测：直连 huggingface.co 在国内不通，官方下载器会一直停在 0 字节。
#: ModelScope 不是 HF 兼容站，服务端的安装接口也不认 ``source``；它走随运行包
#: 分发的官方 ``tools/model_manager_v2.py``（见 ``_install_from_modelscope``）。
MIRRORS = (
    {"id": "hf-mirror", "label": "HF 镜像（hf-mirror.com）", "url": "https://hf-mirror.com", "source": ""},
    {"id": "modelscope", "label": "ModelScope（modelscope.cn）", "url": "", "source": "modelscope"},
    {"id": "direct", "label": "Hugging Face 直连", "url": "", "source": ""},
)

#: ModelScope 上的 audio.cpp 包镜像仓库：官方 spec 只有 HF 地址，这里按包内相对路径取。
_MODELSCOPE_REPO = "HereIsMark/audio.cpp-gguf"
#: 官方管理器随完整运行目录分发；内置运行包只解压可执行文件，可能没有它。
_MANAGER_REL = Path("tools") / "model_manager_v2.py"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: 下载器子进程继承宿主的环境变量。一条**失效的本地代理**（例如 Clash 关掉后
#: ``http_proxy`` 还留在环境里）会让 urllib 每次去连那个端口，报
#: ``WinError 10061``，模型一个字节都下不来。回环地址上的代理必然是本机软件：
#: 端口没人听就说明它没开，摘掉这几格让下载器直连（ModelScope 国内可直连）。
#:
#: 只存小写：Windows 的 ``os.environ`` 大小写不敏感，写进去的键会被规范成大写。
_PROXY_KEYS = frozenset(
    {"http_proxy", "https_proxy", "all_proxy"}
)

#: 代理地址是回环时，判"还在不在"用的连接超时（秒）。
_PROXY_PROBE_TIMEOUT = 0.3

#: 持久化结构版本。v5：实例改由引擎设置推导，不再存一份写死的端口表。
_SCHEMA = 5
#: 扫描到但没人配过端口的模型：从这一段里挑空闲端口，分配结果写进 ``auto_ports``。
#: 起点避开引擎默认的 180xx 与 hub 常用的 189xx，减少和现有服务撞车。
_AUTO_PORT_BASE = 18100
_AUTO_PORT_END = 18200
#: ``_scan_root`` 的"没传连接"标记：与"传入了 None（已经确认没有活服务）"区分开。
_UNRESOLVED = object()


def _default_mirror() -> str:
    return MIRRORS[0]["id"]


def _mirror_url(mirror_id: str) -> str:
    for row in MIRRORS:
        if row["id"] == mirror_id:
            return str(row["url"])
    return ""


def _mirror_source(mirror_id: str) -> str:
    for row in MIRRORS:
        if row["id"] == mirror_id:
            return str(row.get("source") or "")
    return ""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _port_of_url(url: str) -> int:
    try:
        return int(urlparse(url).port or 0)
    except ValueError:
        return 0


def _local_proxy_down(raw: str) -> bool:
    """代理地址是回环、且那个端口没人监听 → ``True``；非回环地址一律当有效。

    只判回环：远程代理连不上可能是网络暂时不通，替用户摘掉会更糟。
    """
    try:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        host = (parsed.hostname or "").lower()
        port = parsed.port or 80
    except ValueError:
        return False
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    try:
        with socket.create_connection((host, port), timeout=_PROXY_PROBE_TIMEOUT):
            return False
    except OSError:
        return True


def _download_env() -> dict[str, str]:
    """下载器子进程的环境：清掉指向已关闭本地代理的那几格，其余照继承。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for key in [name for name in env if name.lower() in _PROXY_KEYS]:
        raw = env[key].strip()
        if raw and _local_proxy_down(raw):
            env.pop(key, None)
            CHANNEL.info(f"下载器忽略失效的本地代理 {key}={raw}", source="audiocpp")
    return env


def _family_for(engine_key: str, known: set[str]) -> str:
    """引擎 → 官方族名：先查别名，再按归一同名匹配（``indextts2`` = ``index_tts2``）。"""
    alias = FAMILY_ALIASES.get(engine_key)
    if alias and alias in known:
        return alias
    target = _norm(engine_key)
    for family in sorted(known):
        if _norm(family) == target:
            return family
    return ""


def _engines(*, active: bool) -> list[tuple[str, str, Any]]:
    """``(key, label, 插件实例)``：合成 + 识别，按 key 排序。

    ``active`` 只看**已启用**的：没启用的引擎 VoiceHub 不会去调，把它列进实例表
    就是凭空多出来的几行。一台都没启用时退回全部内置引擎——否则新装的人既没有
    实例、也就没法装模型，两条规则互相卡死。
    """
    from app.engines.registry import list_asr_engines, list_engines

    tts = list_engines()
    if active:
        from app.usecase.engines import active_engines

        tts = active_engines()
        if not tts:
            tts = list_engines()
    rows: list[tuple[str, str, Any]] = [(key, engine.label, engine) for key, engine in tts.items()]
    rows.extend((key, engine.label, engine) for key, engine in list_asr_engines().items())
    return rows


def _engine_instances() -> list[Instance]:
    """实例清单的唯一起点：**引擎设置里的服务地址**。

    引擎的 ``base_url`` 与 ``model`` 就是 VoiceHub 合成时真正要连的地址与模型 id，
    插件照着它拉起实例，才不会「拉起了，引擎却连到别处」。端口不另抄一份默认表：
    那张表既漏了 voxcpm2，又把 breeze 的 8080 当成唯一答案（用户早把它挪到 18090）。
    """
    from app.usecase.engines import settings_for

    known = {str(row.get("family") or "") for row in catalog.families()}
    found: list[Instance] = []
    for key, label, engine in _engines(active=True):
        with contextlib.suppress(Exception):
            cfg = settings_for(engine)
            base = str(getattr(cfg, "base_url", "") or "")
            host = urlparse(base).hostname if base else None
            port = _port_of_url(base)
            if not port or host not in _LOOPBACK:
                continue
            family = _family_for(key, known)
            if not family:
                continue  # 不是 audio.cpp 托管的模型（如 GPT-SoVITS），不进这张表
            model = str(getattr(cfg, "model", "") or "") or family
            found.append(Instance(port, model, label, family, engine=key))
    found.sort(key=lambda item: (item.port, item.model))
    # 默认地址有撞车（breeze 与 voxcpm2 默认都是 8080）：一个端口只能有一台，
    # 保留先出现的那台——用户把地址改开之后自然就分开了。
    seen: set[int] = set()
    unique: list[Instance] = []
    for item in found:
        if item.port in seen:
            continue
        seen.add(item.port)
        unique.append(item)
    return unique


def _save_engine_base(engine_key: str, port: int) -> None:
    """把引擎设置里的服务地址改到新端口（只改端口，主机与协议照旧）。"""
    from app.engines.registry import detect_asr_engine, detect_engine
    from app.usecase.engines import save_asr_settings, save_engine_settings, settings_for

    engine = detect_engine(engine_key)
    save = save_engine_settings
    if engine is None:
        engine = detect_asr_engine(engine_key)
        save = save_asr_settings
    if engine is None:
        raise ProducerError(f"引擎 {engine_key} 已不存在，改不了它的服务地址。")
    current = str(getattr(settings_for(engine), "base_url", "") or "")
    parsed = urlparse(current) if current else None
    host = (parsed.hostname if parsed else "") or "127.0.0.1"
    scheme = (parsed.scheme if parsed else "") or "http"
    save(engine_key, {"base_url": f"{scheme}://{host}:{port}"})


def _validate_instances(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """手加实例：0–8 条、端口合法且不重复、模型 id 非空。"""
    if len(rows) > 8:
        raise ProducerError("手加实例最多 8 条。")
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        try:
            port = int(row.get("port", 0))
        except (TypeError, ValueError) as exc:
            raise ProducerError("端口必须是数字。") from exc
        model = str(row.get("model") or "").strip()
        family = str(row.get("family") or "").strip()
        if not 1 <= port <= 65535:
            raise ProducerError("端口超出范围。")
        if port in seen:
            raise ProducerError(f"端口 {port} 重复。")
        if not model:
            raise ProducerError("模型 id 不能为空。")
        seen.add(port)
        out.append(
            {
                "port": port,
                "model": model,
                "label": str(row.get("label") or model).strip(),
                "family": family,
            }
        )
    return out


def _auto_start_list(value: Any, rows: Any) -> list[str]:
    """自动拉起从「开关」改成「实例清单」：旧布尔值迁移成全部/空。"""
    models = [str(row.get("model") or "") for row in (rows or []) if isinstance(row, dict)]
    if isinstance(value, bool):
        return [model for model in models if model] if value else []
    if isinstance(value, (list, tuple)):
        wanted = {str(item) for item in value}
        return [model for model in models if model in wanted]
    return []


class RuntimeService:
    """一个插件实例；子进程、下载任务与清单缓存都只属于它自己。"""

    def __init__(self, root: Path, *, autostart: bool = True) -> None:
        self.root = root
        self.models_dir = root / "models"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = root / "state.json"
        self.pool = ProcessPool(root)
        self._lock = threading.RLock()
        self._download = {"state": "idle", "progress": 0.0, "note": ""}
        #: ModelScope 下载由官方 Python 管理器执行，任务状态只有插件自己知道。
        self._model_jobs: dict[str, dict[str, Any]] = {}
        self._model_procs: dict[str, subprocess.Popen[str]] = {}
        self._catalog_note = ""
        self._installer = runtimes.Installer(root, self._set_download)
        self._cfg = self._read_state()
        if self._migrated:
            self._write_state()
        self.pool.boot_markers()
        atexit.register(self.pool.stop_all)
        if autostart and self._cfg.get("auto_start"):
            threading.Thread(target=self._auto_start, name="audiocpp-auto", daemon=True).start()
        threading.Thread(target=self._warm, name="audiocpp-warm", daemon=True).start()

    def _warm(self) -> None:
        """预热连接池与后端清单：首屏不必再等 SSL 上下文和一次子进程。"""
        with contextlib.suppress(Exception):
            manager.client()
        with contextlib.suppress(Exception):
            self._installer.backends()

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #

    def _read_state(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "schema": _SCHEMA,
            "endpoint": "",
            "exe": "",
            "models_root": "",
            "mirror": _default_mirror(),
            "backend": "cuda",
            "runtime_id": "",
            "url": "",
            "sha256": "",
            "version": "",
            "instances": [],
            "auto_start": [],
            "auto_ports": {},
        }
        raw: dict[str, Any] = {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                raw = payload
        except (OSError, ValueError):
            pass
        self._migrated = False
        if raw:
            defaults.update({key: raw[key] for key in defaults if key in raw and key != "schema"})
            schema = int(raw.get("schema") or 0)
            if schema < 3:
                defaults["auto_start"] = _auto_start_list(raw.get("auto_start"), defaults["instances"])
                self._migrated = True
            if schema < 4:
                # v3 的 exe / installed / download_source 作废：exe 可能指向别人的安装，
                # 已装模型改为按模型目录扫，下载源改为 HF 镜像设置。
                # exe 不迁移：谁都可以再填，但绝不替用户把别人的安装认成自己的。
                defaults["exe"] = ""
                defaults["mirror"] = "hf-mirror" if raw.get("download_source") == "hf-mirror" else "direct"
                self._migrated = True
            if schema < _SCHEMA:
                # v4 的实例表是插件按写死的端口表灌进去的默认值，不是用户的选择：
                # 端口与模型 id 现在取自引擎设置，旧行一律丢弃，免得又把默认值
                # 显示成「你的实例」。（手加的实例让用户重新加，比认错一台便宜。）
                defaults["instances"] = []
                self._migrated = True
            if not defaults["endpoint"]:
                defaults["endpoint"] = ""
        if defaults["mirror"] not in {row["id"] for row in MIRRORS}:
            defaults["mirror"] = _default_mirror()
        if defaults["backend"] not in runtimes.BACKENDS:
            defaults["backend"] = "cpu"
        return defaults

    def _write_state(self) -> None:
        write_json(self.state_path, self._cfg)

    def instances(self) -> tuple[Instance, ...]:
        """当前实例：引擎设置里的地址 + 手加实例 + 扫描到的已装模型。

        引擎行给端口与模型 id；手加行补特殊用法；**扫描兜底**——插件只是把
        audio.cpp 能挂的模型摆出来，不能因为引擎里没配就看不见已下载的模型。
        扫描行第一次出现时分配一个稳定端口，存在 ``auto_ports`` 里。
        """
        derived = _engine_instances()
        found: list[Instance] = list(derived)
        seen: set[int] = {item.port for item in derived}
        seen_families = {item.family for item in derived if item.family}
        seen_models = {item.model for item in found}
        with self._lock:
            rows = [row for row in (self._cfg.get("instances") or []) if isinstance(row, dict)]
        for row in rows:
            try:
                port = int(row.get("port", 0))
            except (TypeError, ValueError):
                continue
            model = str(row.get("model") or "").strip()
            if not 1 <= port <= 65535 or not model or port in seen:
                continue
            family = str(row.get("family") or "").strip()
            seen.add(port)
            seen_models.add(model)
            if family:
                seen_families.add(family)
            found.append(
                Instance(
                    port,
                    model,
                    str(row.get("label") or model),
                    family,
                )
            )
        installed = catalog.installed_families(self.models_root(), self.root)
        missing = {
            family
            for family in installed
            if family not in seen_families and family not in seen_models
        }
        if missing:
            ports = self._auto_ports(missing, seen)
            for family in sorted(missing):
                port = ports.get(family)
                if port is None:
                    continue
                info = catalog.family(family, self.root)
                found.append(
                    Instance(port, family, str(info.get("name") or family), family, auto=True)
                )
        return tuple(sorted(found, key=lambda item: (item.port, item.model)))

    def _auto_ports(self, families: set[str], taken: set[int]) -> dict[str, int]:
        """扫描行 → 端口：第一次分到哪就记住哪，重启后不漂移。

        映射存在 ``state.json`` 的 ``auto_ports``：引擎地址改了、手加实例删了，
        已装模型的端口仍按这张表来；端口被别人占了才重新分配。
        """
        with self._lock:
            saved = self._cfg.get("auto_ports")
            mapping = {
                str(key): int(value)
                for key, value in (saved.items() if isinstance(saved, dict) else [])
                if str(value).isdigit() and 1 <= int(value) <= 65535
            }
            used = set(taken)
            changed = False
            for family in sorted(families):
                port = mapping.get(family)
                if port is None or port in used:
                    port = self._free_port(used)
                    if port is None:
                        mapping.pop(family, None)
                        continue
                    mapping[family] = port
                    changed = True
                used.add(port)
            if changed:
                self._cfg["auto_ports"] = mapping
                self._write_state()
        return mapping

    def _free_port(self, used: set[int]) -> int | None:
        for port in range(_AUTO_PORT_BASE, _AUTO_PORT_END):
            if port in used or self.pool.port_open(port):
                used.add(port)
                continue
            return port
        return None

    @property
    def endpoint(self) -> str:
        with self._lock:
            return str(self._cfg.get("endpoint") or "")

    def custom_exe(self) -> Path | None:
        """用户指定的可执行文件；填了但文件不在了就当没填，界面会提示重新选。"""
        raw = str(self._cfg.get("exe") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_file() else None

    def exe(self) -> tuple[Path | None, str]:
        """当前用哪份可执行文件，以及它的来源：``custom`` / ``builtin`` / 空。

        自定义优先——用户指了自己那份安装，就不该被内置运行包顶掉。
        """
        custom = self.custom_exe()
        if custom is not None:
            return custom, "custom"
        builtin = self._installer.exe()
        return (builtin, "builtin") if builtin is not None else (None, "")

    def models_root(self) -> Path:
        """实例的模型目录：默认插件自己的，可指向已有的模型库。"""
        raw = str(self._cfg.get("models_root") or "").strip()
        return Path(raw) if raw else self.models_dir

    def settings(
        self,
        *,
        endpoint: str | None = None,
        mirror: str | None = None,
        backend: str | None = None,
        runtime_id: str | None = None,
        exe: str | None = None,
        models_root: str | None = None,
        instances: list[dict[str, Any]] | None = None,
        auto_start: list[str] | None = None,
    ) -> None:
        with self._lock:
            if endpoint is not None:
                self._cfg["endpoint"] = "" if not str(endpoint).strip() else manager.normalize_url(endpoint)
            if exe is not None:
                text = str(exe).strip()
                if text:
                    path = Path(text)
                    if not path.is_file():
                        raise ProducerError("可执行文件不存在，或不是文件。")
                    self._cfg["exe"] = str(path)
                else:
                    self._cfg["exe"] = ""
                self._installer.forget_backends()
            if models_root is not None:
                text = str(models_root).strip()
                if text:
                    path = Path(text)
                    try:
                        path.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        raise ProducerError(f"模型目录不可用：{exc}") from exc
                    self._cfg["models_root"] = str(path)
                else:
                    self._cfg["models_root"] = ""
            if mirror is not None:
                if mirror not in {row["id"] for row in MIRRORS}:
                    raise ProducerError("下载镜像不在清单内。")
                self._cfg["mirror"] = mirror
            if backend is not None:
                if backend not in runtimes.BACKENDS:
                    raise ProducerError("后端不在清单内。")
                self._cfg["backend"] = backend
                row = runtimes.pick("", backend)
                if row:
                    self._apply_runtime(row)
            if runtime_id is not None:
                row = next((item for item in runtimes.rows() if item.get("id") == runtime_id), None)
                if row is None:
                    raise ProducerError("运行包不在清单内。")
                self._apply_runtime(row)
            if instances is not None:
                validated = _validate_instances(instances)
                reserved = {item.port: item.label for item in _engine_instances()}
                for row in validated:
                    if row["port"] in reserved:
                        raise ProducerError(
                            f"端口 {row['port']} 是「{reserved[row['port']]}」的服务地址（来自引擎设置），"
                            "移它请到引擎设置里改地址。"
                        )
                previous = [row for row in (self._cfg.get("instances") or []) if isinstance(row, dict)]
                keep = {(int(row["port"]), str(row["model"])) for row in validated}
                stale = [
                    int(row["port"])
                    for row in previous
                    if (int(row.get("port", 0)), str(row.get("model") or "")) not in keep
                ]
                self._cfg["instances"] = validated
                for port in stale:
                    self.pool.stop(port)
            if auto_start is not None:
                installed = catalog.installed_families(self._scan_root(), self.root)
                known = {item.model: item.family for item in self.instances()}
                self._cfg["auto_start"] = [
                    model
                    for model in auto_start
                    if known.get(model) and (not known[model] or known[model] in installed)
                ]
            self._write_state()

    def _apply_runtime(self, row: dict[str, Any]) -> None:
        self._cfg["runtime_id"] = str(row.get("id") or "")
        self._cfg["backend"] = str(row.get("backend") or "cpu")
        self._cfg["url"] = str(row.get("url") or "")
        self._cfg["sha256"] = str(row.get("sha256") or "")

    def set_port(self, port: int, value: int) -> None:
        """改一台实例的端口。

        引擎带来的行改**引擎地址**（``base_url``），手加的行改插件清单——端口只有
        一份产地，改完不会「这里显示 18095、引擎还在连 18090」。
        """
        rows = self.instances()
        row = next((item for item in rows if item.port == port), None)
        if row is None:
            raise ProducerError("端口不在实例清单内。")
        if not 1 <= value <= 65535:
            raise ProducerError("端口超出范围。")
        if any(item.port == value for item in rows if item.port != port):
            raise ProducerError(f"端口 {value} 已被其他实例占用。")
        if row.engine:
            _save_engine_base(row.engine, value)
            CHANNEL.info(f"audio.cpp：引擎「{row.label}」的服务地址已改为 :{value}", source="audiocpp")
            return
        if row.auto:
            with self._lock:
                saved = self._cfg.get("auto_ports")
                mapping = dict(saved) if isinstance(saved, dict) else {}
                mapping[row.family] = value
                self._cfg["auto_ports"] = mapping
                self._write_state()
            CHANNEL.info(f"audio.cpp：模型「{row.label}」的实例端口已改为 :{value}", source="audiocpp")
            return
        with self._lock:
            saved = [dict(item) for item in (self._cfg.get("instances") or []) if isinstance(item, dict)]
            for item in saved:
                if int(item.get("port") or 0) == port:
                    item["port"] = value
            self._cfg["instances"] = saved
            self._write_state()

    # ------------------------------------------------------------------ #
    # 服务接入
    # ------------------------------------------------------------------ #

    def connection(self) -> manager.Connection | None:
        """当前接入的服务：填了地址就连它；否则连清单里第一台活着的实例。

        先看自己拉起的（pid 标记在、端口也活着），再退到外部进程——否则端口上
        先应答的那台别人的服务会顶掉自己的实例。不按标记硬筛：VoiceHub 重启后
        标记被清掉，但实例还在跑，它仍然是本插件要用的那台。
        """
        endpoint = self.endpoint
        if endpoint:
            return manager.Connection.open(endpoint, timeout=0.6)
        rows = self.instances()
        probes = self.pool.probe_many(item.port for item in rows)
        for item in sorted(rows, key=lambda row: not self.pool.owned(row.port)):
            if probes.get(item.port, (False, 0))[0]:
                conn = manager.Connection.open(url_of(item.port), timeout=0.5)
                if conn is not None:
                    return conn
        return None

    # ------------------------------------------------------------------ #
    # 运行包
    # ------------------------------------------------------------------ #

    def _set_download(self, state: str, progress: float, note: str) -> None:
        with self._lock:
            self._download = {"state": state, "progress": round(progress, 1), "note": note}

    def start_download(self, *, runtime_id: str = "") -> None:
        """下载所选运行包；地址由清单给定，直连不通自动换镜像。"""
        with self._lock:
            if self._download["state"] == "downloading":
                raise ProducerError("正在下载。")
            row = runtimes.pick(runtime_id.strip(), str(self._cfg.get("backend") or "cpu"))
            if not row:
                raise ProducerError("当前平台没有该后端的运行包。")
            self._apply_runtime(row)
            self._write_state()
            self._download = {"state": "downloading", "progress": 0.0, "note": "准备下载"}
        threading.Thread(target=self._download_run, args=(row,), name="audiocpp-download", daemon=True).start()

    def _download_run(self, row: dict[str, Any]) -> None:
        try:
            exe = self._installer.install(row)
            with self._lock:
                self._cfg["version"] = f"v{runtimes.version()}" if runtimes.version() else ""
                self._write_state()
            self._set_download("done", 100.0, "已安装")
            CHANNEL.info(f"audio.cpp 运行包已安装：{exe}", source="audiocpp")
        except Exception as exc:  # noqa: BLE001 - 后台线程的失败只记状态与日志
            self._set_download("error", 0.0, str(exc))
            CHANNEL.warning(f"audio.cpp 运行包下载失败：{exc}", source="audiocpp")

    def uninstall(self, confirm: bool) -> dict[str, Any]:
        """卸载内置运行包：只清插件自己的 bin/config/logs/pids，模型留给用户处置。

        用自定义可执行文件时没有「卸载」可言——那份程序不在插件目录里，
        界面只给「改回内置」，不碰用户的文件。
        """
        if not confirm:
            raise ProducerError("需要确认。")
        if self.exe()[1] == "custom":
            raise ProducerError("当前使用你指定的可执行文件，插件不会删除它。")
        self.pool.stop_all()
        removed: list[str] = []
        for folder in (self.root / "bin", self.root / "config", self.root / "logs", self.root / "pids"):
            if not folder.exists():
                continue
            try:
                move_to_trash(folder)
                removed.append(folder.name)
            except TrashError as exc:
                raise ProducerError(str(exc)) from exc
        with self._lock:
            self._cfg["version"] = ""
            self._cfg["auto_start"] = []
            self._download = {"state": "idle", "progress": 0.0, "note": ""}
            self._write_state()
        self._installer.forget_backends()
        CHANNEL.info(f"audio.cpp 运行包已卸载：{', '.join(removed) or '无文件'}", source="audiocpp")
        return {"removed": removed}

    # ------------------------------------------------------------------ #
    # 实例进程
    # ------------------------------------------------------------------ #

    def _scan_root(self, conn: manager.Connection | None | object = _UNRESOLVED) -> Path | None:
        """扫「已装模型」的根目录：优先问服务自己，问不到才用配置的目录。

        服务开放管理时以它的 ``models_root`` 为准——插件起的实例在启动后已被指到
        配置目录，外部服务则读它自己的，两边都不会算错。
        """
        live = self.connection() if conn is _UNRESOLVED else conn
        if live is not None and live.management:
            with contextlib.suppress(ProducerError):
                root = live.models_root()
                if root:
                    return Path(root)
        if self.endpoint:
            return None
        return self.models_root()

    def _family_package(self, family: str, installed: set[str]) -> dict[str, Any]:
        row = catalog.family(family, self.root)
        found = [item for item in row.get("packages") or [] if item.get("id") in installed]
        found.sort(key=lambda item: not item.get("default"))
        return found[0] if found else {}

    def _config_for(self, item: Instance, installed: set[str]) -> Path:
        self.pool.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.pool.config_path(item.port)
        # 后端按这份可执行文件自报的能力收敛：内置 CPU 包选 cuda 会起不来。
        available = self._installer.backends(self.exe()[0])
        backend = str(self._cfg.get("backend") or "cpu")
        if available and backend not in available:
            backend = available[0]
        models: list[dict[str, Any]] = []
        package = self._family_package(item.family, installed) if item.family else {}
        folder = catalog.package_dir(self.models_root(), package) if package else None
        if folder is not None and folder.is_dir():
            family = catalog.family(item.family, self.root)
            models.append(
                {
                    "id": item.model,
                    "family": item.family,
                    "task": str((family.get("tasks") or ["tts"])[0]),
                    "mode": MODE_OVERRIDES.get(item.family, "offline"),
                    "path": str(folder),
                }
            )
        payload = {
            "host": "127.0.0.1",
            "port": item.port,
            "backend": backend,
            # 命令行 --ui-management 不生效，必须写进配置；否则空 models 会被拒绝。
            "ui": True,
            "ui_management": True,
            "lazy_load": True,
            "models": models,
        }
        # 这份文件给外部 exe 读：写坏一次它就起不来，走原子写。
        write_json(path, payload)
        return path

    def _spawn_env(self) -> dict[str, str]:
        env = dict(os.environ)
        mirror = _mirror_url(str(self._cfg.get("mirror") or ""))
        if mirror:
            env["AUDIOCPP_HF_BASE_URL"] = mirror
        else:
            env.pop("AUDIOCPP_HF_BASE_URL", None)
        return env

    def start(self, port: int | None = None) -> None:
        """拉起实例；端口上已有服务就不抢，交给界面标成外部实例。"""
        if self.endpoint:
            raise ProducerError("当前接入的是已有服务，无需启动实例。")
        exe, _source = self.exe()
        if exe is None:
            raise ProducerError("没有可用的可执行文件：下载运行包，或指定已有的 audio.cpp 程序。")
        targets = [item for item in self.instances() if port is None or item.port == port]
        if not targets:
            raise ProducerError("端口不在清单内。")
        installed = catalog.installed_packages(self.models_root(), self.root)
        failures: list[str] = []
        for item in targets:
            if self.pool.probe(item.port)[0]:
                continue  # 端口已有服务，不抢、不接管
            if self.pool.port_open(item.port):
                # 健康接口未就绪但端口已占用：可能还在加载，再起一份只会和它抢端口。
                failures.append(f":{item.port} 端口已被其他进程占用，未启动")
                continue
            config = self._config_for(item, installed)
            try:
                _process, handle = self.pool.spawn(exe, config, self._spawn_env())
            except OSError as exc:
                failures.append(f":{item.port} 启动失败：{exc}")
                continue
            if not self.pool.wait_ready(item.port, timeout=12.0):
                self.pool.stop(item.port)
                handle.close()
                tail = self.pool.tail(item.port, 400).strip().replace("\n", " ")[-220:]
                failures.append(f":{item.port} 未就绪{'：' + tail if tail else ''}")
                continue
            self._sync_models_root(item.port)
            CHANNEL.info(f"audio.cpp 实例已启动：{item.port}", source="audiocpp")
        if failures:
            raise ProducerError("；".join(failures))

    def _sync_models_root(self, port: int) -> None:
        """把实例的模型目录指到配置的目录，下载才不会落进别人的安装。"""
        conn = manager.Connection.open(url_of(port), 0.8)
        if conn is None or not conn.management:
            return
        with contextlib.suppress(ProducerError):
            current = conn.models_root()
            target = self.models_root()
            if Path(current or "") != target:
                conn.set_models_root(str(target))

    def stop(self, port: int | None = None) -> None:
        if port is not None:
            self.pool.stop(port)
            return
        self.pool.stop_all()

    def _auto_start(self) -> None:
        """只拉起「模型已装」的那些：勾选本身也只对它们开放，这里再挡一次。"""
        wanted = list(self._cfg.get("auto_start") or [])
        families = catalog.installed_families(self.models_root(), self.root)
        ports = {item.model: item for item in self.instances()}
        for model in wanted:
            item = ports.get(model)
            if item is None or (item.family and item.family not in families):
                continue
            try:
                self.start(item.port)
            except ProducerError as exc:
                CHANNEL.warning(f"audio.cpp 自动启动 :{item.port} 未执行：{exc}", source="audiocpp")

    # ------------------------------------------------------------------ #
    # 模型
    # ------------------------------------------------------------------ #

    def _management(self) -> manager.Connection:
        conn = self.connection()
        if conn is None:
            raise ProducerError("没有可用的 audio.cpp 服务，请先填写地址或启动实例。")
        if not conn.management:
            raise ProducerError("该服务未开放模型管理（启动时缺少 --ui-management）。")
        return conn

    def _start_for_install(self, family: str) -> None:
        """一台实例都没有时，为这次下载先拉一台空实例（带模型管理）。

        实例列表只列「模型已装」的行，所以装第一台模型时用户手上没有可点的启动按钮；
        安装这个动作自己把服务拉起来，装完它就能直接拿去做合成。
        """
        target = next((item for item in self.instances() if item.family == family), None)
        if target is None:
            raise ProducerError("这个模型的引擎还没配服务地址：先在引擎设置里填上地址。")
        CHANNEL.info(f"audio.cpp 安装前先拉起实例 :{target.port}", source="audiocpp")
        self.start(target.port)

    def _model_manager(self) -> Path | None:
        """官方 Python 下载器：服务端的安装接口不认 ``source``，分源只能走它。"""
        exe, _source = self.exe()
        if exe is None:
            return None
        path = exe.parent / _MANAGER_REL
        return path if path.is_file() else None

    def _model_manager_error(self) -> ProducerError:
        return ProducerError(
            "ModelScope 下载需要官方 tools/model_manager_v2.py，当前可执行文件旁边没有这份工具。"
            "切回 HF 镜像，或把可执行文件指向完整运行目录。"
        )

    def _model_specs_dir(self) -> Path:
        """把插件清单写成官方下载器认识的 spec 目录（它只读 family/packages 这些字段）。"""
        folder = self.root / "model_specs"
        folder.mkdir(parents=True, exist_ok=True)
        for row in catalog.families(self.root):
            family = str(row.get("family") or "")
            if not family:
                continue
            path = folder / f"{family}.json"
            path.write_text(json.dumps(row, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return folder

    def _model_cancel_path(self, package_id: str) -> Path:
        return self.root / "downloads" / f"{package_id}.cancel"

    def _manager_args(self, command: str, package_id: str) -> list[str]:
        manager = self._model_manager()
        if manager is None:
            raise self._model_manager_error()
        return [
            sys.executable,
            str(manager),
            "--specs-dir",
            str(self._model_specs_dir()),
            command,
            package_id,
            "--models-root",
            str(self.models_root()),
        ]

    def _run_manager(self, command: str, package_id: str) -> None:
        """同步跑一次管理器（卸载/清理）：失败时把它最后一句原话交给界面。"""
        manager = self._model_manager()
        if manager is None:
            raise self._model_manager_error()
        try:
            result = subprocess.run(
                self._manager_args(command, package_id),
                cwd=str(manager.parent.parent),
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProducerError(f"模型管理器执行失败：{exc}") from exc
        if result.returncode != 0:
            detail = [line.strip() for line in (result.stdout or result.stderr or "").splitlines() if line.strip()]
            raise ProducerError(detail[-1][:200] if detail else f"模型管理器退出码 {result.returncode}。")

    def _install_from_modelscope(self, package_id: str) -> None:
        manager = self._model_manager()
        if manager is None:
            raise self._model_manager_error()
        with self._lock:
            current = self._model_jobs.get(package_id)
            if current and current.get("state") in ("queued", "running", "cancelling"):
                raise ProducerError("这个模型正在下载，等它结束或先停止。")
            cancel = self._model_cancel_path(package_id)
            cancel.parent.mkdir(parents=True, exist_ok=True)
            cancel.unlink(missing_ok=True)
            self._model_jobs[package_id] = {
                "state": "queued",
                "progress_percent": 0.0,
                "message": "正在准备 ModelScope 下载",
                "downloaded_bytes": 0,
                "total_bytes": 0,
            }
        args = self._manager_args("install", package_id) + [
            "--source",
            "modelscope",
            "--source-repo",
            _MODELSCOPE_REPO,
            "--progress",
            "--cancel-file",
            str(cancel),
        ]
        try:
            process = subprocess.Popen(
                args,
                cwd=str(manager.parent.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_CREATE_NO_WINDOW,
                env=_download_env(),
            )
        except OSError as exc:
            with self._lock:
                self._model_jobs[package_id].update(state="failed", message=f"启动下载器失败：{exc}")
            raise ProducerError(f"启动下载器失败：{exc}") from exc
        with self._lock:
            self._model_procs[package_id] = process
            self._model_jobs[package_id].update(state="running", message="正在下载")
        threading.Thread(
            target=self._watch_model_install,
            args=(package_id, process, cancel),
            name=f"audiocpp-model-{package_id}",
            daemon=True,
        ).start()
        CHANNEL.info(f"audio.cpp 开始从 ModelScope 下载：{package_id}", source="audiocpp")

    def _watch_model_install(
        self, package_id: str, process: subprocess.Popen[str], cancel: Path
    ) -> None:
        last = ""
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    text = line.strip()
                    if not text:
                        continue
                    if text.startswith("AUDIOCPP_PROGRESS "):
                        fields: dict[str, str] = {}
                        for part in text.split()[1:]:
                            if "=" in part:
                                key, _, value = part.partition("=")
                                fields[key] = value
                        with contextlib.suppress(TypeError, ValueError):
                            downloaded = int(fields.get("downloaded") or 0)
                            total = int(fields.get("total") or 0)
                            with self._lock:
                                job = self._model_jobs.get(package_id)
                                if job is not None:
                                    job.update(
                                        downloaded_bytes=downloaded,
                                        total_bytes=total,
                                        progress_percent=round(downloaded * 100 / total, 1)
                                        if total
                                        else 0.0,
                                        message="正在下载",
                                    )
                        continue
                    last = text
                    with self._lock:
                        job = self._model_jobs.get(package_id)
                        if job is not None:
                            job["message"] = text[:200]
            code = process.wait()
            stopped = cancel.is_file()
            with self._lock:
                job = self._model_jobs.get(package_id)
                if job is not None:
                    if stopped:
                        job.update(state="cancelled", message="已停止")
                    elif code == 0:
                        job.update(
                            state="done",
                            progress_percent=100.0,
                            message="已安装",
                            downloaded_bytes=job.get("total_bytes") or job.get("downloaded_bytes") or 0,
                        )
                    else:
                        job.update(state="failed", message=last[:200] or f"下载器退出码 {code}")
            if stopped:
                CHANNEL.info(f"audio.cpp ModelScope 下载已停止：{package_id}", source="audiocpp")
            elif code == 0:
                CHANNEL.info(f"audio.cpp ModelScope 下载完成：{package_id}", source="audiocpp")
            else:
                CHANNEL.warning(
                    f"audio.cpp ModelScope 下载失败：{package_id}｜{last[:200]}", source="audiocpp"
                )
        except Exception as exc:  # noqa: BLE001 - 后台线程只记状态与日志
            with self._lock:
                job = self._model_jobs.get(package_id)
                if job is not None:
                    job.update(state="failed", message=str(exc)[:200])
            CHANNEL.warning(f"audio.cpp ModelScope 下载异常：{package_id}｜{exc}", source="audiocpp")
        finally:
            if process.stdout is not None:
                with contextlib.suppress(Exception):
                    process.stdout.close()
            cancel.unlink(missing_ok=True)
            with self._lock:
                self._model_procs.pop(package_id, None)

    def install_model(self, package_id: str) -> None:
        row, package = catalog.package(package_id, self.root)
        if not row:
            raise ProducerError("模型包不在官方清单内。")
        if _mirror_source(str(self._cfg.get("mirror") or "")) == "modelscope":
            # 服务端的安装接口不认 source：ModelScope 必须走官方 Python 管理器，
            # 不需要实例在跑，也不需要它开放模型管理。
            self._install_from_modelscope(str(package["id"]))
            return
        # 没有「能管模型的」实例就先起一台：模型没装时实例列表是空的，用户没有别的地方可点。
        # 端口上已有的服务不开管理（hub 拉起的那些）也按这句处理——起得来就用自己的，
        # 起不来（端口被占）会落到下面的 403 提示。
        conn = self.connection()
        if not self.endpoint and (conn is None or not conn.management):
            self._start_for_install(str(row.get("family") or ""))
            conn = self.connection()
        if conn is None:
            raise ProducerError("没有可用的 audio.cpp 服务，请先填写地址或启动实例。")
        if not conn.management:
            raise ProducerError("该服务未开放模型管理（启动时缺少 --ui-management）。")
        port = self._port_of(conn.url)
        # 只改自己拉起的实例：外部服务（含别人的 hub）模型目录归它自己，不代人设置。
        if port and self.pool.owned(port):
            self._sync_models_root(port)
        conn.install(str(package["id"]))
        CHANNEL.info(f"audio.cpp 开始下载模型：{package_id}", source="audiocpp")

    def _port_of(self, url: str) -> int:
        from urllib.parse import urlparse

        try:
            return int(urlparse(url).port or 0)
        except ValueError:
            return 0

    def stop_model(self, package_id: str) -> None:
        with self._lock:
            process = self._model_procs.get(package_id)
            job = self._model_jobs.get(package_id)
            active = job is not None and job.get("state") in ("queued", "running", "cancelling")
            if process is not None and active:
                job["state"] = "cancelling"
                job["message"] = "正在停止"
        if process is not None:
            cancel = self._model_cancel_path(package_id)
            with contextlib.suppress(OSError):
                cancel.parent.mkdir(parents=True, exist_ok=True)
                cancel.write_text("stop", encoding="utf-8")

            def kill() -> None:
                if process.poll() is None:
                    with contextlib.suppress(OSError):
                        process.terminate()

            timer = threading.Timer(5.0, kill)
            timer.daemon = True
            timer.start()
            return
        self._management().stop_install(package_id)

    def delete_model(self, package_id: str) -> None:
        with self._lock:
            job = self._model_jobs.get(package_id)
            if job is not None and job.get("state") in ("queued", "running", "cancelling"):
                raise ProducerError("这个模型正在下载，先停止再删除。")
        conn = self.connection()
        if conn is not None and conn.management:
            conn.delete_package(package_id)
            return
        # 没有服务在跑时，ModelScope 装下来的包用同一个官方管理器卸载；
        # 没有管理器就沿用原来的报错，不偷偷删文件。
        self._run_manager("uninstall", package_id)
        with self._lock:
            self._model_jobs.pop(package_id, None)
        CHANNEL.info(f"audio.cpp 模型已删除：{package_id}", source="audiocpp")

    def clean_model(self, package_id: str) -> None:
        with self._lock:
            job = self._model_jobs.get(package_id)
            if job is not None and job.get("state") in ("queued", "running", "cancelling"):
                raise ProducerError("下载还没停，先停止再清理。")
            local = job is not None
        if local:
            self._run_manager("clean-partial", package_id)
            self._model_cancel_path(package_id).unlink(missing_ok=True)
            with self._lock:
                self._model_jobs.pop(package_id, None)
            CHANNEL.info(f"audio.cpp 已清理未完成的下载：{package_id}", source="audiocpp")
            return
        self._management().clean_partial(package_id)

    def load_model(self, package_id: str) -> None:
        row, package = catalog.package(package_id, self.root)
        if not row:
            raise ProducerError("模型包不在官方清单内。")
        task = str((row.get("tasks") or [""])[0])
        self._management().load(str(package["id"]), str(row["family"]), task)

    def unload_model(self, model_id: str) -> None:
        self._management().unload(model_id)

    def refresh_catalog(self) -> dict[str, Any]:
        try:
            result = catalog.refresh(self.root)
        except OSError as exc:
            self._catalog_note = str(exc)
            raise ProducerError(str(exc)) from exc
        self._catalog_note = ""
        CHANNEL.info(f"audio.cpp 模型清单已刷新：{result['count']} 个模型", source="audiocpp")
        return result

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    def _job_rows(self, conn: manager.Connection | None) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        if conn is not None and conn.management:
            try:
                for row in conn.install_status():
                    if row.get("id"):
                        rows[str(row["id"])] = row
            except ProducerError:
                rows = {}
        # ModelScope 的任务在插件自己的下载器里，服务端不知道；同 id 时以本地为准。
        with self._lock:
            rows.update({package_id: dict(job) for package_id, job in self._model_jobs.items()})
        return rows

    @staticmethod
    def _job_view(row: dict[str, Any] | None) -> dict[str, Any]:
        if not row:
            return {}
        try:
            progress = float(row.get("progress_percent") or 0)
        except (TypeError, ValueError):
            progress = 0.0
        return {
            "state": str(row.get("state") or ""),
            "progress": max(0.0, min(100.0, progress)),
            "message": str(row.get("message") or ""),
            "downloaded": int(row.get("downloaded_bytes") or 0),
            "total": int(row.get("total_bytes") or 0),
        }

    def _models_view(
        self, installed: set[str], jobs: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for family in catalog.families(self.root):
            packages = []
            running: dict[str, Any] = {}
            done = False
            for item in family.get("packages") or []:
                if not isinstance(item, dict):
                    continue
                package_id = str(item.get("id") or "")
                job = self._job_view(jobs.get(package_id))
                is_done = package_id in installed
                done = done or is_done
                if job and job.get("state") in ("running", "queued", "cancelling") and not running:
                    running = job
                packages.append(
                    {
                        "id": package_id,
                        "name": str(item.get("display_name") or package_id),
                        "description": str(item.get("description") or ""),
                        "format": str(item.get("format") or ""),
                        "precision": str(item.get("precision") or ""),
                        "default": bool(item.get("default")),
                        "files": len(item.get("files") or []),
                        "installed": is_done,
                        "job": job,
                    }
                )
            category = str(family.get("category") or "other")
            status = str(family.get("status") or "supported")
            rows.append(
                {
                    "family": family.get("family"),
                    "name": str(family.get("name") or family.get("family")),
                    "description": str(family.get("description") or ""),
                    "summary": str(family.get("summary") or ""),
                    "category": category,
                    "category_label": catalog.CATEGORY_LABELS.get(category, category),
                    "status": status,
                    "status_label": catalog.STATUS_LABELS.get(status, status),
                    "tasks": [str(item) for item in family.get("tasks") or []],
                    "modes": [str(item) for item in family.get("modes") or []],
                    "languages": [str(item) for item in family.get("languages") or []],
                    "capabilities": [str(item) for item in family.get("capabilities") or []],
                    "tags": [str(item) for item in family.get("tags") or []],
                    "docs": [str(item) for item in family.get("docs") or []],
                    "packages": packages,
                    "installed": done,
                    "job": running,
                }
            )
        return rows

    def _instances_view(
        self, conn: manager.Connection | None, families: set[str]
    ) -> list[dict[str, Any]]:
        endpoint = self.endpoint
        if endpoint:
            alive = conn is not None
            return [
                {
                    "port": self._port_of(endpoint),
                    "model": "",
                    "label": "已接入的服务",
                    "family": "",
                    "engine": "",
                    "source": "endpoint",
                    "url": endpoint,
                    "category": "",
                    "category_label": "",
                    "running": alive,
                    "models": conn.loaded if conn else 0,
                    "owned": False,
                    "external": True,
                    "note": "" if alive else "无法连接",
                    "log": "",
                }
            ]
        rows: list[dict[str, Any]] = []
        items = self.instances()
        probes = self.pool.probe_many(item.port for item in items)
        for item in items:
            alive, count = probes.get(item.port, (False, 0))
            owned = self.pool.owned(item.port)
            family_row = catalog.family(item.family, self.root) if item.family else {}
            category = str(family_row.get("category") or "")
            # 引擎里配了地址 ≠ 这里有服务：模型没装的行不列（从前 6 行一直在，看着像写死的）。
            # 活着的服务必须显示——外部进程不是本插件装的，藏起来只会更糊涂。
            if not alive and item.family and item.family not in families:
                continue
            if alive and not owned:
                note = "外部进程"
            elif owned and not alive:
                note = "进程已退出"
            else:
                note = ""
            rows.append(
                {
                    "port": item.port,
                    "model": item.model,
                    "label": item.label,
                    "family": item.family,
                    "engine": item.engine,
                    "source": "auto" if item.auto else "engine" if item.engine else "manual",
                    "category": category,
                    "category_label": catalog.CATEGORY_LABELS.get(category, category),
                    "url": url_of(item.port),
                    "running": alive,
                    "models": count,
                    # 这台能不能被插件拉起：模型在盘上才算（自动拉起的候选就是它们）。
                    "ready": bool(item.family and item.family in families),
                    "owned": owned,
                    "external": alive and not owned,
                    "note": note,
                    "log": self.pool.tail(item.port),
                }
            )
        return rows

    def running_models(self) -> list[dict[str, Any]]:
        """正在运行的实例注册的模型：``[{id, family, task, path, package, utility, name, url}]``。

        音色处理按这份清单找模型与地址：只认 ``/v1/models`` 里真有的——
        装了没启动、或实例没加载那个包，都不该出现在可选列表里。
        """
        if self.endpoint:
            urls = [self.endpoint]
        else:
            items = self.instances()
            probes = self.pool.probe_many(item.port for item in items)
            urls = [
                url_of(item.port)
                for item in items
                if probes.get(item.port, (False, 0))[0]
            ]
        found: dict[str, dict[str, Any]] = {}
        for url in urls:
            conn = manager.Connection.open(url, 0.8)
            if conn is None:
                continue
            try:
                rows = conn.models()
            except ProducerError:
                continue
            for row in rows:
                model_id = str(row.get("id") or "")
                if not model_id or model_id in found:
                    continue
                load = row.get("load_options") if isinstance(row.get("load_options"), dict) else {}
                found[model_id] = {
                    "id": model_id,
                    "family": str(row.get("family") or ""),
                    "task": str(row.get("task") or ""),
                    "path": str(row.get("path") or ""),
                    "package": str(row.get("package_id") or ""),
                    # 内置工具没有 package：能带出 load_options.utility 就带，带不出由
                    # 消费方按 id / path 认，界面才知道是 DeepFilterNet2 还是 RNNoise。
                    "utility": str(load.get("utility") or row.get("utility") or ""),
                    "name": str(row.get("name") or row.get("display_name") or ""),
                    "url": url,
                }
        return list(found.values())

    # ------------------------------------------------------------------ #
    # 给消费方（音色处理）的模型准备接口
    # ------------------------------------------------------------------ #

    def installed_packages(self) -> set[str]:
        """模型目录里已经装好的包 id；只按官方声明的落点存在与否判断。

        接入外部服务时以它自报的 ``models_root`` 为准（见 :meth:`_scan_root`），
        不能拿本机配置目录去扫别人的模型。
        """
        root = self._scan_root()
        if root is None:
            return set()
        return catalog.installed_packages(root, self.root)

    def download_jobs(self) -> dict[str, dict[str, Any]]:
        """按包 id 返回安装进度；服务没起时也能拿到 ModelScope 的后台任务。"""
        conn = self.connection()
        return {
            package_id: self._job_view(row)
            for package_id, row in self._job_rows(conn).items()
        }

    def ensure_family_instance(self, family: str, label: str = "") -> Instance:
        """保证这个模型族有一行可启动的实例；没有就补一行手动实例。

        安装模型前需要一台能开模型管理的服务，但新用户可能既没配引擎、也没装过
        这个族——不能要求他先去 audio.cpp 页手加一行。
        """
        rows = [item for item in self.instances() if item.family == family]
        if rows:
            return rows[0]
        with self._lock:
            saved = [
                dict(item)
                for item in (self._cfg.get("instances") or [])
                if isinstance(item, dict)
            ]
            if len(saved) >= 8:
                raise ProducerError("手动实例已经有 8 条，先到 audio.cpp 页删一条。")
            used = {item.port for item in self.instances()}
            port = self._free_port(used)
            if port is None:
                raise ProducerError("没有空闲端口分配给这个模型。")
            saved.append(
                {
                    "port": port,
                    "model": family,
                    "label": label or family,
                    "family": family,
                }
            )
            self._cfg["instances"] = saved
            self._write_state()
        return Instance(port, family, label or family, family)

    def ensure_management(self) -> manager.Connection:
        """拿到一台开放模型管理的服务；没有就按现有实例拉一台。

        只在这里替用户启动：下载、加载与处理都需要它。填了外部地址时不另起进程，
        外部服务没开管理就把原因说清楚。
        """
        conn = self.connection()
        if conn is not None and conn.management:
            return conn
        if self.endpoint:
            if conn is None:
                raise ProducerError(f"接入的 audio.cpp 服务连不上（{self.endpoint}）。")
            raise ProducerError("接入的 audio.cpp 服务没有开放模型管理，不能在这里加载模型。")
        if self.exe()[0] is None:
            raise ProducerError(
                "没有可用的 audio.cpp 运行包：先到 audio.cpp 页下载运行包，或指定已有的程序。"
            )
        for item in self.instances():
            if self.pool.probe(item.port)[0]:
                continue
            try:
                self.start(item.port)
            except ProducerError:
                continue
            conn = self.connection()
            if conn is not None and conn.management:
                return conn
        # 现有实例都不可用：起一台不带模型的专用服务，只做模型管理。
        with self._lock:
            saved = [
                dict(item)
                for item in (self._cfg.get("instances") or [])
                if isinstance(item, dict)
            ]
            if len(saved) >= 8:
                raise ProducerError("手动实例已经有 8 条，先到 audio.cpp 页删一条。")
            used = {item.port for item in self.instances()}
            port = self._free_port(used)
            if port is None:
                raise ProducerError("没有空闲端口启动 audio.cpp 服务。")
            saved.append(
                {
                    "port": port,
                    "model": "audio-tools",
                    "label": "音频处理",
                    "family": "",
                }
            )
            self._cfg["instances"] = saved
            self._write_state()
        self.start(port)
        conn = self.connection()
        if conn is not None and conn.management:
            return conn
        raise ProducerError("audio.cpp 服务没有开放模型管理，无法加载模型。")

    def load_builtin(self, utility: str, path: Path) -> None:
        """把内置工具的权重目录登记进运行中的实例。

        这类工具没有包 id：server 的 ``/v1/models/load`` 走 ``path`` 与
        ``load_options.utility``，不需要重启实例。
        """
        if not path.is_dir():
            raise ProducerError("内置工具的权重目录不存在，先下载。")
        conn = self.ensure_management()
        conn.load_path(
            f"builtin-{utility}",
            str(path),
            "builtin_audio_utils",
            "s2s",
            {"utility": utility},
        )

    def state(self) -> dict[str, Any]:
        self.pool.reap()
        conn = self.connection()
        scan_root = self._scan_root(conn)
        installed = catalog.installed_packages(scan_root, self.root)
        families = catalog.installed_families(scan_root, self.root)
        jobs = self._job_rows(conn)
        loaded: list[dict[str, Any]] = []
        if conn is not None:
            with contextlib.suppress(ProducerError):
                loaded = [
                    {
                        "id": str(row.get("id") or ""),
                        "family": str(row.get("family") or ""),
                        "task": str(row.get("task") or ""),
                        "loaded": bool(row.get("loaded")),
                    }
                    for row in conn.models()
                ]
        models_root = str(scan_root) if scan_root is not None else ""
        exe, source = self.exe()
        builtin_exe = self._installer.exe()
        available = self._installer.backends(exe)
        backend = str(self._cfg.get("backend") or "cpu")
        backend_note = (
            f"这份可执行文件只支持 {', '.join(available)}，已回落 {available[0]}。"
            if exe is not None and available and backend not in available
            else ""
        )
        version = str(self._cfg.get("version") or "")
        need = runtimes.minimum()
        outdated = bool(
            source == "builtin"
            and version
            and need
            and runtimes.version_tuple(version) < runtimes.version_tuple(need)
        )
        service = (
            {
                "url": conn.url,
                "reachable": True,
                "version": conn.version,
                "backend": conn.backend,
                "management": conn.management,
                "loaded": conn.loaded,
                "external": bool(self.endpoint),
            }
            if conn is not None
            else {
                "url": self.endpoint,
                "reachable": False,
                "version": "",
                "backend": "",
                "management": False,
                "loaded": 0,
                "external": bool(self.endpoint),
            }
        )
        with self._lock:
            saved_auto = list(self._cfg.get("auto_start") or [])
            download = dict(self._download)
        mirror = str(self._cfg.get("mirror") or "")
        model_manager = self._model_manager()
        # 自动拉起只认已装模型的实例：清单里没有的东西不该出现在这里（从前是全部平铺 + 灰掉）。
        ready = {item.model for item in self.instances() if item.family in families}
        auto_start = [model for model in saved_auto if model in ready]
        server_install = bool(conn is not None and conn.management) or (
            exe is not None and not self.endpoint and not outdated
        )
        # ModelScope 只走官方 Python 管理器；它不认服务端的安装接口，所以不能用上面那条兜底。
        if _mirror_source(mirror) == "modelscope":
            can_install = model_manager is not None and not self.endpoint
        else:
            can_install = server_install
        return {
            "endpoint": self.endpoint,
            "service": service,
            "runtime": {
                "installed": exe is not None,
                "exe": str(exe) if exe else "",
                "source": source,
                "builtin_exe": str(builtin_exe) if builtin_exe else "",
                "custom_exe": str(self._cfg.get("exe") or ""),
                "model_manager": str(model_manager) if model_manager else "",
                "models_root": str(self.models_root()),
                "models_root_custom": bool(self._cfg.get("models_root")),
                "version": version,
                "catalog_version": runtimes.version(),
                "minimum_version": need,
                "outdated": outdated,
                "backend": backend,
                "backends": available,
                "note": backend_note,
                "packages": [
                    {
                        "id": str(row.get("id") or ""),
                        "label": str(row.get("label") or row.get("id") or ""),
                        "backend": str(row.get("backend") or ""),
                        "size": int(row.get("size") or 0),
                        "experimental": bool(row.get("experimental")),
                    }
                    for row in runtimes.rows()
                ],
                "runtime_id": str(self._cfg.get("runtime_id") or ""),
                "download": download,
            },
            "instances": self._instances_view(conn, families),
            "auto_start": auto_start,
            "models": self._models_view(installed, jobs),
            "loaded": loaded,
            "categories": catalog.categories(self.root),
            "catalog": {**catalog.source_note(self.root), "note": self._catalog_note},
            "mirror": mirror,
            "mirrors": [
                {
                    **row,
                    "available": row.get("source") != "modelscope" or model_manager is not None,
                }
                for row in MIRRORS
            ],
            "models_root": models_root,
            "can_start": exe is not None and not self.endpoint and not outdated,
            "can_manage": bool(conn is not None and conn.management),
            # 一台实例都没有也能装：安装动作会先按引擎地址拉起一台空实例。
            "can_install": can_install,
            "homepage": "https://github.com/0xShug0/audio.cpp",
            "root": str(self.root),
        }
