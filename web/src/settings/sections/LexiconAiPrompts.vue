<template>
  <!-- 生产方声明的 AI 提示词：直接摊在卡片里编辑，不套弹窗。
       对话分析有多条预设，用下拉切换当前编辑的那一条；三组固定提示词各两段。
       保存后插件自描述一起刷新——分析弹窗下拉看到的就是这里改过的正文。 -->
  <SettingCard
    v-for="group in groups"
    :key="group.key"
    :title="cardTitle(group)"
    :keywords="keywordsOf(group)"
  >
    <template #icon>
      <component :is="iconOf(group.key)" :size="16" :stroke-width="1.8" />
    </template>

    <template v-if="group.kind === 'analysis'">
      <select
        class="sh-field"
        :value="adding ? NEW_KEY : selectedKey"
        aria-label="对话分析提示词预设"
        @change="onPresetChange"
      >
        <option v-for="item in group.items" :key="item.key" :value="item.key">
          {{ item.label }}
        </option>
        <option v-if="!group.items.length && !adding" value="" disabled>还没有预设</option>
        <option v-if="adding" :value="NEW_KEY">新预设（未保存）</option>
      </select>

      <TextField
        v-model="analysisDraft.label"
        class="ap-label"
        label="预设短名"
        placeholder="短名，如「只给念法」"
      />
      <textarea
        v-model="analysisDraft.text"
        class="sh-field ap-text"
        rows="8"
        aria-label="提示词正文"
        placeholder="整段提示词；可用占位 {book}、{text}、{rows}、{roles}、{emotions}"
      />

      <div v-if="deletedPresets.length" class="ap-deleted">
        <span>已删除：</span>
        <button
          v-for="item in deletedPresets"
          :key="item.key"
          type="button"
          class="sh-btn is-sm is-ghost"
          @click="restorePreset(item.key)"
        >
          恢复「{{ item.label }}」
        </button>
      </div>
    </template>

    <template v-else>
      <div v-for="item in group.items" :key="item.key" class="prompt-fixed">
        <div class="prompt-fixed-head">
          <span class="prompt-fixed-label">{{ item.label }}</span>
          <span v-if="item.required.length" class="prompt-fixed-meta">
            占位：{{ item.required.join('、') }}
          </span>
        </div>
        <textarea
          class="sh-field prompt-text"
          :value="fixedValue(group.key, item.key)"
          :rows="rowsOf(fixedValue(group.key, item.key), item.key === 'system' ? 2 : 6)"
          :aria-label="`${group.label} · ${item.label}`"
          @input="onFixedInput(group.key, item.key, $event)"
        />
      </div>
    </template>

    <template #note>
      <template v-if="group.kind === 'analysis'">
        下拉切换预设；保存后，所有选它的项目下一次分析立刻用新正文。可用占位：
        {book}、{text}、{rows}、{roles}、{emotions}。
      </template>
      <template v-else>{{ group.note }} 改完后这一类 AI 请求下一次就用新正文。</template>
    </template>

    <template #foot>
      <template v-if="group.kind === 'analysis'">
        <ActionButton variant="ghost" size="sm" @click="removePreset">
          {{ adding ? '取消添加' : '删除' }}
        </ActionButton>
        <ActionButton variant="ghost" size="sm" @click="startAdd">添加</ActionButton>
        <ActionButton
          variant="soft"
          size="sm"
          :loading="saving"
          :disabled="!canSaveAnalysis"
          @click="saveAnalysis"
        >
          保存
        </ActionButton>
      </template>
      <template v-else>
        <ActionButton variant="ghost" size="sm" @click="resetFixed(group)">恢复默认</ActionButton>
        <ActionButton variant="soft" size="sm" :loading="saving" @click="saveFixed(group)">
          保存
        </ActionButton>
      </template>
    </template>
  </SettingCard>

  <SettingCard
    v-if="producer && producer.enabled === false"
    title="AI 提示词"
    keywords="AI 提示词 分析 匹配 画像 正则"
  >
    <template #icon><Sparkles :size="16" :stroke-width="1.8" /></template>
    <p class="ap-hint">创作插件已停用：启用后可在这里编辑 AI 提示词。</p>
  </SettingCard>

  <SettingCard v-else-if="error" title="AI 提示词" keywords="AI 提示词 读取失败">
    <template #icon><Sparkles :size="16" :stroke-width="1.8" /></template>
    <template #foot>
      <ActionButton variant="ghost" size="sm" :loading="loading" @click="reload">重新读取</ActionButton>
    </template>
    <p class="ap-hint">{{ error }}</p>
  </SettingCard>
