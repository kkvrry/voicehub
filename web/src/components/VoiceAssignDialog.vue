<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="close">
        <div class="vh-modal assign-card" role="dialog" aria-modal="true" aria-label="指定音色">
          <header class="assign-head">
            <div class="assign-heading">
              <h2 class="assign-title">指定音色</h2>
              <p class="assign-note">
                <span v-if="voiceName" class="assign-voice">{{ voiceName }}</span>
                <span v-if="voiceName && engineLabel" class="assign-sep">·</span>
                <span v-if="engineLabel" class="assign-engine">{{ engineLabel }}</span>
              </p>
            </div>
            <button class="sh-btn is-icon" title="关闭" @click="close">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="assign-body">
            <!-- 目标摆成两栏台账：未指定在左（先做该做的），已指定在右（要改就点开覆盖）。
                 哪一栏空了就整栏收起——空着一整列只会把另一栏挤窄。
                 行数跟在名称后，试听是行末一整格高，行与行之间用细线分账。 -->
            <div v-if="roles.length" class="assign-row is-block">
              <span class="assign-label">{{ scopeLabel }}</span>
              <div class="role-ledger" :class="{ 'is-single': !openRoles.length || !setRoles.length }">
                <section v-if="openRoles.length" class="ledger-col">
                  <header class="ledger-head">
                    <span>未指定</span>
                    <span class="ledger-count">{{ openRoles.length }}</span>
                  </header>
                  <div class="ledger-list" role="radiogroup" :aria-label="`未指定${scopeLabel}`">
                    <button
                      v-for="item in openRoles"
                      :key="item.key"
                      type="button"
                      role="radio"
                      class="ledger-row"
                      :class="{ 'is-on': role === item.key }"
                      :aria-checked="role === item.key"
                      :aria-label="roleTitle(item)"
                      :title="roleTitle(item)"
                      @click="role = item.key"
                    >
                      <span class="ledger-main">
                        <span class="ledger-name">
                          <span class="ledger-role">{{ item.name }}</span>
                          <span v-if="item.mode" class="ledger-mode">
                            {{ item.mode === 'original' ? '原声' : '配音' }}
                          </span>
                          <span class="ledger-rows">{{ item.rows }} {{ unit }}</span>
                        </span>
                      </span>
                    </button>
                  </div>
                </section>

                <section v-if="setRoles.length" class="ledger-col">
                  <header class="ledger-head">
                    <span>已指定</span>
                    <span class="ledger-count">{{ setRoles.length }}</span>
                  </header>
                  <div class="ledger-list" role="radiogroup" :aria-label="`已指定${scopeLabel}`">
                    <div
                      v-for="item in setRoles"
                      :key="item.key"
                      class="ledger-row has-action"
                      :class="{ 'is-on': role === item.key }"
                      role="radio"
                      :aria-checked="role === item.key"
                      :aria-label="roleTitle(item)"
                      :title="roleTitle(item)"
                      tabindex="0"
                      @click="role = item.key"
                      @keydown.enter.self.prevent="role = item.key"
                      @keydown.space.self.prevent="role = item.key"
                    >
                      <span class="ledger-main">
                        <span class="ledger-name">
                          <i class="ledger-dot" aria-hidden="true" />
                          <span class="ledger-role">{{ item.name }}</span>
                          <span v-if="item.mode" class="ledger-mode">
                            {{ item.mode === 'original' ? '原声' : '配音' }}
                          </span>
                          <span class="ledger-rows">{{ item.rows }} {{ unit }}</span>
                        </span>
                        <span class="ledger-voice">
                          <span class="ledger-voice-name">{{ assignedName(item.key) }}</span>
                        </span>
                      </span>
                      <button
                        v-if="previewUrl(item.key)"
                        type="button"
                        class="voice-play"
                        :class="{ 'is-playing': isVoicePlaying(item.key) }"
                        :title="isVoicePlaying(item.key) ? '停止' : '试听这条音色'"
                        aria-label="试听这条音色"
                        @click.stop="toggleVoice(item.key)"
                      >
                        <Square v-if="isVoicePlaying(item.key)" :size="9" fill="currentColor" />
                        <Play v-else :size="10" fill="currentColor" />
                      </button>
                      <span v-else class="voice-play is-empty" aria-hidden="true" />
                    </div>
                  </div>
                </section>
              </div>
            </div>
            <p v-else class="assign-empty">还没有可指定的{{ scopeLabel }}。</p>

            <div class="assign-row">
              <span class="assign-label">引擎</span>
              <EngineSelect
                v-if="engines.length"
                id="assign-engine"
                :value="engineKey"
                :options="engines"
                label="引擎"
                @change="pickEngine"
              />
              <!-- 平台音色的引擎由平台决定，没有第二台可换：写明，别让人以为漏了；
                   本地音色一台可用引擎都没有时也要说清，并禁掉「指定」。 -->
              <span
                v-else
                class="assign-static"
                :class="{ 'is-missing': isLocal }"
                :title="
                  isLocal
                    ? '这条音色要用参考音频克隆，当前没有引擎声明该能力'
                    : '平台音色的引擎由它所属的平台决定'
                "
              >
                {{ isLocal ? '没有可用的引擎' : engineLabel || '随音色来源' }}
              </span>
            </div>

            <template v-if="isLocal">
              <div class="assign-row">
                <span class="assign-label">样例</span>
                <SegmentedControl v-model="mode" label="样例策略" :options="modes" block />
              </div>

              <p v-if="mode === 'emotion'" class="assign-hint">
                按这一行的情绪匹配样例；同义写法（兴奋 = 开心）一并认，没有对应样例时用主样例。
              </p>

              <!-- 固定样例：与音色详情同一种列法，行内可直接试听 -->
              <div v-else-if="samples.length" class="sample-block">
                <div class="sample-head">
                  <span class="assign-label">用哪条</span>
                  <span class="sample-count">{{ samples.length }} 条</span>
                </div>
                <div class="sample-list" role="radiogroup" aria-label="样例">
                  <div
                    v-for="item in samples"
                    :key="item.file"
                    class="sample-opt"
                    :class="{ on: sample === item.file }"
                    role="radio"
                    :aria-checked="sample === item.file"
                    tabindex="0"
                    :title="item.file"
                    @click="sample = item.file"
                    @keydown.enter.prevent="sample = item.file"
                    @keydown.space.prevent="sample = item.file"
                  >
                    <button
                      type="button"
                      class="sample-play"
                      :class="{ 'is-on': isPlaying(item) }"
                      :title="isPlaying(item) ? '停止' : '试听这一段'"
                      @click.stop="toggleSample(item)"
                    >
                      <Square v-if="isPlaying(item)" :size="9" fill="currentColor" />
                      <Play v-else :size="10" fill="currentColor" />
                    </button>
                    <span class="sample-info">
                      <span class="sample-name">{{ sampleName(item) }}</span>
                      <span class="sample-meta">{{ sampleMeta(item) }}</span>
                    </span>
                    <Check v-if="sample === item.file" class="sample-check" :size="14" :stroke-width="2" />
                  </div>
                </div>
              </div>
              <p v-else class="assign-hint">这个音色没有样例可选：合成时会退回主样例。</p>
            </template>

            <label v-if="selectionCount" class="assign-selection">
              <input v-model="assignSelection" type="checkbox" />
              同时把选中的 {{ selectionCount }} {{ unit }}改为{{ selectionTarget }}
            </label>
          </div>

          <footer class="assign-foot">
            <span class="assign-pick" :class="{ 'is-empty': !selectedName }">
              {{ selectedName || `先选${scopeOne}` }}
            </span>
            <button class="sh-btn is-ghost" @click="close">取消</button>
            <button
              class="sh-btn is-primary"
              :disabled="!role || (isLocal && !engines.length) || busy"
              :title="selectedAssigned ? `${scopeOne}已有音色，点下会用它覆盖` : ''"
              @click="submit"
            >
              {{ busy ? '指定中…' : selectedAssigned ? '覆盖' : '指定' }}
            </button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, ref, watch } from 'vue'
