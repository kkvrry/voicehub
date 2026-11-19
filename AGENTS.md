# AGENTS.md — VoiceHub 开发规则

VoiceHub 是本地优先的有声书生产工作台：FastAPI 后端（`app/`）+ Vue 3 前端（`web/`）+ SQLite。

## 0. 项目与文档地图

| 路径 | 内容 |
|---|---|
| `app/` | FastAPI 后端：`api/`、`usecase/`、`domain/`、`infra/`、`engines/`、`llm/`、`producers/` |
| `web/` | Vue 3 + TypeScript + Vite 前端 |
| `tests/` | pytest；后端单元与接口测试 |
| `data/` | 运行数据：`config.json`、`hub.db`、项目库、缓存、录音、日志 |
| `docs/` | 长期设计文档，不是任务记录 |

长期文档：

| 文件 | 职责 |
|---|---|
| [`docs/design.md`](docs/design.md) | 总体架构、音色模型、插件协议、API、前端结构 |
| [`docs/request-spec.md`](docs/request-spec.md) | 合成请求的字段、单位、取值与映射机制 |
| [`docs/engine-integration.md`](docs/engine-integration.md) | 一台引擎如何接入：契约、目录、请求链路、步骤 |
| [`docs/audiobook-flow.md`](docs/audiobook-flow.md) | 有声书数据模型、阶段、状态、成品规则 |
| [`README.md`](README.md) | 用户向介绍、安装、快速开始与文档索引 |

不要新增 `docs/audit-*.md`、`*-plan.md`、`progress.md`、`*-report.md` 一类一次性文档。审计、计划、进度在任务结束后删除；长期有效的结论写进本文件或对应设计文档。文档用现在时，不写日期、提交哈希、代码行号与修改过程。

## 1. 文字与文案

适用于界面、日志、注释与文档。

1. 使用陈述句，不写口语与对话腔。
2. 一句结论优先于一段经过；不记录修改过程与版本轶事。
3. 控件名用名词短语；表格列名说明该列内容。
4. 说明只保留两类：非显然的约束、必须提前知道的风险。界面上可直接看出的操作不重复描述。
5. 每个界面用词都对应真实的数据或行为。
6. 重点说明逐条列出；正文说明不夹带强调。
7. 修改文案时同步修改引用：先 grep 测试与脚本（`scripts/shot.mjs`、`scripts/smoke.py`），两端一起改。

## 2. 契约与对接

- 本服务是契约的唯一来源：`/api/v1` 是唯一前缀，没有别名。
- 不为单个调用方加特例；契约演进保持版本兼容，响应形状不为某一家单独改变。
- 引擎差异由 `app/engines/builtin/<key>/` 吸收；调用方只使用规范参数。
- 前端不认识任何一台引擎：字段名、候选值、能力、分区、参数映射全部由后端下发。
- 文案的唯一产地在后端：`app/engines/base.py`、`app/domain/request_spec.py`、插件目录。前端不写第二份中文，也不在模板里写引擎名分支。
- **加一台引擎只加一个目录**，不修改前端与核心路由。

## 3. 插件域

### 3.1 四类插件

| 域 | 基类 | 职责 |
|---|---|---|
| TTS 引擎 | `engines/base.py` | 文本 → 音频 |
| 语音识别 | `engines/asr_base.py` | 音频 → 文本 |
| 对话模型 | `llm/base.py` | 消息 → 消息 |
| 生产方 | `producers/base.py` | 源文本 → 章节 → 音频 → 成品 |

四类共用发现机制（`builtin/<key>/` + entry points）、配置存取、机密掩码与恢复默认。发现实现只有一份：`app/infra/plugin_registry.py`。单个插件导入或构造失败只记 warning 并跳过，不拖死整库。

识别不套 `EnginePlugin`：识别没有音色与参数映射，只实现 `transcribe` / `readiness` 与能力自描述。生产方域可承载不带流水线的工具型工作区（如运行时管理）：`steps()` 返回空即可，宿主不校验步骤。

