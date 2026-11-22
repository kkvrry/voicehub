<template>
  <div
    class="srow"
    :class="{
      'is-on': selected,
      'is-picked': picked,
      'is-keep': !cue.dub,
      'is-play': playing,
    }"
    role="row"
    :aria-selected="selected"
    @click="pick"
    @contextmenu="onMenu"
  >
    <button
      type="button"
      class="c-idx"
      :class="{ 'is-picked': picked }"
      :aria-pressed="picked ? 'true' : 'false'"
      :title="picked ? '取消选择这一段' : '选择这一段（Shift 连选）'"
      :aria-label="picked ? `取消选择第 ${cue.ordinal} 段` : `选择第 ${cue.ordinal} 段`"
      @click.stop="onCheck"
    >
      {{ pad(cue.ordinal) }}
    </button>

    <span class="c-tc">
      <em>{{ tc(cue.start_ms) }}</em>
      <em class="end">{{ tc(cue.end_ms) }}</em>
    </span>

    <button
      ref="castEl"
      type="button"
      class="c-cast"
      :title="`${role?.name || '旁白'}（点击换角色）`"
      @click.stop="castOpen = true"
    >
      <span class="cast-name">{{ role?.name || '旁白' }}</span>
    </button>

    <textarea
      ref="textEl"
      v-model="draft"
      class="c-text"
      rows="2"
      spellcheck="false"
      data-app-menu
      :aria-label="`第 ${cue.ordinal} 段的文本`"
      :placeholder="cue.dub ? '这一句的字幕' : '这一句保留原声'"
      @click.stop
      @focus="onFocus"
      @blur="onBlur"
      @compositionstart="composing = true"
      @compositionend="onCompositionEnd"
      @keydown="onKey"
    ></textarea>

    <span class="c-st">
      <span class="st" :class="`st-${status.key}`" :title="status.hint">{{ status.label }}</span>
    </span>

    <span class="c-act">
      <button
        type="button"
        class="rbtn"
        :class="{ 'is-on': originalPlaying }"
        :title="originalPlaying ? '停下原声' : '试听这一段的原声'"
        :aria-label="originalPlaying ? '停下原声' : '试听这一段的原声'"
        @click.stop="$emit('audition', cue, 'original')"
      >
        <Pause v-if="originalPlaying" :size="14" :stroke-width="1.8" />
        <Volume2 v-else :size="14" :stroke-width="1.8" />
      </button>
      <button
        type="button"
        class="rbtn"
        :class="{ 'is-on': playing }"
        :disabled="!cue.dub || !cue.audio || cue.audio_ok === false"
        :title="voiceHint"
        :aria-label="playing ? '停下配音' : '试听这一段的配音'"
        @click.stop="$emit('audition', cue, 'dub')"
      >
        <Pause v-if="playing" :size="14" :stroke-width="1.8" />
        <Mic v-else :size="14" :stroke-width="1.8" />
      </button>
      <button
        type="button"
        class="rbtn is-more"
        title="更多操作"
        aria-label="更多操作"
        aria-haspopup="menu"
        @click.stop="$emit('menu', cue, $event)"
      >
        <MoreHorizontal :size="14" :stroke-width="1.8" />
      </button>
    </span>

    <Popover :open="castOpen" :anchor="castEl" :width="238" label="角色" @close="castOpen = false">
      <div class="cast-head">
        <span>角色</span>
        <span class="hint">决定这一句由谁来说；音色在左侧音色栏指定</span>
      </div>
      <div class="cast-roles">
        <button
          v-for="item in roleList"
          :key="item.id"
          type="button"
          class="cast-role"
          :class="{ 'is-on': (cue.role_id || roleList[0]?.id) === item.id }"
          @click="takeRole(item)"
        >
          <span class="name">{{ item.name }}</span>
          <span class="mode">{{ item.mode === 'tts' ? '配音' : '原声' }}</span>
        </button>
      </div>
    </Popover>
  </div>
</template>

<script setup>
// @ts-check
// 段落行：文本与角色/音色都在行内改，两枚按钮分别试听原声与配音。
// 行高固定（虚拟滚动按固定步长算位置），文本最多显示两行，超出在框内滚动。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Mic, MoreHorizontal, Pause, Volume2 } from 'lucide-vue-next'
import Popover from '../../../components/ui/Popover.vue'
import { state } from '../store'

const props = defineProps({
  cue: { type: Object, required: true },
  role: { type: Object, default: null },
  roles: { type: Array, default: () => [] },
  selected: { type: Boolean, default: false },
  picked: { type: Boolean, default: false },
  pending: { type: Boolean, default: false },
  playing: { type: Boolean, default: false },
  originalPlaying: { type: Boolean, default: false },
})
const emit = defineEmits(['pick', 'check', 'select', 'menu', 'audition', 'patch'])

