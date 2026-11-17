// @ts-check
/** 全项目一套标签配色：值 → 色调。颜色本身只在 CSS 的 `.tone-*`（明暗两套）里定义，
 *  这里只决定"归到哪一类"。卡片、详情、样例、筛选都读这一处——同一个词在哪儿
 *  都是同一个颜色；一个词两处两个长相，比没有颜色更让人以为是两回事。
 *
 *  三层判断：
 *
 *  1. **语义词表**（`EXACT` + `KEYWORDS`）：性别 / 语言 / 年龄感 / 情绪共用一张表，
 *     一个词落一个色调，**与它属于哪一类标签无关**。从前是四张互不相干的表
 *     （性别一张、语言一张、情绪一张），年龄感干脆写死成灰——用户自己打的标签
 *     大半没有颜色，就是这个后果。
 *  2. **词表外的词按词本身散列**到同一套调色板：自己打的标签也有颜色，而不是一律灰；
 *     同一个词每次都是同一个色，换台机器也一样（不掺随机数）。
 *  3. 空值 / 未标 → 中性灰：**灰色只表示"没有值"**，不再表示"我们不认识这个词"。
 */

/** 散列兜底用的调色板：不含 gray（灰留给"没值"）。顺序与 CSS 里那 9 个 `.tone-*` 一致。 */
const PALETTE = ['blue', 'indigo', 'purple', 'pink', 'coral', 'amber', 'teal', 'green']

/** 代码类取值 → 色调。这些是**定值**（`zh` / `male` / `teen`…），精确命中，不做包含匹配：
 *  否则 `female` 会先撞上 `male`、`en` 会撞上任何一个含 "en" 的词。 */
const EXACT = {
  // 性别
  male: 'blue',
  female: 'pink',
  other: 'purple',
  // 年龄感：按"轻重"排一条淡淡的梯度，不是各给一色
  child: 'teal',
  teen: 'green',
  young: 'amber',
  middle: 'indigo',
  senior: 'purple',
  // 语言：按语系归类（17 种语言归 6 个色系，同语系一眼看成一类）
  zh: 'amber',
  yue: 'amber',
  ja: 'coral',
  ko: 'green',
  en: 'indigo',
  es: 'indigo',
  fr: 'indigo',
  de: 'indigo',
  pt: 'indigo',
  it: 'indigo',
  nl: 'indigo',
  ru: 'purple',
  uk: 'purple',
  ar: 'teal',
  tr: 'teal',
  id: 'teal',
  vi: 'teal',
}

/** 中文说法 → 色调。匹配是"包含"，**顺序有意义**：先否定/具体，后宽泛
 *  （`不开心` 里含 `开心`，顺序反了就被染成暖色）。
 *  同一套色板同时管性别、年龄、语言与情绪——一个词只认它自己的意思，不管它被
 *  填进了哪一格。
 *  @type {Array<[string, string[]]>} */
const KEYWORDS = [
  // 年龄感：先于情绪，因为「少女 / 少年」这类词本身没有情绪色彩
  ['teal', ['儿童', '小孩', '幼']],
  ['green', ['少年', '少女']],
  ['amber', ['青年', '年轻', '小伙', '姑娘']],
  ['indigo', ['中年', '大叔', '阿姨']],
  ['purple', ['老年', '年长', '老人', '苍老']],
  // 语言（存的是代码，但标签里常写成中文说法）
  ['amber', ['中文', '汉语', '普通话', '粤语', '广东话']],
  ['coral', ['日语', '日文']],
  ['green', ['韩语', '朝鲜语']],
  [
    'indigo',
    [
      '英语',
      '英文',
      '西班牙语',
      '法语',
      '德语',
      '葡萄牙语',
      '意大利语',
      '荷兰语',
    ],
  ],
  ['purple', ['俄语', '乌克兰语']],
  ['teal', ['阿拉伯语', '土耳其语', '印尼语', '越南语', '马来语', '泰语']],
  // 语调 / 音色（角色卡那两组固定标签）：与情绪共用一张表，同一个词同一个色。
  // 冷、硬、糙的归 coral / indigo，暖与厚的归 amber / teal——同一类的词挨着落一个色，
  // 一屏扫下来能看出"这批音色大致是哪种味道"，而不是一堆随机的点。
  [
    'indigo',
    [
      '高冷', '深沉', '干练', '沙哑', '严谨', '干脆', '拘谨', '木讷', '平淡', '絮叨', '冷峻',
      '威严', '清冷', '烟嗓', '金属感',
    ],
  ],
  ['purple', ['慵懒', '空灵']],
  [
    'coral',
    ['凌厉', '倨傲', '刻薄', '急躁', '尖细', '单薄', '发闷', '粗糙', '含混', '发紧', '干涩'],
  ],
  ['green', ['稚嫩']],
  ['amber', ['醇厚', '浑厚', '戏谑', '轻浮', '高亢', '洪亮']],
  [
    'teal',
    ['清亮', '醇雅', '明亮', '温润', '轻柔', '气声', '圆润', '清脆', '清沙', '软糯', '鼻音'],
  ],
  // 情绪与描述：蓝 = 负面低落，珊瑚 = 愤怒，紫 = 惊惧紧张，靛 = 克制/厌烦，
  // 琥珀 = 愉快，青 = 温柔平静，粉 = 动情
  ['blue', [
    '不开心', '悲伤', '难过', '忧郁', '失落', '委屈', '脆弱', '哭', '哀', '疲惫', '疲倦',
    '怅然', '愧疚',
  ]],
  ['coral', ['愤怒', '生气', '发怒', '凶狠', '咬牙', '厉声', '怒吼', '怒', '厌恶', '暴躁', '嫉妒']],
  ['purple', ['惊讶', '吃惊', '震惊', '紧张', '恐惧', '害怕', '颤抖', '慌乱', '忐忑']],
  ['indigo', ['严肃', '冷静', '沉稳', '庄重', '命令', '警告', '坚定', '御姐', '冷漠', '无奈', '厌倦']],
  ['amber', ['开心', '高兴', '喜悦', '兴奋', '愉快', '欢乐', '俏皮', '活泼', '笑', '甜美', '元气', '欣慰']],
  ['teal', ['温柔', '平静', '柔和', '舒缓', '中性', '淡然', '内敛', '低沉', '平铺直叙', '磁性', '释然']],
  ['pink', ['动情']],
]

