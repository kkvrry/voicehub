<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="close">
        <div
          ref="panelEl"
          class="vh-modal params-card"
          role="dialog"
          aria-modal="true"
          aria-labelledby="params-title"
          tabindex="-1"
        >
          <header class="params-head">
            <span class="params-glyph" aria-hidden="true">
              <SlidersHorizontal :size="16" :stroke-width="1.8" />
            </span>
            <div class="params-title-main">
              <div class="params-title-row">
                <h2 id="params-title" class="params-title">行参数</h2>
                <span v-if="changedCount" class="params-changed">{{ changedCount }} 项已改</span>
              </div>
              <p class="params-note">{{ subtitle }}</p>
              <!-- 这一行念的是什么：行号只能定位，正文才能让人确认没点错行。 -->
              <p v-if="rowText" class="params-quote" :title="rowText">{{ rowText }}</p>
            </div>
            <button
              type="button"
              class="sh-btn is-icon"
              title="关闭"
              aria-label="关闭"
              @click="close"
            >
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="params-body" :aria-busy="loading">
            <p v-if="error" class="params-alert is-error">
              <AlertTriangle :size="14" :stroke-width="1.8" aria-hidden="true" />
              <span>{{ error }}</span>
            </p>

            <div v-if="loading" class="params-skeleton" role="status" aria-label="读取中">
              <span></span><span></span><span></span><span></span>
            </div>

            <template v-else>
              <p v-if="voiceMissing" class="params-alert is-warn">
                <AlertTriangle :size="14" :stroke-width="1.8" aria-hidden="true" />
                <span>
                  「{{ data.speaker }}」还没有分配音色：参数可以先存，生成前需在右侧分配。
                </span>
              </p>

              <section v-for="group in groupsWithFields" :key="group.key" class="params-group">
                <header class="params-group-head">
                  <h3 class="params-group-title">{{ group.label }}</h3>
                  <span
                    v-if="changedOf(group.key)"
                    class="params-group-dot"
                    :title="`这一组已改 ${changedOf(group.key)} 项`"
                    aria-hidden="true"
                  ></span>
                </header>
                <!-- 与生成页同一个组件：数值格是细滑块 + 读数 + 默认角标，
                     长文本格是「现成说法 + 输入框」，看着与生成页是同一套东西。 -->
                <ParamFields
                  :fields="supportedOf(group.key)"
                  :values="form"
                  :presets="presets"
                  @update="setField"
                />
              </section>

              <!-- 这台引擎没有落点的参数只占一行：不画输入框，也不藏起来，免得以为漏了 -->
              <section v-if="unsupportedFields.length" class="params-unsupported">
                <h3 class="params-group-title">本引擎不支持</h3>
                <div class="params-off-list">
                  <span
                    v-for="field in unsupportedFields"
                    :key="field.key"
                    class="params-off-chip"
                    :title="field.note || '本引擎无对应参数。'"
                  >
                    {{ field.label }}
                  </span>
                </div>
                <p class="params-off-note">这些项保存时不会随这一行发出去。</p>
              </section>
            </template>
          </div>

          <footer class="params-foot">
            <button
              type="button"
              class="sh-btn is-ghost params-reset"
              :disabled="saving || !changedCount"
              @click="reset"
            >
              <RotateCcw :size="13" :stroke-width="1.8" />
              全部恢复默认
            </button>
            <span class="params-spacer" />
            <button type="button" class="sh-btn is-ghost" :disabled="saving" @click="close">
              取消
            </button>
            <button
              type="button"
              class="sh-btn is-primary"
              :disabled="saving || loading"
              @click="submit"
            >
              {{ saving ? '保存中…' : '保存并生成' }}
            </button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { AlertTriangle, RotateCcw, SlidersHorizontal, X } from 'lucide-vue-next'
import ParamFields from '../../components/ParamFields.vue'
import { store } from '../../store'
import { ensureLexicon } from '../../store/lexicon'
import { loadRowParams, saveRowParams } from './store'
import { overlayTarget } from '../../composables/overlayTarget'
import { useModalA11y } from '../../composables/modalA11y'

const props = defineProps({
  open: { type: Boolean, default: false },
  row: { type: Object, default: null },
})
const emit = defineEmits(['close', 'saved'])

const panelEl = ref(/** @type {any} */ (null))
useModalA11y({ open: () => props.open, panel: panelEl, onClose: () => close() })
const data = reactive({
  groups: /** @type {any[]} */ ([]),
  fields: /** @type {any[]} */ ([]),
  speaker: '',
  engine: '',
  voice_missing: false,
})
const form = ref(/** @type {Record<string, any>} */ ({}))
const loading = ref(false)
const saving = ref(false)
const error = ref('')

