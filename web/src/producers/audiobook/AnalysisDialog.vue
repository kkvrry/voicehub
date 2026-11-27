<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="$emit('close')">
        <div ref="panelEl" class="vh-modal analysis-dialog" role="dialog" aria-modal="true" aria-labelledby="vh-analysis-title">
          <header class="analysis-head">
            <div>
              <h2 id="vh-analysis-title" class="analysis-title">对话分析</h2>
              <p class="analysis-note">
                {{ dialogueRows }} 行对白；通道与模型取自「引擎 → 对话」。
              </p>
            </div>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="$emit('close')">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="analysis-body">
            <section class="analysis-group">
              <div class="analysis-group-title">模型</div>
              <div class="analysis-pair">
                <label class="analysis-field" for="analysis-channel">
                  通道
                  <select id="analysis-channel" v-model="channel" class="sh-field">
                    <option value="">默认通道</option>
                    <option v-for="item in providers" :key="item.key" :value="item.key">
                      {{ item.label }}
                    </option>
                  </select>
                </label>
                <label class="analysis-field" for="analysis-model">
                  模型
                  <select id="analysis-model" v-model="model" class="sh-field">
                    <option value="">默认模型</option>
                    <option v-for="item in models" :key="item.id" :value="item.id">
                      {{ item.label || item.id }}
                    </option>
                    <option v-if="model && !models.some(/** @param {any} item */ (item) => item.id === model)" :value="model">
                      {{ model }}
                    </option>
                  </select>
                </label>
              </div>
              <p v-if="modelNote" class="analysis-hint">{{ modelNote }}</p>
            </section>

            <section class="analysis-group">
              <div class="analysis-group-title">发送</div>
              <label class="analysis-field" for="analysis-scope">
                分析范围
                <select id="analysis-scope" v-model="scope" class="sh-field">
                  <option v-for="item in scopeOptions" :key="item.value" :value="item.value">
                    {{ item.label }}
                  </option>
                </select>
              </label>
              <p v-if="scope === 'pending'" class="analysis-hint">
                只送仍写着「对话」的行；已有说话人的保持原样。
              </p>
              <div class="analysis-pair">
                <label class="analysis-field" for="analysis-granularity">
                  发送粒度
                  <select
                    id="analysis-granularity"
                    v-model="granularity"
                    class="sh-field"
                  >
                    <option v-for="item in granularityOptions" :key="item.value" :value="item.value">
                      {{ item.label }}
                    </option>
                  </select>
                </label>
                <label class="analysis-field" for="analysis-concurrency">
                  并发请求
                  <input
                    id="analysis-concurrency"
                    v-model.number="concurrency"
                    class="sh-field"
                    type="number"
                    min="1"
                    max="16"
                  />
                </label>
              </div>

              <label
                v-if="granularity === 'batch'"
                class="analysis-field"
                for="analysis-batch"
              >
                每批对白行数
                <input
                  id="analysis-batch"
                  v-model.number="batch"
                  class="sh-field"
                  type="number"
                  min="10"
                  max="400"
                />
              </label>
              <p v-else class="analysis-hint">整章一次；单章超出回复上限时自动分段。</p>

              <div class="analysis-pair">
                <label class="analysis-field" for="analysis-context">
                  上下文范围
                  <select id="analysis-context" v-model="contextMode" class="sh-field">
                    <option v-for="item in contextOptions" :key="item.value" :value="item.value">
                      {{ item.label }}
                    </option>
                  </select>
                </label>
                <label
                  v-if="contextMode === 'window'"
                  class="analysis-field"
                  for="analysis-context-lines"
                >
                  上下文行数（每侧）
                  <input
                    id="analysis-context-lines"
                    v-model.number="contextLines"
                    class="sh-field"
                    type="number"
                    min="5"
                    max="400"
                  />
                </label>
              </div>
              <p v-if="contextMode === 'window'" class="analysis-hint">
                每批取对白前后各 {{ contextLines || 0 }} 行正文，随每次请求发送；模型不保留上一批内容。
              </p>
            </section>

            <section class="analysis-group">
              <div class="analysis-group-title">提示词</div>
              <div class="analysis-prompt-bar">
                <select
                  id="analysis-preset"
                  v-model="presetKey"
                  class="sh-field analysis-preset"
                  aria-label="提示词预设"
                  @change="applyPreset"
                >
                  <option v-for="preset in presets" :key="preset.key" :value="preset.key">
                    {{ preset.label }}
                  </option>
                  <option value="custom">自定义</option>
                </select>
                <span v-if="selectedPreset?.note" class="analysis-hint">
                  {{ selectedPreset.note }}
                </span>
              </div>
              <textarea
                id="analysis-prompt"
                v-model="prompt"
                class="sh-field analysis-prompt"
                rows="9"
                spellcheck="false"
                aria-label="分析提示词"
              />
              <p class="analysis-hint">
                预设来自「设置 → 词表 → 对话分析提示词」；在这里改正文只影响本项目。
                可用占位：{{ '{book}' }}、{{ '{text}' }}、{{ '{rows}' }}、{{ '{roles}' }}、{{ '{emotions}' }}。
              </p>
            </section>
          </div>

          <footer class="analysis-foot">
            <button class="sh-btn is-ghost" @click="$emit('close')">取消</button>
            <button class="sh-btn is-soft" :disabled="saving" @click="save(false)">只保存</button>
            <button class="sh-btn is-primary" :disabled="saving" @click="save(true)">
              {{ saving ? '保存中……' : '保存并分析' }}
            </button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import { api } from '../../api'
