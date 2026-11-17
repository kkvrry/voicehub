"""配置层：``config.json`` 的读写，以及**端口那三层的优先级**。

端口值得单独钉住：它的顺序（``--port`` > ``VOICEHUB_PORT`` > ``config.json`` > 默认）
一旦散成几份，"我明明改了配置怎么没生效"就只能靠读代码来查——而那段代码在启动路径上，
没人会为了一个端口去读它。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.infra.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    Config,
    load_config,
    resolve_host,
    resolve_port,
    save_config,
)


@pytest.fixture(autouse=True)
def _no_env_port(monkeypatch: pytest.MonkeyPatch):
    """环境变量是**机器级的**：跑测试的那台机器上可能真设了它，得先摘干净。"""
    monkeypatch.delenv("VOICEHUB_PORT", raising=False)
    monkeypatch.delenv("VOICEHUB_HOST", raising=False)


class TestPort:
    def test_default_when_nobody_said_anything(self, data_dir: Path) -> None:
        assert resolve_port(None) == DEFAULT_PORT

    def test_the_config_file_beats_the_default(self, data_dir: Path) -> None:
        save_config(Config(port=19100))
        assert resolve_port(None) == 19100

    def test_the_env_var_beats_the_config_file(self, data_dir: Path, monkeypatch) -> None:
        save_config(Config(port=19100))
        monkeypatch.setenv("VOICEHUB_PORT", "19200")
        assert resolve_port(None) == 19200

    def test_the_command_line_beats_everything(self, data_dir: Path, monkeypatch) -> None:
        """明知故犯的那一层最后说话——不这样的话，``--port`` 就成了个装饰。"""
        save_config(Config(port=19100))
        monkeypatch.setenv("VOICEHUB_PORT", "19200")
        assert resolve_port(19300) == 19300

    @pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-1", "70000", "17865.5", "inf"])
    def test_a_bad_value_falls_through_instead_of_breaking(self, data_dir: Path, monkeypatch, bad):
        """配置是手也能改的文件：写坏了不该让服务起不来——起不来的话，用户连界面都进不去，
        也就没法把它改回来。所以坏的当"没说"，往下一层落。"""
        monkeypatch.setenv("VOICEHUB_PORT", bad)
        save_config(Config(port=19100))
        assert resolve_port(None) == 19100

    def test_no_config_file_at_all_is_not_an_error(self, data_dir: Path) -> None:
        """服务第一次起来时 config.json 还不存在（列表也是空的）。"""
        assert not (data_dir / "config.json").exists()
        assert resolve_port(None) == DEFAULT_PORT

    def test_the_port_round_trips_through_the_file(self, data_dir: Path) -> None:
        save_config(Config(port=18080))
        assert load_config().port == 18080
        # 0 = "用默认值"：写回去仍是 0，不会被悄悄换成一个具体数字。
        save_config(Config())
        assert load_config().port == 0

    def test_a_port_written_by_hand_can_be_a_float(self, data_dir: Path) -> None:
        """``17865.0`` 是人手改文件很容易写出来的形状，要认。"""
        (data_dir / "config.json").write_text('{"port": 17865.0}', encoding="utf-8")
        assert load_config().port == 17865

    def test_a_port_that_is_not_a_number_reads_as_zero(self, data_dir: Path) -> None:
        (data_dir / "config.json").write_text('{"port": "八千"}', encoding="utf-8")
        assert load_config().port == 0


class TestHost:
    """监听地址与端口同构：默认只回环，开放局域网必须显式说一句。"""

    def test_loopback_is_the_default(self, data_dir: Path) -> None:
        assert resolve_host(None) == DEFAULT_HOST == "127.0.0.1"

    def test_the_config_file_can_open_the_lan(self, data_dir: Path) -> None:
        save_config(Config(host="0.0.0.0"))
        assert resolve_host(None) == "0.0.0.0"

    def test_the_env_var_beats_the_config_file(self, data_dir: Path, monkeypatch) -> None:
        save_config(Config(host="0.0.0.0"))
        monkeypatch.setenv("VOICEHUB_HOST", "192.168.1.20")
        assert resolve_host(None) == "192.168.1.20"

    def test_the_command_line_beats_everything(self, data_dir: Path, monkeypatch) -> None:
        save_config(Config(host="0.0.0.0"))
        monkeypatch.setenv("VOICEHUB_HOST", "192.168.1.20")
        assert resolve_host("127.0.0.1") == "127.0.0.1"

    @pytest.mark.parametrize("bad", ["", "  ", "0.0.0.0/0", "a b", "C:\\hosts"])
    def test_a_bad_value_falls_through_instead_of_breaking(
        self, data_dir: Path, monkeypatch, bad
    ) -> None:
        monkeypatch.setenv("VOICEHUB_HOST", bad)
        save_config(Config(host="0.0.0.0"))
        assert resolve_host(None) == "0.0.0.0"

    def test_the_host_round_trips_through_the_file(self, data_dir: Path) -> None:
        save_config(Config(host="0.0.0.0"))
        assert load_config().host == "0.0.0.0"
        # 空 = 用默认，写回去仍是空串，不会被悄悄钉成 127.0.0.1。
        save_config(Config())
        assert load_config().host == ""


class TestConfigRoundTrip:
    def test_unknown_keys_are_kept_in_extra(self, data_dir: Path) -> None:
        """老版本/别人写的键不该被这一版悄悄抹掉。"""
        (data_dir / "config.json").write_text(
            '{"port": 18080, "unknown_thing": 1}', encoding="utf-8"
        )
        loaded = load_config()
        assert loaded.port == 18080  # 认得的键照样认
        assert loaded.extra == {"unknown_thing": 1}
        save_config(loaded)
        assert '"unknown_thing": 1' in (data_dir / "config.json").read_text(encoding="utf-8")

    def test_a_hand_edit_is_seen_after_the_first_read(self, data_dir: Path) -> None:
        """缓存按 mtime + 大小失效：手改 config.json 之后读到的是新的。"""
        path = data_dir / "config.json"
        path.write_text('{"port": 18080}', encoding="utf-8")
        assert load_config().port == 18080
        # Windows 的 mtime 分辨率可能比两次写更粗；人改配置不会在同一个时钟刻度里完成。
        time.sleep(0.02)
        path.write_text('{"port": 18081}', encoding="utf-8")
        assert load_config().port == 18081

    def test_each_call_gets_its_own_copy(self, data_dir: Path) -> None:
        """缓存的是解析结果，不是共享对象：一处就地改字段不该影响下一次读。"""
        save_config(Config(host="0.0.0.0"))
        first = load_config()
        first.folders.append("C:\\临时")
        first.host = "127.0.0.1"
        again = load_config()
        assert again.folders == []
        assert again.host == "0.0.0.0"
