---
trigger: model_decision
description: >-
  修改 app/producers/（audiobook、dubbing、voiceclean、realtime_chat、audiocpp）或相关工作区后端/前端逻辑时激活。
---

# 生产方工作区规范

各工作区完整设计见 [docs/audiobook-flow.md](../../docs/audiobook-flow.md)、[docs/media-pipeline.md](../../docs/media-pipeline.md) 与 [docs/design.md](../../docs/design.md) §9。

## 1. 通用规则

- 左栏与导航由 `ProducerPlugin` 的 `voice_rail` / `rail_mode` / `nav_section` 自描述；工具型工作区 `steps()` 返回空。
- 合成计划只排已启用引擎（停用引擎的行计划期摘除并记录提示）；运行期撞上 `CODES_ENGINE_UNAVAILABLE` 停掉该引擎剩余行，保留待生成状态，不逐行写失败。
- 音频指纹/缓存键变更须兼容旧项目或显式迁移，避免升级后无故全量作废。

## 2. 有声书（`audiobook`）

- 拆句只切不改字；段落内短旁白可并入下一行，跨说话人对白不合并。
- 改动说话人、情绪、文本、行参数或音色只标 `stale` 并保留旧音频，读取/合成/合并前经 `_settle_stale` 尝试恢复；合并拒绝不可恢复的 `stale` 行。
- 单行失败写 `error` 与 `status='failed'`，保留旧音频与 `audio_key`；行合成前先 `claim_row` 抢占位。
- `bgm_gain_db` 为相对人声增益，非绝对 dBFS；分析缓存键不含候选角色。

## 3. 配音（`dubbing`）

- 引用项目（`source_linked`）删项目不删原片；重指源文件须校验时长与原音轨序号。
- 角色分「配音 / 原声」：原声段不进合成清单；配音段按槽位静音（分轨后仅静音原声人声），`volume=0:enable=` 前置 `asetnsamples=n=256` 避免段首漏音。
- 角色 `engine=auto` 每次合成前通过 `usecase.engines.auto_engine_keys` 仅从已启用、可用、本地引擎重选，并随 `SynthesisResult.engine` 回落。
- 贴齐/导出同一项目共用操作锁；字幕整片写出并与成片同名；前端行内编辑以输入框 DOM 值为准（组字期间让路）并丢弃过期轮询快照（`writeRev`）。

## 4. 音色处理与运行时（`voiceclean` / `audiocpp`）

- `voiceclean` 模型走 audio.cpp `POST /v1/tasks/run`，白名单集中于 `producers/builtin/voiceclean/models.py`；50 ms 偏差仅拦替换不拦入库；文件目标拒绝 `job/apply` 与 `job/save`。
- `audiocpp` 托管实例随 VoiceHub 退出（Job Object），不收养外部进程；`exe` / `models_root` 限本机来源，下载安装包须校验 sha256。
