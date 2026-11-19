<template>
  <section class="app-panel vc">
    <header class="panel-head">
      <div class="panel-head-main">
        <div class="engine-title">音色处理</div>
        <div class="muted">{{ scopeLabel() }}</div>
      </div>
      <div class="vc-head">
        <button v-if="running" class="sh-btn" type="button" @click="onCancel">取消</button>
        <button
          v-else
          class="sh-btn is-primary"
          type="button"
          :disabled="!canStart"
          @click="onStart"
        >
          开始处理
        </button>
      </div>
    </header>

    <div v-if="error" class="vc-error vc-body">{{ error }}</div>
    <div v-else-if="!loaded" class="muted vc-body">载入中…</div>

    <div v-else class="vc-body">
      <div v-if="!ready" class="vc-warn-line">{{ reason }}</div>

      <!-- 当前目标：左栏选中什么，这里就是什么。条上只有三件事——试听、换样例、清除。 -->
      <div v-if="voice" class="vc-target">
        <button
          type="button"
          class="vc-target-play"
          :class="{ on: playingTarget }"
          :disabled="!targetUrl"
          :title="playingTarget ? '暂停' : '试听这一段'"
          :aria-label="playingTarget ? '暂停' : '试听这一段'"
          @click="playTarget"
        >
          <Square v-if="playingTarget" :size="12" fill="currentColor" />
          <Play v-else :size="14" fill="currentColor" />
        </button>
        <div class="vc-target-main">
          <div class="vc-target-line">
            <span class="vc-target-name">{{ voice.shown_name }}</span>
            <span class="vc-target-tag">{{ store.pick.file ? '单条样例' : '整条音色' }}</span>
            <span v-if="!isLocal" class="vc-target-tag is-warn">平台音色</span>
          </div>
          <div class="vc-target-sub">{{ targetSub() }}</div>
        </div>
        <button type="button" class="sh-btn is-soft" :disabled="!hasSamples" @click="changeSample">
          换样例
        </button>
        <button type="button" class="sh-btn is-ghost" @click="clearTarget">清除</button>
      </div>

      <div v-else class="vc-empty">
        <span class="vc-empty-title">从左侧选一个音色</span>
        <span class="vc-empty-hint">
          点卡片即处理它的全部样例；卡右「选样例」可以只处理一条。
        </span>
      </div>

      <div v-if="voice && !isLocal" class="vc-warn-line">
        平台音色的样例在引擎目录里，换掉会让平台侧的参考音频指向被替换的文件。请选本地音色。
      </div>

      <div class="vc-steps" :class="{ 'is-idle': !voice }">
        <article
          v-for="step in steps"
          :key="step.key"
          class="vc-card"
          :class="{ off: !pickOf(step).enabled }"
        >
          <label class="vc-check">
            <input v-model="pickOf(step).enabled" type="checkbox" />
            <span class="box" aria-hidden="true"></span>
            <span class="vc-name">{{ step.label }}</span>
            <span class="vc-order">步骤 {{ step.order }}</span>
          </label>
          <div class="vc-body-row">
            <select v-model="pickOf(step).model" class="sh-field" :aria-label="`${step.label}模型`">
              <option v-for="model in step.models" :key="model.id" :value="model.id">
                {{ model.label }}
              </option>
            </select>
            <span v-if="!installedOf(step)" class="vc-warn">未下载</span>
            <button
              v-if="!installedOf(step)"
              type="button"
              class="sh-btn is-soft"
              :disabled="state.installing !== ''"
              @click="installModel(pickOf(step).model)"
            >
              {{ state.installing ? '下载中' : '安装' }}
            </button>
            <span class="vc-note">{{ noteOf(step) }}</span>
          </div>
        </article>

        <article class="vc-card is-core">
          <div class="vc-check">
            <span class="box on" aria-hidden="true"></span>
            <span class="vc-name">规整</span>
            <span class="vc-order">步骤 {{ core.order }} · 核心</span>
          </div>
          <div class="vc-body-row">
            <span class="vc-note">{{ core.note }}</span>
          </div>
        </article>
      </div>

      <div v-if="job" class="vc-progress">
        <div class="vc-bar"><i :style="{ width: `${percent}%` }"></i></div>
        <span class="vc-hint">{{ progressText }}</span>
      </div>

      <div v-if="items.length" class="vc-results">
        <div class="vc-results-head">
          <span>结果 · {{ job?.scope?.label || '' }}</span>
          <span v-if="job?.state === 'running'" class="vc-hint">处理中，先别切换目标</span>
        </div>
        <div class="vc-row is-head">
          <span class="c-file">样例</span>
          <span class="c-dur">时长</span>
          <span class="c-play">试听</span>
          <span class="c-pick">替换</span>
        </div>
        <div v-for="item in items" :key="item.index" class="vc-row" :class="item.state">
          <span class="c-file" :title="item.file">{{ item.file }}</span>
          <span class="c-dur">{{ durationText(item) }}</span>
          <span class="c-play">
            <button
              type="button"
              class="vc-play"
              :class="{ on: state.playing === `${item.index}:origin` }"
              @click="play(item.index, 'origin')"
            >
              原声
            </button>
            <button
              type="button"
              class="vc-play"
              :class="{ on: state.playing === `${item.index}:result` }"
              :disabled="item.state !== 'done'"
              @click="play(item.index, 'result')"
            >
              处理
            </button>
          </span>
          <span class="c-pick">
            <label v-if="item.replaceable && item.state === 'done'" class="vc-check sm">
              <input
                type="checkbox"
                :checked="state.picked.includes(String(item.index))"
                @change="togglePick(item.index)"
              />
              <span class="box" aria-hidden="true"></span>
            </label>
            <span v-else class="vc-reason">{{ item.error || STATE_LABELS[item.state] || item.state }}</span>
          </span>
        </div>

        <div class="vc-foot">
          <button type="button" class="sh-btn" @click="selectAll">全选可替换</button>
          <button
            type="button"
            class="sh-btn is-primary"
            :disabled="!state.picked.length || busy !== ''"
            @click="applyPicked"
          >
            替换已选 {{ state.picked.length }}
          </button>
          <span v-if="state.applied" class="vc-hint">
            已替换 {{ state.applied.replaced }} 条{{ failures ? `，${failures} 条失败` : '' }}。原件在回收站。
          </span>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
