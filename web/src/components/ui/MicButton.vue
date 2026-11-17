<template>
  <!-- 麦克风：**点一下开始、再点一下结束**（不是按住说）。
       念一句话要停下来想一下，按住那种对讲机式的只适合回答"在吗"。
       录完按 `mode` 决定去哪：`text` 是语音输入（听完即删），`audio` 是参考音频
       （落盘并把路径交回去）。两者共用这一颗按钮——同一件事，不该有两份代码。
       `iconOnly` 只画图标：生成页把它嵌在正文框里，文字版留给需要说明的场合。 -->
  <button
    type="button"
    class="btn mini mic-btn"
    :class="{ 'is-recording': state === 'recording', 'is-icon': iconOnly }"
    :disabled="disabled || state === 'busy'"
    :title="hint"
    :aria-label="label"
    @click="toggle"
  >
    <template v-if="iconOnly">
      <Square v-if="state === 'recording'" :size="12" fill="currentColor" />
      <Loader v-else-if="state === 'busy'" :size="16" class="mic-spin" />
      <Mic v-else :size="16" />
    </template>
    <template v-else>
      <Square v-if="state === 'recording'" :size="12" fill="currentColor" />
      <Loader v-else-if="state === 'busy'" :size="14" class="mic-spin" />
      <Mic v-else :size="14" />
      <span>
        {{ state === 'recording' ? `停（${clock}）` : state === 'busy' ? '识别中' : label }}
      </span>
    </template>
  </button>
</template>

<script setup>
// @ts-check
import { computed, onBeforeUnmount, ref } from 'vue'
import { Loader, Mic, Square } from 'lucide-vue-next'
import { api } from '../../api'
import { formatClock } from '../../utils/format'

const props = defineProps({
  /** `text` = 语音输入（要一句话）；`audio` = 要一段音频（参考音频）。 */
  mode: { type: String, default: 'text' },
  label: { type: String, default: '语音输入' },
  disabled: { type: Boolean, default: false },
  /** 只画图标（嵌在正文框里那种），不占一行文字。 */
  iconOnly: { type: Boolean, default: false },
  /** 点不了时说为什么（"识别引擎未就绪"这类），别让人点了才看见没反应。 */
  disabledHint: { type: String, default: '' },
})
const emit = defineEmits(['text', 'audio', 'error'])

const state = ref('idle') // idle | recording | busy
const seconds = ref(0)
/** @type {MediaRecorder | null} */
let rec = null
/** @type {MediaStream | null} */
let stream = null
/** @type {ReturnType<typeof setInterval> | undefined} */
let timer

const clock = computed(() => formatClock(seconds.value))

const hint = computed(() => {
  if (props.disabled) return props.disabledHint || '现在用不了'
  if (state.value === 'recording') return '再点一下结束并开始识别'
  if (props.mode === 'audio') return '录一段，用它当这次的参考音频'
  return '念一句，识别成文字插到光标处'
})

async function toggle() {
  if (state.value === 'recording') await stop()
  else await start()
}

async function start() {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  } catch (error) {
    emit('error', micError(error))
    return
  }
  const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', ''].find(
    (type) => !type || (window.MediaRecorder?.isTypeSupported?.(type) ?? false)
  )
  try {
    rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
  } catch (error) {
    stopStream()
    emit('error', `这个浏览器录不了音：${error instanceof Error ? error.message : error}`)
    return
  }
  /** @type {Blob[]} */
  const chunks = []
  // 这一份的形状要在 `onstop` 里用，而那时 `rec` 已经被置空了（见下面 `stop`），
  // 所以记在这儿的局部变量上——闭包里的值不会跟着那个变量一起变没。
  const shape = mime || 'audio/webm'
  rec.ondataavailable = (event) => {
    if (event.data && event.data.size) chunks.push(event.data)
  }
  rec.start()
  state.value = 'recording'
  seconds.value = 0
  timer = setInterval(() => (seconds.value += 1), 1000)
  rec.onstop = async () => {
    clearInterval(timer)
    stopStream()
    const blob = new Blob(chunks, { type: shape })
    await send(blob)
  }
}

async function stop() {
  if (!rec) return
  // 真正的收尾在 `onstop` 里：要等最后一块数据到齐。
  // 先置空是防重复点：录的时候这一颗是"停"，不该连点两次。
  const current = rec
  rec = null
  current.stop()
}

function stopStream() {
  stream?.getTracks().forEach((track) => track.stop())
  stream = null
}

/**
 * 录完：转成 wav 再送上去。
 * 浏览器录的是 webm/opus，而识别服务吃的是 wav——**这台机器上没有 ffmpeg**，
 * 指望服务端转格式等于要求用户先去装一个。所以在这里转：解码 → 重采样到
 * 16 kHz 单声道 → 写成 PCM16 的 wav。
 *
 * @param {any} blob
 */
async function send(blob) {
  state.value = 'busy'
  try {
    const wav = await toWav(blob)
    if (props.mode === 'audio') {
      emit('audio', await api.saveRecording(wav))
    } else {
      const payload = await api.dictate(wav)
      const text = String(payload?.text || '').trim()
      if (text) emit('text', text)
      else emit('error', '没听出字，可能没出声或离得太远。')
    }
  } catch (error) {
    emit('error', error instanceof Error ? error.message : String(error))
  } finally {
    state.value = 'idle'
  }
}

/**
 * webm/opus → 16 kHz 单声道 PCM16 wav。
 *
 * @param {any} blob
 */
async function toWav(blob) {
  const ctx = new AudioContext()
  try {
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer())
    const rate = 16000
    const frames = Math.max(1, Math.ceil(decoded.duration * rate))
    const offline = new OfflineAudioContext(1, frames, rate)
    const source = offline.createBufferSource()
    source.buffer = decoded
    source.connect(offline.destination)
    source.start()
    const rendered = await offline.startRendering()
    const pcm = rendered.getChannelData(0)
    const bytes = new ArrayBuffer(44 + pcm.length * 2)
    const view = new DataView(bytes)
    /**
     * @param {number} at
     * @param {string} text
     */
    const writeText = (at, text) => {
      for (let i = 0; i < text.length; i += 1) view.setUint8(at + i, text.charCodeAt(i))
    }
    writeText(0, 'RIFF')
    view.setUint32(4, 36 + pcm.length * 2, true)
    writeText(8, 'WAVEfmt ')
    view.setUint32(16, 16, true)
    view.setUint16(20, 1, true) // PCM
    view.setUint16(22, 1, true) // 单声道
    view.setUint32(24, rate, true)
    view.setUint32(28, rate * 2, true)
    view.setUint16(32, 2, true)
    view.setUint16(34, 16, true)
    writeText(36, 'data')
    view.setUint32(40, pcm.length * 2, true)
    let at = 44
    for (const sample of pcm) {
      const clipped = Math.max(-1, Math.min(1, sample))
      view.setInt16(at, clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff, true)
      at += 2
    }
    return new File([bytes], '录音.wav', { type: 'audio/wav' })
  } finally {
    ctx.close()
  }
}

/**
 * @param {any} error
 */
function micError(error) {
  const name = error?.name || ''
  if (name === 'NotAllowedError') return '浏览器不给用麦克风：在地址栏里允许一次再来。'
  if (name === 'NotFoundError') return '没找到麦克风。'
  if (!navigator.mediaDevices) return '只有 https 或 localhost 才能用麦克风。'
  return `打不开麦克风：${error instanceof Error ? error.message : String(error)}`
}

onBeforeUnmount(() => {
  clearInterval(timer)
  stopStream()
})
</script>
