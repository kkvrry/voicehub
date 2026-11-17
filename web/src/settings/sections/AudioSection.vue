<template>
  <SettingsSection id="audio" title="音频设置">
    <template #icon><AudioLines :size="16" :stroke-width="1.8" /></template>

    <SettingCard title="音频输出" keywords="音频 输出 格式 采样率 码率 音量 归一化 合成">
      <template #icon><AudioLines :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="输出格式"
        desc="MP3 体积小、通用；WAV / FLAC 用于后期处理与无损留存。"
        keywords="mp3 wav flac 无损 编码 格式"
      >
        <SegmentedControl v-model="format" :options="formatOptions" label="输出格式" />
      </SettingRow>

      <SettingRow
        title="采样率"
        desc="越高越接近原始细节，文件也越大；语音合成 44.1 kHz 已足够。"
        keywords="采样率 44100 48000 24000 hz"
      >
        <SegmentedControl v-model="sampleRate" :options="sampleRateOptions" label="采样率" />
      </SettingRow>

      <SettingRow
        title="码率"
        desc="仅 MP3 使用；无损格式按原始数据保存，不做有损压缩。"
        keywords="码率 bitrate 128 192 256 320 kbps mp3"
      >
        <SegmentedControl v-model="bitrate" :options="bitrateOptions" label="码率" :block="false" />
      </SettingRow>

      <SettingRow
        title="默认音量"
        desc="试听与预览的起始音量；不影响导出音频本身的电平。"
        keywords="音量 试听 预览 电平 声音"
        wide
      >
        <RangeSlider v-model="store.audio.volume" :min="0" :max="100" label="默认音量" unit="%" />
        <ActionButton variant="soft" size="sm" :disabled="tonePlaying" @click="playTone">
          <Play :size="14" :stroke-width="1.8" />
          {{ tonePlaying ? '播放中' : '试听' }}
        </ActionButton>
      </SettingRow>

      <SettingRow
        title="响度归一化"
        desc="把不同引擎的输出拉到接近的响度，连续试听时不用反复调音量。"
        keywords="响度 归一化 音量 一致 loudness"
      >
        <ToggleSwitch v-model="store.audio.normalize" label="响度归一化" />
      </SettingRow>
    </SettingCard>

    <SettingCard title="播放行为" keywords="播放 自动 下一段 淡入淡出 连续试听">
      <template #icon><PlayCircle :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="自动播放下一段"
        desc="“全部试听”队列里一段播完自动接下一段。"
        keywords="自动 播放 下一段 队列 全部试听"
      >
        <ToggleSwitch v-model="store.audio.autoPlayNext" label="自动播放下一段" />
      </SettingRow>

      <SettingRow
        title="切换淡入淡出"
        desc="两段音频衔接时的过渡时长，避免爆音。"
        keywords="淡入 淡出 过渡 爆音 交叉"
        wide
      >
        <RangeSlider
          v-model="store.audio.crossfade"
          :min="0"
          :max="600"
          :step="20"
          label="切换淡入淡出"
          unit=" ms"
        />
      </SettingRow>
    </SettingCard>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { AudioLines, Play, PlayCircle } from 'lucide-vue-next'
import { say } from '../../store'
import { useSettingsStore } from '../../stores/settings'
import type { OutputFormat } from '../../stores/settings'
import type { SegmentOption } from '../types'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'
import ToggleSwitch from '../../components/ui/ToggleSwitch.vue'
import SegmentedControl from '../../components/ui/SegmentedControl.vue'
import RangeSlider from '../../components/ui/RangeSlider.vue'
import ActionButton from '../../components/ui/ActionButton.vue'

const store = useSettingsStore()

const formatOptions: SegmentOption[] = [
  { value: 'mp3', label: 'MP3' },
  { value: 'wav', label: 'WAV' },
  { value: 'flac', label: 'FLAC' },
]

const sampleRateOptions: SegmentOption[] = [
  { value: 24000, label: '24 kHz' },
  { value: 44100, label: '44.1 kHz' },
  { value: 48000, label: '48 kHz' },
]

const bitrateOptions = computed<SegmentOption[]>(() =>
  [128, 192, 256, 320].map((value) => ({
    value,
    label: `${value}`,
    disabled: store.audio.format !== 'mp3',
    title: store.audio.format !== 'mp3' ? '仅 MP3 使用码率' : `${value} kbps`,
  }))
)

const format = computed<OutputFormat>({
  get: () => store.audio.format,
  set: (value) => {
    store.audio.format = String(value) as OutputFormat
  },
})

const sampleRate = computed<number>({
  get: () => store.audio.sampleRate,
  set: (value) => {
    store.audio.sampleRate = Number(value)
  },
})

const bitrate = computed<number>({
  get: () => store.audio.bitrate,
  set: (value) => {
    store.audio.bitrate = Number(value)
  },
})

// ---- 试听：用一段短音把「默认音量」放出来，滑块才不是个摆设 ----
const tonePlaying = ref(false)
let audioContext: AudioContext | null = null
let toneTimer = 0

function playTone() {
  try {
    audioContext ||= new AudioContext()
    if (audioContext.state === 'suspended') void audioContext.resume()
    const oscillator = audioContext.createOscillator()
    const gain = audioContext.createGain()
    const now = audioContext.currentTime
    const peak = Math.max(0.0002, (store.audio.volume / 100) * 0.24)
    oscillator.type = 'sine'
    oscillator.frequency.setValueAtTime(660, now)
    gain.gain.setValueAtTime(0.0001, now)
    gain.gain.exponentialRampToValueAtTime(peak, now + 0.08)
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.9)
    oscillator.connect(gain)
    gain.connect(audioContext.destination)
    oscillator.start(now)
    oscillator.stop(now + 0.95)
    tonePlaying.value = true
    window.clearTimeout(toneTimer)
    toneTimer = window.setTimeout(() => {
      tonePlaying.value = false
    }, 950)
  } catch {
    say('浏览器阻止了音频播放，请先与页面交互一次')
  }
}

onBeforeUnmount(() => {
  window.clearTimeout(toneTimer)
  if (audioContext && audioContext.state !== 'closed') void audioContext.close()
})
</script>
