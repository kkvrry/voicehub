"""实时对话的数据面：角色、背景图与聊天记录。

**不在这里碰模型**：对话走宿主的 ``/api/v1/llm/chat``，合成走 ``/api/v1/synthesize``，
识别走 ``/api/v1/asr/stream``。本插件只保存"和谁聊、长什么样、聊过什么"，
所以换一家模型或一条音色都不需要改这里。

状态是一份 JSON，单进程单用户；写入用唯一临时名 + ``os.replace``（``atomic.write_bytes``），
读不到或坏掉时把坏文件留在旁边再回到默认，避免一次坏 JSON 让整页打不开。
"""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.infra import atomic
from app.producers.base import ProducerError

__all__ = [
    "AUDIO_URL_PREFIX",
    "BACKGROUND_SUFFIXES",
    "BUILTIN_BACKGROUNDS",
    "BUILTIN_PORTRAITS",
    "DEFAULT_BACKGROUND",
    "MAX_AUDIO_BYTES",
    "MAX_BACKGROUND_BYTES",
    "MAX_MESSAGES",
    "RealtimeChatService",
]

#: 内置背景四张（书房、雨夜、晨光、霓虹），立绘十一张（各家模型拟人，
#: 外加 Claude 的 Q 版与 GLM 的轮椅版两张备选稿），都随插件包分发；
#: 空值表示"用第一张背景 / 不站人"。
BUILTIN_BACKGROUNDS = ("study", "rain", "dawn", "neon")
DEFAULT_BACKGROUND = ""
BUILTIN_PORTRAITS = (
    "chatgpt",
    "claude",
    "claude-chibi",
    "copilot",
    "deepseek",
    "gemini",
    "glm",
    "glm-wheelchair",
    "kimi",
    "minimax",
    "qwen",
)
#: 一张背景图的上限：它是壁纸，不是素材库。
MAX_BACKGROUND_BYTES = 8 * 1024 * 1024
BACKGROUND_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
#: 一条回复语音的上限：这是聊天台词，不是素材库。
MAX_AUDIO_BYTES = 32 * 1024 * 1024
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".webm"}
#: 回复语音的地址前缀：状态里只留指向本插件数据目录的地址。
AUDIO_URL_PREFIX = "/api/v1/producers/realtime_chat/audio/"
#: 一份记录保留的最近消息条数。太长既占文件，也会把模型上下文撑爆。
MAX_MESSAGES = 60
#: 一个角色最多留几份记录：记录是"聊过的那几次"，不是归档系统。
MAX_SESSIONS = 30
#: 状态形状的版本：2 起是「一个角色下多份记录」，1 是「一个角色一条消息数组」。
STATE_VERSION = 2
#: 角色数量上限：这是聊天页，不是角色卡管理器。
MAX_CHARACTERS = 12
MAX_NAME = 24
#: 合成引擎的「自动」档：每次合成都从这条音色可用的**已开启本地引擎**里挑，
#: 云端引擎不参与自动（它按量计费，不能悄悄被选中）。
ENGINE_AUTO = "auto"
#: 样例策略：``emotion`` 按情绪匹配（认不出回落主样例），``fixed`` 钉住一条。
SAMPLE_MODES = ("emotion", "fixed")
MAX_SAMPLE_FILE = 240
#: 名牌上的一句话介绍：这是给人看的，短就好；人设是给模型看的，不必挤进名牌。
MAX_INTRO = 160
MAX_PERSONA = 1200
MAX_MESSAGE = 2000
MAX_URL = 2000
MAX_VOICE_ID = 400
MAX_ENGINES = 8

#: 立绘的默认站位与大小：靠左站（``x`` 是离左沿的比例），整幅高度铺满房间。
PORTRAIT_X = 0.02
PORTRAIT_Y = 0.0
PORTRAIT_SCALE = 1.0
#: 拖动与滚轮缩放的范围：出了这个范围，人要么跑出画框，要么小得看不清。
PORTRAIT_X_RANGE = (-0.05, 0.72)
PORTRAIT_Y_RANGE = (0.0, 0.45)
PORTRAIT_SCALE_RANGE = (0.6, 1.25)

DEFAULT_PERSONA = (
    "你是小暖，一个温暖、自然、口语化的聊天伙伴。"
    "回答保持一两句话，像真的在说话，不要写动作、旁白或括号说明。"
)