import { Check, Play, Square, X } from 'lucide-vue-next'
import EngineSelect from './EngineSelect.vue'
import SegmentedControl from './ui/SegmentedControl.vue'
import { actions, store } from '../store'
import { api } from '../api'
import { overlayTarget } from '../composables/overlayTarget'

const props = defineProps({
  open: { type: Boolean, default: false },
  /** 左侧卡片上的那条音色（宿主 voice 列表里的对象）。 */
  voice: { type: Object, default: null },
  /** 可选目标：有角色时是 `[{ key, name, rows }]`，没识别出角色时一行一章。 */
  roles: { type: Array, default: () => [] },
  /** 目标类别：`role` 写「角色」，`chapter` 写「章节」。 */
  scope: { type: String, default: 'role' },
  /** 这条音色落到哪台引擎（本地音色由宿主按通道推断后传进来）。 */
  engine: { type: String, default: '' },
  engineLabel: { type: String, default: '' },
  /** 当前目标→音色映射，用来把"还没分配的目标"排在前面。 */
  assignments: { type: Object, default: () => ({}) },
  /** 本地音色可选哪几台引擎（都用音色库的通道）；平台音色为空。 */
  engines: { type: Array, default: () => [] },
  /** 工作区里当前选中的行数：大于 0 时多一格「顺便把这些行改为这个目标」。 */
  selectionCount: { type: Number, default: 0 },
  /** 行数的单位：创作页讲「行」，配音页讲「段」。 */
  unit: { type: String, default: '行' },
  /** 提交中：按钮换成进行状态，避免连点重复指定。 */
  busy: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'assign'])

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const roleList = computed(() => /** @type {any[]} */ (props.roles))
const engineList = computed(() => /** @type {any[]} */ (props.engines))

