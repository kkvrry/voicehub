<template>
  <!-- 一个应用只有一个壳：设置页是壳内的一屏，不是另一个 AppChrome。
       壳会被整个替换时（两个实例各带一份 `data-mode`），弹层、皮肤选择器与
       `--panel` 这套别名都会指向一个已经换掉的元素——进设置页再回来，
       Teleport 就画进了一棵脱离文档的树。 -->
  <AppChrome
    :mode="isSettings ? 'settings' : 'app'"
    :active="activeNav"
    :show-rail="showShellRail"
    :show-search="showVoiceSearch"
    :rail-cards="railCards"
    v-model:query="searchQuery"
    :placeholder="searchPlaceholder"
    :hint="isSettings ? searchHint : ''"
    @search-enter="onSearchEnter"
  >
    <template #search-actions>
      <TopVoiceFilters v-if="showVoiceFilters" />
    </template>
    <template #sidebar>
      <!-- 设置屏是分类栏，引擎页是插件清单，其余页面的第二列都是音色结构树。 -->
      <SettingsRail v-if="isSettings" />
      <PluginRail v-else-if="store.view === 'engine'" />
      <AudioRail v-else-if="store.view === 'audio'" />
      <SourceTree v-else />
    </template>

    <!-- 设置屏与音色库等页面是同一层的兄弟：主区换内容，壳不动。
         设置页的内容按它自己的节奏分节，不套 `.app-page`（那一层是页面宽度政策）。 -->
    <SettingsPage v-if="isSettings" />

    <!-- 其余页面：哪一页写在 `data-page` 上——宽度政策（铺满 / 收到中间）由样式表
         一处按它决定，不给每个页面各自写一遍 max-width。 -->
    <div
      v-else
      class="app-page"
      :data-page="store.view"
      :data-producer="store.view === 'producer' ? store.producer.key : undefined"
    >
      <template v-if="store.view === 'engine'">
        <AsrPanel v-if="store.pluginKind === 'asr'" />
        <EnginePanel v-else-if="store.pluginKind === 'tts'" />
        <LlmPanel v-else />
      </template>
      <div
        v-else-if="store.view === 'producer'"
        class="producer-layout"
        :class="{ 'is-tree-hidden': showProducerRail && treeHidden }"
      >
        <!-- 音色栏由插件自描述决定（`voice_rail`）：创作型工作区要挑音色，
             audio.cpp 这类运行时管理页不需要，整列让给工作区。
             栏的用法同样自描述（`rail_mode`）：`pick` = 点卡即选中（处理目标），
             `assign` = 卡右「指定」把音色交给工作区选角色。 -->
        <aside
          v-if="showProducerRail"
          v-show="showCards"
          class="producer-cards"
        >
          <SynthVoiceRail
            :pickable="railMode === 'pick'"
            :assignable="railMode !== 'pick'"
            :target="railMode === 'pick' ? 'pick' : 'synth'"
          />
        </aside>
        <ProducerWorkspace />
      </div>
      <SynthPanel v-else-if="store.view === 'synth'" />
      <AudioLibrary v-else-if="store.view === 'audio'" />
      <LogView v-else-if="store.view === 'logs'" />
      <template v-else>
        <FilterBar />
        <VoiceGrid />
      </template>
    </div>

    <template #append>
      <!-- 音色详情是音色库的抽屉：设置屏不带它（与设置页没有这一列时一致）。 -->
      <DetailPanel v-if="!isSettings" />
    </template>
  </AppChrome>

  <Transition name="toast-pop">
    <div
      v-if="store.notice"
      :key="store.noticeAt"
      class="toast"
      :class="`is-${store.noticeKind}`"
      role="status"
      aria-live="polite"
      @mouseenter="actions.setNoticePaused(true)"
      @mouseleave="actions.setNoticePaused(false)"
    >
      <component :is="noticeIcon" :size="16" :stroke-width="1.8" aria-hidden="true" />
      <span>{{ store.notice }}</span>
    </div>
  </Transition>

  <Transition name="toast-pop">
    <div
      v-if="store.avatarJob.running"
      class="toast is-info avatar-progress"
      role="status"
      aria-live="polite"
      :style="{ '--job-progress': `${avatarProgress}%` }"
    >
      <Loader2 :size="16" :stroke-width="1.8" class="sh-spin" aria-hidden="true" />
      <span>
        查找头像 {{ store.avatarJob.done }}/{{ store.avatarJob.total }}
        <template v-if="store.avatarJob.name"> · {{ store.avatarJob.name }}</template>
        <template v-if="store.avatarJob.failed"> · 失败 {{ store.avatarJob.failed }}</template>
        <template v-if="store.avatarJob.stopping"> · 当前张完成后停止</template>
      </span>
      <button
        type="button"
        class="toast-stop"
        :disabled="store.avatarJob.stopping"
        @click="actions.stopAvatarJob()"
      >
        {{ store.avatarJob.stopping ? '停止中…' : '停止' }}
      </button>
    </div>
  </Transition>

  <!-- 连接时补逐字稿：后端自己起线程，前端轮询进度；停止只到"当前这条收尾"。 -->
  <Transition name="toast-pop">
    <div
      v-if="store.asrJob.running"
      class="toast is-info asr-progress"
      role="status"
      aria-live="polite"
      :style="{ '--job-progress': `${asrProgress}%` }"
    >
      <Loader2 :size="16" :stroke-width="1.8" class="sh-spin" aria-hidden="true" />
      <span>
        补逐字稿 {{ store.asrJob.done }}<template v-if="store.asrJob.total">/{{ store.asrJob.total }}</template>
        <template v-if="store.asrJob.failed"> · 失败 {{ store.asrJob.failed }}</template>
        <template v-if="store.asrJob.stopping"> · 当前条完成后停止</template>
      </span>
      <button
        type="button"
        class="toast-stop"
        :disabled="store.asrJob.stopping"
        @click="actions.stopAsrFill()"
      >
        {{ store.asrJob.stopping ? '停止中…' : '停止' }}
      </button>
    </div>
  </Transition>

  <!-- 任何页面都能「指定音色」：播放条上那颗按钮把插件自己的弹窗叫出来。
       它不属于某一页，所以挂在壳外、与 view 无关。 -->
  <ProducerOverlay />
  <!-- 引擎清单：从「引擎」页的清单里叫出来，挂在壳外与具体某台无关。 -->
  <EngineCatalog />
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { AlertTriangle, CheckCircle2, Info, Loader2, XCircle } from 'lucide-vue-next'
import { store, actions } from './store'
import AppChrome from './shell/AppChrome.vue'
import SourceTree from './components/SourceTree.vue'
import SynthVoiceRail from './components/SynthVoiceRail.vue'
import TopVoiceFilters from './components/TopVoiceFilters.vue'
import PluginRail from './components/PluginRail.vue'
import ProducerOverlay from './producers/ProducerOverlay.vue'
import ProducerWorkspace from './producers/ProducerWorkspace.vue'
import FilterBar from './components/FilterBar.vue'
import VoiceGrid from './components/VoiceGrid.vue'
import DetailPanel from './components/DetailPanel.vue'
import SettingsPage from './settings/SettingsPage.vue'
import SettingsRail from './settings/SettingsRail.vue'
import { jumpFirst, query as settingsQuery, searchHint } from './settings/pane'
import { useSettingsStore } from './stores/settings'
import AsrPanel from './components/AsrPanel.vue'
import EngineCatalog from './components/EngineCatalog.vue'
import EnginePanel from './components/EnginePanel.vue'
import LlmPanel from './components/LlmPanel.vue'
import SynthPanel from './components/SynthPanel.vue'
import AudioLibrary from './components/AudioLibrary.vue'
import AudioRail from './components/AudioRail.vue'
import LogView from './components/LogView.vue'

