# AGENTS.md — VoiceHub 开发规则

VoiceHub 是本地优先的有声书生产工作台：FastAPI 后端（`app/`）+ Vue 3 前端（`web/`）+ SQLite。

## 0. 项目与文档地图

| 路径 | 内容 |
|---|---|
| `app/` | FastAPI 后端：`api/`、`usecase/`、`domain/`、`infra/`、`engines/`、`llm/`、`embedding/`、`producers/` |
| `web/` | Vue 3 + TypeScript + Vite 前端 |
| `tests/` | pytest；后端单元与接口测试 |
| `data/` | 运行数据：`config.json`、`hub.db`、项目库、缓存、录音、日志 |
| `docs/` | 长期设计文档，不是任务记录 |
| `packaging/sapi_bridge/` | SAPI 桥接：引擎（`VoiceHub.Sapi.cs`）、原生桩（`sapi_stub.rs`）、安装与卸载脚本 |
| `.agents/rules/` | 按领域按需加载的细则（前端、引擎、生产方） |

长期文档：

| 文件 | 职责 |
|---|---|
| [`docs/design.md`](docs/design.md) | 总体架构、音色模型、插件协议、API、前端与文案规范 |
| [`docs/request-spec.md`](docs/request-spec.md) | 合成请求字段、单位、取值、语气词与映射边界 |
| [`docs/engine-integration.md`](docs/engine-integration.md) | 引擎接入契约、目录、请求链路、语义对齐与步骤 |
| [`docs/audiobook-flow.md`](docs/audiobook-flow.md) | 有声书数据模型、阶段、状态、失效与成品规则 |
| [`docs/media-pipeline.md`](docs/media-pipeline.md) | 配音工作区：数据、执行、导出与界面口径 |
| [`docs/sapi-bridge.md`](docs/sapi-bridge.md) | SAPI 桥接：系统朗读接到 `/api/v1/tts`，令牌注册、格式协商与回落 |
| [`README.md`](README.md) | 用户向介绍、安装、快速开始与文档索引 |

- 不要新增 `docs/audit-*.md`、`*-plan.md`、`progress.md`、`*-report.md` 类一次性文档；审计、计划、进度在任务结束后删除，长期结论写入本文件或 `docs/`。
- 文档用现在时，不写日期、提交哈希、代码行号、修改经过或测试数量等易过期数字；行为或契约变化时在同一次改动中同步更新对应文档。

## 1. 核心架构红线

- **唯一接口契约**：`/api/v1` 是唯一前缀，不设别名；不为单个调用方加特例或改响应形状。
- **数据驱动、禁止硬编码分支**：核心路由与前端不按具体引擎或插件 key 写分支；文案、能力、表单、参数映射与工作区布局由后端及插件自声明下发。共性能力经两家以上插件验证后再上收宿主契约，不在插件内重复造轮子。
- **统一发现与依赖边界**：插件发现唯一实现为 `app/infra/plugin_registry.py`，单个插件导入失败仅记 warning 并跳过。核心不安装模型依赖、不下载权重、不维护模型清单（运行时管理类生产方插件在用户显式触发下管理自身数据目录资产除外）。
- **跨层唯一判定点**：已启用引擎看 `usecase.engines.active_engines`；音色落到某台引擎的凭据看 `app/domain/credential.py::credential_of`；正文语气词翻译由网关统一调用 `EnginePlugin.prepare_text` 执行一次。
- **文案**：界面、日志与注释语言简洁专业、结论优先；修改界面文案前先 grep `scripts/shot.mjs` 与 `scripts/smoke.py` 同步更新。

## 2. 数据、并发与安全