def _new_session_id() -> str:
    return f"rec-{uuid.uuid4().hex[:8]}"


def _welcome() -> dict[str, Any]:
    return {
        "id": f"welcome-{uuid.uuid4().hex[:8]}",
        "role": "assistant",
        "text": "晚上好，我在。你可以直接打字，也可以点麦克风说话。",
        "audio_url": "",
        "created": int(time.time() * 1000),
    }


def _new_session(messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": _new_session_id(),
        "created": int(time.time() * 1000),
        "messages": list(messages or []),
    }


def _default_state() -> dict[str, Any]:
    session = _new_session([_welcome()])
    return {
        "version": STATE_VERSION,
        "active_id": "xiaonuan",
        "llm_provider": "",
        "llm_model": "",
        "characters": [
            {
                "id": "xiaonuan",
                "name": "小暖",
                "intro": "旧书店的夜班。话不多，只把话接住。",
                "persona": DEFAULT_PERSONA,
                "voice_id": "",
                "voice_name": "",
                "voice_engine": "",
                "voice_engines": [],
                "voice_cover": "",
                "voice_local": False,
                "voice_sample_mode": "emotion",
                "voice_sample_file": "",
                "background": "",
                "portrait": "",
                "portrait_x": PORTRAIT_X,
                "portrait_y": PORTRAIT_Y,
                "portrait_scale": PORTRAIT_SCALE,
                "sessions": [session],
                "active_session": session["id"],
            }
        ],
    }


def _text(value: Any, limit: int, default: str = "") -> str:
    """收成一段不超长的文本；``None`` / 非字符串按空串。"""
    if value is None:
        return default
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:limit] if value else default


def _created(value: Any) -> int:
    """毫秒时间戳：数字、数字字符串都收，缺失或非法回当前时间。

    旧状态里 ``created`` 可能是手写的 ``"昨天"`` 或空串。统一在这里兜住，
    不让一次坏字段把整份状态打不开。
    """
    if isinstance(value, bool):
        return int(time.time() * 1000)
    if isinstance(value, (int, float)):
        try:
            stamp = int(value)
        except (OverflowError, ValueError):
            # JSON 里的 Infinity / NaN 也是"数字"，但换不成时间戳。
            return int(time.time() * 1000)
    elif isinstance(value, str):
        try:
            stamp = int(value.strip())
        except ValueError:
            return int(time.time() * 1000)
    else:
        return int(time.time() * 1000)
    return stamp if stamp > 0 else int(time.time() * 1000)


def _clean_id(value: Any) -> str:
    raw = _text(value, 64)
    if not raw:
        return ""
    return "".join(ch for ch in raw if ch.isalnum() or ch in "-_")[:48]


