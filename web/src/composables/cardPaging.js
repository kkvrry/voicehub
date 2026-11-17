// @ts-check
/** 让一列音色卡「按整张卡」翻：可视区正好装下整数张，滚动停在卡与卡的交界上。
 *
 * 做两件事：
 *
 * 1. **定卡高**：把列表的可视高度均分成整数张（`n = floor(高 / 基准高)`），
 *    卡高取 `高 / n`。这样最后一张卡的下沿正好落在面板底边上——不切半张。
 *    取 `floor` 而不是 `round`：卡高只会比基准略高，不会矮到把两行字挤出去。
 * 2. **交给 CSS 吸附**：`.rail-list` 上写着 `scroll-snap-type: y proximity`，
 *    卡片 `scroll-snap-align: start`（见 style.css）。
 *    用 `proximity` 而不是 `mandatory`：列表是窗口化的，DOM 里只有视口附近
 *    那几张卡；`mandatory` 会把滚动范围锁在当前窗口里，滚不到下一段。
 *    吸附仍让落点停在卡边，只是不再阻止滚出已挂载区。
 *    **不要再加 `scroll-snap-stop: always`**：那条会把手势卡在一张上，
 *    一次只能翻一张，长列表翻起来极慢；对齐是吸附的事，不是"每次只许一张"的事。
 *
 * 为什么卡高要齐：列表里原先有「名字 / 来源+样例数 / 标签」三行的卡，
 * 也有两行的卡，高度从 52.5 到 73.5 不等——高度不齐时无论怎么吸附都会露出半张。
 * 标签因此挪进了名字行（见 SynthVoiceRail 的注释），卡面统一成两行。
 *
 * 窄屏横滑时列表变成 `flex-direction: row`：宽度是整页给的，这一套不适用，
 * 只保留 CSS 的横向吸附，卡高交还给内容。
 */
import { onBeforeUnmount, onMounted } from 'vue'

const DEFAULT_BASE = 54

/**
 * @param {import('vue').Ref<any>} listRef
 * @param {number} [base]
 */
export function useCardPaging(listRef, base = DEFAULT_BASE) {
  /** @type {ResizeObserver | null} */
  let observer = null
  let frame = 0

  function apply() {
    frame = 0
    const el = listRef.value
    if (!el) return
    // 横滑：卡高不该由这里决定（宽度与卡片数都随视口走），撤掉自己的变量。
    if (getComputedStyle(el).flexDirection === 'row') {
      el.style.removeProperty('--rail-card-h')
      return
    }
    const height = el.getBoundingClientRect().height
    if (!height) return
    // 高不够一张卡时给 1，别把卡压扁。
    const count = Math.max(1, Math.floor(height / base))
    // 只保留两位小数并向下取：15 张 × 54.06 略小于可视高，
    // 余下那点亚像素留在末尾，最后一张卡不会被切出一条 1px 的边。
    const cardHeight = Math.floor((height / count) * 100) / 100
    el.style.setProperty('--rail-card-h', `${cardHeight}px`)
  }

  function schedule() {
    if (frame) return
    frame = requestAnimationFrame(apply)
  }

  onMounted(() => {
    const el = listRef.value
    if (!el) return
    // 只观察列表自己：它的高度由外面的面板决定（flex 撑满），卡高变了不会反过来
    // 改变它的高度，所以不会自我循环。冻结卡出现、窗口改高、面板换高度都会触发。
    observer = new ResizeObserver(schedule)
    observer.observe(el)
    schedule()
  })

  onBeforeUnmount(() => {
    observer?.disconnect()
    observer = null
    if (frame) cancelAnimationFrame(frame)
  })

  return { refresh: schedule }
}
