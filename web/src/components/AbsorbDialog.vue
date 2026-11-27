<template>
  <!-- 入库：把历史里的一段（或一批）音频登记为某条音色的参考音频。
       三条路在一个窗口里说完：回原音色、指定其他音色、新建音色并选归属文件夹。
       库里已有的音频不动——入库是复制一份进音色目录，历史这边照旧留着。 -->
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="close">
        <div
          ref="panelEl"
          class="vh-modal sh-dialog absorb"
          role="dialog"
          aria-modal="true"
          aria-labelledby="vh-absorb-title"
        >
          <header class="sh-dialog-head">
            <h2 id="vh-absorb-title" class="sh-dialog-title">入库到音色</h2>
            <span class="absorb-count">{{ rows.length }} 条</span>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="close">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="sh-dialog-body">
            <div class="absorb-preview">
              <Play :size="12" :stroke-width="1.8" aria-hidden="true" />
              <span class="absorb-text">{{ previewText }}</span>
              <span class="absorb-src">{{ SOURCE_LABELS[rows[0]?.source] || '' }}</span>
            </div>

            <div class="absorb-group">
              <div class="absorb-cap">目标</div>

              <label v-if="originVoice" class="absorb-opt" :class="{ 'is-on': mode === 'origin' }">
                <input v-model="mode" class="absorb-radio" type="radio" value="origin" />
                <span class="absorb-name">原音色 · {{ originVoice }}</span>
                <span class="absorb-tag">默认</span>
              </label>

              <label class="absorb-opt" :class="{ 'is-on': mode === 'voice' }">
                <input v-model="mode" class="absorb-radio" type="radio" value="voice" />
                <span class="absorb-name">指定其他音色</span>
                <span class="absorb-note absorb-picked">{{ chosenName }}</span>
              </label>
              <div v-if="mode === 'voice'" class="absorb-sub">
                <input
                  v-model="query"
                  class="sh-field"
                  type="text"
                  placeholder="搜索音色名"
                  aria-label="搜索音色名"
                />
                <div v-if="voiceHits.length" class="absorb-list">
                  <button
                    v-for="voice in voiceHits"
                    :key="voice.id"
                    type="button"
                    class="absorb-item"
                    :class="{ 'is-on': voiceId === voice.id }"
                    @click="voiceId = voice.id"
                  >
                    <span class="absorb-item-name">{{ voice.shown_name || voice.name }}</span>
                    <span v-if="voice.source !== 'local'" class="absorb-item-note">{{ voice.source }}</span>
                  </button>
                </div>
                <div v-else class="absorb-empty">没有匹配的音色。</div>
              </div>

              <label class="absorb-opt" :class="{ 'is-on': mode === 'new' }">
                <input v-model="mode" class="absorb-radio" type="radio" value="new" />
                <span class="absorb-name">新建音色</span>
                <span class="absorb-note">以这段音频作为它的第一条样例</span>
              </label>
              <div v-if="mode === 'new'" class="absorb-sub">
                <div class="absorb-grid">
                  <label class="absorb-field is-wide">
                    <span class="absorb-label">名称</span>
                    <input v-model="name" class="sh-field" type="text" maxlength="40" />
                  </label>
                  <label class="absorb-field">
                    <span class="absorb-label">性别</span>
                    <select v-model="gender" class="sh-field">
                      <option value="">不限</option>
                      <option v-for="pair in vocab.genders" :key="pair[0]" :value="pair[0]">
                        {{ pair[1] }}
                      </option>
                    </select>
                  </label>
                  <label class="absorb-field">
                    <span class="absorb-label">语言</span>
                    <select v-model="language" class="sh-field">
                      <option value="">不限</option>
                      <option v-for="pair in vocab.languages" :key="pair[0]" :value="pair[0]">
                        {{ pair[1] }}
                      </option>
                    </select>
                  </label>
                </div>
                <div class="absorb-grid is-two">
                  <label class="absorb-field">
                    <span class="absorb-label">归属文件夹</span>
                    <select v-model="folder" class="sh-field">
                      <option value="">音色库根目录</option>
                      <option v-for="item in folders" :key="item.value" :value="item.value">
                        {{ item.label }}
                      </option>
                    </select>
                  </label>
                  <label class="absorb-check">
                    <ToggleSwitch v-model="mainSample" label="设为主样例" />
                    <span>设为主样例</span>
                  </label>
                </div>
              </div>
            </div>

            <label class="absorb-field absorb-emo">
              <span class="absorb-label">情绪</span>
              <select v-model="emotion" class="sh-field">
                <option v-for="pair in emotionOptions" :key="pair[0]" :value="pair[0]">
                  {{ pair[1] }}
                </option>
                <option v-if="emotionExtra" :value="emotion">{{ emotion }}</option>
              </select>
              <span class="absorb-help">随音频写入样例，不是音色的属性。</span>
            </label>

            <label v-if="transcriptFor !== null" class="absorb-field">
              <span class="absorb-label">逐字稿</span>
              <textarea v-model="transcript" class="sh-field absorb-textarea" rows="2"></textarea>
              <span v-if="rows.length > 1" class="absorb-help">多条音频共用这一份，按第一条填入。</span>
            </label>
          </div>

          <footer class="sh-dialog-foot">
            <span class="absorb-hint">入库后复制到音色目录，历史保留原文件。</span>
            <span class="spacer"></span>
            <ActionButton @click="close">取消</ActionButton>
            <ActionButton variant="primary" :disabled="!canConfirm" @click="confirm">入库</ActionButton>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, ref, watch } from 'vue'