### 3.2 模型与依赖

- 核心不安装模型依赖、不下载权重、不维护模型清单。模型服务由用户自行部署，插件只写请求侧。
- 例外：运行时管理类生产方插件可在用户显式触发下下载、部署、卸载其自管的二进制与权重；范围限于插件数据目录与插件目录，不进入核心、不随安装包分发。插件自己的模型目录清单属于插件数据，不属于“核心维护模型清单”。
- 换模型 = 换插件目录；`usecase/asr.py` 等宿主代码不修改。

### 3.3 识别

- 能力与语言覆盖由插件逐项登记：文本 / 语种 / 情感 / 声音事件 / 时间轴。未声明即为不支持，界面如实列出。
- 请求与返回格式由插件声明（`api_format()`、`describe()`、`language_groups`）；前端不写死端点与语言表。
- 应用层开关按当前引擎声明开放；未声明的能力不显示开关。
- 音频统一由服务上传；输入格式按插件声明，非直接支持的格式由插件转换。
- 语种写回仅限主样例、仅填空、不覆盖；应用层开关关闭时跳过。
- 自动识别先过 `usecase/asr.unavailable_reason()`；批量补全另加一次 `reachable()` 探测，服务连不上就不起线程。

### 3.4 音色处理

- 音色处理是生产方域的工具型工作区（`voiceclean`，`voice_rail=true`、`rail_mode="pick"`，`steps()` 为空）：处理对象是音色库的样例行，不进流水线，也没有项目数据。**处理目标只有一个**——左栏选中的音色，或它的一条样例；目标存在宿主的 `store.pick` 里，插件不另存一份，也不做范围切换。平台音色没有本地文件，选它就只提示、不允许开始。
- 宿主按 `ProducerPlugin.rail_mode` 摆左栏：`pick` = 点卡即选中、卡右「选样例」挑一条，`assign` = 卡右「指定」交给工作区。两种都对音色卡生效，宿主不认识具体插件。
- 分工固定：核心做 ffmpeg 规整（单声道、采样率对齐、高通 60 Hz、限峰 −1 dBFS），插件只编排。模型一律走本机 audio.cpp 的 `POST /v1/tasks/run`（`task` 为 `sep` / `s2s`），端点与 TTS 的 `/v1/audio/speech` 不同，不能套 `engines/audio_http.speak`。
- 步骤与模型白名单在 `producers/builtin/voiceclean/models.py`：加一步或换模型只改这里，前端按后端下发的步骤画卡片。超分（AudioSR / UniverSR）不列进降噪——它补出的高频是模型生成的，做训练素材会被当成音色。
- 处理结果先落任务目录，原声与结果各一份，确认后才替换。替换由 `LibraryService.replace_sample_audio` 完成：文件名不变（主样例、逐字稿、平台凭据都按路径认这一条），原件进回收站，处理链写进 sidecar 的 `samples[file].params`。
- 时长偏差超过 50 ms（`service.DURATION_TOLERANCE`）的条目不参与替换，只在结果表里标原因；单模型并发 1，同一时刻只跑一个任务，取消在条与条之间生效。

## 4. 音色通道与引擎面板

