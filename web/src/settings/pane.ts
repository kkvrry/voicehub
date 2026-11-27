/** 设置屏的状态：分组、搜索词、搜索行，以及"切分组 / 跳命中"这几个动作。
 *
 * 为什么放在组件外：设置屏由两半组成——分类栏在壳的左插槽、各组内容在壳的主区，
 * 两者是兄弟节点，靠 provide/inject 传递不到（插槽内容的组件树挂在 App 下）。
 * 所以这一份状态由模块持有，两半各自 import。
 *
 * 生命周期与"屏"一致，不与某个组件实例一致：离开设置屏再回来，分组与搜索词仍在原处——
 * 与音色筛选的留存同一口径（`store.filters` 也是这么留的）。
 */
import { computed, nextTick, reactive, ref, type Component } from 'vue'
import {
  Brain,
  HardDrive,
  Info,
  Keyboard,
  Mic,
  Palette,
  Quote,
  ServerCog,
  SlidersHorizontal,
  Tags,
} from 'lucide-vue-next'
import { useSettingsStore } from '../store/settings'
import { hitTest, type SearchContext, type SearchRow } from './search'
import { prettyCombo } from './utils'

export type SectionMeta = { id: string; label: string; icon: Component; group: string }

/** 分组：顺序即分类栏的顺序。 */
export const sections: SectionMeta[] = [
  { id: 'general', label: '通用设置', icon: SlidersHorizontal, group: '偏好' },
  { id: 'appearance', label: '外观主题', icon: Palette, group: '偏好' },
  { id: 'shortcuts', label: '快捷键', icon: Keyboard, group: '偏好' },
  { id: 'asr', label: '语音识别', icon: Mic, group: '能力' },
  { id: 'embedding', label: '语义检索', icon: Brain, group: '能力' },
  { id: 'services', label: '服务', icon: ServerCog, group: '能力' },
  { id: 'texts', label: '文案', icon: Quote, group: '资源' },
  { id: 'lexicon', label: '词表', icon: Tags, group: '资源' },
  { id: 'storage', label: '数据与存储', icon: HardDrive, group: '系统' },
  { id: 'about', label: '关于', icon: Info, group: '系统' },
]

//: 顶部搜索框这一屏绑的词。离开设置屏不清空：回来接着看那一组。
export const query = ref('')
//: 当前分组。
export const activeSection = ref('general')

//: 各组登记的搜索行（`SectionHeader` 之类的组件往里塞）。
export const rows = reactive<SearchRow[]>([])

/** 搜索上下文：各 section 通过 `useSearchRows()` 拿到它登记自己的行。 */
export const search: SearchContext = {
  query,
  rows,
  matches: (row) => hitTest(query.value, row.title, row.desc, row.keywords, row.context),
}

/** 有未保存改动的分组（读 `settings.dirtySections`）。 */
export function isDirty(section: string): boolean {
  const flags = useSettingsStore().dirtySections as unknown as Record<string, boolean>
  return Boolean(flags[section])
}

/** 顶部搜索框右侧提示：聚焦快捷键。 */
export const searchHint = computed(() =>
  prettyCombo(useSettingsStore().shortcuts.focusSearch).join(' ')
)

export const noResult = computed(
  () => Boolean(query.value.trim()) && !rows.some((row) => search.matches(row))
)

export function countMatches(section: string): number {
  return rows.filter((row) => row.section === section && !row.meta && search.matches(row)).length
}

/** 一次只显示一组；搜索时改为显示所有命中的分组（否则搜索只能搜到当前这一组）。 */
export function sectionVisible(section: string): boolean {
  const q = query.value.trim()
  if (!q) return activeSection.value === section
  if (rows.some((row) => row.section === section && search.matches(row))) return true
  const meta = sections.find((item) => item.id === section)
  return meta ? hitTest(q, meta.label) : false
}

/** 搜索命中可能落在别的分组：返回第一个命中所在的分组（分类栏据此高亮）。 */
export function firstMatchSection(): string {
  const first = query.value.trim() ? rows.find((row) => search.matches(row)) : null
  return first?.section ? String(first.section) : ''
}

export function clearQuery() {
  query.value = ''
}

/** 切换分组：主区只换内容，不滚整页；新的一组从顶部开始看。 */
export function goSection(section: string) {
  activeSection.value = section
  if (query.value) query.value = ''
  void nextTick(() => {
    const content = document.querySelector<HTMLElement>('.app-shell .sh-content')
    content?.scrollTo({ top: 0, behavior: 'smooth' })
  })
}

/** 分类栏的方向键：↑↓/Home/End 在分组间移动并直接切换。 */
export function onRailKeydown(event: KeyboardEvent) {
  const keys = ['ArrowDown', 'ArrowUp', 'Home', 'End']
  if (!keys.includes(event.key)) return
  const buttons = [
    ...(event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('.sh-rail-item'),
  ]
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
  if (index < 0) return
  event.preventDefault()
  let next = index
  if (event.key === 'ArrowDown') next = (index + 1) % buttons.length
  if (event.key === 'ArrowUp') next = (index - 1 + buttons.length) % buttons.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = buttons.length - 1
  buttons[next]?.focus()
  const target = sections[next]
  if (target) goSection(target.id)
}

/** 搜索框回车：跳到第一个命中的设置项，并闪一下。 */
export function jumpFirst() {
  const first = rows.find((row) => !row.meta && search.matches(row))
  if (!first) return
  activeSection.value = String(first.section)
  // 命中的那一组先渲染出来（`v-show` 之后才量得到位置），再滚过去。
  void nextTick(() => {
    const el = document.querySelector<HTMLElement>(`[data-row-id="${first.id}"]`)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.classList.remove('is-flash')
    void el.offsetWidth
    el.classList.add('is-flash')
    window.setTimeout(() => el.classList.remove('is-flash'), 1500)
  })
}
