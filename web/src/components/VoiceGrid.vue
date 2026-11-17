<template>
  <div ref="wrapEl" class="grid-wrap">
    <!-- 只有"一条都还没有"才让位给加载态。已有列表时重查就地在原列表上换数据：
         以前这里按 store.loading 整块换成「加载中……」，等于每改一次筛选、每点一下
         隐藏都把几百张卡卸掉重建一遍。 -->
    <div v-if="firstLoad" class="empty">
      <span class="empty-icon is-busy"><Loader2 :size="20" :stroke-width="1.8" /></span>
      正在读取音色库……
    </div>
    <div v-else-if="store.loadError" class="empty" data-tone="warn">
      <span class="empty-icon is-warn"><TriangleAlert :size="20" :stroke-width="1.8" /></span>
      列表没拉出来：{{ store.loadError }}
      <span class="empty-note">服务还在的话，点左下角那颗刷新按钮重扫一次。</span>
    </div>
    <div v-else-if="!visible.length" class="empty">
      <span class="empty-icon"><component :is="emptyIcon" :size="20" :stroke-width="1.8" /></span>
      <template v-if="store.selection.kind === 'platform'">
        <template v-if="selectedEntry && selectedEntry.can_list">
          该引擎暂无音色入库。<span class="empty-note">在其引擎面板点击「拉取音色清单」。</span>
        </template>
        <template v-else-if="selectedEntry && selectedEntry.uses_reference_audio">
          这台引擎不提供音色清单：合成时用的就是<b>音色库里的音色</b>。
        </template>
        <template v-else> 暂无音色。 </template>
      </template>
      <template v-else-if="hasAnyFilter">
        没有符合筛选的音色。
        <span class="empty-actions">
          <ActionButton size="sm" @click="clearFilters">清除筛选</ActionButton>
        </span>
      </template>
      <template v-else-if="store.selection.kind === 'design'">
        还没有设计音色。
        <span class="empty-note">在生成页左列头点「设计音色」，写一句描述就能建一条。</span>
      </template>
      <template v-else-if="store.tree.total">
        当前分类下还没有音色。
        <span class="empty-actions">
          <ActionButton size="sm" @click="actions.select({ kind: 'all' })">看全部音色</ActionButton>
        </span>
      </template>
      <template v-else>
        尚未扫描到音色。<span class="empty-note">点左下角设置，添加扫描根目录。</span>
      </template>
    </div>
    <div
      v-else
      class="grid-stack"
      :class="{ 'is-refreshing': slowLoading }"
      :aria-busy="store.loading"
    >
      <!-- 每 96 张一块：只有视口附近那一两块挂真卡，离开后整块换成骨架。
           计数与空态仍看全量的 visible，不受窗口影响。 -->
      <VoiceGridBlock
        v-for="block in blocks"
        :key="block.start"
        :voices="visible"
        :start="block.start"
        :count="block.count"
        :initial-active="block.start === 0"
      />
    </div>
  </div>
</template>

<script setup>
// @ts-check
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  FolderOpen,
  FolderSearch,
  Loader2,
  SearchX,
  Sparkles,
  TriangleAlert,
} from 'lucide-vue-next'
import { actions, store, visibleVoices } from '../store'
import ActionButton from './ui/ActionButton.vue'
import VoiceGridBlock from './VoiceGridBlock.vue'

/** 顺序与「全部播放」共用一份（`store/library.js` 的 `visibleVoices`）：
 *  有样例的排前面，平台清单动辄几百条，真正要用的那几条不该沉底。 */
const visible = computed(() => visibleVoices())

/** 一块约 24 行。块大小必须是当前列数的整数倍：否则每块最后一行会留下空位，
 *  下一块又从新行开始，屏幕中间就会看到一段断开的空卡。 */
const ROWS_PER_BLOCK = 24
const wrapEl = ref(/** @type {HTMLElement | null} */ (null))
const columns = ref(4)
const blockSize = computed(() => Math.max(1, columns.value * ROWS_PER_BLOCK))
const blocks = computed(() => {
  const list = visible.value
  const out = []
  const size = blockSize.value
  for (let start = 0; start < list.length; start += size) {
    out.push({ start, count: Math.min(size, list.length - start) })
  }
  return out
})

/** 列数由 CSS 的 auto-fill 决定，JS 这边读实际排出来的轨道数；
 *  读不到（首次加载还没建网格）就先用 4 列，列表一到位会再量。 */
function measureColumns() {
  const grid = wrapEl.value?.querySelector('.grid')
  if (!grid) return
  const template = getComputedStyle(grid).gridTemplateColumns
  const count = template.split(/\s+/).filter(Boolean).length
  if (count > 0 && count !== columns.value) columns.value = count
}

/** @type {ResizeObserver | null} */
let resizeObserver = null
let measureFrame = 0
function scheduleMeasure() {
  window.cancelAnimationFrame(measureFrame)
  measureFrame = window.requestAnimationFrame(measureColumns)
}

onMounted(() => {
  measureColumns()
  resizeObserver = new ResizeObserver(scheduleMeasure)
  if (wrapEl.value) resizeObserver.observe(wrapEl.value)
})

watch(
  () => visible.value.length,
  () => {
    // 首次有数据 / 筛选后列表才建出网格；下一帧再读轨道数。
    void nextTick(measureColumns)
  }
)

const hasAnyFilter = computed(
  () =>
    store.filters.q ||
    store.filters.gender ||
    store.filters.age ||
    store.filters.language ||
    store.filters.tag
)

/** 首屏才有"加载中"：列表已经有东西时，重查只是换数据，不该清空界面。 */
const firstLoad = computed(() => store.loading && !store.voices.length)

/** 慢请求才提示：180ms 内回来的（本机基本都这么快）连半透明都别闪，
 *  一闪一收比什么都不提示更晃眼。 */
const slowLoading = ref(false)
let slowTimer = 0
watch(
  () => store.loading,
  (busy) => {
    window.clearTimeout(slowTimer)
    if (!busy) {
      slowLoading.value = false
      return
    }
    slowTimer = window.setTimeout(() => {
      if (store.loading) slowLoading.value = true
    }, 180)
  }
)
onBeforeUnmount(() => {
  window.clearTimeout(slowTimer)
  window.cancelAnimationFrame(measureFrame)
  resizeObserver?.disconnect()
  resizeObserver = null
})

/** 列表空、又确实带着筛：给一颗就地清掉的按钮，别让人去顶栏找。 */
async function clearFilters() {
  for (const key of Object.keys(store.filters)) store.filters[key] = ''
  await actions.loadVoices()
}
// 按能力（声明了什么）说话，不按引擎名判断，加引擎不用改这里。
const selectedEntry = computed(
  () => store.tree.engines.find((item) => item.source === store.selection.name) || null
)

/** 空状态的头像跟着原因走：筛没了、分类空、还是整库没扫。 */
const emptyIcon = computed(() => {
  if (hasAnyFilter.value) return SearchX
  if (store.selection.kind === 'design') return Sparkles
  if (store.selection.kind === 'platform') return FolderSearch
  return FolderOpen
})
</script>
