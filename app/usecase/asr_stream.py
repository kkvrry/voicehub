"""流式识别：麦克风音频流 → **服务端 VAD 分段** → 逐段交给当前识别引擎。

为什么要有这一层：``/asr/dictation`` 那条路是"整段录完再上传"——何时开始、
何时结束全由前端判断，判得糙（能量阈值）且每轮都要手动操作。这里把"说完
没有"的判断搬到服务端：音频以 WebSocket 流进来，Silero VAD（神经模型，
分得清人声与键盘、风扇、咳嗽）判定说完就切段、识别、把文字推回去——麦克风
可以一直开着，说完一段就收到一段，界面据此实现"连续对话"。

与插件契约的关系：切出来的每一段仍走 :func:`app.usecase.asr.transcribe_file`
（落成临时 wav 再听），**识别插件一行不用改**——流式只是核心提供的编排，
不是新的引擎形态。识别服务通常是单实例串行的，所以逐段识别在进程内加锁。

测试性：神经 VAD 对合成音不响应，单元测试给 :class:`StreamSession` 注入
假打分器（:func:`new_session` 是端点用的工厂，测试替换它）。
"""

from __future__ import annotations

import os
import tempfile
import threading
import wave
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from app.usecase import asr

__all__ = [
    "TARGET_RATE",
    "StreamSession",
    "new_session",
    "transcribe_pcm",
    "vad_unavailable_reason",
]

#: VAD 与分段统一在 16k 单声道下工作；其它采样率进来先线性重采样。
TARGET_RATE = 16000

#: Silero VAD @16k 的标准块长（32ms）。**必须**按这个粒度喂模型。
FRAME_SAMPLES = 512
_FRAME_MS = FRAME_SAMPLES * 1000 // TARGET_RATE  # 32

#: 说话判定阈值与两端的时间参数（与 Silero 官方默认一致的量级）。
THRESHOLD = 0.5
MIN_SPEECH_MS = 250
MIN_SILENCE_MS = 700
PAD_MS = 60
#: 一直不停地说也硬切（识别引擎吃不下无限长的段）。
MAX_SEGMENT_MS = 30_000

#: 逐段识别互斥：识别服务通常一次只跑一条（见 usecase.asr 的串行约定）。
_TRANSCRIBE_LOCK = threading.Lock()


def vad_unavailable_reason() -> str:
    """流式识别自己的依赖缺不缺：缺了给一句"去哪儿装"。空串 = 能用。

    与引擎是否就绪分开——那是 :func:`app.usecase.asr.unavailable_reason` 的事，
    两句都空才算真可用。
    """
    try:
        import silero_vad  # noqa: F401
        import torch  # noqa: F401
    except ImportError as exc:
        return f"流式识别依赖未安装（{exc.name}），请执行 pip install silero-vad。"
    return ""


def _new_vad() -> Callable[[np.ndarray], float]:
    """造一台全新的 VAD：模型带内部状态（h/c），**每路会话独享一台**。

    torch 与 silero_vad 都在这里才 import：FastAPI 主进程不该为用不上的
    流式功能背上 torch 的启动成本。
    """
    import torch
    from silero_vad import load_silero_vad

    model = load_silero_vad()

    def score(chunk: np.ndarray) -> float:
        with torch.no_grad():
            return float(model(torch.from_numpy(chunk), TARGET_RATE))

    return score


def _resample(data: np.ndarray, rate: int) -> np.ndarray:
    """任意采样率的单声道 → 16k。线性插值对 VAD 与识别够用，不做窗函数。"""
    if rate == TARGET_RATE:
        return data
    if rate <= 0:
        raise ValueError(f"采样率不合法：{rate}")
    duration = len(data) / rate
    target_len = max(1, round(duration * TARGET_RATE))
    if target_len == len(data):
        return data
    x_old = np.linspace(0.0, duration, num=len(data), endpoint=False)
    x_new = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return np.interp(x_new, x_old, data)


def new_session(on_speech: Callable[[], None] | None = None) -> StreamSession:
    """端点用这个造会话；测试替换本函数即可注入假打分器。

    :param on_speech: 转入"正在说话"时的回调（界面点亮"在听"）——在喂帧的
        协程里同步调用，务必轻（放个事件进队列这种）。
    """
    return StreamSession(on_speech=on_speech)


