<template>
  <section class="split-panel">
    <article class="sh-card workflow-card">
      <header class="sh-card-head workflow-head">
        <span class="sh-card-icon"><BookOpen :size="16" :stroke-width="1.8" /></span>
        <div class="sh-card-title-main">
          <div class="split-title-line">
            <h2 class="split-title">分章及文本处理</h2>
            <span class="sh-chip">{{ sourceChars }} 字</span>
            <span class="sh-chip">{{ sourceParagraphs }} 段</span>
            <span v-if="chapters.length" class="sh-chip is-accent">{{ chapters.length }} 章</span>
          </div>
          <p class="split-note">先清一遍文本，再分章；已拆句的章节改文本时保留行，不必重新拆句。</p>
          <p v-if="detail?.folder" class="split-path" :title="detail.folder">
            项目目录：{{ detail.folder }}
          </p>
        </div>
      </header>

      <div class="workflow-body">
        <section class="workflow-col split-col" aria-labelledby="wf-split-title">
          <header class="col-head">
            <span class="col-icon"><Scissors :size="15" :stroke-width="1.8" /></span>
            <div class="col-head-main">
              <h3 id="wf-split-title" class="col-title">分章</h3>
              <p class="col-note">先预览再落库；换模式重切会清空已有章节与音频。</p>
            </div>
          </header>

          <div class="col-body split-body">
            <label class="field-label" for="split-pattern">分章模式</label>
            <select id="split-pattern" v-model="patternKey" class="sh-field">
              <option v-for="item in patterns.chapter_patterns" :key="item.key" :value="item.key">
                {{ item.name }}
              </option>
            </select>
            <p class="split-hint">{{ currentPattern?.example || '' }}</p>

            <div v-if="previewData" class="split-preview">
              <div class="split-chips">
                <span class="sh-chip is-accent">{{ previewData.count }} 章</span>
                <span class="sh-chip">{{ previewData.conservation.chapter_chars }} 字</span>
                <span
                  class="sh-chip"
                  :class="previewData.conservation.ok ? 'is-ok' : 'is-warn'"
                >
                  偏差 {{ (previewData.conservation.deviation * 100).toFixed(2) }}%
                </span>
              </div>
              <p
                v-for="(warning, index) in previewData.warnings"
                :key="index"
                class="sh-hint is-warn"
              >
                {{ warning }}
              </p>
              <ol class="chapter-preview">
                <li v-for="(item, index) in previewData.chapters" :key="index">
                  <span class="chapter-preview-title">{{ item.title }}</span>
                  <span class="chapter-preview-chars">{{ item.chars }} 字</span>
                </li>
              </ol>
              <p v-if="previewData.count > previewData.chapters.length" class="sh-hint">
                另有 {{ previewData.count - previewData.chapters.length }} 章未列出。
              </p>
            </div>
            <div v-else class="split-empty">
              <Scissors :size="22" :stroke-width="1.5" />
              <span>
                {{
                  state.busy === 'split-preview'
                    ? '正在预览章节切分……'
                    : '选择模式后会自动预览章节切分结果。'
                }}
              </span>
            </div>
          </div>

          <footer class="col-foot split-foot">
            <span class="col-foot-state">
              {{
                state.busy === 'split-preview'
                  ? '预览中……'
                  : previewData
                    ? `已预览 ${previewData.count} 章`
                    : '切换模式即自动预览'
              }}
            </span>
            <button class="sh-btn is-sm is-primary" :disabled="busy" @click="runSplit">
              {{ state.busy === 'split' ? '分章中……' : chapters.length ? '重新分章' : '分章' }}
            </button>
          </footer>
        </section>

        <section class="workflow-col text-col" aria-labelledby="wf-text-title">
          <header class="col-head">
            <span class="col-icon is-text"><Eraser :size="15" :stroke-width="1.8" /></span>
            <div class="col-head-main">
              <h3 id="wf-text-title" class="col-title">文本处理</h3>
              <p class="col-note">
                {{
                  scoped
                    ? '改各章当前内容：已拆句的章逐行改、保留行；未拆句的改正文。'
                    : '分章前清一遍源文；改完需要重新分章。'
                }}
              </p>
            </div>
          </header>

          <div class="col-body text-body">
            <div class="rule-groups">
              <section class="rule-group is-dialog">
                <header class="rule-group-head">
                  <span class="rule-group-icon">
                    <MessageSquareText :size="15" :stroke-width="1.8" />
                  </span>
                  <div class="rule-group-main">
                    <div class="rule-group-title-line">
                      <h4 class="rule-group-title">查找替换</h4>
                      <span class="rule-group-badge">保留内容</span>
                    </div>
                    <p class="rule-group-note">去掉引号、括号等符号，台词内容留下。</p>
                  </div>
                  <div class="rule-group-tools">
                    <span class="rule-group-count">
                      {{ dialog.length }}/{{ patterns.dialog_presets.length }}
                    </span>
                    <button
                      type="button"
                      class="rule-select-all"
                      :class="{ 'is-on': dialogAll }"
                      :aria-pressed="dialogAll"
                      :disabled="busy || !patterns.dialog_presets.length"
                      @click="toggleGroup('dialog')"
                    >
                      {{ dialogAll ? '取消全选' : '全选' }}
                    </button>
                  </div>
                </header>

                <div class="rule-list">
                  <button
                    v-for="item in patterns.dialog_presets"
                    :key="item.name"
                    type="button"
                    class="rule-item"
                    :class="{ 'is-on': dialog.includes(item.name) }"
                    :aria-pressed="dialog.includes(item.name)"
                    :title="`${item.pattern} → ${item.replacement}`"
                    @click="toggle('dialog', item.name)"
                  >
                    <span class="rule-check">
                      <Check v-if="dialog.includes(item.name)" :size="11" :stroke-width="2.6" />
                    </span>
                    <span class="rule-item-main">
                      <span class="rule-item-name">{{ item.name }}</span>
                      <span class="rule-item-detail">{{ item.pattern }} → {{ item.replacement }}</span>
                    </span>
                  </button>
                </div>

                <div class="rule-custom">
                  <span class="rule-custom-label">自定义规则</span>
                  <div class="custom-row">
                    <div class="ai-field">
                      <input
                        v-model="customPattern"
                        class="sh-field"
                        placeholder="正则表达式 / 描述或样例"
                        aria-label="自定义查找正则"
                      />
                      <button
                        type="button"
                        class="sh-btn is-icon ai-field-btn"
                        :disabled="aiDisabled('dialog')"
                        :title="aiTitle('dialog')"
                        aria-label="按输入内容生成正则"
                        @click="generateAi('dialog')"
                      >
                        <Loader2
                          v-if="aiBusy === 'dialog'"
                          class="ai-spin"
                          :size="14"
                          :stroke-width="1.8"
                        />
                        <Sparkles v-else :size="14" :stroke-width="1.8" />
                      </button>
                    </div>
                    <input
                      v-model="customReplacement"
                      class="sh-field"
                      placeholder="替换为（\1 回填）"
                      aria-label="自定义替换内容"
                    />
                  </div>
                  <RegexAssistBar
                    :suggestion="aiSuggest?.kind === 'dialog' ? aiSuggest : null"
                    :error="aiError?.kind === 'dialog' ? aiError.message : ''"
                    :stale="dialogAiStale"
                    @undo="undoAi('dialog')"
                    @dismiss="dismissAi('dialog')"
                    @retry="retryAi('dialog')"
                  />
                </div>
              </section>

              <section class="rule-group is-bracket">
                <header class="rule-group-head">
                  <span class="rule-group-icon">
                    <Trash2 :size="15" :stroke-width="1.8" />
                  </span>
                  <div class="rule-group-main">
                    <div class="rule-group-title-line">
                      <h4 class="rule-group-title">删除匹配</h4>
                      <span class="rule-group-badge">内容一起删</span>
                    </div>
                    <p class="rule-group-note">注释、广告、网址等，匹配到的整段一起删掉。</p>
                  </div>
                  <div class="rule-group-tools">
                    <span class="rule-group-count">
                      {{ bracket.length }}/{{ patterns.bracket_presets.length }}
                    </span>
                    <button
                      type="button"
                      class="rule-select-all"
                      :class="{ 'is-on': bracketAll }"
                      :aria-pressed="bracketAll"
                      :disabled="busy || !patterns.bracket_presets.length"
                      @click="toggleGroup('bracket')"
                    >
                      {{ bracketAll ? '取消全选' : '全选' }}
                    </button>
                  </div>
                </header>

                <div class="rule-list">
                  <button
                    v-for="item in patterns.bracket_presets"
                    :key="item.name"
                    type="button"
                    class="rule-item"
                    :class="{ 'is-on': bracket.includes(item.name) }"
                    :aria-pressed="bracket.includes(item.name)"
                    :title="item.example"
                    @click="toggle('bracket', item.name)"
                  >
                    <span class="rule-check">
                      <Check v-if="bracket.includes(item.name)" :size="11" :stroke-width="2.6" />
                    </span>
                    <span class="rule-item-main">
                      <span class="rule-item-name">{{ item.name }}</span>
                      <span class="rule-item-detail">{{ item.example }}</span>
                    </span>
                  </button>
                </div>

                <div class="rule-custom">
                  <span class="rule-custom-label">自定义规则</span>
                  <div class="ai-field">
                    <input
                      v-model="customBracket"
                      class="sh-field"
                      placeholder="正则表达式 / 描述或样例（匹配到的整段删除）"
                      aria-label="自定义删除正则"
                    />
                    <button
                      type="button"
                      class="sh-btn is-icon ai-field-btn"
                      :disabled="aiDisabled('bracket')"
                      :title="aiTitle('bracket')"
                      aria-label="按输入内容生成正则"
                      @click="generateAi('bracket')"
                    >
                      <Loader2
                        v-if="aiBusy === 'bracket'"
                        class="ai-spin"
                        :size="14"
                        :stroke-width="1.8"
                      />
                      <Sparkles v-else :size="14" :stroke-width="1.8" />
                    </button>
                  </div>
                  <RegexAssistBar
                    :suggestion="aiSuggest?.kind === 'bracket' ? aiSuggest : null"
                    :error="aiError?.kind === 'bracket' ? aiError.message : ''"
                    :stale="bracketAiStale"
                    @undo="undoAi('bracket')"
                    @dismiss="dismissAi('bracket')"
                    @retry="retryAi('bracket')"
                  />
                </div>
              </section>
            </div>
          </div>

          <footer class="col-foot text-foot">
            <span class="text-selected">
              已选 <b>{{ selectedRuleCount }}</b> 条规则
            </span>
            <button
              class="sh-btn is-sm is-primary"
              :disabled="busy || !selectedRuleCount"
              @click="previewRules"
            >
              {{ state.busy === 'text-preview' ? '预览中……' : '预览并确认' }}
            </button>
          </footer>
        </section>
      </div>
    </article>

    <Transition name="vh-modal">
      <div v-if="showPreview" class="vh-root" @click.self="closePreview">
        <div
          ref="panelEl"
          class="vh-modal preview-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="vh-preview-title"
        >
          <header class="preview-head">
            <div class="split-card-title-main">
              <h2 id="vh-preview-title" class="preview-title">文本处理预览</h2>
              <p class="split-card-note">
                {{
                  scoped
                    ? '取消勾选的那几处保持原样；已拆句的章逐行更新并保留行，改动行的旧音频（若有）会作废。'
                    : '取消勾选的那几处保持原样；应用后源文改写，需要重新分章。'
                }}
              </p>
            </div>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="closePreview">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="preview-tools">
            <span class="sh-chip" :class="state.textPreview?.changed ? 'is-accent' : ''">
              {{ state.textPreview?.changed ? `将应用 ${appliedCount}/${hits.length} 处` : '没有命中' }}
            </span>
            <span class="sh-chip">
              {{ state.textPreview?.chars_before }} → {{ state.textPreview?.chars_after }} 字
            </span>
            <button
              v-if="hits.length"
              class="sh-btn is-sm is-ghost"
              @click="selectAll(appliedCount < hits.length)"
            >
              {{ appliedCount < hits.length ? '全选' : '全部取消' }}
            </button>
          </div>

          <div class="hit-table-wrap preview-table">
            <table class="hit-table">
              <thead>
                <tr>
                  <th class="col-pick"></th>
                  <th v-if="scoped" class="col-chapter">章节</th>
                  <th class="col-rule">规则</th>
                  <th class="col-diff">改前 → 改后</th>
                  <th class="col-context">上下文</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="hit in hits"
                  :key="hit.key"
                  :class="{ 'is-off': excluded.includes(hit.key) }"
                >
                  <td class="col-pick">
                    <input
                      type="checkbox"
                      :checked="!excluded.includes(hit.key)"
                      :aria-label="`应用 ${hit.rule} 的这一处`"
                      @change="toggleHit(hit.key)"
                    />
                  </td>
                  <td v-if="scoped" class="col-chapter">
                    <span class="col-chapter-text">{{ hit.chapter }}</span>
                    <span v-if="hit.line" class="col-chapter-line">第 {{ hit.line }} 行</span>
                  </td>
                  <td class="col-rule">
                    <span class="hit-kind" :class="`is-${hit.kind}`">
                      {{ hit.kind === 'dialog' ? '替换' : '删除' }}
                    </span>
                    <span class="hit-rule-name">{{ hit.rule }}</span>
                  </td>
                  <td class="col-diff">
                    <span class="hit-before">
                      <template v-for="(seg, index) in segmentsOf(hit)" :key="index">
                        <del v-if="seg.removed" class="hit-del">{{ seg.text }}</del>
                        <template v-else>{{ seg.text }}</template>
                      </template>
                    </span>
                    <span class="hit-arrow">→</span>
                    <span class="hit-after">{{ hit.after || '（删除）' }}</span>
                  </td>
                  <td class="col-context">
                    <span v-if="hit.context" class="hit-context">
                      <template v-for="(seg, index) in contextSegments(hit)" :key="index">
                        <mark
                          v-if="seg.hit"
                          class="hit-context-mark"
                          :class="`is-${hit.kind}`"
                        >{{ seg.text }}</mark>
                        <template v-else>{{ seg.text }}</template>
                      </template>
                    </span>
                    <span v-else class="hit-context is-empty">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <footer class="preview-foot">
            <button class="sh-btn is-ghost" @click="closePreview">取消</button>
            <button class="sh-btn is-primary" :disabled="busy || !appliedCount" @click="applyRules">
              {{ state.busy === 'text-apply' ? '应用中……' : `应用 ${appliedCount} 处` }}
            </button>
          </footer>
        </div>
      </div>
    </Transition>
  </section>
