<template>
  <SettingsSection id="lexicon" title="词表" :show-actions="false">
    <template #icon><Tags :size="16" :stroke-width="1.8" /></template>

    <!-- 三份词条都只在编辑时才需要看全：卡上给数量，改在弹窗里。 -->
    <SettingCard title="情绪候选" keywords="情绪 候选 词表 语气 开心 悲伤 忐忑 冷漠">
      <template #icon><Smile :size="16" :stroke-width="1.8" /></template>
      <template #head>
        <ActionButton variant="ghost" size="sm" aria-haspopup="dialog" @click="openDialog('emotions')">
          <Pencil :size="14" :stroke-width="1.8" />
          编辑
        </ActionButton>
      </template>
      <div class="lx-preview">
        <span class="lx-preview-meta">{{ emotionSummary }}</span>
        <div v-if="emotionPreview.length" class="lx-preview-chips">
          <span
            v-for="word in emotionPreview"
            :key="word.value"
            class="sh-chip"
            :class="{ 'is-accent': word.custom }"
          >
            {{ word.value }}
          </span>
          <span v-if="emotionRest" class="sh-chip">+{{ emotionRest }}</span>
        </div>
      </div>
    </SettingCard>

    <SettingCard title="引导指令预设" keywords="引导指令 预设 语气 instruction 朗读">
      <template #icon><Speech :size="16" :stroke-width="1.8" /></template>
      <template #head>
        <ActionButton
          variant="ghost"
          size="sm"
          aria-haspopup="dialog"
          @click="openDialog('instructions')"
        >
          <Pencil :size="14" :stroke-width="1.8" />
          编辑
        </ActionButton>
      </template>
      <div class="lx-preview">
        <span class="lx-preview-meta">{{ instructionSummary }}</span>
        <div v-if="instructionPreview.length" class="lx-preview-chips">
          <span
            v-for="(item, at) in instructionPreview"
            :key="item.key || `c${at}`"
            class="sh-chip"
            :class="{ 'is-accent': item.custom }"
            :title="item.text"
          >
            {{ item.label }}
          </span>
          <span v-if="instructionRest" class="sh-chip">+{{ instructionRest }}</span>
        </div>
      </div>
    </SettingCard>

    <SettingCard title="读音替换" keywords="多音字 读音 拼音 替换 同音字 人名 地名">
      <template #icon><Languages :size="16" :stroke-width="1.8" /></template>
      <template #head>
        <ActionButton
          variant="ghost"
          size="sm"
          aria-haspopup="dialog"
          @click="openDialog('pronunciation')"
        >
          <Pencil :size="14" :stroke-width="1.8" />
          编辑
        </ActionButton>
      </template>
      <div class="lx-preview">
        <span class="lx-preview-meta">{{ pronunciationSummary }}</span>
        <div v-if="pronunciationPreview.length" class="lx-preview-text">
          <span v-for="(entry, at) in pronunciationPreview" :key="at">
            {{ entry.word }}<span class="lx-arrow">→</span>{{ entry.replacement }}
          </span>
          <span v-if="pronunciationRest" class="lx-more">+{{ pronunciationRest }}</span>
        </div>
      </div>
    </SettingCard>

    <LexiconAiPrompts />

    <Teleport to="body">
      <Transition name="vh-modal">
        <div v-if="dialog" class="vh-root" @click.self="closeDialog">
          <div ref="panelEl" class="vh-modal sh-dialog" role="dialog" aria-modal="true" :aria-label="dialogTitle">
            <header class="sh-dialog-head">
              <h2 class="sh-dialog-title">{{ dialogTitle }}</h2>
              <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="closeDialog">
                <X :size="16" :stroke-width="1.8" />
              </button>
            </header>

            <div class="sh-dialog-body">
              <template v-if="dialog === 'emotions'">
                <div class="lx-rows">
                  <template v-for="(word, at) in emotionRows" :key="emotionKey(word, at)">
                    <div v-if="word.group" class="lx-group">{{ word.group }}</div>
                    <div class="lx-row">
                      <TextField
                        :model-value="word.value"
                        label="情绪词"
                        placeholder="情绪词"
                        @update:model-value="editEmotionRow(word.at, $event)"
                      />
                      <button
                        type="button"
                        class="sh-btn is-icon is-sm"
                        :title="`删除「${word.value.trim() || '这一条'}」`"
                        :aria-label="`删除${word.value.trim() || '这一条'}情绪词`"
                        @click="deleteEmotionRow(word.at)"
                      >
                        <X :size="14" :stroke-width="1.8" />
                      </button>
                    </div>
                  </template>

                  <template v-if="draft.emotions.deleted.length">
                    <div class="lx-group">已删除</div>
                    <div
                      v-for="row in draft.emotions.deleted"
                      :key="`x${row.key}`"
                      class="lx-row is-deleted"
                    >
                      <span class="lx-name is-deleted">{{ row.value || row.key }}</span>
                      <button
                        type="button"
                        class="sh-btn is-sm is-ghost"
                        @click="restoreEmotion(row)"
                      >
                        恢复
                      </button>
                    </div>
                  </template>
                </div>

                <div class="lx-add">
                  <TextField
                    v-model="draftEmotion"
                    label="添加情绪词"
                    placeholder="添加情绪词，回车确认"
                    @enter="addEmotion"
                  />
                  <ActionButton
                    variant="soft"
                    size="sm"
                    :disabled="!canAddEmotion"
                    @click="addEmotion"
                  >
                    添加
                  </ActionButton>
                </div>

                <p class="lx-note">
                  默认词可改名或删除；删掉的默认词列在下方，可恢复。删除只改候选表——项目行、
                  样例上已经写下的情绪仍是它们自己的数据，不会跟着变。
                </p>
              </template>

              <template v-else-if="dialog === 'instructions'">
                <div class="lx-rows">
                  <div
                    v-for="(item, at) in instructionRows"
                    :key="instructionKey(item, at)"
                    class="lx-row is-wide"
                  >
                    <TextField
                      :model-value="item.label"
                      label="预设短名"
                      placeholder="短名"
                      @update:model-value="item.label = $event"
                    />
                    <textarea
                      class="sh-field lx-text"
                      rows="2"
                      :value="item.text"
                      :aria-label="`${item.label || '这条预设'} 的句子`"
                      @input="onItemText(item, $event)"
                    />
                    <button
                      type="button"
                      class="sh-btn is-icon is-sm"
                      :title="`删除「${item.label.trim() || '这一条'}」`"
                      :aria-label="`删除${item.label.trim() || '这一条'}预设`"
                      @click="deleteInstructionRow(at)"
                    >
                      <X :size="14" :stroke-width="1.8" />
                    </button>
                  </div>

                  <template v-if="draft.instructions.deleted.length">
                    <div class="lx-group">已删除</div>
                    <div
                      v-for="row in draft.instructions.deleted"
                      :key="`x${row.key}`"
                      class="lx-row is-wide is-deleted"
                    >
                      <span class="lx-label is-deleted">{{ row.label || row.key }}</span>
                      <span class="lx-text is-deleted" :title="row.text">{{ row.text }}</span>
                      <button
                        type="button"
                        class="sh-btn is-sm is-ghost"
                        @click="restoreInstruction(row)"
                      >
                        恢复
                      </button>
                    </div>
                  </template>
                </div>

                <div class="lx-add is-stack">
                  <div class="lx-add-line">
                    <TextField
                      v-model="draftPreset.label"
                      label="预设短名"
                      placeholder="短名，如「气声耳语」"
                    />
                    <ActionButton
                      variant="soft"
                      size="sm"
                      :disabled="!canAddPreset"
                      @click="addPreset"
                    >
                      添加
                    </ActionButton>
                  </div>
                  <textarea
                    v-model="draftPreset.text"
                    class="sh-field"
                    rows="2"
                    aria-label="预设句子"
                    placeholder="一整句语气描述，如「气声多一点，像贴着耳朵说话。」"
                  />
                </div>

                <p class="lx-note">
                  预设只是现成句子：点一颗即填入指令框，也可继续改。删除只改候选表，不影响已经
                  写在行参数里的句子；删掉的默认预设可恢复。新预设的短名不能与默认预设同名。
                </p>
              </template>

              <template v-else>
                <div class="lx-rows">
                  <div v-for="(entry, at) in pronunciation" :key="`p${at}`" class="lx-row is-pron">
                    <TextField
                      :model-value="entry.word"
                      label="词"
                      placeholder="要改的词"
                      @update:model-value="editPronunciation(at, 'word', $event)"
                    />
                    <span class="lx-arrow" aria-hidden="true">→</span>
                    <TextField
                      :model-value="entry.replacement"
                      label="读作"
                      placeholder="读法相同的词"
                      @update:model-value="editPronunciation(at, 'replacement', $event)"
                    />
                    <button
                      type="button"
                      class="sh-btn is-icon is-sm"
                      :title="`删除「${entry.word || '这一条'}」`"
                      :aria-label="`删除${entry.word || '这一条'}读音替换`"
                      @click="dropPronunciation(at)"
                    >
                      <X :size="14" :stroke-width="1.8" />
                    </button>
                  </div>
                  <p v-if="!pronunciation.length" class="lx-empty">
                    还没有替换条目。比如「重庆 → 崇庆」「银行 → 银航」。
                  </p>
                </div>

                <div class="lx-add">
                  <TextField v-model="draftPronunciation.word" label="添加词" placeholder="如：重庆" />
                  <span class="lx-arrow" aria-hidden="true">→</span>
                  <TextField
                    v-model="draftPronunciation.replacement"
                    label="读作"
                    placeholder="如：崇庆"
                  />
                  <ActionButton
                    variant="soft"
                    size="sm"
                    :disabled="!canAddPronunciation"
                    @click="addPronunciation"
                  >
                    添加
                  </ActionButton>
                </div>

                <p class="lx-note">
                  对所有音色生效；按最长词优先替换，单字会替换正文里所有出现位置。方括号里的语气标记不算正文。
                </p>
              </template>
            </div>

            <footer class="sh-dialog-foot">
              <template v-if="dialog === 'emotions'">
                <ActionButton variant="ghost" size="sm" @click="reset('emotions')">
                  恢复默认
                </ActionButton>
                <ActionButton
                  variant="soft"
                  size="sm"
                  :loading="saving === 'emotions'"
                  @click="save('emotions')"
                >
                  保存情绪
                </ActionButton>
              </template>
              <template v-else-if="dialog === 'instructions'">
                <ActionButton variant="ghost" size="sm" @click="reset('instructions')">
                  恢复默认
                </ActionButton>
                <ActionButton
                  variant="soft"
                  size="sm"
                  :loading="saving === 'instructions'"
                  @click="save('instructions')"
                >
                  保存预设
                </ActionButton>
              </template>
              <template v-else>
                <ActionButton variant="ghost" size="sm" @click="clearPronunciation">清空</ActionButton>
                <ActionButton
                  variant="soft"
                  size="sm"
                  :loading="pronunciationSaving"
                  @click="savePronunciation"
                >
                  保存读音
                </ActionButton>
              </template>
            </footer>
          </div>
        </div>
      </Transition>
    </Teleport>
  </SettingsSection>