/** `'不开心'` 要能先撞上"不开心"而不是"开心"：词表里长的先试。
 *  同一色调内部按字数从长到短排一次，跨色调的先后由上面那张表的顺序说了算。 */
/** @type {Array<[string, string[]]>} */
const MATCHERS = KEYWORDS.map(([tone, keys]) => [
  tone,
  [...keys].sort((a, b) => b.length - a.length),
])

/** 词本身 → 一个稳定的色调：FNV-1a，纯函数，不掺随机数（换台机器也是这个色）。 */
/** @param {string} value */
function hashTone(value) {
  let hash = 2166136261
  for (const ch of value) {
    hash ^= ch.codePointAt(0) ?? 0
    hash = Math.imul(hash, 16777619)
  }
  return PALETTE[Math.abs(hash) % PALETTE.length]
}

/** 值 → 色调。三类标签（性别 / 年龄 / 语言 / 情绪 / 自由标签）都走这一个函数。 */
/** @param {any} value */
export function toneOf(value) {
  const raw = String(value ?? '').trim()
  if (!raw) return 'gray'
  const lowered = raw.toLowerCase()
  // `hasOwn` 而不是直接取：标签是自由文本，写个 `constructor` 进来不该取到原型上的东西。
  if (Object.hasOwn(EXACT, lowered)) return /** @type {any} */ (EXACT)[lowered]
  for (const [tone, keys] of MATCHERS) {
    if (keys.some((key) => lowered.includes(key))) return tone
  }
  return hashTone(raw)
}

/** 性别：男蓝 / 女粉 / 其他紫（词表外的手改值也照样有颜色）。 */
/** @param {any} value */
export function genderTone(value) {
  return toneOf(value)
}

/** 语言：按语系归类，与 `EXACT` 里那张表同一份。 */
/** @param {any} value */
export function languageTone(value) {
  return toneOf(value)
}

/** 年龄感：按"轻重"排，不再是一律灰——灰只留给"没标"。 */
/** @param {any} value */
export function ageTone(value) {
  return toneOf(value)
}

/** 情绪：自由文本，按关键词归类；词表外按词散列（**不是灰**）。 */
/** @param {any} value */
export function emotionTone(value) {
  return toneOf(value)
}

/** 能力键 → 色调：引擎「能力与参数」页那一列标签。
 *
 *  键是协议里的能力名（后端下发），不是某台引擎专有的东西；没登记的新能力按词本身
 *  散列，不会掉成灰色。语言面三格各自一色：语言琥珀、跨语言绿、方言珊瑚——它们是
 *  选引擎时最常被问的三件事，混在灰里就白标了。 */
const CAPABILITY_TONES = {
  voice_listing: 'indigo',
  voice_cloning: 'pink',
  voice_design: 'purple',
  instruction_control: 'blue',
  emotion_control: 'amber',
  cross_lingual: 'green',
  dialects: 'coral',
}

/** 能力键 → 色调（未知键按词散列，与自由标签同一条兜底）。 */
/** @param {string} key */
export function capabilityTone(key) {
  const raw = String(key ?? '').trim()
  if (!raw) return 'gray'
  return /** @type {Record<string, string>} */ (CAPABILITY_TONES)[raw] || hashTone(raw)
}

