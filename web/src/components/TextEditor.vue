<template>
  <div class="text-editor">
    <!-- 正文框只管写字；语音输入那颗在标题行（生成页由外面摆，见 SynthPanel）。 -->
    <div class="te-field">
      <!-- 单层：文字就是这一个 contenteditable，`[[…]]` 的底色由 `::highlight()`
           画在同一层文字下面。浏览器只排一次版，没有第二个排版层可漂移。
           回车与粘贴自己接管，内容永远是纯文本（不含块元素），
           这样 DOM 里的文字与模型逐字相等，高亮的区间坐标才靠得住。 -->
      <div
        ref="box"
        class="te-text"
        role="textbox"
        aria-multiline="true"
        aria-label="正文"
        contenteditable="plaintext-only"
        spellcheck="false"
        @input="onInput"
        @keydown="onKeydown"
        @paste.prevent="onPaste"
        @drop.prevent
        @compositionstart="onCompositionStart"
        @compositionend="onCompositionEnd"
      ></div>
    </div>

    <!-- "不是字"的东西（笑声、咳嗽、停多久）用统一标签写：[[笑]] / [[停顿:0.4]]。
         一组一行：先是「通用」（支持语气词的引擎的交集），再是各台引擎多出来的那几条。
         分组由后端算好，这个组件不认识任何标签名字。
         `marks` 关掉时不画这一排：生成页把它摆到正文下面（MarksInline），
         别的用法（参数窗口）照旧挂在下面。 -->
    <div v-if="marks && groupList.length" class="mark-bar">
      <div v-for="group in groupList" :key="group.key || 'common'" class="mark-group">
        <span class="mark-group-label">{{ group.label }}</span>
        <div class="mark-group-items">
          <button
            v-for="mark in group.marks"
            :key="group.key + ':' + mark.name"
            type="button"
            class="mark-btn"
            :class="{ off: mark.missing.length, own: Boolean(group.key) }"
            :title="markTitle(mark, group)"
            @click="insert(mark)"
          >
            {{ mark.label }}
          </button>
          <MarkCustom v-if="group.any" @insert="insert" />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
// @ts-check
/** 正文那一格：编辑框 + 语气词标签（生成页与参数窗口同一个实现）。
 *  `groups` 由 `store/engine.markGroupsOf` 递进来；分组与勾了哪台引擎无关。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import MarkCustom from './MarkCustom.vue'
import { markRanges } from './marks'
import { clearMarks, paintMarks } from './markPaint'

const props = defineProps({
  groups: { type: Array, default: () => [] },
  /** 语气词那一排摆不摆在这里。生成页把它挪到正文下面，所以传 false。 */
  marks: { type: Boolean, default: true },
})

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const groupList = computed(() => /** @type {any[]} */ (props.groups))

const text = defineModel({ type: String, default: '' })
const box = ref(/** @type {any} */ (null))

/** 高亮注册表是文档级的，这个标记只用来区分实例（见 markPaint）。 */
const owner = Symbol('te-marks')
let composing = false

/* ── 模型 ↔ DOM ─────────────────────────────────────────────── */

/** 内容始终是纯文本（回车插入 `\n`、粘贴只取 text/plain），所以 textContent
 *  就是真值，不需要 innerText 那套块元素换算。 */
function readDom() {
  return box.value ? box.value.textContent || '' : ''
}

function onInput() {
  // 组合期间不回写：模型一变就会触发 watch，反过来重设 DOM，把输入法打断。
  if (composing) return
  text.value = readDom()
  paint()
}

function onCompositionStart() {
  composing = true
}

function onCompositionEnd() {
  composing = false
  onInput()
}

/** 外部改值（载入文稿、识别结果、切换项目）：只在真的不一致时回写，
 *  否则每次输入都会重设 textContent，光标被推回末尾。 */
watch(text, (value) => {
  const el = box.value
  if (!el || readDom() === value) return
  el.textContent = value ?? ''
  paint()
})

onMounted(() => {
  if (!box.value) return
  box.value.textContent = text.value ?? ''
  paint()
})

