// @ts-check
import { computed } from 'vue'
import { useSettingsStore } from '../stores/settings'

/** 某个位置是否显示图片头像；关闭时由组件回落到首字母。 */
/** @param {string} scope */
export function useAvatarImage(scope) {
  const settings = useSettingsStore()
  return computed(() => {
    const avatar = /** @type {any} */ (settings.appearance.avatar)
    return avatar.show && avatar[scope]
  })
}