import { Play, X } from 'lucide-vue-next'
import { overlayTarget } from '../composables/overlayTarget'
import { useModalA11y } from '../composables/modalA11y'
import { store } from '../store'
import { SOURCES } from '../store/audio'
import ActionButton from './ui/ActionButton.vue'
import ToggleSwitch from './ui/ToggleSwitch.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  /** 要入库的行 id；顺序即列表顺序。 */
  ids: { type: Array, default: () => [] },
})
const emit = defineEmits(['close', 'confirm'])
const panelEl = ref(/** @type {any} */ (null))
useModalA11y({ open: () => props.open, panel: panelEl, onClose: () => close() })

const SOURCE_LABELS = Object.fromEntries(SOURCES.map((item) => [item.key, item.label]))

const rows = computed(() => store.audio.items.filter((item) => props.ids.includes(item.id)))
const vocab = computed(() => store.tree.vocab)

const mode = ref('voice')
const query = ref('')
const voiceId = ref('')
const name = ref('')
const gender = ref('')
const language = ref('')
const folder = ref('')
const mainSample = ref(false)
const transcript = ref('')
const emotion = ref('')

/** 原音色只有一种情况成立：这一批全是生成物、同源，且那条音色现在还在库里。
 *  音色已删（平台清单换过、目录搬过）时不提这一项——提了也只会入到一条不存在的音色。 */
const originVoice = computed(() => {
  const list = rows.value
  if (!list.length) return ''
  const id = list[0].voice_id
  if (!id || !list.every((row) => row.voice_id === id)) return ''
  const voice = store.voices.find((item) => item.id === id)
  return voice ? voice.shown_name || voice.name : ''
})

const previewText = computed(() => {
  if (!rows.value.length) return ''
  if (rows.value.length > 1) return `${rows.value.length} 条音频`
  const row = rows.value[0]
  return row.text || row.file
})

/** 逐字稿只在「这一批文本一致」时出现；不一致就不替用户编一份。 */
const transcriptFor = computed(() => {
  const list = rows.value
  if (!list.length) return null
  const text = list[0].text
  return list.every((row) => row.text === text) ? text : null
})

/** 情绪候选与生成页同一份（规范里的 emotion 那一格），不另写一套词表。 */
const emotionOptions = computed(
  () => store.spec.fields.find((field) => field.key === 'emotion')?.options || []
)

/** 规范里没有这个词（自定义情绪）时补一项：带入的值不能在下拉里消失。 */
const emotionExtra = computed(
  () =>
    Boolean(emotion.value) &&
    !(/** @type {Array<[string, string]>} */ (emotionOptions.value)).some(
      (pair) => pair[0] === emotion.value
    )
)

/** 这一批情绪一致才带入，不一致就留空让用户选——不替用户猜。 */
const sharedEmotion = computed(() => {
  const list = rows.value
  if (!list.length) return ''
  return list.every((row) => row.emotion === list[0].emotion) ? list[0].emotion : ''
})

const voiceHits = computed(() => {
  const key = query.value.trim().toLowerCase()
  return store.voices
    .filter((voice) => !key || `${voice.shown_name || voice.name} ${voice.source}`.toLowerCase().includes(key))
    .slice(0, 8)
})

/** 归属文件夹：扫描根与它的一级子目录。引擎目录不在候选里——那是平台音色的家。 */
const folders = computed(() => {
  /** @type {{ label: string, value: string }[]} */
  const out = []
  for (const root of store.tree.roots || []) {
    if (root.engine) continue
    const rootName = shortName(root.path)
    for (const child of root.children || []) {
      out.push({ label: `${rootName} / ${child.name}`, value: `${root.path}\\${child.path}` })
    }
  }
  return out
})