/** 设置屏是壳内的一屏：壳的形态（mode）与主区内容跟着它换，壳本身不换。 */
const isSettings = computed(() => store.view === 'settings')

const settings = useSettingsStore()

/** 顶部搜索同一格绑两件事：音色库等页筛音色，设置屏搜设置项。
 *  音色那一份的规矩不变——三页都摆着音色卡片，搜索就地筛卡片；其余页面才回音色库看结果。
 *  音频库有自己的筛选词：那一页搜的是文本、文件名与音色名，不与音色库共用一份。 */
const searchQuery = computed({
  get: () => {
    if (isSettings.value) return settingsQuery.value
    if (store.view === 'audio') return store.audio.q
    return store.filters.q
  },
  set: (value: string) => {
    if (isSettings.value) {
      settingsQuery.value = value
      return
    }
    if (store.view === 'audio') {
      actions.setAudioQuery(value)
      return
    }
    store.filters.q = value
    if (!['library', 'synth', 'producer'].includes(store.view)) store.view = 'library'
    scheduleVoices()
  },
})

const searchPlaceholder = computed(() => {
  if (isSettings.value) return '搜索设置项，例如「音量」「目录」'
  if (store.view === 'audio') return '搜索文本、文件名或音色……'
  return '搜索音色、描述……'
})

