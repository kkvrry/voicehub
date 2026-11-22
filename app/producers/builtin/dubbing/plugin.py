"""配音生产方插件：音视频 → 字幕 → 逐段配音 → 成片。目录即插件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.infra.config import data_dir
from app.producers.base import ProducerPlugin, ProducerStep
from app.producers.builtin.dubbing.api import build_router
from app.producers.builtin.dubbing.service import STEP_MANIFEST, DubbingService
from app.producers.builtin.dubbing.settings import SETTINGS_PAGE_GROUPS, DubbingSettings

__all__ = ["DubbingProducer"]


class DubbingProducer(ProducerPlugin):
    key = "dubbing"
    label = "配音"
    description = "导入音视频，转写字幕，按角色逐段配音并封装成片。"
    version = "0.2.0"
    settings_label = "配音"
    order = 20
    #: 左侧音色栏是挑音色的入口：卡片右端「指定」把这条音色交给当前角色。
    #: 工作区不再自己维护一份音色清单，搜索框、音色树与播放条也跟着主壳走。
    rail_mode = "assign"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return DubbingSettings

    def steps(self) -> tuple[ProducerStep, ...]:
        # 与项目详情里的步骤卡共用一份清单，标签与说明不会各写一套。
        return tuple(ProducerStep(*item) for item in STEP_MANIFEST)

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
