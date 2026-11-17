"""统一日志通道：形状、分发、三档去处、游标补帧。

这一层是**全项目日志的唯一出口**（规则见 ``AGENTS.md`` 的「日志」一节），所以这里钉的
都是"出口本身"的规矩，而不是某一条日志长什么样：

- 一个坏去处**不能连累别的**（否则一个写不动的文件会让界面上一行都没有）；
- stdlib 那条路的级别翻译与来源推导；
- `seq` 与游标——它同时撑着"断线回来补帧"和"快照与流重叠了也不重"；
- **环形转过一圈之后快照要明说"缺了一段"**：假装连续比明说缺了更坏，
  用户会以为中间什么都没发生。

异步那几处用 ``asyncio.run`` 包着跑（本项目的测试环境没装 pytest-asyncio，
而 ``test_breeze_tts.py`` 那批也是这个写法）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from app.domain.log import (
    LEVELS,
    ChannelLogHandler,
    LogChannel,
    LogRecord,
    MemorySink,
    source_of,
)
from app.infra.logfile import FileSink
from app.infra.loghub import CHANNEL, LogHub


class Recorder:
    """一个只把收到的行记下来的假去处（**故意不继承任何基类**：
    sink 是协议，不是继承关系——这正是要钉住的那条设计）。"""

    def __init__(self, *, boom: bool = False) -> None:
        self.lines: list[LogRecord] = []
        self.boom = boom

    def emit(self, record: LogRecord) -> None:
        if self.boom:
            raise RuntimeError("这个去处坏了")
        self.lines.append(record)


class TestChannel:
    def test_a_record_reaches_every_sink(self) -> None:
        channel = LogChannel()
        first, second = Recorder(), Recorder()
        channel.add_sink(first)
        channel.add_sink(second)

        channel.info("你好", source="gateway")

        assert [r.message for r in first.lines] == ["你好"]
        assert [r.message for r in second.lines] == ["你好"]

    def test_a_broken_sink_does_not_stop_the_others(self) -> None:
        """**这条是整块设计的地基**：写文件的那个坏了（磁盘满、权限不对），
        界面那一路必须照旧收到——否则"日志写不下去"会表现成"界面上一行都没有"，
        而那一刻正是最需要日志的时候。"""
        channel = LogChannel()
        channel.add_sink(Recorder(boom=True))
        alive = Recorder()
        channel.add_sink(alive)

        channel.error("这一条要活着出去", source="system")

        assert [r.message for r in alive.lines] == ["这一条要活着出去"]

    def test_publishing_never_raises_even_when_every_sink_is_broken(self) -> None:
        channel = LogChannel()
        channel.add_sink(Recorder(boom=True))
        channel.publish(LogRecord(level="error", message="不该抛"))  # 不抛就是通过

    def test_the_same_sink_is_not_added_twice(self) -> None:
        """重复挂同一个会被忽略——启动与任务开始都会走这条路径，
        挂重了就是每一条日志在界面上出现两遍。"""
        channel = LogChannel()
        sink = Recorder()
        channel.add_sink(sink)
        channel.add_sink(sink)

        channel.info("只该来一次", source="app")

        assert len(sink.lines) == 1

    def test_a_sink_can_be_removed(self) -> None:
        """任务结束要能把自己那个 sink 摘掉，否则每跑一次任务就多留一个。"""
        channel = LogChannel()
        sink = Recorder()
        channel.add_sink(sink)
        channel.remove_sink(sink)

        channel.info("摘掉之后不该再来", source="app")

        assert sink.lines == []

    def test_removing_a_sink_that_was_never_added_is_not_an_error(self) -> None:
        LogChannel().remove_sink(Recorder())

    @pytest.mark.parametrize("level", LEVELS)
    def test_the_convenience_entries_carry_their_own_level(self, level: str) -> None:
        channel = LogChannel()
        sink = Recorder()
        channel.add_sink(sink)

        channel.log(level, "一句话", source="api")

        assert sink.lines[0].level == level

    def test_the_four_levels_are_the_same_four_as_the_other_repo(self) -> None:
        """分级是与有声书那一侧对齐的第一件事——两仓的日志栏要长得一样，
        先得"级别"这个词指的是同一批词。"""
        assert LEVELS == ("debug", "info", "warning", "error")


class TestSource:
    def test_the_package_prefix_is_stripped(self) -> None:
        assert source_of("app.usecase.asr") == "usecase.asr"

    def test_a_third_party_logger_keeps_its_own_name(self) -> None:
        """第三方库的名字原样留下——"这条是哪个库说的"要一眼看得出来。"""
        assert source_of("httpx._client") == "httpx._client"
        assert source_of("uvicorn.error") == "uvicorn.error"

    def test_an_empty_name_falls_back_to_system(self) -> None:
        assert source_of("") == "system"
        assert source_of(None) == "system"


class TestStdlibHandler:
    def _forward(self, record: logging.LogRecord) -> Recorder:
        channel = LogChannel()
        sink = Recorder()
        channel.add_sink(sink)
        ChannelLogHandler(channel).emit(record)
        return sink

    def _record(self, level: int, message: str, name: str = "app.infra.ffmpeg") -> logging.LogRecord:
        return logging.LogRecord(name, level, __file__, 1, message, None, None)

    def test_a_logging_record_becomes_a_log_record(self) -> None:
        sink = self._forward(self._record(logging.INFO, "pip 装完了"))

        assert sink.lines[0].message == "pip 装完了"
        assert sink.lines[0].source == "infra.ffmpeg"
        assert sink.lines[0].level == "info"

    def test_warning_and_error_keep_their_level(self) -> None:
        sink = self._forward(self._record(logging.WARNING, "慢"))
        assert sink.lines[0].level == "warning"

    def test_critical_lands_on_error(self) -> None:
        """``CRITICAL`` 并进 error：这一档在我们这边没有独立的语义，
        而"收起态能不能看见"是按**算不算失败**分的——critical 当然是失败。"""
        sink = self._forward(self._record(logging.CRITICAL, "炸了"))
        assert sink.lines[0].level == "error"

    def test_an_exception_is_carried_as_the_type_name(self) -> None:
        """异常只带**类型名**，不带栈：栈有几行由 stdlib 决定，
        而"是哪一种错"才是事后要筛的那一格。"""
        try:
            raise ValueError("boom")
        except ValueError:
            record = self._record(logging.ERROR, "合成失败")
            record.exc_info = __import__("sys").exc_info()
        sink = self._forward(record)

        assert sink.lines[0].ctx["exc_type"] == "ValueError"

    def test_no_exception_means_no_empty_key(self) -> None:
        """空键与"没要求"在界面上长得一样，却白占地方——所以只带确实存在的那几样。"""
        sink = self._forward(self._record(logging.WARNING, "没带异常"))
        assert "exc_type" not in sink.lines[0].ctx

    def test_the_attached_channel_is_readable(self) -> None:
        """挂之前要靠它认人：**同一个通道挂两遍，每条日志会出现两次**。"""
        channel = LogChannel()
        assert ChannelLogHandler(channel).channel is channel


class TestMemorySink:
    def test_it_keeps_only_the_last_few(self) -> None:
        """它是"最近攒着的那点东西"，不是全量历史——全量在文件那一份里。"""
        sink = MemorySink(limit=3)
        for index in range(5):
            sink.emit(LogRecord(level="info", message=f"第 {index} 条"))

        assert [r.message for r in sink.recent(10)] == ["第 2 条", "第 3 条", "第 4 条"]

    def test_recent_is_oldest_first_and_takes_the_tail(self) -> None:
        sink = MemorySink(limit=10)
        for index in range(5):
            sink.emit(LogRecord(level="info", message=f"第 {index} 条"))

        assert [r.message for r in sink.recent(2)] == ["第 3 条", "第 4 条"]

    def test_clear_empties_it(self) -> None:
        sink = MemorySink()
        sink.emit(LogRecord(level="info", message="一条"))
        sink.clear()

        assert sink.recent(10) == []


class TestLogHub:
    def test_every_record_gets_its_own_number(self) -> None:
        hub = LogHub()
        for _ in range(3):
            hub.emit(LogRecord(level="info", message="一条"))

        assert [line["seq"] for line in hub.snapshot()["lines"]] == [1, 2, 3]
        assert hub.cursor == 3

    def test_snapshot_only_gives_what_is_after_the_cursor(self) -> None:
        hub = LogHub()
        for index in range(3):
            hub.emit(LogRecord(level="info", message=f"第 {index} 条"))

        body = hub.snapshot(since=1)

        assert [line["message"] for line in body["lines"]] == ["第 1 条", "第 2 条"]
        assert body["cursor"] == 3

    def test_a_wrapped_ring_says_it_dropped_something(self) -> None:
        """**"缺了一段"必须说出来**：环形转过一圈之后，`since=0` 的人拿到的
        不是从头那一份，而它看起来与"从头"一模一样。"""
        hub = LogHub(limit=2)
        for index in range(5):
            hub.emit(LogRecord(level="info", message=f"第 {index} 条"))

        body = hub.snapshot(since=0)

        assert body["dropped"] is True
        assert [line["message"] for line in body["lines"]] == ["第 3 条", "第 4 条"]

    def test_a_cursor_inside_the_ring_is_not_a_gap(self) -> None:
        hub = LogHub(limit=10)
        for index in range(3):
            hub.emit(LogRecord(level="info", message=f"第 {index} 条"))

        assert hub.snapshot(since=1)["dropped"] is False

    def test_an_empty_hub_is_not_a_gap(self) -> None:
        """一条日志都没有时不能说"缺了一段"——那会把空状态说成故障。"""
        assert LogHub().snapshot(since=0)["dropped"] is False

    def test_the_snapshot_can_be_capped_and_keeps_the_newest(self) -> None:
        """首屏要的是"最近发生了什么"，所以截断截掉的是**老的**那一头。"""
        hub = LogHub(limit=50)
        for index in range(10):
            hub.emit(LogRecord(level="info", message=f"第 {index} 条"))

        assert [line["message"] for line in hub.snapshot(limit=2)["lines"]] == [
            "第 8 条",
            "第 9 条",
        ]

    def test_the_stream_gives_the_backlog_then_the_new_ones(self) -> None:
        """先补帧、再推新的，两条都要。缺前一条 = "打开日志栏看不到历史"，
        缺后一条 = "看完历史就再也没有新的了"。"""
        hub = LogHub()

        async def drive() -> list[str]:
            seen: list[str] = []

            async def reader() -> None:
                async for frame in hub.stream(since=0):
                    seen.append(frame["message"])
                    if len(seen) >= 2:
                        return

            hub.emit(LogRecord(level="info", message="老的那一条"))
            task = asyncio.create_task(reader())
            await asyncio.sleep(0.01)
            hub.emit(LogRecord(level="info", message="新的那一条"))
            await asyncio.wait_for(task, timeout=2)
            return seen

        assert asyncio.run(drive()) == ["老的那一条", "新的那一条"]

    def test_the_stream_can_start_after_the_cursor(self) -> None:
        hub = LogHub()
        hub.emit(LogRecord(level="info", message="不要这一条"))
        hub.emit(LogRecord(level="info", message="要这一条"))

        async def first() -> dict:
            async for frame in hub.stream(since=1):
                return frame

        assert asyncio.run(first())["message"] == "要这一条"

    def test_a_closed_stream_unsubscribes_itself(self) -> None:
        """客户端断开（生成器被关掉）之后订阅必须被摘掉。不摘的话界面上一切正常，
        只是每开一次日志栏就多留一个**永远没人读**的队列：白占内存，
        而且每一次 `emit` 都要为它多做一遍无用功。"""
        hub = LogHub()

        async def drive() -> tuple[int, int]:
            pending = asyncio.create_task(hub.stream(since=0).__anext__())
            await asyncio.sleep(0.01)
            while_subscribed = hub.subscribers
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.01)
            return while_subscribed, hub.subscribers

        assert asyncio.run(drive()) == (1, 0)


class TestFileSink:
    def test_one_line_per_record_keeps_the_level_source_and_ctx(self, tmp_path: Path) -> None:
        path = tmp_path / "logs" / "voicehub.log"
        sink = FileSink(path)
        sink.emit(LogRecord(level="warning", message="清单拉不到", source="engines", ctx={"engine": "minimax"}))

        text = path.read_text(encoding="utf-8")
        assert "[WARNING]" in text
        assert "engines 清单拉不到" in text
        assert "engine=minimax" in text
        assert text.endswith("\n")

    def test_a_record_without_ctx_has_no_trailing_pipe(self, tmp_path: Path) -> None:
        """空 ctx 不缀尾巴——否则每一行都拖着半截分隔符，看着像漏了字段。"""
        path = tmp_path / "v.log"
        FileSink(path).emit(LogRecord(level="info", message="一句话", source="app"))

        assert "|" not in path.read_text(encoding="utf-8")

    def test_it_rotates_once_when_it_grows_past_the_limit(self, tmp_path: Path) -> None:
        """不设上限就是"装十次模型之后磁盘上多出一个几十兆的文本"。
        只留一代：真正要长期留存该接的是外部日志系统，不是这个文件。"""
        path = tmp_path / "v.log"
        sink = FileSink(path, max_bytes=1)  # 第二次写之前必定超
        sink.emit(LogRecord(level="info", message="第一代", source="app"))
        sink.emit(LogRecord(level="info", message="第二代", source="app"))

        assert "第二代" in path.read_text(encoding="utf-8")
        assert "第一代" in path.with_name("v.log.1").read_text(encoding="utf-8")

    def test_zero_means_no_rotation(self, tmp_path: Path) -> None:
        path = tmp_path / "v.log"
        sink = FileSink(path, max_bytes=0)
        for index in range(3):
            sink.emit(LogRecord(level="info", message=f"第 {index} 条", source="app"))

        assert not path.with_name("v.log.1").exists()
        assert len(path.read_text(encoding="utf-8").splitlines()) == 3

    def test_the_directory_is_created_on_demand(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "v.log"
        FileSink(path).emit(LogRecord(level="info", message="一条", source="app"))

        assert path.is_file()


class TestApi:
    """两条口：快照与流。**这里只钉快照**——流那一条永远不会自己结束，
    用 ``TestClient`` 去读会挂住；它的行为由 :class:`TestLogHub` 那几例直接钉
    ``LogHub.stream``（HTTP 那一层只是把生成器套进 ``text/event-stream``）。"""

    def test_the_snapshot_answers_with_its_three_keys(self, client) -> None:
        """**一条日志都没有时也要答得出来**：首屏拿到空表是正常的，
        报错才是坏的（界面会一直挂着"连接中"）。"""
        body = client.get("/api/v1/logs").json()

        assert set(body) >= {"lines", "cursor", "dropped"}
        assert isinstance(body["lines"], list)
        assert isinstance(body["cursor"], int)

    def test_a_logged_line_comes_back_with_seq_source_and_ctx(self, client) -> None:
        CHANNEL.info("测试：这一条要能被读到", source="gateway", engine="minimax")

        body = client.get("/api/v1/logs?since=0&limit=1000").json()
        hits = [line for line in body["lines"] if line["message"] == "测试：这一条要能被读到"]

        assert hits, "刚写的那一条没回来"
        assert hits[0]["source"] == "gateway"
        assert hits[0]["ctx"]["engine"] == "minimax"
        assert hits[0]["seq"] >= 1

    def test_the_cursor_lets_a_client_ask_for_only_what_it_missed(self, client) -> None:
        """断线重连就是靠这一条：带上上次读到的游标，只拿之后那一段。"""
        first = client.get("/api/v1/logs?since=0&limit=1000").json()
        CHANNEL.warning("测试：游标之后的那一条", source="api")

        second = client.get(f"/api/v1/logs?since={first['cursor']}").json()

        assert [line["message"] for line in second["lines"]] == ["测试：游标之后的那一条"]

    def test_a_limit_younger_than_the_log_does_not_break_the_cursor(self, client) -> None:
        """截断的是行、不是游标：游标必须还是**最新的那个号**，
        否则界面下次会拿一个旧游标去要，那一段就会一遍遍重来。"""
        CHANNEL.info("测试：截断也要给对游标", source="api")

        body = client.get("/api/v1/logs?since=0&limit=1").json()

        assert body["cursor"] >= body["lines"][-1]["seq"]