- **原子写**：小文件使用 `app/infra/atomic.py`（`write_bytes` / `write_json`，唯一 `.part` 临时文件 + Windows 占用重试）；媒体发布走 `app/infra/ffmpeg.py`。
- **配置与机密**：写配置走 `save_config`；机密字段空串表示不修改，`{"_clear": [...]}` 清空，`{"_reset": true}` 恢复默认并保留机密。
- **SQLite**：通过连接封装的 `with` 上下文操作（如 `app/infra/sqlite.py`），禁止新开裸 `sqlite3.connect`。
- **路径与本机隔离**：用户传入相对名只取 `Path(name).name` 并经 `archive.resolve_within` 校验；删除、迁移、打开目录、自定义可执行路径等本机/破坏性操作须经 `app/api/localonly.py` 限制为本机来源。
- **子进程与异步**：一律使用 argv 列表，禁止 `shell=True`，显式指定 `encoding="utf-8", errors="replace"`；事件循环内禁止同步阻塞 IO（httpx / ffmpeg / 大文件读写放线程池）。
- **单实例与并发幂等**：同一数据目录仅允许一个服务进程（`data/service.lock`），调试/测试另起服务须指向临时 `VOICEHUB_DATA_DIR`。后台任务带 `owner` / `heartbeat_at` 回收；行合成前先 `claim_row`；付费调用先 `idempotent_begin` 抢占，失败/取消时 `abandon`；批内遇 `CODES_ENGINE_UNAVAILABLE` 停掉该引擎剩余行，不逐行报错。
- **日志**：`app/infra/loghub.py` 是全项目日志唯一出口；跨线程入队须使用 `call_soon_threadsafe`；`emit` 永不抛异常。
- **已知边界（勿假装已解决）**：配置读写已提供进程内 `update_config` 事务锁（但尚无跨进程锁，且仍有零散 `load_config` → `save_config` 调用待迁）；有声书合并产物的跨文件回滚事务、跨进程合并锁及 Vue 组件级渲染单测仍未补齐。

## 3. 验证与环境

```bash
# 后端
./.venv/Scripts/python.exe -m ruff check app/ tests/
./.venv/Scripts/python.exe -m pytest tests/ -q

# 前端
cd web && npm run typecheck && npm test && npm run build

# 接口冒烟（服务已启动）
./.venv/Scripts/python.exe scripts/smoke.py --base http://127.0.0.1:17865

# 界面自检与截图（Git Bash；cmd/PowerShell 用 %TEMP%）
node scripts/shot.mjs http://127.0.0.1:17865/ "$TEMP/vh-shots"
```

- 后端一律使用 `.venv/Scripts/python.exe`，不用系统 Python；测试前清 `NO_PROXY` / `no_proxy`（或走 `app/infra/netenv.py` 归一），防止 httpx 解析 `[::1]` 失败。
- 启动服务（非启动脚本拉起时）需显式设 `VOICEHUB_DATA_DIR=./data`（或仓库绝对路径下的 `data/`），否则会回落到 `%LOCALAPPDATA%\voicehub`。
- `scripts/smoke.py` **不是只读**：会把 `--library`（默认 `demo_library`）写进扫描根并重扫。禁止对着正在使用的 `VOICEHUB_DATA_DIR` 运行，须先起临时数据目录服务或跑完恢复。
- 前端新增逻辑模块优先 `.ts` 与 `<script setup lang="ts">`，延续 JS 模块时保留 `// @ts-check`；禁止关闭检查或堆 `any`。`typecheck && build` 通过不代表界面正确，界面改动须配合 `scripts/shot.mjs` 实测。

## 4. 协作与提交

- 默认只提交本次任务涉及的文件：先核对 `git status` / `git diff`，再 `git commit --only <路径>`；工作树可能存在并行会话改动，禁止用 `git add -A` 混入他人修改。
- 仅在用户明确要求「提交所有更新」时检查并包含全部未提交项目文件，仍排除 `data/`、`.workbuddy/`、`.shots/`、`.demo/`、`promo/`、`vibe_images/`、`node_modules/`、`dist/`、`build/`，提交前逐项 diff 排除疑似密钥或无关大文件。
- 推送仅在用户明确要求时执行；推当前分支，不强推。
- 用户问「为什么 / 先回答我」时先给根因与数据，不先改代码；仅否定具体行为时保留相邻能力；批量导入/命名认不准时丢弃重来，不落猜测名字，只写确定字段。
