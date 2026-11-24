<template>
  <SettingsSection id="storage" title="数据与存储">
    <template #icon><HardDrive :size="16" :stroke-width="1.8" /></template>

    <SettingCard title="扫描根目录" keywords="扫描 根目录 目录 路径 音色库 分类 添加 移除">
      <template #icon><FolderTree :size="16" :stroke-width="1.8" /></template>
      <template #head>
        <ActionButton variant="soft" size="sm" @click="store.addRoot()">
          <Plus :size="14" :stroke-width="1.8" />
          添加目录
        </ActionButton>
        <ActionButton variant="ghost" size="sm" :loading="scanning" @click="rescan">
          <RotateCw v-if="!scanning" :size="14" :stroke-width="1.8" />
          重新扫描
        </ActionButton>
      </template>

      <div v-if="store.scanRoots.length" class="sh-list scan-list">
        <div v-for="(root, index) in store.scanRoots" :key="index" class="sh-list-row">
          <TextField
            :model-value="root"
            label="扫描根目录路径"
            placeholder="例如 D:\\音色库"
            mono
            :clearable="false"
            @update:model-value="setRoot(index, $event)"
          />
          <button
            type="button"
            class="sh-btn is-icon is-sm"
            :title="`移除第 ${index + 1} 个目录`"
            :aria-label="`移除第 ${index + 1} 个目录`"
            @click="store.removeRoot(index)"
          >
            <X :size="14" :stroke-width="1.8" />
          </button>
        </div>
      </div>
      <div v-else class="sh-empty">
        <FolderOpen :size="20" :stroke-width="1.8" />
        <strong>还没有扫描目录</strong>
      </div>

      <div class="scan-hint">
        <Info :size="14" :stroke-width="1.8" />
        <span>
          建议三层以上：扫描根 / 分类 / 音色（例：音色库 / 三国杀 / SP关羽 / 音频）。
          目录暂时不存在也没关系，移动盘未挂载时不会报错。
        </span>
      </div>
    </SettingCard>

    <SettingCard title="存储位置" keywords="存储 位置 生成 缓存 日志 识别 权重 占用 打开">
      <template #icon><Database :size="16" :stroke-width="1.8" /></template>

      <div v-if="store.backend.online" class="sh-storage-list">
        <div v-for="entry in store.entries" :key="entry.key" class="sh-storage-row">
          <div>
            <div class="sh-storage-label">
              {{ entry.label }}
              <span v-if="entry.files" class="sh-chip">{{ entry.files }} 个文件</span>
              <span v-if="entry.locked_by" class="sh-chip is-warn">由 {{ entry.locked_by }} 决定</span>
            </div>
            <TextField
              v-if="entry.editable && !entry.locked_by && fieldOf(entry)"
              class="storage-field"
              :model-value="dirValue(entry)"
              :label="entry.label"
              :placeholder="`默认：${entry.path}`"
              mono
              clearable
              @update:model-value="setDir(entry, $event)"
            />
            <div v-else class="sh-storage-path">{{ entry.path }}</div>
            <div class="sh-hint storage-note">{{ entry.note }}</div>
          </div>
          <div class="sh-storage-side">
            <span class="sh-storage-size">{{ formatBytes(entry.size) }}</span>
            <ActionButton variant="ghost" size="sm" @click="store.openEntry(entry.key)">
              <FolderOpen :size="14" :stroke-width="1.8" />
              打开
            </ActionButton>
          </div>
        </div>
      </div>

      <div v-else class="sh-empty">
        <ServerOff :size="20" :stroke-width="1.8" />
        <strong>后端未连接</strong>
        <span>{{ store.backend.message || '启动本地服务后才能读取真实目录与占用。' }}</span>
        <ActionButton variant="ghost" size="sm" :loading="retrying" @click="reload">
          <RefreshCw v-if="!retrying" :size="14" :stroke-width="1.8" />
          重新检测
        </ActionButton>
      </div>
    </SettingCard>

    <SettingCard title="缓存回收" keywords="缓存 回收 上限 保留 天数 清理 空间 转换">
      <template #icon><Trash2 :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="缓存上限"
        desc="0 = 不限。"
        keywords="缓存 上限 体积 空间 mb 回收"
      >
        <TextField
          class="cache-field"
          type="number"
          suffix="MB"
          label="缓存上限"
          :model-value="String(store.cachePolicy.limit_mb)"
          :disabled="!store.backend.online"
          @update:model-value="setCache('limit_mb', $event)"
        />
      </SettingRow>

      <SettingRow
        title="保留天数"
        desc="0 = 不按时间删。"
        keywords="保留 天数 过期 回收"
      >
        <TextField
          class="cache-field"
          type="number"
          suffix="天"
          label="保留天数"
          :model-value="String(store.cachePolicy.keep_days)"
          :disabled="!store.backend.online"
          @update:model-value="setCache('keep_days', $event)"
        />
      </SettingRow>

      <div class="sh-hint">只清转换产物，音色与生成结果不受影响。</div>
    </SettingCard>

    <SettingCard title="服务与维护" keywords="服务 后端 状态 维护 日志 文档 重扫">
      <template #icon><Wrench :size="16" :stroke-width="1.8" /></template>

      <SettingRow title="后端服务" keywords="后端 服务 连接 状态">
        <span class="sh-chip" :class="store.backend.online ? 'is-ok' : 'is-warn'">
          <span class="sh-status-dot" :class="store.backend.online ? 'is-online' : 'is-offline'" />
          {{ store.backend.online ? '运行中' : '未连接' }}
        </span>
        <ActionButton variant="ghost" size="sm" :loading="retrying" @click="reload">
          <RefreshCw v-if="!retrying" :size="14" :stroke-width="1.8" />
          检测
        </ActionButton>
      </SettingRow>

      <SettingRow title="运行日志" keywords="日志 记录 排查">
        <ActionButton variant="ghost" size="sm" @click="openLogs">
          <ScrollText :size="14" :stroke-width="1.8" />
          打开日志
        </ActionButton>
      </SettingRow>

      <SettingRow title="接口文档" keywords="api 文档 swagger 接口">
        <a class="sh-btn is-ghost is-sm" href="/docs" target="_blank" rel="noreferrer">
          <ExternalLink :size="14" :stroke-width="1.8" />
          打开文档
        </a>
      </SettingRow>
    </SettingCard>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  Database,
  ExternalLink,
  FolderOpen,
  FolderTree,
  HardDrive,
  Info,
  Plus,
  RefreshCw,
  RotateCw,
  ScrollText,
  ServerOff,
  Trash2,
  Wrench,
  X,
} from 'lucide-vue-next'
import { actions, say } from '../../store'
import { useSettingsStore } from '../../stores/settings'
import type { StorageEntry } from '../../stores/settings'
import { formatBytes } from '../../utils/format'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'
import TextField from '../../components/ui/TextField.vue'
import ActionButton from '../../components/ui/ActionButton.vue'

