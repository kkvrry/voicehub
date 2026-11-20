// @ts-check
// 右键菜单的唯一状态与分发入口。对象组件只上报「是什么」与「能做什么」，
// 文案、图标与顺序在 menus/catalog.js 里按对象类型排。
import { reactive } from 'vue'
import { buildMenu } from '../menus/catalog'

/**
 * @typedef {Object} MenuItem
 * @property {string} key
 * @property {string} label
 * @property {any} [icon]
 * @property {boolean} [danger]
 * @property {boolean} [disabled]
 * @property {string} [reason]
 * @property {MenuItem[]} [children]
 * @property {() => void | Promise<void>} [run]
 */

export const menuState = reactive({
  open: false,
  session: 0,
  groups: /** @type {MenuItem[][]} */ ([]),
  x: 0,
  y: 0,
  /** 锚点模式：菜单贴在按钮右下；为空时按光标位置展开。 */
  anchor: /** @type {HTMLElement | null} */ (null),
  /** 触发元素：外点判断与 Esc 后焦点归还都认它。 */
  trigger: /** @type {HTMLElement | null} */ (null),
})

/** 输入框、下拉、可编辑区与非折叠选区交还给浏览器：这些地方的原生菜单不能丢。
 *  标了 `data-app-menu` 的输入框（创作页行文本）在没有选区时让给项目菜单——
 *  那一格的主操作是行菜单，想要复制仍然先选中文字。
 * @param {MouseEvent} event */
export function canTakeOver(event) {
  if (!(event instanceof MouseEvent)) return false
  if (event.shiftKey) return false
  const target = event.target
  if (!(target instanceof Element)) return false
  const field = target.closest('input, textarea')
  if (field) {
    if (!field.hasAttribute('data-app-menu')) return false
    if (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement) {
      const start = field.selectionStart ?? 0
      const end = field.selectionEnd ?? 0
      if (end > start) return false
    }
  }
  if (
    target.closest(
      'select, [contenteditable="true"], [contenteditable=""], [data-native-menu]'
    )
  ) {
    return false
  }
  const selection = window.getSelection()
  if (
    selection &&
    !selection.isCollapsed &&
    selection.anchorNode &&
    target.contains(selection.anchorNode)
  ) {
    return false
  }
  return true
}

/**
 * @param {string} type
 * @param {any} context
 * @param {{ event?: MouseEvent, anchor?: HTMLElement | null }} [options]
 */
export function openObjectMenu(type, context, options = {}) {
  const event = options.event || null
  const anchor = options.anchor || null
  if (event && !canTakeOver(event)) return false
  const groups = buildMenu(type, context)
    .map((group) => group.filter((item) => item))
    .filter((group) => group.length)
  if (!groups.length) return false
  if (event) {
    event.preventDefault()
    event.stopPropagation()
    menuState.x = event.clientX
    menuState.y = event.clientY
  }
  menuState.anchor = anchor
  menuState.trigger =
    event && event.currentTarget instanceof HTMLElement ? event.currentTarget : anchor
  menuState.groups = /** @type {MenuItem[][]} */ (groups)
  menuState.session += 1
  menuState.open = true
  return true
}

/**
 * @param {{ restoreFocus?: boolean }} [options]
 */
export function closeContextMenu(options = {}) {
  if (!menuState.open) return
  const trigger = menuState.trigger
  menuState.open = false
  menuState.groups = []
  menuState.anchor = null
  menuState.trigger = null
  if (options.restoreFocus && trigger?.isConnected) {
    trigger.focus({ preventScroll: true })
  }
}