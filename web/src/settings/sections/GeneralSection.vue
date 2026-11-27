<template>
  <SettingsSection id="general" title="通用设置">
    <template #icon><SlidersHorizontal :size="16" :stroke-width="1.8" /></template>

    <SettingCard title="基础设置" keywords="启动 自动扫描 记住 筛选 首页 说明 通用 基础">
      <template #icon><Power :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="启动时自动扫描音色库"
        keywords="自动 启动 扫描 刷新 音色库"
      >
        <ToggleSwitch v-model="store.general.autoScan" label="启动时自动扫描音色库" />
      </SettingRow>

      <SettingRow
        title="记住上次的筛选条件"
        keywords="筛选 记忆 恢复 标签 性别 语言"
      >
        <ToggleSwitch v-model="store.general.rememberFilters" label="记住上次的筛选条件" />
      </SettingRow>

      <SettingRow
        title="显示设置项说明"
        keywords="说明 描述 紧凑 文字"
      >
        <ToggleSwitch v-model="store.general.showDescriptions" label="显示设置项说明" />
      </SettingRow>
    </SettingCard>

    <SettingCard
      title="启动与访问"
      keywords="启动 开机 自启 登录 托盘 桌面版 网络 局域网 监听 地址 端口 令牌 鉴权 访问 安全 同网段 https 证书 加密 麦克风"
    >
      <template #icon><Server :size="16" :stroke-width="1.8" /></template>

      <SettingRow title="开机自启" :desc="autoDesc" keywords="开机 自启 登录 启动 托盘">
        <ToggleSwitch
          v-model="autoStart"
          label="开机自启"
          :disabled="!store.service.autoSupported"
        />
      </SettingRow>

      <SettingRow
        title="允许局域网访问"
        desc="重启生效"
        keywords="局域网 同网段 手机 平板 其他设备 监听"
      >
        <ToggleSwitch v-model="lanAllowed" label="允许局域网访问" />
      </SettingRow>

      <SettingRow
        title="HTTPS"
        desc="自签证书会告警；关闭后其它设备用不了麦克风；自动 = 开放局域网时开启。"
        keywords="https 证书 自签 加密 麦克风 安全上下文 重启"
      >
        <SegmentedControl v-model="httpsMode" :options="httpsOptions" label="HTTPS" />
      </SettingRow>

      <SettingRow
        title="访问令牌"
        keywords="令牌 密码 鉴权 登录 访问控制"
      >
        <TextField
          v-model="tokenInput"
          type="password"
          label="访问令牌"
          autocomplete="new-password"
          clearable
        />
      </SettingRow>

      <div class="service-foot">
        <span class="sh-hint">{{ lanState }}</span>
      </div>
    </SettingCard>

    <SettingCard
      title="系统朗读（SAPI）"
      keywords="系统 朗读 sapi 语音 引擎 桥接 默认 音色 阅读器 word windows"
    >
      <template #icon><Volume2 :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="SAPI 5 语音桥接"
        :desc="sapiInstallDesc"
        keywords="安装 卸载 注册 管理员 权限 桥接 sapi"
      >
        <div class="sapi-actions">
          <ActionButton
            v-if="!store.sapi.installed"
            variant="primary"
            size="sm"
            :loading="store.sapi.busy"
            :disabled="!store.backend.online || !store.sapi.canInstall"
            @click="store.installSapi()"
          >
            安装
          </ActionButton>
          <template v-else>
            <ActionButton
              variant="soft"
              size="sm"
              :loading="store.sapi.busy"
              :disabled="!store.backend.online || !store.sapi.canInstall"
              @click="store.installSapi()"
            >
              重新安装
            </ActionButton>
            <ActionButton
              variant="danger"
              size="sm"
              :loading="store.sapi.busy"
              :disabled="!store.backend.online || !store.sapi.canInstall"
              @click="store.uninstallSapi()"
            >
              卸载
            </ActionButton>
          </template>
        </div>
      </SettingRow>

      <SettingRow
        title="朗读音色"
        desc="引擎：自动 · 留空由引擎自动选择"
        keywords="音色 声线 声音 选择 朗读 sapi"
      >
        <div class="sapi-voice">
          <VoicePicker
            v-model="sapiVoiceId"
            :voices="voiceList"
            placeholder="自动选择或搜索音色"
          />
          <button
            v-if="sapiVoiceId"
            type="button"
            class="sh-btn is-icon is-sm"
            title="清空音色（恢复自动选择）"
            aria-label="清空音色"
            @click="sapiVoiceId = ''"
          >
            <X :size="14" :stroke-width="1.8" />
          </button>
        </div>
      </SettingRow>

      <SettingRow
        title="设为系统默认音色"
        desc="走 SAPI 5 的系统程序自动使用 VoiceHub 朗读"
        keywords="默认 音色 接管 系统朗读 sapi"
      >
        <ToggleSwitch
          v-model="sapiDefault"
          label="设为系统默认音色"
          :disabled="!store.sapi.supported || !store.sapi.installed || store.sapi.busy"
        />
      </SettingRow>

      <div class="service-foot">
        <span class="sh-hint">{{ sapiFoot }}</span>
      </div>
    </SettingCard>

  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { Power, Server, SlidersHorizontal, Volume2, X } from 'lucide-vue-next'