const store = useSettingsStore()

const scanning = ref(false)
const retrying = ref(false)

type StorageField = 'output' | 'cache' | 'log' | 'asr'

const FIELDS: Record<string, StorageField> = {
  output: 'output',
  cache: 'cache',
  log: 'log',
  asr: 'asr',
}

function fieldOf(entry: StorageEntry): StorageField | '' {
  return FIELDS[entry.key] || ''
}

function setRoot(index: number, value: string) {
  const next = [...store.scanRoots]
  next[index] = value
  store.scanRoots = next
}

type CacheField = 'limit_mb' | 'keep_days'

function setCache(field: CacheField, value: string) {
  // 只收数字：``type=number`` 在空值 / 半截输入时给空串，那当作 0（= 该条不启用）。
  const digits = value.replace(/[^0-9]/g, '')
  store.cachePolicy[field] = digits ? Number(digits) : 0
}

function setDir(entry: StorageEntry, value: string) {
  const field = fieldOf(entry)
  if (!field) return
  store.dirs[field] = value
}

function dirValue(entry: StorageEntry): string {
  const field = fieldOf(entry)
  return field ? store.dirs[field] : ''
}

const canRescan = computed(() => store.backend.online && !scanning.value)

async function rescan() {
  if (!canRescan.value) {
    say('后端未连接，无法扫描')
    return
  }
  scanning.value = true
  try {
    await actions.rescan()
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    scanning.value = false
  }
}

async function reload() {
  retrying.value = true
  try {
    await store.loadRemote()
  } finally {
    retrying.value = false
  }
}

function openLogs() {
  actions.openLogs()
}
</script>

<style scoped>
.scan-list {
  padding-bottom: 4px;
}
.storage-field {
  margin-top: 7px;
}
.cache-field {
  width: 132px;
}
.storage-note {
  margin-top: 5px;
}
.scan-hint {
  display: flex;
  gap: 8px;
  margin: 12px 0 6px;
  padding: 10px 12px;
  border-radius: var(--sh-r-md);
  background: var(--sh-accent-soft);
  color: var(--sh-text-3);
  font-size: 11.7px;
  line-height: 1.62;
}
.scan-hint svg {
  flex: none;
  margin-top: 1px;
  color: var(--sh-accent);
}
.sh-empty {
  gap: 8px;
}
</style>
