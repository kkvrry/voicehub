<template>
  <SettingsSection id="about" title="关于" desc="版本、源码与许可" :show-actions="false">
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
import { computed } from 'vue'
import { ExternalLink, GitBranch, Info, Package, Scale } from 'lucide-vue-next'
import { useSettingsStore } from '../../stores/settings'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'

const REPO = 'https://github.com/kkvrry/voicehub'

const settings = useSettingsStore()
const version = computed(() => settings.backend.version)
const origin = window.location.origin
</script>

<style scoped>
.about-mono {
  font-family: 'Cascadia Code', 'JetBrains Mono', 'Consolas', monospace;
  font-size: 12px;
  color: var(--sh-text-2);
}
</style>
