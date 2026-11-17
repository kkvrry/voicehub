// @ts-check
/**
 * 滚轮在「一格一格」的控件上翻项。原生 `<select>` 与自绘下拉共用这一处阈值。
 *
 * 鼠标滚轮一格才发一个事件（deltaY≈100），触控板一次滑动却会连发几十个，
 * 逐个处理会瞬间翻完整张表。分两种走法：
 *
 * - 单次位移已经够一格（鼠标）：立刻翻，不受冷却影响，连滚才跟手；
 * - 位移很小（触控板）：累到阈值再翻一格，翻完冷却一小段，
 *   免得惯性滑完还在继续翻；停手久了清零，残留的零头不会攒到下回。
 */
const THRESHOLD = 36
const COOLDOWN = 130
const RESET = 200

export function createWheelStepper() {
  let acc = 0
  let last = -Infinity
  /** @param {WheelEvent} event */
  return function step(event) {
    const raw =
      Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX
    if (!raw) return 0
    // deltaMode：0 = 像素、1 = 行（Firefox）、2 = 页。
    const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 100 : 1
    const delta = raw * scale
    const now = performance.now()
    // 上一段滑动已经结束：零头作废，别让它跟下一次点击/滚动凑成一格。
    if (now - last > RESET) acc = 0
    if (Math.abs(delta) >= THRESHOLD) {
      acc = 0
      last = now
      return delta > 0 ? 1 : -1
    }
    if (now - last < COOLDOWN) return 0
    acc += delta
    if (Math.abs(acc) < THRESHOLD) return 0
    const dir = acc > 0 ? 1 : -1
    acc = 0
    last = now
    return dir
  }
}