</template>

<script setup>
// @ts-check
/** 设置页「词表」里的 AI 提示词：生产方声明、这里编辑、各任务实际读同一份。
 *
 *  数据来源是 `/producers/{key}/prompts`（默认在仓库，用户改的只存覆盖）；
 *  分析预设保存后顺带刷新插件自描述，创作页的分析弹窗因此立刻换到新正文。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Code2, Sparkles, UserRound, Wand2 } from 'lucide-vue-next'
import { actions, say, store } from '../../store'
import { api } from '../../api'
import ActionButton from '../../components/ui/ActionButton.vue'
import SettingCard from '../ui/SettingCard.vue'
import TextField from '../../components/ui/TextField.vue'

const NEW_KEY = '__new__'

const KEYWORDS = {
  analysis: 'AI 提示词 对话分析 预设 说话人 情绪 引导指令 精简 保守',
  voice_assign: 'AI 提示词 匹配 音色 选配音 系统提示词 用户提示词 模板',
  voice_profile: 'AI 提示词 角色 画像 声音 描述 系统提示词 用户提示词 模板',
  regex_rule: 'AI 提示词 正则 文本 清洗 系统提示词 用户提示词 模板',
}

const ICONS = {
  analysis: Sparkles,
  voice_assign: Wand2,
  voice_profile: UserRound,
  regex_rule: Code2,
}

const producer = computed(() =>
  (store.producers || []).find(/** @param {any} item */ (item) => (item.prompt_groups || []).length)
)
const promptState = ref(/** @type {any} */ (null))
const loadedKey = ref('')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const selectedKey = ref('')
const adding = ref(false)
const analysisDraft = ref({ label: '', text: '' })
const fixedDrafts = ref(/** @type {Record<string, any>} */ ({}))

/** 分析一组 + 三组固定提示词，统一成卡片列表。 */
const groups = computed(() => {
  if (!promptState.value) return []
  const analysis = promptState.value.analysis || {}
  return [
    {
      key: 'analysis',
      kind: 'analysis',
      label: analysis.label || '对话分析',
      note: analysis.note || '',
      items: analysis.effective || [],
    },
    ...(promptState.value.fixed || []).map(/** @param {any} group */ (group) => ({
      key: group.key,
      kind: 'fixed',
      label: group.label,
      note: group.note,
      items: group.items || [],
    })),
  ]
})

const analysisItems = computed(() => groups.value.find(/** @param {any} group */ (group) => group.kind === 'analysis')?.items || [])
const currentAnalysis = computed(
  () => analysisItems.value.find(/** @param {any} item */ (item) => item.key === selectedKey.value) || null
)
const deletedPresets = computed(() => {
  const analysis = promptState.value?.analysis || {}
  const hidden = new Set(analysis.hidden || [])
  return (analysis.defaults || []).filter(/** @param {any} item */ (item) => hidden.has(item.key))
})
const canSaveAnalysis = computed(
  () => Boolean(analysisDraft.value.label.trim() && analysisDraft.value.text.trim())
)

/**
 * @param {string} key
 */
function iconOf(key) {
  return /** @type {Record<string, any>} */ (ICONS)[key] || Sparkles
}

/**
 * @param {any} group
 */
function cardTitle(group) {
  return `${group.label}提示词`
}

/**
 * @param {any} group
 */
function keywordsOf(group) {
  return /** @type {Record<string, string>} */ (KEYWORDS)[group.key] || 'AI 提示词'
}

/**
 * @param {any} text
 * @param {any} base
 */
function rowsOf(text, base) {
  const lines = String(text || '').split('\n').length
  return Math.min(20, Math.max(base, lines))
}

/**
 * @param {string} key
 */
function isDefaultKey(key) {
  return (promptState.value?.analysis?.defaults || []).some(/** @param {any} item */ (item) => item.key === key)
}

