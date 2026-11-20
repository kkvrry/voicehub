<template>
  <!-- 日志页：右侧主区那一页。四段并排，正文才拿得到地方；
       入口在左栏（LogEntry）。 -->
  <div class="logview">
    <header class="logview-head">
      <h1>
        日志
        <!-- 两个数是两件事：看得见的 / 手里一共有的，"筛到 0 条"与"一条都没有"分得开。 -->
        <span class="result-count">{{ shown.length }} / {{ lines.length }} 行</span>
      </h1>
      <span class="spacer" />
      <!-- 两句都是如实说：缺了一段就说缺了，断了就说断了。 -->
      <span v-if="store.logs.dropped" class="hint" data-tone="warn">
        中间有一段没接住——环形转过一圈了，更早的那些请到日志文件里翻。
      </span>
      <span v-else-if="!store.logs.connected" class="hint" data-tone="warn">
        连接断了，正在重连……
      </span>
      <span v-else class="live"><span class="live-dot" aria-hidden="true" />实时</span>
    </header>

    <PanelCard flush class="logview-card">
      <div class="logview-tools">
        <div class="levels">
          <ChipToggle
            v-for="l in levels"
            :key="l.key"
            v-model="l.on"
            :label="l.label"
            :tone="toneOf(l.key)"
          />
        </div>
        <!-- 来源候选**按收到的行动态去重**：新来源自动出现，零配置。 -->
        <select v-model="source" class="sh-field logview-source" aria-label="来源">
          <option value="">全部来源</option>
          <option v-for="s in sources" :key="s" :value="s">{{ s }}</option>
        </select>
        <input
          v-model="needle"
          class="sh-field logview-needle"
          placeholder="搜索正文"
          aria-label="搜索正文"
        />
        <span class="spacer" />
        <ActionButton size="sm" @click="actions.clearLogs()">清屏</ActionButton>
        <ActionButton size="sm" @click="exportView()">导出</ActionButton>
      </div>

      <div ref="box" class="logview-list" @scroll="onScroll()">
        <!-- 表头与行共用同一套列宽（CSS 里那一处变量说了算），写两遍迟早错开一格。
             注意模板注释必须用 Vue 自己的注释格式收口：写成 JS 的注释尾巴不会收口，
             会把后面一整段吞进注释里，而编译器一声不响。 -->
        <div class="ln ln-head">
          <span>时间</span>
          <span>级别</span>
          <span>来源</span>
          <span>正文</span>
        </div>
        <div v-if="!shown.length" class="empty-line">当前条件下没有日志。</div>
        <div
          v-for="l in shown"
          :key="l.seq || l.at + l.message"
          class="ln"
          :class="l.level"
          @contextmenu="openLineMenu(l, $event)"
        >
          <!-- 只显示到秒；完整时间在 title 里，要贴给别人用「导出」那一份。 -->
          <span class="at" :title="l.at">{{ logClock(l.at) }}</span>
          <span class="lv">{{ levelText(l.level) }}</span>
          <span class="src" :title="l.source || 'unknown'">{{ l.source || 'unknown' }}</span>
          <span class="msg">
            <template v-if="needleText">
              <template v-for="(part, index) in highlightParts(l.message)" :key="index">
                <mark v-if="part.hit" class="log-hit">{{ part.text }}</mark>
                <template v-else>{{ part.text }}</template>
              </template>
            </template>
            <template v-else>{{ l.message }}</template>
          </span>
        </div>
      </div>
    </PanelCard>
  </div>
</template>

<script setup>
// @ts-check
import { computed, nextTick, ref, watch } from 'vue'
import { store, actions } from '../store'
import ActionButton from './ui/ActionButton.vue'
import ChipToggle from './ui/ChipToggle.vue'
import PanelCard from './ui/PanelCard.vue'
import { openObjectMenu } from '../composables/contextMenu'

/** debug 默认关：它是诊断细节，开着会把真正要看的行冲掉。 */
const LEVELS = [
  { key: 'debug', label: '调试', on: false },
  { key: 'info', label: '信息', on: true },
  { key: 'warning', label: '警告', on: true },
  { key: 'error', label: '错误', on: true },
]

/** 级别在行内只占两个汉字，与勾选框同一套词。 */
const LEVEL_LABEL = { debug: '调试', info: '信息', warning: '警告', error: '错误' }

