// @ts-check
/** 弹层的键盘与焦点：Esc 关闭、Tab 在层内循环、关闭后把焦点还给触发元素。
 *
 * 只管键盘与焦点，不接管遮罩点击、滚动锁与视觉。面板本身由各弹层自己给
 * `tabindex="-1"`（或由这里补上），聚焦容器而不是第一颗按钮：读屏先报出对话框名，
 * Tab 再进内容。
 *
 * 嵌套弹层只响应最上面那一层——判据是文档里最后一个 `.vh-root`，与挂载顺序一致。
 */
import { nextTick, onBeforeUnmount, onMounted, watch } from 'vue'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/** @param {Element} el */
function visible(el) {
  return el.getClientRects().length > 0
}

/** @param {HTMLElement} panel @returns {HTMLElement[]} */
function focusables(panel) {
  return /** @type {HTMLElement[]} */ (Array.from(panel.querySelectorAll(FOCUSABLE))).filter(visible)
}

/** 本层是否在最上面。没有 `.vh-root` 包裹（就地弹层）时按最上层处理。 */
/** @param {HTMLElement} panel */
function topmost(panel) {
  const root = panel.closest('.vh-root')
  if (!root) return true
  const roots = document.querySelectorAll('.vh-root')
  return roots[roots.length - 1] === root
}

/** 选择器与右键菜单自己有 Esc：它们在上层时先让给它们。 */
function popupOnTop() {
  return [...document.querySelectorAll('.vpop, .ctx-menu')].some(visible)
}

/** 触发元素往上三层：它被重建时，用同一块区域找回焦点。 */
/** @param {HTMLElement} el */
function ancestorsOf(el) {
  /** @type {HTMLElement[]} */
  const chain = []
  let node = el.parentElement
  while (node && node !== document.body && chain.length < 3) {
    chain.push(node)
    node = node.parentElement
  }
  return chain
}

/**
 * @param {{
 *   open: () => boolean,
 *   panel: { value: HTMLElement | null },
 *   onClose: () => void,
 *   initialFocus?: () => HTMLElement | null | undefined,
 * }} opts
 */
export function useModalA11y(opts) {
  /** @type {HTMLElement | null} */
  let lastFocus = null
  /** @type {HTMLElement[]} */
  let lastAncestors = []

  /** @param {KeyboardEvent} event */
  function onKeydown(event) {
    if (!opts.open()) return
    const panel = opts.panel.value
    if (!panel || !topmost(panel)) return

    if (event.key === 'Escape') {
      if (popupOnTop()) return
      event.preventDefault()
      // 这一层吃掉 Esc：不再往下传，免得应用级 Esc（退出设置页一类）跟着响应。
      event.stopPropagation()
      opts.onClose()
      return
    }
    if (event.key !== 'Tab') return

    const items = focusables(panel)
    const active = /** @type {HTMLElement | null} */ (document.activeElement)
    if (!items.length) {
      event.preventDefault()
      panel.focus()
      return
    }
    const first = items[0]
    const last = items[items.length - 1]
    if (event.shiftKey && (active === first || !panel.contains(active))) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
      event.preventDefault()
      first.focus()
    }
  }

  watch(opts.open, async (open) => {
    if (open) {
      await enter()
      return
    }
    await leave()
  })

  // 懒挂载的用法（父级 `v-if` 才挂弹层）走不到 watch：挂上来时 open 已经是 true。
  onMounted(() => {
    if (opts.open()) enter()
  })

  /** 焦点收进面板；面板自己还没有 `tabindex` 时补一个，免得补不上而无法聚焦。 */
  async function enter() {
    lastFocus = /** @type {HTMLElement | null} */ (document.activeElement)
    lastAncestors = lastFocus ? ancestorsOf(lastFocus) : []
    window.addEventListener('keydown', onKeydown, true)
    await nextTick()
    const panel = opts.panel.value
    if (!panel) return
    if (!panel.hasAttribute('tabindex')) panel.setAttribute('tabindex', '-1')
    const target =
      opts.initialFocus?.() ||
      /** @type {HTMLElement | null} */ (panel.querySelector('[autofocus]')) ||
      panel
    target.focus({ preventScroll: true })
  }

  async function leave() {
    window.removeEventListener('keydown', onKeydown, true)
    const back = lastFocus
    // 关场动画期间面板还在文档里，焦点也还留在上面：先留住元素，再判断焦点是不是"卡在正在关的层里"。
    const panel = opts.panel.value
    lastFocus = null
    await nextTick()
    const active = /** @type {HTMLElement | null} */ (document.activeElement)
    const stale = !active || active === document.body || Boolean(panel?.contains(active))
    // 用户已经点到别处就不抢回来。
    if (!stale) return
    if (back?.isConnected) {
      back.focus({ preventScroll: true })
      return
    }
    // 触发元素在弹层开着时被重建（设置卡片按弹层状态重渲染）：退到它原来所在的那一块，
    // 取最近一个还在文档里、且有可聚焦子项的祖先。再找不到就不抢焦点。
    for (const node of lastAncestors) {
      if (!node.isConnected) continue
      const target = focusables(node)[0]
      if (target) {
        target.focus({ preventScroll: true })
        return
      }
    }
  }

  onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown, true))
}