</template>

<script setup>
// @ts-check
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  BookOpen,
  Check,
  Eraser,
  Loader2,
  MessageSquareText,
  Scissors,
  Sparkles,
  Trash2,
  X,
} from 'lucide-vue-next'
import { say } from '../../store'
import { confirmAction } from './confirm'
import RegexAssistBar from './RegexAssistBar.vue'
import { useModalA11y } from '../../composables/modalA11y'
import {
  applyText,
  loadAssistStatus,
  openSplit,
  previewSplit,
  previewText,
  runAssist,
  splitProject,
  state,
} from './store'

const dialog = ref(/** @type {string[]} */ ([]))
const bracket = ref(/** @type {string[]} */ ([]))
const customPattern = ref('')
const customReplacement = ref('')
const customBracket = ref('')
const aiBusy = ref('')
const aiError = ref(/** @type {any} */ (null))
const aiSuggest = ref(/** @type {any} */ (null))
/**
 * 上一次生成的原始输入：即使建议条被关掉，也能从同一条输入重新生成，而不是把正则当输入。
 *
 * @type {Record<string, any>}
 */
const aiLast = {
  dialog: { sample: '', pattern: '', previousPattern: '', previousReplacement: '' },
  bracket: { sample: '', pattern: '', previousPattern: '', previousReplacement: '' },
}
//: 用户取消勾选的那几处：默认全勾，只处理例外。
const excluded = ref(/** @type {string[]} */ ([]))
const showPreview = ref(false)
const panelEl = ref(/** @type {any} */ (null))
useModalA11y({ open: () => showPreview.value, panel: panelEl, onClose: () => closePreview() })
/** 预览时实际发出去的规则：应用时必须用它，不能读"现在的勾选"。 */
const previewRulesUsed = ref(/** @type {any} */ (null))