</template>

<script setup>
// @ts-check
/** 用户词表：情绪候选与引导指令预设。
 *
 *  默认那份在仓库（情绪词表 + 各引擎 instructions.json），用户只记差别（删除 / 改名 /
 *  自定义），存进数据目录的两个文件——仓库更新默认时，没动过的人自动跟着走。界面上改的
 *  是草稿，保存之前后端那一份没动（与「文案」那一栏同一条规矩）。
 *
 *  删除只改候选表：项目行、样例上已经写下的词是它们自己的数据，这里不碰。
 *  默认行直接编辑：改名原地生效（分组与位置不变），不用先删再加。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Languages, Pencil, Smile, Speech, Tags, X } from 'lucide-vue-next'
import { actions, say, store } from '../../store'
import { api } from '../../api'
import ActionButton from '../../components/ui/ActionButton.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingsSection from '../ui/SettingsSection.vue'
import TextField from '../../components/ui/TextField.vue'
import LexiconAiPrompts from './LexiconAiPrompts.vue'
import { useModalA11y } from '../../composables/modalA11y'

/** 情绪：`rows` 是当前摆出来的行，`deleted` 是删掉的默认词（可恢复）。
 *  行带 `origin` 与默认词原名 `key`，保存时据此算 hidden / renamed / custom。 */
