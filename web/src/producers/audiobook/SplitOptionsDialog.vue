<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="$emit('close')">
        <div ref="panelEl" class="vh-modal options-card" role="dialog" aria-modal="true" aria-labelledby="vh-split-options-title">
          <header class="options-head">
            <div class="split-card-title-main">
              <h2 id="vh-split-options-title" class="options-title">拆句选项</h2>
              <p class="options-note">选项按项目保存；拆句与重拆都按当前选项切行。</p>
            </div>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="$emit('close')">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="options-body">
            <section class="option-block" role="group" aria-labelledby="split-mode-title">
              <span id="split-mode-title" class="option-title">拆分方式</span>
              <SegmentedControl
                v-model="form.mode"
                :options="modeOptions"
                label="拆分方式"
                block
              />
              <p class="option-hint">{{ modeHint }}</p>

              <div v-if="form.mode !== 'none'" class="option-sub">
                <div class="option-sub-row">
                  <label class="option-sub-label" for="split-max">单行字数上限</label>
                  <input
                    id="split-max"
                    v-model.number="form.max_chars"
                    class="sh-field option-number"
                    type="number"
                    min="50"
                    max="2000"
                  />
                </div>
                <p class="option-hint">{{ limitHint }}</p>
              </div>
            </section>

            <section
              class="option-block"
              :class="{ 'is-muted': form.mode === 'none' }"
              role="group"
              aria-labelledby="dialogue-title"
            >
              <div class="option-heading">
                <span id="dialogue-title" class="option-title">识别对话</span>
                <p id="dialogue-help" class="option-hint">{{ dialogueHint }}</p>
              </div>

              <div
                v-if="form.mode !== 'none'"
                class="quote-tags"
                role="group"
                aria-labelledby="dialogue-title"
              >
                <button
                  v-for="item in quoteStyles"
                  :key="item.key"
                  type="button"
                  class="quote-tag"
                  :class="{ on: isQuoteOn(item.key), auto: item.key === 'auto' }"
                  :aria-pressed="isQuoteOn(item.key)"
                  @click="toggleQuote(item.key)"
                >
                  {{ item.label }}
                </button>
              </div>

              <p
                v-if="dialogueActive"
                class="option-hint quote-hint"
                :class="{ 'is-warn': form.quote === 'auto' }"
              >
                {{ quoteHint }}
              </p>
            </section>

            <p class="options-scope">{{ scopeHint }}</p>
          </div>

          <footer class="options-foot">
            <button class="sh-btn is-ghost" @click="$emit('close')">取消</button>
            <button class="sh-btn is-ghost" @click="save('')">保存</button>
            <button v-if="canResplit" class="sh-btn is-soft" @click="save('resplit')">
              重拆本章
            </button>
            <button class="sh-btn is-primary" @click="save('run')">{{ runLabel }}</button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, ref, watch } from 'vue'
import SegmentedControl from '../../components/ui/SegmentedControl.vue'
import { X } from 'lucide-vue-next'
import { loadSplitMeta, saveSplitOptions, state } from './store'
import { overlayTarget } from '../../composables/overlayTarget'
import { useModalA11y } from '../../composables/modalA11y'

const props = defineProps({
  open: { type: Boolean, default: false },
  canResplit: { type: Boolean, default: false },
  //: 已有行 / 章节数：决定主按钮的说法，以及是否要提示「这会重切全书」。
  rows: { type: Number, default: 0 },
  chapters: { type: Number, default: 0 },
})
const emit = defineEmits(['close', 'run', 'resplit'])
const panelEl = ref(/** @type {any} */ (null))
useModalA11y({ open: () => props.open, panel: panelEl, onClose: () => emit('close') })

const form = ref({ mode: 'rule', dialogue: true, quote: 'cn_double', max_chars: 200 })

const modeOptions = computed(() =>
  (state.splitMeta?.modes || []).map(
    /** @param {any} item */
    (item) => ({ label: item.label, value: item.key })
  )
)
const quoteStyles = computed(() => state.splitMeta?.quotes || [])
const sentenceMarks = computed(() => state.splitMeta?.sentence_marks || '。；！？!?…')
/** 不拆时整章只有一行，对话识别无从谈起：标签收起、说明改写。 */
const dialogueActive = computed(() => form.value.mode !== 'none' && form.value.dialogue)
const activeQuoteLabel = computed(
  () =>
    quoteStyles.value.find(
      /** @param {any} item */
      (item) => item.key === form.value.quote
    )?.label || '所选引号'
)

const modeHint = computed(() => {
  if (form.value.mode === 'paragraph') return '一段一行；超过字数的段落再按句拆开。'
  if (form.value.mode === 'none') return '整章只留一行，不再拆分，也不需要分析。'
  return `按句末标点 ${sentenceMarks.value} 切开；同一段内短句并到不超过字数上限，不跨段。`
})

