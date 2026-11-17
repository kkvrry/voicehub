// @ts-check
// 应用内确认 / 单值输入：替换 window.confirm 与 window.prompt。
// 原生弹窗带不出"后果与可否还原"，样式也不归应用管；这里只有一句标题、一句后果，
// 需要取值时多一格输入框（重命名、停顿秒数都走这一条）。
import { reactive } from 'vue'

export const confirmState = reactive({
  open: false,
  title: '',
  message: '',
  confirmText: '确定',
  danger: true,
  //: `{ type: 'text' | 'number', placeholder }`；为 null 时只确认，不取值。
  input: /** @type {null | { type: string, placeholder?: string }} */ (null),
  value: '',
  //: 多个执行动作（「合并当前章」「合并全书」这类）：给了就不显示单一的确认键，点哪个执行哪个。
  actions:
    /** @type {null | Array<{ value: string, label: string, disabled?: boolean, danger?: boolean }>} */ (
      null
    ),
})

/** @type {((value: any) => void) | null} */
let settle = null

/**
 * 打开确认框。
 * 返回 `Promise<true | string | { value, action } | null>`：只确认时 `true` / `null`；
 * 带输入时是去掉首尾空白的值；带 `actions` 时是 `{ value, action }`。
 *
 * @param {{
 * title?: string, message?: string, confirmText?: string, danger?: boolean,
 * input?: null | { type: string, placeholder?: string }, value?: string | number,
 * actions?: Array<{ value: string, label: string, disabled?: boolean, danger?: boolean }> | null,
 * }} [options]
 */
export function confirmAction(options = {}) {
  settle?.(null)
  return new Promise((resolve) => {
    settle = resolve
    Object.assign(confirmState, {
      title: options.title || '确认操作',
      message: options.message || '',
      confirmText: options.confirmText || '确定',
      danger: options.danger !== false,
      input: options.input || null,
      value: options.value == null ? '' : String(options.value),
      actions: options.actions?.length ? options.actions : null,
      open: true,
    })
  })
}

/**
 * ``accepted``：`false` 取消、`true` 确认、字符串则是点了哪个动作。
 *
 * @param {any} accepted
 */
export function resolveConfirm(accepted) {
  confirmState.open = false
  const done = settle
  settle = null
  if (!done) return
  if (accepted === false || accepted == null) return done(null)
  const value = confirmState.value.trim()
  if (confirmState.actions) {
    // 回车走第一个能用的动作：动作按钮各自置灰时，回车不该绕过它。
    const action =
      typeof accepted === 'string'
        ? accepted
        : (confirmState.actions.find((item) => !item.disabled) || confirmState.actions[0]).value
    return done({ value: value || null, action })
  }
  if (!confirmState.input) return done(true)
  done(value || null)
}
