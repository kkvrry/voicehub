<template>
  <SettingsSection id="embedding" title="语义检索" :show-actions="false">
    <template #icon><Brain :size="16" :stroke-width="1.8" /></template>

    <SettingCard
      title="本地向量模型"
      keywords="向量 语义 检索 embedding 模型 下载 BGE 音色 召回 离线"
    >
      <template #icon><Boxes :size="16" :stroke-width="1.8" /></template>

      <div v-if="error" class="sh-hint emb-error">{{ error }}</div>
      <div v-if="!rows.length && !error" class="sh-empty">
        <Boxes :size="20" :stroke-width="1.8" />
        <strong>没有可用的向量模型</strong>
        <span>内置提供方还没加载。</span>
      </div>

      <SettingRow
        v-for="item in rows"
        :key="item.key"
        :title="item.label"
        :desc="item.description"
        :keywords="`${item.label} ${item.license} ${item.dimension}`"
        stacked
      >
        <template #below>
          <div class="emb-meta">
            <span class="sh-chip">{{ sizeText(item) }}</span>
            <span class="sh-chip">{{ item.dimension }} 维</span>
            <span v-if="item.license" class="sh-chip">{{ item.license }}</span>
            <span class="sh-chip" :class="{ 'is-warn': item.state === 'error' }">
              {{ stateText(item) }}
            </span>
          </div>
          <div v-if="item.state === 'downloading'" class="emb-progress">
            <div class="emb-progress-bar">
              <span :style="{ width: `${Math.max(2, item.progress)}%` }" />
            </div>
            <span class="emb-progress-text">
              {{ Math.round(item.progress) }}% · {{ item.note || '下载中……' }}
            </span>
          </div>
          <div v-if="item.state === 'error'" class="sh-hint emb-error">{{ item.note }}</div>
          <div class="sh-hint emb-path" :title="item.path">{{ item.path }}</div>
        </template>

        <ActionButton
          v-if="item.state === 'not_downloaded' || item.state === 'error'"
          variant="primary"
          size="sm"
          :loading="busy === item.key"
          @click="download(item)"
        >
          <Download :size="14" :stroke-width="1.8" />
          {{ item.state === 'error' ? '重试下载' : '下载并启用' }}
        </ActionButton>
        <ActionButton
          v-else-if="item.state === 'ready'"
          variant="ghost"
          size="sm"
          :loading="busy === item.key"
          @click="remove(item)"
        >
          <Trash2 :size="14" :stroke-width="1.8" />
          删除模型
        </ActionButton>
        <span v-else class="sh-hint">下载中……</span>
      </SettingRow>

      <div class="emb-note">
        下载后，AI 分配音色时会先用它在全库里按角色描述召回相近音色，再把候选交给模型复选；
        向量缓存在本机，音色没改就不重复计算，重启后也直接复用。未下载时仍按样例、收藏与名称取前 N 条。
      </div>
    </SettingCard>
  </SettingsSection>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Boxes, Brain, Download, Trash2 } from 'lucide-vue-next'
import { api } from '../../api'
import { say } from '../../store'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'
import ActionButton from '../../components/ui/ActionButton.vue'
import { formatBytes } from '../../utils/format'

interface EmbeddingRow {
  key: string
  label: string
  description: string
  license: string
  dimension: number
  size: number
  path: string
  installed: boolean
  state: 'not_downloaded' | 'downloading' | 'ready' | 'error'
  progress: number
  note: string
}

const rows = ref<EmbeddingRow[]>([])
const error = ref('')
const busy = ref('')
let timer: number | undefined
let disposed = false

async function load() {
  try {
    rows.value = (await api.embeddingProviders()) as EmbeddingRow[]
    error.value = ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
  schedule()
}

function schedule() {
  window.clearTimeout(timer)
  if (disposed) return
  if (!rows.value.some((item) => item.state === 'downloading')) return
  timer = window.setTimeout(() => void load(), 800)
}

async function refreshOne(key: string) {
  try {
    const row = (await api.embeddingProvider(key)) as EmbeddingRow
    rows.value = rows.value.map((item) => (item.key === key ? row : item))
  } catch {
    /* 轮询失败下次再试 */
  }
  schedule()
}

async function download(item: EmbeddingRow) {
  if (busy.value) return
  busy.value = item.key
  try {
    await api.downloadEmbedding(item.key)
    say(`开始下载 ${item.label}`)
    await refreshOne(item.key)
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), { kind: 'error' })
  } finally {
    busy.value = ''
  }
}

async function remove(item: EmbeddingRow) {
  if (busy.value) return
  if (!window.confirm(`删除「${item.label}」模型？\n\n删除后语义检索会退回标签与关键词匹配。`)) {
    return
  }
  busy.value = item.key
  try {
    await api.removeEmbedding(item.key)
    say(`已删除 ${item.label}`)
    await refreshOne(item.key)
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), { kind: 'error' })
  } finally {
    busy.value = ''
  }
}

function stateText(item: EmbeddingRow) {
  if (item.state === 'ready') return '已启用'
  if (item.state === 'downloading') return '下载中'
  if (item.state === 'error') return '下载失败'
  return '未下载'
}

function sizeText(item: EmbeddingRow) {
  return formatBytes(item.size)
}

onMounted(() => void load())
onBeforeUnmount(() => {
  disposed = true
  window.clearTimeout(timer)
})
</script>

<style scoped>
.emb-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 7px;
}
.emb-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.emb-progress-bar {
  flex: 1;
  max-width: 320px;
  height: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--sh-line-soft);
}
.emb-progress-bar span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--sh-accent);
  transition: width 200ms ease;
}
.emb-progress-text {
  font-size: 10.8px;
  color: var(--sh-text-4);
}
.emb-error {
  color: var(--sh-warn);
}
.emb-path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.emb-note {
  margin-top: 10px;
  font-size: 10.8px;
  line-height: 1.6;
  color: var(--sh-text-4);
}
</style>