function emptyDraft() {
  return {
    emotions: { rows: [], deleted: [] },
    instructions: { rows: [], deleted: [] },
  }
}

const draft = ref(/** @type {any} */ (emptyDraft()))
const draftEmotion = ref('')
const draftPreset = ref({ label: '', text: '' })
const saving = ref('')
/** '' | 'emotions' | 'instructions' | 'pronunciation'：哪份词条正开着弹窗。 */
const dialog = ref('')
const panelEl = ref(/** @type {any} */ (null))
useModalA11y({ open: () => Boolean(dialog.value), panel: panelEl, onClose: () => closeDialog() })
const pronunciation = ref(/** @type {any[]} */ ([]))
const draftPronunciation = ref({ word: '', replacement: '' })
const pronunciationSaving = ref(false)

function emotionDraftFromStore() {
  const lex = store.lexicon.emotions || {}
  const hidden = new Set(lex.hidden || [])
  const renamed = lex.renamed || {}
  const defaults = lex.defaults || []
  const rows = []
  const active = new Set()
  for (const word of defaults) {
    if (hidden.has(word)) continue
    const value = renamed[word] || word
    rows.push({ origin: 'default', key: word, value })
    active.add(value)
  }
  for (const word of lex.custom || []) {
    if (active.has(word)) continue
    rows.push({ origin: 'custom', key: '', value: word })
    active.add(word)
  }
  const deleted = defaults
    .filter(/** @param {any} word */ (word) => hidden.has(word))
    .map(/** @param {any} word */ (word) => ({ key: word, value: word }))
  return { rows, deleted }
}

