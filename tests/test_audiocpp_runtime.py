"""audio.cpp 插件：接入方式、清单、进程与官方管理接口的转发。"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.producers.base import ProducerError
from app.producers.builtin.audiocpp import catalog, manager
from app.producers.builtin.audiocpp import runtime as rt
from app.producers.builtin.audiocpp.plugin import AudiocppRuntime


class _FakeConn:
    """一台假服务：按需声明管理能力，并记录收到的命令。"""

    def __init__(self, url: str = "http://127.0.0.1:18092", management: bool = True) -> None:
        self.url = url
        self.info = {"status": "ok", "version": "0.9.4", "backend": "cuda", "models": 1}
        self.info["ui_management"] = management
        self.calls: list[tuple[str, Any]] = []

    @property
    def version(self) -> str:
        return str(self.info.get("version") or "")

    @property
    def backend(self) -> str:
        return str(self.info.get("backend") or "")

    @property
    def management(self) -> bool:
        return bool(self.info.get("ui_management"))

    @property
    def loaded(self) -> int:
        return int(self.info.get("models") or 0)

    def models_root(self) -> str:
        return ""

    def models(self) -> list[dict[str, Any]]:
        return [{"id": "cosyvoice3", "family": "cosyvoice3", "task": "tts", "loaded": True}]

    def set_models_root(self, path: str) -> str:
        self.calls.append(("models-root", path))
        return path

    def install(self, package_id: str) -> dict[str, Any]:
        self.calls.append(("install", package_id))
        return {}

    def install_status(self) -> list[dict[str, Any]]:
        return []

    def stop_install(self, package_id: str) -> dict[str, Any]:
        self.calls.append(("stop", package_id))
        return {}

    def delete_package(self, package_id: str) -> dict[str, Any]:
        self.calls.append(("delete", package_id))
        return {}

    def clean_partial(self, package_id: str) -> dict[str, Any]:
        self.calls.append(("clean", package_id))
        return {}

    def load(self, package_id: str, family: str, task: str) -> dict[str, Any]:
        self.calls.append(("load", (package_id, family, task)))
        return {}

    def unload(self, model_id: str) -> dict[str, Any]:
        self.calls.append(("unload", model_id))
        return {}


@pytest.fixture()
def service(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> rt.RuntimeService:
    """插件的实例状态机：数据目录隔离，端口探测全部打桩。

    ``data_dir`` 必须带——实例清单现在取自引擎设置，读的正是这份配置。
    """
    _ = data_dir
    monkeypatch.setattr(manager, "health", lambda url, timeout=1.0: {})
    monkeypatch.setattr(manager.Connection, "open", classmethod(lambda cls, url, timeout=1.0: None))
    monkeypatch.setattr(rt.ProcessPool, "probe", lambda self, port: (False, 0))
    monkeypatch.setattr(rt.ProcessPool, "port_open", staticmethod(lambda port, timeout=0.15: False))
    return rt.RuntimeService(tmp_path / "audiocpp", autostart=False)


def _enable_engine(key: str, **settings: Any) -> None:
    """把一台引擎写进（隔离的）config.json 并启用它。"""
    from app.infra.config import load_config, save_config

    cfg = load_config()
    cfg.engines[key] = dict(settings)
    cfg.engines_enabled = sorted(set(cfg.engines_enabled or []) | {key})
    save_config(cfg)


@pytest.fixture()
def fake_conn(monkeypatch: pytest.MonkeyPatch) -> _FakeConn:
    conn = _FakeConn()
    monkeypatch.setattr(manager.Connection, "open", classmethod(lambda cls, url, timeout=1.0: conn))
    return conn


def _fake_exe(service: rt.RuntimeService) -> None:
    exe = service.root / "bin" / "audiocpp_server.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"exe")
    service._installer.forget_backends()


def _materialize(models_root: Path, family: str) -> dict[str, Any]:
    """按官方落点造一份"已装"的模型，返回它的包描述。"""
    package = catalog.family(family)["packages"][0]
    for path in catalog.package_files(models_root, package):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"gguf")
    return package


def test_instances_come_from_engine_settings(service: rt.RuntimeService) -> None:
    """端口与模型 id 只有一个产地：引擎「服务地址」那一栏。

    从前这里抄了一份写死的端口表，于是「breeze 在 8080」和用户实际配的 18090
    各说各话；现在改引擎地址，实例行立刻跟着走。
    """
    _enable_engine("breeze_tts", base_url="http://127.0.0.1:19001", model="breeze-tts-x")
    rows = {row.port: row for row in service.instances()}
    assert 19001 in rows
    assert rows[19001].model == "breeze-tts-x"
    assert rows[19001].family == "breeze_tts"
    assert rows[19001].engine == "breeze_tts"
    assert 8080 not in rows  # 默认表现在不参与了


def test_instances_fall_back_to_builtin_defaults(service: rt.RuntimeService) -> None:
    """一台引擎都没启用时用内置默认地址：否则既没有实例、也没法装模型。

    默认值本身撞车（breeze 与 voxcpm2 都是 8080）：一个端口只留一台。
    """
    ports = [row.port for row in service.instances()]
    assert len(ports) == len(set(ports))
    assert {8080, 18090, 18091, 18092, 18093, 18999} <= set(ports)


def test_port_edit_writes_engine_address(service: rt.RuntimeService) -> None:
    """改端口 = 改引擎的服务地址：两边不会一个 19002、一个还连 19001。"""
    from app.engines.registry import detect_engine
    from app.usecase.engines import settings_for

    _enable_engine("breeze_tts", base_url="http://127.0.0.1:19001", model="breeze-tts-x")
    service.set_port(19001, 19002)
    ports = {row.port for row in service.instances()}
    assert 19002 in ports
    assert 19001 not in ports
    engine = detect_engine("breeze_tts")
    assert engine is not None
    assert settings_for(engine).base_url == "http://127.0.0.1:19002"


def test_manual_instance_cannot_take_engine_port(service: rt.RuntimeService) -> None:
    with pytest.raises(ProducerError, match="引擎设置"):
        service.settings(instances=[{"port": 18092, "model": "whatever"}])


def test_plugin_is_workspace_only() -> None:
    plugin = AudiocppRuntime()
    assert plugin.key == "audiocpp"
    assert plugin.steps() == ()
    assert plugin.settings_schema() is None
    assert plugin.describe()["icon_url"] == "/api/v1/producers/audiocpp/icon"
    assert plugin.describe()["voice_rail"] is False
    # 运行时管理是工具：导航与引擎、日志同组，不进日常页。
    assert plugin.describe()["nav_section"] == "tools"


def test_state_has_no_host_config(service: rt.RuntimeService) -> None:
    """配置只落插件目录，不写宿主 config.json。"""
    service.settings(backend="cuda")
    saved = json.loads(service.state_path.read_text(encoding="utf-8"))
    assert saved["backend"] == "cuda"
    state = service.state()
    assert state["runtime"]["installed"] is False
    assert state["root"] == str(service.root)


def test_migration_drops_foreign_exe_and_installed_list(
    tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧版把别人的 exe 当「已安装」：v4 起一律作废，只认插件 bin 里的。"""
    _ = data_dir
    monkeypatch.setattr(manager, "health", lambda url, timeout=1.0: {})
    monkeypatch.setattr(manager.Connection, "open", classmethod(lambda cls, url, timeout=1.0: None))
    monkeypatch.setattr(rt.ProcessPool, "probe", lambda self, port: (False, 0))
    root = tmp_path / "audiocpp"
    root.mkdir(parents=True)
    (root / "state.json").write_text(
        json.dumps(
            {
                "schema": 3,
                "exe": "D:/someone-else/audiocpp_server.exe",
                "installed": ["sensevoice_small_q8"],
                "download_source": "hf-mirror",
                "backend": "cuda",
                "instances": [{"port": 8080, "model": "breeze-tts-2", "label": "BreezeTTS 2"}],
            }
        ),
        encoding="utf-8",
    )
    service = rt.RuntimeService(root, autostart=False)
    state = service.state()
    assert state["runtime"]["installed"] is False
    assert state["runtime"]["exe"] == ""
    assert json.loads((root / "state.json").read_text(encoding="utf-8"))["schema"] == rt._SCHEMA
    assert service.state_path.exists()
    # v5：实例表不再存写死的端口表，旧行清空；界面上的行从引擎设置现算。
    assert json.loads((root / "state.json").read_text(encoding="utf-8"))["instances"] == []
    assert state["mirror"] == "hf-mirror"