def _image_ok(raw: bytes) -> bool:
    """只验文件头：这里不是图片解码器，够挡住明显的非图片上传。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if raw.startswith(b"\xff\xd8\xff"):
        return True
    return len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def _ratio(value: Any, default: float, bounds: tuple[float, float]) -> float:
    """比例字段：只收有限数字，越界夹回边界，坏值回默认。

    立绘的站位与大小是拖动出来的，前端会频繁写；这里不信任任何输入，
    免得一个 NaN 让 ``left: NaN%`` 把整张图挤出画框。
    """
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    low, high = bounds
    return round(max(low, min(high, number)), 4)


class RealtimeChatService:
    """一个数据目录对应一份聊天状态。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.backgrounds_dir = self.root / "backgrounds"
        self.backgrounds_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = self.root / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self._lock = threading.RLock()
        self._state: dict[str, Any] | None = None

    # ---- 状态 ---------------------------------------------------------- #

    def state(self) -> dict[str, Any]:
        """整份状态；返回副本，调用方改不到服务里的那一份。"""
        with self._lock:
            if self._state is None:
                self._state = self._load()
            return copy.deepcopy(self._state)

    def save(self, payload: Any) -> dict[str, Any]:
        """整份替换。前端是唯一写方；规范化后落盘并回同一份。"""
        normalized = self._normalize(payload)
        with self._lock:
            self._state = normalized
            self._write(normalized)
            return copy.deepcopy(normalized)

    def _load(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return _default_state()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self._park_bad_file()
            return _default_state()
        try:
            return self._normalize(raw)
        except ValueError:
            # 规范化本身遇到"收不进来的形状"（例如旧版本留下的类型错误）：
            # 与坏 JSON 同一处置，留档后回落默认，不让整页打不开。
            self._park_bad_file()
            return _default_state()

    def _park_bad_file(self) -> None:
        """坏状态不静默覆盖：留在旁边，用户还能找回手写的角色/记录。"""
        target = self.state_path.with_name(
            f"state.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
        )
        try:
            self.state_path.replace(target)
        except OSError:
            pass

    def _write(self, state: dict[str, Any]) -> None:
        text = json.dumps(state, ensure_ascii=False, indent=2)
        atomic.write_bytes(self.state_path, text.encode("utf-8"))

    # ---- 背景 ---------------------------------------------------------- #

    def save_background(self, raw: bytes, filename: str) -> dict[str, str]:
        """存一张自定义背景，返回状态里要记的键与文件名。"""
        suffix = Path(filename or "").suffix.lower()
        if suffix not in BACKGROUND_SUFFIXES:
            raise ProducerError("背景只接受 PNG / JPG / WebP 图片。")
        if not raw:
            raise ProducerError("背景图片是空的。")
        if len(raw) > MAX_BACKGROUND_BYTES:
            raise ProducerError(
                f"背景图片超过 {MAX_BACKGROUND_BYTES // (1024 * 1024)} MB，先压缩一下。"
            )
        if not _image_ok(raw):
            raise ProducerError("这个文件不是可识别的 PNG / JPG / WebP 图片。")
        name = f"bg-{uuid.uuid4().hex[:12]}{suffix}"
        atomic.write_bytes(self.backgrounds_dir / name, raw)
        return {"key": f"custom:{name}", "name": name}

    def background_file(self, name: str) -> Path:
        """只认背景目录里的文件名；不接受路径分隔符与 ``..``。"""
        clean = Path(name).name
        if not clean or clean != name:
            raise ProducerError("找不到这张背景。")
        target = self.backgrounds_dir / clean
        if not target.is_file():
            raise ProducerError("这张背景已经不在了，重新上传一张。")
        return target

    # ---- 回复语音 ------------------------------------------------------ #

    def save_audio(self, raw: bytes, filename: str) -> dict[str, str]:
        """存一条回复的语音，返回状态里要记的地址与文件名。

        合成走 ``persist="none"``：宿主的缓存与生成目录都不留副本，音频只归
        这份聊天记录。删记录 / 删角色时由前端调删除接口把它一起收走。
        """
        suffix = Path(filename or "").suffix.lower()
        if suffix not in AUDIO_SUFFIXES:
            raise ProducerError("语音格式不认识，换一段 WAV / MP3 再试。")
        if not raw:
            raise ProducerError("合成的语音是空的。")
        if len(raw) > MAX_AUDIO_BYTES:
            raise ProducerError(f"合成的语音超过 {MAX_AUDIO_BYTES // (1024 * 1024)} MB，不能保存。")
        name = f"reply-{uuid.uuid4().hex[:12]}{suffix}"
        atomic.write_bytes(self.audio_dir / name, raw)
        return {"name": name, "url": f"{AUDIO_URL_PREFIX}{name}"}

    def audio_file(self, name: str) -> Path:
        """只认语音目录里的文件名；不接受路径分隔符与 ``..``。"""
        clean = Path(name).name
        if not clean or clean != name:
            raise ProducerError("找不到这段语音。")
        target = self.audio_dir / clean
        if not target.is_file():
            raise ProducerError("这段语音已经不在了。")
        return target

    def delete_audio(self, name: str) -> bool:
        """删一条回复语音。已经不在了也算删掉——调用方不需要分辨是谁先收走的。"""
        clean = Path(name).name
        if not clean or clean != name:
            raise ProducerError("找不到这段语音。")
        try:
            (self.audio_dir / clean).unlink()
            deleted = True
        except FileNotFoundError:
            deleted = False
        except OSError as exc:
            raise ProducerError("这段语音删不掉，可能正被播放。") from exc
        self._forget_audio_in_state(clean)
        return deleted

    def _forget_audio_in_state(self, name: str) -> None:
        """内存里已经加载的状态同步摘掉这个地址，避免回包再给出一个 404。"""
        url = f"{AUDIO_URL_PREFIX}{name}"
        with self._lock:
            if self._state is None:
                return
            changed = False
            for char in self._state.get("characters") or []:
                for session in char.get("sessions") or []:
                    for message in session.get("messages") or []:
                        if message.get("audio_url") == url:
                            message["audio_url"] = ""
                            changed = True
            if changed:
                self._write(self._state)

    def _audio_url(self, value: Any) -> str:
        """只留指向本插件语音目录、且文件还在的地址。

        合成历史里可能有全局缓存地址或已被删掉的回复；留下它们只会在播放时
        换来一个必然的 404。读与写都走这一处。
        """
        raw = _text(value, MAX_URL)
        if not raw.startswith(AUDIO_URL_PREFIX):
            return ""
        name = raw[len(AUDIO_URL_PREFIX) :]
        clean = Path(name).name
        if not clean or clean != name:
            return ""
        return raw if (self.audio_dir / clean).is_file() else ""

    # ---- 规范化 -------------------------------------------------------- #

    def _background_value(self, value: Any) -> str:
        """只认四张内置背景里的一张、或存在的那张自定义图。

        旧版本留下的内置键（``builtin:scene`` / ``builtin:code`` / ``mist`` …）
        已经没有对应素材，一律落回空（= 默认的第一张），不再掉进一块纯黑底。
        """
        raw = _text(value, 300)
        if raw.startswith("custom:"):
            name = raw.split(":", 1)[1]
            clean = Path(name).name
            if clean and clean == name and (self.backgrounds_dir / clean).is_file():
                return f"custom:{clean}"
            return DEFAULT_BACKGROUND
        if raw.startswith("builtin:"):
            key = raw.split(":", 1)[1]
            return f"builtin:{key}" if key in BUILTIN_BACKGROUNDS else DEFAULT_BACKGROUND
        return DEFAULT_BACKGROUND

    def _portrait_value(self, value: Any) -> str:
        """立绘只能取插件包里的那几张；没选或点了不认识的就是"不站人"。"""
        raw = _text(value, 120)
        if raw.startswith("builtin:"):
            key = raw.split(":", 1)[1]
            return f"builtin:{key}" if key in BUILTIN_PORTRAITS else ""
        return ""

    def _normalize(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return _default_state()
        raw_chars = payload.get("characters")
        if not isinstance(raw_chars, list) or not raw_chars:
            return _default_state()

        characters: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        for index, raw in enumerate(raw_chars[:MAX_CHARACTERS]):
            if not isinstance(raw, dict):
                continue
            char_id = _clean_id(raw.get("id")) or f"char-{uuid.uuid4().hex[:8]}"
            if char_id in used_ids:
                char_id = f"{char_id}-{index + 1}"
            used_ids.add(char_id)
            characters.append(self._normalize_character(char_id, raw))
        if not characters:
            return _default_state()

        active = _clean_id(payload.get("active_id"))
        if active not in used_ids:
            active = characters[0]["id"]
        provider, model = self._model_choice(payload, raw_chars)
        return {
            "version": STATE_VERSION,
            "active_id": active,
            "llm_provider": provider,
            "llm_model": model,
            "characters": characters,
        }

    def _model_choice(self, payload: dict[str, Any], raw_chars: list[Any]) -> tuple[str, str]:
        """对话模型挂在整份状态上，不挂在角色上：换角色不该换模型。

        旧状态把这两个字段写在角色里，读进来时把第一处非空值搬上来，用户选过的
        那家模型不丢；之后再写就只认顶层这一份。
        """
        provider = _text(payload.get("llm_provider"), 120)
        model = _text(payload.get("llm_model"), 200)
        if provider or model:
            return provider, model
        for item in raw_chars:
            if not isinstance(item, dict):
                continue
            provider = _text(item.get("llm_provider"), 120)
            model = _text(item.get("llm_model"), 200)
            if provider or model:
                return provider, model
        return "", ""

    def _normalize_character(self, char_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        name = _text(raw.get("name"), MAX_NAME, "角色")
        engines: list[str] = []
        for item in raw.get("voice_engines") or []:
            key = _text(item, 80)
            if key and key not in engines:
                engines.append(key)
            if len(engines) >= MAX_ENGINES:
                break
        voice_id = _text(raw.get("voice_id"), MAX_VOICE_ID)
        engine = _text(raw.get("voice_engine"), 80)
        if not engines:
            engine = ""
        elif engine == ENGINE_AUTO:
            # 「自动」不是某台引擎的键，留着让前端每次按"现在开着哪台"解析。
            pass
        elif not engine:
            # 老状态有音色却没有引擎（以前只在两台以上时才让选）：默认自动。
            engine = ENGINE_AUTO if voice_id else ""
        elif engine not in engines:
            # 旧状态里可能留着已停用 / 写错的键：落到这条音色可用的第一台。
            engine = engines[0]

        mode = _text(raw.get("voice_sample_mode"), 16, "emotion")
        if mode not in SAMPLE_MODES:
            mode = "emotion"
        sample_file = _text(raw.get("voice_sample_file"), MAX_SAMPLE_FILE)
        if mode != "fixed":
            sample_file = ""
        elif not sample_file:
            # 钉了一条却没有文件：退回按情绪匹配，不留一个永远失效的档位。
            mode = "emotion"

        sessions = self._normalize_sessions(raw)
        active_session = _clean_id(raw.get("active_session"))
        if active_session not in {row["id"] for row in sessions}:
            active_session = sessions[-1]["id"]
        return {
            "id": char_id,
            "name": name,
            "intro": _text(raw.get("intro"), MAX_INTRO),
            "persona": _text(raw.get("persona"), MAX_PERSONA),
            "voice_id": voice_id,
            "voice_name": _text(raw.get("voice_name"), 80),
            "voice_engine": engine,
            "voice_engines": engines,
            "voice_cover": _text(raw.get("voice_cover"), MAX_URL),
            # 本地音色才有样例可选（固定样例 / 按情绪匹配）；平台音色没有这一层。
            "voice_local": raw.get("voice_local") is True,
            "voice_sample_mode": mode,
            "voice_sample_file": sample_file,
            "background": self._background_value(raw.get("background")),
            "portrait": self._portrait_value(raw.get("portrait")),
            "portrait_x": _ratio(raw.get("portrait_x"), PORTRAIT_X, PORTRAIT_X_RANGE),
            "portrait_y": _ratio(raw.get("portrait_y"), PORTRAIT_Y, PORTRAIT_Y_RANGE),
            "portrait_scale": _ratio(
                raw.get("portrait_scale"), PORTRAIT_SCALE, PORTRAIT_SCALE_RANGE
            ),
            "sessions": sessions,
            "active_session": active_session,
        }

    def _normalize_sessions(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """一个角色下的多份记录。

        旧状态（版本 1）只有一条 ``messages``：原样收成一份记录，不丢聊到一半的内容。
        记录全空时补一份空的——前端只认"至少有一份"，比处处判空稳。
        """
        rows: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        raw_sessions = raw.get("sessions")
        source = raw_sessions if isinstance(raw_sessions, list) else None
        if source:
            for item in source[-MAX_SESSIONS:]:
                if not isinstance(item, dict):
                    continue
                session_id = _clean_id(item.get("id")) or _new_session_id()
                if session_id in used_ids:
                    session_id = _new_session_id()
                used_ids.add(session_id)
                created = _created(item.get("created"))
                rows.append(
                    {
                        "id": session_id,
                        "created": created,
                        "messages": self._normalize_messages(item.get("messages")),
                    }
                )
        if not rows:
            rows.append(
                _new_session(self._normalize_messages(raw.get("messages")))
            )
        return rows

    def _normalize_messages(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        rows: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        for item in raw[-MAX_MESSAGES:]:
            if not isinstance(item, dict):
                continue
            role = _text(item.get("role"), 16)
            if role not in {"user", "assistant"}:
                continue
            text = _text(item.get("text"), MAX_MESSAGE)
            if not text:
                continue
            message_id = _clean_id(item.get("id")) or f"msg-{uuid.uuid4().hex[:8]}"
            if message_id in used_ids:
                message_id = f"msg-{uuid.uuid4().hex[:8]}"
            used_ids.add(message_id)
            row: dict[str, Any] = {
                "id": message_id,
                "role": role,
                "text": text,
                "audio_url": self._audio_url(item.get("audio_url")),
                "created": _created(item.get("created")),
            }
            if item.get("error"):
                row["error"] = True
            if item.get("audio_error"):
                row["audio_error"] = _text(item.get("audio_error"), 300)
            notes = item.get("notes")
            if isinstance(notes, list):
                kept = [_text(note, 200) for note in notes if _text(note, 200)]
                if kept:
                    row["notes"] = kept[:10]
            rows.append(row)
        return rows