const patterns = computed(
  () => state.patterns || { chapter_patterns: [], dialog_presets: [], bracket_presets: [] }
)
const chapters = computed(() => state.detail?.chapters || [])
const detail = computed(() => state.detail)
const sourceChars = computed(() => state.source?.chars ?? state.detail?.source?.chars ?? 0)
const sourceParagraphs = computed(
  () => state.source?.paragraphs ?? state.detail?.source?.paragraphs ?? 0
)
const patternKey = computed({
  get: () => state.splitPattern,
  set: (value) => {
    state.splitPattern = value
  },
})
const currentPattern = computed(() =>
  patterns.value.chapter_patterns.find(/** @param {any} item */ (item) => item.key === patternKey.value)
)
const previewData = computed(() => state.splitPreview)

/** 换模式就重切预览：用户只做一次选择，不必再点一颗「预览」。
 *  180ms 的停顿只为合并"键盘上下键连翻"造成的连发，最终仍只认最后一个模式。 */
let splitPreviewTimer = 0
watch(
  patternKey,
  () => {
    window.clearTimeout(splitPreviewTimer)
    const key = patternKey.value
    if (!key) return
    splitPreviewTimer = window.setTimeout(() => {
      if (patternKey.value === key) void previewSplit(key)
    }, 180)
  },
  { immediate: true }
)
onBeforeUnmount(() => window.clearTimeout(splitPreviewTimer))
const scoped = computed(() => (state.detail?.chapters?.length || 0) > 0)
const BUSY = ['split', 'split-preview', 'text-preview', 'text-apply']
const busy = computed(() => BUSY.includes(state.busy))

