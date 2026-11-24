<template>
  <SettingsSection id="about" title="关于" desc="版本、更新、源码与许可" :show-actions="false">
    <template #icon><Info :size="16" :stroke-width="1.8" /></template>

    <SettingCard title="程序信息" keywords="关于 版本 构建 服务 地址 端口">
      <template #icon><Package :size="16" :stroke-width="1.8" /></template>

      <SettingRow title="VoiceHub" desc="本地音色库与有声书生产工作台" keywords="名称 简介 音色库 有声书">
        <span class="sh-chip">{{ version ? `v${version}` : '版本未知' }}</span>
      </SettingRow>

      <SettingRow title="服务地址" keywords="地址 端口 本地 服务 url 访问">
        <span class="about-mono">{{ origin }}</span>
      </SettingRow>
    </SettingCard>

    <SettingCard title="更新" keywords="更新 检查 发布 release 最新 版本 github">
      <template #icon><RefreshCw :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="发布版本"
        :desc="releaseHint"
        keywords="检查更新 新版本 最新发布 release github 正式版"
      >
        <div class="about-release">
          <span v-if="releaseChip" class="sh-chip" :class="releaseChipClass">{{ releaseChip }}</span>
          <ActionButton
            variant="ghost"
            size="sm"
            :loading="checking"
            :disabled="!settings.backend.online"
            @click="checkRelease"
          >
            <RefreshCw v-if="!checking" :size="14" :stroke-width="1.8" />
            检查更新
          </ActionButton>
        </div>
      </SettingRow>

      <SettingRow v-if="latest" title="最新发布" :desc="latestHint" keywords="最新发布 发布日 版本 release">
        <a class="sh-btn is-ghost is-sm" :href="latest.url" target="_blank" rel="noreferrer">
          <ExternalLink :size="14" :stroke-width="1.8" />
          查看
        </a>
      </SettingRow>
    </SettingCard>

    <SettingCard title="源码与反馈" keywords="仓库 源码 地址 github 反馈 issue 文档">
      <template #icon><GitBranch :size="16" :stroke-width="1.8" /></template>

      <SettingRow title="源码仓库" keywords="源码 仓库 代码 github">
        <a class="sh-btn is-ghost is-sm" :href="REPO" target="_blank" rel="noreferrer">
          <ExternalLink :size="14" :stroke-width="1.8" />
          打开
        </a>
      </SettingRow>

      <SettingRow title="问题反馈" keywords="问题 反馈 bug issue 报错">
        <a class="sh-btn is-ghost is-sm" :href="`${REPO}/issues`" target="_blank" rel="noreferrer">
          <ExternalLink :size="14" :stroke-width="1.8" />
          打开
        </a>
      </SettingRow>
    </SettingCard>

    <SettingCard title="许可" keywords="许可 协议 license mit 版权 第三方 模型 权重">
      <template #icon><Scale :size="16" :stroke-width="1.8" /></template>

      <SettingRow title="许可证" desc="Copyright (c) 2026 kkvrry" keywords="许可 协议 mit 版权 商用">
        <span class="sh-chip">MIT</span>
      </SettingRow>

      <SettingRow
        title="第三方组件"
        desc="引擎、模型权重与云服务遵循各自许可，本仓库不分发权重。"
        keywords="第三方 引擎 模型 权重 商用 条款 致谢"
      >
        <a class="sh-btn is-ghost is-sm" :href="`${REPO}#致谢`" target="_blank" rel="noreferrer">
          <ExternalLink :size="14" :stroke-width="1.8" />
          打开
        </a>
      </SettingRow>
    </SettingCard>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ExternalLink, GitBranch, Info, Package, RefreshCw, Scale } from 'lucide-vue-next'
import { api } from '../../api'
import { useSettingsStore } from '../../stores/settings'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'
import ActionButton from '../../components/ui/ActionButton.vue'

const REPO = 'https://github.com/kkvrry/voicehub'

interface ReleaseLatest {
  tag: string
  version: string
  name: string
  published_at: string
  url: string
}

interface ReleaseCurrent {
  version: string
  mode: 'source' | 'desktop'
  commit: string
  ahead: number
  dirty: boolean
  base_tag: string
}

interface ReleaseCheck {
  current: ReleaseCurrent
  latest: ReleaseLatest | null
  update_available: boolean
  checked_at: string
  error: { code: string; message: string } | null
}

const settings = useSettingsStore()
const version = computed(() => settings.backend.version)
const origin = window.location.origin

const checking = ref(false)
const release = ref<ReleaseCheck | null>(null)
const releaseFailure = ref('')

const latest = computed(() => (checking.value ? null : release.value?.latest || null))
const hasUpdate = computed(() => Boolean(release.value?.update_available))

const releaseChip = computed(() => {
  if (checking.value) return ''
  if (releaseFailure.value || release.value?.error) return '检查失败'
  const item = release.value?.latest
  if (!item) return ''
  return hasUpdate.value ? `有新版本 ${item.tag}` : '已是最新'
})

const releaseChipClass = computed(() => {
  if (releaseFailure.value || release.value?.error) return 'is-warn'
  return hasUpdate.value ? 'is-accent' : 'is-ok'
})

const releaseHint = computed(() => {
  if (!settings.backend.online) return '本地服务未连接，无法检查更新。'
  if (checking.value) return '正在查询 GitHub 上的最新正式发布。'
  if (releaseFailure.value) return releaseFailure.value

  const result = release.value
  if (!result) return ''
  if (result.error) return result.error.message

  const item = result.latest
  if (!item) return '还没有可用的发布信息。'
  if (result.update_available) {
    return `${item.name || item.tag} 发布于 ${formatDate(item.published_at)}；需手动更新。`
  }
  const current = result.current
  const sourceNote =
    current.mode === 'source' && current.commit
      ? `当前源码版 ${current.commit}${current.ahead > 0 ? `，领先 ${current.ahead} 个提交` : ''}。`
      : ''
  return `已是最新正式发布。${sourceNote}`
})

const latestHint = computed(() => {
  const item = latest.value
  if (!item) return ''
  const date = formatDate(item.published_at)
  return [item.name || item.tag, date ? `发布于 ${date}` : ''].filter(Boolean).join(' · ')
})

function formatDate(value: string): string {
  const time = Date.parse(value)
  if (Number.isNaN(time)) return ''
  const date = new Date(time)
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

async function checkRelease() {
  if (checking.value) return
  checking.value = true
  releaseFailure.value = ''
  try {
    release.value = (await api.release(true)) as ReleaseCheck
  } catch (error) {
    release.value = null
    releaseFailure.value = error instanceof Error ? error.message : String(error)
  } finally {
    checking.value = false
  }
}
</script>

<style scoped>
.about-mono {
  font-family: 'Cascadia Code', 'JetBrains Mono', 'Consolas', monospace;
  font-size: 12px;
  color: var(--sh-text-2);
}

.about-release {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
</style>
