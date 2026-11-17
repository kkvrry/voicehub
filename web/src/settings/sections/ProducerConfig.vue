<template>
  <div class="producer-config">
    <!-- 表单由宿主渲染：分组卡、行表、控件列宽都在 SchemaFields 里。
         插件只声明 schema 与「设置页画哪几组」，宿主不写任何插件分支。 -->
    <SchemaFields
      layout="rows"
      :schema="plugin.settings_schema"
      :values="values"
      :secrets-set="secretsSet"
      :cleared="cleared"
      :id-prefix="`vh-${plugin.key}`"
      :keywords="plugin.label"
      :include-groups="pageGroups"
      bare
      @toggle-clear="toggleClear"
      @open-field="openField"
    >
      <template #group-icon="{ title }">
        <component :is="groupIcon(title)" :size="16" :stroke-width="1.8" />
      </template>
    </SchemaFields>

  </div>
</template>

<script setup>
// @ts-check
import { computed, reactive, ref, watch } from 'vue'
import {
  Captions,
  Combine,
  FolderOpen,
  MessageSquare,
  Mic,
  Music,
  SlidersHorizontal,
} from 'lucide-vue-next'
import { actions, say } from '../../store'
import { api } from '../../api'
import SchemaFields from '../../components/ui/SchemaFields.vue'

const props = defineProps({
  /** 后端自描述的那一包（含 settings_schema / values / secrets_set）。 */
  plugin: { type: Object, required: true },
})

const values = reactive(/** @type {Record<string, any>} */ ({}))
const cleared = ref(/** @type {string[]} */ ([]))
const saving = ref(false)
const secretsSet = ref(new Set())

/** 图标只是外观：分组名换了就退回通用图标，不影响表单。 */
const GROUP_ICONS = {
  项目: FolderOpen,
  对话分析: MessageSquare,
  逐行合成: Mic,
  成品合并: Combine,
  字幕: Captions,
  背景音乐: Music,
}

/** 画哪几组由插件声明；没声明就是整份 schema。 */
const pageGroups = computed(() => props.plugin?.settings_page_groups || [])

/** 本页可见的字段：恢复默认只复位这些。 */
const pageKeys = computed(() => {
  const properties = props.plugin?.settings_schema?.properties || {}
  const wanted = new Set(pageGroups.value)
  return Object.entries(properties)
    .filter(([, spec]) => !wanted.size || wanted.has(spec.group || ''))
    .map(([key]) => key)
})

/** 与已保存值逐项比对：字段全是标量，键集合对齐就够。 */
const dirty = computed(() => {
  if (cleared.value.length) return true
  const saved = props.plugin?.values || {}
  const keys = new Set([...Object.keys(values), ...Object.keys(saved)])
  for (const key of keys) {
    if (values[key] !== saved[key]) return true
  }
  return false
})

/**
 * @param {any} label
 */
function groupIcon(label) {
  return /** @type {Record<string, any>} */ (GROUP_ICONS)[label] || SlidersHorizontal
}

/**
 * @param {any} plugin
 */
function resetFrom(plugin) {
  for (const key of Object.keys(values)) delete values[key]
  Object.assign(values, plugin?.values || {})
  secretsSet.value = new Set(plugin?.secrets_set || [])
  cleared.value = []
}

watch(() => props.plugin.key, () => resetFrom(props.plugin), { immediate: true })
watch(() => props.plugin.values, () => resetFrom(props.plugin))

/**
 * @param {string} key
 */
function toggleClear(key) {
  cleared.value = cleared.value.includes(key)
    ? cleared.value.filter(/** @param {any} item */ (item) => item !== key)
    : [...cleared.value, key]
}

/**
 * 「打开」：字段声明 open 的（如项目目录），由后端在资源管理器里打开实际落点。
 *
 * @param {string} key
 */
async function openField(key) {
  try {
    await api.openProducerField(props.plugin.key, key)
  } catch (err) {
    say(String(err instanceof Error ? err.message : String(err)))
  }
}

async function save() {
  saving.value = true
  try {
    const saved = await actions.saveProducerSettings(props.plugin.key, {
      ...values,
      _clear: cleared.value,
    })
    resetFrom(saved)
  } finally {
    saving.value = false
  }
}

async function doReset() {
  saving.value = true
  try {
    const properties = /** @type {Record<string, any>} */ (
      props.plugin?.settings_schema?.properties || {}
    )
    const patch = /** @type {Record<string, any>} */ ({})
    for (const key of pageKeys.value) {
      const spec = /** @type {any} */ (properties[key] || {})
      if ('default' in spec) patch[key] = spec.default
    }
    if (!Object.keys(patch).length) return
    const saved = await actions.saveProducerSettings(props.plugin.key, patch)
    resetFrom(saved)
  } finally {
    saving.value = false
  }
}
/** 底部动作由 `PluginDialog` 统一画：这里只交状态与两个动作。 */
defineExpose({ dirty, saving, save, doReset })
</script>

<style scoped>
/* 只出现在弹窗里：表单直接铺在弹窗上，不再自带一层底与边框。 */
.producer-config {
  display: flex;
  flex-direction: column;
  gap: var(--sh-card-gap);
}
</style>