- 发现不等于启用：未启用的引擎只出现在引擎清单里，左栏、生成页勾选、生产方候选、分配候选、语气词交集与合成网关一律只认已启用（`usecase.engines.active_engines` 是唯一出口）。启停只有一个控件语言：开关；停用不动配置与库内音色。
- 引导指令预设不看启停：这一格是自由文本，预设只是"可以照抄的一句话"，按**全部已发现**插件的 `instructions.json` 合并（`usecase/vocab.py::_default_instructions`），并按短名与正文去重（标点差异算同一句）。按已启用收束会让候选随开关增减，用户读到的是"词被删了"；不认这一格的引擎在装配时把它剔掉，不会出错。
- 库与归档按全部插件看：停用引擎的保存目录仍要从扫描根里摘掉，它留下的产物仍要能按 id 找到。行的参数面板同理——按插件身份取，回答“它能调什么”，与启停无关。
- 生成计划只排已启用的引擎：指向停用引擎的行在计划里摘掉、按引擎记一条提示，不逐行撞网关；重新启用后同一批行照旧能排进计划。
- 计划算完到跑完之间被停用的，批内在第一行撞上 `engine_not_enabled` 就停掉这台引擎的剩余行：与「连不上」同一处置，不逐行报错、不标失败（`engines/base.py::CODES_ENGINE_UNAVAILABLE`）。
- 启停会换掉语气词「通用」组（已启用引擎之间的交集）。前端在启停、保存引擎设置之后必须重取这一份（`store/engine.js`），不得沿用会话内缓存；引导指令预设与此无关——它按全部插件合并，启停不改变候选。
- 通道表示「这台引擎使用哪些音色、如何供给」，默认值由能力推导，可配置范围以能力为上限。通道只决定配置与分配默认，不参与单次路由；网关不得读通道来决定发什么。
- 一条音色落在一台引擎上带什么凭据（音色 id / 参考音频 / 设计描述），只在 `app/domain/credential.py` 判定；网关、生产者与前端不各自再推一遍。若将来通道要影响路由，只能改 `credential_of`。
- 预置音色一律发平台音色 id，不拿它自己的样例复刻；样例只服务本地音色、复刻音色与跨平台借用。
- `library` 通道不把引擎保存目录当扫描根；生成物一律挂回自己的音色（平台或本地）。
- 引擎行是管理入口：行上不挂拉清单按钮与音色计数；这些操作在面板的「音色」分区。
- 清除音色是引擎级动作：删掉该来源在库内的全部条目，平台侧数据与磁盘上的样例不动（生成的样例留在保存目录，由用户自行处理）。入口在面板「音色」分区，按实际清除条数回报。
- 面板页签由插件 `ui_sections()` 声明，核心过滤后下发；前端不判断某台引擎是否应有克隆页。
- 「恢复默认」只作用于插件设置，不复位通道。

## 5. 表达标注

- 统一写法为双中括号：`[[笑]]`、`[[停顿:0.4]]`。
- 引擎不支持的标签在装配时删除，不硬发。删完正文为空则报 `empty_text`。
- 「通用」分组由核心跨引擎计算（`domain/marks.py`）：所有支持语气词的引擎的交集；认任意标签的引擎按全集计入。
- 插件登记标签用 `MARKS` 的规范名；照平台原文写法登记也可以，别名在装载时归一。新增或改动声明后跑 `scripts/marks_audit.py` 核对未收录的写法。
- 本层只负责发送；插件声明写法（`mark_map` / `mark_passthrough`），翻译由网关在发出前统一执行一次（`EnginePlugin.prepare_text`），插件不各自调用。

## 6. 能力与参数页

页面默认四段，顺序固定：**能力 → 支持语言 → 参数映射 → 引擎要求**；语气词标签表按需附在最后。新增段落必须在 `ui_sections()` 中声明并先更新 `docs/design.md`，不得让前端临时插入。

- 能力卡只摆标签；「跨语言合成」是其中一枚，**标签后面不写解释**。
- 语言与方言是清单，摆在「支持语言」一栏：一行语言、一行方言，名字只罗列。同时算方言的词（粤语）只在方言里出现。未声明即不画，不写「不支持」。
- `describe()` 的 `capabilities` 不手抄字段：从 `Capabilities` 的 dataclass 自动派生；新增能力先改 dataclass，再决定是否登记 `CAPABILITY_LABELS`。

### 6.1 参数映射表

列固定为：**项目规范 | 映射位置 | 换算 | 说明**。

