# 媒体工作流（配音）设计方案 v2

> 输入音视频，按可组合的操作产出成片与字幕。
> 本版相对 v1 的改动：固定九步改为**类型化对象 + 可组合操作**；角色成为对象；听写页并入本工作区。
> 设计稿：`docs/design/dubbing-station.html`。
> 已落地部分见 §0：本版先做「抽音轨 → 转录 → 逐段合成 → 贴齐 → 混音 → 导出」，
> 分轨、变声、配乐属于变换域，尚未实现。

## 0. 已落地范围（v0.1）

导航新增「配音」（`producers/builtin/dubbing/`）。工作区三列，右列是七步状态机，
不建可拖拽的节点画布；步骤状态由后端算，界面不自己推。

```
上传（音视频）
  → 抽音轨      ffmpeg 解复用，统一 44.1k 立体声 PCM
  → 转录        ffmpeg silencedetect 切段 → 逐段走 usecase/asr
  → 角色        旁白默认存在；每段可改角色，角色决定音色
  → 配音        gateway.synthesize 逐段合成，产物统一 44.1k 单声道 PCM
  → 贴齐        按时间码补静音、加速贴齐、越界顺延（usecase/assembly.py）
  → 混音        人声与原声按电平混合，按设置的响度目标校准
  → 导出        视频封装 MP4（画面拷贝，可选 mov_text 字幕轨）/ 音频转 MP3·M4A·WAV
```

与设计稿的差异，都是"当前只做有依据的部分"：

| 设计稿 | 已落地 | 原因 |
|---|---|---|
| 五条轨（原声/人声/伴奏/配音/配乐） | 三条（原声/配音/成品） | 分轨与配乐未接入，画出来就是空轨 |
| 九步含分轨、变声、配乐 | 七步 | 变换域未实现 |
| 工作流可保存、可换模板 | 单链路 | 图编辑留到有第二个链路之后 |
| 段落表虚拟滚动 | 已实现（固定行高 + 可视窗口） | — |
| 段色带 canvas | 已实现 | 2000 段不逐段建 DOM |

接口（`/api/v1/producers/dubbing`）：

| 方法与路径 | 作用 |
|---|---|
| `GET /overview` | 项目列表、存储目录、识别可用性 |
| `POST /projects` | 上传音视频建项目（探不到时长即拒收） |
| `GET /projects/{id}` | 项目、段落、角色、七步状态、任务、产物路径 |
| `DELETE /projects/{id}` | 删项目（源文件与产物一并删） |
| `POST /projects/{id}/extract` | 重新抽音轨 |
| `POST /projects/{id}/options` | 原声音量等混音选项 |
| `POST /projects/{id}/transcribe` · `/transcribe/cancel` | 转录与取消 |
| `PUT /projects/{id}/cues/{cue_id}` | 改文本 / 角色 / 时间码（改文本与角色会把该段标为过期） |
| `POST /projects/{id}/cues/{cue_id}/split` · `/merge` | 按时间点拆分、与下一段合并 |
| `DELETE /projects/{id}/cues/{cue_id}` | 删段并重排序号 |
| `POST /projects/{id}/roles` · `PUT/DELETE .../roles/{id}` | 角色增删改（换音色只影响该角色名下段落） |
| `POST /projects/{id}/synth` · `/synth/cancel` | 逐段合成与取消 |
| `POST /projects/{id}/assemble` · `/export` | 贴齐混音、导出成片 |
| `GET /projects/{id}/media/{kind}` | `source` / `original` / `vocal` / `mix` / `cue` / `subtitle` / `export` |

数据落在 `<数据目录>/producers/dubbing/projects/<id>/`：
`source.*`、`audio/original.wav`、`work/{segments,tts,assemble}/`、`out/{vocal.wav,mix.wav,subtitles.*,成品}`。
项目元数据在同级 `dubbing.db`（projects / cues / roles / jobs 四张表，WAL）。

两条实现口径：

- **时间码来自实测边界**。切段用 `silencedetect`（默认 −35 dB / 0.35 s），段落边界即字幕
  时间码；超过「最长段」的连续语音按等长硬切，而不是按字数估。
