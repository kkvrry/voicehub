<template>
  <div
    ref="shellEl"
    class="app-shell"
    :data-mode="mode"
    :class="{ 'has-player': Boolean(app.player.url) }"
    :data-look="settings.appearance.look"
    :data-accent="settings.appearance.accent"
    :data-density="settings.appearance.density"
    :data-motion="settings.appearance.reduceMotion ? 'off' : 'on'"
    :data-ambient="settings.appearance.ambient ? 'on' : 'off'"
    :data-desc="settings.general.showDescriptions ? 'on' : 'off'"
    :data-nav-auto="settings.appearance.navAutoHide ? 'on' : 'off'"
    :data-nav-labels="settings.appearance.navHideLabels ? 'off' : 'on'"
    :style="settings.shellStyle"
  >
    <div class="sh-ambient" aria-hidden="true"><span /><span /><span /></div>

    <!-- 顶部品牌栏：所有页面共用这一条（设置页与音色库不再是两套壳） -->
    <header class="sh-topbar">
      <button type="button" class="sh-brand" title="回到音色库" @click="navigate('library')">
        <span class="sh-brand-mark">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M4 10v4M8 6.5v11M12 3.5v17M16 6.5v11M20 10v4"
              stroke="currentColor"
              stroke-width="1.9"
              stroke-linecap="round"
            />
          </svg>
        </span>
        <span class="sh-brand-text">
          <span class="sh-brand-name">VoiceHub</span>
          <span class="sh-brand-sub">Voice Studio</span>
        </span>
      </button>

      <!-- 播放条属于这次会话，不属于某一页：挂在顶栏上，换页断不了声音。
           放完自动收起（见 store/playback.js 的 ended）。 -->
      <Transition name="pb">
        <PlayerBar v-if="app.player.url" />
      </Transition>

      <div v-if="showSearch" class="sh-search">
        <Search :size="16" :stroke-width="1.8" aria-hidden="true" />
        <input
          ref="searchInput"
          v-model="queryProxy"
          type="text"
          :placeholder="placeholder"
          :aria-label="placeholder"
          @keydown.enter.prevent="$emit('search-enter')"
          @keydown.esc.prevent="clearSearch"
        />
        <button
          v-if="queryProxy"
          type="button"
          class="sh-search-clear"
          title="清空搜索"
          aria-label="清空搜索"
          @click="clearSearch"
        >
          <X :size="14" :stroke-width="1.8" />
        </button>
        <kbd v-else>{{ hintText }}</kbd>
        <slot name="search-actions" />
      </div>

      <div class="sh-topbar-right">
        <slot name="topbar" />
        <button
          type="button"
          class="sh-theme-toggle"
          :title="isDark ? '切换到浅色' : '切换到深色'"
          :aria-label="isDark ? '切换到浅色主题' : '切换到深色主题'"
          @click="toggleTheme"
        >
          <span class="sh-theme-icon">
            <Moon v-if="isDark" :size="14" :stroke-width="1.8" />
            <Sun v-else :size="14" :stroke-width="1.8" />
          </span>
          <span class="sh-theme-label">{{ isDark ? '深色' : '浅色' }}</span>
        </button>
      </div>
    </header>

    <div v-if="offlineBanner" class="sh-banner">
      <AlertTriangle :size="14" :stroke-width="1.8" />
      <span>后端服务未连接：目录与存储位置会先存在本机，启动服务后再保存一次即可同步。</span>
      <button type="button" class="sh-banner-close" aria-label="关闭提示" @click="bannerClosed = true">
        <X :size="14" :stroke-width="1.8" />
      </button>
    </div>

    <div class="sh-body">
      <!-- 导航装一层坞：正常时是网格第一列；开启自动隐藏后坞收成左边缘一条窄带，
           悬停 / 键盘聚焦才滑出，不再占正文宽度。 -->
      <div class="sh-nav-dock">
        <AppNav
          :active="active"
          :rail-toggle="mode === 'app' && showRail && Boolean($slots.sidebar)"
          :rail-hint="railHint"
          @navigate="navigate"
        />
      </div>

      <!-- 第二列与右侧内容合成一块（分类栏 + 内容 + 底栏）：
           设置页整块居中，分类栏始终紧贴卡片；音色库这一块铺满，第二列是音色树。
           「引擎」页的第二列换成插件清单（TTS / 对话），「日志」页不要第二列。
           左栏不另设收起控件：再点一次当前页的导航图标即收起 / 展开（见 navigate）。 -->
      <div
        class="sh-pane"
        :class="{ 'is-bare': !showRail || !$slots.sidebar || !showTree }"
      >
        <aside
          v-if="showRail && $slots.sidebar"
          v-show="showTree"
          class="sh-rail"
          :class="{ 'sh-sidebar': mode === 'app' }"
        >
          <slot name="sidebar" />
        </aside>

        <main v-if="mode === 'settings'" ref="contentEl" class="sh-content">
          <div class="sh-content-inner">
            <slot />
          </div>
        </main>
        <main v-else class="sh-content sh-content-app">
          <slot />
        </main>
      </div>
    </div>

    <slot name="append" />
    <ContextMenuHost />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { AlertTriangle, Moon, Search, Sun, X } from 'lucide-vue-next'