- 按 `SPEC_FIELDS` 逐项列全，顺序一致；无落点写「—」并说明原因，不允许省略该项。
- `reference_audio` / `reference_text` 不在 `SPEC_FIELDS` 的映射行里：前者归「引擎要求」，后者是请求字段、由样例自动带出。不要为了“列全”把它们塞进参数映射页。
- 说明写在行内，不放在表下。
- 落点由插件声明：静态映射用 `ParamMapping`；形状随请求变化的用 `dynamic_mappings()`；无落点用 `unsupported()`。重复别名不单列。
- 换算列写中文说法（`register_transform(..., label="倍率 → 半音")`）；未登记时退回登记名。
- 跨参数约束写在 `EngineMapping(cautions=...)`；行内重点用 `ParamMapping(warning=True)`。

### 6.2 引擎要求

- 参考音频的容器、逐字稿、必填条件只能由插件在 `synthesis_spec()` 中声明；前端与核心不按引擎名分支。
- 核心按声明自动处理：`usecase/reference.py` 按 `reference_formats` 转码，按 `needs_transcript` 补逐字稿；处理结果随合成结果返回。
- 界面用关键词标签表达要求，自动处理用一行短说明。

### 6.3 插件默认配置

不在面板中单列默认值表。设置表单按插件模型默认值渲染，「恢复默认」由后端按同一模型复位。

## 7. 界面规范

外观分四套：`classic`（原色）、`paper`（纸墨）、`celadon`（素瓷）、`glass`（玻璃）。默认原色；原色、纸墨与素瓷不使用模糊与背景光，玻璃可调模糊与圆角。

非玻璃外观的令牌只覆盖颜色与形状，组件里写死的渐变、内高光、毛玻璃要在各自的 `look-*.css` 末尾按选择器压平——只换令牌压不住 `linear-gradient` 与 `backdrop-filter`。

### 7.1 令牌

- 颜色、圆角、间距、阴影、透明度、动效的值写在 `web/src/settings/theme.css` 的 `:root`；深色覆盖 `:root[data-theme='dark']`。
- 组件只引用 `--sh-*` 变量，不写十六进制色值，也不写 `var(--x, #hex)` 兜底。例外只有主题/外观层（`theme.css`、`look-*.css`）与插件图标 SVG 数据。
- 旧变量名（`--bg` / `--panel` / `--accent` / `--radius`）在 `web/src/style.css` 中作为 `--sh-*` 的别名，不复制色值。
- 圆角与间距使用阶梯变量：`--sh-r-*`、`--sh-radius-card`、`--sh-gap-1…6`。
- 强调色与密度是用户偏好，挂在 `.app-shell` 上覆盖；基础令牌在 `:root`。
- 风格层（`look-*.css`）只覆盖令牌与必要的硬编码形状，选择器统一带 `:root[data-look='<key>'] .app-shell` 前缀。

### 7.2 组件

- 公共控件位于 `web/src/components/ui/`：`ActionButton`、`TextField`、`ToggleSwitch`、`RangeSlider`、`SegmentedControl`、`ChipToggle`、`PanelCard`、`SchemaFields`、`ChipSelect`、`TagChips`、`Popover`、`GenderPicker`、`MicButton`。
- 设置页组合件（`SettingCard` / `SettingRow` / `SettingsSection`）位于 `settings/ui/`。
- 优先使用公共控件。使用原生控件时：输入与下拉挂 `.sh-field`，按钮挂 `.sh-btn`（`is-primary` / `is-ghost` / `is-soft` / `is-icon`），不新增第三套类名。
- 设置项按后端 `ui_hints(group=...)` 分组；依赖项置灰并说明条件。
- 插件设置表单（引擎 / 对话模型）使用平面表单，不套卡片；设置页仍使用 `SettingCard`。
- 插件设置与设置页共用 `SchemaFields` 行表：分组、行、控件列宽由宿主提供，插件只交 schema。
- 焦点态只有一层：控件自绘边框与 ring，并设置 `outline: none`；全局 `:focus-visible` 只用 `:where()` 兜底。

### 7.3 图标

- 只使用 `lucide-vue-next`，不混用字符图标。
- 尺寸只用 12 / 14 / 16 / 18 / 20 / 24；描边统一 1.8。
- 图标按钮必须带 `title` 与 `aria-label`；默认色 `--sh-text-3`，悬停或选中为 `--sh-accent`。