def test_catalog_comes_from_official_specs() -> None:
    families = {row["family"]: row for row in catalog.families()}
    for key in ("breeze_tts", "cosyvoice3", "index_tts2", "sense_asr", "qwen3_asr"):
        assert key in families
        assert families[key]["description"].strip()
        assert families[key]["packages"]
    assert catalog.CATEGORY_LABELS["tts"] == "语音合成"


def test_installed_scan_uses_declared_layout(tmp_path: Path) -> None:
    package = catalog.family("sense_asr")["packages"][0]
    assert "sensevoice_small_q8" not in catalog.installed_packages(tmp_path)
    _materialize(tmp_path, "sense_asr")
    assert "sensevoice_small_q8" in catalog.installed_packages(tmp_path)
    assert "sense_asr" in catalog.installed_families(tmp_path)
    assert catalog.package_dir(tmp_path, package) == tmp_path / str(package["target_directory"])


def test_state_lists_every_family_with_status(service: rt.RuntimeService) -> None:
    state = service.state()
    rows = {row["family"]: row for row in state["models"]}
    assert len(rows) == len(catalog.families())
    assert rows["sense_asr"]["installed"] is False
    assert rows["sense_asr"]["category_label"] == "语音识别"
    assert rows["sense_asr"]["packages"][0]["precision"]