const rules = computed(() => ({
  dialog: dialog.value,
  bracket: bracket.value,
  custom_dialog: { pattern: customPattern.value.trim(), replacement: customReplacement.value },
  custom_bracket: customBracket.value.trim(),
}))
const hits = computed(() => state.textPreview?.hits || [])
const appliedCount = computed(() => hits.value.length - excluded.value.length)
const selectedRuleCount = computed(
  () =>
    dialog.value.length +
    bracket.value.length +
    (customPattern.value.trim() ? 1 : 0) +
    (customBracket.value.trim() ? 1 : 0)
)
const dialogAll = computed(
  () =>
    patterns.value.dialog_presets.length > 0 &&
    patterns.value.dialog_presets.every(/** @param {any} item */ (item) => dialog.value.includes(item.name))
)
const bracketAll = computed(
  () =>
    patterns.value.bracket_presets.length > 0 &&
    patterns.value.bracket_presets.every(/** @param {any} item */ (item) => bracket.value.includes(item.name))
)

const dialogAiStale = computed(() => aiStaleFor('dialog'))
const bracketAiStale = computed(() => aiStaleFor('bracket'))

/**
 * @param {any} kind
 */
function fieldRef(kind) {
  return kind === 'dialog' ? customPattern : customBracket
}

/**
 * @param {any} kind
 */