/** 回车只在设置屏有意义：跳到第一个命中的设置项。 */
function onSearchEnter() {
  if (isSettings.value) jumpFirst()
}

/** 音色库、生成页、创作页共用顶栏这一套搜索与筛选：性别 / 年龄 / 语言 / 标签。 */
const showVoiceFilters = computed(
  () => store.view === 'library' || store.view === 'synth' || store.view === 'producer'
)

/** 导航高亮：生产方是 `producer:<key>`，其余页直接用视图名。 */
const activeNav = computed(() =>
  store.view === 'producer' ? `producer:${store.producer.key}` : store.view
)

/** 当前生产方要不要左侧音色栏：插件在清单里自描述，宿主不按 key 判断。 */
const showProducerRail = computed(() => {
  const entry = store.producers.find((item) => item.key === store.producer.key)
  return entry ? entry.voice_rail !== false : true
})

/** 音色栏怎么用：`pick` = 点卡即选中（处理型工作区），`assign` = 卡右「指定」
 *  （创作型）。缺省按指定——老插件没这个字段。 */
const railMode = computed(() => {
  const entry = store.producers.find((item) => item.key === store.producer.key)
  return entry?.rail_mode === 'pick' ? 'pick' : 'assign'
})

/** 这一页有没有音色卡片列：生成页有，带音色栏的生产方工作区也有。
 *  三档切换只在有这一列时成立；没有这一列的页只切目录树。 */
const railCards = computed(
  () => store.view === 'synth' || (store.view === 'producer' && showProducerRail.value)
)

/** 音色卡片列只在 `none` 那一档收起；`cards` 是"只有它"。 */
const showCards = computed(() => settings.appearance.railVisibility !== 'none')

/** 目录树收起（`cards` / `none`）后，工作区要把原本让给树的左边距还回来。 */
const treeHidden = computed(() => settings.appearance.railVisibility !== 'both')

/** 工具型工作区（不要音色栏）也不显示音色结构树与音色搜索：这一页不归音色库管。 */
const showShellRail = computed(
  () => store.view !== 'logs' && (store.view !== 'producer' || showProducerRail.value)
)
const showVoiceSearch = computed(
  () => store.view !== 'producer' || showProducerRail.value
)

/** 提示图标跟着档次走：错误/警告/成功/普通各一颗。 */
const NOTICE_ICONS = {
  info: Info,
  success: CheckCircle2,
  warn: AlertTriangle,
  error: XCircle,
}
const noticeIcon = computed(
  () => NOTICE_ICONS[store.noticeKind as keyof typeof NOTICE_ICONS] || Info
)

/** 进度条按"已收尾的张数"算：失败也算收尾，不然停在一张坏图上永远 0%。 */
const avatarProgress = computed(() => {
  const job = store.avatarJob
  if (!job.total) return 0
  return Math.min(100, Math.round(((job.done + job.failed) / job.total) * 100))
})

/** 补逐字稿的进度条：总数是这一轮开始时的待补条数，写回与失败都算走过。 */
const asrProgress = computed(() => {
  const job = store.asrJob
  if (!job.total) return 0
  return Math.min(100, Math.round(((job.done + job.failed) / job.total) * 100))
})

let voiceTimer = 0

function scheduleVoices() {
  window.clearTimeout(voiceTimer)
  voiceTimer = window.setTimeout(() => {
    void actions.loadVoices()
  }, 220)
}

onMounted(async () => {
  // 日志流一进来就接上：左栏入口的角标要实时显示"攒了多少条要看的"。
  actions.openLogStream()
  // 生产方清单决定导航项与设置分组；先取一次，导航才不会空一格。
  void actions.loadProducers()
  await actions.refresh()
})

onBeforeUnmount(() => {
  window.clearTimeout(voiceTimer)
})
</script>