const textEl = ref(/** @type {any} */ (null))
const castEl = ref(/** @type {any} */ (null))
const draft = ref(props.cue.text || '')
const castOpen = ref(false)
/** 输入法正在组字：这期间的 Enter / Esc 是在选词，不是"保存 / 撤销"。 */
let composing = false
let focused = false

/** 试听按钮的说法：旧音/上一次成功的那一版要说清，别让人以为改的音色没生效。 */
const voiceHint = computed(() => {
  const cue = props.cue
  if (!cue.dub) return '这一段保留原声：用左侧的原声键试听'
  if (!cue.audio) return '这一段还没有配音'
  if (cue.audio_ok === false) return '旧音频文件已不在磁盘上：重配这一段后恢复'
  if (props.playing) return '停下配音'
  if (cue.state === 'stale') return '试听配音（还是改之前那一版，重配后才会变）'
  if (cue.state === 'failed') return '试听配音（上一次成功的那一版）'
  return '试听这一段的配音'
})

const status = computed(() => {
  const cue = props.cue
  // 写库没成功：这一段还留在本机重试，别让它看起来"已经改好了"。
  if (props.pending) return { key: 'unsaved', label: '未保存', hint: '这次改动还没写进库，正在重试' }
  if (!cue.dub) return { key: 'keep', label: '原声', hint: '这一段保留源文件的声音' }
  if (cue.state === 'failed') {
    // 失败时旧产物还在（听得到上一版）：说清是"重配"失败，不是这一段没有声音。
    return {
      key: 'failed',
      label: cue.audio ? '重配失败' : '失败',
      hint: cue.error || '这一段的合成没有成功',
    }
  }
  if (cue.state === 'stale') return { key: 'stale', label: '待重配', hint: '文本或音色改过了，还是旧音频' }
  if (cue.state === 'ready' && cue.audio) return { key: 'ready', label: '已配音', hint: '' }
  return { key: 'empty', label: '未配音', hint: '还没有合成' }
})

// 别处的改动（贴齐、合成、撤销）要落到框里；但正在打的这一句不能被刷掉。
watch(
  () => props.cue.text,
  (text) => {
    if (!focused) draft.value = text || ''
  }
)

// props 里 Array 推出来是 unknown[]；按真实形状收窄一次。
const roleList = computed(() => /** @type {any[]} */ (props.roles))

/** @param {any} role */
function takeRole(role) {
  if ((props.cue.role_id || roleList.value[0]?.id) !== role.id) {
    emit('patch', props.cue, { role_id: role.id })
  }
  castOpen.value = false
}

/** @param {MouseEvent} event */
function pick(event) {
  emit('pick', props.cue, event)
}

/** @param {MouseEvent} event */
function onCheck(event) {
  emit('check', props.cue, event)
}

/** @param {MouseEvent} event */
function onMenu(event) {
  emit('menu', props.cue, event)
}

function onFocus() {
  focused = true
  // 正在改的这一句不能被轮询刷掉（store 会保住它的本地副本）。
  state.editing = props.cue.id
  // 只是选中，不搬播放头：点进文本是想改字，不是想从这一句开始听。
  emit('select', props.cue)
}

function onBlur() {
  focused = false
  commit()
  if (state.editing === props.cue.id) state.editing = ''
}

// 滚动把这一行移出可视窗口时，blur 不一定来：先落盘，别让刚敲的字跟着组件消失。
onBeforeUnmount(() => {
  if (focused) commit()
  if (state.editing === props.cue.id) state.editing = ''
})

function onCompositionEnd() {
  composing = false
}

/** @param {KeyboardEvent} event */
function onKey(event) {
  // 229 是 Chromium 给输入法按键的 keyCode（此时 key 可能仍报 Enter）。
  if (composing || event.isComposing || event.keyCode === 229) return
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    commit()
    return
  }
  if (event.key === 'Escape') {
    event.preventDefault()
    draft.value = props.cue.text || ''
    textEl.value?.blur()
  }
}

/** 只提交真的改过的句子；空文本当"改到一半"，放回原文，不写库。 */
function commit() {
  const raw = textEl.value?.value ?? draft.value
  const value = raw.trim()
  if (!value) {
    draft.value = props.cue.text || ''
    if (textEl.value) textEl.value.value = draft.value
    return
  }
  if (value === props.cue.text) return
  emit('patch', props.cue, { text: value })
}