const limitHint = computed(() =>
  form.value.mode === 'paragraph'
    ? '超过这个字数的段落才继续按句拆开。'
    : '同一段内，相邻短句会并到不超过这个字数；不跨段。'
)

const dialogueHint = computed(() => {
  if (form.value.mode === 'none') return '不拆时整章只有一行，不区分对白。'
  if (!form.value.dialogue) return '未选择：不看引号，所有行都按旁白处理。'
  return '已启用；点同一标签取消，点另一个标签换写法。'
})

const quoteHint = computed(() => {
  if (form.value.quote === 'auto') {
    return '自动会同时识别多种引号；一本书混用两种以上时容易误判，建议选定一种。'
  }
  if (form.value.mode === 'paragraph') {
    return `只认 ${activeQuoteLabel.value}；含台词的段落会先按句拆开。`
  }
  return `只认 ${activeQuoteLabel.value}；没有句读的引号（术语、引文）按旁白处理。`
})

/** 两个入口的差别只有范围：全部章节 / 本章，切行规则一致。 */
const runLabel = computed(() => (props.rows ? '重拆全部章节' : '开始拆句'))
const scopeHint = computed(() =>
  props.rows
    ? `现有 ${props.chapters} 章 ${props.rows} 行。文本未变的行保留角色、情绪与音频；文本改过的行重建。`
    : '按当前选项把每章正文切成行。'
)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    await loadSplitMeta()
    const options = state.detail?.split_options || {}
    form.value = {
      mode: options.mode || 'rule',
      dialogue: options.dialogue !== false,
      quote: options.quote || 'cn_double',
      max_chars: options.max_chars || 200,
    }
  }
)

/** @param {string} key */
function isQuoteOn(key) {
  return dialogueActive.value && form.value.quote === key
}

/** 标签即开关：选中一种就启用；再点同一个则取消，不选就不识别。 */
/** @param {string} key */
function toggleQuote(key) {
  if (form.value.mode === 'none') return
  if (form.value.dialogue && form.value.quote === key) {
    form.value.dialogue = false
    return
  }
  form.value.quote = key
  form.value.dialogue = true
}

/** @param {'' | 'close' | 'run' | 'resplit'} action */
async function save(action) {
  const saved = await saveSplitOptions({
    mode: form.value.mode,
    dialogue: form.value.mode === 'none' ? false : form.value.dialogue,
    quote: form.value.quote,
    max_chars: Number(form.value.max_chars) || 200,
  })
  if (!saved) return
  if (action === 'run') emit('run')
  else if (action === 'resplit') emit('resplit')
  else emit('close')
}
</script>

<style scoped>
.options-card {
  width: min(520px, 100%);
  padding: 20px 22px 16px;
  gap: 14px;
}
.options-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.options-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  color: var(--sh-text);
}
.options-note {
  margin: 3px 0 0;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
.options-body {
  display: grid;
  gap: 10px;
}
.option-block {
  display: grid;
  gap: 8px;
  padding: 13px 14px 14px;
  border: 1px solid var(--sh-line-soft);
  border-radius: 14px;
  background: var(--sh-glass-soft);
  transition: opacity 0.16s ease;
}
.option-block.is-muted {
  opacity: 0.72;
}
.option-title {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--sh-text-2);
}
.option-hint {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--sh-text-4);
}
.option-heading {
  display: grid;
  gap: 4px;
}
.quote-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.quote-tag {
  padding: 6px 10px;
  border: 1px solid var(--sh-line-soft);
  border-radius: 999px;
  background: var(--sh-panel);
  color: var(--sh-text-2);
  font: inherit;
  font-size: 11.5px;
  line-height: 1.35;
  white-space: nowrap;
  cursor: pointer;
  transition: border-color var(--sh-t-fast) var(--sh-ease),
    background var(--sh-t-fast) var(--sh-ease), color var(--sh-t-fast) var(--sh-ease);
}
.quote-tag:hover {
  border-color: var(--sh-line);
  background: var(--sh-glass-soft);
}
.quote-tag.on {
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}
.quote-tag:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--sh-accent-ring);
}
.quote-hint {
  margin-top: 1px;
}
.option-sub {
  display: grid;
  gap: 7px;
  margin-top: 2px;
  padding: 10px 11px 11px;
  border: 1px solid var(--sh-line-soft);
  border-radius: 10px;
  background: var(--sh-panel);
}
.option-sub-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.option-sub-label {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--sh-text-3);
}
.option-number {
  width: 108px;
  margin-left: auto;
}
.quote-hint.is-warn {
  color: var(--sh-warn);
}
.options-scope {
  margin: 0 2px;
  font-size: 11.2px;
  line-height: 1.55;
  color: var(--sh-text-4);
}
.options-foot {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
</style>
