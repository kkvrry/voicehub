"""密钥落盘：DPAPI 往返、保存后不再是明文、旧明文启动迁移。"""

from __future__ import annotations

import pytest

from app.infra import secrets as secret_store
from app.infra.config import load_config, save_config

pytestmark = pytest.mark.skipif(
    not secret_store.available(), reason="这台机器没有 Windows DPAPI"
)


def _raw(data_dir) -> str:
    return (data_dir / "config.json").read_text(encoding="utf-8")


class TestSeal:
    def test_round_trip(self):
        sealed = secret_store.seal("sk-中文-123")
        assert secret_store.sealed(sealed)
        assert "sk-中文-123" not in sealed
        assert secret_store.reveal(sealed) == "sk-中文-123"

    def test_empty_and_legacy_pass_through(self):
        assert secret_store.seal("") == ""
        assert secret_store.reveal("sk-legacy") == "sk-legacy"

    def test_broken_ciphertext_reads_as_empty(self):
        """配置可能被手改坏：解不开就当没填，不让设置页整页读不出来。"""
        assert secret_store.reveal(secret_store.PREFIX + "not-base64!!") == ""


class TestStoredConfig:
    def test_engine_key_is_not_plaintext_on_disk(self, data_dir):
        from app.usecase.engines import require_engine, save_engine_settings, settings_for

        save_engine_settings("minimax", {"api_key": "sk-plain-text"})
        raw = _raw(data_dir)
        assert "sk-plain-text" not in raw
        assert secret_store.PREFIX in raw
        assert settings_for(require_engine("minimax")).api_key == "sk-plain-text"  # type: ignore[attr-defined]

    def test_llm_key_is_not_plaintext_on_disk(self, data_dir):
        from app.usecase.llm import require_provider, save_provider_settings, settings_for

        save_provider_settings("deepseek", {"api_key": "sk-llm-text"})
        raw = _raw(data_dir)
        assert "sk-llm-text" not in raw
        assert settings_for(require_provider("deepseek")).api_key == "sk-llm-text"  # type: ignore[attr-defined]

    def test_startup_migration_seals_legacy_plaintext(self, data_dir):
        from app.usecase.engines import require_engine, settings_for
        from app.usecase.migrate_secrets import migrate

        config = load_config()
        config.engines["minimax"] = {"api_key": "sk-legacy"}
        save_config(config)
        assert "sk-legacy" in _raw(data_dir)  # 旧格式：明文

        assert migrate() >= 1

        raw = _raw(data_dir)
        assert "sk-legacy" not in raw
        assert settings_for(require_engine("minimax")).api_key == "sk-legacy"  # type: ignore[attr-defined]

    def test_migration_is_idempotent(self, data_dir):
        from app.usecase.migrate_secrets import migrate

        config = load_config()
        config.engines["minimax"] = {"api_key": "sk-once"}
        save_config(config)
        migrate()
        before = _raw(data_dir)
        assert migrate() == 0
        assert _raw(data_dir) == before
