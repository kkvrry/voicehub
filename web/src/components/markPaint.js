// @ts-check
/** 给正文里的 `[[…]]` 上底色：CSS Custom Highlight API（`::highlight()`）。
 *
 * 和旧做法（透明镜像叠在 textarea 上）的区别：高亮画在**同一层**文字底下，
 * 位置由浏览器的排版给出，不存在两套排版互相对齐的问题；它也不改 DOM，
 * 光标、选区与输入法组合都不受影响。
 *
 * 一个文档只有一套 highlight 注册表（名字写死在 CSS 里），所以多个正文框
 * 同时在场时各自把区间交到这里合并，卸载时只撤自己那份。
 */
const REGISTRY = 'te-mark'
/** @type {Map<PropertyKey, Range[]>} */
const owners = new Map()

function supported() {
  return typeof CSS !== 'undefined' && 'highlights' in CSS && typeof Highlight === 'function'
}

function commit() {
  if (!supported()) return
  const all = [...owners.values()].flat()
  if (all.length) CSS.highlights.set(REGISTRY, new Highlight(...all))
  else CSS.highlights.delete(REGISTRY)
}

/** 偏移 → Range。正文始终是纯文本（无块元素），文本节点首尾相接，
 *  依次累加长度即可定位。 */
/**
 * @param {HTMLElement} el
 * @param {number} start
 * @param {number} end
 * @returns {Range | null}
 */
function rangeOf(el, start, end) {
  /** @type {Array<{ node: Node, start: number, end: number }>} */
  const nodes = []
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  let total = 0
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const length = (node.nodeValue || '').length
    if (!length) continue
    nodes.push({ node, start: total, end: total + length })
    total += length
  }
  const first = nodes.find((item) => start < item.end)
  const last = nodes.find((item) => end <= item.end)
  if (!first || !last) return null
  const range = document.createRange()
  range.setStart(first.node, Math.max(0, start - first.start))
  range.setEnd(last.node, Math.max(0, end - last.start))
  return range
}

/** `spans` 是 `[{start, end}]`（见 marks.markRanges），空数组等于清掉这个实例的高亮。 */
/**
 * @param {PropertyKey} owner
 * @param {HTMLElement} el
 * @param {Array<{ start: number, end: number }>} spans
 */
export function paintMarks(owner, el, spans) {
  if (!el) return
  owners.set(
    owner,
    spans
      .map(({ start, end }) => rangeOf(el, start, end))
      .filter(/** @returns {range is Range} */ (range) => Boolean(range))
  )
  commit()
}

/** @param {PropertyKey} owner */
export function clearMarks(owner) {
  owners.delete(owner)
  commit()
}