// @ts-check
// 工作区只有一屏：目标音频条 → 步骤卡 → 进度 → 结果。
// 处理目标来自左栏的选中（`store.pick`），这一页不另设范围切换。
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { Play, Square } from 'lucide-vue-next'
import { store } from '../../store'
import {
  applyPicked,
  cancel,
  changeSample,
  clearTarget,
  installModel,
  loadTarget,
  play,
  playTarget,
  refresh,
  reset,
  scopeLabel,
  selectAll,
  start,
  state,
  targetSample,
  targetSub,
  targetVoice,
  togglePick,
} from './store'

/** @type {Record<string, string>} */
const STATE_LABELS = { pending: '等待', done: '完成', applied: '已替换', failed: '失败', skipped: '跳过' }

const loaded = computed(() => state.loaded)
const error = computed(() => state.error)
const job = computed(() => state.job)
const steps = computed(() => state.data?.steps || [])
const core = computed(() => state.data?.core || { order: 3, note: '' })
const items = computed(() => state.job?.items || [])
const ready = computed(() => Boolean(state.data?.ready))
const reason = computed(() => state.data?.reason || '')
const running = computed(() => state.job?.state === 'running')
const busy = computed(() => state.busy || '')
const failures = computed(() => state.applied?.failures?.length || 0)

/** 左栏选中的那条音色：详情到了带样例，没到就先用列表项（够画名字）。 */
const voice = computed(() => targetVoice())
const isLocal = computed(() => voice.value?.is_local !== false)
const hasSamples = computed(() => {
  const detail = state.target
  if (detail?.id === store.pick.voiceId) return (detail.samples || []).length > 0
  return (voice.value?.sample_count || 0) > 0
})
const canStart = computed(
  () => ready.value && Boolean(voice.value) && isLocal.value && hasSamples.value && busy.value === ''
)

const targetUrl = computed(() => targetSample()?.audio_url || '')
const playingTarget = computed(
  () => Boolean(targetUrl.value) && store.player.url === targetUrl.value && store.player.playing
)

