<template>
  <SettingsSection id="general" title="通用设置">
    <template #icon><SlidersHorizontal :size="16" :stroke-width="1.8" /></template>

    <SettingCard title="基础设置" keywords="启动 自动扫描 记住 筛选 首页 说明 通用 基础">
      <template #icon><Power :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="启动时自动扫描音色库"
        desc="打开应用后在后台重扫全部扫描根目录，新增的音频不必手动刷新。"
        keywords="自动 启动 扫描 刷新 音色库"
      >
        <ToggleSwitch v-model="store.general.autoScan" label="启动时自动扫描音色库" />
      </SettingRow>

      <SettingRow
        title="记住上次的筛选条件"
        desc="下次打开时恢复性别、语言、标签等筛选；关闭后每次从完整列表开始。"
        keywords="筛选 记忆 恢复 标签 性别 语言"
      >
        <ToggleSwitch v-model="store.general.rememberFilters" label="记住上次的筛选条件" />
      </SettingRow>

      <SettingRow
        title="默认首页"
        desc="从设置或日志返回时先落在哪一页。"
        keywords="默认 首页 落点 音色库 生成"
      >
        <SegmentedControl v-model="defaultView" :options="viewOptions" label="默认首页" />
      </SettingRow>

      <SettingRow
        title="显示设置项说明"
        desc="关闭后只保留标题，一屏能看更多项；不确定某项作用时可再打开。"
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
        desc="同网段设备可直接打开本机界面。保存后需重启服务才生效。"
        keywords="局域网 同网段 手机 平板 其他设备 监听"
      >
        <ToggleSwitch v-model="lanAllowed" label="允许局域网访问" />
      </SettingRow>

      <SettingRow
        title="HTTPS"
        desc="自签证书，浏览器首次打开会提示不受信任。关闭后其它设备用不了麦克风；自动 = 开放局域网时开启。"
        keywords="https 证书 自签 加密 麦克风 安全上下文 重启"
      >
        <SegmentedControl v-model="httpsMode" :options="httpsOptions" label="HTTPS" />
      </SettingRow>

      <SettingRow
        title="访问令牌"
        desc="留空表示不校验，只适合可信网络；设了之后其它设备首次打开要在登录页输入。"
        keywords="令牌 密码 鉴权 登录 访问控制"
      >
        <TextField
          v-model="tokenInput"
          type="password"
          label="访问令牌"
          placeholder="留空 = 不校验"
          autocomplete="new-password"
          clearable
        />
      </SettingRow>

      <div class="service-foot">
        <span class="sh-hint">{{ lanState }}</span>
      </div>
    </SettingCard>

  </SettingsSection>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Power, Server, SlidersHorizontal } from 'lucide-vue-next'
import { useSettingsStore } from '../../stores/settings'
import type { SegmentOption } from '../types'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'
import ToggleSwitch from '../../components/ui/ToggleSwitch.vue'
import SegmentedControl from '../../components/ui/SegmentedControl.vue'
import TextField from '../../components/ui/TextField.vue'

const store = useSettingsStore()

const viewOptions: SegmentOption[] = [
  { value: 'library', label: '音色库' },
  { value: 'synth', label: '生成' },
]

const defaultView = computed({
  get: () => store.general.defaultView,
  set: (value) => {
    store.general.defaultView = value === 'synth' ? 'synth' : 'library'
  },
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

/** 状态行：只写当前实测值，不写"配置里存了什么"。 */
const lanState = computed(() => {
  if (!store.backend.online) return '后端未连接'
  // 前两段是**生效**的暴露面（拨了开关没重启时不能说已开放），后面才是配置要等重启。
  const { host, port, lan, liveLan, liveTokenSet, https, liveHttps, httpsKnown } = store.service
  const where = liveLan ? `局域网可访问${liveTokenSet ? '（需令牌）' : '，无令牌'}` : '仅本机'
  const base = `${liveHttps ? 'https' : 'http'}://${host}:${port} · ${where}`
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

/** 不支持时把原因说清：源码态写注册表指向 python，登录时会拉起一个不该起的东西。 */
const autoDesc = computed(() =>
  store.service.autoSupported
    ? '登录 Windows 后自动启动并在托盘驻留，不打开浏览器。'
    : '只在桌面版可用：当前进程不是由启动器拉起的。'
)
</script>

<style scoped>
.service-foot {
  padding-top: 4px;
}
</style>