/** 挂载点由壳登记：这颗弹窗随应用常驻（播放条、创作页都能叫它），
 *  而壳会被整个换掉（进设置页），绑死挂载时那个元素就再也画不出来。 */
const role = ref('')
const engineKey = ref('')
/** 用户自己点过引擎：此后换目标不再把它拽回原来那个的引擎。
 *  「先选引擎、再选目标」是常见顺序，那一下选择不该被后一步吃掉。 */
const engineTouched = ref(false)
const mode = ref('emotion')
const sample = ref('')
const samples = ref(/** @type {any[]} */ ([]))
const loading = ref(false)
const assignSelection = ref(true)

const modes = [
  { value: 'emotion', label: '按情绪匹配' },
  { value: 'fixed', label: '固定样例' },
]

const isLocal = computed(() => Boolean(props.voice?.is_local))
const scopeLabel = computed(() => (props.scope === 'chapter' ? '章节' : '角色'))
const scopeOne = computed(() => (props.scope === 'chapter' ? '这一章' : '这个角色'))
const voiceName = computed(() => props.voice?.shown_name || '')

/**
 * 这个目标配了哪条音色（空 = 还没指定）：实心点回答"有没有"，音色名留在悬停与读屏里。
 *
 * @param {any} key
 */
function assignedName(key) {
  return props.assignments[key]?.voice_name || ''
}

/** 两栏的顺序沿用目标表：左边是这次真要挑的，右边是可能要被覆盖的。 */
const openRoles = computed(() => roleList.value.filter((item) => !assignedName(item.key)))
const setRoles = computed(() => roleList.value.filter((item) => assignedName(item.key)))
/** 选中的目标本来就有音色：按钮改说「覆盖」，让这一下不是无声替换。 */
const selectedAssigned = computed(() => Boolean(role.value && assignedName(role.value)))
const selectedName = computed(
  () =>
    roleList.value.find(
      /** @param {any} item */ (item) => item.key === role.value
    )?.name || ''
)
const selectionTarget = computed(() =>
  selectedName.value ? `「${selectedName.value}」` : `选中的${scopeLabel.value}`
)