function fieldOf(kind) {
  return fieldRef(kind).value
}

/**
 * @param {any} kind
 */
function aiStaleFor(kind) {
  const item = aiSuggest.value
  if (!item || item.kind !== kind) return false
  return fieldOf(kind).trim() !== item.pattern
}

/**
 * @param {any} kind
 */
function aiDisabled(kind) {
  return (
    busy.value ||
    Boolean(aiBusy.value) ||
    !fieldOf(kind).trim() ||
    state.assistStatus?.ready === false
  )
}

/**
 * @param {any} kind
 */
function aiTitle(kind) {
  if (state.assistStatus?.ready === false) {
    return state.assistStatus.note || '对话模型还没配置好'
  }
  if (aiBusy.value === kind) return '正在生成正则……'
  if (busy.value) return '等当前操作结束再生成'
  if (!fieldOf(kind).trim()) return '先在输入框里输入需求或样例'
  return '按输入框里的需求或样例生成正则'
}

/**
 * 输入框内容就是需求或样例：生成成功后直接换成正则，保留原内容供撤销。
 *
 * @param {any} kind
 * @param {any} forcedSample
 */
async function generateAi(kind, forcedSample = '') {
  if (aiBusy.value) return
  const field = fieldRef(kind)
  const previous = aiSuggest.value?.kind === kind ? aiSuggest.value : null
  const last = aiLast[kind]
  const current = field.value.trim()
  const generated = Boolean(
    (previous && current === previous.pattern.trim()) ||
      (last.pattern && current === last.pattern.trim())
  )
  const source = previous || last
  const sample = (forcedSample || (generated ? source.sample : current)).trim()
  if (!sample) return
  const previousPattern = generated ? source.previousPattern : field.value
  const previousReplacement = generated
    ? source.previousReplacement
    : customReplacement.value
  aiBusy.value = kind
  aiError.value = null
  try {
    const result = await runAssist('regex_rule', { kind, sample })
    const proposal = result.proposal || {}
    const preview = result.preview || {}
    if (kind === 'dialog') {
      customPattern.value = proposal.pattern || ''
      customReplacement.value = proposal.replacement || ''
    } else {
      customBracket.value = proposal.pattern || ''
    }
    aiLast[kind] = {
      sample,
      pattern: proposal.pattern || '',
      previousPattern,
      previousReplacement,
    }
    aiSuggest.value = {
      kind,
      sample,
      pattern: proposal.pattern || '',
      replacement: proposal.replacement || '',
      explanation: proposal.explanation || '',
      preview,
      previousPattern,
      previousReplacement,
    }
  } catch (error) {
    aiError.value = {
      kind,
      sample,
      message: error instanceof Error ? error.message : String(error),
    }
  } finally {
    aiBusy.value = ''
  }
}

