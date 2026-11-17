<!-- 服务：实例卡片 + 每台自己的端口/启停/自启/日志；只读接入的旧配置只保留断开。 -->
<template>
  <section v-if="!data.endpoint" class="acp-service">
    <div class="acp-bar">
      <div class="acp-filter">
        <SegmentedControl v-model="filter" :options="filterOptions" label="筛选实例" />
      </div>
      <div class="acp-bar-actions">
        <ActionButton size="sm" variant="ghost" :loading="refreshing" @click="reload">
          <RefreshCw v-if="!refreshing" :size="13" :stroke-width="1.8" aria-hidden="true" />
          刷新
        </ActionButton>
        <ActionButton
          size="sm"
          variant="primary"
          :loading="state.busy === 'start:null'"
          :disabled="!canStartAll"
          @click="startAll"
        >
          <Play v-if="state.busy !== 'start:null'" :size="13" :stroke-width="1.8" aria-hidden="true" />
          全部启动
        </ActionButton>
        <ActionButton
          size="sm"
          variant="ghost"
          :loading="state.busy === 'stop:null'"
          :disabled="!ownedRunning"
          @click="stopAll"
        >
          <Square v-if="state.busy !== 'stop:null'" :size="13" :stroke-width="1.8" aria-hidden="true" />
          全部停止
        </ActionButton>
      </div>
    </div>

    <div v-if="visibleRows.length" class="acp-groups">
      <section v-for="group in visibleGroups" :key="group.key" class="acp-group">
        <header class="acp-group-head">
          <h3>{{ group.label }}</h3>
          <span class="acp-group-count">{{ group.rows.length }}</span>
        </header>
        <div class="acp-grid">
          <article
            v-for="row in group.rows"
            :key="row.port"
            class="acp-card"
            :class="`is-${statusKey(row)}`"
            :data-family="row.family"
            :data-port="row.port"
            :data-source="row.source"
          >
            <header class="acp-card-head">
              <span
                class="acp-status-dot"
                :class="`is-${statusKey(row)}`"
                :title="statusLabel(row)"
                aria-hidden="true"
              />
              <div class="acp-card-title">
                <h3 :title="row.label">{{ row.label }}</h3>
                <span
                  v-if="statusKey(row) === 'external'"
                  class="acp-state is-external"
                >{{ statusLabel(row) }}</span>
              </div>
              <ActionButton
                v-if="!row.running"
                variant="icon"
                size="sm"
                class="acp-run"
                :loading="state.busy === `start:${row.port}`"
                :disabled="!data.can_start"
                :title="`启动 ${row.label}`"
                :aria-label="`启动 ${row.label}`"
                @click="start(row.port)"
              >
                <Play v-if="state.busy !== `start:${row.port}`" :size="14" :stroke-width="2.2" aria-hidden="true" />
              </ActionButton>
              <ActionButton
                v-else-if="!row.external"
                variant="icon"
                size="sm"
                class="acp-run is-stop"
                :loading="state.busy === `stop:${row.port}`"
                :title="`停止 ${row.label}`"
                :aria-label="`停止 ${row.label}`"
                @click="stop(row.port)"
              >
                <Square v-if="state.busy !== `stop:${row.port}`" :size="12" :stroke-width="2" aria-hidden="true" />
              </ActionButton>
            </header>

            <div class="acp-port">
              <span class="acp-port-label">端口</span>
              <template v-if="editingPort === row.port">
                <input
                  v-model="portDraft"
                  class="sh-field acp-port-input"
                  inputmode="numeric"
                  :aria-label="`${row.label} 端口`"
                  @keydown.enter.prevent="commitPort(row)"
                  @keydown.esc.prevent="cancelPort"
                  @blur="commitPort(row)"
                />
              </template>
              <template v-else>
                <code class="acp-port-value">:{{ row.port }}</code>
                <button
                  v-if="!row.external"
                  type="button"
                  class="sh-btn is-icon is-sm acp-port-edit"
                  :disabled="state.busy === 'port'"
                  :title="`编辑 ${row.label} 端口`"
                  :aria-label="`编辑 ${row.label} 端口`"
                  @click="beginPort(row)"
                >
                  <Pencil :size="13" :stroke-width="1.8" />
                </button>
              </template>
            </div>

            <div v-if="row.running" class="acp-card-actions">
              <a
                class="sh-btn is-primary is-sm is-block acp-main"
                :href="row.url"
                target="_blank"
                rel="noreferrer"
                title="打开这台实例自带的界面"
              >
                <ExternalLink :size="13" :stroke-width="1.8" aria-hidden="true" />
                打开界面
              </a>
            </div>

            <footer class="acp-card-foot">
              <ToggleSwitch
                :model-value="autoSet.has(row.model)"
                :label="`自动拉起 ${row.label}`"
                :title="autoTitle(row)"
                :disabled="autoDisabled(row)"
                @update:model-value="toggleAuto(row.model)"
              />
              <span class="acp-auto-label">自动拉起</span>
              <ActionButton size="sm" variant="ghost" class="acp-log" @click="openLog(row)">
                <Terminal :size="13" :stroke-width="1.8" aria-hidden="true" />
                日志
              </ActionButton>
            </footer>
          </article>
        </div>
      </section>
    </div>
    <p v-else class="acp-empty">没有符合条件的实例。</p>

    <Teleport :to="overlayTarget">
      <Transition name="acp-drawer">
        <div v-if="state.showLog" class="acp-log-root" @click.self="closeLog" @keydown.esc.stop="closeLog">
          <section
            ref="logPanel"
            class="acp-log-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="acp-log-title"
            tabindex="-1"
          >
            <header class="acp-log-head">
              <div class="acp-log-meta">
                <h3 id="acp-log-title">实例日志</h3>
                <span v-if="logRow" class="acp-log-sub">{{ logRow.label }} · :{{ logRow.port }}</span>
              </div>
              <select v-model.number="state.logPort" class="sh-field acp-log-select" aria-label="选择实例日志">
                <option v-for="row in rows" :key="row.port" :value="row.port">
                  {{ row.label }} · :{{ row.port }}
                </option>
              </select>
              <ActionButton variant="icon" size="sm" title="关闭日志" @click="closeLog">
                <X :size="15" :stroke-width="1.8" aria-hidden="true" />
              </ActionButton>
            </header>
            <p v-if="logRow?.external" class="acp-log-note">
              外部进程：这份日志可能来自之前插件拉起的同名实例，不一定是当前进程。
            </p>
            <pre class="acp-log-pre">{{ logText }}</pre>
            <footer class="acp-log-foot">每台引擎的 audiocpp_server stdout / stderr</footer>
          </section>
        </div>
      </Transition>
    </Teleport>
  </section>

  <PanelCard v-else title="服务">
    <template #head>
      <span class="acp-dot" :class="connected ? 'is-on' : ''" />
      <span class="sh-hint">{{ statusText }}</span>
      <a v-if="uiUrl" class="sh-btn is-soft is-sm" :href="uiUrl" target="_blank" rel="noreferrer">
        打开界面 ↗
      </a>
    </template>

    <div class="acp-row">
      <span class="acp-label">已接入</span>
      <code class="acp-grow acp-url">{{ data.endpoint }}</code>
      <ActionButton size="sm" variant="ghost" :loading="state.busy === 'endpoint'" @click="clearEndpoint">断开</ActionButton>
    </div>
    <p class="acp-note">{{ endpointNote }}</p>
  </PanelCard>
