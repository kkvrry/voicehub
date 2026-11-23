# 引擎接入设计

> 本文说明一台 TTS 引擎怎样接入 VoiceHub：契约、目录、请求链路、判定归属与接入步骤。
> 总体架构见 [design.md](design.md) §4–5，参数定义见 [request-spec.md](request-spec.md)，硬性约束见 [AGENTS.md](../AGENTS.md)。

## 1. 边界

- 核心提供：统一请求、音色库、路由、并发闸、参考音频准备、配置与机密、界面自描述。
- 插件提供：一台引擎的配置模型、能力声明、参数映射、请求装配、HTTP 调用、音色清单与克隆、图标与文案。
- 核心不 import 任何具体插件；插件只依赖 `app.engines.base`、`app.engines.mapping` 与各自的子包。
- 模型服务与权重不随核心分发；插件只写请求侧（运行时管理类生产方插件除外，见 AGENTS §3.2）。

## 2. 接入原则

### 2.1 一台引擎 = 一个目录

请求装配、配置模型、音色获取、参数映射、错误翻译、图标与预设全部在该目录内。核心只通过基类与发现机制接触插件，删除目录即删除功能。加一台引擎不修改 `usecase/`、`api/` 与前端。

### 2.2 声明取代分支

以下事实全部由插件声明，核心与前端按数据分支，不按引擎名或 key 分支：

| 声明 | 回答的问题 | 消费方 |
|---|---|---|
| `Capabilities` | 这台引擎能做什么、部署在哪、上限多少 | 界面、网关、生产方、通道推导 |
| `SynthesisSpec` | 合成需要什么、接受什么格式、参考音频有何要求 | 网关、`usecase/reference.py`、界面 |
| `EngineMapping` | 统一参数与原生参数是否同一功能、落点在哪、怎么换算 | `apply_mapping`、参数映射页 |
| `mark_map` / `mark_passthrough` | 正文语气词标签认哪些、怎么写 | `prepare_text`、`GET /marks` |
| `ui_sections` | 引擎页有哪几块、顺序如何 | 页签 |
| `settings_schema` | 要配哪些字段、怎么画、哪些是机密 | 设置表单、保存校验 |
| `value_choices` / `field_presets` | 哪些取值、哪些现成说法 | 界面的候选与提示 |

### 2.3 单一规范入口

上游只说一种形状：`SynthesisRequest`。字段定义在 `app/engines/base.py`，界面参数表定义在 `app/domain/request_spec.py`，两者用 `SPEC_FIELDS` 对齐。引擎的私有字段先进入 `extra`，由该引擎的映射表决定落点；核心不解释 `extra` 的键。

「空 = 没要求」是规范的基本语义：空串不发送，`0` 是明确要求，输出格式空串表示由引擎按配置决定，不补默认值。插件与核心都不得把空值替换成默认值后再发送。

### 2.4 差异分三层收敛

| 层 | 位置 | 负责 |
|---|---|---|
| 声明层 | `<key>/plugin.py` | 能力、契约、映射表、语气词、页面结构 |
| 装配层 | `<key>/mapping.py` + `build_body()` | 统一参数 → 原生请求体；配置默认值补齐 |
| 传输层 | `<key>/client.py` | 端点、鉴权、序列化、响应解析、错误翻译 |

核心不认识任何一层的私有格式。装配顺序固定：先落上游显式给的值，再补配置默认值；反过来会让引擎自己的默认覆盖上游的明确要求。

### 2.5 发现不等于启用

发现到的插件全部进入适配清单（`GET /engines/catalog`）；左栏、生成页、生产方候选、`engines_for_voice` 与合成网关只认已启用（`usecase.engines.active_engines` 是唯一出口）。

- 启用清单存 `config.json` 的 `engines_enabled`；字段缺失时按「用过的」推导，不落盘。
- 保存设置顺手启用；停用不动配置与库内音色。
- 运行期停用按整台处置：批内第一行撞上 `engine_not_enabled` 即停掉该引擎剩余行，与「连不上」同一路径。
- 库与归档仍按全部插件看：停用引擎的保存目录仍要从扫描根摘掉，它留下的产物仍要能按 id 找到。

### 2.6 凭据是路由的唯一判定

一条音色落到某台引擎上，这次带什么由 `app/domain/credential.py::credential_of` 判定，结果只有四种：

| 凭据 | 含义 | 条件 |
|---|---|---|
| `platform` | 平台音色 id | 预置音色回到自己的平台 |
| `reference` | 参考音频 + 参考文本 | 本地音色、复刻音色、跨平台且有样例 |
| `design` | 一句音色描述 | 设计音色，任何声明 `voice_design` 的引擎都能用 |
| `none` | 落不到这台引擎 | 发请求前报 `voice_not_usable` |

