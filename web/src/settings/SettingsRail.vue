<template>
  <!-- 中间分类栏：只切分组，右侧不再把六组一次铺到底。
       它挂在壳的左插槽，与主区是兄弟节点，所以状态走 `settings/pane` 那一份。 -->
  <div class="sh-rail-title">设置</div>
  <div class="sh-rail-group" @keydown="onRailKeydown">
    <template v-for="(section, index) in sections" :key="section.id">
      <div v-if="index === 0 || sections[index - 1].group !== section.group" class="sh-rail-sub">
        {{ section.group }}
      </div>
      <button
        type="button"
        class="sh-rail-item"
        :class="{ 'is-active': activeSection === section.id }"
        :aria-current="activeSection === section.id ? 'true' : undefined"
        @click="goSection(section.id)"
      >
        <component :is="section.icon" :size="18" :stroke-width="1.8" aria-hidden="true" />
        <span class="sh-rail-label">{{ section.label }}</span>
        <span v-if="isDirty(section.id)" class="sh-rail-dot" title="这一组有未保存的改动" />
        <span v-else-if="query && countMatches(section.id)" class="sh-rail-count">
          {{ countMatches(section.id) }}
        </span>
      </button>
    </template>
  </div>

  <div class="sh-rail-foot">
    <div class="sh-rail-card">
      <div class="sh-rail-card-row">
        <span class="sh-status-dot" :class="settings.backend.online ? 'is-online' : 'is-offline'" />
        {{ settings.backend.online ? '本地服务运行中' : '本地服务未连接' }}
      </div>
      <div class="sh-rail-card-note">
        VoiceHub · 本地音色工作台<br />
        设置存于本机，目录由服务持久化
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  activeSection,
  countMatches,
  goSection,
  isDirty,
  onRailKeydown,
  query,
  sections,
} from './pane'
import { useSettingsStore } from '../stores/settings'

const settings = useSettingsStore()
</script>
