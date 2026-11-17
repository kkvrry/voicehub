<template>
  <section class="app-panel">
    <header class="panel-head">
      <div class="panel-head-main">
        <div class="engine-title">
          audio.cpp
          <span class="badge" :class="headline.cls">{{ headline.text }}</span>
          <span v-if="version" class="badge gray">{{ version }}</span>
        </div>
      </div>
      <a v-if="data" class="engine-link" :href="data.homepage" target="_blank" rel="noreferrer">项目主页 ↗</a>
    </header>

    <div v-if="!data" class="panel-body">
      <div class="hint" :data-tone="state.error ? 'warn' : undefined">{{ state.error || '载入中' }}</div>
    </div>

    <template v-else>
      <div class="tabs" role="tablist" aria-label="audio.cpp 页面">
        <button
          v-for="item in TABS"
          :key="item.key"
          type="button"
          class="tab"
          :class="{ active: tab === item.key }"
          role="tab"
          :aria-selected="tab === item.key"
          @click="tab = item.key"
        >{{ item.label }}</button>
      </div>
      <div ref="bodyEl" class="panel-body acp-stack">
        <ServiceCard v-if="tab === 'service'" />
        <RuntimeCard v-else-if="tab === 'runtime'" />
        <ModelCard v-else />
      </div>
    </template>
  </section>
</template>

<script setup>
// @ts-check
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ModelCard from './components/ModelCard.vue'
import RuntimeCard from './components/RuntimeCard.vue'
import ServiceCard from './components/ServiceCard.vue'
import { open, refresh, state } from './store'

const TABS = [
  { key: 'service', label: '服务' },
  { key: 'runtime', label: '运行包' },
  { key: 'models', label: '模型' },
]

const data = computed(() => state.data)
const tab = ref('service')
const bodyEl = ref(/** @type {HTMLElement | null} */ (null))
const version = computed(() => data.value?.service?.version || data.value?.runtime?.version || '')

watch(tab, () => {
  if (bodyEl.value) bodyEl.value.scrollTop = 0
})

const headline = computed(() => {
  if (data.value?.endpoint) {
    return data.value.service.reachable
      ? { text: '已接入', cls: 'green' }
      : { text: '连不上', cls: 'warn' }
  }
  const rows = data.value?.instances || []
  if (
    rows.some(
      /** @param {any} row */
      (row) => row.owned && row.running
    )
  ) {
    return { text: '运行中', cls: 'green' }
  }
  if (
    rows.some(
      /** @param {any} row */
      (row) => row.external
    )
  ) {
    return { text: '外部进程', cls: 'gray' }
  }
  return data.value?.runtime?.installed
    ? { text: '已停止', cls: 'gray' }
    : { text: '未配置', cls: 'warn' }
})

let timer = 0
let alive = true

function busy() {
  const runtimeJob = data.value?.runtime?.download?.state === 'downloading'
  const modelJob = (data.value?.models || []).some(
    /** @param {any} row */
    (row) =>
      (row.packages || []).some(
        /** @param {any} pkg */
        (pkg) => pkg.job && ['running', 'queued'].includes(pkg.job.state)
      )
  )
  return runtimeJob || modelJob
}

function schedule() {
  if (!alive) return
  timer = window.setTimeout(async () => {
    await refresh()
    schedule()
  }, busy() ? 1500 : 4000)
}

onMounted(async () => {
  alive = true
  await open()
  schedule()
})

onBeforeUnmount(() => {
  alive = false
  window.clearTimeout(timer)
})
</script>

<style scoped>
.acp-stack {
  display: flex;
  flex-direction: column;
  gap: var(--sh-gap-3, 12px);
}
</style>