两条硬规则：预置音色一律发平台 id，不拿它自己的样例复刻；样例是通用素材，任何接受参考音频的引擎都能借。

「音色通道」（`domain/channel.py`）只决定配置与分配弹窗的默认供给方式，不参与单次路由。网关不得读通道来决定发什么。

### 2.7 发前失败优于发后解释

能提前判定的问题在发出前拦下，并说明「没有发出去」：

- 必填缺失、格式不支持 → 网关 `validate_against_spec`；
- 字数超过引擎声明的上限 → 网关 `validate_against_spec`（`Capabilities.max_chars`）；有声书计划期另有一次批量预检，两处同源；
- 映射必填缺失、值认不出 → `apply_mapping`；
- 平台枚举、区间、模型与语气词的互斥 → 插件自己的守卫（`_guard_ranges` 一类）；
- 音色落不到引擎 → `credential_of` 返回 `none`，网关直接报错。

已经发出但没全量生效的部分写入 `SynthesisResult.notes`（引擎没接受）或 `prep_notes`（VoiceHub 代做了），两者在响应里分开。

### 2.8 能代做的由核心代做

`usecase/reference.py` 只按插件声明执行，不按引擎名分支：

| 声明 | 核心动作 |
|---|---|
| `reference_formats` | 不在列表内的容器先用 ffmpeg 转 wav |
| `reference_duration` | 超上限截到上限内并重听逐字稿；低于下限直接拒绝 |
| `needs_transcript` | 参考音频缺逐字稿时调用识别引擎补，并写回样例 |
| `reference_path_ascii` | 路径含非 ASCII 时复制到纯 ASCII 暂存目录再发 |

插件不重复实现这四件事；核心做完后把前三件写进 `prep_notes` 返回。

### 2.9 能力布尔分两类

`Capabilities` 里同时装着「路由事实」与「展示事实」，读法不同：

| 字段 | 性质 | 读它的地方 |
|---|---|---|
| `voice_listing` | 路由 / 发现 | 清单拉取、通道推导、分配候选；界面叫「内置音色」 |
| `voice_cloning` | 路由 | 平台注册式克隆、页签、分配候选 |
| `sample_cloning` | 路由 | 样本式克隆（样本即音色，平台不保存 id） |
| `voice_design` | 路由 / 能力 | 凭据判定、可合成引擎筛选、页签 |
| `reference_audio` | 路由 | 凭据判定、通道推导、分配候选；**不作为能力标签** |
| `max_chars` | 路由 / 限制 | 网关 `validate_against_spec`、有声书计划期；界面放在「引擎要求」 |
| `max_concurrency` | 路由 | 有声书并发上限 |
| `instruction_control` | 展示 | 「引导指令」能力标签 |
| `emotion_control` | 展示 | 「情绪控制」能力标签 |
| `languages` / `dialects` / `cross_lingual` / `language_note` / `language_hint` | 展示 / 路由 | 「支持语言」一栏与能力标签；前两者归一成的 `supported_codes` 决定网关发不发 `language`；`dialect_codes` + `language_hint` 只在已支持的方言格上提示口音来源 |

界面上三种克隆实现（`voice_cloning` / `sample_cloning` / `reference_audio`）只显示**一条**「音色克隆」，说明按实现选：平台注册 id、样本复刻、或每次随请求提供参考音频。`reference_audio` 只在「引擎要求」里出现（参考音频必填 / 可选 / 格式 / 时长），不再冒充能力。

新增能力字段前先回答：它会改变路由吗？会，就必须同时给出消费方与守卫测试；不会，才放进展示类。

`describe()` 的 `capabilities` 包**不手抄**字段：它从 `Capabilities` 的 dataclass 字段自动派生，只排除语言面（顶层 `languages` 单独下发）与 `sample_formats`（只给核心校验）。所以新增能力只需要改 dataclass；要让界面显示中文标签，再在 `CAPABILITY_LABELS` 里登记。

### 2.10 文案唯一产地在后端

引擎名、能力名、形态说明、页签名、通道说明、孤儿说明都只在后端定义（`engines/base.py`、`domain/channel.py`、`usecase/engines.py`）。前端只渲染下发值，不写引擎 key 分支、不写第二份中文。

插件 `client.py` 的报错文案是唯一例外：它需要说平台原话。这一部分也必须以 `label` 为准：客户端函数接收 `label`（关键字参数），由插件的 `synthesize` / `build_body` 传入 `self.label`，不另抄一份引擎名。

### 2.11 插件无状态，发现结果缓存

- 插件实例不保存请求状态；配置每次从 `config.json` 现读（文件按 mtime 缓存解析结果）。
- 发现结果按「基类 + entry point 组 + 内置根 + 插件文件」缓存到进程结束；装插件需要重启。
- 一个插件导入或构造失败只记 warning 并跳过，不拖死整库。

