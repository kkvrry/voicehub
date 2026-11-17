// 快捷键：把 KeyboardEvent 归一成 `Ctrl+Shift+L` 这样的字符串，以及反过来比对。
//
// 归一化的意义：浏览器给的 `e.key` 大小写、同名修饰键（Meta/Win/⌘）都不统一，
// 直接存 `e.key` 会在换浏览器后全部失配。这里只保留一套写法，读与写都走它。
import { reactive } from 'vue'

const MODIFIERS = new Set(['Control', 'Shift', 'Alt', 'Meta', 'AltGraph', 'CapsLock', 'Dead'])

/** 正在录制快捷键的动作（空串 = 没在录）。全局键盘监听要先让路。 */
export const recorder = reactive({ action: '' })

export function normalizeKey(key: string): string {
  if (key === ' ') return 'Space'
  if (key === 'Esc') return 'Escape'
  if (key.length === 1) return key.toUpperCase()
  return key
}

export function isModifierOnly(key: string): boolean {
  return MODIFIERS.has(key)
}

/** 事件 → 归一化组合键。只按修饰键时返回 null（还在等真正的键）。 */
export function comboFromEvent(event: KeyboardEvent): string | null {
  const key = normalizeKey(event.key)
  if (!key || isModifierOnly(key)) return null
  const parts: string[] = []
  if (event.ctrlKey) parts.push('Ctrl')
  if (event.altKey) parts.push('Alt')
  if (event.shiftKey) parts.push('Shift')
  if (event.metaKey) parts.push('Meta')
  parts.push(key)
  return parts.join('+')
}

/** 单个无修饰键只允许功能键与符号键：否则打字的字母会把页面功能抢走。 */
export function isSafeBareKey(combo: string): boolean {
  const parts = combo.split('+')
  if (parts.length > 1) return true
  const key = parts[0]
  if (/^F\d{1,2}$/.test(key)) return true
  if (['Escape', 'Space', 'Tab', '/', '?', '\\', '`', '-', '=', '[', ']'].includes(key)) return true
  return key.startsWith('Arrow') || key === 'PageUp' || key === 'PageDown' || key === 'Home' || key === 'End'
}

/** 一组绑定里有没有重复项（返回重复的组合键，空数组 = 没有冲突）。 */
export function findDuplicates(bindings: Record<string, string>): string[] {
  const seen = new Map<string, number>()
  for (const combo of Object.values(bindings)) {
    if (!combo) continue
    seen.set(combo, (seen.get(combo) || 0) + 1)
  }
  return [...seen.entries()].filter(([, count]) => count > 1).map(([combo]) => combo)
}