const chosenName = computed(() => {
  const voice = store.voices.find((item) => item.id === voiceId.value)
  return voice ? voice.shown_name || voice.name : ''
})

const canConfirm = computed(() => {
  if (!rows.value.length) return false
  if (mode.value === 'origin') return Boolean(originVoice.value)
  if (mode.value === 'voice') return Boolean(voiceId.value)
  return Boolean(name.value.trim())
})

/** @param {string} path */
function shortName(path) {
  const parts = String(path || '').replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || path
}

/** 每次打开都回到默认：原音色在就选它，否则让用户挑一条。 */
watch(
  () => props.open,
  (open) => {
    if (!open) return
    mode.value = originVoice.value ? 'origin' : 'voice'
    query.value = ''
    voiceId.value = ''
    const source = rows.value.find((row) => row.voice_name)
    name.value = source ? `${source.voice_name} · 朗读` : ''
    gender.value = ''
    language.value = ''
    folder.value = ''
    mainSample.value = false
    transcript.value = transcriptFor.value || ''
    emotion.value = sharedEmotion.value
  }
)

function close() {
  emit('close')
}

function confirm() {
  if (!canConfirm.value) return
  emit('confirm', {
    mode: mode.value,
    voiceId: voiceId.value,
    voiceName:
      mode.value === 'voice'
        ? store.voices.find((voice) => voice.id === voiceId.value)?.shown_name || ''
        : name.value.trim(),
    folder: folder.value,
    gender: gender.value,
    language: language.value,
    transcript: transcript.value,
    emotion: emotion.value,
    mainSample: mainSample.value,
  })
}
</script>

<style scoped>
.absorb {
  width: min(520px, 100%);
}
.absorb-count {
  font-size: 12px;
  color: var(--sh-text-3);
}
.sh-dialog-head .sh-btn.is-icon {
  margin-left: auto;
}
.absorb-preview {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 11px;
  border-radius: var(--sh-r-sm);
  background: var(--sh-chip);
  color: var(--sh-text-3);
}
.absorb-text {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--sh-text-2);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.absorb-src {
  font-size: 11px;
  color: var(--sh-text-3);
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-2xs);
  padding: 1px 6px;
}
.absorb-group {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.absorb-cap {
  font-size: 12px;
  color: var(--sh-text-3);
}
.absorb-opt {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 12px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  cursor: pointer;
}
.absorb-opt.is-on {
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.absorb-radio {
  margin: 0;
  accent-color: var(--sh-accent);
}
.absorb-name {
  font-size: 13px;
  color: var(--sh-text);
}
.absorb-note {
  font-size: 11px;
  color: var(--sh-text-3);
}
.absorb-picked {
  margin-left: auto;
  color: var(--sh-text-2);
}
.absorb-tag {
  margin-left: auto;
  font-size: 11px;
  color: var(--sh-text-3);
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-2xs);
  padding: 1px 5px;
}
.absorb-sub {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0 2px 4px;
}
.absorb-list {
  display: flex;
  flex-direction: column;
  max-height: 168px;
  overflow-y: auto;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-sm);
}
.absorb-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: 0;
  background: transparent;
  color: var(--sh-text);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.absorb-item:hover {
  background: var(--sh-hover);
}
.absorb-item.is-on {
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}
.absorb-item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.absorb-item-note {
  font-size: 11px;
  color: var(--sh-text-3);
}
.absorb-empty {
  font-size: 12px;
  color: var(--sh-text-3);
  padding: 8px 2px;
}
.absorb-grid {
  display: grid;
  grid-template-columns: 1fr 92px 108px;
  gap: 8px;
}
.absorb-grid.is-two {
  grid-template-columns: 1fr auto;
  align-items: end;
}
.absorb-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.absorb-field.is-wide {
  grid-column: auto;
}
.absorb-emo {
  margin-top: 12px;
  max-width: 200px;
}
.absorb-label {
  font-size: 11px;
  color: var(--sh-text-3);
}
.absorb-check {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 32px;
  font-size: 12px;
  color: var(--sh-text-2);
  cursor: pointer;
}
.absorb-textarea {
  resize: vertical;
  line-height: 1.5;
  padding-top: 7px;
  padding-bottom: 7px;
}
.absorb-help {
  font-size: 11px;
  color: var(--sh-text-4);
}
.absorb-hint {
  font-size: 11px;
  color: var(--sh-text-3);
}
.spacer {
  flex: 1;
}
</style>