### 2.12 不做的抽象

不做「万能适配器」，不把各家请求体强行统一。参数映射只统一声明方式，装配仍在该引擎目录内。跨引擎的公共逻辑只在被至少两台引擎证明重复后上收，例如 `audio_http.py` 只服务同一套 `/v1/audio/speech` 的三台本机引擎。

## 3. 目录与文件

```
app/engines/builtin/<key>/
  __init__.py        必需：导出插件类（发现机制导入这个包，通常 from .plugin import XEngine）
  plugin.py          类定义、能力、契约、装配与调用入口
  settings.py        配置模型（pydantic + ui_hints）
  client.py          HTTP 调用、响应解析、错误翻译
  mapping.py         参数映射表（参数多或形状复杂时拆）
  voices.py          清单响应解析（仅清单结构复杂时拆）
  instructions.json  引导指令预设（可选，数据文件；与别家合并成一张全局候选表，不绑定本插件）
  icon.svg           平台图标（可选，缺省用中性图标）
```

- 引导指令预设是"可以照抄的一句话"，合并后全局共用：按全部已发现插件的 `instructions.json` 合并，按短名与正文去重（标点差异也算同一句），与启停无关。不认这一格的引擎在装配时被剔掉，所以别家写得再具体也不会出错——写给自己这台听得懂的说法即可。

- 内置发现按 `__init__.py` 认定子目录（引擎、识别、对话模型、向量模型同一套），导入包；包的 `__init__` 必须把插件类带到模块顶层。生产方插件写在 `plugin.py` 里，发现判据不同（`app/producers/registry.py`）。
- 第三方包在 `pyproject.toml` 声明 `voicehub.engines` entry point，同名覆盖内置。
- 识别引擎的目录相同，基类换成 `app/engines/asr_base.py`，识别没有音色与映射表。

## 4. 契约清单

### 4.1 身份与配置

| 成员 | 回答 | 默认 |
|---|---|---|
| `key` / `label` | 机器键与显示名 | 必须显式给出 |
| `version` / `homepage` | 版本与主页 | `0.1.0` / 空 |
| `secrets` | 额外机密字段名 | 空；模型字段也可标 `secret` |
| `settings_schema()` | 配置模型 | 空模型 |
| `secret_fields()` | 掩码名单 | 模型标记 + `secrets` |
| `readiness(cfg)` | 现在缺什么 | 空 = 就绪；不在这里发网络请求 |
| `configured_concurrency(cfg)` | 当前账号额度 | 读配置的 `concurrency`，0 = 未配 |
| `reconcile_settings(incoming, stored)` | 保存前的联动整理 | 原样返回 |

`settings_schema` 的每个字段都必须有 `label`（由 `ui_hints` 提供），否则界面无法渲染。读配置容错、写配置严格：坏值读取时丢弃并回落，保存时若本次提交的字段非法则拒绝。

### 4.2 合成契约

`SynthesisSpec` 是「发请求前要核对的事实」：

| 字段 | 含义 | 消费方 |
|---|---|---|
| `requires` | `SynthesisRequest` 中必须非空的字段名 | 网关 |
| `formats` | 接受的输出格式，第一个为默认 | 网关、界面 |
| `extra_schema` | 引擎专有参数的 JSON Schema | 参数映射页的标签与说明；核心不按它校验 |
| `reference_formats` | 参考音频能读的容器 | `reference.prepare` |
| `needs_transcript` | 给参考音频时必须有逐字稿 | `reference.prepare` |
| `reference_duration` | 参考音频可用秒数区间 | `reference.prepare` |
| `reference_path_ascii` | 参考音频路径必须纯 ASCII | `reference.prepare` |

`extra_schema` 当前只服务参数映射页的渲染；核心不按它校验。扩展参数**实际接受哪些**由 `accepted_extra_keys()` 从映射表 + `dynamic_mappings()` 派生，网关按它过滤，`describe()` 的 `synthesis_spec.accepted_extra` 下发同一份。插件读了没登记的私有键，请求里那一格会被剔除，并在 warnings 里说明。

约束随配置变化的引擎重写 `synthesis_spec_for(cfg)`；默认退回静态声明。`describe()` 会按当前配置下发实际那一份。

### 4.3 参数映射

`ParamMapping` 的语义：

| 字段 | 语义 |
|---|---|
| `source` | 统一参数名（请求字段或 `extra` 键） |
| `target` | 原生请求体的点号路径，只能 `a.b.c` |
| `transform` | 已登记的换算器名；未登记会在装配时抛错 |
| `skip_empty` | 上游没给就整个不发（默认 True） |
| `required` | 缺了在装配时报错 |
| `drop_unmapped` | 给了但换算不出：True 剔掉并把该格交给插件写入 `notes`；False 发前报错 |
| `note` / `warning` | 参数映射页的「说明」与重点标记 |
| `label` | 引擎专有参数的中文名 |