const voiceMissing = computed(() => Boolean(data.voice_missing))
const engineLabel = computed(
  () => store.engines?.find(/** @param {any} item */ (item) => item.key === data.engine)?.label || data.engine
)
/** 引导指令的现成说法：用户词表的生效值（各引擎默认的并集 + 用户自定义，后端算好）。
 *  不按引擎分组——指令是自由文本，这台引擎不认的话，保存时那一项本就不支持、不发。 */
const instructionPresets = computed(() => store.lexicon.instructions?.effective || [])
/** ParamFields 要的是「按参数键分表」的预设，不是一整排。 */
const presets = computed(() => ({ instruction: instructionPresets.value }))
const subtitle = computed(() => {
  const bits = []
  if (props.row) bits.push(`第 ${String(props.row.ordinal + 1).padStart(2, '0')} 行`)
  if (data.speaker) bits.push(data.speaker)
  if (engineLabel.value) bits.push(engineLabel.value)
  return bits.join(' · ')
})
/** 行正文：换行与连续空白压成一句，显示用；完整句子留在 title 里。 */
const rowText = computed(() => String(props.row?.text || '').replace(/\s+/g, ' ').trim())

/**
 * @param {any} group
 */
function supportedOf(group) {
  return data.fields.filter((field) => field.group === group && field.supported)
}

/** 只画有可用项的分组：一整组都不支持时，没必要先摆一个小标题再摆空。 */
const groupsWithFields = computed(() =>
  data.groups.filter(/** @param {any} group */ (group) => supportedOf(group.key).length > 0)
)
const unsupportedFields = computed(() => data.fields.filter((field) => !field.supported))

/**
 * 空串 / 没填过 = 这一格不参与请求；角标与计数都按这一条。
 *
 * @param {any} field
 */
function hasValue(field) {
  const value = form.value[field.key]
  return value !== '' && value !== null && value !== undefined
}
const changedCount = computed(
  () => data.fields.filter((field) => field.supported && hasValue(field)).length
)
/**
 * @param {any} groupKey
 */
function changedOf(groupKey) {
  return data.fields.filter(
    (field) => field.supported && field.group === groupKey && hasValue(field)
  ).length
}

/**
 * @param {string} key
 * @param {any} value
 */
function setField(key, value) {
  form.value[key] = value
}

/** 表单 → 提交体：空串与 null 剔掉，未支持的项不发。 */
function payload() {
  const out = /** @type {Record<string, any>} */ ({})
  for (const field of data.fields) {
    if (!field.supported) continue
    const value = form.value[field.key]
    if (value === '' || value === null || value === undefined) continue
    out[field.key] = value
  }
  return out
}

/** 表单回填：这一行**已经存着**的参数（后端随每个 field 的 ``value`` 给）。
 *  已生成过的音频再打开时，看见的就是当时用的那套值，可以直接改；
 *  没存过的格留空——空着才是「引擎按自己的配置走」，占位符只说默认值，不替人填。 */
function fill() {
  const next = /** @type {Record<string, any>} */ ({})
  for (const field of data.fields) {
    const value = field.value
    next[field.key] = value === undefined || value === null ? '' : value
  }
  form.value = next
}

async function load() {
  if (!props.row) return
  loading.value = true
  error.value = ''
  try {
    const [payloadData] = await Promise.all([loadRowParams(props.row.id), ensureLexicon()])
    Object.assign(data, {
      groups: payloadData.groups || [],
      // 参数规范只给"这一格是什么"，候选表由调用方补：ParamFields 统一按数组读。
      fields: (payloadData.fields || []).map(
        /** @param {any} field */
        (field) => ({
        ...field,
        options: field.options || [],
        // 空着 = 引擎默认：长文本格要给一句占位说明，否则空白框看着像坏了。
          placeholder: field.widget === 'textarea' ? '留空 = 引擎默认' : '',
        })
      ),
      speaker: payloadData.speaker || '',
      engine: payloadData.engine || '',
      voice_missing: Boolean(payloadData.voice_missing),
    })
    fill()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (!open) return
    void load()
    // 焦点收进对话框：Esc 能立刻关，读屏也从头念一遍。
    void nextTick(() => panelEl.value?.focus())
  }
)

function close() {
  if (saving.value) return
  emit('close')
}

/** 只清「这一行真会发出去」的那些格；不支持的项本来就不在请求里。 */
function reset() {
  const cleared = /** @type {Record<string, any>} */ ({})
  for (const field of data.fields) {
    if (field.supported) cleared[field.key] = ''
  }
  form.value = { ...form.value, ...cleared }
}

async function submit() {
  if (!props.row) return
  saving.value = true
  error.value = ''
  try {
    await saveRowParams(props.row.id, payload())
    emit('saved', props.row.id)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped src="./RowParamsDialog.css"></style>