- **改一段只重跑这一段**。合成键 = 文本 + 引擎 + 音色 + 参数 + 读音表版本；
  键没变的段第二次直接复用，不请求引擎。

## 1. 修订对照

| v1 | v2 | 原因 |
|---|---|---|
| 九步顺序写死 | 操作靠端口种类连接，图是数据 | 增减一步要改代码与界面 |
| 步骤只是列表项 | 每步产出命名对象，可试听、可另存 | 「每一轮都有对象」 |
| 角色是一张表 | 角色是对象 `casting`，TTS/RVC 以它为输入 | 按角色分流要进图 |
| 听写页独立 | 并入同一工作区；纯音频不显示视频区 | 字幕编辑只应有一处 |

## 2. 对象与种类

图上的边不传「音频」，传**带种类的对象**。种类是契约的一部分，只有这九种：

| 种类 | 含义 | 形态 |
|---|---|---|
| `media` | 源容器，含流清单（音轨、字幕轨、分辨率、帧率） | 原文件 |
| `audio.mix` | 一条完整混音 | WAV |
| `audio.vocal` | 人声，单条或按段 | WAV |
| `audio.bed` | 原片的伴奏与背景（音乐、环境、音效） | WAV |
| `audio.music` | 配乐素材（生成或引入，非原片内容） | WAV |
| `cues` | 带时间码与角色的段落序列 | JSON |
| `casting` | 角色 → 处理方式与音色 | JSON |
| `track` | 贴齐后的编排：轨道、每段定位、贴合状态 | JSON + 段音频 |
| `video` | 成片 | MP4 |

三条规则：

1. **种类由核心定义，插件不得新造。** 允许插件自定义种类的代价是：A 插件的 `audio.voice` 接不上 B 插件的 `audio.vocal`，"自由组合"变成自由踩坑。加种类要改核心并迁移存量项目。
2. **种类同时决定端口、界面控件与落盘方式**，不只是一个字符串。
3. **中间件一律 WAV PCM16**：混音与配乐 44.1kHz 立体声，人声与识别副本 16kHz 单声道。不落 MP3 中间件——一个项目里会转码五六次，每次都损一点。

## 3. 操作契约

```python
@dataclass(frozen=True, slots=True)
class Port:
    name: str                 # 端口名，同一节点内唯一（vocal / bed / music）
    kinds: tuple[str, ...]    # 接受或产出的种类，按优先级排列
    required: bool = True
    many: bool = False        # 可接多路（逐段产物按 cue 归并，一段只允许一个来源）

@dataclass(frozen=True, slots=True)
class Operation:
    key: str; label: str; vendor: str; version: str
    inputs: tuple[Port, ...]
    outputs: tuple[Port, ...]
    unit: str = "whole"       # whole 整条 / item 按段、按行
    params: type[BaseModel] | None = None
    engine: str = ""          # 依赖的模型实例种类（tts / asr / timbre / stems / music）

    def run(self, ctx, src: dict[str, list[Artifact]], params, out: Path) -> dict[str, list[Artifact]]:
        """unit=item 时只处理一个单元：ctx.unit 给出 id 与内容。"""
```

**宿主负责一切非模型的事**：解码与重采样、切段与合并、缓存键、串行租约、进度、失败隔离、命名与落盘。插件只发一次请求、回一个文件。这条是「各步骤仅管输入输出」的落点——否则每个插件各写一套 ffmpeg 调用，半年后没人敢动。

内置操作：

| op | 输入 | 输出 | unit | 说明 |
|---|---|---|---|---|
| `extract` | `media` | `audio.mix` | whole | 解复用；多音轨时按流选 |
| `stems` | `audio.mix` | `audio.vocal`、`audio.bed` | whole | 分离模型 |
| `asr` | `audio.vocal` \| `audio.mix` | `cues` | item | 段级识别，优先吃人声轨 |
| `tts` | `cues`、`casting` | `audio.vocal` | item | 只处理标为 tts 的角色 |
| `rvc` | `audio.vocal`、`casting` | `audio.vocal` | item | 只处理标为 rvc 的角色，保时长 |
| `music` | — | `audio.music` | whole | 生成或引入 |
| `align` | `cues`、`audio.vocal`×N | `track` | whole | 补静音 / 加速贴齐 / 越界顺延；按 cue 归并多路 |
| `mix` | `track`、`audio.bed?`、`audio.music?` | `audio.mix` | whole | 侧链、响度、限峰 |
| `mux` | `audio.mix`、`media` | `video` | whole | 视频流复制，软字幕 |

