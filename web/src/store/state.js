// @ts-check
// 全局状态——单一数据源：左栏选中、筛选、列表、详情、播放器都只有这一份，
// 各域只从这里读写，不留副本。
import { reactive } from 'vue'

export const store = reactive({
  tree: {
    roots: /** @type {any[]} */ ([]),
    engines: /** @type {any[]} */ ([]),
    /** @type {number | undefined} */
    total: undefined,
    /** @type {number | undefined} */
    design: undefined,
    vocab: { genders: [], ages: [], languages: [], tones: [], timbres: [] },
  },
  voices: /** @type {any[]} */ ([]),
  allVoices: /** @type {any[]} */ ([]), // 生成页那个音色下拉用的（不带筛选）
  tagCloud: /** @type {any[]} */ ([]), // 当前筛选下剩哪些标签、各剩几条（范围刻意排除 tag，见 api.tags）
  detail: /** @type {any} */ (null), // 右侧抽屉里那个音色（带 samples）
  // 左栏选中：{ kind: 'all' } | { kind: 'local', root, category } | { kind: 'platform', name }
  //          | { kind: 'design' }（设计音色：跨引擎的一组，不是某台引擎的清单）
  /** @type {{ kind: string, root?: string, category?: string, name?: string }} */
  selection: { kind: 'all' },
  /**
   * 列表筛选：五个固定格 + 按当前选中附加的 source / root / category / design。
   * 不能收成窄形状——`currentQuery()` 会往上加条件。
   * @type {Record<string, any>}
   */
  filters: { q: '', gender: '', age: '', language: '', tag: '' },
  showHidden: true,
  loading: false,
  loadError: '', //: 上一次拉列表失败的理由（空 = 正常），列表空了要说得出为什么
  notice: '',
  //: 提示的档次（info / success / warn / error）与时间戳；时间戳给动画换 key。
  noticeKind: 'info',
  noticeAt: 0,
  // 匹配头像的进度（toast 里显示，可停止）。`stopping` 是按下停止后、
  // 当前这张还没收尾的那一小段。
  avatarJob: {
    running: false,
    done: 0,
    total: 0,
    failed: 0,
    name: '',
    stopping: false,
  },
  //: 头像来源清单（后端下发：具体站点，前端不写死）。取到一次就留着。
  avatarSources: /** @type {{ key: string, label: string, hint: string }[]} */ ([]),
  //: 「匹配头像」的来源选择弹层：`total` 是这次要补的音色数。
  avatarMatch: { open: false, total: 0, source: '' },
  avatarPick: /** @type {any} */ (null), // 正在挑头像的那条音色（卡片右键与详情共用）
  // 连接时补逐字稿那一轮的进度（toast 里显示，可停止）。后端在启动 / 保存设置时
  // 自己起线程，前端靠轮询跟上；`stopping` 是按下停止后、当前这条还没收尾的一段。
  asrJob: {
    running: false,
    done: 0,
    total: 0,
    failed: 0,
    stopping: false,
  },
  // 播放条状态，不属于任何一页：`<audio>` 实例与队列都在 playback.js，页面重画带不走它。
  // - `url` 空 = 没东西可播（播放条整条不出现）；
  // - `queue` / `index`：「全部试听」灌进来的队列；
  // - `position` / `duration` 是秒，由 `<audio>` 事件写入。
  player: {
    url: '', label: '', sub: '',
    queue: /** @type {any[]} */ ([]), index: 0,
    // 手动队列：放完当前这条不自动接下一段，上/下一段只由用户点。
    manual: false,
    playing: false, position: 0, duration: 0, error: '',
  },
  // 「设置」是右侧主区那一页（`store.view === 'settings'`）：落在哪一栏
  // 是页面自己的状态（SettingsPage 的 activeSection），不在这儿记一份——
  // 同一件事两个产地，必然只被改到一处。
  theme: 'light', // 解析后的实际主题 'light' | 'dark'（applyTheme 会同步到 <html data-theme>）
  themeMode: 'system', // 用户选择：'light' | 'dark' | 'system'（设置页「外观」那一栏绑这个）
  engines: /** @type {any[]} */ ([]), // 已启用的引擎自描述（含掩码后的当前配置、通道、页面分区）
  //: 适配清单：发现到的全部引擎（含未启用）。与 `engines` 是两个集合——
  //: 前者回答"这台怎么配"，后者回答"现在用哪几台"。
  engineCatalog: /** @type {any[]} */ ([]),
  //: 「引擎清单」那一层开着没有。它是引擎页的一部分，不单独占一个视图。
  catalogOpen: false,
  // 「引擎」这一页放的是插件：一条 TTS 引擎、一条对话模型、一条识别引擎，
  // 都是插件域里的一项（识别方向相反：音频 → 文字，同样可更换）。
  // `pluginKind` 决定右栏画哪一种面板，`engineKey` 记着看的是哪一个
  // （TTS 与识别共用它：两种面板不会同时出现，`pluginKind` 就是判据）。
  pluginKind: 'tts', // 'tts' | 'llm' | 'asr'
  engineKey: '',
  engineSection: 'settings',

  // 对话模型（LLM）：与 TTS 引擎并列的插件域（那边"文本→音频"，这边"消息→消息"）。
  // `providers` 是后端 `GET /llm/providers` 那一包，已掩码；`key` 是当前看的那家。
  // `models` 是平台清单（带能力与上下文），`testing` 只控制「测试连通」那颗按钮。
  llm: {
    providers: /** @type {Record<string, any>[]} */ ([]),
    key: '',
    models: /** @type {Record<string, any>[]} */ ([]),
    modelsReason: '',
    testing: false,
  },

  // 语音识别：另一类插件（方向相反："音频 → 文字"），自己一份状态。
  // `engines` 每项都带 describe + 掩码后的 values；`ready`（现在能不能用）与
  // `current`（选了哪台）不是一回事——配错地址时 current 有值而 ready 是 false。
  // `options` 是应用层选择（自动识别 / 写回 / 录音转写 / 语种写回），与引擎自身的配置分开。
  asr: {
    engines: /** @type {any[]} */ ([]),
    current: '',
    auto: true,
    ready: false,
    note: '',
    options: {
      auto_transcript: true,
      writeback: true,
      transcribe_recordings: true,
      language_writeback: true,
    },
  },

  // 「指定音色」请求：左侧音色卡点「指定」时把音色放进来，生产方取走后清空。
  // 宿主不认识"角色"这件事，只负责把用户点的那条音色递过去。
  /** @type {any} */
  assignVoice: null,
  //: 这次是「指定给已有目标」还是「用这条音色新建角色」：由音色栏的 `rail_mode` 决定，
  //: 插件的浮层读它分流。播放条那条始终是 assign。
  assignMode: 'assign',
  //: 生产方临时改写音色栏的动作（`''` = 跟随插件声明）。正在编辑某个角色时由插件写成
  //: `assign`，卡片右端那颗按钮跟着从「创建」变「指定」。
  railModeOverride: '',
  //: 「指定给…」里的目标：角色 / 某一章。由生产方读到项目后写入，宿主只管显示。
  assignTarget: '角色',
  // 「拾取」：声明 `rail_mode = pick` 的生产方工作区用它——点卡即选中音色并替换序列，
  // 卡右「添加」把整张卡追加进序列（`mode` 区分两种动作，`rev` 每次递增）。
  // 与 `synth` 分开：生成页那次用哪段声音，与这里处理谁，是两件事。
  pick: {
    voiceId: '',
    file: '',
    mode: '',
    rev: 0,
  },
  // 生产方插件：宿主只记「清单」与「当前打开哪一家」。
  // 项目、章节、行这些书籍状态在插件自己的 store 里（producers/<key>/store.js），
  // 宿主不认识它们的数据形状。
  producers: /** @type {Record<string, any>[]} */ ([]),
  //: 已停用的生产方 key，由清单里的 `enabled` 摊平而来。停用是后端状态：
  //: 服务不构造、接口拒绝；导航与设置分组只是跟着它收起。
  disabledProducers: /** @type {string[]} */ ([]),
  producer: {
    key: '',
  },
  //: 生成页下方「语音识别」块自己的状态；它不属于任何一次合成请求。
  listen: {
    file: '',
    running: false,
    text: '',
    model: '',
    language: '',
    languageCode: '',
    languageWritten: false,
    error: '',
  },

  // ---- 生成（docs/request-spec.md 那一套）----
  //: 主区在放什么：音色库列表 / 生成页 / 引擎（TTS 与对话插件）/ 日志页 / 设置页 /
  //: 某个生产方的工作区（`producer`，具体是哪一家看 `producer.key`）。
  view: 'library', // 'library' | 'audio' | 'synth' | 'engine' | 'logs' | 'settings' | 'producer'

  // 历史：录音、内录、生成物三类音频的同一张表。
  // 列表、计数与引擎分组都由后端算（`GET /api/v1/audio`），这里只存当前这一屏的结果；
  // 选择与播放留在本机。
  audio: {
    items: /** @type {any[]} */ ([]),
    counts: { all: 0, record: 0, loopback: 0, engine: 0 },
    engines: /** @type {{ label: string, count: number }[]} */ ([]),
    total: 0,
    unabsorbed: 0,
    hasMore: false,
    loading: false,
    error: '',
    q: '',
    source: 'all', // 'all' | 'record' | 'loopback' | 'engine'
    engine: '', // 生成物按引擎目录再分一层；空 = 不分
    range: 'all', // 'all' | 'today' | '7d' | '30d'
    absorbed: '', // '' | 'no' | 'yes'
    selected: /** @type {string[]} */ ([]),
  },

  spec: {
    groups: /** @type {any[]} */ ([]),
    fields: /** @type {any[]} */ ([]),
  }, // 规范：分组与每一项的中文名/单位/区间（后端给）
  //: 语气词标签分组（`GET /api/v1/marks`）：`common` 是支持标签的引擎的交集，
  //: `engines` 是各台多出来的那几条。与勾了哪台无关，不跟着 `engines` 走。
  markGroups: {
    common: /** @type {any[]} */ ([]),
    engines: /** @type {any[]} */ ([]),
    participants: /** @type {any[]} */ ([]),
  },
  synth: {
    text: '',
    voiceId: '',
    engines: /** @type {string[]} */ ([]), // 勾选的引擎 key（一个音色扇出给它们）
    values: /** @type {Record<string, any>} */ ({}), // 其余每一格：键 → 值（空串 = 没要求，不发）
    samples: /** @type {any[]} */ ([]), // 当前音色的音频：详情卡里那一串样例的名单
    // 麦克风录的那一段：{ name, path, text }。它不是任何音色的样例，只在临时目录里，
    // 所以不在 `samples` 名单里——但这一格要认它、要显示它，另记一份。
    recording: /** @type {any} */ (null),
    // 「置顶冻结」的那张卡：{ voiceId, file }。参考音频那一格撤掉之后，
    // 它是"这次用谁、哪一段"唯一的落点：卡片列顶上一张不随列表滚走的卡。
    pinned: /** @type {any} */ (null),
  },
  //: 每台引擎一条：{ key, label, state, detail, audio_url, cost_note, stale }。
  //: 只重跑勾上的那几台，没勾的留着上一次的结果（`stale` = 不是这一轮的）。
  synthJobs: /** @type {any[]} */ ([]),
  synthRunning: false,
  // 平台音色详情里的「生成样例」：一台引擎、一个音色、一句话，与生成页的扇出是两件事。
  audition: {
    text: '',
    values: /** @type {Record<string, any>} */ ({}),
    job: /** @type {any} */ (null),
    running: false,
  },
  // 文案库：分组的现成句子（设置页可改）。空数组是有效状态——用户可能删空了。
  // 显式给类型：只写 `[]` 会被推成 `never[]`，设置页遍历时就取不到字段了。
  texts: /** @type {{ name: string, lines: string[] }[]} */ ([]),
  // 用户词表（设置页「词表」）：情绪候选与引导指令预设。默认在仓库，
  // 用户那份只记差别（删除 hidden / 改名 renamed / 自定义 custom），生效值由后端算。
  // `instructions.effective` 就是生成页、试听、行参数弹框实际点选的那一份。
  lexicon: {
    emotions: {
      defaults: /** @type {string[]} */ ([]),
      hidden: /** @type {string[]} */ ([]),
      renamed: /** @type {Record<string, string>} */ ({}),
      custom: /** @type {string[]} */ ([]),
      effective: /** @type {string[]} */ ([]),
      //: 生效值的分组摆法：`[{ label, words }]`，与 `effective` 是同一份词。
      groups: /** @type {{ label: string, words: string[] }[]} */ ([]),
      //: 默认词的分组（不受删除与改名影响）：设置页摆默认行、删除后可恢复。
      default_groups: /** @type {{ label: string, words: string[] }[]} */ ([]),
    },
    instructions: {
      defaults: /** @type {{ label: string, text: string }[]} */ ([]),
      hidden: /** @type {string[]} */ ([]),
      renamed: /** @type {Record<string, string>} */ ({}),
      custom: /** @type {{ label: string, text: string }[]} */ ([]),
      effective: /** @type {{ label: string, text: string }[]} */ ([]),
    },
  },

  // 日志：只存后端统一通道最近那一段，是只读流水，谁都不改。
  // - `lines`：环形缓冲（1000 条，与后端同深，浅了补帧会只剩半段）；
  // - `cursor`：读到的最大 `seq`，重连时带上；
  // - `dropped`：环形转过一圈、中间缺了一段，缺了就说缺了；
  // - `connected`：SSE 通不通。没有 `open` 布尔：开着没有是 `store.view === 'logs'` 的事。
  logs: {
    lines: /** @type {any[]} */ ([]),
    cursor: 0,
    dropped: false,
    connected: false,
  },
  // 日志的增量统计：与 `logs.lines` 同进同出（log.js 的 claim 维护）。
  // 导航与左栏的角标、日志页的来源下拉都读这份——每条日志到达时对整份数组
  // 做一遍全量 filter，生成音频时后端每秒几十条，几个消费方叠起来正是
  // "什么都没干也觉得卡"的一种来源。进一条记一条、环丢一条减一条，O(1)。
  logStats: { total: 0, warning: 0, error: 0 },
  //: 出现过的来源（去重，首次出现序）。计数归零时移除，同样由 log.js 维护。
  logSources: /** @type {string[]} */ ([]),
})

//: 性别 / 年龄感 / 语言的候选词表——由后端给（`/api/v1/tree` 的 `vocab`），
//: 前端不写第二份：词表加一档时只改 domain，界面自动跟上。
export const vocab = {
  get genders() {
    return store.tree.vocab?.genders || []
  },
  get ages() {
    return store.tree.vocab?.ages || []
  },
  get languages() {
    return store.tree.vocab?.languages || []
  },
  /** 角色卡的两组固定标签：语调（怎么念）/ 音色（声音质地）。 */
  get tones() {
    return store.tree.vocab?.tones || []
  },
  get timbres() {
    return store.tree.vocab?.timbres || []
  },
}

/**
 * 值 → 显示名。词表里没有的值**原样显示**（旧数据/手改过的值不该变成空白）。
 * @param {Array<[string, string]>} pairs
 * @param {string} value
 * @returns {string}
 */
export function labelOf(pairs, value) {
  if (!value) return ''
  const found = pairs.find((pair) => pair[0] === value)
  return found ? found[1] : value
}