function instructionDraftFromStore() {
  const lex = store.lexicon.instructions || {}
  const hidden = new Set(lex.hidden || [])
  const renamed = lex.renamed || {}
  const defaults = lex.defaults || []
  const custom = lex.custom || []
  const byLabel = new Map(custom.map(/** @param {any} item */ (item) => [item.label, item]))
  const defaultLabels = new Set(defaults.map(/** @param {any} item */ (item) => item.label))
  const rows = []
  const seen = new Set()
  for (const item of defaults) {
    if (hidden.has(item.label)) continue
    const override = byLabel.get(item.label)
    const label = renamed[item.label] || item.label
    rows.push({
      origin: 'default',
      key: item.label,
      label,
      text: override ? override.text : item.text,
    })
    seen.add(label)
  }
  for (const item of custom) {
    if (defaultLabels.has(item.label) || seen.has(item.label)) continue
    rows.push({ origin: 'custom', key: '', label: item.label, text: item.text })
    seen.add(item.label)
  }
  const deleted = defaults
    .filter(/** @param {any} item */ (item) => hidden.has(item.label))
    .map(/** @param {any} item */ (item) => ({ key: item.label, label: item.label, text: item.text }))
  return { rows, deleted }
}

function fromStore() {
  draft.value = {
    emotions: emotionDraftFromStore(),
    instructions: instructionDraftFromStore(),
  }
}

/** 默认词 → 默认分组。改名后仍摆在原组里，删除后恢复也回原位。 */
const defaultEmotionGroup = computed(() => {
  const map = new Map()
  for (const group of store.lexicon.emotions?.default_groups || []) {
    for (const word of group.words || []) map.set(word, group.label)
  }
  return map
})

/** 分组标题挂在每组第一行上；自定义词单独一组。 */
const emotionRows = computed(() => {
  let last = ''
  return draft.value.emotions.rows.map(
    /** @param {any} row @param {number} at */
    (row, at) => {
      const label =
        row.origin === 'default' ? defaultEmotionGroup.value.get(row.key) || '' : '自定义'
      const group = label !== last ? label : ''
      last = label
      return { ...row, at, group }
    }
  )
})

