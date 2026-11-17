"""有声书的 AI 任务表：加一种用法 = 加一个类并登记在这里。"""

from __future__ import annotations

from app.producers.builtin.audiobook.assist.regex_rule import RegexTask
from app.producers.builtin.audiobook.assist.voice_assign import VoiceAssignTask
from app.usecase.assist import AssistTask

__all__ = ["TASKS"]

TASKS: dict[str, AssistTask] = {
    task.key: task for task in (RegexTask(), VoiceAssignTask())
}
