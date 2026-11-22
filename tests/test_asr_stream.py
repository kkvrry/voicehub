"""流式识别：**服务端 VAD 分段**与它的 WebSocket 通道。

守住的是：

1. 状态机按"说话—停顿"切段：说一段、停一拍，就得恰好一段，起音不削、
   结尾静音不拖；不足最短说话长度的碎段丢弃；
2. 说个不停也有上限：段长到顶硬切，识别引擎不会收到无限长的段；
3. 通道的准入与产出：引擎没配好给一句"去哪儿配"而不是 traceback；
   说完一段就推一段（``segment``），收尾把没说完的话冲出来（``done``）；
4. 听完即删：临时 wav 不留盘。

神经 VAD 对合成音不响应，所以打分器一律注入假的（按脚本吐概率）；
识别层与 ``test_dictation`` 同款假法（记下被问了什么、答固定台词）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from app.usecase import asr, asr_stream

#: 一帧 = 512 样本 @16k 的 int16（32ms）。测试里它就是"状态机走一步"。
FRAME_BYTES = asr_stream.FRAME_SAMPLES * 2


def _frame() -> bytes:
    return bytes(FRAME_BYTES)


class ScriptedScorer:
    """按脚本逐帧吐概率：脚本走完停在最后一个值（测试不用算到正好）。"""

    def __init__(self, probs: list[float]) -> None:
        self._probs = list(probs)

    def __call__(self, _chunk: np.ndarray) -> float:
        return self._probs.pop(0) if self._probs else self._last

    @property
    def _last(self) -> float:
        return 0.0


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #


class TestSegmenter:
    def test_one_utterance_becomes_one_segment(self):
        """说话 10 帧 + 静音满 700ms → 恰好一段；段里含起音垫帧。"""
        # 前 8 帧静音（进垫帧），10 帧说话，之后一路静音到判定说完。
        scorer = ScriptedScorer([0.1] * 8 + [0.9] * 10)
        session = asr_stream.StreamSession(scorer=scorer)
        segments: list[bytes] = []
        for _ in range(40):
            segments.extend(session.feed(_frame(), 16000))
        assert len(segments) == 1, "说完停住就该切段，多切或漏切都错"
        seconds = len(segments[0]) // 2 / asr_stream.TARGET_RATE
        # 10 帧说话（320ms）+ 起音垫 + 结尾垫，去掉多余的静音尾巴。
        assert 0.25 <= seconds <= 0.7, f"段长 {seconds:.2f}s 不合理"

    def test_two_utterances_with_a_pause_become_two_segments(self):
        scorer = ScriptedScorer(
            [0.9] * 10 + [0.1] * 25 + [0.9] * 10 + [0.1] * 25
        )
        session = asr_stream.StreamSession(scorer=scorer)
        segments: list[bytes] = []
        for _ in range(80):
            segments.extend(session.feed(_frame(), 16000))
        assert len(segments) == 2, "停顿隔开的两句话是两段"

    def test_a_blink_does_not_make_a_segment(self):
        """一声咳嗽（两帧高概率）不足最短说话时长，不成段。"""
        scorer = ScriptedScorer([0.9] * 2 + [0.1] * 30)
        session = asr_stream.StreamSession(scorer=scorer)
        segments: list[bytes] = []
        for _ in range(40):
            segments.extend(session.feed(_frame(), 16000))
        assert segments == [], "碎段要丢，不然识别引擎被噪声刷屏"

    def test_speaking_forever_is_cut_at_the_cap(self):
        """一直不停地说：段长到顶硬切，后续的话进下一段。"""
        scorer = ScriptedScorer([0.9] * 2000)
        session = asr_stream.StreamSession(scorer=scorer)
        segments: list[bytes] = []
        for _ in range(2000):  # 64s：够切两刀
            segments.extend(session.feed(_frame(), 16000))
        assert len(segments) >= 2, "超过 30s 的独白必须切开"
        for segment in segments:
            seconds = len(segment) // 2 / asr_stream.TARGET_RATE
            assert seconds <= asr_stream.MAX_SEGMENT_MS / 1000 + 0.1

    def test_flush_pushes_out_the_unfinished_utterance(self):
        """还在说话中就收尾：已说的话要冲出去，不能吞掉。"""
        scorer = ScriptedScorer([0.1] * 4 + [0.9] * 12)
        session = asr_stream.StreamSession(scorer=scorer)
        segments: list[bytes] = []
        for _ in range(16):
            segments.extend(session.feed(_frame(), 16000))
        assert segments == [], "还没停顿就不该切段"
        tail = session.flush()
        assert tail is not None, "收尾得把没说完的冲出来"
        seconds = len(tail) // 2 / asr_stream.TARGET_RATE
        assert 0.25 <= seconds <= 0.7

    def test_other_sample_rates_are_resampled(self):
        """48k 进来先统一到 16k：帧数与 16k 的完全一致。"""
        scorer = ScriptedScorer([0.9] * 10)
        session = asr_stream.StreamSession(scorer=scorer)
        for _ in range(10):
            segments = session.feed(_frame() * 3, 48000)  # 1536 样本 @48k = 512 @16k
        assert segments == [], "还没停顿不该切段"
        tail = session.flush()
        assert tail is not None, "重采样后的说话帧要能拼成段"


# --------------------------------------------------------------------------- #
# 识别一跳
# --------------------------------------------------------------------------- #


class TestTranscribePcm:
    def test_the_wav_is_deleted_after_listening(self, monkeypatch: pytest.MonkeyPatch):
        """听完即删：这段音频唯一的用途就是这一句话。"""
        seen: list[Any] = []

        def fake(path):
            seen.append(path)
            return {"text": "月亮升起来了。"}

        monkeypatch.setattr(asr, "transcribe_file", fake)
        result = asr_stream.transcribe_pcm(_frame() * 20)
        assert result["text"] == "月亮升起来了。"
        assert seen and not seen[0].exists(), "临时 wav 不能留盘"


# --------------------------------------------------------------------------- #
# WebSocket 通道
# --------------------------------------------------------------------------- #


class TestStreamEndpoint:
    @pytest.fixture()
    def wired(self, monkeypatch: pytest.MonkeyPatch):
        """把识别与 VAD 都换成假的：引擎已就绪、VAD 按脚本吐概率。"""
        monkeypatch.setattr(asr, "unavailable_reason", lambda: "")
        monkeypatch.setattr(asr_stream, "vad_unavailable_reason", lambda: "")
        heard: list[Any] = []

        def fake_transcribe(path):
            heard.append(path)
            return {"text": "月亮升起来了。", "seconds": 0.5}

        monkeypatch.setattr(asr, "transcribe_file", fake_transcribe)

        def fake_session(on_speech=None):
            script = [0.1] * 4 + [0.9] * 10 + [0.1] * 25
            return asr_stream.StreamSession(scorer=ScriptedScorer(script), on_speech=on_speech)

        monkeypatch.setattr(asr_stream, "new_session", fake_session)
        return heard

    def test_speaking_produces_events_in_order(self, client, wired):
        with client.websocket_connect("/api/v1/asr/stream") as ws:
            ws.send_json({"type": "start", "sample_rate": 16000})
            assert ws.receive_json()["type"] == "ready"
            for _ in range(40):
                ws.send_bytes(_frame())
            events = [ws.receive_json(), ws.receive_json()]
            kinds = [event["type"] for event in events]
            assert kinds == ["speech", "segment"], f"事件顺序不对：{kinds}"
            assert events[1]["text"] == "月亮升起来了。"
            ws.send_json({"type": "stop"})
            assert ws.receive_json()["type"] == "done"

    def test_without_an_engine_we_say_where_to_get_one(self, client, monkeypatch):
        monkeypatch.setattr(asr, "unavailable_reason", lambda: "识别引擎尚未就绪：缺配置。")
        with client.websocket_connect("/api/v1/asr/stream") as ws:
            event = ws.receive_json()
            assert event["type"] == "error"
            assert "识别引擎" in event["message"]

    def test_an_unknown_command_is_rejected(self, client, wired):
        with client.websocket_connect("/api/v1/asr/stream") as ws:
            ws.send_json({"type": "start", "sample_rate": 16000})
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "explode"})
            event = ws.receive_json()
            assert event["type"] == "error"
            assert "start / stop" in event["message"]

    def test_stop_flushes_the_utterance_in_flight(self, client, wired, monkeypatch):
        """说话说到一半就点停：没说完的那截要冲出来成为一段。"""

        def frozen_session(on_speech=None):
            # 前 4 帧静音后一直说话，停不下来——全靠 stop 把话冲出去。
            return asr_stream.StreamSession(
                scorer=ScriptedScorer([0.1] * 4 + [0.9] * 9999), on_speech=on_speech
            )

        monkeypatch.setattr(asr_stream, "new_session", frozen_session)
        with client.websocket_connect("/api/v1/asr/stream") as ws:
            ws.send_json({"type": "start", "sample_rate": 16000})
            assert ws.receive_json()["type"] == "ready"
            for _ in range(20):
                ws.send_bytes(_frame())
            assert ws.receive_json()["type"] == "speech"
            ws.send_json({"type": "stop"})
            segment = ws.receive_json()
            assert segment["type"] == "segment", "收尾要把没说完的话冲成一段"
            assert segment["text"] == "月亮升起来了。"
            assert ws.receive_json()["type"] == "done"