/**
 * 已指定音色的试听地址：钉了固定样例就用那一段，否则用它自己的主样例。
 * 音色清单由宿主按需载入（`store.allVoices`），拿不到就只藏按钮，不挡指定。
 *
 * @param {any} key
 */
function previewUrl(key) {
  const mapping = props.assignments[key]
  if (!mapping?.voice_id) return ''
  if (mapping.sample_mode === 'fixed' && mapping.sample_file) {
    const voice = encodeURIComponent(mapping.voice_id)
    return `/api/v1/audio/${voice}/${encodeURIComponent(mapping.sample_file)}`
  }
  return (store.allVoices || []).find(/** @param {any} item */ (item) => item.id === mapping.voice_id)?.main_audio_url || ''
}

/**
 * @param {any} key
 */
function isVoicePlaying(key) {
  const url = previewUrl(key)
  return Boolean(url) && store.player.url === url && store.player.playing
}

/**
 * @param {any} key
 */
function toggleVoice(key) {
  const url = previewUrl(key)
  if (!url) return
  if (store.player.url === url) {
    actions.togglePlay()
    return
  }
  actions.play({ url, label: assignedName(key), sub: '试听' })
}

/**
 * 每一行的说明与读屏文字：名称、行数、有没有指定，一处拼。
 *
 * @param {any} item
 */
function roleTitle(item) {
  const voice = assignedName(item.key)
  return [
    item.name,
    item.mode ? (item.mode === 'original' ? '原声' : '配音') : '',
    item.rows ? `${item.rows} ${props.unit}` : '',
    voice ? `已指定 ${voice}` : '未指定',
  ]
    .filter(Boolean)
    .join(' · ')
}

/**
 * 样例名：去掉序号与扩展名的文件名（情绪与主样例另有位置，不在这行重复）。
 *
 * @param {any} item
 */
function sampleName(item) {
  return item.file.replace(/\.[^.]+$/, '').replace(/^\d+[_-]?/, '')
}

/**
 * 样例副行：情绪 / 主样例 / 时长。
 *
 * @param {any} item
 */
function sampleMeta(item) {
  const bits = []
  if (item.emotion) bits.push(item.emotion)
  if (item.is_main) bits.push('主样例')
  if (item.duration > 0) bits.push(`${item.duration.toFixed(1)}s`)
  return bits.join(' · ')
}

/**
 * 底栏正在放的是不是这一段（暂停也算当前这条）。
 *
 * @param {any} item
 */
function isPlaying(item) {
  return Boolean(item.audio_url) && store.player.url === item.audio_url && store.player.playing
}

/**
 * @param {any} item
 */
function toggleSample(item) {
  if (!item.audio_url) return
  if (store.player.url === item.audio_url) {
    actions.togglePlay()
    return
  }
  actions.play({
    url: item.audio_url,
    label: props.voice?.shown_name || '',
    sub: sampleName(item),
    voice: props.voice,
  })
}

/** 打开时重置：**不预选任何目标**——先把"有谁、谁配过"看清楚，再点一个；
 *  引擎跟到选中目标原来的那台（见下面 watch(role)）。 */
watch(
  () => props.open,
  async (open) => {
    if (!open) return
    role.value = ''
    engineKey.value = engineList.value[0]?.key || ''
    engineTouched.value = false
    mode.value = 'emotion'
    sample.value = ''
    samples.value = []
    assignSelection.value = true
    // 已指定目标的试听按钮要读主样例地址：清单不在就先取一份，失败也不拦指定。
    if (!store.allVoices?.length) void actions.loadAllVoices()
    if (!props.voice?.id || !isLocal.value) return
    loading.value = true
    try {
      const detail = await api.voice(props.voice.id)
      samples.value = detail.samples || []
      const main = samples.value.find(/** @param {any} item */ (item) => item.is_main)
      sample.value = (main || samples.value[0])?.file || ''
    } catch {
      // 扫不到样例不影响指定：合成时退回主样例。
    } finally {
      loading.value = false
    }
  }
)

