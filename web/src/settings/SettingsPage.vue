<template>
  <!-- 设置屏的主区。壳由 `App.vue` 统一提供（一个应用只有一个壳），
       这一屏只画抬头与当前那几组设置；分类栏在 `SettingsRail`。 -->
  <header class="sh-page-head">
    <h1>设置</h1>
  </header>

  <div v-if="noResult" class="sh-no-result">
    <SearchX :size="24" :stroke-width="1.8" />
    <strong>没有找到「{{ query }}」</strong>
    <span>换个关键词试试，比如「目录」「玻璃」「头像」「快捷键」。</span>
  </div>

  <div v-show="sectionVisible('general')" class="sh-section-slot"><GeneralSection /></div>
  <div v-show="sectionVisible('services')" class="sh-section-slot"><ServicesSection /></div>
  <div v-show="sectionVisible('appearance')" class="sh-section-slot"><AppearanceSection /></div>
  <div v-show="sectionVisible('shortcuts')" class="sh-section-slot"><ShortcutsSection /></div>
  <div v-show="sectionVisible('asr')" class="sh-section-slot"><AsrSection /></div>
  <div v-show="sectionVisible('embedding')" class="sh-section-slot"><EmbeddingSection /></div>
  <div v-show="sectionVisible('texts')" class="sh-section-slot"><TextsSection /></div>
  <div v-show="sectionVisible('lexicon')" class="sh-section-slot"><LexiconSection /></div>
  <div v-show="sectionVisible('storage')" class="sh-section-slot"><StorageSection /></div>
  <div v-show="sectionVisible('about')" class="sh-section-slot"><AboutSection /></div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, provide, watch } from 'vue'
import { SearchX } from 'lucide-vue-next'
import { actions, say, store as appStore } from '../store'
import { useSettingsStore } from '../stores/settings'
import { ACTIVE_KEY, SEARCH_KEY } from './search'
import { comboFromEvent, recorder } from './keymap'
import {
  activeSection,
  clearQuery,
  firstMatchSection,
  noResult,
  query,
  search,
  sectionVisible,
} from './pane'
import GeneralSection from './sections/GeneralSection.vue'
import ServicesSection from './sections/ServicesSection.vue'
import AppearanceSection from './sections/AppearanceSection.vue'
import ShortcutsSection from './sections/ShortcutsSection.vue'
import AsrSection from './sections/AsrSection.vue'
import EmbeddingSection from './sections/EmbeddingSection.vue'
import TextsSection from './sections/TextsSection.vue'
import LexiconSection from './sections/LexiconSection.vue'
import StorageSection from './sections/StorageSection.vue'
import AboutSection from './sections/AboutSection.vue'

const settings = useSettingsStore()
const app = appStore

provide(SEARCH_KEY, search)
// 抬头右侧的操作按钮只挂在当前这一组上
provide(ACTIVE_KEY, activeSection)

// 搜索命中可能落在别的分组：分类栏高亮跟着移到第一个命中的分组。
watch(query, () => {
  const section = firstMatchSection()
  if (section) activeSection.value = section
})

/** 这一屏自己的键：保存设置、换主题、回音色库。与页面一起挂/卸，
 *  在别的屏按这几个键不生效——它们的含义是"在设置里"。 */
function onKeydown(event: KeyboardEvent) {
  if (recorder.action) return
  const combo = comboFromEvent(event)
  if (!combo) return

  const target = event.target as HTMLElement | null
  const typing =
    !!target &&
    (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)

  if (combo === settings.shortcuts.saveSettings) {
    event.preventDefault()
    void settings.save()
    return
  }
  if (combo === settings.shortcuts.toggleTheme) {
    event.preventDefault()
    settings.appearance.theme = app.theme === 'dark' ? 'light' : 'dark'
    return
  }
  if (combo === settings.shortcuts.backToLibrary) {
    if (typing) {
      target?.blur()
      return
    }
    if (query.value) {
      clearQuery()
      return
    }
    event.preventDefault()
    void actions.openLibrary()
  }
}

onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  await settings.ensureRemote()
  // 生产方分组来自插件清单；从别处直接进设置页时也要有一份。
  if (!app.producers.length) void actions.loadProducers()
  // 语音识别栏的引擎下拉来自后端清单；直接进设置页时也要有一份。
  if (!app.asr.engines.length) void actions.loadAsr().catch(() => {})

  // 启动时自动扫描：一次会话只触发一次，避免每次进设置页都重扫。
  try {
    if (settings.general.autoScan && settings.backend.online && !sessionStorage.getItem('vh-autoscan')) {
      sessionStorage.setItem('vh-autoscan', '1')
      await actions.rescan()
    }
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.sh-section-slot {
  display: flex;
  flex-direction: column;
}
</style>
