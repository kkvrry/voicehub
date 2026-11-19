<template>
  <nav class="sh-nav" aria-label="主导航">
    <button
      v-for="item in items"
      :key="item.key"
      type="button"
      class="sh-nav-item"
      :class="{ 'is-active': active === item.key }"
      :title="item.label"
      :aria-current="active === item.key ? 'page' : undefined"
      @click="$emit('navigate', item.key)"
    >
      <span
        v-if="item.img"
        class="sh-nav-glyph"
        :style="{ '--glyph': `url(${item.img})` }"
        aria-hidden="true"
      />
      <component v-else :is="item.icon" :size="20" :stroke-width="1.8" aria-hidden="true" />
      <span class="sh-nav-label">{{ item.label }}</span>
      <span
        v-if="item.key === 'logs' && alerts"
        class="sh-nav-badge"
        :title="`${alerts} 条警告 / 错误`"
      >
        {{ alerts > 99 ? '99+' : alerts }}
      </span>
    </button>
    <div class="sh-nav-spacer" />
    <button
      v-for="item in tailItems"
      :key="item.key"
      type="button"
      class="sh-nav-item"
      :class="{ 'is-active': active === item.key }"
      :title="item.label"
      :aria-current="active === item.key ? 'page' : undefined"
      @click="$emit('navigate', item.key)"
    >
      <span
        v-if="item.img"
        class="sh-nav-glyph"
        :style="{ '--glyph': `url(${item.img})` }"
        aria-hidden="true"
      />
      <component v-else :is="item.icon" :size="20" :stroke-width="1.8" aria-hidden="true" />
      <span class="sh-nav-label">{{ item.label }}</span>
      <span
        v-if="item.key === 'logs' && alerts"
        class="sh-nav-badge"
        :title="`${alerts} 条警告 / 错误`"
      >
        {{ alerts > 99 ? '99+' : alerts }}
      </span>
    </button>
    <div class="sh-nav-divider" />
    <button
      type="button"
      class="sh-nav-item"
      :class="{ 'is-active': active === 'settings' }"
      title="设置"
      :aria-current="active === 'settings' ? 'page' : undefined"
      @click="$emit('navigate', 'settings')"
    >
      <SettingsIcon :size="20" :stroke-width="1.8" aria-hidden="true" />
      <span class="sh-nav-label">设置</span>
    </button>
  </nav>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { AudioLines, Cpu, Mic, ScrollText, Settings as SettingsIcon, Sparkles } from 'lucide-vue-next'
import { store } from '../store'

defineProps<{
  /** 'library' | 'synth' | 'engine' | 'logs' | 'settings' */
  active: string
}>()

defineEmits<{ navigate: [key: string] }>()

type NavItem = { key: string; label: string; icon?: any; img?: string }

/** 生产方自报落哪一组：``tools`` 与引擎、日志同摆在下半段，其余进日常页。 */
function producerItems(section: string) {
  return store.producers
    .filter(
      (plugin) =>
        !store.disabledProducers.includes(plugin.key) &&
        (plugin.nav_section === 'tools' ? 'tools' : 'main') === section
    )
    .map<NavItem>((plugin) => ({
      key: `producer:${plugin.key}`,
      label: plugin.label,
      img: plugin.icon_url,
    }))
}

const items = computed<NavItem[]>(() => [
  { key: 'library', label: '音色库', icon: AudioLines },
  { key: 'synth', label: '生成', icon: Sparkles },
  // 生产方插件自带工作区：位置由插件声明（order），图标用插件目录里的那一张。
  // 停用的插件不进导航；清单本身仍留着，设置 → 插件里可以再启用。
  ...producerItems('main'),
  // 音频库是成品库：排在动作页之后、工具组之前（素材 → 动作 → 成品）。
  { key: 'audio', label: '音频库', icon: Mic },
])

/** 引擎与日志是工具，不是每天要点的入口：沉到导航下半段；
 *  声明了 ``nav_section='tools'`` 的生产方（运行时管理这类）也摆在这一组。 */
const tailItems = computed<NavItem[]>(() => [
  ...producerItems('tools'),
  { key: 'engine', label: '引擎', icon: Cpu },
  { key: 'logs', label: '日志', icon: ScrollText },
])

/** 日志入口的角标：警告与错误的条数。计数由 log.js 随收件增量维护，
 *  每条日志 O(1)——在这里全量 filter 的话，生成音频时每秒几十条日志
 *  足以让导航跟着掉帧。 */
const alerts = computed(() => store.logStats.warning + store.logStats.error)
</script>