### 7.4 版式

- 页面 = 标题行 + 卡片栈；卡片间距由容器 `gap` 统一。
- 数据表页（创作）不叠卡：表头、工具条、表格依次铺在平面上，用分隔线分层。
- 应用页（引擎 / 日志）限制在 `--sh-page-max` 内居中；卡片墙铺满，通过网格增加列数。
- 行内动作分两层：主操作紧邻状态（未生成 = 生成、已生成 = 播放、失败 = 重试）；低频操作收进行尾「⋯」菜单。
- 同一动作只给一个入口；底栏常驻按钮放在 `panel-foot`。
- 播放条嵌在顶栏里、右沿贴住居中搜索框（留 12px），只在有东西可播时出现：没有边框与底色，一行写完传输键、曲目标题与样例位置，以及收藏 / 指定 / 开卡 / 关闭；标题用样例文件名（音色卡队列用音色名），不写逐字稿。进度是它下沿的一条渐变线，播放时有流光，不占行内宽度。宽屏下它不改变搜索框的位置；宽度不足时依次收起名字（1300 以下）与「收藏 / 指定」（1260 以下），1100 以下顶栏没有中缝，播放条回到行内排、搜索框随之让宽。搜索框不在这一页时退回音色栏上方。
- 键盘可达：可点元素使用 `<button>` 或带 `role` 的元素；焦点环由控件自绘，全局规则只兜底。
- 只有一个壳 `AppChrome`：设置页是壳内一屏，不要再给页面套第二层壳；弹层只走 `<Teleport :to="overlayTarget">`（`web/src/composables/overlayTarget.js`）。
- 生产方工作区由插件自描述：`voice_rail=false` 的工具型工作区同时隐藏左栏音色卡片、音色结构树与音色搜索；宿主不按插件 key 判断。
- 当前布局：音色管理入口是行表上方最右侧的一颗按钮；分配表在弹窗里改，工作区右列是章节目录（平板 ≤1400px 改为表格上方的横向条，≤1100px 收起音色卡片列）。这是当前设计，不是不可变规则；改动时同步 `docs/design.md`。
- 长操作（扫描、补全、批量合成、合并）必须立即显示状态与进度，不能只留完成日志。短设置直接展开；多预设用下拉切换 + 保存/添加，不铺平列表或另开编辑弹窗。

### 7.5 并发

- 合成并发取 `min(设置并发, 插件声明的 max_concurrency)`；引擎自己配了并发额度时按它的额度，不受项目设置盖住。
- 多引擎同批另有一把总闸：`max(设置并发, 最大单引擎额度)`，避免总并发按引擎数翻倍。
- 本机单模型服务（audio.cpp 的 GGUF 实例）必须声明 `max_concurrency = 1`；并发只会增加排队时间。
- 云端引擎不声明上限时，按用户设置执行。
- 云端平台的配额是 **RPM**，不是并发数：一条短句几百毫秒就返回，并发再低也能把每分钟请求数打穿。平台有每分钟配额的云端插件必须给出 `rpm` 设置项，请求一律经 `app/engines/ratelimit.py` 的闸口（并发位 + 滑动窗口）；平台没有该限制的在插件说明里写清。平台回限流时按错误码 `rate_limited` 冷却整台引擎并退避重试，不逐行判死。

## 8. 数据、文件与安全

