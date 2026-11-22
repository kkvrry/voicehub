<template>
  <section class="wf-params" :aria-label="`${stepLabel}参数`">
    <p v-if="schema" class="wf-params-scope">全局默认值：对所有配音项目生效。</p>
    <div v-if="schema" class="wf-params-body">
      <SchemaFields
        layout="rows"
        bare
        :schema="schema"
        :values="values"
        :id-prefix="`dub-step-${stepKey}`"
        :keywords="`配音 ${stepLabel}`"
      />
    </div>
    <p v-else class="wf-params-note">插件清单还没取到，稍后重开这一格。</p>

    <footer v-if="keys.length" class="wf-params-foot">
      <button
        type="button"
        class="sh-btn is-sm is-ghost"
        :disabled="saving || !dirty"
        @click="doReset"
      >恢复默认</button>
      <span class="wf-params-dirty">{{ dirty ? '有未保存的改动' : '已保存' }}</span>
      <button
        type="button"
        class="sh-btn is-sm is-primary"
        :disabled="saving || !dirty"
        @click="save"
      >{{ saving ? '保存中…' : '保存' }}</button>
    </footer>

    <p v-if="error" class="wf-params-error" role="alert">{{ error }}</p>
  </section>
</template>

<script setup>
// @ts-check
// 一步的参数格：字段按后端 schema 上的 `step` 归属，表单仍由宿主的
// SchemaFields 按 schema 画。这里只负责本步那几格的初值、恢复默认与保存。
import { computed, reactive, ref, watch } from 'vue'
import SchemaFields from '../../../components/ui/SchemaFields.vue'
import { actions, store } from '../../../store'

const props = defineProps({
  stepKey: { type: String, required: true },
  stepLabel: { type: String, default: '' },
})

const emit = defineEmits(['saved'])

const values = reactive(/** @type {Record<string, any>} */ ({}))
const saving = ref(false)
const error = ref('')

const plugin = computed(
  () => (store.producers || []).find((item) => item.key === 'dubbing') || null
)

/** 本步的字段：顺序照 schema 声明，不另排。 */
const fields = computed(() =>
  Object.entries(plugin.value?.settings_schema?.properties || {}).filter(
    ([, spec]) => /** @type {any} */ (spec)?.step === props.stepKey
  )
)

const keys = computed(() => fields.value.map(([key]) => key))

/** 只留本步的字段，并去掉分组：参数格外面已经有步骤名，不再套第二层卡片。 */
const schema = computed(() => {
  if (!fields.value.length) return null
  /** @type {Record<string, any>} */
  const properties = {}
  for (const [key, spec] of fields.value) {
    const clean = { .../** @type {any} */ (spec) }
    delete clean.group
    properties[key] = clean
  }
  return { type: 'object', properties }
})

const dirty = computed(() => {
  const saved = plugin.value?.values || {}
  return keys.value.some((key) => values[key] !== saved[key])
})

/** @param {any} source */
function resetFrom(source) {
  for (const key of Object.keys(values)) delete values[key]
  const saved = source?.values || {}
  for (const key of keys.value) values[key] = saved[key]
}

watch(() => plugin.value?.values, () => {
  // 保存成功会换一份新值；用户手里的未保存改动不能被一次轮询冲掉。
  if (!dirty.value) resetFrom(plugin.value)
}, { deep: true })

watch(() => props.stepKey, () => {
  error.value = ''
  resetFrom(plugin.value)
}, { immediate: true })

// 插件清单比工作区晚到一步时，字段从「没有」变成「有几个」：
// 这一步必须补一次初值，否则 keys 有了、values 还是空的。
watch(() => keys.value.join('|'), () => resetFrom(plugin.value))

async function save() {
  if (!plugin.value) return
  saving.value = true
  error.value = ''
  try {
    const payload = /** @type {Record<string, any>} */ ({})
    for (const key of keys.value) payload[key] = values[key]
    const saved = await actions.saveProducerSettings('dubbing', payload)
    resetFrom(saved)
    emit('saved')
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    saving.value = false
  }
}

async function doReset() {
  const properties = /** @type {Record<string, any>} */ (
    plugin.value?.settings_schema?.properties || {}
  )
  const payload = /** @type {Record<string, any>} */ ({})
  for (const key of keys.value) {
    if ('default' in (properties[key] || {})) payload[key] = properties[key].default
  }
  if (!Object.keys(payload).length) return
  saving.value = true
  error.value = ''
  try {
    const saved = await actions.saveProducerSettings('dubbing', payload)
    resetFrom(saved)
    emit('saved')
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : String(failure)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.wf-params {
  margin-top: 8px;
  padding: 0 10px 8px;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-sm);
  background: var(--sh-field);
}

/* 右列只有两百多像素：说明与控件上下排，不挤成左右两栏。 */
.wf-params-body :deep(.sh-row) {
  grid-template-columns: minmax(0, 1fr);
  gap: 5px;
  padding: 8px 0;
}

.wf-params-body :deep(.sh-row-control) {
  justify-content: stretch;
}

.wf-params-body :deep(.sh-row-title) {
  font-size: 12px;
}

.wf-params-body :deep(.sh-row-desc) {
  margin-top: 2px;
  font-size: 10.5px;
  line-height: 1.5;
}

.wf-params-body :deep(.sh-field) {
  height: 30px;
  font-size: 12px;
}

.wf-params-scope {
  margin: 0;
  padding: 8px 0 0;
  font-size: 10px;
  color: var(--sh-text-4);
}

.wf-params-note {
  margin: 0;
  padding: 10px 0;
  font-size: 10.5px;
  color: var(--sh-text-4);
}

/* 参数格比右列高：保存键贴住滚动区底边，改完不必先滚到面板末尾。 */
.wf-params-foot {
  position: sticky;
  bottom: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 -10px;
  padding: 8px 10px;
  border-top: 1px solid var(--sh-line-soft);
  background: var(--sh-field);
}

.wf-params-dirty {
  margin-left: auto;
  font-size: 10px;
  color: var(--sh-text-4);
}

.wf-params-error {
  margin: 6px 0 0;
  font-size: 10.5px;
  color: var(--sh-danger);
}
</style>