/** 换目标时跟到它原来的引擎：同一个音色在不同引擎上听感差别很大。
 *  但只要用户自己点过引擎，就以他选的为准——不再被目标切换覆盖。 */
watch(role, (key) => {
  if (engineTouched.value) return
  const current = props.assignments[key]?.engine
  if (props.engines.some(/** @param {any} item */ (item) => item.key === current)) engineKey.value = current
})

/**
 * @param {string} key
 */
function pickEngine(key) {
  engineTouched.value = true
  engineKey.value = key
}

function close() {
  emit('close')
}

function submit() {
  if (!role.value) return
  emit('assign', {
    key: role.value,
    engine: engineKey.value,
    sample_mode: isLocal.value ? mode.value : 'emotion',
    sample_file: isLocal.value && mode.value === 'fixed' ? sample.value : '',
    selection: Boolean(props.selectionCount && assignSelection.value),
  })
}
</script>

<style scoped>
.assign-card {
  width: min(600px, 100%);
  padding: 18px 20px 14px;
  gap: 12px;
}
.assign-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.assign-heading {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.assign-title {
  margin: 0;
  font-size: 16px;
  font-weight: 650;
  letter-spacing: 0.01em;
  color: var(--sh-text);
}
.assign-note {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  margin: 0;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
.assign-voice {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
  color: var(--sh-text-2);
}
.assign-engine {
  flex: none;
  color: var(--sh-text-4);
}
.assign-sep {
  flex: none;
  color: var(--sh-line);
}
.assign-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
  overflow-y: auto;
}
.assign-empty {
  margin: 0;
  padding: 22px 12px;
  border: 1px dashed var(--sh-line);
  border-radius: var(--sh-r-sm);
  font-size: 12px;
  text-align: center;
  color: var(--sh-text-4);
}
.assign-static {
  font-size: 11.5px;
  color: var(--sh-text-3);
}
.assign-static.is-missing {
  color: var(--sh-warn);
}
.assign-row {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
}
/* 目标那一行换成两栏台账：表头固定，名单各自滚动，标签列与上面那行对齐 */
.assign-row.is-block {
  align-items: start;
}
.assign-row.is-block > .assign-label {
  padding-top: 7px;
}
.role-ledger {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 10px;
  min-width: 0;
}
.role-ledger.is-single {
  grid-template-columns: minmax(0, 1fr);
}
.ledger-col {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  background: var(--sh-field);
}
.ledger-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--sh-line);
  background: var(--sh-hover);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--sh-text-3);
}
.ledger-count {
  color: var(--sh-text-4);
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.ledger-list {
  flex: 1 1 auto;
  min-height: 0;
  max-height: 264px;
  overflow-y: auto;
  /* 系统开了「自动隐藏滚动条」时，滚动条是浮在内容上的：给它留一条空边，
     不然它正好压在行末那一整格试听按钮上。经典滚动条只是多出一条同样宽的留白。 */
  padding-right: 14px;
  scrollbar-width: thin;
  scrollbar-color: var(--sh-line) transparent;
}
/* 一条一条分账：行与行之间是实线，试听占行末一整格，选中在左沿压一道强调色。 */
.ledger-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  align-items: stretch;
  width: 100%;
  padding: 0;
  border: 0;
  border-bottom: 1px solid var(--sh-line-soft);
  border-radius: 0;
  background: transparent;
  color: var(--sh-text-2);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background var(--sh-t-fast) var(--sh-ease),
    box-shadow var(--sh-t-fast) var(--sh-ease);
}
.ledger-row.has-action {
  grid-template-columns: minmax(0, 1fr) 38px;
}
.ledger-row:last-child {
  border-bottom: 0;
}
.ledger-row:hover {
  background: var(--sh-hover);
}
.ledger-row:focus-visible {
  outline: 2px solid var(--sh-accent-ring);
  outline-offset: -2px;
}
.ledger-row.is-on {
  background: var(--sh-accent-soft);
  box-shadow: inset 3px 0 0 var(--sh-accent);
}
.ledger-main {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 1px;
  min-width: 0;
  padding: 7px 9px;
}
.ledger-name {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
}
.ledger-role {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--sh-text);
}
.ledger-row.is-on .ledger-role {
  color: var(--sh-accent);
}
.ledger-dot {
  flex: none;
  align-self: center;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--sh-accent);
}
.ledger-voice {
  display: flex;
  align-items: center;
  min-width: 0;
  padding-left: 12px;
  font-size: 10.5px;
  color: var(--sh-text-4);
  white-space: nowrap;
}
.ledger-voice-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ledger-rows {
  flex: none;
  font-size: 10.5px;
  font-weight: 400;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}
