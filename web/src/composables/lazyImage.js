// @ts-check
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

/** 1×1 透明 GIF：未进入视口时占住 src，避免浏览器把整库封面一起拉下来。 */
const BLANK =
  'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'

/** 一个共享的观察器：每张卡各建一个会多出几千个观察器，没必要。 */
const states = new WeakMap()
/** @type {IntersectionObserver | null} */
let observer = null

function ensureObserver() {
  if (observer) return observer
  observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const state = states.get(entry.target)
        if (!state) continue
        state.visible = entry.isIntersecting
        state.apply()
      }
    },
    // 提前约两屏加载，滚动时不至于每张都从空白开始；离开视口后清回占位，
    // 解码后的位图不跟着几百张卡一直常驻。
    { rootMargin: '800px 0px' }
  )
  return observer
}

/**
 * 封面图只在接近视口时才真正设 src。
 *
 * `loading="lazy"` 在 `content-visibility` + 嵌套滚动容器里并不可靠：实测全新
 * 浏览器仍会把 1,165 张封面一起加载。这里用共享观察器兜底，并在离开视口后
 * 释放图片；返回的 `el` 绑到 `<img>`，`src` 绑到 `:src`。
 *
 * @param {import('vue').Ref<string>} srcRef 真实地址
 */
export function useLazyImage(srcRef) {
  const el = ref(null)
  const src = ref(BLANK)
  const state = {
    visible: false,
    apply() {
      src.value = state.visible ? srcRef.value || BLANK : BLANK
    },
  }

  onMounted(() => {
    const target = el.value
    if (!target) return
    states.set(target, state)
    ensureObserver().observe(target)
  })

  onBeforeUnmount(() => {
    const target = el.value
    if (!target) return
    observer?.unobserve(target)
    states.delete(target)
  })

  watch(srcRef, () => state.apply())

  return { el, src }
}
