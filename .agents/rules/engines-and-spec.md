---
trigger: model_decision
description: >-
  修改 app/engines/、app/domain/request_spec.py、app/domain/credential.py、app/domain/marks.py 或接入/调整 TTS、ASR、LLM 插件时激活。
---

# 引擎契约与请求规范

完整规范见 [docs/engine-integration.md](../../docs/engine-integration.md) 与 [docs/request-spec.md](../../docs/request-spec.md)。

## 1. 插件组织与唯一判定点

- 一台引擎一个目录（`app/engines/builtin/<key>/`），自声明配置、能力、映射与文案；禁止在核心路由或前端按具体引擎 key 硬编码分支（共性逻辑经两家以上验证后再上收宿主契约）。
- 启用清单唯一出口：`usecase.engines.active_engines`（保存配置顺手启用；停用不动配置与库内音色）。`library` 通道不把保存目录当扫描根；「恢复默认」不复位通道；归档、扫描根排除与行参数面板按全部已发现插件计算。
- 路由凭据唯一判定：`app/domain/credential.py::credential_of`（预置音色一律发平台 id，样例服务本地/复刻/跨平台借用）。
- `Capabilities` 新增字段须明确属路由类还是展示类，并维护守卫测试（如 `tests/test_credential.py`）；`describe()` 自动派生能力包，不手抄字段。

## 2. 参数映射与表达标注

- 映射前先确认语义一致，禁止按名字相似直译；空值代表「未要求」，不得补成默认值再发送；无落点项写入 `unsupported()`。
- 引导指令预设按全部已发现插件的 `instructions.json` 合并去重，不随启停增减。
- 统一语气词写法为 `[[笑]]` / `[[停顿:0.4]]`；翻译只在网关通过 `EnginePlugin.prepare_text` 执行一次，不支持的标签删除（删空报 `empty_text`）。改动 `mark_map` 后运行 `scripts/marks_audit.py` 核对。
- 本机单模型服务声明 `max_concurrency = 1`；有每分钟配额的云端插件须提供 `rpm` 并接入 `engines/ratelimit.py`。

## 3. 能力与参数页契约

- 引擎面板页签由 `ui_sections()` 声明，核心过滤下发；「能力与参数」默认四段顺序为 **能力 → 支持语言 → 参数映射 → 引擎要求**（语气词标签表按需附后）。
- 参数映射表固定四列：**项目规范 | 映射位置 | 换算 | 说明**，按 `SPEC_FIELDS` 逐项列全；跨参数约束写 `EngineMapping(cautions=...)`，行内重点用 `warning=True`。
- 新增段落或修改契约时同步更新对应设计文档，不在前端临时插入或单列默认值表。