const instructionRows = computed(() => draft.value.instructions.rows)

/**
 * @param {any} row
 * @param {any} at
 */
function emotionKey(row, at) {
  return row.origin === 'default' ? `d${row.key}` : `c${at}`
}

/**
 * @param {any} row
 * @param {any} at
 */
function instructionKey(row, at) {
  return row.origin === 'default' ? `d${row.key}` : `c${at}`
}

/** 卡上只写数量：词条本身不进设置页，编辑时才铺开。 */
const emotionSummary = computed(() => {
  const total = draft.value.emotions.rows.length
  const custom = draft.value.emotions.rows.filter(/** @param {any} row */ (row) => row.origin === 'custom').length
  const deleted = draft.value.emotions.deleted.length
  const parts = [`${total} 个词`]
  if (custom) parts.push(`${custom} 个自定义`)
  if (deleted) parts.push(`${deleted} 个已删除`)
  return parts.join(' · ')
})

const instructionSummary = computed(() => {
  const total = draft.value.instructions.rows.length
  const custom = draft.value.instructions.rows.filter(/** @param {any} row */ (row) => row.origin === 'custom').length
  const deleted = draft.value.instructions.deleted.length
  const parts = [`${total} 条预设`]
  if (custom) parts.push(`${custom} 个自定义`)
  if (deleted) parts.push(`${deleted} 个已删除`)
  return parts.join(' · ')
})

const pronunciationSummary = computed(() =>
  pronunciation.value.length ? `${pronunciation.value.length} 条替换` : '尚未添加'
)

/** 卡上给前几条实际内容：只写数字看不出这份词表里有什么。 */
const PREVIEW_LIMIT = 8

const emotionPreview = computed(() =>
  draft.value.emotions.rows
    .filter(/** @param {any} row */ (row) => row.value.trim())
    .slice(0, PREVIEW_LIMIT)
    .map(/** @param {any} row */ (row) => ({ value: row.value.trim(), custom: row.origin === 'custom' }))
)
const emotionRest = computed(() =>
  Math.max(0, draft.value.emotions.rows.filter(/** @param {any} row */ (row) => row.value.trim()).length - PREVIEW_LIMIT)
)

const instructionPreview = computed(() =>
  draft.value.instructions.rows.slice(0, 6).map(/** @param {any} row */ (row) => ({
    key: row.origin === 'default' ? row.key : '',
    label: row.label.trim(),
    text: row.text,
    custom: row.origin === 'custom',
  }))
)
const instructionRest = computed(() =>
  Math.max(0, draft.value.instructions.rows.length - 6)
)

const pronunciationPreview = computed(() => pronunciation.value.slice(0, 3))
const pronunciationRest = computed(() => Math.max(0, pronunciation.value.length - 3))

const DIALOG_TITLES = {
  emotions: '情绪候选',
  instructions: '引导指令预设',
  pronunciation: '读音替换',
}

const dialogTitle = computed(
  () => /** @type {Record<string, string>} */ (DIALOG_TITLES)[dialog.value] || ''
)

/**
 * @param {any} which
 */
function openDialog(which) {
  dialog.value = which
  // 遮罩盖住页面，滚轮仍会滚到背后的设置页。
  document.body.style.overflow = 'hidden'
}

function closeDialog() {
  dialog.value = ''
  document.body.style.overflow = ''
}

onBeforeUnmount(() => {
  document.body.style.overflow = ''
})

const canAddEmotion = computed(() => {
  const word = draftEmotion.value.trim()
  if (!word) return false
  const active = draft.value.emotions.rows
    .filter(/** @param {any} row */ (row) => row.origin === 'default')
    .map(/** @param {any} row */ (row) => row.value.trim())
  const deleted = draft.value.emotions.deleted.map(/** @param {any} row */ (row) => row.key)
  if (active.includes(word) || deleted.includes(word)) return false
  return !draft.value.emotions.rows.some(/** @param {any} row */ (row) => row.value.trim() === word)
})