def test_install_forwards_to_official_api(
    service: rt.RuntimeService, fake_conn: _FakeConn
) -> None:
    service.settings(endpoint="127.0.0.1:18092")
    service.install_model("sensevoice_small_q8")
    assert fake_conn.calls == [("install", "sensevoice_small_q8")]


def test_install_rejects_unknown_package(
    service: rt.RuntimeService, fake_conn: _FakeConn
) -> None:
    service.settings(endpoint="127.0.0.1:18092")
    with pytest.raises(ProducerError, match="不在官方清单"):
        service.install_model("not_a_package")


def test_external_without_management_is_read_only(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn(management=False)
    monkeypatch.setattr(manager.Connection, "open", classmethod(lambda cls, url, timeout=1.0: conn))
    service.settings(endpoint="127.0.0.1:18092")
    state = service.state()
    assert state["can_manage"] is False
    assert state["models"][0]["installed"] is False
    with pytest.raises(ProducerError, match="未开放模型管理"):
        service.install_model("sensevoice_small_q8")


def test_endpoint_disables_local_instances(
    service: rt.RuntimeService, fake_conn: _FakeConn
) -> None:
    service.settings(endpoint="127.0.0.1:18092")
    state = service.state()
    assert state["can_start"] is False
    assert [row["url"] for row in state["instances"]] == ["http://127.0.0.1:18092"]
    assert state["instances"][0]["external"] is True
    with pytest.raises(ProducerError, match="无需启动"):
        service.start()


def test_endpoint_normalizes_address(service: rt.RuntimeService) -> None:
    service.settings(endpoint="  127.0.0.1:18092/  ")
    assert service.endpoint == "http://127.0.0.1:18092"
    service.settings(endpoint="")
    assert service.endpoint == ""


def test_start_requires_runtime(service: rt.RuntimeService) -> None:
    with pytest.raises(ProducerError, match="没有可用的可执行文件"):
        service.start()


def test_start_does_not_spawn_on_a_live_but_unready_port(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口已 listen、健康接口还没就绪：绝不能趁这个窗口再起第二份。"""
    _fake_exe(service)
    monkeypatch.setattr(rt.ProcessPool, "port_open", staticmethod(lambda port, timeout=0.15: True))
    monkeypatch.setattr(
        rt.ProcessPool,
        "spawn",
        lambda self, exe, config, env=None: pytest.fail("端口已占用时不应 spawn"),
    )
    with pytest.raises(ProducerError, match="端口已被"):
        service.start(8080)


def test_custom_exe_is_used_instead_of_builtin(
    service: rt.RuntimeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户指了自己那份 audio.cpp：优先用它拉起实例，不再要求下载运行包。"""
    exe = tmp_path / "elsewhere" / "audiocpp_server.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"exe")
    service.settings(exe=str(exe))
    state = service.state()
    assert state["runtime"]["source"] == "custom"
    assert state["runtime"]["exe"] == str(exe)
    assert state["can_start"] is True

    spawned: list[Path] = []

    def spawn(self: Any, exe_path: Path, config: Path, env: Any = None) -> Any:
        spawned.append(exe_path)
        return _Proc(), io.BytesIO()

    monkeypatch.setattr(rt.ProcessPool, "spawn", spawn)
    monkeypatch.setattr(rt.ProcessPool, "wait_ready", lambda self, port, timeout=10.0: True)
    monkeypatch.setattr(rt.ProcessPool, "owned", lambda self, port: True)
    service.start(8080)
    assert spawned == [exe]


def test_settings_rejects_missing_exe(service: rt.RuntimeService) -> None:
    with pytest.raises(ProducerError, match="可执行文件不存在"):
        service.settings(exe="D:/nowhere/audiocpp_server.exe")


def test_uninstall_leaves_custom_exe_alone(
    service: rt.RuntimeService, tmp_path: Path
) -> None:
    """自定义程序不在插件目录里，卸载没有意义，也不该碰用户的文件。"""
    exe = tmp_path / "mine" / "audiocpp_server.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"exe")
    service.settings(exe=str(exe))
    with pytest.raises(ProducerError, match="不会删除"):
        service.uninstall(True)
    assert exe.is_file()


def test_custom_models_root_is_used(
    service: rt.RuntimeService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型目录可指向已有的模型库：已装判定与实例配置都跟着它走。"""
    _fake_exe(service)
    library = tmp_path / "library"
    package = _materialize(library, "breeze_tts")
    service.settings(models_root=str(library))
    assert service.state()["runtime"]["models_root_custom"] is True
    monkeypatch.setattr(rt.ProcessPool, "spawn", lambda self, exe, config, env=None: (_Proc(), io.BytesIO()))
    monkeypatch.setattr(rt.ProcessPool, "wait_ready", lambda self, port, timeout=10.0: True)
    monkeypatch.setattr(rt.ProcessPool, "owned", lambda self, port: True)
    service.start(8080)
    payload = json.loads(service.pool.config_path(8080).read_text(encoding="utf-8"))
    assert payload["models"][0]["path"] == str(catalog.package_dir(library, package))


def test_start_allows_empty_models(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没下模型也能起实例：模型安装要用运行中的实例，否则两条规则互锁。"""
    _fake_exe(service)
    monkeypatch.setattr(rt.ProcessPool, "spawn", lambda self, exe, config, env=None: (_Proc(), io.BytesIO()))
    monkeypatch.setattr(rt.ProcessPool, "wait_ready", lambda self, port, timeout=10.0: True)
    monkeypatch.setattr(rt.ProcessPool, "owned", lambda self, port: True)
    service.start(8080)
    payload = json.loads(service.pool.config_path(8080).read_text(encoding="utf-8"))
    assert payload["ui_management"] is True
    assert payload["models"] == []
    assert payload["backend"] in rt.runtimes.BACKENDS


def test_config_includes_installed_model(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已装模型写进实例配置：id 用引擎的模型名，family 用官方族名。"""
    _fake_exe(service)
    package = _materialize(service.models_dir, "breeze_tts")
    monkeypatch.setattr(rt.ProcessPool, "spawn", lambda self, exe, config, env=None: (_Proc(), io.BytesIO()))
    monkeypatch.setattr(rt.ProcessPool, "wait_ready", lambda self, port, timeout=10.0: True)
    monkeypatch.setattr(rt.ProcessPool, "owned", lambda self, port: True)
    service.start(8080)
    payload = json.loads(service.pool.config_path(8080).read_text(encoding="utf-8"))
    assert payload["models"][0]["id"] == "breeze-tts-2"
    assert payload["models"][0]["family"] == "breeze_tts"
    assert payload["models"][0]["path"] == str(catalog.package_dir(service.models_dir, package))


def test_sense_asr_starts_in_streaming_mode(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SenseVoice 的 offline 路径要 Silero VAD，内置运行包不带；实例用 streaming。"""
    _fake_exe(service)
    _materialize(service.models_dir, "sense_asr")
    monkeypatch.setattr(rt.ProcessPool, "spawn", lambda self, exe, config, env=None: (_Proc(), io.BytesIO()))
    monkeypatch.setattr(rt.ProcessPool, "wait_ready", lambda self, port, timeout=10.0: True)
    monkeypatch.setattr(rt.ProcessPool, "owned", lambda self, port: True)
    service.start(18102)
    payload = json.loads(service.pool.config_path(18102).read_text(encoding="utf-8"))
    assert payload["models"][0]["id"] == "sense-asr"
    assert payload["models"][0]["mode"] == "streaming"


def test_failed_start_leaves_no_pid_marker(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """起不来不能留下 pid，否则界面会显示「可停止」但进程已死。"""
    _fake_exe(service)
    monkeypatch.setattr(rt.ProcessPool, "spawn", lambda self, exe, config, env=None: (_Proc(-1), io.BytesIO()))
    monkeypatch.setattr(rt.ProcessPool, "wait_ready", lambda self, port, timeout=10.0: False)
    service.pool.tail = lambda port, limit=1600: "boom"  # type: ignore[method-assign]
    with pytest.raises(ProducerError, match="未就绪"):
        service.start(8080)
    assert not (service.pool.pid_dir / "8080.pid").exists()


def test_auto_start_requires_installed_family(service: rt.RuntimeService) -> None:
    service.settings(auto_start=["breeze-tts-2"])
    assert service.state()["auto_start"] == []
    _materialize(service.models_dir, "breeze_tts")
    service.settings(auto_start=["breeze-tts-2", "ghost"])
    assert service.state()["auto_start"] == ["breeze-tts-2"]


def test_spawn_env_follows_mirror(service: rt.RuntimeService) -> None:
    service.settings(mirror="hf-mirror")
    assert service._spawn_env()["AUDIOCPP_HF_BASE_URL"] == "https://hf-mirror.com"
    service.settings(mirror="direct")
    assert "AUDIOCPP_HF_BASE_URL" not in service._spawn_env()
    service.settings(mirror="modelscope")
    assert "AUDIOCPP_HF_BASE_URL" not in service._spawn_env()


_FAKE_MANAGER = '''
import pathlib
import sys

args = sys.argv[1:]
root = pathlib.Path(args[args.index("--models-root") + 1])
print("selected pulsevad_2_1k_f32 (pulsevad)")
print("AUDIOCPP_PROGRESS downloaded=0 total=100", flush=True)
target = root / "PulseVAD-GGUF"
target.mkdir(parents=True, exist_ok=True)
(target / "pulsevad-2.1k-f32.gguf").write_bytes(b"gguf")
print("AUDIOCPP_PROGRESS downloaded=100 total=100", flush=True)
print("installed pulsevad_2_1k_f32")
'''


def _fake_manager(service: rt.RuntimeService) -> Path:
    path = service.root / "bin" / "tools" / "model_manager_v2.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_FAKE_MANAGER, encoding="utf-8")
    return path


def test_modelscope_installs_through_the_official_python_manager(
    service: rt.RuntimeService,
) -> None:
    """服务端安装接口不认 source：ModelScope 只能交给官方 Python 管理器。"""
    _fake_exe(service)
    manager = _fake_manager(service)
    service.settings(mirror="modelscope")
    state = service.state()
    assert state["can_install"] is True
    assert state["runtime"]["model_manager"] == str(manager)
    assert next(row for row in state["mirrors"] if row["id"] == "modelscope")["available"] is True

    service.install_model("pulsevad_2_1k_f32")
    row = {}
    for _ in range(100):
        family = next(item for item in service.state()["models"] if item["family"] == "pulsevad")
        row = next(item for item in family["packages"] if item["id"] == "pulsevad_2_1k_f32")
        if row["installed"]:
            break
        time.sleep(0.05)
    assert row["installed"]
    assert (service.models_dir / "PulseVAD-GGUF" / "pulsevad-2.1k-f32.gguf").is_file()
    assert (service.root / "model_specs" / "pulsevad.json").is_file()


def test_modelscope_requires_the_python_manager(service: rt.RuntimeService) -> None:
    _fake_exe(service)
    service.settings(mirror="modelscope")
    state = service.state()
    assert state["can_install"] is False
    assert next(row for row in state["mirrors"] if row["id"] == "modelscope")["available"] is False
    with pytest.raises(ProducerError, match="model_manager_v2.py"):
        service.install_model("pulsevad_2_1k_f32")


def test_mirror_rejects_unknown(service: rt.RuntimeService) -> None:
    with pytest.raises(ProducerError, match="下载镜像"):
        service.settings(mirror="nope")


def test_settings_rejects_unknown_backend(service: rt.RuntimeService) -> None:
    with pytest.raises(ProducerError, match="后端不在清单内"):
        service.settings(backend="tpu")


def test_scanned_models_get_cards_without_engine_settings(service: rt.RuntimeService) -> None:
    """扫描到的模型不靠引擎也要有卡片：插件是挂载点，不是引擎的影子。"""
    _materialize(service.models_dir, "canary_asr")
    rows = [row for row in service.instances() if row.family == "canary_asr"]
    assert len(rows) == 1
    assert rows[0].auto
    assert rows[0].model == "canary_asr"
    assert rows[0].label == "Canary 180M Flash"
    assert rows[0].port >= 18100
    view = next(row for row in service.state()["instances"] if row["family"] == "canary_asr")
    assert view["source"] == "auto"
    assert view["category"] == "asr"
    assert view["ready"] is True
    assert view["port"] == rows[0].port
    assert [row.port for row in service.instances() if row.family == "canary_asr"] == [rows[0].port]


def test_auto_instance_port_edit_is_saved(service: rt.RuntimeService) -> None:
    """扫描行的端口也能改，并且下次读状态仍是这个端口。"""
    _materialize(service.models_dir, "canary_asr")
    row = next(item for item in service.instances() if item.family == "canary_asr")
    service.set_port(row.port, 19010)
    moved = next(item for item in service.instances() if item.family == "canary_asr")
    assert moved.port == 19010
    saved = json.loads(service.state_path.read_text(encoding="utf-8"))
    assert saved["auto_ports"]["canary_asr"] == 19010


def test_instances_are_configurable(service: rt.RuntimeService) -> None:
    service.settings(
        instances=[
            {"port": 9001, "model": "breeze-tts-2", "label": "BreezeTTS 2", "family": "breeze_tts"},
            {"port": 9002, "model": "dots_tts", "label": "dots.tts", "family": "dots_tts"},
        ]
    )
    ports = [item.port for item in service.instances()]
    assert 9001 in ports and 9002 in ports
    assert ports == sorted(ports)
    saved = json.loads(service.state_path.read_text(encoding="utf-8"))
    assert saved["instances"][0]["port"] == 9001
    assert saved["instances"][0]["family"] == "breeze_tts"


def test_instances_reject_bad_ports(service: rt.RuntimeService) -> None:
    with pytest.raises(ProducerError, match="重复"):
        service.settings(
            instances=[{"port": 9001, "model": "a"}, {"port": 9001, "model": "b"}]
        )
    with pytest.raises(ProducerError, match="范围"):
        service.settings(instances=[{"port": 70000, "model": "a"}])


def test_uninstall_requires_confirm(service: rt.RuntimeService) -> None:
    with pytest.raises(ProducerError, match="需要确认"):
        service.uninstall(False)


def test_uninstall_keeps_models(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """卸载运行包不动模型：模型是用户下载的数据，删不删由模型表决定。"""
    _fake_exe(service)
    keep = service.models_dir / "keep.txt"
    keep.write_text("x", encoding="utf-8")
    monkeypatch.setattr(rt, "move_to_trash", lambda path: __import__("shutil").rmtree(path))
    service.uninstall(True)
    assert keep.is_file()
    assert not (service.root / "bin").exists()


def test_catalog_refresh_reports_upstream_error(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "refresh", lambda root: (_ for _ in ()).throw(OSError("连不上")))
    with pytest.raises(ProducerError, match="连不上"):
        service.refresh_catalog()
    assert service.state()["catalog"]["note"] == "连不上"


class _Proc:
    def __init__(self, code: int | None = None) -> None:
        self.pid = 4242
        self._code = code

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        self._code = -1

    def kill(self) -> None:
        self._code = -1

    def wait(self, timeout: float | None = None) -> int | None:
        return self._code


def test_instance_rows_only_show_installed_models(service: rt.RuntimeService) -> None:
    """引擎里配了地址不等于「这里有一台」：模型没装的行不列。

    从前 6 行一直在（端口 + 模型 id 明明是从引擎设置推出来的，看着却像写死的表）。
    """
    assert service.state()["instances"] == []
    _materialize(service.models_dir, "breeze_tts")
    rows = service.state()["instances"]
    assert [row["port"] for row in rows] == [8080]
    assert rows[0]["label"] == "BreezeTTS 2"


def test_running_service_is_listed_even_without_model(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """活着的服务不能藏：别人的进程我们没装过模型，但它确实在跑。"""
    monkeypatch.setattr(rt.ProcessPool, "probe", lambda self, port: (True, 1))
    rows = service.state()["instances"]
    assert len(rows) == len(service.instances()) > 0
    assert all(row["running"] for row in rows)


def test_auto_start_chips_only_cover_installed_models(service: rt.RuntimeService) -> None:
    """自动拉起的候选只有已装模型：没下的东西不该出现（哪怕是灰的）。"""
    _materialize(service.models_dir, "breeze_tts")
    service.settings(auto_start=["breeze-tts-2", "cosyvoice3"])
    assert service.state()["auto_start"] == ["breeze-tts-2"]


def test_install_starts_an_instance_first(
    service: rt.RuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一台实例都没有时点「安装」：先按引擎地址拉起一台空实例，再调下载。"""
    _fake_exe(service)
    conn = _FakeConn("http://127.0.0.1:8080")
    runs = {"count": 0}
    monkeypatch.setattr(
        rt.ProcessPool,
        "spawn",
        lambda self, exe, config, env=None: (runs.update(count=runs["count"] + 1) or _Proc(), io.BytesIO()),
    )
    monkeypatch.setattr(rt.ProcessPool, "wait_ready", lambda self, port, timeout=10.0: True)
    monkeypatch.setattr(rt.ProcessPool, "owned", lambda self, port: runs["count"] > 0)
    monkeypatch.setattr(rt.ProcessPool, "probe", lambda self, port: (runs["count"] > 0, 0))
    monkeypatch.setattr(
        manager.Connection, "open", classmethod(lambda cls, url, timeout=1.0: conn)
    )
    package = catalog.family("breeze_tts")["packages"][0]["id"]
    service.install_model(package)
    assert runs["count"] == 1
    payload = json.loads(service.pool.config_path(8080).read_text(encoding="utf-8"))
    assert payload["ui_management"] is True
    assert ("install", package) in conn.calls


def test_a_runtime_without_a_checksum_is_refused(tmp_path: Path) -> None:
    """清单缺 sha256 就别装：这个包下载完是要直接执行的。"""
    from app.producers.builtin.audiocpp.runtimes import Installer

    installer = Installer(tmp_path / "rt", lambda *_args: None)
    with pytest.raises(ProducerError, match="sha256"):
        installer.install({"url": "https://example.com/a.zip", "sha256": ""})
