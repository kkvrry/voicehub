// @ts-check
/**
 * 没有封面时的头像底色。
 *
 * 同一套名字在任何一处（音色库卡片、左栏、详情头、处理页目标）都得到同一个底，
 * 不随刷新、排序、列表位置改变。两种写法并存：
 *
 * - 原色与纸墨是平涂色，与这两套外观的平面材质一致；
 * - 玻璃与素瓷用双色渐变，色相从名字散出，第二色相偏一点，像有光源。
 *
 * 由 `avatarFill` 按当前外观选择，调用方不各自判断。
 */

/**
 * 平涂底色（原色 / 纸墨）。
 * @param {string} name
 * @returns {string}
 */
export function avatarColor(name) {
  let hash = 0
  for (const ch of name || '?') hash = (hash * 31 + (ch.codePointAt(0) || 0)) % 360
  return `hsl(${hash}, 55%, 62%)`
}

/**
 * 按外观取头像底色：玻璃与素瓷用渐变，其余用平涂。
 * @param {string} name
 * @param {string} look
 * @returns {string}
 */
export function avatarFill(name, look) {
  return look === 'glass' || look === 'celadon'
    ? avatarGradient(name)
    : avatarColor(name)
}

/**
 * 双色渐变（玻璃 / 素瓷）。
 * @param {string} name
 * @returns {string}
 */
export function avatarGradient(name) {
  let hash = 2166136261
  for (const ch of name || '?') {
    hash ^= ch.codePointAt(0) || 0
    hash = Math.imul(hash, 16777619)
  }
  const value = Math.abs(hash)
  const hue = value % 360
  const second = (hue + 38 + (Math.floor(value / 97) % 26)) % 360
  return `linear-gradient(145deg, hsl(${hue} 68% 68%), hsl(${second} 62% 54%))`
}