const percent = computed(() => {
  const current = state.job
  if (!current?.total) return 0
  return Math.round((current.finished / current.total) * 100)
})
const progressText = computed(() => {
  const current = state.job
  if (!current) return ''
  if (current.state === 'running') return `处理中 ${current.finished} / ${current.total}`
  if (current.state === 'cancelled') return `已取消，完成 ${current.finished} / ${current.total}`
  if (current.state === 'applied') return `已替换 ${state.applied?.replaced ?? 0} 条`
  return `已完成 ${current.finished} / ${current.total}${current.failed ? ` · ${current.failed} 条失败` : ''}`
})

/** @param {any} step */
function pickOf(step) {
  if (!state.picks[step.key]) state.picks[step.key] = { enabled: false, model: '' }
  return state.picks[step.key]
}

/** @param {any} step */
function installedOf(step) {
  const model = step.models.find((/** @type {any} */ row) => row.id === pickOf(step).model)
  return Boolean(model?.installed)
}

/** @param {any} step */
function noteOf(step) {
  const model = step.models.find((/** @type {any} */ row) => row.id === pickOf(step).model)
  return model?.note || step.note
}

/** @param {any} item */
function durationText(item) {
  if (!item.origin_seconds) return '—'
  const before = item.origin_seconds.toFixed(1)
  const after = item.result_seconds ? item.result_seconds.toFixed(1) : '—'
  return `${before} → ${after} s`
}

function onStart() {
  start()
}

function onCancel() {
  cancel()
}

watch(
  () => state.job?.state,
  (value) => {
    if (value === 'done') selectAll()
  },
)

// 左栏换了目标：取回样例。换的只是样例时不必重拉整包，`loadTarget` 自己会判断。
watch(
  () => [store.pick.voiceId, store.pick.file],
  () => {
    void loadTarget()
  },
)

onMounted(() => {
  state.picked = []
  void refresh()
  void loadTarget()
})

onBeforeUnmount(reset)
</script>

<style scoped>
.vc {
  display: flex;
  flex-direction: column;
  gap: 0;
  min-height: 0;
}
.vc-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px 22px 24px;
  display: flex;
  flex-direction: column;
  gap: var(--sh-gap-3);
}
.vc-error {
  color: var(--sh-danger);
}
.vc-head {
  display: flex;
  align-items: center;
  gap: var(--sh-gap-2);
}
.vc-head .sh-btn[disabled] {
  opacity: 0.5;
}
.vc-warn-line {
  font-size: 12px;
  color: var(--sh-warn);
  background: var(--sh-warn-soft);
  border-radius: var(--sh-r-xs);
  padding: 6px 10px;
}

/* 目标音频条：选中谁、处理哪一段，全在这条上说完。 */
.vc-target {
  display: flex;
  align-items: center;
  gap: var(--sh-gap-3);
  padding: 8px 10px;
  border: 0.5px solid var(--sh-line);
  border-radius: var(--sh-radius-inner);
  background: var(--sh-panel);
  box-shadow: var(--sh-inset-hi);
}
.vc-target-play {
  width: 32px;
  height: 32px;
  flex: none;
  border: 0;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--sh-accent);
  color: var(--sh-accent-contrast);
  cursor: pointer;
}
.vc-target-play.on {
  background: var(--sh-accent-soft-2);
  color: var(--sh-accent);
  box-shadow: inset 0 0 0 1px var(--sh-accent);
}
.vc-target-play:disabled {
  background: var(--sh-line-soft);
  color: var(--sh-text-4);
  cursor: not-allowed;
}
.vc-target-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.vc-target-line {
  display: flex;
  align-items: center;
  gap: var(--sh-gap-2);
  min-width: 0;
}
.vc-target-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--sh-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vc-target-tag {
  flex: none;
  font-size: 10px;
  line-height: 1.6;
  padding: 0 6px;
  border-radius: var(--sh-r-2xs);
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.vc-target-tag.is-warn {
  color: var(--sh-warn);
  background: var(--sh-warn-soft);
}
.vc-target-sub {
  font-size: 11px;
  color: var(--sh-text-3);
}
.vc-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 22px 16px;
  border: 0.5px dashed var(--sh-line);
  border-radius: var(--sh-radius-inner);
  text-align: center;
}
.vc-empty-title {
  font-size: 13px;
  color: var(--sh-text-2);
}
.vc-empty-hint {
  font-size: 11px;
  color: var(--sh-text-4);
}