`EngineMapping.constants` 是每次请求原样带上的固定量（模型名、`stream: false` 等），会随 `describe()` 下发到前端，**不得放密钥**。`cautions` 是跨参数约束，界面逐条显示；「情绪与引导指令只发一路」由核心追加，插件不重复写。

`unsupported()` 回答「规范里有、这台没有落点」的项及原因，参数映射页按它画「—」。`dynamic_mappings()` 只回答「有落点但映射表表达不了」的项，不参与装配；它与映射表一起组成 `accepted_extra_keys()`，网关照这份白名单过滤 `extra`。`value_choices()` 只给枚举描述，不参与门禁：认不出的值由映射的 `drop_unmapped` 决定是剔除还是报错。

语言格另有一道声明检查：`capabilities().languages` 与 `dialects` 归一成的代码合起来是这台引擎认得的集合（`describe()` 的 `languages.supported_codes` 下发同一份）。网关（`usecase.gateway._settle_language`）在装配前剔掉集合外的 `language` / `reference_language`，按不指定发送并在 `notes` 回报；生成页「语言」栏按同一份列出不支持的引擎。不支持的语种仍列「不支持」；只有已支持的方言（`languages.dialect_codes`）才用 `languages.hint` 说明实际口音由什么决定（如 OmniVoice 的粤语由参考音频决定），提示摆在生成页音频条下面。统一词表外的代码不判——词表只覆盖界面能选的那些，600+ 语言的插件可能认得更多。

### 4.4 参数语义对齐

映射不是「名字翻译」，是「先证明同一个功能，再决定落点」。同名不同义是最高风险的错误：`speaker` 在统一层是 `instruction` 的历史别名，在别家可能指预置音色名；`volume` 可能一家是倍率、另一家是 0–10 档位。按名字直译时，错的那次通常不会报错，只会念错或静默失效。

映射前逐项过四问，四问都过才算同一个参数：

| 问题 | 要确认的事 | 不过时怎么处理 |
|---|---|---|
| 控制对象一致吗 | 两个字段控制的是同一个听觉维度：语速、音高、音量、情绪、引导、静默、种子、语言、格式 | 不一致就 `unsupported()` 写原因，不借最近的字段 |
| 单位与刻度一致吗 | 倍率 / 百分比 / 半音 / dB / 毫秒 / 0–1 强度 | 先换算再发；换算器必须登记，中文说法写进 `label` |
| 空值与默认语义一致吗 | 空 = 没要求；`0` 是明确要求；`-1` = 随机；空格式 = 引擎决定 | 语义不同就由插件在装配时特判，不能把空值补成默认值再发 |
| 生效条件一致吗 | 只在某些模型、模板、或给了参考音频时才生效 | 发前守卫，或写入 `notes`；不能静默发一个无效值 |

#### 判定结果与处理

| 情况 | 处理 | 例子 |
|---|---|---|
| 同一功能、同一刻度 | 直接映射 | `speed` → MiniMax `voice_setting.speed`（同为 0.5–2.0 倍率） |
| 同一功能、不同刻度 | `transform` 换算 | `pitch` 倍率 → MiniMax 半音；`guidance_scale` 0–1 → BreezeTTS CFG 0–8 |
| 同一功能、平台更窄 | 收窄枚举或区间；认不出按策略剔除或报错 | 情绪：MiniMax 八档，认不出剔掉并说明；语言：声明里没有的代码剔掉，按不指定发送 |
| 同一功能、映射表表达不了 | `dynamic_mappings()` 说明落点，装配留在插件 | `seed` 负数在插件里抽样；BreezeTTS 的「参考音频或引导至少给一个」 |
| 平台没有这个功能 | `unsupported()` 写原因，界面显示「—」 | GPT-SoVITS 没有 `lead_silence` / `lag_silence` |
| 一个统一参数对应多个原生字段 | 插件装配，`dynamic_mappings()` 只描述 | 情绪与引导在不同平台各走不同机制 |
| 多个统一参数共用一个原生字段 | 别名按同一落点登记，**正名排在最后**，同时给时正名覆盖别名 | BreezeTTS 的 `cfg` / `cfg_scale` / `guidance_scale` |

`speaker` 与 `instruct` 是旧版写法，网关在装配前统一并到 `instruction`；插件只认规范名，不再各写一份别名表。平台原生字段若也叫 `speaker` 但语义不同，映射的 `source` 应改用规范名或自造键，避免与核心别名撞车。

#### 落点位置也是语义