const canAddPreset = computed(() => {
  const label = draftPreset.value.label.trim()
  const text = draftPreset.value.text.trim()
  if (!label || !text) return false
  // 与任何默认短名（含已删除的）同名会被后端当成覆盖默认，而不是新预设。
  if ((store.lexicon.instructions?.defaults || []).some(/** @param {any} item */ (item) => item.label === label)) {
    return false
  }
  return !draft.value.instructions.rows.some(/** @param {any} row */ (row) => row.label.trim() === label)
})

const canAddPronunciation = computed(() => {
  const word = draftPronunciation.value.word.trim()
  const replacement = draftPronunciation.value.replacement.trim()
  if (!word || !replacement) return false
  return !pronunciation.value.some(/** @param {any} entry */ (entry) => entry.word.trim() === word)
})

function addEmotion() {
  const word = draftEmotion.value.trim()
  if (!canAddEmotion.value) return
  draft.value.emotions.rows.push({ origin: 'custom', key: '', value: word })
  draftEmotion.value = ''
}

/**
 * @param {any} item
 * @param {Event} event
 */
function onItemText(item, event) {
  item.text = /** @type {HTMLTextAreaElement} */ (event.target).value
}

function addPreset() {
  if (!canAddPreset.value) return
  draft.value.instructions.rows.push({
    origin: 'custom',
    key: '',
    label: draftPreset.value.label.trim(),
    text: draftPreset.value.text.trim(),
  })
  draftPreset.value = { label: '', text: '' }
}

/**
 * @param {any} at
 * @param {any} value
 */
function editEmotionRow(at, value) {
  const row = draft.value.emotions.rows[at]
  if (row) row.value = value
}

/**
 * @param {any} at
 */
function deleteEmotionRow(at) {
  const row = draft.value.emotions.rows[at]
  if (!row) return
  draft.value.emotions.rows.splice(at, 1)
  if (row.origin === 'default') {
    draft.value.emotions.deleted.push({ key: row.key, value: row.value.trim() })
  }
}

/**
 * 恢复回默认顺序里：默认行总在自定义行之前。
 *
 * @param {any} row
 */
function restoreEmotion(row) {
  const deleted = draft.value.emotions.deleted
  const at = deleted.indexOf(row)
  if (at < 0) return
  deleted.splice(at, 1)
  const defaults = store.lexicon.emotions?.defaults || []
  const order = defaults.indexOf(row.key)
  const before = draft.value.emotions.rows.filter(/** @param {any} item */ (item) => {
    if (item.origin !== 'default') return false
    return defaults.indexOf(item.key) < order
  }).length
  draft.value.emotions.rows.splice(before, 0, {
    origin: 'default',
    key: row.key,
    value: row.value || row.key,
  })
}

/**
 * @param {any} at
 */
function deleteInstructionRow(at) {
  const row = draft.value.instructions.rows[at]
  if (!row) return
  draft.value.instructions.rows.splice(at, 1)
  if (row.origin === 'default') {
    draft.value.instructions.deleted.push({
      key: row.key,
      label: row.label.trim(),
      text: row.text,
    })
  }
}

/**
 * @param {any} row
 */
function restoreInstruction(row) {
  const deleted = draft.value.instructions.deleted
  const at = deleted.indexOf(row)
  if (at < 0) return
  deleted.splice(at, 1)
  const defaults = store.lexicon.instructions?.defaults || []
  const order = defaults.findIndex(/** @param {any} item */ (item) => item.label === row.key)
  const before = draft.value.instructions.rows.filter(/** @param {any} item */ (item) => {
    if (item.origin !== 'default') return false
    return defaults.findIndex(/** @param {any} entry */ (entry) => entry.label === item.key) < order
  }).length
  draft.value.instructions.rows.splice(before, 0, {
    origin: 'default',
    key: row.key,
    label: row.label || row.key,
    text: row.text,
  })
}