import { actions, say, store } from '../../store'
import { loadAssistStatus, state } from './store'
import { overlayTarget } from '../../composables/overlayTarget'
import { useModalA11y } from '../../composables/modalA11y'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['close', 'run'])
const panelEl = ref(/** @type {any} */ (null))
useModalA11y({ open: () => props.open, panel: panelEl, onClose: () => emit('close') })

const providers = ref(/** @type {any[]} */ ([]))
const models = ref(/** @type {any[]} */ ([]))
const modelNote = ref('')
const channel = ref('')
const model = ref('')
const prompt = ref('')
const batch = ref(100)
const scope = ref('all')
const granularity = ref('batch')
const concurrency = ref(2)
const contextMode = ref('chapter')
const contextLines = ref(40)
const presetKey = ref('custom')
const saving = ref(false)

const plugin = computed(() =>
  store.producers.find(/** @param {any} item */ (item) => item.key === store.producer.key)
)
const dialogueRows = computed(() => state.detail?.stats?.dialogue || 0)

/**
 * 可选值来自插件 schema：发送粒度 / 上下文范围的说法只有后端一份。
 *
 * @param {string} key
 */
function choicesOf(key) {
  const pairs = /** @type {any[]} */ (
    plugin.value?.settings_schema?.properties?.[key]?.choices || []
  )
  return pairs.map(
    /** @param {any[]} pair */
    ([value, label]) => ({ value, label })
  )
}
const scopeOptions = computed(() => choicesOf('analysis_scope'))
const granularityOptions = computed(() => choicesOf('analysis_granularity'))
const contextOptions = computed(() => choicesOf('analysis_context'))

/**
 * 默认值只有一处产地（后端 schema）；这里只兜底，不另写一套数字。
 *
 * @param {string} key
 * @param {any} fallback
 */
function defaultOf(key, fallback) {
  const value = plugin.value?.settings_schema?.properties?.[key]?.default
  return value === undefined || value === null || value === '' ? fallback : value
}

/** 提示词预设：正文与说法都由后端给（设置页改过的生效值），这里只负责切换。 */
const presets = computed(() => plugin.value?.analysis_presets || [])
const selectedPreset = computed(
  () => presets.value.find(/** @param {any} item */ (item) => item.key === presetKey.value) || null
)

/** 框里显示的就是这次会用的提示词：自定义优先，没填过就摊开内置那份。 */
function syncPreset() {
  const match = presets.value.find(/** @param {any} item */ (item) => item.text === prompt.value)
  presetKey.value = match ? match.key : 'custom'
}

function applyPreset() {
  const preset = presets.value.find(/** @param {any} item */ (item) => item.key === presetKey.value)
  if (preset) prompt.value = preset.text
}