`tts` 与 `rvc` 都产出人声，但**端口种类不同**：一个吃 `cues`，一个吃 `audio.vocal`。这正是"每种操作定义种类来确认对象"的用处——界面不会把文本喂给变声器。

## 4. 图与工作流

- **建图即类型匹配**：为某端口挑上游，列表按 `kinds` 过滤。没有候选时端口写「缺输入」，并列出能产出该种类的操作，而不是让用户自己猜。
- **校验**：无环；单值端口只有一个来源；至少一条路能到 `video` 或 `audio.mix`；**每个段落恰好一个出处**——按 `casting` 判定，标为 tts 的段由配音节点生产、标为 rvc 的段由变声节点生产，缺处理器时报「角色「陈默」标为变声，但没有启用的变声节点」。校验在保存时执行，失败给一句可读原因。
- **角色不建并行分支**：`casting` 是数据对象，`tts` / `rvc` 逐段读它决定这一段谁来处理。两节点可以同时存在（混用：主角换声、配角重配），各自只产出属于自己的段，`align` 按 cue 归并。界面上是角色表，不是三条线——图上真实并行分支会让调度、缓存、进度同时变复杂，而收益只是画得好看。
- **工作流 = 图 + 参数默认值 + 名称**，存 `workflows` 表，可跨项目复用。内置三个：`变声重制`、`重配`、`仅字幕`。
- **切换只在同形态内允许**：`变声重制` 与 `重配` 是同形态（有配音链），可在工作流列顶部的下拉里直接换，节点状态、端口与角色归属随之变；`仅字幕` 是没有配音节点、只导 SRT 的另一种形态，建项目时选择，不在已有项目上切换——否则磁盘上已有的配音产物与「未启用」的节点会自相矛盾。
- **模板带 `schema_version`**：插件升级改了参数名，载入时按版本迁移；迁移不了就明确报「模板与当前插件版本不兼容」，不静默套默认值。没有这一条，「可保存工作流」半年后全是废纸。

内置链路（同一套操作，开关不同）：

```
变声重制（默认）：extract → stems ─┬→ asr ─┬→ rvc（陈默 42 段）──┐
                               │       └→ tts（其余 135 段）─┼→ align → mix → mux
                               └→ bed ─────────────────────┘
重配：        extract → stems ─┬→ asr → tts（214 段）────────┐
                               └→ bed ───────────────────────┴→ align → mix → mux
仅字幕：      extract → asr → cues                      （导出 SRT，不走配音）
```

`mix` 还可再接 `music` 的产物；不接就是没有配乐。

## 5. 执行

```python
def run_node(proj, node):
    src = {p.name: [artifact(e) for e in edges_into(node, p)] for p in op.inputs}
    base = digest(op.key, VERSION, node.params, [a.fp for a in flat(src)])
    if (hit := cache.whole(base)):
        return hit                                  # 整条命中，零成本
    with lease(op.engine):                          # 每实例一把，默认 1
        if op.unit == "whole":
            return op.run(ctx, src, cfg, out) | cache.put(base)
        for unit in units(src):                     # 段或行
            k = digest(base, unit.id, unit.fp)
            got = cache.get(k) or op.run_one(ctx, unit, cfg, out)
            cache.put(k, got, commit_each=True)     # 逐单元落盘，可断点续跑
```