- 原子写：小文件用 `app/infra/atomic.py` 的 `write_bytes` / `write_json`；临时文件名带 pid/线程/随机后缀，写入后 `os.replace`。不要直接写最终路径。Windows 上 `os.replace` 仍可能被占用（`WinError 32`），媒体发布走 `app/infra/ffmpeg.py` 的退避重试。
- 配置：写配置走 `save_config` 的整份替换。当前没有跨请求锁，`atomic.write_json` 仍用固定 `.tmp`，坏 JSON 会回落空配置——这三项是已知边界；新增并发保存路径前先补锁、唯一临时名与坏文件保留。保存引擎设置顺手启用；空串表示不修改，`{"_clear": [...]}` 清机密，`{"_reset": true}` 恢复默认但保留机密。
- SQLite：用 `app/infra/sqlite.py` 的连接子类，`with` 结束即 close；不要新开裸 `sqlite3.connect`。
- 路径：用户给的相对名只取 `Path(name).name`；拼接后必须走 `archive.resolve_within`。删除、迁移、打开目录、自定义可执行文件等破坏性/本机动作只允许本机来源，用 `app/api/localonly.py`。
- 子进程：一律 argv 列表，禁止 `shell=True`；ffmpeg/ffprobe/`audio.cpp` 的输出显式 `encoding="utf-8", errors="replace"`，否则中文 Windows 会 GBK 解码失败。
- 异步：事件循环里不做同步 httpx、ffmpeg、读大文件或阻塞 IO；放线程池。
- 单实例：同一数据目录只允许一个服务进程（`data/service.lock`）。不要为了调试再起第二个实例；两个进程写同一批行会落下半截音频。
- 任务：`jobs` 带 `owner` / `heartbeat_at`，启动时只回收“进程已不在或心跳超时”的任务，不盲清 `running`；行合成前先 `claim_row`，`row_claims` 抢不到就跳过；失败或取消释放占位。
- 幂等：付费调用先 `idempotent_begin` 抢占，失败/取消 `abandon`；不要先调引擎再补记录。
- 失败行的目标行为是只写 `error` / `status='failed'`、保留 `audio` / `audio_key`；当前 `set_row_error` 仍会清空（已知边界）。只有参数改动产生的 `stale` 行能自动恢复，不要把失败行当可恢复。
- 批量合成撞上 `engine_unreachable` / `engine_not_enabled` 时停掉整台引擎的剩余行，不逐行写失败。
- audio.cpp：实例随 VoiceHub 退出（Job Object）；不要收养外部进程、不要跨进程保活；自动拉起前确认端口没有活实例。`exe` / `models_root` 只接受本机来源；安装包必须有 sha256 并校验。
- 识别输入：非 WAV 先转 16 kHz 单声道；TTS 参考音频按插件声明的容器转码。转码缓存按目标路径串行，中间文件唯一命名。麦克风录音听完即删；参考录音放 `recordings/`，服务重启清空。
- 内录：只能在本机采（WASAPI loopback，`infra/loopback.py`），浏览器不参与。热键是系统级资源，只由托盘注册；核心只提供起停接口、状态与错误码，不判断托盘是否在跑——`hotkey_ok` 为 `null` 即“未注册”。采集不套 16 kHz 链路：48 kHz 单声道原样落盘；全程静音不是失败，用 `peak` / `silent` 说明。
- 识别语言写回：仅主样例、仅填空、不覆盖；非主样例只回显。
- BGM：`bgm_gain_db` 是相对人声的增益，不是绝对 dBFS；开启响度标准化时 `-20 dB` 约等于旧脚本 `-35 dBFS` 的绝对电平。不要把它改成绝对值。

## 9. 日志

- `app/infra/loghub.py` 是全项目日志的唯一出口：进程内自增 `seq`、环形快照、SSE 广播；写文件是 `FileSink` 的事。
- 一个坏去处不能连累其他去处；`emit` 永不抛。
- 生产者是任意线程，入队必须 `call_soon_threadsafe`；不要跨线程直接塞 `asyncio.Queue`。
- 环形转过一圈后，快照必须明说“缺了一段”；假装连续比明说缺了更坏。
- 前端日志流重连要有退避和上限；离开页面停止轮询。

## 10. 有声书流水线

细节见 [`docs/audiobook-flow.md`](docs/audiobook-flow.md)。硬规则：

