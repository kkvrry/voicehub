<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="$emit('close')">
        <div class="vh-modal merge-dialog" role="dialog" aria-modal="true" :aria-label="loudnessOnly ? '响度处理' : '合并音频'">
          <header class="merge-head">
            <div>
              <h2 class="merge-title">{{ loudnessOnly ? '响度处理' : '合并音频' }}</h2>
              <p class="merge-note">{{ note }}</p>
            </div>
            <button class="sh-btn is-icon" title="关闭" @click="$emit('close')">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="merge-body">
            <section v-if="!loudnessOnly" class="merge-group">
              <div class="merge-group-title">成品</div>
              <div class="merge-pair">
                <label class="merge-field" for="merge-format">
                  格式
                  <select id="merge-format" v-model="form.merge_format" class="sh-field">
                    <option v-for="item in choices('merge_format')" :key="item.value" :value="item.value">
                      {{ item.label }}
                    </option>
                  </select>
                </label>
                <label class="merge-field" for="merge-bitrate">
                  码率
                  <select
                    id="merge-bitrate"
                    v-model="form.merge_bitrate"
                    class="sh-field"
                    :disabled="form.merge_format === 'wav'"
                  >
                    <option v-for="item in choices('merge_bitrate')" :key="item.value" :value="item.value">
                      {{ item.label }}
                    </option>
                  </select>
                </label>
              </div>
              <div class="merge-triple">
                <label class="merge-field" for="merge-gap">
                  行间静音（毫秒）
                  <input
                    id="merge-gap"
                    v-model.number="form.row_gap_ms"
                    class="sh-field"
                    type="number"
                    :min="schema.row_gap_ms?.minimum"
                    :max="schema.row_gap_ms?.maximum"
                  />
                </label>
                <label class="merge-field" for="merge-lead">
                  章节前静音（毫秒）
                  <input
                    id="merge-lead"
                    v-model.number="form.chapter_lead_ms"
                    class="sh-field"
                    type="number"
                    :min="schema.chapter_lead_ms?.minimum"
                    :max="schema.chapter_lead_ms?.maximum"
                  />
                </label>
                <label class="merge-field" for="merge-tail">
                  章节后静音（毫秒）
                  <input
                    id="merge-tail"
                    v-model.number="form.chapter_tail_ms"
                    class="sh-field"
                    type="number"
                    :min="schema.chapter_tail_ms?.minimum"
                    :max="schema.chapter_tail_ms?.maximum"
                  />
                </label>
              </div>
            </section>

            <section v-if="schema.row_balance_enabled" class="merge-group">
              <div class="merge-group-title">{{ schema.row_balance_enabled.group }}</div>
              <p class="merge-hint">处理顺序：逐行生成 → 行级均衡 → 拼接 / BGM → 成品响度。</p>
              <div
                v-for="section in loudnessSections"
                :key="section.enabled"
                class="merge-loudness-section"
              >
                <div class="merge-toggle">
                  <span class="merge-toggle-main">
                    {{ schema[section.enabled].label }}
                    <span class="merge-hint">{{ schema[section.enabled].description }}</span>
                  </span>
                  <ToggleSwitch
                    v-model="form[section.enabled]"
                    :label="schema[section.enabled].label"
                  />
                </div>
                <div v-if="form[section.enabled]" class="merge-loudness-options">
                  <template v-for="field in section.fields" :key="field">
                    <label
                      v-if="field !== 'master_loudness_target' || form.master_loudness_preset === 'custom'"
                      class="merge-field"
                      :for="`merge-${field}`"
                    >
                      {{ schema[field].label }}
                      <select
                        v-if="schema[field].widget === 'select'"
                        :id="`merge-${field}`"
                        v-model="form[field]"
                        class="sh-field"
                        :aria-describedby="`merge-${field}-hint`"
                      >
                        <option v-for="item in choices(field)" :key="item.value" :value="item.value">
                          {{ item.label }}
                        </option>
                      </select>
                      <input
                        v-else-if="field !== 'master_true_peak_db' || form.master_loudness_preset !== 'acx'"
                        :id="`merge-${field}`"
                        v-model.number="form[field]"
                        class="sh-field"
                        type="number"
                        :min="schema[field].minimum"
                        :max="schema[field].maximum"
                        :step="schema[field].type === 'integer' ? 1 : 'any'"
                        :aria-describedby="`merge-${field}-hint`"
                      />
                      <input
                        v-else
                        :id="`merge-${field}`"
                        class="sh-field"
                        type="number"
                        :value="schema[field].default"
                        :aria-describedby="`merge-${field}-hint`"
                        disabled
                      />
                      <span :id="`merge-${field}-hint`" class="merge-hint">
                        {{ schema[field].description }}
                      </span>
                    </label>
                  </template>
                </div>
                <ActionButton
                  v-if="section.enabled === 'row_balance_enabled'"
                  variant="ghost"
                  size="sm"
                  :disabled="!form.row_balance_enabled || !audioCount || taskBusy || saving"
                  :title="processRowsHint"
                  @click="processRows"
                >
                  处理全书行音频
                </ActionButton>
              </div>
              <p class="merge-hint">保存后需重新处理行音频或合并；已有成品不会自动改变。</p>
            </section>

            <section v-if="!loudnessOnly" class="merge-group">
              <div class="merge-group-title">字幕</div>
              <div class="merge-switches">
                <div class="merge-toggle">
                  <span>生成 SRT 字幕</span>
                  <ToggleSwitch v-model="form.subtitle_srt" label="生成 SRT 字幕" />
                </div>
                <div class="merge-toggle">
                  <span>生成 LRC 字幕</span>
                  <ToggleSwitch v-model="form.subtitle_lrc" label="生成 LRC 字幕" />
                </div>
              </div>
              <div class="merge-toggle">
                <span class="merge-toggle-main">
                  变速贴齐时隙
                  <span class="merge-hint">仅字幕项目：某行长过原时隙时按需加速贴回，关掉则顺延。</span>
                </span>
                <ToggleSwitch v-model="form.subtitle_fit" label="变速贴齐时隙" />
              </div>
              <label class="merge-field merge-field--narrow" for="merge-fit-max">
                最大加速倍率
                <input
                  id="merge-fit-max"
                  v-model.number="form.subtitle_fit_max"
                  class="sh-field"
                  type="number"
                  :min="schema.subtitle_fit_max?.minimum"
                  :max="schema.subtitle_fit_max?.maximum"
                  step="0.05"
                  :disabled="!form.subtitle_fit"
                />
              </label>
            </section>

            <section v-if="!loudnessOnly" class="merge-group">
              <div class="merge-group-title">背景音乐</div>
              <div class="merge-toggle">
                <span class="merge-toggle-main">
                  叠加背景音乐
                  <span class="merge-hint">另出一版混入 BGM 的成品；原声版保留。</span>
                </span>
                <ToggleSwitch v-model="form.bgm_enabled" label="叠加背景音乐" />
              </div>
              <div class="merge-folder">
                <label class="merge-field" for="merge-bgm-folder">
                  BGM 文件夹
                  <input
                    id="merge-bgm-folder"
                    v-model="form.bgm_folder"
                    class="sh-field"
                    type="text"
                    placeholder="mp3 / wav / flac / m4a / ogg，随机抽曲"
                    spellcheck="false"
                  />
                </label>
                <ActionButton
                  variant="ghost"
                  size="sm"
                  :disabled="!form.bgm_folder?.trim()"
                  @click="openFolder"
                >
                  <FolderOpen :size="14" :stroke-width="1.8" />
                  打开
                </ActionButton>
              </div>
              <div class="merge-pair">
                <label class="merge-field" for="merge-bgm-gain">
                  BGM 音量（dB）
                  <input
                    id="merge-bgm-gain"
                    v-model.number="form.bgm_gain_db"
                    class="sh-field"
                    type="number"
                    :min="schema.bgm_gain_db?.minimum"
                    :max="schema.bgm_gain_db?.maximum"
                    step="1"
                  />
                </label>
                <div class="merge-toggle">
                  <span class="merge-toggle-main">
                    音量标准化
                    <span class="merge-hint">先拉到统一响度再混入。</span>
                  </span>
                  <ToggleSwitch v-model="form.bgm_normalize" label="音量标准化" />
                </div>
              </div>
              <p class="merge-hint">{{ fadeHint }}</p>
            </section>
          </div>

          <footer class="merge-foot">
            <ActionButton variant="ghost" size="sm" :disabled="!dirty || saving || taskBusy" @click="save()">
              <Save :size="14" :stroke-width="1.8" />
              保存
            </ActionButton>
            <span class="spacer"></span>
            <button
              v-if="!loudnessOnly"
              class="sh-btn is-ghost"
              :disabled="!chapter || !chapter.rows || taskBusy || saving"
              @click="run('chapter')"
            >
              <Combine :size="14" :stroke-width="1.8" />
              合并本章
            </button>
            <button
              v-if="!loudnessOnly"
              class="sh-btn is-ghost"
              :disabled="!audioCount || taskBusy || saving"
              :title="
                pendingCount
                  ? '按章节顺序合并还没合并的章节；已合并的跳过，只做剩下的'
                  : '已全部合并：按当前设置整本重做'
              "
              @click="run('all')"
            >
              <Layers :size="14" :stroke-width="1.8" />
              {{ pendingCount ? '合并全部章节' : '重新合并全部' }}
            </button>
            <button
              v-if="!loudnessOnly"
              class="sh-btn is-ghost"
              :disabled="!allMerged || taskBusy || saving"
              title="每章一个文件，打包成书名.zip"
              @click="run('export')"
            >
              <Download :size="14" :stroke-width="1.8" />
              导出 ZIP
            </button>
            <button
              v-if="!loudnessOnly"
              class="sh-btn is-ghost"
              :disabled="!allMerged || taskBusy || saving"
              title="整本一个文件，章节内嵌，手机播放器可直接跳章"
              @click="run('m4b')"
            >
              <FileAudio :size="14" :stroke-width="1.8" />
              导出 M4B
            </button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, reactive, ref, watch } from 'vue'
