<template>
  <aside class="dub-col dub-flow" aria-label="工作流">
    <div class="dub-col-head">
      <span class="t">工作流</span>
      <span class="n">{{ steps.length }} 步</span>
    </div>

    <ol class="flow">
      <li v-for="(step, index) in steps" :key="step.key" class="flow-node" :class="`is-${step.state}`">
        <div class="flow-top">
          <span class="flow-index">{{ pad(index + 1) }}</span>
          <span class="flow-name">{{ step.label }}</span>
          <span class="flow-state">{{ STATE_LABEL[step.state] || step.state }}</span>
        </div>
        <div class="flow-sum">{{ step.summary }}</div>
        <div v-if="step.state === 'run' && running" class="flow-bar">
          <i :style="{ width: progressWidth }"></i>
        </div>
        <div v-if="actionOf(step.key)" class="flow-actions">
          <button
            class="sh-btn is-soft is-small"
            type="button"
            :disabled="Boolean(state.busy) || !actionOf(step.key).enabled"
            @click="actionOf(step.key).run()"
          >{{ actionOf(step.key).label }}</button>
        </div>
      </li>
    </ol>

    <div class="dub-col-foot">
      <label class="dub-field">
        <span>原声音量 <b>{{ gainLabel }}</b></span>
        <input
          type="range"
          min="-60"
          max="0"
          step="1"
          :value="gain"
          :disabled="!project"
          @change="onGain($event.target.value)"
        />
      </label>
      <p class="dub-hint">
        人声分离、变声与配乐走独立的变换域，当前版本未接入；本页只做「转写 → 重配」这一条链路。
      </p>
    </div>
  </aside>
</template>

<script setup>
// 右列：步骤状态与唯一的主动作。状态由后端算（detail.steps），界面不自己推。
import { computed } from 'vue'
import {
  assemble,
  exportFilm,
  mixReady,
  project,
  refresh,
  saveOriginalGain,
  state,
  steps,
  synthAll,
  transcribe,
} from '../store'
import * as api from '../api'

const STATE_LABEL = { done: '完成', run: '运行中', wait: '等待', off: '未启用' }

const running = computed(() => state.detail?.job?.state === 'running')
const progressWidth = computed(() => {
  const current = state.detail?.job
  if (!current?.total) return '8%'
  return `${Math.min(100, Math.round((current.done / current.total) * 100))}%`
})

const gain = computed(() => Number(project.value?.options?.original_gain ?? -60))
const gainLabel = computed(() =>
  gain.value <= -60 ? '静音' : `${gain.value > 0 ? '+' : ''}${gain.value} dB`
)

function onGain(value) {
  void saveOriginalGain(Number(value))
}

async function cancel(kind) {
  try {
    if (kind === 'transcribe') await api.cancelTranscribe(state.currentId)
    else await api.cancelSynth(state.currentId)
    await refresh({ silent: true })
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error)
  }
}

/** 每一步能不能点、点了做什么；没有动作的步骤返回 null。 */
function actionOf(key) {
  const busy = Boolean(state.busy) || running.value
  if (key === 'transcribe') {
    if (running.value && state.detail?.job?.kind === 'transcribe') {
      return { label: '取消', enabled: true, run: () => cancel('transcribe') }
    }
    const ready = state.detail?.asr?.ready
    return {
      // 已经有段落时这一下会整批替换：说清是"重新"，不让人以为是补几段。
      label: (state.detail?.stats?.total || 0) > 0 ? '重新转录' : '转录',
      enabled: !busy && Boolean(project.value) && ready,
      run: () => transcribe(),
    }
  }
  if (key === 'synth') {
    if (running.value && state.detail?.job?.kind === 'synth') {
      return { label: '取消', enabled: true, run: () => cancel('synth') }
    }
    const stats = state.detail?.stats || { total: 0, ready: 0 }
    const hasCues = (state.detail?.cues || []).length > 0
    return {
      label: stats.ready > 0 && stats.ready < stats.total ? '合成剩余' : '开始配音',
      enabled: !busy && hasCues,
      run: () => synthAll(),
    }
  }
  if (key === 'align') {
    const hasAudio = (state.detail?.cues || []).some((cue) => cue.audio)
    return { label: '贴齐', enabled: !busy && hasAudio, run: () => assemble() }
  }
  if (key === 'export') {
    return {
      label: '导出',
      enabled: !busy && !state.exporting && mixReady.value,
      run: () => exportFilm(),
    }
  }
  return null
}

function pad(value) {
  return String(value).padStart(2, '0')
}
</script>

<style scoped>
.dub-flow {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.flow {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  margin: 0;
  padding: 0 10px 8px;
  list-style: none;
}

.flow-node {
  position: relative;
  padding: 8px 10px 9px 12px;
  border-left: 1px solid var(--sh-line);
  margin-left: 6px;
}

.flow-node::before {
  content: '';
  position: absolute;
  left: -4px;
  top: 15px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--sh-line);
  box-shadow: 0 0 0 3px var(--sh-panel);
}

.flow-node.is-done::before { background: var(--sh-ok); }
.flow-node.is-run::before { background: var(--sh-accent); }
.flow-node.is-off { opacity: 0.55; }

.flow-top {
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.flow-index {
  font-family: var(--sh-mono, ui-monospace, monospace);
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.flow-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--sh-text);
}

.flow-state {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--sh-text-3);
}

.is-run .flow-state { color: var(--sh-accent); }
.is-done .flow-state { color: var(--sh-ok); }

.flow-sum {
  margin-top: 2px;
  font-size: 11px;
  color: var(--sh-text-3);
  line-height: 1.5;
  word-break: break-word;
}

.flow-bar {
  margin-top: 6px;
  height: 3px;
  border-radius: 2px;
  background: var(--sh-track, rgba(120, 148, 186, 0.22));
  overflow: hidden;
}

.flow-bar i {
  display: block;
  height: 100%;
  background: var(--sh-accent);
  transition: width 240ms var(--sh-ease, ease);
}

.flow-actions {
  margin-top: 6px;
}

.flow-actions .sh-btn {
  height: 24px;
  padding: 0 10px;
  font-size: 11.5px;
}

.dub-col-foot {
  flex: none;
  padding: 10px 14px 12px;
  border-top: 1px solid var(--sh-line-soft);
}

.dub-field {
  display: block;
  font-size: 11px;
  color: var(--sh-text-3);
}

.dub-field b {
  color: var(--sh-text);
  font-weight: 600;
  margin-left: 4px;
  font-variant-numeric: tabular-nums;
}

.dub-field input {
  width: 100%;
  margin-top: 4px;
  accent-color: var(--sh-accent);
}

.dub-hint {
  margin: 8px 0 0;
  font-size: 10.5px;
  line-height: 1.6;
  color: var(--sh-text-4);
}
</style>