import { useSettingsStore } from '../../store/settings'
import { actions, store as shellStore } from '../../store'
import type { SegmentOption } from '../types'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'
import ToggleSwitch from '../../components/ui/ToggleSwitch.vue'
import SegmentedControl from '../../components/ui/SegmentedControl.vue'
import TextField from '../../components/ui/TextField.vue'
import ActionButton from '../../components/ui/ActionButton.vue'
import VoicePicker from '../../components/VoicePicker.vue'

const store = useSettingsStore()

onMounted(() => {
  if (!shellStore.allVoices.length) {
    void actions.loadAllVoices().catch(() => {})
  }
})

const voiceList = computed(() =>
  shellStore.allVoices.length ? shellStore.allVoices : shellStore.voices
)

const sapiVoiceId = computed({
  get: () => store.sapi.voiceId,
  set: (value: string) => {
    store.sapi.voiceId = value
  },
})

const sapiDefault = computed({
  get: () => store.sapi.isDefault,
  set: (value: boolean) => {
    store.sapi.isDefault = value
  },
})

const sapiInstallDesc = computed(() => {
  if (!store.backend.online) return '后端未连接'
  if (!store.sapi.supported) return '仅 Windows 可用'
  if (store.sapi.installed) return '已安装；修改音色与默认开关保存即生效'
  if (!store.sapi.canInstall) return '未找到桥接安装脚本'
  return '未安装；安装需管理员权限并编译原生桩'
})

const sapiFoot = computed(() => {
  if (!store.backend.online) return '后端未连接'
  if (!store.sapi.supported) return '仅支持 Windows SAPI 5'
  const state = store.sapi.installed
    ? store.sapi.isDefault
      ? '已安装 · 当前为系统默认音色'
      : '已安装 · 未设为默认'
    : '未安装'
  const ep = store.sapi.endpoint ? ` · ${store.sapi.endpoint}` : ''
  return `${state}${ep} · 接口不可用时自动回落系统音色`
})

/** 局域网开关：读当前生效值，改动只进待提交状态。 */
const lanAllowed = computed({
  get: () => store.service.lan,
  set: (value: boolean) => {
    store.service.lan = value
  },
})

const httpsOptions: SegmentOption[] = [
  { value: 'auto', label: '自动' },
  { value: 'on', label: '开启' },
  { value: 'off', label: '关闭' },
]

const httpsMode = computed({
  get: () => store.service.https as string,
  set: (value: string | number) => {
    store.service.https = value === 'on' || value === 'off' ? value : 'auto'
  },
})

const tokenInput = computed({
  get: () => store.service.token,
  set: (value: string) => {
    store.service.token = value
  },
})

/** 状态行：只写当前生效的暴露面；配置改了但没重启的，另起「重启后……」。 */
const lanState = computed(() => {
  if (!store.backend.online) return '后端未连接'
  const { lan, liveLan, liveTokenSet, https, liveHttps, httpsKnown } = store.service
  const base = liveLan ? `局域网可访问${liveTokenSet ? '（需令牌）' : '，无令牌'}` : '仅本机'
  const notes: string[] = []
  if (lan !== liveLan) notes.push(`重启后${lan ? '开放局域网' : '回到仅本机'}`)
  // `auto` 跟着（同样待兑现的）局域网开关走；老进程不报 scheme 时这句无从谈起。
  if (httpsKnown) {
    const wantHttps = https === 'on' || (https === 'auto' && lan)
    if (wantHttps !== liveHttps) notes.push(`重启后改用 ${wantHttps ? 'https' : 'http'}`)
  }
  // 浏览器只在 https 或 localhost 下给麦克风；这一页拿不到就直说，别让人去猜开关。
  if (!window.isSecureContext) notes.push('本页用不了麦克风')
  return notes.length ? `${base} · ${notes.join(' · ')}` : base
})

const autoStart = computed({
  get: () => store.service.autostart,
  set: (value: boolean) => {
    store.service.autostart = value
  },
})

/** 只有桌面启动器能写注册表；源码态禁用开关，只标一句不可用的原因。 */
const autoDesc = computed(() => (store.service.autoSupported ? '' : '仅桌面版生效'))
</script>

<style scoped>
.service-foot {
  padding-top: 4px;
}

.sapi-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.sapi-voice {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  width: 240px;
  max-width: 100%;
}

.sapi-voice :deep(.vpick) {
  flex: 1;
  min-width: 0;
}
</style>