</template>

<script setup>
// @ts-check
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ExternalLink, Pencil, Play, RefreshCw, Square, Terminal, X } from 'lucide-vue-next'
import ActionButton from '../../../components/ui/ActionButton.vue'
import PanelCard from '../../../components/ui/PanelCard.vue'
import SegmentedControl from '../../../components/ui/SegmentedControl.vue'
import ToggleSwitch from '../../../components/ui/ToggleSwitch.vue'
import { overlayTarget } from '../../../composables/overlayTarget'
import { refresh, saveAutoStart, savePort, setEndpoint, start, state, stop } from '../store'

const data = computed(() => state.data)
const rows = computed(() => data.value?.instances || [])
const connected = computed(() => Boolean(data.value?.service?.reachable))
const runningCount = computed(() => rows.value.filter(/** @param {any} row */ (row) => row.running).length)
const stoppedCount = computed(() => rows.value.length - runningCount.value)
const ownedRunning = computed(() => rows.value.filter(/** @param {any} row */ (row) => row.owned && row.running).length)
const autoSet = computed(() => new Set(data.value?.auto_start || []))
const uiUrl = computed(() => (connected.value ? data.value?.service?.url || '' : ''))
const statusText = computed(() => {
  const service = data.value?.service
  if (!data.value?.endpoint) return ''
  if (!service?.reachable) return '连不上'
  return `v${service.version} · ${service.backend} · ${service.management ? '可管理' : '只读'}`
})
const endpointNote = computed(() => {
  const service = data.value?.service
  if (!service?.reachable) return '地址暂时连不上：确认服务已启动，或检查端口。'
  if (!service.management) {
    return '它没开模型管理，这里只能合成：装模型请点上面的「打开界面」，或断开后回到实例视图。'
  }
  return '只读接入一台服务，不拉进程、不改它的模型目录。'
})