watch(prompt, syncPreset)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    const values = plugin.value?.values || {}
    channel.value = values.analysis_channel || ''
    model.value = values.analysis_model || ''
    scope.value = values.analysis_scope || 'all'
    granularity.value = values.analysis_granularity || 'batch'
    batch.value = Number(values.analysis_batch) || Number(defaultOf('analysis_batch', 120))
    concurrency.value =
      Number(values.analysis_concurrency) || Number(defaultOf('analysis_concurrency', 4))
    contextMode.value = values.analysis_context || 'chapter'
    contextLines.value =
      Number(values.analysis_context_lines) || Number(defaultOf('analysis_context_lines', 80))
    const stored = String(values.analysis_prompt || '').trim()
    const storedKey = String(values.analysis_preset || '').trim()
    if (stored) {
      prompt.value = stored
    } else {
      // 没存过自定义正文：显示所选预设的生效正文（设置里改过就跟新的走）。
      const preset =
        presets.value.find(/** @param {any} item */ (item) => item.key === storedKey) ||
        presets.value.find(/** @param {any} item */ (item) => item.key === 'default') ||
        presets.value[0]
      prompt.value = preset?.text || ''
    }
    syncPreset()
    try {
      providers.value = await api.llmProviders()
    } catch {
      providers.value = []
    }
    await loadModels(channel.value)
  }
)

watch(channel, (value, previous) => {
  if (value === previous) return
  model.value = ''
  void loadModels(value)
})

/**
 * @param {string} key
 */
async function loadModels(key) {
  models.value = []
  modelNote.value = ''
  if (!key) return
  try {
    const result = await api.llmModels(key)
    models.value = result.items || []
    if (!result.ok) modelNote.value = result.reason || '拉不到模型清单，用通道的默认模型。'
  } catch (error) {
    modelNote.value = String(error instanceof Error ? error.message : String(error))
  }
  if (!models.value.length) {
    const provider = providers.value.find(/** @param {any} item */ (item) => item.key === key)
    const choices = provider?.settings_schema?.properties?.model?.choices || []
    models.value = /** @type {any[]} */ (choices).map(
      /** @param {any[]} pair */
      ([id, label]) => ({ id, label })
    )
  }
}

/**
 * @param {any} value
 * @param {any} min
 * @param {any} max
 * @param {any} fallback
 */
function clamp(value, min, max, fallback) {
  const number = Number(value)
  if (!Number.isFinite(number)) return fallback
  return Math.max(min, Math.min(max, Math.round(number)))
}

/**
 * @param {any} thenRun
 */
async function save(thenRun) {
  const current = plugin.value
  if (!current) return
  saving.value = true
  try {
    const text = prompt.value.trim()
    // 与某条预设一字不差时只存预设键：正文的唯一产地在设置页，项目不再抄一份。
    const match = presets.value.find(/** @param {any} item */ (item) => item.text.trim() === text)
    await actions.saveProducerSettings(current.key, {
      analysis_channel: channel.value,
      analysis_model: model.value,
      analysis_scope: scope.value,
      analysis_granularity: granularity.value,
      analysis_batch: clamp(batch.value, 10, 400, 120),
      analysis_concurrency: clamp(concurrency.value, 1, 16, 4),
      analysis_context: contextMode.value,
      analysis_context_lines: clamp(contextLines.value, 5, 400, 80),
      analysis_preset: match ? match.key : text ? 'custom' : '',
      analysis_prompt: match ? '' : text,
    })
    say('分析设置已保存')
    void loadAssistStatus(true)
    emit('close')
    if (thenRun) emit('run')
  } catch (error) {
    say(String(error instanceof Error ? error.message : String(error)), { kind: 'error' })
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.analysis-dialog {
  width: min(680px, 100%);
  max-height: min(90vh, 820px);
  padding: 18px 20px 16px;
  gap: 12px;
}
.analysis-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.analysis-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  color: var(--sh-text);
}
.analysis-note {
  margin: 3px 0 0;
  font-size: 11.8px;
  color: var(--sh-text-4);
}
.analysis-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-right: 2px;
}
.analysis-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.analysis-group-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--sh-text-4);
}
.analysis-pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 10px;
}
.analysis-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
.analysis-field .sh-field {
  width: 100%;
}
.analysis-hint {
  margin: 0;
  font-size: 11.2px;
  color: var(--sh-text-4);
}
.analysis-prompt-bar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.analysis-preset {
  flex: 0 0 180px;
}
.analysis-prompt-bar .analysis-hint {
  flex: 1;
  min-width: 0;
}
.analysis-prompt {
  resize: vertical;
  min-height: 150px;
  font-size: 12px;
  line-height: 1.6;
}
.analysis-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