`ParamMapping.target` 必须是平台**真正读取**的那一层。BreezeTTS 的 `instruction` 放在顶层会被静默忽略，只有 `options.instruction` 生效；这类错误不返回 4xx，只会让参数「看起来发了、实际没生效」。落点只写点号路径，构造 `ParamMapping` 时校验；形状随请求变化的（数组下标、一个键两种含义）不要硬塞进映射表，用 `dynamic_mappings()` 说明并由插件装配。

#### 翻译只发生一次

| 层 | 负责 | 不负责 |
|---|---|---|
| `prepare_text` | 正文语气词标签的写法翻译与删除 | 参数换算 |
| `ParamMapping.transform` | 单值换算：单位、刻度、枚举、布尔、取整 | 改请求形状、读配置 |
| 插件 `build_body()` | 配置默认值、条件分支、跨字段守卫、别名优先级 | 在 client 里再做一次 |
| `client.py` | 序列化已装配好的请求体 | 任何参数语义翻译 |

同一个值被翻译两次，症状是「发出去的数字对不上界面」。换算器未登记时 `apply_mapping` 直接抛错，不静默用恒等函数；自定义换算用 `register_transform(name, fn, label="中文说法")`，`label` 就是参数映射页「换算」列。

#### 空值、区间与失败策略

- `skip_empty` 默认 True：空值不发送。`0` 不是空值，照发；`-1` 是种子自己的哨兵值，由插件抽成非负整数并在 `notes` 回报抽到的值。
- 区间校验分两层：`SPEC_FIELDS` 的 `min` / `max` 是界面滑块边界；平台更窄的硬区间由插件在发前守卫（MiniMax 的语速、音量、音高）。统一层不替平台截断，也不静默钳制。
- 认不出的枚举：情绪可以剔（整章不因一个词失败）；语言同样可以剔——把服务端不认的代码发过去会整段失败，而引擎自己会按正文判断，剔掉这一格更接近“不指定”。两种策略都要在 `notes` 里说清，不能静默。
- `value_choices()` 只给界面候选与逐行打标，不是白名单；真正是否接受由映射与插件守卫决定。

#### 本仓库的对照例子

| 统一参数 | 平台 | 对齐结果 |
|---|---|---|
| `pitch` 倍率 | MiniMax | 换算为半音整数，取最近整半音；平台只收整数 |
| `guidance_scale` 0–1 | BreezeTTS | 换算为 CFG 0–8（0.5 → 4），保留一位小数 |
| `emotion` 中文词 | MiniMax | 八档枚举；「中性」落 `fluent`，「平静」落 `calm`；认不出的剔掉并说明 |
| `emotion` 中文词 | BreezeTTS | 没有原生情绪字段，写成一句 `options.instruction`；认不出的词进模板，不丢 |
| `emotion` 中文词 | Kitta Audio | 写成「情绪：X。」并到 `instruction` 前面；同义先归一，认不出的原样保留 |
| `language` 代码 | OmniVoice | 只发语言表里的代码（`yue` 等）；`cmn-xa` 这类方言点不在表里，剔掉并按不指定发送；已支持的粤语在生成页提示口音由参考音频决定 |
| `language` 代码 | MiniMax | 转 `language_boost` 枚举；认不出剔掉这一格，退回设置里的语言增强值 |
| `lead_silence` / `lag_silence` | GPT-SoVITS | 无落点；句间停顿由 `fragment_interval` 配置控制，不能拿它冒充前后静默 |
| `seed` -1 | BreezeTTS / DotsTTS | 插件抽非负数再发，抽到的值写回 `notes` |
| `format` 空串 | MiniMax | 不发送，由 `audio_format` 配置补；空串不等于 wav |
| `volume` 倍率 | MiniMax | 同为倍率，直接发；平台允许 0–10，比统一滑块的 0.2–2.0 更宽 |
| `volume` 倍率 | Fish Audio | 换算为分贝（`20×log₁₀`），平台按分贝偏移接收 |
| `emotion` 中文词 | Fish Audio | 写成正文开头的 `[标签]`（英文词）；认不出的词原样放进去 |
| `instruction` 自由文本 | Fish Audio | 同上，`[自由文本]` 标签；与情绪同一落点，故不触发「只发一路」 |
| `bitrate` bps | Fish Audio | 换算为 kbps；平台只收 64/128/192，取最接近的一档并在映射页写明 |

#### 语义对齐的验证

1. 参数映射页逐项对照 `SPEC_FIELDS`：每个规范参数要么有落点，要么在 `unsupported()` 里有原因，不允许空着不说。
2. 每个数值映射在最小、中间、最大三个值上各发一次，检查「发送参数」日志里的实际落点与值。
3. 每个枚举各发一个认得的与一个不认得的，确认前者落到正确枚举、后者按声明剔除或报错，且 `notes` / 错误文案说明了原因。
4. 有条件的参数按条件各测一遍：模型切换、模板切换、有无参考音频。
5. 平台文档与实测冲突时以实测为准，并把结论写进 `ParamMapping.note` 或 `cautions`，不要只留在提交记录里。