const filter = ref('all')
const refreshing = ref(false)
const editingPort = ref(0)
const portDraft = ref('')
const logPanel = ref(/** @type {any} */ (null))
/** @type {Element | null} */
let logReturnFocus = null

const filterOptions = computed(() => [
  { value: 'all', label: `全部 ${rows.value.length}` },
  { value: 'running', label: `运行中 ${runningCount.value}` },
  { value: 'stopped', label: `已停止 ${stoppedCount.value}` },
])

const visibleRows = computed(() => {
  if (filter.value === 'running') return rows.value.filter(/** @param {any} row */ (row) => row.running)
  if (filter.value === 'stopped') return rows.value.filter(/** @param {any} row */ (row) => !row.running)
  return rows.value
})

const GROUP_ORDER = [
  'tts',
  'asr',
  'speech_analysis',
  'audio_generation',
  'audio_tools',
  'voice_conversion',
  'community',
  'other',
]

/** 分类来自官方清单的 category：同一类服务排在一起，缺分类的落到「其他」。 */
const visibleGroups = computed(() => {
  const buckets = new Map()
  for (const row of visibleRows.value) {
    const key = row.category || 'other'
    let bucket = buckets.get(key)
    if (!bucket) {
      bucket = { key, label: row.category_label || '其他', rows: [] }
      buckets.set(key, bucket)
    }
    bucket.rows.push(row)
  }
  /** @param {string} key */
const rank = (key) => {
    const at = GROUP_ORDER.indexOf(key)
    return at < 0 ? 99 : at
  }
  return [...buckets.values()].sort((a, b) => rank(a.key) - rank(b.key))
})

const canStartAll = computed(
  () => Boolean(data.value?.can_start) && rows.value.some(/** @param {any} row */ (row) => !row.running)
)

const logRow = computed(
  () => rows.value.find(/** @param {any} row */ (row) => row.port === state.logPort) || rows.value[0] || null
)
const logText = computed(() => logRow.value?.log || '没有日志。')

/**
 * @param {any} row
 */
function statusKey(row) {
  return row.external ? 'external' : row.running ? 'running' : 'stopped'
}

/**
 * @param {any} row
 */
function statusLabel(row) {
  if (row.external) return '外部进程'
  return row.running ? '运行中' : '已停止'
}

/**
 * @param {any} row
 */
function autoDisabled(row) {
  return !row.ready || row.external || state.busy === 'auto'
}

/**
 * @param {any} row
 */
function autoTitle(row) {
  if (!row.ready) return '模型未安装在本机，不能自动拉起'
  if (row.external) return '外部进程不归插件管理'
  if (state.busy === 'auto') return '正在保存自动拉起设置'
  return 'VoiceHub 启动时自动拉起这台实例'
}