class StreamSession:
    """一路麦克风流的 VAD 分段器。**非线程安全**：只在收到帧的协程里用。

    状态机：静音帧只保留最近的 :data:`PAD_MS`（说话起始的回填），说话帧进
    缓冲；连续静音满 :data:`MIN_SILENCE_MS` 判定说完，整段交出去。段长超过
    :data:`MAX_SEGMENT_MS` 硬切，防止"一直不停地说"把识别引擎吃挂。
    """

    def __init__(
        self,
        scorer: Callable[[np.ndarray], float] | None = None,
        on_speech: Callable[[], None] | None = None,
    ) -> None:
        self._score = scorer or _new_vad()
        self._on_speech = on_speech
        self._residual = np.zeros(0, dtype=np.int16)
        self._in_speech = False
        self._silence_frames = 0
        self._speech: list[np.ndarray] = []
        self._speech_samples = 0
        self._pad: deque[np.ndarray] = deque(maxlen=max(1, PAD_MS // _FRAME_MS))

    def reset(self) -> None:
        """回到初始状态（同一连接复用会话时用；当前端点是"一连接一会话"，留着以防万一）。"""
        self._residual = np.zeros(0, dtype=np.int16)
        self._in_speech = False
        self._silence_frames = 0
        self._speech = []
        self._speech_samples = 0
        self._pad.clear()

    @property
    def in_speech(self) -> bool:
        """此刻是不是正在说话（界面用来点亮"在听"）。"""
        return self._in_speech

    def feed(self, pcm: bytes, rate: int) -> list[bytes]:
        """喂一帧 int16 单声道 PCM，返回这一帧**切出来**的完整语音段（16k int16 bytes）。

        一帧切出至多一段；返回空列表表示"没有段落完成"。
        """
        frame = np.frombuffer(pcm, dtype=np.int16).astype(np.int16)
        if frame.size == 0:
            return []
        data = _resample(frame.astype(np.float32), rate)
        data = np.clip(data, -32768.0, 32767.0).astype(np.int16)
        merged = np.concatenate([self._residual, data])
        segments: list[bytes] = []
        offset = 0
        while offset + FRAME_SAMPLES <= merged.size:
            chunk = merged[offset : offset + FRAME_SAMPLES]
            offset += FRAME_SAMPLES
            segment = self._step(chunk)
            if segment is not None:
                segments.append(segment)
        self._residual = merged[offset:]
        return segments

    def flush(self) -> bytes | None:
        """收尾：还停在说话状态里就把已说的话交出去（不足 :data:`MIN_SPEECH_MS` 丢弃）。"""
        if not self._in_speech:
            return None
        return self._take_segment()

    # ---- 内部 ------------------------------------------------------------- #

    def _step(self, chunk: np.ndarray) -> bytes | None:
        """按 512 样本块走一步状态机；返回这一步切出的段（如有）。"""
        prob = self._score(chunk.astype(np.float32) / 32768.0)
        speaking = prob >= THRESHOLD
        if speaking:
            self._silence_frames = 0
            if not self._in_speech:
                # 说话开始：把垫帧回填进段首，别把起音削掉。
                self._in_speech = True
                self._speech = list(self._pad)
                self._speech_samples = sum(item.size for item in self._speech)
                self._pad.clear()
                if self._on_speech is not None:
                    self._on_speech()
            self._speech.append(chunk)
            self._speech_samples += chunk.size
            if self._speech_samples * 1000 // TARGET_RATE >= MAX_SEGMENT_MS:
                return self._take_segment()
            return None
        if not self._in_speech:
            self._pad.append(chunk)
            return None
        # 说话中的静音帧：先收着当结尾垫，满了 :data:`MIN_SILENCE_MS` 才算说完。
        self._speech.append(chunk)
        self._speech_samples += chunk.size
        self._silence_frames += 1
        if self._silence_frames * _FRAME_MS >= MIN_SILENCE_MS:
            return self._take_segment()
        return None

    def _take_segment(self) -> bytes | None:
        """收段：去掉多余的结尾静音（只留 :data:`PAD_MS`），不足最短说话时长丢弃。"""
        keep_frames = max(1, PAD_MS // _FRAME_MS)
        drop = max(0, self._silence_frames - keep_frames)
        self._in_speech = False
        self._silence_frames = 0
        if drop:
            self._speech = self._speech[:-drop]
        if not self._speech:
            self._speech_samples = 0
            return None
        segment = np.concatenate(self._speech)
        self._speech = []
        self._speech_samples = 0
        if segment.size * 1000 // TARGET_RATE < MIN_SPEECH_MS:
            return None
        return segment.tobytes()


def transcribe_pcm(pcm: bytes) -> dict[str, Any]:
    """一段 16k int16 单声道 PCM → 临时 wav → 当前引擎识别 → 删临时文件。

    与 ``/asr/dictation`` 同一个规矩：这段音频的唯一用途就是这一句话，听完即删。
    逐段识别串行（:data:`_TRANSCRIBE_LOCK`）：识别服务通常一次只跑一条。
    """
    path = _write_wav(pcm)
    try:
        with _TRANSCRIBE_LOCK:
            return asr.transcribe_file(path)
    finally:
        path.unlink(missing_ok=True)


def _write_wav(pcm: bytes) -> Path:
    """裸 PCM 落成 16k 单声道 wav——识别插件吃的是文件（契约如此）。"""
    descriptor, name = tempfile.mkstemp(suffix=".wav")
    # mkstemp 给的是**开着的**句柄：wav 自会按路径再开一份，这份得立刻关掉，
    # 否则 Windows 上删临时文件会遇到"另一个程序正在使用"。
    os.close(descriptor)
    path = Path(name)
    try:
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(TARGET_RATE)
            writer.writeframes(pcm)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path