function loadAnalysisDraft() {
  const item = currentAnalysis.value
  analysisDraft.value = item
    ? { label: item.label, text: item.text }
    : { label: '', text: '' }
}

function syncAnalysisSelection() {
  const items = analysisItems.value
  if (!items.length) {
    selectedKey.value = ''
    if (!adding.value) analysisDraft.value = { label: '', text: '' }
    return
  }
  if (!items.some(/** @param {any} item */ (item) => item.key === selectedKey.value)) {
    selectedKey.value = items[0].key
  }
  loadAnalysisDraft()
}

/**
 * @param {any} value
 */
function pickPreset(value) {
  if (value === NEW_KEY) {
    startAdd()
    return
  }
  adding.value = false
  selectedKey.value = value
  loadAnalysisDraft()
}

function startAdd() {
  adding.value = true
  analysisDraft.value = { label: '', text: '' }
}

/**
 * @param {any} groupKey
 * @param {any} itemKey
 */
function fixedValue(groupKey, itemKey) {
  return fixedDrafts.value[groupKey]?.[itemKey] ?? ''
}

/**
 * @param {any} groupKey
 * @param {any} itemKey
 * @param {any} value
 */
function setFixed(groupKey, itemKey, value) {
  if (!fixedDrafts.value[groupKey]) fixedDrafts.value[groupKey] = {}
  fixedDrafts.value[groupKey][itemKey] = value
}

/** @param {Event} event */
function onPresetChange(event) {
  pickPreset(/** @type {HTMLSelectElement} */ (event.target).value)
}

/**
 * @param {string} groupKey
 * @param {string} itemKey
 * @param {Event} event
 */
function onFixedInput(groupKey, itemKey, event) {
  setFixed(groupKey, itemKey, /** @type {HTMLInputElement} */ (event.target).value)
}

/**
 * @param {any} onlyKey
 */
function syncFixedDrafts(onlyKey = '') {
  const next = /** @type {Record<string, any>} */ (onlyKey ? { ...fixedDrafts.value } : {})
  for (const group of promptState.value?.fixed || []) {
    if (onlyKey && group.key !== onlyKey) continue
    next[group.key] = Object.fromEntries(group.items.map(/** @param {any} item */ (item) => [item.key, item.text]))
  }
  fixedDrafts.value = next
}

async function reload() {
  const target = producer.value
  if (!target) return
  loading.value = true
  error.value = ''
  try {
    promptState.value = await api.producerPrompts(target.key)
    loadedKey.value = target.key
    syncFixedDrafts()
    syncAnalysisSelection()
  } catch (err) {
    error.value = String(err instanceof Error ? err.message : String(err))
  } finally {
    loading.value = false
  }
}

/**
 * @param {any} patch
 * @param {any} refreshPlugin
 */
async function persist(patch, refreshPlugin) {
  const target = producer.value
  if (!target || saving.value) return null
  saving.value = true
  try {
    promptState.value = await api.saveProducerPrompts(target.key, patch)
    if (refreshPlugin) await actions.loadProducers()
    say('AI 提示词已保存')
    return promptState.value
  } catch (err) {
    say(String(err instanceof Error ? err.message : String(err)), { kind: 'error' })
    return null
  } finally {
    saving.value = false
  }
}

async function saveAnalysis() {
  const label = analysisDraft.value.label.trim()
  const text = analysisDraft.value.text.trim()
  if (!label || !text) {
    say('预设短名和提示词正文都要填。', { kind: 'warn' })
    return
  }
  const currentKey = adding.value ? '' : selectedKey.value
  const clash = analysisItems.value.find(
    /** @param {any} item */
    (item) => item.key !== currentKey && item.label === label
  )
  if (clash) {
    say(`预设短名重复：「${label}」。`, { kind: 'warn' })
    return
  }
  const analysis = promptState.value?.analysis || {}
  const defaults = new Map((analysis.defaults || []).map(/** @param {any} item */ (item) => [item.key, item]))
  const hidden = [...(analysis.hidden || [])]
  const custom = (analysis.custom || []).map(/** @param {any} item */ (item) => ({ ...item }))
  if (adding.value) {
    custom.push({ key: '', label, text })
  } else {
    const at = custom.findIndex(/** @param {any} item */ (item) => item.key === currentKey)
    const original = defaults.get(currentKey)
    if (original && label === original.label && text === original.text) {
      // 与出厂一字不差就不存覆盖：以后仓库改默认，这一条能跟着走。
      if (at >= 0) custom.splice(at, 1)
    } else {
      const entry = { key: currentKey, label, text }
      if (at >= 0) custom[at] = entry
      else custom.push(entry)
    }
  }
  const saved = await persist({ analysis: { hidden, custom } }, true)
  if (!saved) return
  if (adding.value) {
    const item = (saved.analysis?.effective || []).find(/** @param {any} entry */ (entry) => entry.label === label)
    adding.value = false
    if (item) {
      selectedKey.value = item.key
      analysisDraft.value = { label: item.label, text: item.text }
      return
    }
  }
  syncAnalysisSelection()
}