/**
 * @param {any} kind
 */
function undoAi(kind) {
  const item = aiSuggest.value
  if (!item || item.kind !== kind) return
  if (kind === 'dialog') {
    customPattern.value = item.previousPattern
    customReplacement.value = item.previousReplacement
  } else {
    customBracket.value = item.previousPattern
  }
  aiLast[kind] = { sample: '', pattern: '', previousPattern: '', previousReplacement: '' }
  aiSuggest.value = null
  if (aiError.value?.kind === kind) aiError.value = null
}

/**
 * @param {any} kind
 */
function dismissAi(kind) {
  if (aiSuggest.value?.kind === kind) aiSuggest.value = null
  if (aiError.value?.kind === kind) aiError.value = null
}

/**
 * @param {any} kind
 */
function retryAi(kind) {
  const sample = aiError.value?.kind === kind ? aiError.value.sample : ''
  aiError.value = null
  if (sample) void generateAi(kind, sample)
}

/** 规则一改，上一份预览就不再对应当前勾选；必须作废，不能留一颗"查看预览"去打开旧结果。 */
watch(
  [dialog, bracket, customPattern, customReplacement, customBracket],
  () => {
    excluded.value = []
    state.textPreview = null
    previewRulesUsed.value = null
  },
  { deep: true }
)

/**
 * 只把“被删掉的那一段”交出来：去掉符号时高亮的就是符号本身，不是整句台词。
 *
 * @param {any} hit
 */
function segmentsOf(hit) {
  const before = hit.before || ''
  const after = hit.after || ''
  if (!after) return [{ text: before, removed: true }]
  const at = before.indexOf(after)
  if (at >= 0) {
    return [
      ...(at > 0 ? [{ text: before.slice(0, at), removed: true }] : []),
      { text: after, removed: false },
      ...(at + after.length < before.length
        ? [{ text: before.slice(at + after.length), removed: true }]
        : []),
    ]
  }
  let head = 0
  while (head < before.length && head < after.length && before[head] === after[head]) head += 1
  let tail = 0
  while (
    tail < before.length - head &&
    tail < after.length - head &&
    before[before.length - 1 - tail] === after[after.length - 1 - tail]
  ) {
    tail += 1
  }
  return [
    ...(head ? [{ text: before.slice(0, head), removed: false }] : []),
    { text: before.slice(head, before.length - tail), removed: true },
    ...(tail ? [{ text: before.slice(before.length - tail), removed: false }] : []),
  ]
}

/**
 * 上下文窗口按 `context_at` / `context_len` 切三段，只给命中那一段上色。
 *
 * @param {any} hit
 */