### 4.5 语气词

- 统一写法 `[[笑]]` / `[[停顿:0.4]]`。
- `mark_map()` 登记「统一名 → 原生写法」；`mark_passthrough()` 声明「认任意标签」的模板。
- 网关在发出前统一调用 `prepare_text` 一次；插件重写它处理「写法随正文变化」的情况（BreezeTTS 按中英换括号）。
- 不在表里的标签从正文删除，不硬发。删除后正文为空则报 `empty_text`。
- 「通用」分组由 `domain/marks.py` 跨已启用引擎求交集；插件不参与这份计算。

### 4.6 音色

| 能力 | 方法 | 说明 |
|---|---|---|
| 清单 | `list_voices(cfg)` | 拉不到返回 `VoiceList(why=...)`，不抛异常；界面叫「内置音色」 |
| 克隆 | `clone_voice(cfg, CloneRequest)` | 平台保存音色 id；`sample_cloning` 时样本即音色，样本容器由 `Capabilities.sample_formats` 声明（空 = 默认 mp3 / wav） |
| 设计 | `clone_voice` + `design_prompt` | 平台不保存音色，描述随音色入库，每次合成重新生成 |
| 参考音频 | 核心供给 | `reference_audio=True`：每次合成把本地音色的样例交给引擎；界面合并显示为「音色克隆」 |

`VoiceMeta.id` 是发给平台的值，`name` 只给界面。清单每次调用都重新拉取，不缓存；`VoiceList` 只有 `items` / `why` / `cost_note`。

clone 与 design 共用 `CloneRequest`：`design_prompt` 非空走设计；`use_case.clone_voice` 按 `voice_design` / `voice_cloning` / `sample_cloning` 分流并负责入库。

### 4.7 页面与图标

`ui_sections()` 默认给出 `settings` / `voices` / `about`，声明克隆或设计能力时追加对应页签。键必须在 `SECTION_LABELS` 里，画不出的键会被核心丢掉。`UiSection.meta` 用于「克隆/设计各用哪个模型」这类事实，原样下发。

「能力与参数」页不是手写文案：能力标签、参数映射行、注意事项、参考音频要求与语言面都由 `describe()` 从 `capabilities()` / `capability_notes` / `mapping()` / `unsupported()` / `dynamic_mappings()` / `synthesis_spec()` 生成，前端只渲染。中文说法与逐条原因仍由插件/核心撰写，但"有哪些行、落点在哪、换算什么、支不支持"来自声明，不在前端另写一份。

「能力」卡只列用户能理解的能力：内置音色、音色克隆（三种实现合并为一条）、音色设计、引导指令、情绪控制、跨语言；顺序固定为引导指令在情绪之前。**文本上限是限制，不是能力**，与参考音频要求一起放在「引擎要求」里。

`synthesis_spec.accepted_extra` 是给网关与 API 调用方的机器可读契约，页面不单独列一栏；页面上看到的参数行来自 `mapping()`，两者与网关过滤同源。

`icon_svg()` 默认读插件目录的 `icon.svg`，缺失回中性图标。图标是 SVG 文本，不引入外部资源。

## 5. 请求链路

```
POST /api/v1/synthesize
  → api/sections/studio.py::synthesize
  → usecase.gateway.synthesize（统一记成败，批量时只记 debug）
      _route:
        1. 正文去空白；空 → empty_text
        2. 读音替换 pronunciation.apply（线程池）
        3. 取已启用引擎 active_engines → engine_not_enabled / engine_not_found
        4. prepare_text（语气词翻译与删除）
        5. normalize_voice（credential_of）→ platform / reference / design / none
           库中没有时回落：先按「引擎键:名字」查库，再问实时清单（仅有 voice_listing）
        6. settings_for(engine) → cfg（解密后的真实配置）
        7. _settle_expression：情绪与引导指令都有原生落点时只发指令
        8. reference.prepare（转码 / 截取 / 补逐字稿 / ASCII 路径，线程池）
        9. _settle_extra：只保留 accepted_extra_keys 里的键，其余剔除并记 warning
       10. validate_against_spec（requires、formats、max_chars）
       11. _call_engine：ratelimit.permit → engine.synthesize
             → build_body（apply_mapping + 配置默认值）→ client（HTTP）
      → SynthesisResult
  → api 层按 persist 落盘（none / cache / sample）并返回
```

日志口径：

- `发送参数` 一行记录实际请求的统一形状，排查「调了没生效」先看它。
- 失败在网关记 warning；批量合成由生产方带行号记，网关降为 debug，避免同一件事两份。
- 插件在 client 里记录真正发出的请求体（长文本截断、参考音频只留文件名或长度），日志里的引擎名同样取自传入的 `label`。
- 被剔除的扩展参数在网关记 warning，并进响应的 `warnings`（`code=parameter_ignored`），不静默消失。