.ledger-mode {
  flex: none;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--sh-chip);
  color: var(--sh-text-3);
  font-size: 9.5px;
  font-weight: 500;
  line-height: 14px;
}
/* 试听：占满整行高度的一格，左沿一条线与文字分开；没音频的行走同宽空位，行尾对齐。 */
.voice-play {
  align-self: stretch;
  width: 38px;
  min-height: 100%;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-left: 1px solid var(--sh-line-soft);
  border-radius: 0;
  background: transparent;
  color: var(--sh-text-3);
  cursor: pointer;
  transition: background var(--sh-t-fast) var(--sh-ease), color var(--sh-t-fast) var(--sh-ease),
    border-color var(--sh-t-fast) var(--sh-ease);
}
.voice-play:hover,
.voice-play.is-playing {
  border-left-color: var(--sh-accent);
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}
.voice-play.is-empty {
  cursor: default;
  background: transparent;
}
@media (max-width: 640px) {
  .role-ledger {
    grid-template-columns: minmax(0, 1fr);
  }
  .ledger-list {
    max-height: 168px;
  }
}
.assign-label {
  font-size: 12.2px;
  color: var(--sh-text-2);
}
.assign-selection {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 7px 9px;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-sm);
  background: var(--sh-field);
  font-size: 11.5px;
  color: var(--sh-text-2);
  cursor: pointer;
}
.assign-selection input {
  accent-color: var(--sh-accent);
}
.assign-hint {
  margin: 0;
  padding-left: 74px;
  font-size: 10.8px;
  line-height: 1.5;
  color: var(--sh-text-4);
}
.sample-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.sample-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.sample-count {
  font-size: 10.5px;
  color: var(--sh-text-4);
}
/* 与音色详情同一套列法：一行一条，左侧试听，点整行选中 */
.sample-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 186px;
  overflow-y: auto;
  /* 同台账：右侧留出悬浮滚动条的空边，行末的对勾不会被压住。 */
  padding: 2px 14px 2px 2px;
  scrollbar-width: thin;
  scrollbar-color: var(--sh-line) transparent;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-sm);
  background: var(--sh-field);
}
.sample-opt {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 7px;
  border-radius: var(--sh-r-2xs);
  cursor: pointer;
}
.sample-opt:hover {
  background: var(--sh-panel);
}
.sample-opt.on {
  background: var(--sh-accent-soft);
}
.sample-play {
  flex: none;
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 1px solid var(--sh-line-soft);
  border-radius: 50%;
  background: var(--sh-panel);
  color: var(--sh-text-3);
  cursor: pointer;
  transition: border-color var(--sh-t-fast) var(--sh-ease), color var(--sh-t-fast) var(--sh-ease);
}
.sample-play:hover,
.sample-play.is-on {
  border-color: var(--sh-accent);
  color: var(--sh-accent);
}
.sample-info {
  display: flex;
  flex-direction: column;
  gap: 1px;
  flex: 1;
  min-width: 0;
}
.sample-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--sh-text);
}
.sample-meta {
  font-size: 10.5px;
  color: var(--sh-text-4);
}
.sample-check {
  flex: none;
  color: var(--sh-accent);
}
.assign-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--sh-line-soft);
}
.assign-pick {
  min-width: 0;
  margin-right: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--sh-text-2);
}
.assign-pick.is-empty {
  font-weight: 400;
  color: var(--sh-text-4);
}
</style>