function contextSegments(hit) {
  const context = hit.context || ''
  const at = Number(hit.context_at) || 0
  const len = Number(hit.context_len) || 0
  if (!context) return []
  if (len <= 0 || at >= context.length) return [{ text: context, hit: false }]
  return [
    ...(at > 0 ? [{ text: context.slice(0, at), hit: false }] : []),
    { text: context.slice(at, at + len), hit: true },
    ...(at + len < context.length
      ? [{ text: context.slice(at + len), hit: false }]
      : []),
  ]
}

/**
 * @param {string} key
 */
function toggleHit(key) {
  const index = excluded.value.indexOf(key)
  if (index >= 0) excluded.value.splice(index, 1)
  else excluded.value.push(key)
}

/**
 * @param {any} value
 */
function selectAll(value) {
  excluded.value = value
    ? []
    : hits.value.map(
        /** @param {any} hit */
        (hit) => hit.key
      )
}

/**
 * @param {any} group
 * @param {any} name
 */
function toggle(group, name) {
  const target = group === 'dialog' ? dialog : bracket
  const index = target.value.indexOf(name)
  if (index >= 0) target.value.splice(index, 1)
  else target.value.push(name)
}

/**
 * @param {any} group
 */
function toggleGroup(group) {
  const presets =
    group === 'dialog' ? patterns.value.dialog_presets : patterns.value.bracket_presets
  const target = group === 'dialog' ? dialog : bracket
  const all = group === 'dialog' ? dialogAll.value : bracketAll.value
  target.value = all ? [] : presets.map(/** @param {any} item */ (item) => item.name)
}

async function runSplit() {
  if (chapters.value.length) {
    const answer = await confirmAction({
      title: '重新分章',
      message: `现有 ${chapters.value.length} 章连同逐行音频、成品都会被清掉，无法还原。`,
      confirmText: '重新分章',
    })
    if (!answer) return
  }
  void splitProject(patternKey.value)
}

async function previewRules() {
  if (!selectedRuleCount.value) return
  excluded.value = []
  // 数组要复制：勾选是就地增删的，共用引用会让"预览时用的规则"跟着后续改动走。
  previewRulesUsed.value = {
    dialog: [...dialog.value],
    bracket: [...bracket.value],
    custom_dialog: { ...rules.value.custom_dialog },
    custom_bracket: rules.value.custom_bracket,
    exclude: [],
  }
  await previewText(previewRulesUsed.value)
  // 请求期间勾选变了：watcher 已把预览规则作废，这份结果不再对应界面，别打开它。
  if (!previewRulesUsed.value) {
    state.textPreview = null
    return
  }
  if (!state.textPreview) return
  if (!state.textPreview.changed) {
    state.textPreview = null
    previewRulesUsed.value = null
    say('这些规则没有命中任何文字。', { kind: 'warn' })
    return
  }
  showPreview.value = true
}

function closePreview() {
  showPreview.value = false
  state.textPreview = null
  excluded.value = []
  previewRulesUsed.value = null
}

async function applyRules() {
  const chosen = previewRulesUsed.value
  if (!chosen) return
  const rowHits = hits.value.filter(
    /** @param {any} hit */
    (hit) => hit.line
  )
  const answer = await confirmAction({
    title: '应用文本处理',
    message: scoped.value
      ? rowHits.length
        ? `将应用 ${appliedCount.value} / ${hits.value.length} 处改动；已拆句的章逐行更新并保留行，改动行的旧音频（若有）会作废，不需要重新拆句。`
        : `将应用 ${appliedCount.value} / ${hits.value.length} 处改动；未拆句的章更新正文，需要重新拆句。`
      : `将应用 ${appliedCount.value} / ${hits.value.length} 处改动；源文改写后，现有章节与音频作废，需要重新分章。`,
    confirmText: '应用',
  })
  if (!answer) return
  const saved = await applyText({ ...chosen, exclude: [...excluded.value] })
  if (saved) closePreview()
}

// 没有章节时面板是自动出现的（不是点开的）：挂载时补齐原文与规则表。
onMounted(() => {
  if (!state.source) void openSplit()
  void loadAssistStatus()
})
</script>

<style scoped src="./SplitPanel.css"></style>
