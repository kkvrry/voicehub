<template>
  <!-- 参考音频的逐字稿。它跟上面那个下拉是同一件事的两半：选哪段音频、那段说了什么。
       要逐字稿的引擎（breeze 那类）缺了它会被中转站临时听一遍，所以摆在一起让人
       先看见，而不是另起一块「语音识别」让人自己找对应关系。 -->
  <div class="ref-text">
    <textarea
      v-model="text"
      class="field-textarea ref-text-area"
      :disabled="!sample"
      :placeholder="placeholder"
      aria-label="参考音频文本"
    ></textarea>
    <div class="ref-text-foot">
      <!-- 录音与样例走同一条识别路：录音也可能因为「录音后自动转写」关闭而没有文字，
           所以那颗按钮对两者都摆出来。 -->
      <button
        class="btn mini"
        :disabled="!sample || !ready || job.running"
        :title="ready ? '用识别引擎听一遍这段音频' : '先去「引擎 → 识别」配置识别引擎'"
        @click="recognize"
      >
        {{ job.running ? '识别中…' : hasText ? '重新识别' : '识别' }}
      </button>
      <button class="btn mini" :disabled="!dirty" @click="save">
        {{ isRecording ? '用这句' : '写入' }}
      </button>
      <span v-if="job.error" class="hint" data-tone="warn">{{ job.error }}</span>
      <span v-else-if="!ready" class="hint">识别引擎未就绪</span>
      <span v-else-if="job.language && job.file === sample?.file" class="hint">
        识别语种：{{ languageLabel
        }}<template v-if="job.languageWritten">（已写入音色）</template>
      </span>
    </div>
  </div>
</template>

<script setup>
// @ts-check
/** 只跟"这一次要用的那条参考音频"打交道：显示它的说话内容、可以重新听一遍、
 *  可以改完写回。名单来自 `store.synth.samples`，写入与识别都由后端说了算。 */
import { computed, ref, watch } from 'vue'
import { actions, labelOf, store, vocab } from '../store'

const job = computed(() => store.listen)
const ready = computed(() => Boolean(store.asr.ready))
const languageLabel = computed(
  () => labelOf(vocab.languages, job.value.languageCode) || job.value.language
)
const voiceId = computed(() => store.synth.voiceId)

/** 麦克风录的那一段（不是任何音色的样例，只在临时目录里）。 */
const recording = computed(() => store.synth.recording)
const isRecording = computed(() => Boolean(recording.value))

/** 这一次要用的那条：显式挑过就是它，否则是主样例——与后端补参考音频的口径一致。
 *  录的那段没有"样例"这回事，但它同样有逐字稿要填，所以按同一个形状摆出来。 */
const sample = computed(() => {
  if (recording.value) {
    return { file: recording.value.name, text: recording.value.text, path: recording.value.path }
  }
  const path = store.synth.values.reference_audio
  if (path) return store.synth.samples.find((item) => item.path === path) || null
  return store.synth.samples.find((item) => item.is_main) || null
})

const hasText = computed(() => Boolean(sample.value?.text))

const placeholder = computed(() => {
  if (!voiceId.value) return '先选音色'
  if (!sample.value) return '这条音色没有本地音频'
  if (isRecording.value) return '这段录音说了什么；可点「识别」自动填入，也可以直接改'
  return '这段音频说了什么；要逐字稿的引擎靠它'
})

const text = ref('')

/** 存着的与框里的不一样，才让「写入」可点：识别出来没改也算不一样。 */
const dirty = computed(() => text.value.trim() !== (sample.value?.text || '').trim())

/** 刚识别完的那一句优先（此时还没写入），否则显示样例已存的说话内容。 */
function sync() {
  const current = sample.value
  const heard = job.value.file && job.value.file === current?.file ? job.value.text : ''
  text.value = heard || current?.text || ''
}

watch(sample, sync, { immediate: true })
watch(() => [job.value.file, job.value.text], sync)

async function recognize() {
  if (!sample.value) return
  if (isRecording.value) {
    const heard = await actions.listenRecording(sample.value.file)
    if (heard) {
      store.synth.values.reference_text = String(heard).trim()
      if (recording.value) recording.value.text = String(heard).trim()
    }
    return
  }
  if (!voiceId.value) return
  await actions.listen(voiceId.value, sample.value.file)
}

async function save() {
  if (!sample.value || !voiceId.value) return
  // 录音没有"样例"可写回：它的逐字稿就是这一格的值，改完直接进请求体。
  if (isRecording.value) {
    store.synth.values.reference_text = text.value.trim()
    if (recording.value) recording.value.text = text.value.trim()
    return
  }
  await actions.saveHeard(voiceId.value, sample.value.file, text.value)
}
</script>