/** @param {number} value */
function pad(value) {
  return String(value).padStart(3, '0')
}

/** @param {number} ms */
function tc(ms) {
  const total = Math.max(0, Math.round(ms / 10) * 10)
  const minutes = Math.floor(total / 60000)
  const rest = total % 60000
  return `${String(minutes).padStart(2, '0')}:${String(Math.floor(rest / 1000)).padStart(2, '0')},${String(rest % 1000).padStart(3, '0')}`
}
</script>

<style scoped>
.srow {
  display: grid;
  grid-template-columns: 40px 64px 128px minmax(0, 1fr) 52px 78px;
  align-items: center;
  gap: 6px;
  height: 52px;
  padding: 0 8px;
  border-bottom: 1px solid var(--sh-line-soft);
  color: var(--sh-text-2);
  cursor: default;
}

.srow:hover { background: var(--sh-hover); }

.srow.is-picked { background: var(--sh-accent-soft); }
.srow.is-picked:hover { background: var(--sh-accent-soft-2); }

.srow.is-on {
  background: var(--sh-accent-soft);
  box-shadow: inset 2px 0 0 var(--sh-accent);
}

.srow.is-keep .c-text { color: var(--sh-text-3); }

.c-idx {
  justify-self: start;
  min-width: 32px;
  height: 24px;
  padding: 0 5px;
  border-radius: 5px;
  font-family: var(--sh-mono);
  font-size: 11px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
  cursor: pointer;
}

.c-idx:hover {
  background: var(--sh-hover);
  color: var(--sh-accent);
}

.c-idx.is-picked {
  background: var(--sh-accent);
  color: var(--sh-accent-contrast);
  font-weight: 600;
}

.c-tc {
  display: flex;
  flex-direction: column;
  gap: 1px;
  font-family: var(--sh-mono);
  font-size: 10px;
  line-height: 1.35;
  color: var(--sh-text-2);
  font-variant-numeric: tabular-nums;
}

.c-tc em { font-style: normal; }
.c-tc .end { color: var(--sh-text-4); }

.c-cast {
  display: flex;
  align-items: center;
  width: 100%;
  padding: 4px 5px;
  border: 1px solid transparent;
  border-radius: 5px;
  text-align: left;
}

.c-cast:hover { border-color: var(--sh-line); background: var(--sh-field); }

.cast-name {
  min-width: 0;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--sh-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.c-text {
  box-sizing: border-box;
  width: 100%;
  height: 42px;
  padding: 2px 6px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--sh-text);
  font: inherit;
  font-size: 12.5px;
  line-height: 1.5;
  resize: none;
  overflow-y: auto;
}

.c-text:hover { border-color: var(--sh-line); background: var(--sh-field); }

.c-text:focus {
  outline: none;
  border-color: var(--sh-accent-ring);
  background: var(--sh-field-focus);
}

.c-st { text-align: right; }

.st {
  font-size: 10.5px;
  color: var(--sh-text-3);
  white-space: nowrap;
}

.st-ready { color: var(--sh-ok); }
.st-stale { color: var(--sh-warn); }
.st-failed { color: var(--sh-danger); }
.st-keep { color: var(--sh-text-4); }
.st-unsaved { color: var(--sh-warn); font-weight: 600; }

.c-act {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
}

.rbtn {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 5px;
  color: var(--sh-text-4);
}

.rbtn:hover:not(:disabled) { background: var(--sh-chip); color: var(--sh-accent); }
.rbtn.is-on { color: var(--sh-accent); background: var(--sh-accent-soft); }
.rbtn:disabled { opacity: 0.35; cursor: default; }
.rbtn.is-more { opacity: 0; }

.srow:hover .rbtn.is-more,
.srow.is-on .rbtn.is-more,
.rbtn.is-more:focus-visible { opacity: 1; }

/* ---- 角色与音色弹层 ---- */
.cast-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--sh-text-3);
}

.cast-head .hint { margin-left: auto; font-weight: 400; font-size: 10px; color: var(--sh-text-4); }

.cast-roles { display: flex; flex-wrap: wrap; gap: 4px; }

.cast-role {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
  padding: 4px 8px;
  border: 1px solid var(--sh-line);
  border-radius: 999px;
  background: var(--sh-field);
  color: var(--sh-text-2);
  font-size: 11.5px;
}

.cast-role:hover { border-color: var(--sh-accent-ring); color: var(--sh-text); }
.cast-role.is-on { border-color: var(--sh-accent); background: var(--sh-accent-soft); color: var(--sh-text); }
.cast-role .name { font-weight: 600; }
.cast-role .mode { font-size: 10px; color: var(--sh-text-4); }
</style>
