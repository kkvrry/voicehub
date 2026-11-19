"""音色处理插件：分离 → 降噪 → 规整，试听后替换样例音频。"""

from app.producers.builtin.voiceclean.plugin import VoiceClean

__all__ = ["VoiceClean"]
