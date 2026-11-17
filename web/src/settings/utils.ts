// 设置页内部的小工具：数值夹取、可读的按键名。
// 容量格式化在 `utils/format.ts`——引擎面板与设置页都要用，不是设置页独占。

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

const KEY_LABELS: Record<string, string> = {
  ArrowUp: '↑',
  ArrowDown: '↓',
  ArrowLeft: '←',
  ArrowRight: '→',
  Escape: 'Esc',
  Backspace: '⌫',
  Delete: 'Del',
  Enter: '↵',
  Space: 'Space',
  Tab: 'Tab',
  PageUp: 'PgUp',
  PageDown: 'PgDn',
  Home: 'Home',
  End: 'End',
  Meta: 'Meta',
}

export function prettyKey(key: string): string {
  return KEY_LABELS[key] || key
}

export function prettyCombo(combo: string): string[] {
  return combo.split('+').map(prettyKey)
}
