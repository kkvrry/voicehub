<template>
  <SettingsSection id="asr" title="语音识别">
    <template #icon><Mic :size="16" :stroke-width="1.8" /></template>

    <SettingCard
      title="识别引擎"
      keywords="语音识别 引擎 转文字 当前 引擎状态 配置"
    >
      <template #icon><AudioLines :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="当前识别引擎"
        desc="未指定时按引擎顺序选用当前开着的服务；引擎地址与模型在「引擎 → 识别」里配置。"
        keywords="识别 引擎 选择 当前 asr 转写 自动"
      >
        <div class="asr-pick">
          <select
            v-if="asr.engines.length"
            class="sh-field"
            :value="asr.auto ? '' : asr.current"
            :disabled="choosing"
            aria-label="当前识别引擎"
            @change="choose(($event.target as HTMLSelectElement).value)"
          >
            <option value="">自动（按引擎顺序选开着的服务）</option>
            <option v-for="row in asr.engines" :key="row.key" :value="row.key">
              {{ row.label }}{{ row.ready ? '' : '（未就绪）' }}
            </option>
          </select>
          <span v-else class="sh-hint">尚无识别引擎。</span>
          <span v-if="asr.auto && currentEngine" class="sh-hint">
            当前：{{ currentEngine.label }}
          </span>
          <ActionButton
            v-if="currentEngine"
            variant="soft"
            size="sm"
            @click="configure"
          >
            配置
          </ActionButton>
        </div>
      </SettingRow>

      <SettingRow
        v-if="currentEngine"
        title="引擎状态"
        :desc="currentEngine.ready ? '已就绪，可以使用。' : currentEngine.note || '尚未就绪。'"
        keywords="就绪 状态 地址 模型 未配置"
      >
        <span class="asr-state" :class="{ bad: !currentEngine.ready }">
          {{ currentEngine.ready ? '已就绪' : '未就绪' }}
        </span>
      </SettingRow>

      <div v-else class="sh-empty">
        <Mic :size="20" :stroke-width="1.8" />
        <strong>还没有识别引擎</strong>
        <span>识别引擎是插件：在 app/engines/builtin/ 下建一个目录，重启即出现。</span>
      </div>
    </SettingCard>

    <SettingCard
      title="自动识别"
      keywords="自动 识别 逐字稿 写回 样例 录音 转写 语种"
    >
      <template #icon><Sparkles :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="连接时自动识别"
        desc="识别服务连上后（启动、保存引擎设置、测试连接成功），自动把库里还没有逐字稿的样例听一遍并写回。一轮补一批，下次连接接着补；已有文字的音频不会重复识别。"
        keywords="连接 自动 识别 逐字稿 补全 批量 样例 启动"
      >
        <ToggleSwitch
          v-model="settings.asr.auto_on_connect"
          :disabled="settings.saving"
          label="连接时自动识别"
        />
      </SettingRow>

      <SettingRow
        title="缺逐字稿时自动识别"
        desc="参考音频没有「说话内容」时，先识别再合成；生成页选中的无文本样例也走这一条。关闭后需要手动填写。"
        keywords="自动 识别 逐字稿 参考音频 缺失 补文本"
      >
        <ToggleSwitch
          v-model="settings.asr.auto_transcript"
          :disabled="settings.saving"
          label="缺逐字稿时自动识别"
        />
      </SettingRow>

      <SettingRow
        title="识别结果写回样例"
        desc="自动识别出的文字写回该样例的「说话内容」，下次不必再识别。关闭后结果只用于本次合成。"
        keywords="写回 样例 说话内容 保存 自动"
      >
        <ToggleSwitch
          v-model="settings.asr.writeback"
          :disabled="!settings.asr.auto_transcript || settings.saving"
          label="识别结果写回样例"
        />
      </SettingRow>

      <!-- 能力开关跟着当前引擎的声明走：Qwen3-ASR 声明了语种检测，才出现这一行。
           没声明的功能不摆一个点了才知道不生效的开关。 -->
      <SettingRow
        v-if="supportsLanguage"
        title="语种识别"
        desc="识别结果里的语种写回主样例所属音色；音色已有语言时不覆盖。关闭后识别仍回报语种，只是不写回。"
        keywords="语种 语言 识别 写回 音色 自动"
      >
        <ToggleSwitch
          v-model="settings.asr.language_writeback"
          :disabled="settings.saving"
          label="语种识别"
        />
      </SettingRow>

      <SettingRow
        title="录音后自动转写"
        desc="麦克风录的参考音频落盘后顺手识别。关闭后录音仍可用，可以在参考音频那一格点「识别」补上。"
        keywords="录音 麦克风 自动 转写 参考音频"
      >
        <ToggleSwitch
          v-model="settings.asr.transcribe_recordings"
          :disabled="settings.saving"
          label="录音后自动转写"
        />
      </SettingRow>
    </SettingCard>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { AudioLines, Mic, Sparkles } from 'lucide-vue-next'
import { actions, say, store } from '../../store'
import { useSettingsStore } from '../../stores/settings'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'
import ToggleSwitch from '../../components/ui/ToggleSwitch.vue'
import ActionButton from '../../components/ui/ActionButton.vue'

interface AsrEngineRow {
  key: string
  label: string
  ready: boolean
  note: string
  capabilities?: { returns_language?: boolean }
  returns?: { key: string; has: boolean }[]
}

interface AsrState {
  engines: AsrEngineRow[]
  current: string
  auto: boolean
}

const settings = useSettingsStore()
const asr = computed(() => store.asr as unknown as AsrState)
const currentEngine = computed(
  () => asr.value.engines.find((row) => row.key === asr.value.current) || null
)

/** 当前引擎声明了语种检测，才把「语种识别」这一项摆进自动识别。 */
const supportsLanguage = computed(() => {
  const engine = currentEngine.value
  if (!engine) return false
  if (engine.capabilities) return Boolean(engine.capabilities.returns_language)
  return Boolean(engine.returns?.find((row) => row.key === 'language')?.has)
})

const choosing = ref(false)

async function choose(key: string) {
  const wanted = key || ''
  if (wanted === (asr.value.auto ? '' : asr.value.current) || choosing.value) return
  choosing.value = true
  try {
    await actions.setAsrCurrent(wanted)
    say(wanted ? '已切换识别引擎' : '已改为自动选择')
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    choosing.value = false
  }
}

function configure() {
  if (!currentEngine.value) return
  void actions.openAsrEngine(currentEngine.value.key)
}

onMounted(async () => {
  // 打开这一页时对一下补逐字稿的进度：任务是后端起的，toast 靠轮询跟上。
  void actions.syncAsrFill()
  try {
    await settings.ensureAsrOptions()
  } catch {
    if (!asr.value.engines.length) await actions.loadAsr().catch(() => {})
  }
})
</script>

<style scoped>
.asr-pick {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.asr-pick .sh-field {
  min-width: 180px;
}
.asr-state {
  font-size: 12px;
  color: var(--sh-ok, #2ea05a);
}
.asr-state.bad {
  color: var(--sh-warn, #b7791f);
}
</style>
