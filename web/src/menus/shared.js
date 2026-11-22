// @ts-check
// 右键菜单的公共动作：复制一段文字。生产方菜单下沉到各自目录后仍共用这一份。
import { Copy as CopyIcon } from 'lucide-vue-next'
import { say } from '../store'

/** @param {string} text @param {string} label @param {string} [key] */
export function copyItem(text, label, key = 'copy') {
  const value = String(text || '').trim()
  if (!value) return null
  return {
    key,
    label,
    icon: CopyIcon,
    run: () => copyText(value),
  }
}

/** @param {string} text */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    say('已复制', { kind: 'success' })
    return
  } catch {
    //  clipboard 只在安全上下文可用；本地服务正常走上一支。
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  try {
    document.execCommand('copy')
    say('已复制', { kind: 'success' })
  } catch {
    say('复制失败')
  } finally {
    area.remove()
  }
}