- 拆句只切不改字；一种引号写法贯穿一本书；段落内短旁白可并入下一行，跨说话人的对白不合并。
- 行音频指纹用 v2：文本 + 引擎 + 引擎身份（插件版本 / 模型 / 后端）+ 音色 + 情绪 + 样例 + 行级参数 + 该行实际命中的读音替换。输出格式不进 v2 键；不要为了“修指纹”把格式加回去。
- 改动说话人、情绪、文本、行参数或音色只标 `stale`，保留旧音频；读取/合成/合并前 `_settle_stale` 重算指纹，能对上就恢复。合并遇到不能恢复的 `stale` 行拒绝。
- 失败行的目标是不清 `audio` / `audio_key`；当前 `set_row_error` 仍会清空（已知边界），旧音频只在 `stale` 行可恢复。
- 合成计划按已启用引擎排；停用引擎的行计划期摘掉，运行期撞上就停整台。
- 单模型本机服务（audio.cpp 的 GGUF 实例）声明 `max_concurrency = 1`；云端引擎按 RPM 闸口。
- 合并按行序；本章/全书/字幕/BGM/M4B 的产物按章节落盘；成组原子性尚未完全保证，合并前检查 `stale`。
- BGM 增益是相对值；别用绝对 -35 dBFS 的旧口径解释新设置。
- 分析缓存键包含通道、模型、提示词、书、上下文、情绪词表与温度；候选角色不进键。

## 11. 验证

```bash
# 后端
./.venv/Scripts/python.exe -m ruff check app/ tests/
./.venv/Scripts/python.exe -m pytest tests/ -q

# 前端
cd web && npm run typecheck && npm run build

# 接口冒烟（服务已启动）
./.venv/Scripts/python.exe scripts/smoke.py --base http://127.0.0.1:17865

# 界面自检与截图（Git Bash；cmd/PowerShell 用 %TEMP%）
node scripts/shot.mjs http://127.0.0.1:17865/ "$TEMP/vh-shots"
```

- 用 `.venv/Scripts/python.exe`（3.12），不要用系统 Python 3.10；测试前清 `NO_PROXY` / `no_proxy`（或走 `app/infra/netenv.py` 的归一），否则 httpx 解析 `[::1]` 会失败。
- 跑服务先设 `VOICEHUB_DATA_DIR=F:/ai/audiobook-studio/data`，否则会落到空的 `LOCALAPPDATA`。
- 新增 `.js` 或 `<script setup>` 必须写 `// @ts-check`；`npm run typecheck` 只检查 `.ts` 与带标记的文件。不要用关闭 `checkJs` 或堆 `any` 来“修绿”。
- `scripts/shot.mjs` 需要运行中的服务与 Chrome；typecheck/build 通过不等于界面正确。四套外观（classic / paper / celadon / glass）与浅/深色都要看；截图点击会写盘时，指向 demo 库副本。
- 不把测试数量写进文档或断言文案；数量会变，行为不会。

## 12. 常见错误

**环境与验证**

- 用系统 Python 3.10 跑测试 → 依赖缺失或行为不同；一律用 `.venv`。
- 不设 `VOICEHUB_DATA_DIR` 就启动 → 读到空的用户配置，误判“配置丢了”。
- 新 JS 文件忘记 `// @ts-check` → 类型检查静默跳过，问题留到运行期。
- 只跑 typecheck/build 就宣称界面正确 → 实际布局/交互没验；至少跑 `shot.mjs` 或明确说明未做浏览器验证。

**后端**

