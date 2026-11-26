"""有声书生产方插件：目录即插件，移除目录即移除功能。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.infra.config import data_dir
from app.producers.base import ProducerPlugin, ProducerStep
from app.producers.builtin.audiobook import ai_prompts
from app.producers.builtin.audiobook.api import build_router
from app.producers.builtin.audiobook.pipeline import AudiobookService
from app.producers.builtin.audiobook.settings import SETTINGS_PAGE_GROUPS, AudiobookSettings

__all__ = ["AudiobookProducer"]


class AudiobookProducer(ProducerPlugin):
    key = "audiobook"
    label = "创作"
    description = "导入全书、分章拆句、对白分析、逐行合成与合并。"
    version = "0.1.0"
    settings_label = "创作"
    order = 10

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return AudiobookSettings

    def steps(self) -> tuple[ProducerStep, ...]:
        return (
            ProducerStep("chapters", "分章", "按选定模式把原文切成章节，可先预览。"),
            ProducerStep("sentences", "拆句", "章节正文切分为发声单元，标注叙述与对白。"),
            ProducerStep("analysis", "分析", "推断对白的说话人与情绪，可人工修正。"),
            ProducerStep("audio", "生成", "按角色音色逐行合成，音色取自 VoiceHub 音色库。"),
            ProducerStep("qc", "预检", "回读已生成的音频，找出念错、漏读与截断；只提示，不改状态。"),
            ProducerStep("merge", "合并", "按章节顺序拼接逐行音频，输出成品音频。"),
        )

    def service(self, root: Path) -> Any:
        return AudiobookService(root, self.current_settings)

    def _projects_root(self) -> Path:
        """「项目目录」留空时的实际落点：与 AudiobookService.projects_root 同一条规则。
        describe 时还没有 service，这里照算一份（只读配置，不建表不迁移）。"""
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
        # BGM 目录只在配过之后才可打开：空串不是路径，按钮不该摆出来。
        bgm = str(getattr(self.current_settings(), "bgm_folder", "") or "").strip()
        return {"project_dir": extra, "bgm_folder": {"open": True} if bgm else {}}

    def open_settings_field(self, field: str) -> Path:
        if field == "project_dir":
            return self._projects_root()
        if field == "bgm_folder":
            settings = self.current_settings()
            folder = str(getattr(settings, "bgm_folder", "") or "").strip()
            if not folder:
                raise KeyError(field)
            return Path(folder).expanduser()
        raise KeyError(field)

    def router(self, service: Any) -> APIRouter:
        return build_router(service)

    def after_library_scan(self, runtime: Any, library: Any) -> None:
        """扫描后自愈：本地音色目录移动会让旧 voice_id 失效，按唯一同名重绑。"""
        try:
            service = runtime.get()
        except Exception:  # noqa: BLE001 - 停用的插件不参与自愈
            return
        from app.producers.builtin.audiobook.rebind import rebind_stale_voices

        rebind_stale_voices(service.store, library)

    def describe(self) -> dict[str, Any]:
        """在通用自描述之外补这一家的生效提示词预设与设置页可见分组。"""
        return {
            **super().describe(),
            "analysis_presets": ai_prompts.presets_payload(),
            "prompt_groups": ai_prompts.groups_meta(),
            "settings_page_groups": list(SETTINGS_PAGE_GROUPS),
        }
