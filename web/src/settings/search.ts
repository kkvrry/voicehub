// 设置页的搜索索引。
//
// 每一行、每一张卡片挂载时把自己登记进来，页面按 query 判断是否命中。用登记而不是
// 另写一份"设置项清单"：清单会和界面分叉，登记不会。
//
// 卡片/分区按"内部有没有命中的行"决定显隐，所以隐藏用 v-show（display:none）而不是
// v-if——v-if 会卸载行、把索引也一起删掉，搜索就再也搜不到它们了。
import { inject, type InjectionKey, type Ref } from 'vue'

export interface SearchRow {
  id: string
  section: string
  card: string
  title: string
  desc?: string
  keywords?: string
  /** 所在分区与卡片的标题，让「外观」「基础设置」这类词也能命中里面的项。 */
  context?: string
  /** 卡片自己登记的那条（只在判断显隐时用，不渲染）。 */
  meta?: boolean
}

export interface SearchContext {
  query: Ref<string>
  rows: SearchRow[]
  matches: (row: SearchRow) => boolean
}

export interface SectionMeta {
  id: string
  title: string
  desc: string
}

export const SEARCH_KEY: InjectionKey<SearchContext> = Symbol('settings-search')
export const CARD_KEY: InjectionKey<string> = Symbol('settings-card')
export const CARD_TITLE_KEY: InjectionKey<string> = Symbol('settings-card-title')
export const SECTION_KEY: InjectionKey<SectionMeta> = Symbol('settings-section')
/** 当前显示的是哪一组：只有它把「保存设置 / 恢复默认」摆在抬头右侧。 */
export const ACTIVE_KEY: InjectionKey<Ref<string>> = Symbol('settings-active')

export function useSearch(): SearchContext | null {
  return inject(SEARCH_KEY, null)
}

export function useCardId(): string {
  return inject(CARD_KEY, '')
}

export function useActiveSection(): Ref<string> | null {
  return inject(ACTIVE_KEY, null)
}

export function useSectionMeta(): SectionMeta {
  return inject(SECTION_KEY, { id: '', title: '', desc: '' })
}

/** 把 query 按空格拆词，全部命中才算命中（顺序无关，中英文都按包含判断）。 */
export function hitTest(query: string, ...fields: Array<string | undefined>): boolean {
  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (!tokens.length) return true
  const haystack = fields.filter(Boolean).join(' ').toLowerCase()
  return tokens.every((token) => haystack.includes(token))
}