- 直接写最终文件或沿用固定 `.tmp` 名 → 并发写坏或留下半截文件；新写盘用 `atomic.write_bytes` 或自建唯一临时名（`write_json` 目前仍是固定名）。
- 配置保存没有跨请求锁 → 读改写窗口内并发保存会丢更新；改动配置写入时优先补锁，不要新增并发保存路径。
- 裸 `sqlite3.connect` → Windows 句柄不释放，撞上 ffmpeg 的文件占用；用 `infra/sqlite.py`。
- 直接拼接客户端路径 → 目录穿越；用 `resolve_within`，删除/迁移接口加 `localonly`。
- 子进程 `text=True` 不指定编码 → 中文 Windows GBK 解码失败；显式 UTF-8 + replace。
- 在 async 路由里同步调 httpx/ffmpeg → 拖住整个事件循环；放线程池。
- 付费调用先发后记 → 重试/双击重复计费；先 `idempotent_begin`。
- 行合成不抢 `claim_row` → 两个任务同时写同一行。
- 失败时清 `audio` / `audio_key`（当前 `set_row_error` 行为）→ 只有 `stale` 行能恢复；改该函数时按“失败保留音频”的目标行为处理。
- 逐行处理 `engine_unreachable` → 满屏失败；整台引擎停。
- 按名字相似做参数映射 → 静默发错/失效；先过四问。
- 空值补默认值再发 → 把“没要求”变成“明确要求”；不要补。
- 插件里再调一次 `prepare_text` → 标签翻译两遍。
- 前端认识引擎 key 或写死引擎名 → 加引擎要改前端；所有文案/能力从后端下发。并发说明写“云端引擎”，不要写具体厂商。
- 轮询不设上限/卸载不清理 → 服务停了永久重连；指数退避 + 停止条件。
- 给页面再套一层壳 → Teleport 绑到旧元素；只用 `AppChrome` 与 `overlayTarget`。
- 组件里写死颜色/第三套类名 → 主题切换失效；用 `--sh-*` 与 `.sh-btn` / `.sh-field`。
- 工具型生产方只隐藏音色卡片、不隐藏音色树/搜索 → 页面仍归音色库管；按 `voice_rail` 一起收。
- 直接收养 audio.cpp 外部进程或跨进程保活 → 生命周期失控；只认自管实例。
- 把 BGM 增益当绝对 dBFS → 响度完全不对；它是相对人声的增益。

**文档**

- 把一次性审计/计划/进度写进 `docs/` → 下一次会话读到过期行号；结论写回长期文档后删除。
- 在文档里写“当前最大 N 条”“测试 N 项” → 数据一变就过期；用“数千条”或省略。
- 改了规则只改代码不改 `AGENTS.md` / 设计文档 → 下一位 agent 按旧规则改回去。

**已知边界（不要假装已解决）**

- 配置保存没有跨请求锁；`atomic.write_json` 仍用固定 `.tmp`；坏 JSON 会静默回落空配置。不要把并发保存当成安全，改动该模块时优先补锁、唯一临时名与坏文件保留。
- `set_row_error` 会清空 `audio` / `audio_key`；只有参数改动产生的 `stale` 行能自动恢复。不要把失败行当可恢复，也不要在恢复逻辑里依赖失败行的旧音频。
- 合并产物的字幕/BGM/音频成组原子性、跨进程合并锁、前端单元测试仍未覆盖；不要从“合并成功”推断三者一定同版。

## 13. 文档与提交

- 行为、契约、规则变化时，同一次改动里更新对应文档；不要留“代码已改、文档下回再说”。
- 文档只保留长期有效的结论；一次性材料在任务结束前删除（git 历史仍可查）。
- 默认只提交本次任务涉及的文件：先 `git status` / `git diff` 核对，再 `git commit --only <自己的路径>`。工作树可能同时有其他会话的改动，不要用 `git add -A` 把别人的工作卷进来。
- 用户明确说「提交所有更新」时，才检查并包含全部未提交的项目文件（包括此前已有的改动）；仍排除个人/临时目录：`data/`、`.workbuddy/`、`.shots/`、`.demo/`、`promo/`、`vibe_images/`、`node_modules/`、`dist/`、`build/`。提交前逐项看 `git diff`，排除疑似密钥或无关大文件。
- 推送只在用户明确要求时执行；推当前分支，不强推。
- 用户问“为什么 / 先回答我”时，先给根因与数据，不要先改代码。
- 用户只否定某个具体行为时，保留相邻能力；改一句提示词不等于关掉整个功能。
- 批量导入/命名认不准时丢弃重来，不落猜测的名字；重做前先对齐硬对应方案并做数量对账。批量导入只写确定的字段，不自动补描述、来源标签或多套一层目录。