.vc-steps {
  display: flex;
  flex-direction: column;
  gap: var(--sh-gap-2);
}
/* 没选目标之前步骤卡只是"将要做什么"的说明：照样画出来，但不接受编辑。 */
.vc-steps.is-idle {
  opacity: 0.55;
  pointer-events: none;
}
.vc-card {
  border: 0.5px solid var(--sh-line);
  border-radius: var(--sh-radius-inner);
  padding: 10px 12px;
  background: var(--sh-panel);
}
.vc-card.off {
  border-color: var(--sh-line-soft);
  background: transparent;
}
.vc-card.is-core {
  background: transparent;
  border-style: dashed;
}
.vc-check {
  display: flex;
  align-items: center;
  gap: var(--sh-gap-2);
  cursor: pointer;
  font-size: 13px;
}
.vc-check input {
  position: absolute;
  opacity: 0;
  width: 1px;
  height: 1px;
}
.vc-check .box {
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 0.5px solid var(--sh-line);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
}
.vc-check input:checked + .box,
.vc-check .box.on {
  background: var(--sh-accent);
  border-color: var(--sh-accent);
}
.vc-check input:checked + .box::after,
.vc-check .box.on::after {
  content: '';
  width: 7px;
  height: 4px;
  border-left: 1.6px solid #fff;
  border-bottom: 1.6px solid #fff;
  transform: translateY(-1px) rotate(-45deg);
}
.vc-check.sm .box {
  width: 14px;
  height: 14px;
}
.vc-name {
  font-weight: 500;
}
.vc-order {
  font-size: 11px;
  color: var(--sh-text-4);
}
.vc-body-row {
  display: flex;
  align-items: center;
  gap: var(--sh-gap-2);
  flex-wrap: wrap;
  margin-top: var(--sh-gap-2);
  padding-left: 22px;
}
/* 下拉不铺满：后面还要站「未下载 / 安装 / 一句取舍」，铺满会把它们挤到下一行。 */
.vc-body-row .sh-field {
  width: auto;
  flex: 1 1 200px;
  min-width: 0;
  max-width: 360px;
  height: 32px;
  font-size: 12px;
}
.vc-note {
  font-size: 11px;
  color: var(--sh-text-3);
}
.vc-warn {
  font-size: 11px;
  color: var(--sh-warn);
}
.vc-hint {
  font-size: 12px;
  color: var(--sh-text-3);
}
.vc-progress {
  display: flex;
  align-items: center;
  gap: var(--sh-gap-3);
}
.vc-bar {
  flex: 1;
  height: 4px;
  border-radius: 2px;
  background: var(--sh-line-soft);
  overflow: hidden;
}
.vc-bar i {
  display: block;
  height: 100%;
  background: var(--sh-accent);
  transition: width 0.2s linear;
}
.vc-results {
  border-top: 0.5px solid var(--sh-line-soft);
  padding-top: var(--sh-gap-2);
}
.vc-results-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sh-gap-2);
  font-size: 11px;
  color: var(--sh-text-4);
  padding-bottom: 4px;
}
.vc-row {
  display: flex;
  align-items: center;
  gap: var(--sh-gap-3);
  padding: 8px 2px;
  border-bottom: 0.5px solid var(--sh-line-soft);
  font-size: 12px;
}
.vc-row.is-head {
  font-size: 11px;
  color: var(--sh-text-4);
  padding-bottom: 6px;
}
.vc-row.failed .c-file {
  color: var(--sh-text-3);
}
.c-file {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.c-dur {
  width: 96px;
  flex: none;
  font-size: 11px;
  color: var(--sh-text-3);
}
.c-play {
  width: 120px;
  flex: none;
  display: flex;
  gap: var(--sh-gap-1);
}
.c-pick {
  width: 90px;
  flex: none;
  display: flex;
  justify-content: flex-end;
}
.vc-play {
  border: 0.5px solid var(--sh-line);
  background: transparent;
  color: var(--sh-text-3);
  font: inherit;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--sh-r-2xs);
  cursor: pointer;
}
.vc-play.on {
  color: var(--sh-accent);
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.vc-play:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.vc-reason {
  font-size: 11px;
  color: var(--sh-warn);
  text-align: right;
}
.vc-foot {
  display: flex;
  align-items: center;
  gap: var(--sh-gap-2);
  padding-top: var(--sh-gap-3);
}
</style>
