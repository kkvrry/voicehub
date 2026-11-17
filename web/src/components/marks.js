// @ts-check
/** 正文里的标签（``[[笑]]`` / ``[[停顿:0.4]]``）：识别与切片。
 *
 * 这里只做纯文本切分，不认识任何具体的标签名——标签有哪些由引擎插件声明。
 * 正文框是单层渲染，高亮靠 `::highlight()`：所以除了给渲染用的片段，
 * 还给出区间坐标（`markRanges`），两处共用同一条正则。
 */
const MARK = /\[\[[^\[\]\n]*\]\]/g

/** @param {string} text */
export function hasMarks(text) {
  return /\[\[[^\[\]\n]*\]\]/.test(String(text || ''))
}

/** 切成 ``{text, mark}`` 片段：``mark: true`` 的那段是标签本身，其余是正文。 */
/**
 * @param {string} text
 * @returns {Array<{ text: string, mark: boolean }>}
 */
export function markSegments(text) {
  const source = String(text || '')
  /** @type {Array<{ text: string, mark: boolean }>} */
  const parts = []
  let last = 0
  for (const match of source.matchAll(MARK)) {
    if (match.index > last) parts.push({ text: source.slice(last, match.index), mark: false })
    parts.push({ text: match[0], mark: true })
    last = match.index + match[0].length
  }
  if (last < source.length) parts.push({ text: source.slice(last), mark: false })
  return parts
}

/** 标签在整段文本里的坐标：``[{start, end}]``。CSS Custom Highlight API 要的是区间，
 *  不是片段——它按区间在**同一层**文字下面画底色。 */
/**
 * @param {string} text
 * @returns {Array<{ start: number, end: number }>}
 */
export function markRanges(text) {
  /** @type {Array<{ start: number, end: number }>} */
  const spans = []
  for (const match of String(text || '').matchAll(MARK)) {
    spans.push({ start: match.index, end: match.index + match[0].length })
  }
  return spans
}
