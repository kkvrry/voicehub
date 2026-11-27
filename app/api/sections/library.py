"""路由分区「library」：只注册本域的接口；路径前缀与错误约定见 :mod:`app.api.routes`。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app.api.clientaddr import is_local_client
from app.api.errors import ApiError, not_found
from app.api.localonly import require_local
from app.api.schemas import (
    DeleteOut,
    ListResponse,
    TagOut,
    VoiceDetailOut,
    VoiceOut,
)
from app.api.views import (
    DesignPromptIn,
    MainSampleIn,
    PrefsIn,
    SamplePatch,
    VoicePatch,
    _voice_out,
)
from app.domain.models import SOURCE_LOCAL
from app.infra import archive
from app.infra.config import cache_base
from app.infra.shell import open_folder
from app.infra.thumbs import ThumbError, thumbnail
from app.usecase import voice_design
from app.usecase.library import LibraryService

__all__ = ["register"]


def register(router: APIRouter, service: LibraryService, version: str) -> None:
    @router.get("/voices", response_model=ListResponse[VoiceOut], summary="音色列表", tags=["library"])
    def voices(
        source: str = "",
        category: str = "",
        root: str = "",
        gender: str = "",
        age: str = "",
        language: str = "",
        tag: str = "",
        q: str = "",
        design: bool = False,
        limit: int = 0,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按筛选条件列音色。

        :param design: 只看设计音色（左栏那一行）。
        :param limit: 取几条；``0``（默认）= 不分页，一次全给——老界面靠一次拿全量做
            本地筛选，改默认值会让它悄悄少数据。要分页就显式传。
        :param offset: 跳过几条，仅 ``limit > 0`` 时有意义。
        """
        page = max(limit, 0)
        start = max(offset, 0)
        # 多取一条用来判断后面还有没有：比再跑一次 COUNT(*) 便宜，且不受并发写影响。
        found = service.list_voices(
            source=source,
            category=category,
            root=root,
            gender=gender,
            age=age,
            language=language,
            tag=tag,
            q=q,
            design=design,
            limit=page + 1 if page else 0,
            offset=start,
        )
        has_more = bool(page) and len(found) > page
        if has_more:
            found = found[:page]
        # 徽标数据一次算好（"这条是否由某台引擎生成"），规则与左栏根归属同源。
        origins = service.voice_origins(found)
        return {
            "object": "list",
            "data": [_voice_out(v, engine=origins.get(v.id)) for v in found],
            "has_more": has_more,
        }

    @router.get("/tags", response_model=ListResponse[TagOut], summary="标签云", tags=["library"])
    def tags(
        source: str = "",
        category: str = "",
        root: str = "",
        gender: str = "",
        age: str = "",
        language: str = "",
        q: str = "",
        design: bool = False,
    ) -> dict[str, Any]:
        """当前筛选下各标签的剩余数量。

        **刻意不接收 ``tag`` 参数**：已选中某个标签时，这排标签的用途是"改选另一个"，
        因此统计范围必须不受该标签影响。
        """
        return {
            "object": "list",
            "data": [
                {"object": "tag", "tag": tag, "count": count}
                for tag, count in service.tag_cloud(
                    source=source,
                    category=category,
                    root=root,
                    gender=gender,
                    age=age,
                    language=language,
                    q=q,
                    design=design,
                )
            ],
            "has_more": False,
        }

    @router.get(
        "/voices/{voice_id}", response_model=VoiceDetailOut, summary="音色详情",
        tags=["library"],
    )
    def voice_detail(voice_id: str) -> dict[str, Any]:
        voice = service.get_voice(voice_id)
        return _voice_out(
            voice, with_samples=True, engine=service.voice_origins([voice]).get(voice.id)
        )

    @router.delete("/voices/{voice_id}", response_model=DeleteOut, summary="移除音色", tags=["library"])
    def delete_voice(voice_id: str, request: Request) -> dict[str, Any]:
        """移除一条音色。本地音色的文件移入回收站（可还原），不是永久删除。

        仅删除库中记录时，下次扫描会重新发现它，"移除"将失去意义。
        """
        require_local(
            request,
            code="delete_local_only",
            message="删除音色或样例只能在本机使用。",
        )
        result = service.delete_voice(voice_id)
        return {
            "id": voice_id,
            "object": "voice",
            "deleted": True,
            "trashed": result.get("trashed") or [],
        }

    @router.put("/voices/{voice_id}", response_model=VoiceDetailOut, summary="更新音色", tags=["library"])
    def update_voice(voice_id: str, body: VoicePatch) -> dict[str, Any]:
        patch = body.model_dump(exclude_none=True)
        return _voice_out(service.update_voice(voice_id, patch), with_samples=True)

    @router.put(
        "/voices/{voice_id}/samples/{file}",
        tags=["library"],
        response_model=VoiceDetailOut,
        summary="更新样例",
    )
    def update_sample(voice_id: str, file: str, body: SamplePatch) -> dict[str, Any]:
        patch = body.model_dump(exclude_none=True)
        return _voice_out(service.update_sample(voice_id, file, patch), with_samples=True)

    @router.post(
        "/voices/design-prompt", summary="写一句音色描述", tags=["library"],
    )
    async def design_prompt(body: DesignPromptIn) -> dict[str, Any]:
        """按音色名（可带草稿）让对话模型写一句设计描述。

        只回这一句：不建音色、不碰引擎——描述填进输入框，用户改完再提交。
        """
        name = body.name.strip()
        brief = body.brief.strip()
        if not name and not brief:
            raise ApiError(
                status=400,
                code="invalid_value",
                message="先填音色名或一句草稿，再让 AI 写。",
            )
        return {"object": "design_prompt", **await voice_design.suggest(name, brief)}

    @router.post(
        "/voices/{voice_id}/main-sample", response_model=VoiceDetailOut, summary="指定主样例",
        tags=["library"],
    )
    def main_sample(voice_id: str, body: MainSampleIn) -> dict[str, Any]:
        return _voice_out(service.set_main_sample(voice_id, body.file), with_samples=True)

    @router.delete(
        "/voices/{voice_id}/samples/{file}",
        response_model=VoiceDetailOut,
        summary="移除样例",
        tags=["library"],
    )
    def delete_sample(voice_id: str, file: str, request: Request) -> dict[str, Any]:
        """移除一条样例。文件进回收站（可还原），不是永久删除。

        只摘库里的记录是不够的：文件还在原位的话，下次扫描又把它认回来。
        """
        require_local(
            request,
            code="delete_local_only",
            message="删除音色或样例只能在本机使用。",
        )
        return _voice_out(service.delete_sample(voice_id, file), with_samples=True)

    @router.post("/voices/{voice_id}/prefs", response_model=VoiceOut, summary="收藏/隐藏", tags=["library"])
    def prefs(voice_id: str, body: PrefsIn) -> dict[str, Any]:
        return _voice_out(
            service.set_prefs(voice_id, hidden=body.hidden, favorite=body.favorite)
        )

    @router.post("/voices/{voice_id}/reveal", summary="在资源管理器里打开音色目录", tags=["library"])
    def reveal_voice(voice_id: str, request: Request) -> dict[str, Any]:
        """打开这条音色自己的文件夹（服务在本机运行才做得到）。

        平台音色没有文件夹（生成物才落到某台引擎的保存目录），所以这里只认
        ``voice.folder``；没有就明说，别开一个空窗口。

        只认本机来源：界面在别人机器上打开时，这个动作会落到**宿主机**的桌面，
        等于把"弹窗"这个能力交给了任意能访问端口的人。
        """
        if not is_local_client(request):
            raise ApiError(
                status=403,
                code="reveal_local_only",
                message="「打开目录」只能在本机使用。",
            )
        voice = service.get_voice(voice_id)
        if not voice.folder:
            raise ApiError(status=400, code="folder_missing", message="这条音色没有本地文件夹。")
        path = Path(voice.folder)
        if not path.is_dir():
            raise ApiError(status=400, code="folder_missing", message="文件夹已经不在了。")
        try:
            open_folder(path)
        except OSError as exc:
            raise ApiError(
                status=400, code="reveal_failed", message=f"打不开这个目录：{exc}"
            ) from exc
        return {"object": "reveal", "folder": str(path)}

    @router.get("/voices/{voice_id}/reference", summary="参考音频与逐字稿", tags=["library"])
    def reference(voice_id: str) -> dict[str, Any]:
        """主样例的路径、说话内容与情绪——供上游一行请求取全。"""
        return {"object": "reference", **service.reference_of(voice_id)}

    @router.get("/audio/{voice_id}/{file}", summary="音频流", tags=["library"])
    def audio(voice_id: str, file: str) -> FileResponse:
        voice = service.get_voice(voice_id)
        if file not in {s.file for s in voice.samples}:
            raise not_found("sample_not_found", "库中不存在这条音频。")
        if voice.folder:
            target = archive.resolve_within(Path(voice.folder), file)
            if target is not None and target.is_file():
                return FileResponse(str(target))
        # 生成样例：本地音色挂上的那几段躺在引擎保存目录里，不在它自己的扫描目录。
        generated = service.generated_sample_path(voice_id, file)
        if generated is not None and generated.is_file():
            return FileResponse(str(generated))
        raise not_found("sample_not_found", "库中不存在这条音频（文件可能已被移走）。")

    @router.get("/cover/{voice_id}", summary="封面图", tags=["library"])
    def cover(voice_id: str, s: str = "") -> FileResponse:
        voice = service.get_voice(voice_id)
        if not voice.cover or not voice.folder:
            raise not_found("cover_not_found", "该音色没有封面。")
        # 文件可能在扫描之后被删/被移走（重新生成、手工清理）；先确认再交给
        # FileResponse——它找不到文件时抛的是 RuntimeError，会把整个服务带崩。
        target = Path(voice.folder) / voice.cover
        if not target.is_file():
            raise not_found("cover_not_found", "封面文件已不存在。")
        # URL 自带 mtime（?v=），内容一变地址就变：可以放心让浏览器长缓存。
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        if s == "thumb":
            try:
                return FileResponse(
                    str(thumbnail(target, cache_base() / "covers")),
                    media_type="image/webp",
                    headers=headers,
                )
            except ThumbError:
                pass  # 原图打不开（奇怪格式）：回原图，让浏览器自己判断
        return FileResponse(str(target), headers=headers)


    # ---- 平台音色 --------------------------------------------------------

    @router.get(
        "/platform-voices", response_model=ListResponse[VoiceOut], summary="平台音色列表",
        tags=["library"],
    )
    def platform_voices() -> dict[str, Any]:
        """平台音色的本地索引。登记入口已移除；清单由各引擎自行拉取。"""
        return {
            "object": "list",
            "data": [
                _voice_out(v)
                for v in service.list_voices()
                if v.source != SOURCE_LOCAL
            ],
            "has_more": False,
        }

    @router.delete(
        "/platform-voices/{voice_id:path}",
        tags=["library"],
        response_model=DeleteOut,
        summary="移除平台音色条目",
    )
    def delete_platform_voice(voice_id: str) -> dict[str, Any]:
        """仅从本地索引移除；平台侧数据不变。"""
        service.delete_platform_voice(voice_id)
        return {
            "id": voice_id,
            "object": "platform_voice",
            "deleted": True,
            "trashed": [],
        }