const levels = ref(LEVELS.map((l) => ({ ...l })))
const source = ref('')
const needle = ref('')
/** 用户向上滚开过 = 暂停跟随；回到底部自动恢复，不给"继续"按钮。 */
const pinned = ref(true)
const box = ref(/** @type {any} */ (null))

/**
 * 只显示到秒：日期在这一页里没人看，挤掉的是正文的位置。
 *
 * @param {any} iso
 */
function logClock(iso) {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return '--:--:--'
  const pad = (/** @type {number} */ n) => String(n).padStart(2, '0')
  return `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`
}

/**
 * @param {any} level
 */
function levelText(level) {
  return /** @type {Record<string, string>} */ (LEVEL_LABEL)[level] || level || ''
}

/**
 * 级别 chip 的色调跟行内的左边框一致：警告橙、错误红，其余中性。
 *
 * @param {any} level
 */
function toneOf(level) {
  if (level === 'warning') return 'warn'
  if (level === 'error') return 'danger'
  return 'neutral'
}

const lines = computed(() => store.logs.lines || [])

/** 来源候选按收到的行动态去重，零配置——去重表由 log.js 增量维护，
 *  这里只排序（候选就几个，比每条日志重算一遍整表便宜）。 */
const sources = computed(() => [...store.logSources].sort())
const needleText = computed(() => needle.value.trim())
const needleLower = computed(() => needleText.value.toLowerCase())

/** 过滤在内存里做，不防抖：一千行以内一帧滤完，加延时只是让输入变钝。 */
const shown = computed(() => {
  const on = new Set(levels.value.filter((l) => l.on).map((l) => l.key))
  const want = needleLower.value
  return lines.value.filter((line) => {
    if (!on.has(line.level || 'info')) return false
    if (source.value && (line.source || 'unknown') !== source.value) return false
    if (want && !(line.message || '').toLowerCase().includes(want)) return false
    return true
  })
})

/**
 * 把正文按搜索词切成命中段与普通段；没有搜索词时整段原样返回。
 * @param {any} message
 * @returns {{ text: string, hit: boolean }[]}
 */
function highlightParts(message) {
  const value = String(message || '')
  const need = needleLower.value
  if (!need) return [{ text: value, hit: false }]
  const parts = []
  let from = 0
  while (from < value.length) {
    const at = value.toLowerCase().indexOf(need, from)
    if (at < 0) break
    if (at > from) parts.push({ text: value.slice(from, at), hit: false })
    const end = at + needleText.value.length
    parts.push({ text: value.slice(at, end), hit: true })
    from = end
  }
  if (from < value.length) parts.push({ text: value.slice(from), hit: false })
  return parts
}

/** @param {any} line @param {MouseEvent} event */
function openLineMenu(line, event) {
  openObjectMenu(
    'log.line',
    {
      text: `${line.at} [${(line.level || '').toUpperCase()}] ${line.source || 'unknown'} ${line.message}`,
      source: line.source || '',
      level: line.level || '',
      onlySource: () => {
        source.value = line.source || 'unknown'
      },
      onlyLevel: () => {
        const keep = line.level || 'info'
        levels.value = levels.value.map((item) => ({ ...item, on: item.key === keep }))
      },
    },
    { event }
  )
}



function toBottom() {
  const el = box.value
  if (el) el.scrollTop = el.scrollHeight
}

/** 用户滚了：离底部超过一行高就暂停跟随，回到附近就恢复。 */
function onScroll() {
  const el = box.value
  if (!el) return
  pinned.value = el.scrollHeight - el.scrollTop - el.clientHeight < 24
}

/** 导出按当前过滤下载；时间写全，贴出去之后没有界面给它翻译。 */
function exportView() {
  const text = shown.value
    .map((l) => `${l.at} [${(l.level || '').toUpperCase()}] ${l.source || 'unknown'} ${l.message}`)
    .join('\n')
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'voicehub-log.txt'
  a.click()
  URL.revokeObjectURL(url)
}

watch(
  () => shown.value.length,
  async () => {
    if (!pinned.value) return
    await nextTick()
    toBottom()
  }
)
// 进这一页就贴到底：新来的行在下面，而"先看到最新那条"是打开日志最自然的要求。
watch(
  () => store.view,
  async (view) => {
    if (view !== 'logs') return
    await nextTick()
    toBottom()
  },
  { immediate: true }
)
</script>
