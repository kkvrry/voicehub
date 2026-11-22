<template>
  <Popover :open="open" :anchor="anchor" :width="470" label="配音参数" @close="$emit('close')">
    <div class="dset">
      <header class="dset-head">
        <div>
          <h3 class="dset-title">配音参数</h3>
          <p class="dset-note">切分、贴齐与导出的默认值；对所有配音项目生效。</p>
        </div>
        <button type="button" class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="$emit('close')">
          <X :size="14" :stroke-width="1.8" />
        </button>
      </header>

      <div class="dset-body">
        <SchemaFields
          v-if="plugin"
          layout="rows"
          bare
          :schema="plugin.settings_schema"
          :values="values"
          :include-groups="groups"
          id-prefix="dub-workspace"
          keywords="配音"
        />
        <p v-else class="dset-note">插件清单还没取到：稍后重开这一格。</p>
      </div>

      <footer class="dset-foot">
        <button type="button" class="sh-btn is-sm is-ghost" :disabled="saving || !dirty" @click="doReset">
          恢复默认
        </button>
        <span class="dset-dirty">{{ dirty ? '有未保存的改动' : '' }}</span>
        <button type="button" class="sh-btn is-sm is-ghost" @click="$emit('close')">关闭</button>
        <button type="button" class="sh-btn is-sm is-primary" :disabled="saving || !dirty" @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </footer>
    </div>
  </Popover>
</template>

<script setup>
// @ts-check
// 工作区里的参数格：生产方设置里「设置页只画目录」的那几组（切分 / 贴齐 / 导出）
// 在这里编辑。表单仍由宿主的 SchemaFields 按 schema 画，插件不另写一套控件。
import { computed, reactive, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import Popover from '../../../components/ui/Popover.vue'
import SchemaFields from '../../../components/ui/SchemaFields.vue'
import { actions, store } from '../../../store'
import { refresh } from '../store'

const props = defineProps({
  open: { type: Boolean, default: false },
  anchor: { type: Object, default: null },
})
defineEmits(['close'])

const values = reactive(/** @type {Record<string, any>} */ ({}))
const saving = ref(false)

const plugin = computed(
  () => (store.producers || []).find((item) => item.key === 'dubbing') || null
)

/** 与 `settings_page_groups` 互补：目录那一组留在设置页，其余参数在使用现场。
 *  分组由后端 schema 声明，插件新增一组时这里自动出现，不再手抄组名。 */
const groups = computed(() => {
  const properties = /** @type {Record<string, any>} */ (
    plugin.value?.settings_schema?.properties || {}
  )
  const page = new Set(/** @type {string[]} */ (plugin.value?.settings_page_groups || []))
  const picked = /** @type {string[]} */ ([])
  for (const spec of Object.values(properties)) {
    const group = String(spec?.group || '')
    if (group && !page.has(group) && !picked.includes(group)) picked.push(group)
  }
  return picked
})

const keys = computed(() => {
  const properties = plugin.value?.settings_schema?.properties || {}
  const wanted = new Set(groups.value)
  return Object.entries(properties)
    .filter(([, spec]) => wanted.has(/** @type {any} */ (spec).group || ''))
    .map(([key]) => key)
})

const dirty = computed(() => {
  const saved = plugin.value?.values || {}
  return keys.value.some((key) => values[key] !== saved[key])
})

/** @param {any} source */
function resetFrom(source) {
  for (const key of Object.keys(values)) delete values[key]
  Object.assign(values, source?.values || {})
}

watch(() => plugin.value?.values, () => resetFrom(plugin.value), { deep: true })
watch(
  () => props.open,
  (open) => {
    if (open) resetFrom(plugin.value)
  },
  { immediate: true }
)

async function save() {
  if (!plugin.value) return
  saving.value = true
  try {
    const payload = /** @type {Record<string, any>} */ ({})
    for (const key of keys.value) payload[key] = values[key]
    const saved = await actions.saveProducerSettings('dubbing', payload)
    resetFrom(saved)
    await refresh({ silent: true })
  } finally {
    saving.value = false
  }
}

async function doReset() {
  if (!plugin.value) return
  const properties = /** @type {Record<string, any>} */ (
    plugin.value.settings_schema?.properties || {}
  )
  const payload = /** @type {Record<string, any>} */ ({})
  for (const key of keys.value) {
    if ('default' in (properties[key] || {})) payload[key] = properties[key].default
  }
  if (!Object.keys(payload).length) return
  saving.value = true
  try {
    const saved = await actions.saveProducerSettings('dubbing', payload)
    resetFrom(saved)
    await refresh({ silent: true })
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.dset {
  display: flex;
  flex-direction: column;
  max-height: min(72vh, 620px);
}

.dset-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--sh-line-soft);
}

.dset-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--sh-text);
}

.dset-note {
  margin: 3px 0 0;
  font-size: 10.5px;
  line-height: 1.5;
  color: var(--sh-text-4);
}

.dset-body {
  flex: 1;
  min-height: 0;
  margin-top: 6px;
  overflow-y: auto;
  padding-right: 2px;
}

.dset-foot {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--sh-line-soft);
}

.dset-dirty {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--sh-text-4);
}
</style>