`extra` 的边界：`SynthesizeIn` 在入口拒绝 `text` / `voice_id` / `emotion` / `format` 作为 `extra` 键（422 `invalid_request`）。这四项已由顶层显式承载；`as_values()` 末尾的 `update(extra)` 只留给 `reference_audio` / `reference_text` 这类覆盖口（见 `usecase/reference.py`），不能让 `extra` 变成第二份正文或音色。

## 6. 并发与限流

三个数分工：

| 数 | 来源 | 作用 |
|---|---|---|
| 项目设置并发 | 用户设置 | 默认总并发 |
| 引擎当前额度 | `configured_concurrency(cfg)` | 云端账号的 CONN 额度，用户可改 |
| 引擎硬上限 | `Capabilities.max_concurrency` | 能力上限，例如本机单模型服务 = 1 |

生产方取法：引擎配置额度优先；与硬上限同时存在时取较小者；都没有才用项目设置。多引擎同批另有一把总闸 `max(项目设置, 最大单引擎额度)`，避免总并发按引擎数翻倍。

限流是另一维：

- 云端平台的配额是 RPM，不是并发数；插件必须给 `rpm` 设置项。
- `engines/ratelimit.py` 按「引擎 key + 主机」建两道闸：并发信号量与滑动窗口，跨任务、跨项目共享。
- 平台明确回 `rate_limited` 时调用 `penalize` 让同一键统一冷却，再由网关按 2s / 6s 退避重试；不逐行判死。
- 本机单模型服务必须声明 `max_concurrency = 1`；并发只会增加排队时间。

## 7. 错误模型

`EngineError(reason, code, retryable)` 是合成链路的统一异常。`reason` 是面向用户的中文说明；`code` 供调用方分支；`retryable` 表示原样重试是否有意义。网关只对**本机**引擎消费 `retryable`（云端重试可能重复计费），且最多退避两次。

| code | 含义 | 典型来源 | 批量处置 |
|---|---|---|---|
| `engine_failed` | 服务在，但这次不行 | 4xx / 平台业务错误 | 该行失败 |
| `engine_unreachable` | 连接被拒、端口没人听 | `net_error` | 停掉整台引擎 |
| `engine_not_enabled` | 引擎已停用 | 网关 | 停掉整台引擎 |
| `engine_not_found` | 插件不存在 | 网关 | 停掉整台引擎 |
| `rate_limited` | 平台限流 | 插件翻译平台回复 | 冷却 + 退避，不判死 |
| `engine_unsupported` | 这台做不到该操作 | 基类默认实现 | 该行失败 |
| `empty_text` | 正文为空或标签删完 | 网关 | 该行失败 |
| `voice_not_usable` | 音色落不到该引擎 | 网关 | 该行失败 |

`CODES_ENGINE_UNAVAILABLE` 是「整台做不了」的集合；批量合成撞上其中任一码即停掉该引擎剩余行，不逐行标失败。

错误文案纪律：

- 网络层只说事实：引擎名、地址、失败形状，不推断「服务没启动」这类原因。
- 平台回的业务错误原样带出，前面加引擎名。
- 5xx 不重试；`rate_limited` 与**本机**引擎的 `retryable` 错误由网关退避重试；云端 503 不自动重发，避免重复计费。
- 错误体形状统一由 `app/api/errors.py` 生成，新代码不散抛 `HTTPException`。
- 新增错误码必须登记进 `CODES`；`tests/test_api_contract.py` 会扫描错误构造里的字符串 `code=`，漏登记即失败。

## 8. 配置与机密

- 表单由 `settings_schema().model_json_schema()` 渲染；字段的 `label` / `group` / `step` / `widget` / `choices` / `secret` 由 `ui_hints` 提供。
- 机密存 Windows DPAPI 加密后的 `config.json`；读接口掩码为空串并另给 `secrets_set`。
- 空串表示「不修改」；真正清空用 `{"_clear": ["api_key"]}`；恢复默认用 `{"_reset": true}`，机密保留。
- 保存时只校验本次提交的字段；旧版本留下的非法值不妨碍修改其他字段。
- 保存设置顺手启用该引擎，省一次「先开开关、再填配置」。
- 通道、保存目录与设置分开存（`config.channels` / `config.engine_outputs`），保存密钥不覆盖它们。

## 9. 接入步骤

