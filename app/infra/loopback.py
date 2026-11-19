"""系统输出内录（WASAPI loopback）：录"电脑正在播放的声音"。

浏览器没有内录 API，麦克风也拿不到其他程序的输出，所以这一层只能在本机做。
采集走 ``soundcard`` 的 loopback 设备，与播放共用同一条输出链路：

- **只录默认输出设备**，不做设备选择：多设备选择属于设置项，当前无界面承载。
- **静音时录到的是纯静音**，不是失败。WASAPI 的 loopback 在系统静音或音量为 0 时
  仍持续出帧，因此 :meth:`Recorder.stop` 返回 ``peak``，由调用方判定"有没有声音"。
- **采样率 48 kHz、单声道、PCM16**：与输出链路一致，不重采样；参考音频链路的
  16 kHz 是识别与克隆用的另一套口径，不在这里套用。
- 边录边写盘，内存占用与时长无关；超过上限自动收尾，避免长时间挂着写满磁盘。

采集线程独占设备与文件句柄：:meth:`Recorder.start` 只等"设备已打开"这一信号，
之后的一切读写都在那个线程里，主线程不持有半开状态。
"""

from __future__ import annotations

import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from app.infra import recordings

__all__ = ["MAX_SECONDS", "SAMPLE_RATE", "LoopbackError", "Recorder", "available"]

#: 采集采样率：与 Windows 共享混音口径一致。
SAMPLE_RATE = 48000

#: 单次内录上限（分钟）。到点自动收尾，文件保留。
MAX_SECONDS = 60 * 60

#: 每次向设备要的帧数（0.1 秒）。块太小空转多，太大停止时最多丢 0.1 秒。
_BLOCK_SECONDS = 0.1


class LoopbackError(RuntimeError):
    """采集不可用或中途失败；``str(exc)`` 可直接作为界面说明。"""


def available() -> tuple[bool, str]:
    """这台机器现在能不能内录，返回 ``(可用, 不可用原因)``。

    只做静态判定（平台、依赖、输出设备），不打开设备：开关状态与错误提示要能
    在录制之前给出，不能靠"点了才知道"。
    """
    import sys

    if sys.platform != "win32":
        return False, "内录只在 Windows 上可用。"
    try:
        import soundcard  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺依赖与导入期异常同一种处置
        return False, "缺少内录组件（soundcard）。"
    try:
        import soundcard as sc

        speaker = sc.default_speaker()
    except Exception as exc:  # noqa: BLE001 - 驱动异常不该抛到接口层
        return False, f"读取输出设备失败：{exc}"
    if speaker is None:
        return False, "没有可用的输出设备。"
    return True, ""


def _open_loopback() -> Any:
    """打开默认输出设备的 loopback 通道；打不开抛 :class:`LoopbackError`。"""
    import soundcard as sc

    speaker = sc.default_speaker()
    if speaker is None:
        raise LoopbackError("没有可用的输出设备。")
    try:
        return sc.get_microphone(speaker.name, include_loopback=True)
    except Exception as exc:  # 统一换成一句可读的原因
        raise LoopbackError(f"打开系统输出失败：{exc}") from exc


def _target() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = recordings.root()
    path = folder / f"内录-{stamp}.wav"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"内录-{stamp}-{n}.wav"
    return path


class Recorder:
    """一次内录。实例一次性使用：``start()`` → ``stop()``，不复用。

    :param samplerate: 采集采样率，默认 :data:`SAMPLE_RATE`。
    :param max_seconds: 上限，到时自动停止；文件照常保留。
    """

    def __init__(
        self, *, samplerate: int = SAMPLE_RATE, max_seconds: float = MAX_SECONDS
    ) -> None:
        self.samplerate = samplerate
        self.max_seconds = max_seconds
        self._frames = max(1, int(samplerate * _BLOCK_SECONDS))
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._path: Path | None = None
        self._error = ""
        self._started_at = 0.0
        self._stopped_at = 0.0
        self._count = 0
        self._peak = 0.0

    # -- 对外 ------------------------------------------------------------- #

    def start(self) -> Path:
        """开始采集，返回目标文件路径。设备打不开时抛 :class:`LoopbackError`。"""
        path = _target()
        self._path = path
        self._thread = threading.Thread(target=self._run, name="voicehub-capture", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            raise LoopbackError("打开系统输出超时。")
        if self._error:
            raise LoopbackError(self._error)
        return path

    def stop(self) -> dict[str, Any]:
        """停止采集并等待文件收尾，返回 ``{file, seconds, bytes, peak, truncated}``。

        没开始、已停止或采集线程已自行退出时返回最后一次的统计；文件为空则删除。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)
        if self._error:
            raise LoopbackError(self._error)
        path = self._path
        if path is None:
            raise LoopbackError("没有正在进行的录制。")
        size = path.stat().st_size if path.exists() else 0
        if self._count <= 0 or size <= 44:
            path.unlink(missing_ok=True)
            raise LoopbackError("没有录到声音。")
        return {
            "file": str(path),
            "seconds": round(self._count / self.samplerate, 2),
            "bytes": size,
            "peak": round(self._peak, 4),
            "truncated": self._count >= self.max_seconds * self.samplerate,
        }

    def status(self) -> dict[str, Any]:
        """进行中的统计，供界面与托盘显示；未开始时除 ``recording`` 外均为零值。"""
        recording = self._thread is not None and self._thread.is_alive() and not self._error
        elapsed = (time.monotonic() - self._started_at) if recording else 0.0
        return {
            "recording": recording,
            "seconds": round(elapsed, 1) if recording else 0.0,
            "peak": round(self._peak, 4),
            "file": str(self._path) if self._path else "",
            "error": self._error,
        }

    # -- 采集线程 --------------------------------------------------------- #

    def _run(self) -> None:
        try:
            mic = _open_loopback()
            with mic.recorder(samplerate=self.samplerate, blocksize=self._frames) as rec, wave.open(
                str(self._path), "wb"
            ) as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(self.samplerate)
                self._started_at = time.monotonic()
                self._ready.set()
                while not self._stop.is_set():
                    if time.monotonic() - self._started_at >= self.max_seconds:
                        break
                    # 设备卡住时 record 会阻塞，而它没有超时参数；停止请求只能等这一块
                    # 返回后才能生效——0.1 秒的块让这个延迟可以忽略。
                    self._count += self._write(out, rec.record(numframes=self._frames))
        except Exception as exc:  # noqa: BLE001 - 原因要原样带给调用方
            self._error = str(exc) or exc.__class__.__name__
        finally:
            self._stopped_at = time.monotonic()
            self._ready.set()

    def _write(self, out: wave.Wave_write, data: Any) -> int:
        """把一块浮点采样混成单声道 PCM16 写入，返回写入的帧数。"""
        import numpy as np

        block = np.asarray(data, dtype=np.float32)
        if block.ndim == 2 and block.shape[1] > 1:
            block = block.mean(axis=1)
        else:
            block = block.reshape(-1)
        if block.size == 0:
            return 0
        peak = float(np.abs(block).max())
        self._peak = max(self._peak, peak)
        pcm = np.clip(block, -1.0, 1.0) * 32767.0
        out.writeframes(pcm.astype("<i2").tobytes())
        return int(block.size)