import AppNav from './AppNav.vue'
import ContextMenuHost from '../components/ui/ContextMenu.vue'
import PlayerBar from '../components/PlayerBar.vue'
import { actions, store as appStore } from '../store'
import { useSettingsStore } from '../stores/settings'
import { comboFromEvent } from '../settings/keymap'
import { prettyCombo } from '../settings/utils'
import { setShellHost, releaseShellHost } from '../composables/overlayTarget'

const props = withDefaults(
  defineProps<{
    mode?: 'settings' | 'app'
    /** 高亮哪一项（'library' | 'synth' | 'engine' | 'logs' | 'settings'） */
    active?: string
    query?: string
    showSearch?: boolean
    /** 要不要第二列：日志页没有「我有哪些音色」这回事，整块铺开。 */
    showRail?: boolean
    /** 当前页有没有音色卡片列：有就按「树 + 卡 / 只有卡 / 都收」三档循环，
     *  没有就只在「有树 / 无树」两档之间切换。 */
    railCards?: boolean
    placeholder?: string
    hint?: string
  }>(),
  {
    mode: 'app',
    active: 'library',
    query: '',
    showSearch: true,
    showRail: true,
    railCards: false,
    placeholder: '搜索',
    hint: '',
  }
)

const emit = defineEmits<{
  'update:query': [value: string]
  'search-enter': []
  'search-escape': []
}>()

const settings = useSettingsStore()
const app = appStore

const searchInput = ref<HTMLInputElement | null>(null)
const contentEl = ref<HTMLElement | null>(null)
const shellEl = ref<HTMLElement | null>(null)
const bannerClosed = ref(false)

const isDark = computed(() => app.theme === 'dark')

/** 左栏可见档：设置页的分类栏不参与，始终按"两列都在"画。 */
const leftRailState = computed(() =>
  props.mode === 'app' ? settings.appearance.railVisibility : 'both'
)

/** 目录树只在 `both` 那一档出现；`cards` 是"只有音色栏"，`none` 是都收起。 */
const showTree = computed(() => leftRailState.value === 'both')

/** 当前页图标再点时，提示下一档会发生什么：
 *  三档页（有音色栏）是"收目录树 → 收音色栏 → 全展开"，
 *  两档页（音色库 / 引擎 / 音频库）是"收左栏 ↔ 展开左栏"。 */
const railHint = computed(() => {
  if (!props.railCards) {
    return leftRailState.value === 'both' ? '点击收起左栏' : '点击展开左栏'
  }
  if (leftRailState.value === 'both') return '点击收起目录树'
  if (leftRailState.value === 'cards') return '点击收起音色栏'
  return '点击展开左栏'
})

/** 再点一次当前页的导航图标即切换左栏：不新增控件，图标本来就在手边。 */
function cycleLeftRails() {
  const state = settings.appearance.railVisibility
  if (!props.railCards) {
    // 这一页没有音色栏：只切目录树，用 `cards` 表示"树收起、音色栏留给有它的页"，
    // 不写 `none`——否则在音色库收起树，回到生成页会把音色栏也一起收掉。
    settings.appearance.railVisibility = state === 'both' ? 'cards' : 'both'
    return
  }
  settings.appearance.railVisibility =
    state === 'both' ? 'cards' : state === 'cards' ? 'none' : 'both'
}

const queryProxy = computed({
  get: () => props.query,
  set: (value: string) => emit('update:query', value),
})
const hintText = computed(
  () => props.hint || prettyCombo(settings.shortcuts.focusSearch).join(' ')
)
const offlineBanner = computed(
  () => settings.backend.checked && !settings.backend.online && !bannerClosed.value
)

function toggleTheme() {
  settings.appearance.theme = isDark.value ? 'light' : 'dark'
}

function navigate(key: string) {
  // 当前页的图标再点一次 = 收起 / 展开左栏；没有左栏的页（日志、工具型工作区）
  // 就停在原地，不把同一页重新打开一遍。
  if (key === props.active && props.mode === 'app') {
    if (props.showRail) cycleLeftRails()
    return
  }
  if (key === 'library') {
    void actions.openLibrary()
    return
  }
  if (key === 'synth') {
    void actions.openSynth()
    return
  }
  if (key === 'audio') {
    actions.openAudio()
    return
  }
  if (key === 'logs') {
    actions.openLogs()
    return
  }
  if (key === 'engine') {
    void actions.openPluginHub()
    return
  }
  if (key.startsWith('producer:')) {
    void actions.openProducer(key.slice('producer:'.length))
    return
  }
  if (key === 'settings') actions.openSettings()
}

function clearSearch() {
  emit('update:query', '')
  emit('search-escape')
}

function focusSearch() {
  searchInput.value?.focus()
  searchInput.value?.select()
}

/** 全局只保留这一份「/」处理：设置页不再自己监听，免得两处抢同一个键。 */
function onKeydown(event: KeyboardEvent) {
  const combo = comboFromEvent(event)
  if (!combo || combo !== settings.shortcuts.focusSearch) return
  const target = event.target as HTMLElement | null
  const typing =
    !!target &&
    (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
  if (typing) return
  event.preventDefault()
  focusSearch()
}

onMounted(() => {
  // 常驻弹层（引擎清单、指定音色）跟着这一棵壳走：进设置页会把壳换掉。
  setShellHost(shellEl.value)
  window.addEventListener('keydown', onKeydown)
  void settings.ensureRemote()
})

onBeforeUnmount(() => {
  releaseShellHost(shellEl.value)
  window.removeEventListener('keydown', onKeydown)
})

defineExpose({ focusSearch, contentEl })
</script>
