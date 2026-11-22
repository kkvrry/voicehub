"""audio.cpp 运行实例 → 引擎可用性：族名归位、读不到时的回退、清单字段。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.engines.registry import list_engines
from app.infra.config import load_config, save_config
from app.usecase import engines, live_models
from app.usecase import producers as producer_usecase


def test_family_alias_maps_to_engine_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """族名与 key 不同名时按 audio.cpp 那一份别名表归位，陌生族不进清单。"""
    monkeypatch.setattr(
        live_models,
        "running_models",
        lambda **_: [
            {"id": "x", "family": "index_tts2", "url": "http://127.0.0.1:18101"},
            {"id": "y", "family": "not_an_engine", "url": "http://127.0.0.1:18109"},
        ],
    )
    out = live_models.live_engine_keys(["indextts2", "cosyvoice3"])
    assert list(out) == ["indextts2"]
    assert out["indextts2"]["url"] == "http://127.0.0.1:18101"


def test_unreadable_runtime_is_empty_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件没构造或读状态失败就是没有可报告的模型，不该让清单报错。"""

    def boom(_key: str) -> Any:
        raise RuntimeError("audio.cpp 没起来")

    monkeypatch.setattr(producer_usecase, "started_service", boom)
    monkeypatch.setattr(live_models, "_cached", None)
    assert live_models.running_models(fresh=True) == []


def test_engine_payloads_carry_running(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """三份清单都带 running，只反映"有模型在跑"，不改 key 顺序与 ready。"""
    running = next(iter(list_engines()))
    other = next(key for key in list_engines() if key != running)
    config = load_config()
    config.engines_enabled = [running, other]
    save_config(config)
    monkeypatch.setattr(
        engines,
        "live_engine_keys",
        lambda: {running: {"id": "x", "family": running, "url": ""}},
    )
    rows = [
        {row["key"]: row for row in engines.catalog_payload()},
        {row["key"]: row for row in engines.engine_choices()},
    ]
    for table in rows:
        assert table[running]["running"] is True
        assert table[other]["running"] is False
    assert engines.settings_payload(running)["running"] is True
    assert engines.settings_payload(other)["running"] is False


def test_available_kind_by_deployment() -> None:
    """云端填了 Key 就可用，本地还得实例在跑；ready 与 running 各自不变。"""
    # 云端：就绪即可，running 恒假也算可用
    assert engines._available("cloud", ready=True, running=False) is True
    # 本地：光就绪不够，实例没起不可用
    assert engines._available("local", ready=True, running=False) is False
    assert engines._available("local", ready=True, running=True) is True
    # 无论哪种形态，没就绪就不可用
    assert engines._available("cloud", ready=False, running=True) is False
    assert engines._available("local", ready=False, running=True) is False


def test_cloud_engine_with_key_is_available(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """云引擎填了 Key（ready）即下发 available=True，不需要任何运行实例。"""
    cloud = next(k for k, e in list_engines().items() if e.capabilities().kind == "cloud")
    config = load_config()
    config.engines_enabled = [cloud]
    # 走密钥存储写 Key，保证 _stored 解密后 configured=True
    from app.infra import secrets as secret_store

    fields = list(list_engines()[cloud].secret_fields())
    assert fields  # 云引擎要有密钥字段，否则这个测试的前提不成立
    stored = secret_store.seal_fields({fields[0]: "test-key-for-available"}, fields)
    config.engines[cloud] = stored
    save_config(config)
    # 没有任何运行实例
    monkeypatch.setattr(engines, "live_engine_keys", dict)

    catalog = {row["key"]: row for row in engines.catalog_payload()}
    assert catalog[cloud]["ready"] is True
    assert catalog[cloud]["running"] is False
    assert catalog[cloud]["available"] is True
    assert engines.settings_payload(cloud)["available"] is True
