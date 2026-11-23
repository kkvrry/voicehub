"""生产方插件契约：把一条"文本 → 成品"的流水线接进 VoiceHub。

生产方与 TTS 引擎、对话模型是三种不同的插件域：

- 引擎：``文本 → 一段音频``；
- 对话模型：``消息 → 消息``；
- 生产方：``源文本 → 章节 → 发声单元 → 音频 → 成品``，自带步骤、项目数据与工作区。

三者共用同一套宿主规则：一个插件一个目录、自声明配置模型、读时掩码、
配置存本机、插件自己提供图标与路由。**依赖方向单向**：插件依赖本模块，
宿主不认识任何具体生产方；移除插件目录即移除该功能。
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar

from fastapi import APIRouter
from pydantic import BaseModel

__all__ = ["ProducerError", "ProducerPlugin", "ProducerStep"]


class ProducerError(RuntimeError):
    """生产方业务错误：面向用户的失败（源文本为空、章节未拆、音色未分配）。

    宿主把它翻成 400 + 一句话，不是 500——这类失败是用户下一步要处理的事，
    不是程序坏了。
    """

    code = "producer_error"


@dataclass(frozen=True, slots=True)
class ProducerStep:
    """流水线上的一个步骤。

    :param key: 稳定标识，项目数据里记这一列。
    :param label: 步骤名，只写动作（分章 / 拆句 / 分析 / 生成 / 合并）。
    :param note: 一句话说明输入与输出。
    :param unit: 作用单位：``project`` / ``chapter`` / ``row``。
    """

    key: str
    label: str
    note: str
    unit: str = "project"


class ProducerPlugin(ABC):
    """一个生产方插件。"""

    #: 稳定 key，同时是数据目录名与路由段。
    key: ClassVar[str]
    #: 导航与工作区标题。
    label: ClassVar[str]
    #: 插件卡片上的一句话说明；空 = 卡片不写说明。
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1.0.0"
    #: 设置页分组的标题；空 = 用 ``label``。
    settings_label: ClassVar[str] = ""
    homepage: ClassVar[str] = ""
    #: 导航顺序，小的在前。
    order: ClassVar[int] = 100
    #: 导航分组：``main`` = 日常页面（音色库 / 生成 / 各生产方 / 音频库）；
    #: ``tools`` = 下半段工具组（引擎 / 日志）。运行时管理这类工具型工作区放 ``tools``。
    nav_section: ClassVar[str] = "main"
    #: 配置里含"指定并执行本机程序"这类字段：保存只认本机来源。默认不设限。
    settings_local_only: ClassVar[bool] = False
    #: 工作区左侧是否需要宿主那条音色栏。工具型插件（如 audio.cpp）不需要。
    voice_rail: ClassVar[bool] = True
    #: 音色栏的用法：``assign`` = 卡片右端「指定」把音色交给工作区（创作型），
    #: ``pick`` = 点卡即选中、卡右「选样例」挑一条（处理型），
    #: ``create`` = 卡右「创建」用这条音色新建一个角色（角色对话）。
    #: 宿主只按这个字段摆栏，不认识具体插件。
    rail_mode: ClassVar[str] = "assign"

    @classmethod
    def settings_schema(cls) -> type[BaseModel] | None:
        """配置模型；``None`` = 这个插件没有配置项。"""
        return None

    def steps(self) -> tuple[ProducerStep, ...]:
        """流水线步骤，按执行顺序。空 = 插件只提供工作区，不声明步骤。"""
        return ()

    def readiness(self, cfg: BaseModel | None) -> str:
        """没就绪的原因；空串 = 就绪。界面只显示这一句，不猜。"""
        return ""

    def current_settings(self) -> BaseModel | None:
        """读当前配置。由宿主实现；延迟导入避免契约与用例模块循环。"""
        from app.usecase.producers import settings_for

        return settings_for(self)

    def icon_path(self) -> Path | None:
        """插件目录下的 ``icon.svg``（缺省自动找）。"""
        module = sys.modules.get(type(self).__module__)
        file = getattr(module, "__file__", "") or ""
        path = Path(file).parent / "icon.svg" if file else None
        return path if path and path.is_file() else None

    def icon_svg(self) -> str:
        path = self.icon_path()
        return path.read_text(encoding="utf-8") if path else ""

    @abstractmethod
    def service(self, root: Path) -> Any:
        """按数据根目录建服务对象；一个插件只有一个实例。"""

    @abstractmethod
    def router(self, service: Any) -> APIRouter:
        """插件自己的接口，由宿主挂在 ``/api/v1/producers/<key>`` 下。"""

    def settings_extras(self) -> dict[str, dict[str, Any]]:
        """配置字段的动态补写：字段名 → 并进 schema 属性的一包键值；空 = 没有要现算的。

        schema 里的 ``placeholder`` 是写死的；「留空后实际落在哪」「能不能直接打开」
        要看当前配置与数据目录才知道，插件在这里现算，宿主出包时写回 schema。
        前端只认 ``placeholder`` 与 ``open`` 两个键（``open`` = 字段指向的目录可打开）。
        """
        return {}

    def open_settings_field(self, field: str) -> Path:
        """打开某个配置字段指向的目录；不支持的字段抛 ``KeyError``。"""
        raise KeyError(field)

    def describe(self) -> dict[str, Any]:
        """自描述：界面据此画导航、步骤条与设置分组，不写任何分支。"""
        schema_cls = self.settings_schema()
        schema = schema_cls.model_json_schema() if schema_cls else None
        for field, extra in self.settings_extras().items():
            prop = (schema or {}).get("properties", {}).get(field)
            if prop is not None:
                prop.update(extra)
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "version": self.version,
            "homepage": self.homepage,
            "order": self.order,
            # 导航落哪一组：宿主只按这个字段摆位置，不认识具体插件。
            "nav_section": self.nav_section,
            # 工作区左侧要不要音色栏：插件自描述，宿主不按 key 写分支。
            "voice_rail": self.voice_rail,
            # 栏的用法（指定 / 选中）：同上，宿主只认这两个值。
            "rail_mode": self.rail_mode,
            "icon_url": f"/api/v1/producers/{self.key}/icon",
            "steps": [asdict(step) for step in self.steps()],
            "settings_schema": schema,
            "settings_group": {
                "key": f"producer:{self.key}",
                "label": self.settings_label or self.label,
            },
        }