- **租约**：每个模型实例一把，默认并发 1。图上并行的两条分支不会同时压同一张卡。之前靠「步骤顺序」约束，现在图是自由的，必须由调度层保证。
- **缓存**：产物键 = 操作 key + 版本 + 参数 + 输入指纹；单元级再加单元 id 与内容指纹。改一段文本只重跑这一段，其余命中。
- **失效**：上游指纹变化时，下游产物保留但标 `stale`，界面灰显并给「重跑」。只有用户删产物才真删文件。
- **失败隔离**：单元失败不进整体失败；节点状态 `partial n/m`，可只重试失败单元。
- **取消**：当前单元收尾后停，已完成产物保留。

## 6. 数据与接口

```
<生产者数据根>/dubbing/<project_id>/
  source/<原名>                 # 原件，不覆盖
  objects/<node_id>/<port>.<ext># 每个节点的产物，按端口命名
  objects/<node_id>/<port>/<unit_id>.wav   # unit=item 的单元产物
  project.db                    # 表：projects / nodes / edges / artifacts / cues / casting
  out/<原名>.mp4  <原名>.srt
```

| 方法       | 路径                                    | 用途                             |
| -------- | ------------------------------------- | ------------------------------ |
| GET      | `/api/v1/producers/dubbing/workflows` | 模板列表                           |
| POST     | `/api/v1/producers/dubbing/workflows` | 当前图另存为模板                       |
| GET/POST | `/projects`                           | 列表 / 上传建项目（默认模板）               |
| GET      | `/projects/{id}/graph`                | 节点、边、产物、状态                     |
| POST     | `/projects/{id}/nodes`                | 按 op_key 加节点，自动接当前缺口           |
| PUT      | `/nodes/{nid}`                        | 参数、启停、替换实现                     |
| DELETE   | `/nodes/{nid}`                        | 删节点与其下游标记 stale                |
| POST     | `/nodes/{nid}/run` · `/cancel`        | 执行 / 停止                        |
| GET      | `/objects/{oid}`                      | 产物信息；`?stream=1` 取音频/视频（Range） |
| PUT      | `/projects/{id}/cues`                 | 段落批量编辑（文本、角色、时间码）              |
| PUT      | `/projects/{id}/casting`              | 角色表                            |
| GET      | `/projects/{id}/export?kind=`         | `video` / `audio` / `srt`      |

宿主侧另有 `usecase/workflow.py`（校验、拓扑、租约）与 `usecase/transforms.py`（变换域发现与调用）。

## 7. 界面

一页到底，三栏三区：

```
┌ 导航 ┬ 配音 · 项目 · [模板 变声重制 ▾] ──────────── 引擎 · 另存为工作流 · 导出 ┐
│      │ 源 + 角色 288 │ 预览（16:9，字幕叠加三档）    │ 工作流 300             │
│      │               ├──────────────────────────────┤ 每步：端口映射、状态、   │
│      │               │ 下区：[时间轴 | 字幕] 切换     │ 产物；底部导出          │
└──────┴───────────────┴──────────────────────────────┴────────────────────────┘
```

中列只有上下两块：上为预览，下为**一块共用区域**，用「时间轴 / 字幕」切换，默认时间轴。字幕不常显——常显会挤掉预览，而看片与改字本来不同时进行。

- **字幕视图是编辑的唯一入口**：下区切到「字幕」，行内改文本、点角色 chip 换人、拖时间码微调；失焦即提交，只重跑那一段。切回「时间轴」看排布与进度，两者不并列。这就是原先听写页的编辑器。
- **预览叠加三档**：`关 / 文本 / 文本 + 角色`。只影响预览；导出是否烧录是另一个开关。改字幕时开着「文本 + 角色」，按空格播放，边听边改。
- **工作流列**：列头是一个模板下拉（不是三个按钮）与节点数；每行显示名称、端口映射（`mix → vocal + bed`）、状态、产物入口。不可用的操作置灰并给一行原因（「未安装 timbre 实例」）。下拉换模板即换整张图的状态，不需要二次确认。
- **加一步**：只列出"输出种类能接上当前缺口"的操作——类型系统在界面上的唯一体现，也是它与节点画布的分界。
- **纯音频项目**隐藏视频区，段落表与导出不变：一次拖入即出 SRT。

## 8. 性能

段数决定渲染方式。12 分 30 秒约 214 段，30 分钟约 500 段，两小时约 2 000 段。

