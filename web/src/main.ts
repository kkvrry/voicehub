import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { initFilters, initTheme, store } from './store'
import { useSettingsStore } from './store/settings'
import { createWheelStepper } from './utils/wheel-step'
import './style.css'
import './settings/theme.css'
import './settings/look-classic.css'
import './settings/look-paper.css'
import './settings/look-celadon.css'
import './settings/look-stage.css'

// 先恢复筛选/主题再挂应用：挂载那一刻就会按条件拉列表，也避免主题闪一下。
initTheme()
initFilters()

/** 电脑端：悬停原生下拉时滚轮直接换选项，省得先点开再选。
 *  滚轮事件交给最内层 select；`data-wheel="off"` 用于"选中即执行"的动作菜单。
 *  步进阈值与自绘下拉共用 `utils/wheel-step`——同一只手感，不各定一套。 */
function enableSelectWheel() {
  const step = createWheelStepper()
  document.addEventListener(
    'wheel',
    (event) => {
      const target = event.target
      if (!(target instanceof Element)) return
      const select = target.closest('select')
      if (
        !(select instanceof HTMLSelectElement) ||
        select.disabled ||
        select.multiple ||
        select.size > 1 ||
        select.dataset.wheel === 'off' ||
        select.options.length < 2 ||
        !event.deltaY
      ) {
        return
      }
      // 指针落在这一格上就不让页面跟着滚：滚的是选项，不是文档。
      event.preventDefault()
      const dir = step(event)
      if (!dir) return
      const start = select.selectedIndex
      let next = start
      for (let i = start + dir; i >= 0 && i < select.options.length; i += dir) {
        if (!select.options[i].disabled) {
          next = i
          break
        }
      }
      if (next === start) return
      select.selectedIndex = next
      select.dispatchEvent(new Event('change', { bubbles: true }))
    },
    { passive: false }
  )
}

enableSelectWheel()

// 设置页可以直接进来（刷新、书签、从文档里点链接）：`?view=settings` 或 `#settings`。
const params = new URLSearchParams(window.location.search)
if (params.get('view') === 'settings' || window.location.hash === '#settings') {
  store.view = 'settings'
}

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)

// 设置域在挂载前补水：外观类偏好（强调色、玻璃强度、密度）要在首帧就位。
useSettingsStore(pinia).hydrate()

app.mount('#app')
