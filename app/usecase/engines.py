"""引擎用例：配置读写（密钥掩码）、拉清单入库、音色克隆。

密钥口径：存 = Windows DPAPI（当前用户）加密后写入本机 config.json，拷走文件也解不开；
读 = 先解密再校验，``GET`` 掩码为空串并另给 ``secrets_set``；
写 = 掩码字段提交空串表示不改。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app.domain.channel import (
    VoiceChannel,
    allowed_modes,
    as_payload,
    default_channel,
    resolve_channel,
)
from app.domain.credential import DESIGN, PLATFORM, REFERENCE, credential_of
from app.domain.marks import mark_groups
from app.domain.models import platform_voice_id
from app.engines import ratelimit
from app.engines.asr_base import AsrEngine
from app.engines.base import (
    KIND_LABELS,
    ORPHAN_LABEL,
    SECTION_LABELS,
    CloneRequest,
    EngineError,
    EnginePlugin,
    VoiceMeta,
)
from app.engines.registry import detect_asr_engine, detect_engine, list_asr_engines, list_engines
from app.infra import archive
from app.infra import migrate as migrate_mod
from app.infra import secrets as secret_store
from app.infra.config import load_config, save_config
from app.infra.loghub import CHANNEL

__all__ = [
    "AsrConfigError",
    "EngineConfigError",
    "EngineSettingsError",
    "active_engines",
    "asr_payload",
    "asr_settings_for",
    "catalog_payload",
    "channel_of",
    "clone_voice",
    "enabled_keys",
    "engine_entries",
    "engine_settings",
    "engine_voices",
    "is_enabled",
    "marks_payload",
    "output_info",
    "page_sections",
    "preview_engine_output_dir",
    "refresh_voices",
    "require_active_engine",
    "require_asr_engine",
    "save_asr_settings",
    "save_asr_settings_for",
    "save_engine_channel",
    "save_engine_output_dir",
    "save_engine_settings",
    "set_engine_enabled",
    "settings_payload",
]

#: 复刻样本要整份读入内存再写归档，给一个明确的量级上限。
_MAX_SAMPLE_BYTES = 20 * 1024 * 1024


class EngineConfigError(Exception):
    """引擎配置/调用相关的用例错误——api 层翻成 400/404，``str(exc)`` 是可读说明。"""


class AsrConfigError(EngineConfigError):
    """识别引擎的配置错误。与合成引擎同一个口径（同一张错误表）。"""


class EngineSettingsError(Exception):
    """设置值不合法：保存时直接拒绝，不静默回落到默认值。

    刻意**不**继承 :class:`EngineConfigError`：后者在 api 层是 404
    ``engine_not_found``，在用例层还有 ``except`` 把它当成「这台引擎没装」。
    值不合法是另一回事，混进去会被当成「引擎不存在」吞掉。
    """


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def _tolerant(schema: type[BaseModel], stored: dict[str, Any]) -> BaseModel:
    """把存下的字典读成模型：坏字段丢掉、其余保留，不让整台引擎读不出配置。"""
    known = {k: v for k, v in (stored or {}).items() if k in schema.model_fields}
    try:
        return schema(**known)
    except ValidationError:
        good: dict[str, Any] = {}
        for name, value in known.items():
            try:
                schema.model_validate({name: value})
            except ValidationError:
                continue
            good[name] = value
        return schema(**good)


def _invalid_settings_message(schema: type[BaseModel], exc: ValidationError) -> str:
    """把第一条校验失败翻成一句中文，别把 pydantic 的英文原话丢给界面。"""
    errors = exc.errors()
    if not errors:
        return "设置未保存：值不合法。"
    first = errors[0]
    loc = first.get("loc") or ()
    name = str(loc[-1]) if loc else ""
    field = schema.model_fields.get(name)
    hints = (field.json_schema_extra or {}) if field else {}
    label = str(hints.get("label") or name or "设置")
    ctx = first.get("ctx") or {}
    kind = str(first.get("type") or "")
    if kind == "greater_than":
        rule = f"须大于 {ctx.get('gt')}"
    elif kind == "greater_than_equal":
        rule = f"不能小于 {ctx.get('ge')}"
    elif kind == "less_than_equal":
        rule = f"不能大于 {ctx.get('le')}"
    elif kind in ("float_type", "int_type", "float_parsing", "int_parsing"):
        rule = "须填数字"
    else:
        rule = "值不合法"
    return f"设置未保存：「{label}」{rule}。"


def require_engine(key: str) -> EnginePlugin:
    """按 key 取插件，**不看启停**：适配清单、图标、启停本身都要够得到停用的那台。"""
    engine = detect_engine(key)
    if engine is None:
        usable = "、".join(sorted(enabled_keys() & set(list_engines()))) or "（无）"
        raise EngineConfigError(f"引擎 {key} 不存在。已启用引擎：{usable}。")
    return engine


# --------------------------------------------------------------------------- #
# 启用与停用
#
# 发现到一台插件不等于它要出现在界面上：左栏、生成页、生产方的候选都只认"已启用"。
# 未启用的仍留在适配清单里（``/engines/catalog``），可以随时添加。
# --------------------------------------------------------------------------- #


def enabled_keys() -> set[str]:
    """已启用的引擎 key。**唯一一处**回答"这台现在算不算在用"。

    配置里没有这个字段（升级前写的 config.json）时按"用过的"推导：有配置段、
    有通道、有保存目录、是当前识别引擎，都算用过。推导结果不写盘——读配置不该改配置；
    用户第一次启停时才会落下明确的清单。
    """
    config = load_config()
    available = set(list_engines())
    stored = config.engines_enabled
    if stored is not None:
        # ``[]`` 是"真的都停用了"，与"没写过这个字段"必须分开（见 Config.engines_enabled）。
        return {key for key in stored if key in available}
    used = set(config.engines) | set(config.channels) | set(config.engine_outputs)
    if config.asr_engine:
        used.add(config.asr_engine)
    return used & available


def is_enabled(key: str) -> bool:
    return (key or "").strip() in enabled_keys()


def active_engines() -> dict[str, EnginePlugin]:
    """已启用的合成引擎（``key`` → 实例），按 key 排序。

    **界面与调用的唯一入口**：凡是要"这台能不能用"的地方都走它，而不是 ``list_engines``。
    """
    engines = list_engines()
    enabled = enabled_keys()
    return {key: engine for key, engine in engines.items() if key in enabled}


def require_active_engine(key: str) -> EnginePlugin:
    """按 key 取一台**已启用**的引擎；不存在与未启用分开说。"""
    engine = require_engine(key)
    if not is_enabled(engine.key):
        raise EngineConfigError(
            f"引擎 {engine.label} 未启用。在「引擎」页把它打开后再用。"
        )
    return engine


def set_engine_enabled(key: str, enabled: bool) -> dict[str, Any]:
    """启用 / 停用一台引擎。配置与库内音色都不动——停用只是不再出现。"""
    engine = require_engine(key)
    keys = enabled_keys()
    if enabled:
        keys.add(engine.key)
    else:
        keys.discard(engine.key)
    config = load_config()
    config.engines_enabled = sorted(keys)
    save_config(config)
    CHANNEL.info(
        f"{'已启用' if enabled else '已停用'}引擎：{engine.label}",
        source="engines",
        engine=engine.key,
    )
    return {"key": engine.key, "label": engine.label, "enabled": engine.key in keys}


def _facts(engine: EnginePlugin) -> tuple[BaseModel, Any, str]:
    """一台引擎的"当前状态"三件套：配置、能力、未就绪原因。

    清单、左栏、选项表要的都是这三样，形状不同但取法一字不差；写三遍就是三处
    会各自漂移的地方（改一处忘两处，界面就会互相矛盾）。
    """
    cfg = _tolerant(engine.settings_schema(), _stored(engine))
    return cfg, engine.capabilities(), engine.readiness(cfg)


def catalog_payload() -> list[dict[str, Any]]:
    """适配清单：发现到的每一台，带"是否已启用"。供「引擎清单」用。"""
    enabled = enabled_keys()
    out: list[dict[str, Any]] = []
    for key, engine in list_engines().items():
        cfg, caps, note = _facts(engine)
        out.append(
            {
                "key": key,
                "label": engine.label,
                "kind": caps.kind,
                "kind_label": KIND_LABELS.get(caps.kind, ("", ""))[0],
                "kind_note": KIND_LABELS.get(caps.kind, ("", ""))[1],
                "icon_url": f"/api/v1/engines/{key}/icon",
                "enabled": key in enabled,
                "ready": not note,
                "note": note,
                "needs_key": _needs_key(engine, cfg),
                # 添加前给人看的两条事实：它有没有音色清单、能不能克隆。
                "can_list": caps.voice_listing,
                "can_clone": caps.voice_cloning or caps.sample_cloning,
            }
        )
    return out


def _stored(engine: EnginePlugin | AsrEngine) -> dict[str, Any]:
    """配置段 → 可交给插件的字典：机密字段先解密，旧明文原样通过。"""
    raw = load_config().engines.get(engine.key) or {}
    return secret_store.reveal_fields(raw, engine.secret_fields())


def settings_for(engine: EnginePlugin) -> BaseModel:
    """这台插件实例的当前配置。认实例不认 key：便于测试注入自定义引擎。"""
    return _tolerant(engine.settings_schema(), _stored(engine))


def engine_settings(key: str) -> tuple[EnginePlugin, BaseModel]:
    """``key`` → （插件，它的配置模型）。**读不出来也返回默认值**，不抛。"""
    engine = require_engine(key)
    return engine, settings_for(engine)


# --------------------------------------------------------------------------- #
# 音色通道
# --------------------------------------------------------------------------- #


def engines_for_voice(voice: Any) -> list[str]:
    """这条音色能被哪些引擎用——**唯一一处**算这个问题的循环。

    判据来自 :func:`app.domain.credential.credential_of`：一种凭据对应一项能力，
    凭据为空（:data:`~app.domain.credential.NONE`）就不出现在这里。不看就绪状态：
    "能不能用"是路由问题，"配没配好"是界面该灰掉的另一件事。
    """
    source = str(voice.source or "")
    out: list[str] = []
    for key, engine in active_engines().items():
        caps = engine.capabilities()
        cred = credential_of(voice, engine_key=key)
        if cred == REFERENCE:
            usable = bool(caps.reference_audio)
        elif cred == DESIGN:
            usable = bool(caps.voice_design)
        elif cred == PLATFORM:
            usable = key == source  # 预置音色只回自己的平台：那个 id 只有它认
        else:
            usable = False
        if usable:
            out.append(key)
    return out


def channel_of(engine: EnginePlugin) -> VoiceChannel:
    """这台引擎生效中的音色通道：默认值由能力推导，再被 ``channels[key]`` 覆盖。"""
    caps = engine.capabilities()
    default = default_channel(
        voice_listing=caps.voice_listing, reference_audio=caps.reference_audio
    )
    allowed = allowed_modes(
        voice_listing=caps.voice_listing, reference_audio=caps.reference_audio
    )
    stored = load_config().channels.get(engine.key) or {}
    return resolve_channel(default, allowed, stored)


def channel_payload(engine: EnginePlugin) -> dict[str, Any]:
    """通道 → 下发给界面的那一包（标签、说明、默认值、可选项齐全）。"""
    caps = engine.capabilities()
    default = default_channel(
        voice_listing=caps.voice_listing, reference_audio=caps.reference_audio
    )
    allowed = allowed_modes(
        voice_listing=caps.voice_listing, reference_audio=caps.reference_audio
    )
    stored = load_config().channels.get(engine.key) or {}
    return as_payload(resolve_channel(default, allowed, stored), default=default, allowed=allowed)


def page_sections(engine: EnginePlugin) -> list[dict[str, Any]]:
    """引擎页面的分区表——页签由它出，前端不认识任何引擎与能力键。

    两处补充/过滤：「音色」块的说明由生效通道填；插件声明的分区键必须在
    :data:`~app.engines.base.SECTION_LABELS` 里，画不出的键丢掉。
    ``meta`` 原样下发（克隆/设计各用哪个模型之类，由插件声明）。
    """
    channel = channel_payload(engine)
    out: list[dict[str, Any]] = []
    for section in engine.ui_sections():
        if section.key not in SECTION_LABELS:
            continue
        note = channel["mode_note"] if section.key == "voices" else section.note
        out.append(
            {
                "key": section.key,
                "label": section.label,
                "note": note,
                "meta": dict(section.meta or {}),
            }
        )
    return out


def save_engine_channel(key: str, patch: dict[str, Any]) -> dict[str, Any]:
    """改这台引擎的音色通道。空串 / 缺 ``mode`` = 回到默认（删掉该段配置）。

    越界的模式丢回默认而非报错：能力是上限，回落是唯一安全结果。
    """
    engine = require_engine(key)
    caps = engine.capabilities()
    allowed = allowed_modes(
        voice_listing=caps.voice_listing, reference_audio=caps.reference_audio
    )
    default = default_channel(
        voice_listing=caps.voice_listing, reference_audio=caps.reference_audio
    )
    channel = resolve_channel(default, allowed, patch or {})

    config = load_config()
    channels = dict(config.channels)
    if channel == default:
        # 写回默认 = 不留这一段：留着"与默认一样"的段，改默认值时会成为看不见的钉子。
        channels.pop(key, None)
    else:
        channels[key] = {
            "mode": channel.mode,
            "scope": channel.scope,
        }
    config.channels = channels
    save_config(config)
    return channel_payload(engine)


def _mark_sources() -> list[dict[str, Any]]:
    """每台**已启用**引擎声明的标签表（静态事实，与配没配好无关）。

    停用的引擎不参与：「通用」是所有支持语气词的引擎之间的交集，把一台停用的算进去，
    已启用那几台共同的标签会被它挤掉——界面上就会少掉几个其实人人认的标签。

    ``mark_any`` = 这家认任意标签（见 ``EnginePlugin.mark_passthrough``）：通用运算里
    它的支持面按全集算，界面上另给一格自定义输入。
    """
    return [
        {
            "key": key,
            "label": engine.label,
            "mark_map": engine.mark_map(),
            "mark_any": bool(engine.mark_passthrough()),
        }
        for key, engine in active_engines().items()
    ]


def marks_payload() -> dict[str, Any]:
    """语气词标签的跨引擎分组（``GET /api/v1/marks``）。

    单开端点：「通用」是引擎之间的交集，任何一台自己算不出来，且与勾选无关。
    """
    return mark_groups(_mark_sources())


def _mask(engine: EnginePlugin | AsrEngine, model: BaseModel) -> dict[str, Any]:
    secrets = engine.secret_fields()
    data = model.model_dump()
    for name in secrets:
        if name in data:
            data[name] = ""
    return data


def _needs_key(engine: EnginePlugin | AsrEngine, cfg: BaseModel) -> bool:
    """这条「未就绪」是不是只差密钥？

    界面据此不再重复一句"还没填 API Key"——头上的「未就绪」徽标已经说了；
    而缺模型、缺依赖这类原因仍然要摆出来（那不是一眼能看出来的）。
    """
    secrets = engine.secret_fields()
    return bool(secrets) and not any(
        str(getattr(cfg, name, "") or "").strip() for name in secrets
    )


def settings_payload(key: str, *, common: set[str] | None = None) -> dict[str, Any]:
    """给界面的整包：掩码后的当前值 + 渲染表单要的一切。

    :param common: 哪几条标签是「通用」的（跨引擎交集）。一次要出好几台时由调用方
        算一遍传进来——``/engines`` 一次要出九台，每台都重算就是把同一份交集算九遍，
        而它每次都要读全部引擎的配置。
    """
    engine, cfg = engine_settings(key)
    data = engine.describe(cfg)
    note = engine.readiness(cfg)
    if common is None:
        common = {row["name"] for row in mark_groups(_mark_sources())["common"]}
    # 通用的排在前（稳定排序，各组内部仍按 MARKS 次序）。
    data["marks"] = sorted(
        ({**row, "common": row["name"] in common} for row in data.get("marks") or ()),
        key=lambda row: not row["common"],
    )
    return {
        **data,
        "values": _mask(engine, cfg),
        # 是否已启用：界面的开关照它画，不自己比对清单。
        "enabled": is_enabled(key),
        "secrets_set": [name for name in sorted(engine.secret_fields()) if getattr(cfg, name, "")],
        "ready": not note,
        "note": note,
        # 只差密钥：界面不再复述一句提示（徽标已经说了）。
        "needs_key": _needs_key(engine, cfg),
        # 音色通道（音色从哪来、怎么供），随这一包下发。
        "channel": channel_payload(engine),
        # 页面分区表，页签由它出。
        "page": {"sections": page_sections(engine)},
        # 音色保存目录：框架给的，不属于插件的 settings_schema，装了即有。
        "output": output_info(key, engine.label),
    }


def output_info(key: str, label: str) -> dict[str, Any]:
    """这台引擎的音色保存目录。``custom=False`` 时回默认位置，``default`` 供"恢复默认"显示。"""
    return {
        "dir": str(archive.engine_root(key, label, create=False)),
        "custom": bool(archive.configured_dirs().get(key)),
        "default": str(archive.default_root() / archive.safe_segment(label)),
    }


def save_engine_output_dir(key: str, directory: str, *, migrate: bool = False) -> dict[str, Any]:
    """改这台引擎的保存目录。空串 = 回到默认（全局根下的引擎子目录）。

    不校验目录存在（可能在未挂载的移动盘上），与扫描根同口径。

    :param migrate: 把旧目录内容搬到新目录；默认不搬（改配置不该挪用户文件），
        界面保存前用 :func:`preview_engine_output_dir` 询问。搬完不删旧目录本身。
    """
    engine = require_engine(key)
    config = load_config()
    outputs = dict(config.engine_outputs)
    cleaned = (directory or "").strip()
    previous = output_info(key, engine.label)["dir"]  # 改之前那个"生效值"
    if cleaned:
        outputs[key] = cleaned
    else:
        outputs.pop(key, None)
    config.engine_outputs = outputs
    save_config(config)
    info = output_info(key, engine.label)

    moved: dict[str, Any] | None = None
    if migrate and previous and previous != info["dir"]:
        plan = migrate_mod.plan_move(Path(previous), Path(info["dir"]))
        if plan.entries:
            moved = asdict(migrate_mod.apply_move(plan))
    info = output_info(key, engine.label)
    info["moved"] = moved
    # 旧目录还在时一并给出：用户须知道旧文件去哪了、还留没留。
    info["previous_dir"] = previous if previous and previous != info["dir"] else ""
    return info


def preview_engine_output_dir(key: str, directory: str) -> dict[str, Any]:
    """保存前预览：这次改动要不要搬东西（不碰文件）。没有可搬的则静默保存。"""
    engine = require_engine(key)
    previous = output_info(key, engine.label)["dir"]
    cleaned = (directory or "").strip()
    target = cleaned or str(archive.default_root() / archive.safe_segment(engine.label))
    if not previous or previous == target:
        return {"needed": False, "reason": ""}
    plan = migrate_mod.plan_move(Path(previous), Path(target))
    return {
        "needed": not plan.empty,
        "reason": plan.note,
        "source": plan.source,
        "target": plan.target,
        "entries": plan.entries,
        "audio": plan.audio,
        "bytes": plan.bytes,
    }


def save_engine_settings(key: str, patch: dict[str, Any]) -> BaseModel:
    """保存配置。掩码字段提交空串 = 不改（见模块顶部）。

    框架级指令：``"_clear": ["api_key"]`` 显式清空机密项（空串已被解释为"不改"，
    没有这个口子密钥就删不掉）；``"_reset": true`` 恢复默认（非机密回落插件默认值，
    机密保留——删密钥是 ``_clear`` 的活）。
    """
    return _save_settings(require_engine(key), patch)


def _save_settings(plugin: EnginePlugin | AsrEngine, patch: dict[str, Any]) -> BaseModel:
    """合成与识别共用的那份保存逻辑：**规则只写一遍**（掩码、清空、恢复默认）。"""
    schema = plugin.settings_schema()
    secrets = plugin.secret_fields()

    body = dict(patch or {})
    reset = bool(body.pop("_reset", False))
    cleared = {name for name in (body.pop("_clear", None) or []) if name in secrets}

    stored = _stored(plugin)
    if reset:
        # 只留机密：其余字段从字典里消失 → pydantic 用插件声明的默认值补齐。
        stored = {name: value for name, value in stored.items() if name in secrets}
    clean: dict[str, Any] = {}
    for name, value in body.items():
        if name not in schema.model_fields:
            continue  # 不认识的键直接丢
        if name in secrets and name not in cleared and isinstance(value, str) and not value.strip():
            continue
        clean[name] = value
    for name in sorted(cleared):
        clean[name] = ""

    # 插件可以在合并之前把某些字段带过去：换了模型版本，采样参数该按那一版的
    # 推荐值走（见 GptSovitsEngine.reconcile_settings）。识别引擎没有这个钩子。
    reconcile = getattr(plugin, "reconcile_settings", None)
    if callable(reconcile):
        clean = dict(reconcile(clean, stored))

    # 读可以回落（历史脏配置不该让整台引擎读不出来），写不能回落：
    # 存进去的值必须过校验，否则界面提示「已保存」却悄悄用了默认值。
    #
    # 但严格只对**这次提交的字段**：旧版本按旧约束写下的值（比如 timeout 还没有
    # gt 约束时存的 0）不该让人改别的设置也寸步难行，还报一个他没碰过的字段。
    merged = {**stored, **clean}
    try:
        model = schema.model_validate(merged)
    except ValidationError as exc:
        broken = {str(error["loc"][-1]) for error in exc.errors() if error.get("loc")}
        if broken & set(clean):
            raise EngineSettingsError(_invalid_settings_message(schema, exc)) from exc
        model = _tolerant(schema, merged)
    config = load_config()
    config.engines[plugin.key] = secret_store.seal_fields(model.model_dump(), secrets)
    # 填了配置就等于在用：保存设置顺手把它添加进来，省一次「先添加、再配置」的往返。
    # 识别引擎不走这套——识别的"当前那台"由 ``config.asr_engine`` 单独回答。
    if isinstance(plugin, EnginePlugin) and not is_enabled(plugin.key):
        config.engines_enabled = sorted(enabled_keys() | {plugin.key})
    save_config(config)
    return model


# --------------------------------------------------------------------------- #
# 识别引擎（音频 → 文字）：发现、配置、状态
#
# 与合成引擎**共享的只有这三件事**：发现（同一批目录）、配置（同一份
# ``config.engines``，同一套掩码/恢复默认规则）、界面自描述的形状。
# 能力与调用入口各走各的：识别没有音色、没有参数映射表。
# --------------------------------------------------------------------------- #


def require_asr_engine(key: str) -> AsrEngine:
    engine = detect_asr_engine(key)
    if engine is None:
        raise AsrConfigError(
            f"识别引擎 {key} 不存在。可用引擎："
            f"{'、'.join(sorted(list_asr_engines())) or '（无）'}。"
        )
    return engine


def asr_settings_for(engine: AsrEngine) -> BaseModel:
    """这台识别引擎的当前配置（认实例不认 key，便于测试注入）。"""
    return _tolerant(engine.settings_schema(), _stored(engine))


def save_asr_settings(key: str, patch: dict[str, Any]) -> BaseModel:
    """保存识别引擎配置；语义与 :func:`save_engine_settings` 完全一致。"""
    return _save_settings(require_asr_engine(key), patch)


def save_asr_settings_for(engine: AsrEngine, patch: dict[str, Any]) -> BaseModel:
    """按**实例**保存（与 :func:`asr_settings_for` 对称）：调用方已经拿到那台了。"""
    return _save_settings(engine, patch)


def asr_payload(engine: AsrEngine) -> dict[str, Any]:
    """给界面的一条识别引擎：描述 + 掩码后的当前值 + 就绪说明。"""
    cfg = asr_settings_for(engine)
    note = engine.readiness(cfg)
    return {
        **engine.describe(),
        "values": _mask(engine, cfg),
        "secrets_set": [
            name for name in sorted(engine.secret_fields()) if getattr(cfg, name, "")
        ],
        "ready": not note,
        "note": note,
        "needs_key": _needs_key(engine, cfg),
    }


def engine_entries(counts: dict[str, int]) -> list[dict[str, Any]]:
    """左栏「引擎」段：以**已启用**的插件为准（启用了就有一条，0 条也显示），挂上库内计数。

    每行都带 ``enabled``——与适配清单、引擎自描述同一个字段、同一个极性（``True`` 表示
    在用）。另外两种行：``enabled=False`` 且 ``orphan=False``（用户停用了，但库里还有
    它的音色——不出现的话那些音色会凭空消失）与 ``orphan=True``（插件本身已卸载）。
    """
    engines = list_engines()
    enabled = enabled_keys()
    out: list[dict[str, Any]] = []
    for key, engine in engines.items():
        cfg, caps, note = _facts(engine)
        if key not in enabled:
            # 未启用且有遗留音色：只留一行"停用的引擎"，点一下只看它的音色。
            if not counts.get(key):
                continue
            out.append(
                {
                    "source": key,
                    "key": key,
                    "label": engine.label,
                    "orphan": False,
                    "enabled": False,
                    "kind": caps.kind,
                    "kind_label": KIND_LABELS.get(caps.kind, ("", ""))[0],
                    "kind_note": "已停用：不参与生成，也不在音色来源里出现。",
                    "icon_url": f"/api/v1/engines/{key}/icon",
                    "total": int(counts[key]),
                    "ready": True,
                    "note": "",
                    "needs_key": False,
                    "can_list": False,
                    "can_clone": False,
                    "uses_reference_audio": caps.reference_audio,
                }
            )
            continue
        out.append(
            {
                "source": key,
                "key": key,
                "label": engine.label,
                "kind": caps.kind,
                "kind_label": KIND_LABELS.get(caps.kind, ("", ""))[0],
                "kind_note": KIND_LABELS.get(caps.kind, ("", ""))[1],
                "icon_url": f"/api/v1/engines/{key}/icon",
                "total": int(counts.get(key, 0)),
                "ready": not note,
                "note": note,
                # 只差密钥：左栏与面板都不再复述提示。
                "needs_key": _needs_key(engine, cfg),
                # 这台引擎还在（与下面的 orphan 条目相对），界面据此分支显示。
                "orphan": False,
                "enabled": True,
                "can_list": caps.voice_listing,
                "can_clone": caps.voice_cloning or caps.sample_cloning,
                # 音色通道（音色从哪来、怎么供），界面照它说话，不自行解释能力布尔。
                "channel": channel_payload(engine),
                # 是否需要参考音频而非音色 id；界面据此决定 0 条音色时的提示。
                "uses_reference_audio": caps.reference_audio,
                # 音色保存目录（框架给的）；其下生成物是普通本地音色，路径供悬停说明。
                "output": output_info(key, engine.label),
            }
        )
    # 库里存在、但没有对应插件的 source（引擎卸载后留下的行）——照样显示，否则那些
    # 音色会凭空消失。``orphan`` 明说，唯一有意义的动作是"看它留下的音色"。
    for name, total in sorted(counts.items()):
        if name in engines:
            continue
        out.append(
            {
                "source": name,
                "key": "",
                "label": name,
                "orphan": True,
                # 插件都没了，谈不上启用；``orphan`` 才是这一行的判据（界面先看它）。
                "enabled": False,
                "kind": "",
                "kind_label": ORPHAN_LABEL[0],
                "kind_note": ORPHAN_LABEL[1],
                "icon_url": "",
                "total": int(total),
                "ready": True,
                "note": "",
                "can_list": False,
                "can_clone": False,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 调用
# --------------------------------------------------------------------------- #


def engine_choices() -> tuple[dict[str, Any], ...]:
    """现在**已启用**哪几台引擎，够上游画下拉用。刻意只给键 / 名字 / 形态 / 可用性，
    不搬完整的 ``describe()``（那是本站界面的内部结构）。"""
    out: list[dict[str, Any]] = []
    for key, engine in active_engines().items():
        _cfg, caps, note = _facts(engine)
        out.append(
            {
                "key": key,
                "label": engine.label,
                "kind": caps.kind,
                "kind_label": KIND_LABELS.get(caps.kind, ("", ""))[0],
                "ready": not note,
                "note": note,
                "can_list": caps.voice_listing,
            }
        )
    return tuple(out)


def _local_voice_items(service: Any) -> list[dict[str, Any]]:
    """库里的本地音色 → ``/engines/{key}/voices`` 条目形状。``id`` 是库内 id
    （扫描时定），上游须原样传回，网关才找得到；``name`` 才是显示名。"""
    if service is None:
        return []
    try:
        voices = service.list_voices()
    except Exception:  # noqa: BLE001 - 清单是锦上添花，坏了不连累设置页
        return []
    return [
        {
            "id": v.id,
            "name": v.shown_name,
            "description": v.description,
            "category": v.category,
            "tags": list(v.tags),
        }
        for v in voices
        if v.is_local
    ]


async def engine_voices(key: str, service: Any = None) -> dict[str, Any]:
    """这台引擎现在有哪些音色——只读，不入库（与 ``/refresh`` 分工）。

    拉不到返回 200 + ``ok:false`` + 理由（"没拉到"是业务结果，不是协议错误）。
    参考音频类引擎（无清单端点）回落到库里的本地音色，上游选中后把库内 id
    原样传回，合成时由网关翻译成参考音频。
    """
    engine = require_active_engine(key)
    cfg = settings_for(engine)
    caps = engine.capabilities()
    if not caps.voice_listing:
        # 判据是能力，不是通道：有清单的引擎不因通道改变就不显示清单。
        local = _local_voice_items(service) if caps.reference_audio and service is not None else []
        if local:
            return {
                "ok": True,
                "key": key,
                "label": engine.label,
                "items": local,
                "reason": "",
                "cost_note": "",
                "value_choices": {k: list(v) for k, v in engine.value_choices().items()},
            }
        return {
            "ok": False,
            "key": key,
            "label": engine.label,
            "items": [],
            "reason": f"{engine.label} 不提供音色清单（{engine.describe()['kind_label']}引擎）。",
        }
    # 拉清单也是一次平台请求：与合成共用同一份额度，别在限流窗口里再打一下。
    async with ratelimit.permit(key, cfg, concurrency=ratelimit.limit_for(engine, cfg)):
        result = await engine.list_voices(cfg)
    items = [
        {
            "id": meta.id,
            "name": meta.name,
            "description": meta.description,
            "category": meta.category,
            "tags": list(meta.tags),
        }
        for meta in result.items
    ]
    return {
        "ok": bool(items),
        "key": key,
        "label": engine.label,
        "items": items,
        "reason": "" if items else (result.why or "本次未获取到音色清单。"),
        "cost_note": result.cost_note,
        # 这台引擎认的中文情绪词，供上游逐行打标（不认的当场标无效）。
        "value_choices": {k: list(v) for k, v in engine.value_choices().items()},
    }


async def refresh_voices(service: Any, key: str) -> dict[str, Any]:
    """拉这台引擎的音色清单并入库。拉不到不算失败——把理由带回去。三种结果各记一级日志。"""
    engine = require_active_engine(key)
    cfg = settings_for(engine)
    if not engine.capabilities().voice_listing:
        # 能不能拉清单是能力，与通道无关。
        reason = f"{engine.label} 不提供音色清单（形态：{engine.describe()['kind_label']}）。"
        # debug：插件声明过的特性，不是故障；记 warning 会让每次误点都加黄点。
        CHANNEL.debug(reason, source="engines", engine=key)
        return {
            "ok": False,
            "reason": reason,
            "added": 0,
            "updated": 0,
            "total": 0,
        }

    async with ratelimit.permit(key, cfg, concurrency=ratelimit.limit_for(engine, cfg)):
        result = await engine.list_voices(cfg)
    if not result.items:
        reason = result.why or "本次未获取到音色清单。"
        # warning：这次真没拿到（超时、密钥过期等），但库中条目未动，重试可能就好。
        CHANNEL.warning(reason, source="engines", engine=key)
        return {
            "ok": False,
            "reason": reason,
            "added": 0,
            "updated": 0,
            "total": 0,
        }
    added, updated = service.import_platform_voices(key, result.items)
    CHANNEL.info(
        f"{engine.label} 清单已更新：新增 {added}、更新 {updated}、共 {len(result.items)} 条。",
        source="engines",
        engine=key,
        added=added,
        updated=updated,
        total=len(result.items),
    )
    return {
        "ok": True,
        "reason": "",
        "added": added,
        "updated": updated,
        "total": len(result.items),
        "cost_note": result.cost_note,
    }


async def clone_voice(service: Any, key: str, req: CloneRequest) -> VoiceMeta:
    """克隆一个音色，成功即入库（它已是可用的平台音色）。

    不支持克隆抛 :class:`EngineError`（400，这次请求做不到）而非
    :class:`EngineConfigError`（404，没有这台引擎）。
    """
    engine = require_active_engine(key)
    cfg = settings_for(engine)
    caps = engine.capabilities()
    designing = bool(req.design_prompt.strip())
    if designing and not caps.voice_design:
        raise EngineError(f"{engine.label} 不支持音色设计。")
    if not designing and not (caps.voice_cloning or caps.sample_cloning):
        raise EngineError(f"{engine.label} 不支持音色克隆。")
    # 克隆是计费操作，与合成排队：本机引擎声明 1 时，克隆也不能插队并行。
    async with ratelimit.permit(key, cfg, concurrency=ratelimit.limit_for(engine, cfg)):
        meta = await engine.clone_voice(cfg, req)
    if designing:
        # 平台不返回音色 id：描述就是这条音色的凭据，以后每次合成带着它走设计模型。
        service.add_platform_voice(
            key, meta, payload={"design_prompt": req.design_prompt.strip()}
        )
    elif caps.sample_cloning:
        # 平台没有音色 id：这条音色就是这段样本。复制进它自己的样例目录，
        # 以后每次合成都按 payload.ref_audio 把样本带给引擎（voiceclone 模型）。
        source = Path(req.audio_path)
        suffix = source.suffix.lower().lstrip(".") or "wav"
        allowed = tuple(fmt.lower().lstrip(".") for fmt in caps.sample_formats) or (
            "mp3",
            "wav",
        )
        if suffix not in allowed:
            raise EngineError(
                f"{engine.label} 的复刻样本只认 {' / '.join(allowed)}，当前是 .{suffix}。"
            )
        try:
            if source.stat().st_size > _MAX_SAMPLE_BYTES:
                raise EngineError(
                    f"音源超过 {_MAX_SAMPLE_BYTES // 1024 // 1024} MB。"
                )
            data = source.read_bytes()
        except OSError as exc:
            raise EngineError(f"读不到音源文件：{source}") from exc
        _rel, absolute = archive.write_audio(
            key,
            engine.label,
            meta.name,
            data,
            fmt=suffix,
            voice_id=platform_voice_id(key, meta.id),
            text=req.transcript or "",
        )
        service.add_platform_voice(key, meta, payload={"ref_audio": str(absolute)})
    else:
        service.add_platform_voice(key, meta)
    CHANNEL.info(
        f"已{'设计' if designing else '克隆'}并入库：{meta.name}（{engine.label}）",
        source="engines",
        engine=key,
        voice_id=meta.id,
    )
    return meta