| 段数 | 每段一个 DOM 块 | 结论 |
|---|---|---|
| ≤ 300 | 约 1 200 节点 | 直接渲染 |
| 300–800 | 约 3 200 节点 | 缩放、拖拽开始掉帧 |
| 800+ | 1.2 万节点以上 | 必须换策略 |

两条硬规则：**概览走 canvas，DOM 只留可视窗口**（段宽 < 6px 时不建元素）；**播放头只改 `transform`**，播放时 rAF 读 `video.currentTime`，暂停即停。

七条口径：波形由服务端算 2 000 个峰值；段色带 canvas 每列取色；段块窗口化（左右各留一屏）；段落表固定行高虚拟滚动（常驻 ≤ 40 行，聚焦行才变输入控件）；`cues` 用 `shallowRef` + 版本号；拖拽 `pointerup` 才提交；进度只写日志，界面按批合并一次渲染。

## 9. 风险

| 风险 | 说明 | 处置 |
|---|---|---|
| 图过于自由 | 用户连出跑不通的图，或语义正确但结果荒谬 | 保存时校验；运行前预演（列出将执行的节点与预计时长） |
| 模板腐化 | 插件升级后旧模板失配 | `schema_version` + 迁移 + 明确报错 |
| 同卡争用 | 并行分支同时要 GPU | 实例租约；界面显示「排队中」 |
| 分离残留 | 人声轨残留伴奏，转换后成噪声 | 暴露质量参数；不达标提示换更强模型 |
| 逐段请求放大 | 2 000 段 = 2 000 次调用 | 单元级缓存 + 断点续跑 + 批量节点（P1） |
| 磁盘 | 原件 + 每节点产物 + 逐段音频 | 「清理中间产物」只留 `out/` 与 `cues`/`casting` |

## 10. 分期

| 期 | 内容 |
|---|---|
| P0 | 内置两模板可跑：extract / stems / asr / tts / align / mix / mux；casting；段落表；三段中区 |
| P1 | 图编辑（加删节点、重接端口）、工作流保存与载入、`rvc`、`music` |
| P2 | 节点画布（高级模式）、多卡租约、批量项目、说话人自动聚类 |

## 11. 验收

- 关掉 `stems`：`asr` 自动改吃 `audio.mix`，链路仍能跑到 `video`，界面不报缺输入。
- 把某角色从 tts 改成 rvc：只有该角色的段重跑，其余段命中缓存。
- 改一段文本：该段产物失效，其它段与 `stems` 不重跑。
- 载入 v1 时期保存的模板：或迁移成功，或明确报不兼容，不静默改参数。
- 2 000 段项目：拖动播放头与缩放时主线程 ≥ 50fps，段落表可视行 ≤ 40。

## 12. 我的意见

关于这份修订，有六点与"完全自由"不同：

1. **底层真图，界面不做节点画布。** 端口拖拽、连线合法性、自动布局、框选缩放，实现成本约为线性工作流列的 3–5 倍；而实际用法 90% 是两三条固定链。类型化的"槽"已经给出组合能力，画布留到 P2，且只在高级模式出现。
2. **种类必须封闭在核心。** 开放自定义种类看似灵活，实际会让两个插件无法对接，且迁移成本随项目增长。宁可加种类时改一次核心。
3. **角色走数据，不走分支。** 每个角色一条并行边，图会从 7 个节点膨胀到 20 个，调度与缓存都要重做。角色表 + 逐段读表，效果相同，代价小一个量级。
4. **合并听写页要有前提**：纯音频项目必须保留"一次拖入即出 SRT"的轻路径，否则日常最高频的小任务被重流程拖慢。导航项可以去掉，入口不能只剩重流程。
5. **模板不版本化就等于没有工作流。** 这一条不做，P1 的"保存工作流"会在第一次插件升级后全部失效。
6. **`tts` 与 `rvc` 不要合并成一个"声→声"操作。** 前者吃文本与角色，后者吃音频与参考音色；强行统一，界面就无法判断该给哪个端口喂什么，"每种操作定义种类"也就失去了意义。