async function reload() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await refresh()
  } finally {
    refreshing.value = false
  }
}

function startAll() {
  start(null)
}

function stopAll() {
  stop(null)
}

/**
 * @param {any} row
 */
async function beginPort(row) {
  if (row.external || state.busy === 'port') return
  editingPort.value = row.port
  portDraft.value = String(row.port)
  await nextTick()
  const input = /** @type {HTMLInputElement | null} */ (
    document.querySelector('.acp-port-input')
  )
  input?.focus()
  input?.select()
}

function cancelPort() {
  editingPort.value = 0
  portDraft.value = ''
}

/**
 * @param {any} row
 */
async function commitPort(row) {
  if (editingPort.value !== row.port) return
  const value = Number(portDraft.value)
  editingPort.value = 0
  portDraft.value = ''
  if (value !== row.port) await savePort(row, value)
}

/**
 * @param {any} model
 */
function toggleAuto(model) {
  const next = autoSet.value.has(model)
    ? [...autoSet.value].filter(/** @param {any} item */ (item) => item !== model)
    : [...autoSet.value, model]
  saveAutoStart(next)
}

/**
 * @param {any} row
 */
async function openLog(row) {
  logReturnFocus = document.activeElement
  state.logPort = row.port
  state.showLog = true
  await nextTick()
  logPanel.value?.focus()
}

async function closeLog() {
  state.showLog = false
  await nextTick()
  if (logReturnFocus instanceof HTMLElement) logReturnFocus.focus()
}

async function clearEndpoint() {
  state.endpointDirty = false
  await setEndpoint('')
}

watch(
  () => data.value?.endpoint,
  (endpoint) => {
    if (endpoint) state.showLog = false
  }
)

onBeforeUnmount(() => {
  state.showLog = false
})
</script>

<style scoped>
.acp-service {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.acp-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 14px;
  padding: 0 0 12px;
}
.acp-filter {
  display: flex;
  min-width: 0;
}
.acp-bar-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.acp-groups {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.acp-group-head {
  display: flex;
  align-items: baseline;
  gap: 7px;
  padding: 0 2px 7px;
}
.acp-group-head h3 {
  margin: 0;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--sh-text-3);
}
.acp-group-count {
  font-size: 10.5px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}

