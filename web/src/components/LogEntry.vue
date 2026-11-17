<template>
  <!-- 左栏只放一个入口：名字 + 角标；正文在右侧主区那一页（LogView）。
       最新一条挂在 title 上，悬停能看到，不占地方。 -->
  <div
    class="tree-item log-entry"
    :class="{ active: store.view === 'logs' }"
    :title="tip"
    @click="actions.openLogs()"
  >
    <span class="logo">☰</span>
    <span>日志</span>
    <!-- 角标数的是收到的全部，不受那一页的勾选与搜索影响——
         跟着过滤器走等于把事故藏起来。 -->
    <span class="log-entry-badges">
      <b v-if="warnCount" class="badge warn" :title="'警告 ' + warnCount + ' 条'">{{ warnCount }}</b>
      <b v-if="errorCount" class="badge err" :title="'错误 ' + errorCount + ' 条'">{{ errorCount }}</b>
    </span>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import { store, actions } from '../store'

const last = computed(() => store.logs.lines[store.logs.lines.length - 1] || null)
// 计数与来源都由 log.js 增量维护（进一条记一条、环丢一条减一条）：
// 每条日志到达时把整份数组滤一遍，生成音频时就是白送主线程几次全量遍历。
const warnCount = computed(() => store.logStats.warning)
const errorCount = computed(() => store.logStats.error)

/** 悬停说什么：打开哪儿 + 最新那一条。 */
const tip = computed(() => {
  const where = '在右侧打开日志'
  if (!last.value) return `${where}（暂无日志）`
  return `${where}\n最新：${last.value.message}`
})
</script>