/** 当前行 → 保存体：默认行没改名不进 renamed，自定义行进 custom。 */
function emotionPatch() {
  const hidden = draft.value.emotions.deleted.map(/** @param {any} row */ (row) => row.key)
  const renamed = /** @type {Record<string, any>} */ ({})
  const custom = []
  const seen = new Set()
  for (const row of draft.value.emotions.rows) {
    const value = row.value.trim()
    if (!value) return { error: '还有情绪词是空的。' }
    if (seen.has(value)) return { error: `「${value}」重复了。` }
    seen.add(value)
    if (row.origin === 'default') {
      if (value !== row.key) renamed[row.key] = value
    } else {
      custom.push(value)
    }
  }
  return { patch: { hidden, renamed, custom } }
}

/** 默认行：改短名进 renamed，改句子进同名自定义（后端就地覆盖）。 */
function instructionPatch() {
  const hidden = draft.value.instructions.deleted.map(/** @param {any} row */ (row) => row.key)
  const renamed = /** @type {Record<string, any>} */ ({})
  const custom = []
  const defaults = new Map(
    (store.lexicon.instructions?.defaults || []).map(/** @param {any} item */ (item) => [item.label, item])
  )
  const seen = new Set()
  for (const row of draft.value.instructions.rows) {
    const label = row.label.trim()
    const text = row.text.trim()
    if (!label || !text) return { error: '还有预设的短名或句子是空的。' }
    if (seen.has(label)) return { error: `「${label}」重复了。` }
    seen.add(label)
    if (row.origin === 'default') {
      if (label !== row.key) renamed[row.key] = label
      const original = defaults.get(row.key)
      if (!original || text !== original.text) custom.push({ label: row.key, text })
    } else {
      custom.push({ label, text })
    }
  }
  return { patch: { hidden, renamed, custom } }
}

function addPronunciation() {
  if (!canAddPronunciation.value) return
  pronunciation.value.push({
    word: draftPronunciation.value.word.trim(),
    replacement: draftPronunciation.value.replacement.trim(),
  })
  draftPronunciation.value = { word: '', replacement: '' }
}

/**
 * 行内直接改草稿；按下标定位，改词也不会找错人。
 *
 * @param {any} at
 * @param {string} key
 * @param {any} value
 */
function editPronunciation(at, key, value) {
  const entry = pronunciation.value[at]
  if (!entry) return
  entry[key] = value
}

/**
 * @param {any} at
 */
function dropPronunciation(at) {
  pronunciation.value.splice(at, 1)
}

/**
 * @param {any} result
 */
function applyPronunciation(result) {
  pronunciation.value = (result.entries || []).map(/** @param {any} entry */ (entry) => ({
    word: entry.word || '',
    replacement: entry.replacement || '',
  }))
}

async function loadPronunciation() {
  try {
    applyPronunciation(await api.pronunciations())
  } catch (error) {
    say(error instanceof Error ? error.message : String(error), { kind: 'error' })
  }
}

/**
 * @param {any} entries
 * @param {any} message
 */
async function persistPronunciation(entries, message) {
  if (pronunciationSaving.value) return
  pronunciationSaving.value = true
  try {
    const result = await api.savePronunciations(entries)
    applyPronunciation(result)
    say(message)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error), { kind: 'error' })
  } finally {
    pronunciationSaving.value = false
  }
}

async function savePronunciation() {
  const entries = pronunciation.value.map(/** @param {any} entry */ (entry) => ({
    word: entry.word.trim(),
    replacement: entry.replacement.trim(),
  }))
  if (entries.some(/** @param {any} entry */ (entry) => !entry.word || !entry.replacement)) {
    say('还有读音条目没填完。', { kind: 'warn' })
    return
  }
  if (new Set(entries.map(/** @param {any} entry */ (entry) => entry.word)).size !== entries.length) {
    say('「词」重复了。', { kind: 'warn' })
    return
  }
  await persistPronunciation(entries, '读音替换已保存')
}

async function clearPronunciation() {
  if (!pronunciation.value.length) return
  if (!window.confirm('清空全部读音替换？')) return
  await persistPronunciation([], '读音替换已清空')
}

/**
 * @param {any} section
 */
