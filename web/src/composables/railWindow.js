// @ts-check
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

/**
 * 音色栏的窗口化：只挂载滚动位置附近的那一段。
 *
 * 这一列每张卡是定高的（桌面由 `useCardPaging` 写 `--rail-card-h`），所以窗口
 * 可以直接按 scrollTop / 卡高算；上下用占位块补足未挂载的那一段，滚动条与
 * 吸附位置保持不变。窄屏横向排列时改用 scrollLeft / 卡宽，同一套逻辑。
 *
 * @param {import('vue').Ref<any[]>} source 全量列表
 * @param {import('vue').Ref<HTMLElement | null>} listRef 滚动容器
 * @param {{ overscan?: number }} [options] 视口外多挂几张，滚动时不至于露白
 */
export function useRailWindow(source, listRef, options = {}) {
  const overscan = options.overscan ?? 6
  const rendered = ref(/** @type {any[]} */ ([]))
  const padTop = ref(0)
  const padBottom = ref(0)
  const horizontal = ref(false)
  let frame = 0
  /** @type {ResizeObserver | null} */
  let observer = null

  function measure() {
    const el = listRef.value
    if (!el) return
    horizontal.value = getComputedStyle(el).flexDirection === 'row'
    const total = source.value.length
    if (!total) {
      rendered.value = []
      padTop.value = 0
      padBottom.value = 0
      return
    }
    const card = /** @type {HTMLElement | null} */ (el.querySelector('.rail-card'))
    const itemSize = card
      ? horizontal.value
        ? card.offsetWidth
        : card.offsetHeight
      : horizontal.value
        ? 187
        : 54
    if (!itemSize) return
    const viewSize = horizontal.value ? el.clientWidth : el.clientHeight
    const scroll = horizontal.value ? el.scrollLeft : el.scrollTop
    const start = Math.max(0, Math.floor(scroll / itemSize) - overscan)
    const end = Math.min(total, Math.ceil((scroll + viewSize) / itemSize) + overscan)
    rendered.value = source.value.slice(start, end)
    padTop.value = start * itemSize
    padBottom.value = Math.max(0, (total - end) * itemSize)
    // 第一次还没有卡片可量尺寸，估计值与真实卡高差一点：挂上后用真实高度再量一次。
    if (!card) nextTick(schedule)
  }

  function schedule() {
    if (frame) return
    frame = requestAnimationFrame(() => {
      frame = 0
      measure()
    })
  }

  onMounted(() => {
    const el = listRef.value
    if (!el) return
    el.addEventListener('scroll', schedule, { passive: true })
    observer = new ResizeObserver(schedule)
    observer.observe(el)
    schedule()
  })

  onBeforeUnmount(() => {
    const el = listRef.value
    if (el) el.removeEventListener('scroll', schedule)
    observer?.disconnect()
    observer = null
    if (frame) cancelAnimationFrame(frame)
  })

  watch(
    source,
    () => {
      const el = listRef.value
      // 换筛选 / 换目录后旧滚动位置可能已经越界；回到顶部，别停在空白段上。
      if (el) {
        el.scrollTop = 0
        el.scrollLeft = 0
      }
      schedule()
    },
    { immediate: true }
  )

  return { rendered, padTop, padBottom, horizontal, refresh: schedule }
}