.acp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
  gap: var(--sh-gap-3, 12px);
}
.acp-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-height: 147px;
  padding: 14px 15px 12px;
  border-radius: var(--sh-radius-card, 18px);
  border: 1px solid var(--sh-glass-border);
  background: linear-gradient(
    165deg,
    rgba(255, 255, 255, 0.86) 0%,
    rgba(255, 255, 255, 0.62) 52%,
    rgba(246, 251, 255, 0.6) 100%
  );
  backdrop-filter: blur(var(--sh-blur, 20px)) saturate(140%);
  -webkit-backdrop-filter: blur(var(--sh-blur, 20px)) saturate(140%);
  box-shadow: var(--sh-inset-hi), var(--sh-shadow-1);
  overflow: hidden;
  transition:
    transform var(--sh-t) var(--sh-ease),
    box-shadow var(--sh-t) var(--sh-ease),
    border-color var(--sh-t) var(--sh-ease);
}
:root[data-theme='dark'] .acp-card {
  background: linear-gradient(165deg, rgba(25, 39, 62, 0.72), rgba(17, 28, 46, 0.58));
}
.acp-card:hover {
  transform: translateY(-1px);
  box-shadow: var(--sh-inset-hi), var(--sh-shadow-2);
  border-color: color-mix(in srgb, var(--sh-accent) 22%, var(--sh-glass-border));
}
.acp-card::before {
  content: '';
  position: absolute;
  inset: 0 0 auto;
  height: 3px;
  background: transparent;
}
.acp-card.is-running::before {
  background: linear-gradient(90deg, var(--sh-ok), color-mix(in srgb, var(--sh-ok) 22%, transparent));
}
.acp-card.is-external {
  border-color: color-mix(in srgb, var(--sh-warn) 20%, var(--sh-glass-border));
}
.acp-card.is-external::before {
  background: repeating-linear-gradient(90deg, var(--sh-warn) 0 6px, transparent 6px 12px);
}
.acp-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.acp-status-dot {
  position: relative;
  width: 8px;
  height: 8px;
  flex: none;
  border-radius: 50%;
  background: var(--sh-text-4);
}
.acp-status-dot.is-running {
  background: var(--sh-ok);
}
.acp-status-dot.is-running::after {
  content: '';
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  background: var(--sh-ok);
  opacity: 0.18;
  animation: acp-pulse 2.2s var(--sh-ease) infinite;
}
.acp-status-dot.is-external {
  background: var(--sh-warn);
}
@keyframes acp-pulse {
  0%,
  100% {
    transform: scale(0.8);
    opacity: 0.12;
  }
  50% {
    transform: scale(1.35);
    opacity: 0.24;
  }
}
.acp-card-title {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex: 1 1 auto;
  min-width: 0;
}
.acp-card-head .acp-run {
  flex: none;
  margin-left: auto;
  width: 30px;
  height: 30px;
  padding: 0;
  border-radius: 50%;
  border: 1px solid transparent;
  color: #fff;
  background: var(--sh-accent);
}
.acp-card-head .acp-run:hover:not(:disabled) {
  background: color-mix(in srgb, var(--sh-accent) 86%, #000);
}
.acp-card-head .acp-run.is-stop {
  color: var(--sh-danger);
  background: var(--sh-danger-soft);
  border-color: color-mix(in srgb, var(--sh-danger) 28%, transparent);
}
.acp-card-head .acp-run.is-stop:hover:not(:disabled) {
  background: color-mix(in srgb, var(--sh-danger) 18%, transparent);
}
.acp-card-title h3 {
  margin: 0;
  font-size: 13.5px;
  font-weight: 700;
  letter-spacing: -0.005em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.acp-state {
  font-size: 10.5px;
  color: var(--sh-text-4);
  white-space: nowrap;
}
.acp-state.is-external {
  color: var(--sh-warn);
}
.acp-port {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 32px;
  padding: 3px 5px 3px 9px;
  border-radius: var(--sh-r-sm);
  border: 1px solid var(--sh-line-soft);
  background: var(--sh-field);
}
.acp-port-label {
  font-size: 10.5px;
  color: var(--sh-text-4);
}
.acp-port-value {
  font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
  color: var(--sh-text-2);
  font-variant-numeric: tabular-nums;
}
.acp-port-input {
  width: 110px;
  height: 26px;
  padding: 0 7px;
  font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.acp-port-edit {
  width: 26px;
  height: 26px;
  margin-left: auto;
}
.acp-card-actions {
  display: flex;
  align-items: center;
  gap: 7px;
}
.acp-card-actions .acp-main {
  flex: 1 1 auto;
}
.acp-card-foot {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: auto;
  padding-top: 9px;
  border-top: 1px solid var(--sh-line-soft);
}
.acp-auto-label {
  font-size: 11px;
  color: var(--sh-text-3);
  white-space: nowrap;
}
.acp-log {
  margin-left: auto;
}
.acp-empty {
  margin: 18px 0;
  padding: 16px;
  border: 1px dashed var(--sh-line);
  border-radius: var(--sh-radius-card);
  color: var(--sh-text-3);
  font-size: 12px;
  text-align: center;
}

/* ------------------------------------------------------------------ 旧只读接入 */
.acp-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sh-gap-2, 8px);
  min-height: 40px;
  padding: 4px 0;
  border-bottom: 1px solid var(--sh-line-soft);
}
.acp-label {
  flex: none;
  width: 72px;
  font-size: 12px;
  color: var(--sh-text-3);
}
.acp-grow {
  flex: 1 1 220px;
  min-width: 0;
}
.acp-url {
  font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
  color: var(--sh-text-2);
  overflow-wrap: anywhere;
}
.acp-note {
  margin: 6px 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--sh-text-3);
}
.acp-dot {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--sh-text-4);
}
.acp-dot.is-on {
  background: var(--sh-ok, #35a06a);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--sh-ok, #35a06a) 18%, transparent);
}

/* ------------------------------------------------------------------ 日志抽屉 */
.acp-log-root {
  position: fixed;
  inset: 0;
  z-index: 45;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding: 22px;
  background: rgba(16, 31, 56, 0.26);
  backdrop-filter: blur(7px) saturate(120%);
  -webkit-backdrop-filter: blur(7px) saturate(120%);
}
:root[data-theme='dark'] .acp-log-root {
  background: rgba(4, 10, 20, 0.58);
}
.acp-log-panel {
  display: flex;
  flex-direction: column;
  width: min(900px, 100%);
  max-height: min(520px, 72vh);
  border-radius: var(--sh-radius-card);
  border: 1px solid var(--sh-glass-border);
  background: var(--sh-glass-strong);
  backdrop-filter: blur(var(--sh-blur-strong, 30px)) saturate(140%);
  -webkit-backdrop-filter: blur(var(--sh-blur-strong, 30px)) saturate(140%);
  box-shadow: var(--sh-shadow-2);
  overflow: hidden;
  outline: none;
}
.acp-log-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 14px;
  border-bottom: 1px solid var(--sh-line-soft);
}
.acp-log-meta {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}
.acp-log-meta h3 {
  margin: 0;
  font-size: 12.5px;
  font-weight: 700;
  color: var(--sh-text);
}
.acp-log-sub {
  font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
  font-size: 10.5px;
  color: var(--sh-text-3);
}
.acp-log-select {
  width: 220px;
  height: 30px;
  margin-left: auto;
  font-size: 11.5px;
}
.acp-log-note {
  margin: 0;
  padding: 8px 14px;
  color: var(--sh-warn);
  background: var(--sh-warn-soft);
  font-size: 11px;
  line-height: 1.5;
}
.acp-log-pre {
  flex: 1;
  min-height: 150px;
  margin: 0;
  padding: 13px 16px;
  overflow: auto;
  background: #0b1524;
  color: #d7e5ff;
  font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.acp-log-foot {
  padding: 8px 14px;
  border-top: 1px solid var(--sh-line-soft);
  color: var(--sh-text-4);
  font-size: 10.5px;
}
.acp-drawer-enter-active,
.acp-drawer-leave-active {
  transition: opacity var(--sh-t) var(--sh-ease);
}
.acp-drawer-enter-from,
.acp-drawer-leave-to {
  opacity: 0;
}
.acp-drawer-enter-active .acp-log-panel,
.acp-drawer-leave-active .acp-log-panel {
  transition: transform var(--sh-t-slow) var(--sh-ease-out);
}
.acp-drawer-enter-from .acp-log-panel,
.acp-drawer-leave-to .acp-log-panel {
  transform: translateY(14px);
}

@media (max-width: 780px) {
  .acp-bar {
    align-items: flex-start;
  }
  .acp-filter {
    width: 100%;
  }
  .acp-filter :deep(.sh-seg) {
    display: flex;
    width: 100%;
  }
  .acp-filter :deep(.sh-seg-btn) {
    flex: 1;
  }
  .acp-bar-actions {
    width: 100%;
    margin-left: 0;
  }
  .acp-bar-actions :deep(.sh-btn) {
    flex: 1;
  }
  .acp-grid {
    grid-template-columns: 1fr;
  }
  .acp-log-root {
    padding: 0;
  }
  .acp-log-panel {
    width: 100%;
    max-height: 78vh;
    border-radius: var(--sh-radius-card) var(--sh-radius-card) 0 0;
  }
  .acp-log-head {
    flex-wrap: wrap;
  }
  .acp-log-select {
    width: 100%;
    margin-left: 0;
    order: 3;
  }
}
</style>