async function save(section) {
  const built = section === 'emotions' ? emotionPatch() : instructionPatch()
  if (built.error) {
    say(built.error, { kind: 'warn' })
    return
  }
  saving.value = section
  try {
    await actions.saveLexicon({ [section]: built.patch })
    fromStore()
    say(section === 'emotions' ? '情绪候选已保存' : '引导指令预设已保存')
  } catch (err) {
    say(String(err instanceof Error ? err.message : String(err)), { kind: 'error' })
  } finally {
    saving.value = ''
  }
}

/**
 * 恢复默认 = 存一份空差别（删除、改名与自定义都清掉）。
 *
 * @param {any} section
 */
async function reset(section) {
  if (!window.confirm('恢复默认会清空这一节里你改名、删除和添加的内容，确定？')) return
  saving.value = section
  try {
    await actions.saveLexicon({ [section]: { hidden: [], renamed: {}, custom: [] } })
    fromStore()
    say('已恢复默认')
  } catch (err) {
    say(String(err instanceof Error ? err.message : String(err)), { kind: 'error' })
  } finally {
    saving.value = ''
  }
}

onMounted(async () => {
  await actions.ensureLexicon()
  fromStore()
  await loadPronunciation()
})
</script>

<style scoped>
.lx-preview {
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.lx-preview-meta {
  font-size: 12px;
  color: var(--sh-text-3);
}
.lx-preview-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.lx-preview-chips .sh-chip {
  height: 22px;
  padding: 0 9px;
  font-size: 11px;
  font-weight: 500;
}
.lx-preview-text {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  font-size: 12px;
  color: var(--sh-text-2);
}
.lx-preview-text .lx-arrow {
  margin: 0 4px;
}
.lx-more {
  color: var(--sh-text-4);
}
.lx-note {
  margin: 12px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--sh-text-4);
}
.sh-dialog-foot {
  justify-content: flex-end;
}
.lx-rows {
  display: flex;
  flex-direction: column;
}
/* 分组标题（基础 / 复合 / 自定义 / 已删除）：一组一段，标题与它下面第一行之间不划线。 */
.lx-group {
  padding: 14px 0 4px;
  color: var(--sh-text-4);
  font-size: 10.8px;
  font-weight: 700;
  letter-spacing: 0.08em;
}
.lx-group:first-child {
  padding-top: 0;
}
.lx-group + .lx-row {
  border-top: 0;
}
.lx-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}
.lx-row:first-child {
  border-top: 0;
}
/* 引导指令行：短名一列、句子一列、删除各一列。 */
.lx-row.is-wide {
  grid-template-columns: 92px minmax(0, 1fr) auto;
}
/* 读音行：词 → 读作 → 删除。 */
.lx-row.is-pron {
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto;
}
.lx-arrow {
  flex: none;
  font-size: 12px;
  color: var(--sh-text-4);
}
.lx-empty {
  margin: 4px 0 2px;
  font-size: 12px;
  color: var(--sh-text-4);
}
/* 每行都挂一颗按钮太吵：默认隐身，指着这一行才出现；删除行与已删除那类常显。 */
.lx-row .sh-btn {
  opacity: 0.28;
  transition: opacity var(--sh-t-fast) var(--sh-ease);
}
.lx-row:hover .sh-btn,
.lx-row:focus-within .sh-btn,
.lx-row.is-deleted .sh-btn {
  opacity: 1;
}
.lx-row.is-deleted .lx-name,
.lx-row.is-deleted .lx-label,
.lx-row.is-deleted .lx-text {
  color: var(--sh-text-4);
  text-decoration: line-through;
}
.lx-name,
.lx-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.4px;
  color: var(--sh-text-2);
}
.lx-label {
  font-size: 12px;
  color: var(--sh-text-3);
}
.lx-text {
  min-width: 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--sh-text-3);
  /* 句子可能很长：摆两行，超出的收起，全文在 title 里。 */
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}
textarea.lx-text {
  display: block;
  resize: vertical;
  -webkit-line-clamp: unset;
}
.lx-add {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}
.lx-add .tf {
  flex: 1;
}
.lx-add.is-stack {
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
}
.lx-add-line {
  display: flex;
  align-items: center;
  gap: 8px;
}
.lx-add-line .tf {
  flex: 1;
}
</style>
