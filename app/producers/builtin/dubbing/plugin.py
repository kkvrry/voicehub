"""配音生产方插件：音视频 → 字幕 → 逐段配音 → 成片。目录即插件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.infra.config import data_dir
from app.producers.base import ProducerPlugin, ProducerStep
from app.producers.builtin.dubbing.api import build_router
from app.producers.builtin.dubbing.service import DubbingService
from app.producers.builtin.dubbing.settings import SETTINGS_PAGE_GROUPS, DubbingSettings

__all__ = ["DubbingProducer"]


class DubbingProducer(ProducerPlugin):
    key = "dubbing"
    label = "配音"
    description = "导入音视频，转写字幕，按角色逐段配音并封装成片。"
    version = "0.1.0"
    settings_label = "配音"
    order = 20
    #: 角色的音色在左列就地选，工作区不需要宿主那条通用音色栏。
    voice_rail = False

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return DubbingSettings

    def steps(self) -> tuple[ProducerStep, ...]:
        return (
            ProducerStep("extract", "抽音轨", "解复用源文件，取出一条完整音轨。"),
            ProducerStep("transcribe", "转录", "按静音切段逐段识别，段落边界即字幕时间码。", "project"),
            ProducerStep("synth", "配音", "按角色逐段合成，只重跑改动过的段。", "row"),
            ProducerStep("assemble", "贴齐", "按时间码补静音、加速贴齐、越界顺延。"),
            ProducerStep("export", "导出", "封装成片并写出字幕。"),
        )

    def service(self, root: Path) -> Any:
        return DubbingService(root, self.current_settings)

    def _projects_root(self) -> Path:
        settings = self.current_settings()
        folder = str(getattr(settings, "project_dir", "") or "").strip()
        return Path(folder).expanduser() if folder else data_dir() / "producers" / self.key / "projects"

    def settings_extras(self) -> dict[str, dict[str, Any]]:
        root = self._projects_root()
        extra: dict[str, Any] = {"placeholder": f"默认：{root}", "open": True}
        try:
            count = sum(1 for item in root.iterdir() if item.is_dir())
        except OSError:
            count = 0
        if count:
            extra["badge"] = f"{count} 个项目"
        return {"project_dir": extra}

    def open_settings_field(self, field: str) -> Path:
        if field == "project_dir":
            return self._projects_root()
        raise KeyError(field)

    def router(self, service: Any) -> APIRouter:
        return build_router(service)

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "settings_page_groups": list(SETTINGS_PAGE_GROUPS),
        }