1. 建目录 `app/engines/builtin/<key>/`，写 `plugin.py`，给出 `key` / `label` / 图标。
2. 写 `settings.py`：连接、密钥、模型、默认值、`concurrency` / `rpm`（云端）；每个字段带 `ui_hints`。
3. 声明 `capabilities()`：形态、清单（界面叫内置音色）、克隆、设计、参考音频、引导指令、情绪、语言、上限；复刻音色另声明 `sample_formats`。只声明真的能做的。
4. 声明 `synthesis_spec()`：必填、格式、参考音频要求。
5. 写 `mapping.py`：逐项按 §4.4 的四问确认语义，再写落点、换算、`unsupported()` 原因；插件会读、但映射表表达不了的私有键必须登记进 `dynamic_mappings()`，否则网关会把它当不认识而剔除。
6. 写 `client.py`：端点、鉴权、超时、状态码策略、错误翻译；网络失败用 `audio_http.net_error` 或同口径文案。对外函数接一个 `label` 关键字参数，文案里的引擎名只从这里取。
7. 实现 `build_body()` 与 `synthesize()`：先映射上游值，再补配置默认值；不静默丢参数；调 client 时传 `label=self.label`。
8. 按能力实现 `list_voices()` / `clone_voice()`；清单失败回 `VoiceList(why=...)`。
9. 需要时加 `mark_map()`、`field_presets()` / `instructions.json`、`ui_sections()`。
10. 跑验证：

```
cd web && npm run typecheck && npm run build
./.venv/Scripts/python.exe -m pytest tests/ -q
./.venv/Scripts/python.exe -m ruff check app/ tests/
node scripts/shot.mjs http://127.0.0.1:17865/ %TEMP%/vh-shots
```

11. 手动核对：启用/停用、就绪提示、设置保存与掩码、清单拉取、克隆/设计、合成成功、连不上、限流、超长文本、无落点参数。

最小骨架：

```python
class MyEngine(EnginePlugin):
    key = "my_engine"
    label = "My Engine"

    @classmethod
    def settings_schema(cls) -> type[BaseModel]:
        return MySettings

    def capabilities(self) -> Capabilities:
        return Capabilities(kind=KIND_CLOUD, voice_listing=True)

    def synthesis_spec(self) -> SynthesisSpec:
        return SynthesisSpec(requires=("voice_id",), formats=("mp3", "wav"))

    def mapping(self) -> EngineMapping:
        return MAPPING

    def unsupported(self) -> dict[str, str]:
        return {"pitch": "平台无对应参数。"}

    async def synthesize(self, cfg: BaseModel, req: SynthesisRequest) -> SynthesisResult:
        settings = _as_settings(cfg)
        body = apply_mapping(MAPPING, req.as_values())
        return await client.synthesize(settings, body, label=self.label)
```

## 10. 常见错误

- 声明了能力却没有实现（`voice_listing=True` 但 `list_voices` 用基类默认），界面出现一颗点了才报错的按钮。
- 把平台约束写进核心：样本格式、体积上限、路径规则、模型枚举应属于插件声明。
- 在 `client.py` 里另抄一份引擎名，`label` 改名后报错仍说旧名；客户端函数应接收 `label` 并由插件传入。
- 静默丢弃不支持的参数：应写入 `unsupported()` 或 `SynthesisResult.notes`。
- 按名字相似映射，不核对语义：`speaker` / `volume` / 静默这类词在不同平台含义不同，先过 §4.4 的四问。
- 把落点放在平台不读的那一层：请求成功、参数静默失效，比 4xx 更难发现。
- 平台没有的功能借一个相近字段顶上：`lead_silence` 不能拿句间停顿冒充。
- 空串补默认值再发：把「没要求」变成了「明确要求」，引擎行为会跟着变。
- 清单拉不到就抛异常：空表与「平台没有音色」外观相同，必须用 `why` 说明。
- 在事件循环里做同步 IO（同步 httpx、ffmpeg、读大文件），拖住全部并发请求。
- 本机单模型服务不声明 `max_concurrency = 1`，并发只增加排队。
- 平台有每分钟配额的云端插件不给 `rpm`，短句会打穿配额；没有该限制的要在插件说明里写清。
- 把密钥放进 `mapping.constants`，它会随 `describe()` 下发到前端。
- 映射落点写非点号路径，`ParamMapping` 构造时即报错。
- 在插件里再调一次 `prepare_text`，标签被翻译两遍。
- `build_body` 读了私有键却只在代码里认：契约里没有落点，网关会把它剔除；映射表或 `dynamic_mappings()` 必须先登记。

## 11. 保持不动的取舍

- 不做通用请求体适配器。各家形状差异大，统一声明方式、各自装配更可靠。
- 不做前端引擎分支。界面只渲染后端下发的能力、页签、映射与候选。
- 不把模型服务纳入核心。插件只写请求侧，运行时管理属于生产方插件的独立域。
- 不允许静默丢参数。认不出的值要么发前报错，要么剔除并在 `notes` 说明。
- 不把停用引擎从库与归档里抹掉。停用只影响调用与展示，不影响已有文件与音色。
