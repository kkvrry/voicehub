// 展示层共用的格式化：容量与时长。设置页、引擎面板、播放条、录音按钮都取这一份，
// 同一份数据在任何一处显示都一致。

/** 字节数 → 人读的容量。0 与非数按 0 处理，负值不上屏。 */
export function formatBytes(size: number): string {
  const n = Number(size)
  if (!n || n < 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = n
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[index]}`
}

/** 秒 → `mm:ss`。超过一小时按总分钟数显示（`62:05`），不另起小时位。
 *  ``padded: false`` 时分钟不补零（``3:07``）——窄栏里省一格。 */
export function formatClock(seconds: number, options: { padded?: boolean } = {}): string {
  const total = Math.max(0, Math.floor(Number(seconds) || 0))
  const pad = (n: number) => String(n).padStart(2, '0')
  const minutes = Math.floor(total / 60)
  return `${options.padded === false ? String(minutes) : pad(minutes)}:${pad(total % 60)}`
}