import { Combine, Download, FileAudio, FolderOpen, Layers, Save, X } from 'lucide-vue-next'
import ActionButton from '../../components/ui/ActionButton.vue'
import ToggleSwitch from '../../components/ui/ToggleSwitch.vue'
import { api } from '../../api'
import { actions, say, store } from '../../store'
import { confirmAction } from './confirm'
import {
  state,
  mergeAllChapters,
  mergeChapter,
  downloadChapters,
  downloadM4b,
  runningJob,
  runStep,
  selectChapter,
} from './store'
import { overlayTarget } from '../../composables/overlayTarget'

const props = defineProps({
  open: { type: Boolean, default: false },
  loudnessOnly: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

const detail = computed(() => state.detail)
const plugin = computed(() => store.producers.find(/** @param {any} item */ (item) => item.key === store.producer.key))
const schema = computed(
  /** @returns {Record<string, any>} */
  () => plugin.value?.settings_schema?.properties || {}
)
const chapter = computed(
  () => detail.value?.chapters.find(/** @param {any} item */ (item) => item.id === state.chapterId) || null
)
const allMerged = computed(
  () =>
    Boolean(detail.value?.chapters.length) &&
    detail.value.chapters.every(/** @param {any} item */ (item) => item.stage === 'merged' && item.audio)
)
/** 有逐行音频、且还没合并过的章节：批量合并只做这些。 */
const audioCount = computed(
  () => detail.value?.chapters.filter(/** @param {any} item */ (item) => Number(item.done) > 0).length || 0
)
const pendingCount = computed(
  () =>
    detail.value?.chapters.filter(
      /** @param {any} item */
      (item) => Number(item.done) > 0 && !(item.stage === 'merged' && item.audio)
    ).length || 0
)
const note = computed(() => {
  if (!detail.value) return ''
  const stats = detail.value.stats
  const rest = pendingCount.value ? ` · 待合并 ${pendingCount.value} 章` : ''
  return `${stats.merged}/${stats.chapters} 章已合并 · 本章 ${chapter.value?.rows || 0} 行${rest}`
})

/** 合并参数只在弹窗里改：候选、默认值与范围均取插件 schema。 */
const formKeys = [
  'merge_format',
  'merge_bitrate',
  'row_gap_ms',
  'chapter_lead_ms',
  'chapter_tail_ms',
  'subtitle_srt',
  'subtitle_lrc',
  'subtitle_fit',
  'subtitle_fit_max',
  'bgm_enabled',
  'bgm_folder',
  'bgm_gain_db',
  'bgm_normalize',
  'row_balance_enabled',
  'row_balance_mode',
  'row_balance_strength',
  'row_balance_max_gain_db',
  'master_loudness_enabled',
  'master_loudness_preset',
  'master_loudness_target',
  'master_true_peak_db',
]
const loudnessSections = [
  {
    enabled: 'row_balance_enabled',
    fields: ['row_balance_mode', 'row_balance_strength', 'row_balance_max_gain_db'],
  },
  {
    enabled: 'master_loudness_enabled',
    fields: ['master_loudness_preset', 'master_loudness_target', 'master_true_peak_db'],
  },
]
const form = reactive(/** @type {Record<string, any>} */ ({}))
const taskBusy = computed(() => Boolean(state.busy || runningJob() || state.importing))
const processRowsHint = computed(() => {
  if (!form.row_balance_enabled) return '需先开启行级均衡。'
  if (!audioCount.value) return '全书还没有可处理的行音频。'
  if (taskBusy.value || saving.value) return '等待当前任务结束。'
  return '保存当前设置后处理全书已有行音频；旧成品需重新合并。'
})

const saving = ref(false)
const saved = ref(/** @type {Record<string, any>} */ ({}))

/**
 * @param {string} key
 */
function choices(key) {
  const pairs = /** @type {any[]} */ (schema.value[key]?.choices || [])
  return pairs.map(([value, label]) => ({ value, label }))
}

/**
 * @param {any} values
 */
function syncFrom(values) {
  for (const key of formKeys) {
    const spec = schema.value[key]
    if (!spec) {
      delete form[key]
      continue
    }
    form[key] = values?.[key] ?? spec.default
  }
  saved.value = { ...form }
}

watch(
  [() => plugin.value?.values, schema],
  ([values]) => syncFrom(values),
  { immediate: true }
)

const dirty = computed(() => Object.keys(form).some(/** @param {any} key */ (key) => form[key] !== saved.value[key]))

/**
 * 数值按 schema 的上下限夹住：手输入越界不该让合并当场失败。
 *
 * @param {string} key
 */
function clamped(key) {
  const spec = schema.value[key] || {}
  const value = form[key] === '' || form[key] == null ? NaN : Number(form[key])
  const fallback = Number(spec.default)
  const min = Number.isFinite(spec.minimum) ? spec.minimum : -Infinity
  const max = Number.isFinite(spec.maximum) ? spec.maximum : Infinity
  const base = Number.isFinite(value) ? value : fallback
  return Math.min(max, Math.max(min, base))
}

function patchOf() {
  return Object.fromEntries(
    Object.keys(form).map(/** @param {any} key */ (key) => {
      const spec = schema.value[key]
      // ACX 控件固定为 schema 的保守峰值默认值，不沿用自定义预设的更高上限。
      if (key === 'master_true_peak_db' && form.master_loudness_preset === 'acx') {
        return [key, spec.default]
      }
      if (spec.type === 'integer') return [key, Math.round(clamped(key))]
      if (spec.type === 'number') return [key, clamped(key)]
      if (spec.type === 'boolean') return [key, Boolean(form[key])]
      const value = String(form[key] ?? spec.default).trim()
      const allowed = choices(key)
      return [key, allowed.length && !allowed.some(/** @param {any} item */ (item) => item.value === value) ? spec.default : value]
    })
  )
}

async function save() {
  if (!plugin.value || taskBusy.value || saving.value) return false
  const producerKey = plugin.value.key
  const projectId = detail.value?.id
  saving.value = true
  try {
    const payload = await actions.saveProducerSettings(producerKey, patchOf())
    if (plugin.value?.key !== producerKey) return false
    syncFrom(payload.values)
    if (detail.value?.id === projectId) await selectChapter(state.chapterId)
    say(props.loudnessOnly ? '响度设置已保存' : '合并设置已保存')
    return true
  } catch (error) {
    say(String(error instanceof Error ? error.message : String(error)), { kind: 'error' })
    return false
  } finally {
    saving.value = false
  }
}

async function processRows() {
  if (!form.row_balance_enabled || !audioCount.value || taskBusy.value || saving.value) return
  const projectId = detail.value?.id
  const producerKey = plugin.value?.key
  if (!(await save())) return
  // 保存期间可能切换项目或开始另一项任务，不能把处理请求发给新项目。
  if (detail.value?.id !== projectId || plugin.value?.key !== producerKey || taskBusy.value) return
  emit('close')
  await runStep('loudness')
}

/**
 * @param {any} kind
 */
async function run(kind) {
  if (taskBusy.value || saving.value) return
  const projectId = detail.value?.id
  const producerKey = plugin.value?.key
  const current = () =>
    detail.value?.id === projectId && plugin.value?.key === producerKey && !taskBusy.value
  const force = kind === 'all' && !pendingCount.value
  if (force) {
    const answer = await confirmAction({
      title: '重新合并全部',
      message: `${audioCount.value} 章已合并，会按当前设置整本重做。`,
      confirmText: '重新合并',
      danger: true,
    })
    if (!answer) return
  }
  if (!current()) return
  if (dirty.value && !(await save())) return
  if (!current()) return
  emit('close')
  if (kind === 'chapter') await mergeChapter()
  else if (kind === 'all') await mergeAllChapters({ force })
  else if (kind === 'm4b') await downloadM4b()
  else await downloadChapters()
}

async function openFolder() {
  if (!plugin.value) return
  try {
    await api.openProducerField(plugin.value.key, 'bgm_folder')
  } catch (error) {
    say(String(error instanceof Error ? error.message : String(error)))
  }
}

/** BGM 的淡入淡出跟着章节前后静音走：静音为 0 时后端按短过渡处理。 */
const fadeHint = computed(() => {
  if (!form.bgm_enabled) return '混入时长与人声一致；淡入淡出按章节前后静音。'
  const lead = Math.round(clamped('chapter_lead_ms')) / 1000
  const tail = Math.round(clamped('chapter_tail_ms')) / 1000
  if (!lead && !tail) return '章节前后没有静音：淡入淡出改用短过渡。'
  return `淡入 ${lead || 0} 秒、淡出 ${tail || 0} 秒，与章节前后静音同长。`
})
</script>

<style scoped>
.merge-dialog {
  width: min(600px, 100%);
  max-height: min(90vh, 780px);
  padding: 18px 20px 14px;
  gap: 12px;
}
.merge-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.merge-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  color: var(--sh-text);
}
.merge-note {
  margin: 3px 0 0;
  font-size: 11.8px;
  color: var(--sh-text-4);
}
.merge-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-right: 2px;
}
.merge-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 13px 13px;
  border: 1px solid var(--sh-line-soft);
  border-radius: 12px;
  background: var(--sh-glass-soft);
}
.merge-loudness-section,
.merge-loudness-options {
  display: flex;
  flex-direction: column;
  gap: var(--sh-gap-2);
}
.merge-loudness-section + .merge-loudness-section {
  padding-top: var(--sh-gap-2);
  border-top: 1px solid var(--sh-line-soft);
}
.merge-loudness-options {
  margin-left: var(--sh-gap-2);
  padding-left: var(--sh-gap-2);
  border-left: 1px solid var(--sh-line-soft);
}
.merge-loudness-section > .sh-btn {
  align-self: flex-start;
}
.merge-group-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--sh-text-4);
}
.merge-pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 10px;
}
.merge-triple {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px 10px;
}
.merge-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
.merge-field .sh-field {
  width: 100%;
}
.merge-field--narrow {
  width: 148px;
}
.merge-switches {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
}
.merge-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: 11.8px;
  color: var(--sh-text-2);
}
.merge-switches .merge-toggle {
  gap: 8px;
}
.merge-toggle-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.merge-folder {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}
.merge-folder .merge-field {
  flex: 1;
}
.merge-hint {
  margin: 0;
  font-size: 11.2px;
  line-height: 1.5;
  color: var(--sh-text-4);
}
.merge-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}
.merge-foot .spacer {
  flex: 1;
}
</style>
