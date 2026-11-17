<!-- JSON Schema 单个字段的控件。网格表单与设置页行表共用这一份，
     否则分组版和行版总有一边的候选、留空规则先失修。 -->
<template>
  <!-- 选项由插件给，控件由宿主统一画。引擎页用下拉；设置页行表里的小集合
       跟同页其他设置项一样用胶囊分段，靠 `segmented` 由调用方决定。 -->
  <template v-if="field.widget === 'select'">
    <SegmentedControl
      v-if="segmented && field.choices.length <= 4"
      :id="fieldId"
      :model-value="values[field.key]"
      :options="segmentOptions"
      :label="field.label"
      @update:model-value="values[field.key] = $event"
    />
    <select
      v-else
      :id="fieldId"
      v-model="values[field.key]"
      class="sh-field"
      :aria-label="field.label"
      :disabled="disabled"
    >
      <option v-for="pair in field.choices" :key="String(pair[0])" :value="pair[0]">
        {{ pair[1] }}
      </option>
    </select>
  </template>

  <!-- 对话模型通道：候选来自 app/llm 下的插件；模型再跟着通道走。 -->
  <select
    v-else-if="field.widget === 'llm_provider'"
    :id="fieldId"
    class="sh-field"
    :aria-label="field.label"
    :disabled="disabled"
    :value="values[field.key]"
    @change="onChannelChange(field.key, $event)"
  >
    <option value="">默认通道</option>
    <option v-for="provider in providerList" :key="provider.key" :value="provider.key">
      {{ provider.label }}
    </option>
  </select>

  <select
    v-else-if="field.widget === 'llm_model'"
    :id="fieldId"
    class="sh-field"
    :aria-label="field.label"
    :disabled="disabled"
    :value="values[field.key]"
    @change="onValueChange(field.key, $event)"
  >
    <option value="">默认模型</option>
    <option v-for="model in modelList" :key="model.id" :value="model.id">
      {{ model.label || model.id }}
    </option>
  </select>

  <ToggleSwitch
    v-else-if="field.widget === 'switch'"
    :model-value="Boolean(values[field.key])"
    :label="field.label"
    :disabled="disabled"
    @update:model-value="values[field.key] = $event"
  />

  <textarea
    v-else-if="field.widget === 'textarea'"
    :id="fieldId"
    v-model="values[field.key]"
    class="sh-field is-textarea"
    :aria-label="field.label"
    :placeholder="field.placeholder"
    :disabled="disabled"
    rows="4"
  ></textarea>

  <input
    v-else-if="field.widget === 'number'"
    :id="fieldId"
    v-model.number="values[field.key]"
    class="sh-field"
    type="number"
    :min="field.min"
    :max="field.max"
    :step="field.step"
    :aria-label="field.label"
    :disabled="disabled"
  />

  <input
    v-else-if="field.widget === 'text' || field.widget === 'password'"
    :id="fieldId"
    class="sh-field"
    :value="values[field.key]"
    :type="field.widget === 'password' ? 'password' : 'text'"
    :placeholder="placeholderOf(field)"
    autocomplete="off"
    :aria-label="field.label"
    :disabled="disabled"
    @input="onValueChange(field.key, $event)"
  />

  <p v-if="field.widget === 'llm_model' && modelNote" class="field-note">{{ modelNote }}</p>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import SegmentedControl from './SegmentedControl.vue'
import ToggleSwitch from './ToggleSwitch.vue'

const props = defineProps({
  field: { type: Object, required: true },
  values: { type: Object, required: true },
  providers: { type: Array, default: () => [] },
  models: { type: Array, default: () => [] },
  modelNote: { type: String, default: '' },
  secretsSet: { type: Object, default: () => new Set() },
  cleared: { type: Array, default: () => [] },
  idPrefix: { type: String, default: 'vh-plugin' },
  disabled: { type: Boolean, default: false },
  /** 小集合候选用胶囊分段（设置页行表）；网格表单保持下拉。 */
  segmented: { type: Boolean, default: false },
})

const emit = defineEmits(['channel'])

const fieldId = computed(() => `${props.idPrefix}-${props.field.key}`)
// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const providerList = computed(() => /** @type {any[]} */ (props.providers))
const modelList = computed(() => /** @type {any[]} */ (props.models))
const segmentOptions = computed(() =>
  /** @type {Array<[any, any]>} */ (props.field.choices).map(([value, label]) => ({
    value,
    label: label || String(value),
    disabled: props.disabled,
  }))
)

/** @param {string} key @param {Event} event */
function onChannelChange(key, event) {
  emit('channel', key, /** @type {HTMLSelectElement} */ (event.target).value)
}

/** @param {string} key @param {Event} event */
function onValueChange(key, event) {
  props.values[key] = /** @type {HTMLInputElement} */ (event.target).value
}

/**
 * @param {any} field
 */
function placeholderOf(field) {
  if (!field.secret) return field.placeholder || ''
  if (props.cleared.includes(field.key)) return '保存后清空'
  return props.secretsSet.has(field.key) ? '留空则不修改' : '未填写'
}
</script>

<style scoped>
.sh-field.is-textarea {
  height: auto;
  min-height: 96px;
  padding: 9px 11px;
  line-height: 1.65;
  resize: vertical;
}
.field-note {
  margin: 2px 0 0;
  font-size: 11.2px;
  line-height: 1.55;
  color: var(--sh-text-4);
}
</style>