async function removePreset() {
  if (adding.value) {
    adding.value = false
    syncAnalysisSelection()
    return
  }
  const item = currentAnalysis.value
  if (!item) return
  if (!window.confirm(`删除预设「${item.label}」？删掉的默认预设可以恢复。`)) return
  const analysis = promptState.value?.analysis || {}
  const hidden = [...(analysis.hidden || [])]
  if (isDefaultKey(item.key) && !hidden.includes(item.key)) hidden.push(item.key)
  const custom = (analysis.custom || []).filter(/** @param {any} entry */ (entry) => entry.key !== item.key)
  const saved = await persist({ analysis: { hidden, custom } }, true)
  if (saved) syncAnalysisSelection()
}

/**
 * @param {string} key
 */
async function restorePreset(key) {
  const analysis = promptState.value?.analysis || {}
  const hidden = (analysis.hidden || []).filter(/** @param {any} value */ (value) => value !== key)
  const saved = await persist({ analysis: { hidden, custom: analysis.custom || [] } }, true)
  if (!saved) return
  adding.value = false
  selectedKey.value = key
  loadAnalysisDraft()
}

/**
 * @param {any} group
 */
async function saveFixed(group) {
  const patch = /** @type {Record<string, any>} */ ({})
  for (const item of group.items) {
    const text = fixedValue(group.key, item.key).trim()
    if (!text) {
      say(`「${group.label} · ${item.label}」不能为空。`, { kind: 'warn' })
      return
    }
    patch[item.key] = text
  }
  const saved = await persist({ fixed: { [group.key]: patch } }, false)
  if (saved) syncFixedDrafts(group.key)
}

/**
 * @param {any} group
 */
async function resetFixed(group) {
  if (!window.confirm(`恢复「${group.label}」的出厂提示词？现在改的会丢。`)) return
  // 空组 = 删掉这一组的覆盖：仓库以后改默认能跟着走。
  const saved = await persist({ fixed: { [group.key]: {} } }, false)
  if (saved) syncFixedDrafts(group.key)
}

onMounted(async () => {
  if (!store.producers.length) await actions.loadProducers()
  if (producer.value && producer.value.enabled !== false) await reload()
})

watch(
  () => [producer.value?.key, producer.value?.enabled],
  ([key, enabled], [, previousEnabled]) => {
    if (!key || enabled === false) return
    // 分析预设保存后会重拉插件清单：同一个 key 不必再读一次提示词。
    if (key !== loadedKey.value || (previousEnabled === false && enabled !== false)) {
      void reload()
    }
  }
)
</script>

<style scoped>
.ap-label {
  margin-top: 10px;
}
.ap-text {
  margin-top: 8px;
  height: auto;
  min-height: 150px;
  padding: 9px 11px;
  font-size: 12.3px;
  line-height: 1.65;
  resize: vertical;
}
.ap-deleted {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  margin-top: 10px;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
.ap-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--sh-text-3);
}
.prompt-fixed {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 12px;
}
.prompt-fixed:first-child {
  padding-top: 0;
}
.prompt-fixed-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.prompt-fixed-label {
  font-size: 12.6px;
  font-weight: 650;
  color: var(--sh-text-1);
}
.prompt-fixed-meta {
  font-size: 11.2px;
  color: var(--sh-text-4);
}
.prompt-text {
  height: auto;
  padding: 9px 11px;
  font-size: 12.2px;
  line-height: 1.6;
  resize: vertical;
}
</style>