onBeforeUnmount(() => clearMarks(owner))

/* ── 高亮 ───────────────────────────────────────────────────── */

function paint() {
  if (!box.value) return
  paintMarks(owner, box.value, markRanges(readDom()))
}

/* ── 写入（光标处） ─────────────────────────────────────────── */

function caretRange() {
  const el = box.value
  if (!el) return null
  const selection = window.getSelection()
  if (selection && selection.rangeCount) {
    const current = selection.getRangeAt(0)
    if (el.contains(current.commonAncestorContainer)) return current
  }
  const range = document.createRange()
  range.selectNodeContents(el)
  range.collapse(false)
  return range
}

/**
 * 光标在正文里的偏移：插空格、判断"挨不挨着字"都要靠它。
 *
 * @param {any} el
 */
function caretOffset(el) {
  const range = caretRange()
  if (!range) return readDom().length
  const probe = range.cloneRange()
  probe.selectNodeContents(el)
  probe.setEnd(range.startContainer, range.startOffset)
  return probe.toString().length
}

/**
 * 唯一往正文里塞字的地方：插完光标停在新内容之后，再同步模型与高亮。
 * 不走 execCommand：它能记进撤销栈，但在「行尾换行之后」插入时会把字插到换行
 * 前面（Chromium 按可见位置算插入点），光标与内容就此对不上。这里自己插节点，
 * 位置绝对确定——代价是这类插入不进撤销栈。
 *
 * @param {any} value
 */
function type(value) {
  const piece = String(value ?? '')
  const el = box.value
  if (!el || !piece) return
  el.focus()
  const selection = window.getSelection()
  const range = caretRange()
  if (!selection || !range) return
  range.deleteContents()
  const node = document.createTextNode(piece)
  range.insertNode(node)
  const after = document.createRange()
  after.setStart(node, node.length)
  after.collapse(true)
  selection.removeAllRanges()
  selection.addRange(after)
  text.value = readDom()
  paint()
}

/**
 * @param {any} event
 */
function onKeydown(event) {
  // Enter 自己插 `\n`：交给浏览器会生成 <div>/<br>，纯文本这个前提就没了。
  // 组合中的回车是「选词」，不能拦。
  if (event.isComposing || composing) return
  if (event.key !== 'Enter') return
  event.preventDefault()
  type('\n')
}

/**
 * @param {any} event
 */
function onPaste(event) {
  const piece = event.clipboardData?.getData('text/plain') ?? ''
  if (piece) type(piece)
}

/**
 * 悬停时说什么：先写清它是什么，再说这次有谁会收不到它。
 *
 * @param {any} mark
 * @param {any} group
 */
function markTitle(mark, group) {
  const what = mark.arg ? `[[${mark.name}:0.4]]` : `[[${mark.name}]]`
  const lines = [what]
  if (group.note) lines.push(group.note)
  if (mark.missing.length) lines.push(`${mark.missing.join('、')} 不认它，会从正文里删掉`)
  return lines.join('\n')
}

/**
 * 插在**光标处**，不是追加到末尾：写正文的人知道这句话该从哪里开始笑。
 *
 * @param {any} mark
 */
function insert(mark) {
  type(mark.arg ? `[[${mark.name}:0.4]]` : `[[${mark.name}]]`)
}

/**
 * 识别出来的话也插在**光标处**：与插标签同一条规矩，念的是"接着这里往下说"。
 * 前后补空格只在真的挨着字的时候：粘贴一整句到空框里不该两头多出空格。
 *
 * @param {any} value
 */
function insertText(value) {
  const piece = String(value || '').trim()
  const el = box.value
  if (!piece || !el) return
  const before = readDom().slice(0, caretOffset(el))
  const lead = before && !before.endsWith(' ') && !before.endsWith('\n') ? ' ' : ''
  type(`${lead}${piece}`)
}

/** 右栏/标题行要把标签与识别结果插进**这个**输入框：把"插在光标处"这条规矩
 *  留在本组件里（它是唯一知道输入框在哪的那一方），外面只递内容进来。 */
defineExpose({ insert, insertText })
</script>
