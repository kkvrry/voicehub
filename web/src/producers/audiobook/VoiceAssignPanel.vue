<template>
  <!-- 音色弹窗的「分配」栏：整张分配表 + 应用按钮。外壳（遮罩、标题、栏目切换）在
       VoiceToolsDialog 里；这里只负责表格本身。 -->
  <div class="voice-panel">
    <div class="auto-body">
      <div class="plan-bar">
        <div class="plan-bar-main">
          <span class="auto-section-label">分配表</span>
          <span class="auto-note">
            {{ roleList.length }} 个角色 · {{ assignedCount }} 个已有音色
            <template v-if="defaultText"> · {{ defaultText }}</template>
          </span>
        </div>
        <div class="plan-bar-actions">
          <button type="button" class="sh-btn is-sm is-ghost" :disabled="busy" @click="resetPlan">
            还原
          </button>
          <button
            type="button"
            class="sh-btn is-sm is-ghost"
            title="只补还没选音色的角色；要重挑某一行就点那一行的星星"
            :disabled="busy || !options.engines.length || !aiRows.length"
            @click="matchAll"
          >
            <Loader2 v-if="matching === 'all'" class="spin" :size="13" :stroke-width="1.8" />
            <Sparkles v-else :size="13" :stroke-width="1.8" />
            AI 匹配
          </button>
        </div>
      </div>

      <div v-if="error" class="plan-error">
              <TriangleAlert :size="14" :stroke-width="1.8" />
              <span>{{ error }}</span>
            </div>

      <div v-if="roleList.length" class="plan-wrap">
        <table class="plan-table">
          <thead>
            <tr>
              <th class="col-role">角色</th>
              <th class="col-engine">引擎</th>
              <th class="col-mode">生成方式</th>
              <th class="col-voice">音色</th>
              <th class="col-sample">样例</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="role in roleList" :key="role.name">
              <tr :class="{ 'is-changed': isChanged(role.name) }">
                <td class="col-role">
                  <div class="role-main">
                    <span class="role-name" :title="role.name">{{ role.name }}</span>
                    <span v-if="isChanged(role.name)" class="sh-chip is-accent">待应用</span>
                  </div>
                  <div class="role-meta">
                    <span>{{ role.rows }} 行</span>
                    <span
                      v-if="reasonOf(role.name) !== '—'"
                      class="role-reason"
                      :title="reasonOf(role.name)"
                    >
                      · {{ reasonOf(role.name) }}
                    </span>
                  </div>
                  <!-- AI 匹配时按它挑的音色：性别、年龄、音色词，留在角色栏里
                       方便对照这颗音色为什么合适；手动指定后也继续显示。 -->
                  <div v-if="profileOf(role.name)" class="role-profile" :title="profileOf(role.name)">
                    {{ profileOf(role.name) }}
                  </div>
                  <div v-if="noteOf(role.name)" class="role-note" :title="noteOf(role.name)">
                    {{ noteOf(role.name) }}
                  </div>
                </td>
                <td class="col-engine">
                  <EngineSelect
                    :value="planOf(role.name).engine"
                    :options="engineOptions"
                    :disabled="busy"
                    :wheel="false"
                    :label="`${role.name} 的引擎`"
                    @change="setEngine(role.name, $event)"
                  />
                </td>
                <td class="col-mode">
                  <EngineSelect
                    :value="planOf(role.name).mode"
                    :options="modeOptionsFor(role.name)"
                    :disabled="busy || !options.engines.length"
                    :wheel="false"
                    :label="`${role.name} 的生成方式`"
                    @change="setMode(role.name, $event)"
                  />
                </td>
                <td class="col-voice">
                  <!-- 音色设计没有音色可选：这一格本身就是描述输入框，
                       与左边的「生成方式」同行对应，不再另开一行。
                       框里没写字也能点星星——描述由 AI 现写，用户只需改。 -->
                  <div
                    v-if="planOf(role.name).mode === 'design'"
                    class="voice-field"
                    :class="{ 'is-disabled': busy }"
                  >
                    <input
                      v-model="planOf(role.name).design_prompt"
                      type="text"
                      class="voice-design"
                      :disabled="busy"
                      placeholder="描述声线、年龄感与说话方式，或点星星让 AI 写"
                      :aria-label="`${role.name} 的音色描述`"
                      :title="planOf(role.name).design_prompt"
                      @input="touch(role.name)"
                    />
                    <div v-if="!isNarrator(role.name)" class="voice-field-acts">
                      <button
                        type="button"
                        class="field-act"
                        :class="{ 'is-busy': matching === role.name }"
                        :disabled="busy"
                        title="AI 匹配"
                        :aria-label="`让 AI 写 ${role.name} 的音色描述`"
                        @click="matchOne(role.name)"
                      >
                        <Loader2
                          v-if="matching === role.name"
                          class="spin"
                          :size="13"
                          :stroke-width="2"
                        />
                        <Sparkles v-else :size="13" :stroke-width="1.9" />
                      </button>
                    </div>
                  </div>
                  <VoiceCombo
                    v-else
                    :value="planOf(role.name).voice_id"
                    :options="voiceOptionsFor(role.name)"
                    :disabled="busy || !planOf(role.name).engine"
                    :label="`${role.name} 的音色`"
                    placeholder="输入音色名或标签"
                    @change="setVoice(role.name, $event)"
                  >
                    <template #suffix>
                      <button
                        type="button"
                        class="field-act"
                        :disabled="!playable(role.name)"
                        :title="planOf(role.name).sample_file ? '试听这一行选中的样例' : '试听这条音色（主样例）'"
                        aria-label="试听音色"
                        @click="play(role.name)"
                      >
                        <Play :size="11" :stroke-width="2.2" fill="currentColor" />
                      </button>
                      <button
                        v-if="!isNarrator(role.name)"
                        type="button"
                        class="field-act"
                        :class="{ 'is-busy': matching === role.name }"
                        :disabled="busy"
                        title="AI 匹配"
                        :aria-label="`让 AI 匹配 ${role.name} 的音色`"
                        @click="matchOne(role.name)"
                      >
                        <Loader2
                          v-if="matching === role.name"
                          class="spin"
                          :size="13"
                          :stroke-width="2"
                        />
                        <Sparkles v-else :size="13" :stroke-width="1.9" />
                      </button>
                    </template>
                  </VoiceCombo>
                </td>
                <td class="col-sample">
                  <span v-if="planOf(role.name).mode === 'design'" class="sample-none">—</span>
                  <EngineSelect
                    v-else
                    :value="planOf(role.name).sample_file"
                    :options="sampleOptionsFor(role.name)"
                    :disabled="busy || !planOf(role.name).voice_id"
                    :wheel="false"
                    :label="`${role.name} 用哪条样例`"
                    @open="ensureSamples(role.name)"
                    @change="setSample(role.name, $event)"
                  />
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <p v-else class="auto-empty">还没有可分配的角色，先完成对白分析。</p>

      <ul v-if="warnings.length" class="auto-warnings">
        <li v-for="(warning, index) in warnings" :key="index">{{ warning }}</li>
      </ul>
    </div>

    <footer class="auto-foot">
      <span class="auto-foot-state">{{ footState }}</span>
      <button type="button" class="sh-btn is-ghost" :disabled="applying" @click="close">
        取消
      </button>
      <button
        type="button"
        class="sh-btn is-primary"
        :disabled="applying || !options.engines.length || !changedRows.length"
        @click="apply"
      >
        {{ applying ? '应用中……' : `应用 ${changedRows.length} 项` }}
      </button>
    </footer>
  </div>
</template>

<script setup>
// @ts-check
import { computed, reactive, ref, watch } from 'vue'
import { Loader2, Play, Sparkles, TriangleAlert } from 'lucide-vue-next'
import { actions, say, store } from '../../store'
import { api } from '../../api'
import { assignVoices, runAssist } from './store'
import EngineSelect from './EngineSelect.vue'
import VoiceCombo from './VoiceCombo.vue'
import { voiceOptions } from './api'

const props = defineProps({
  open: { type: Boolean, default: false },
  /** 分配表的角色（含旁白）；``current`` 是当前分配，可能为空。 */
  roles: { type: Array, default: () => [] },
})
const emit = defineEmits(['close', 'applied'])

const roleList = computed(() => /** @type {any[]} */ (props.roles))


/** @type {Record<string, string>} */
const MODE_LABELS = { preset: '平台预置', clone: '音色克隆', design: '音色设计' }
/** 旁白不是书里的角色：它占一行、能手动指定，但不交给 AI 匹配。 */
const NARRATOR = '旁白'
/**
 * @param {any} name
 */
const isNarrator = (name) => name === NARRATOR
const applying = ref(false)
/** 正在匹配的角色名，或 ``'all'``；'' = 空闲。 */
const matching = ref('')
const error = ref('')
const warnings = ref([])
const options = reactive(/** @type {any} */ ({
  engines: [],
  voices: [],
  default_engine: '',
  default_mode: '',
  default_label: '',
}))
/** 每行的编辑状态；``source`` 说明音色从哪来，``touched`` 记录用户是否动过这一行。 */
const plan = ref(/** @type {Record<string, any>} */ ({}))

const busy = computed(() => Boolean(matching.value) || applying.value)
const assignedCount = computed(
  () => roleList.value.filter((role) => currentOf(role.name)?.voice_id).length
)
const defaultText = computed(() => {
  if (!options.default_label) return ''
  const mode = MODE_LABELS[options.default_mode]
  return mode ? `默认 ${options.default_label} · ${mode}` : `默认 ${options.default_label}`
})
const engineOptions = computed(() =>
  (options.engines || []).map(/** @param {any} item */ (item) => ({
    key: item.key,
    label: item.kind === 'local' ? `${item.label} · 本地` : item.label,
  }))
)
/** 顶部「AI 匹配」只补还没音色的行：已有音色的、手动挑过的都不动。
 *  要重挑某一行就点那一行的星星；旁白不是书里的角色，两边都不参与。 */
const aiRows = computed(() =>
  roleList.value.filter((role) => !isNarrator(role.name) && !isResolved(role.name))
)
const changedRows = computed(() =>
  roleList.value.filter((role) => isResolved(role.name) && isChanged(role.name))
)
const footState = computed(() => {
  if (matching.value === 'all') return 'AI 正在匹配……'
  if (matching.value) return `AI 正在匹配「${matching.value}」……`
  if (!options.engines.length) return '没有可用引擎：先在设置里配置一台。'
  if (!changedRows.value.length) return ''
  return `将应用 ${changedRows.value.length} 项`
})

/* 弹窗改成两栏合一后，这一栏是「打开时新建」的：创建那一刻 open 已经是 true，
   不会再有 false → true 的变化。所以必须 immediate，否则分配表停在空状态，
   引擎 / 方式 / 音色全没值，看起来像全部不可选。 */
watch(
  () => props.open,
  (open) => {
    if (!open) return
    error.value = ''
    warnings.value = []
    matching.value = ''
    resetPlan()
    void loadOptions().then(() => {
      if (props.open && untouched()) resetPlan()
    })
  },
  { immediate: true }
)

watch(
  () => roleList.value.map((role) => role.name).join('|'),
  () => {
    if (props.open && untouched()) resetPlan()
  }
)

/**
 * @param {any} name
 */
function planOf(name) {
  return (
    plan.value[name] || {
      engine: '',
      mode: '',
      voice_id: '',
      design_prompt: '',
      /** 固定样例的文件名；空 = 按情绪匹配。 */
      sample_file: '',
      reason: '',
      /** AI 这次写了一句与库里不同的描述：音色没变也要点「应用」才存得下来。 */
      reason_dirty: false,
      /** AI 按它挑的音色：性别 · 年龄 · 音色词，显示在角色栏。 */
      profile: '',
      /** 画像里那句身份与说话方式，比词表更像人话。 */
      note: '',
      source: 'current',
      touched: false,
    }
  )
}

/**
 * @param {any} name
 */
function currentOf(name) {
  return roleList.value.find((role) => role.name === name)?.current || null
}

function untouched() {
  return Object.values(plan.value).every(
    (choice) => choice.source !== 'manual' && choice.source !== 'ai' && !choice.touched
  )
}

/**
 * @param {any} engineKey
 */
function defaultModeOf(engineKey) {
  const found = options.engines.find(/** @param {any} item */ (item) => item.key === engineKey)
  return found?.default_mode || 'clone'
}

/**
 * 当前音色是哪种方式来路：设计 / 本地克隆 / 平台预置（换到别家当参考音频时按克隆算）。
 *
 * @param {any} voice
 * @param {any} engineKey
 */
function modeOfVoice(voice, engineKey) {
  if (!voice) return ''
  if (voice.design) return 'design'
  if (voice.local) return 'clone'
  if (voice.source && voice.source === engineKey) return 'preset'
  return 'clone'
}

/**
 * @param {any} name
 */
function designPromptOf(name) {
  const voiceId = currentOf(name)?.voice_id
  if (!voiceId) return ''
  return options.voices.find(/** @param {any} voice */ (voice) => voice.id === voiceId)?.design_prompt || ''
}

/** 把整张表铺回「当前分配」：打开弹窗、点「还原」都走这里。 */
function resetPlan() {
  plan.value = Object.fromEntries(
    roleList.value.map((role) => {
      const current = role.current || null
      // 保留原引擎：它要是已经不可用，换默认引擎会让「没动过」的一行看起来像改过。
      const engine = current?.engine || options.default_engine
      const voice = options.voices.find(/** @param {any} item */ (item) => item.id === current?.voice_id)
      const mode = modeOfVoice(voice, engine) || defaultModeOf(engine)
      // 库里存着上次 AI 写下的角色描述（画像 note）：打开就摆回角色栏。
      const savedReason = String(current?.reason || '').trim()
      return [
        role.name,
        {
          engine,
          mode,
          voice_id: mode === 'design' ? '' : current?.voice_id || '',
          design_prompt: mode === 'design' ? designPromptOf(role.name) : '',
          // 复刻「当前分配」：原来钉住某条样例的，这里也填回去。
          sample_file:
            mode !== 'design' && current?.sample_mode === 'fixed'
              ? current.sample_file || ''
              : '',
          reason: savedReason,
          reason_dirty: false,
          profile: '',
          note: '',
          // saved = 从库里读回来的依据，仍然算「没动过」，AI 再匹配时可以整体刷新。
          source: savedReason ? 'saved' : 'current',
          touched: false,
        },
      ]
    })
  )
  error.value = ''
  warnings.value = []
}

async function loadOptions() {
  try {
    const payload = await voiceOptions(store.producer.key)
    options.engines = payload.engines || []
    options.voices = payload.voices || []
    options.default_engine = payload.default_engine || ''
    options.default_mode = payload.default_mode || ''
    options.default_label =
      options.engines.find(/** @param {any} item */ (item) => item.key === options.default_engine)?.label || ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

/**
 * @param {any} engineKey
 */
function modesOf(engineKey) {
  const found = options.engines.find(/** @param {any} item */ (item) => item.key === engineKey)
  return found?.modes?.length ? found.modes : []
}

/**
 * @param {any} name
 */
function modeOptionsFor(name) {
  return modesOf(planOf(name).engine).map(
    /** @param {string} mode */
    (mode) => ({
      key: mode,
      label: MODE_LABELS[mode] || mode,
    })
  )
}

/**
 * 这台引擎 + 这个方式下真正能选的音色（不含「当前值兜底」）。
 *
 * @param {any} name
 */
function voiceCandidates(name) {
  const choice = planOf(name)
  if (choice.mode === 'design') return []
  return (options.voices || []).filter(/** @param {any} voice */ (voice) => {
    if (!voice.engines?.includes(choice.engine)) return false
    if (choice.mode === 'preset') {
      return !voice.local && voice.source === choice.engine && !voice.design
    }
    if (choice.mode === 'clone') return voice.samples > 0 && !voice.design
    return true
  })
}

/**
 * 下拉里的一项：候选之外，当前值即使不匹配筛选也留着，方便看和换回去。
 * ``keywords`` 把标签、语言这些不进标签行的字段也给搜索：音色名只记得一半时，
 * 打「温柔 女」也能找到相近的候选。
 *
 * @param {any} name
 */
function voiceOptionsFor(name) {
  const choice = planOf(name)
  const usable = voiceCandidates(name)
  const current = currentOf(name)
  if (current?.voice_id && !usable.some(/** @param {any} voice */ (voice) => voice.id === current.voice_id)) {
    usable.unshift({
      id: current.voice_id,
      name: current.voice_name || current.voice_id,
      gender: '',
      age: '',
      samples: 0,
      engines: [choice.engine],
    })
  }
  return usable.map(/** @param {any} voice */ (voice) => {
    const bits = [voice.gender, voice.age].filter(Boolean).join('·')
    const tail = voice.design ? ' · 设计' : bits ? ` · ${bits}` : ''
    const keywords = [
      voice.name,
      voice.gender,
      voice.age,
      voice.language,
      voice.design ? '设计' : '',
      ...(voice.tags || []),
    ]
      .filter(Boolean)
      .join(' ')
    return { key: voice.id, label: `${voice.name}${tail}`, keywords }
  })
}

/** 样例：默认「按情绪匹配」，展开这一格时才去拉这条音色的样例行。 */
const DEFAULT_SAMPLE = { key: '', label: '按情绪匹配' }
const sampleCache = reactive(new Map())
const samplePending = reactive(new Set())

/**
 * 样例名与音色详情同一套说法：去序号扩展名，后面跟情绪 / 主样例。
 *
 * @param {any} item
 */
function sampleLabel(item) {
  const name = String(item.file || '').replace(/\.[^.]+$/, '').replace(/^\d+[_-]?/, '')
  const bits = [item.emotion, item.is_main ? '主样例' : ''].filter(Boolean)
  return bits.length ? `${name} · ${bits.join(' · ')}` : name
}

/**
 * @param {any} name
 */
function sampleOptionsFor(name) {
  const choice = planOf(name)
  if (choice.mode === 'design' || !choice.voice_id) return [DEFAULT_SAMPLE]
  const rows = sampleCache.get(choice.voice_id) || []
  return [DEFAULT_SAMPLE, ...rows.map(/** @param {any} item */ (item) => ({ key: item.file, label: sampleLabel(item) }))]
}

/**
 * @param {any} name
 */
async function ensureSamples(name) {
  const voiceId = planOf(name).voice_id
  if (!voiceId || sampleCache.has(voiceId) || samplePending.has(voiceId)) return
  samplePending.add(voiceId)
  try {
    const detail = await api.voice(voiceId)
    sampleCache.set(voiceId, detail.samples || [])
  } catch {
    // 扫不到样例不影响分配：合成时会退回主样例。
    sampleCache.set(voiceId, [])
  } finally {
    samplePending.delete(voiceId)
  }
}

/**
 * @param {any} name
 * @param {File} file
 */
function setSample(name, file) {
  const choice = plan.value[name]
  if (!choice) return
  choice.sample_file = file || ''
  touched(choice)
}

/**
 * 角色栏里的描述：AI 写下的那句一直显示，换引擎、换音色、换样例都不清。
 *
 * @param {any} name
 */
function reasonOf(name) {
  const choice = planOf(name)
  if (choice.reason) return choice.reason
  if (!isResolved(name)) return '—'
  if (choice.source === 'ai') return 'AI 匹配'
  if (choice.mode === 'design') {
    // 原样填回的设计音色不算改过，只有动过描述才说「用户设置」。
    return choice.design_prompt.trim() === designPromptOf(name) && !choice.touched
      ? '—'
      : '用户设置'
  }
  if (choice.source === 'manual' || choice.touched) return '用户设置'
  return '—'
}

/**
 * 角色栏里的声音画像：AI 按它挑的音色（没有就整行不占位）。
 *
 * @param {any} name
 */
function profileOf(name) {
  return planOf(name).profile || ''
}

/**
 * 画像里那句「身份 + 年龄感 + 说话方式」：比词表更能说明这是谁。
 *
 * @param {any} name
 */
function noteOf(name) {
  return planOf(name).note || ''
}

/**
 * @param {any} name
 */
function isResolved(name) {
  const choice = planOf(name)
  if (choice.mode === 'design') return Boolean(choice.design_prompt.trim())
  return Boolean(choice.voice_id)
}

/**
 * @param {any} name
 */
function isChanged(name) {
  const choice = planOf(name)
  const current = currentOf(name)
  // AI 这次写下了新描述：即使音色没动，也得有一次「应用」把它存下来。
  if (choice.reason_dirty) return true
  if (choice.mode === 'design') {
    const prompt = choice.design_prompt.trim()
    if (!prompt) return false
    return prompt !== designPromptOf(name)
  }
  if (!choice.voice_id) return false
  const currentSample = current?.sample_mode === 'fixed' ? current.sample_file || '' : ''
  return (
    choice.voice_id !== (current?.voice_id || '') ||
    (choice.engine || '') !== (current?.engine || '') ||
    (choice.sample_file || '') !== currentSample
  )
}

/**
 * 用户动过这一行：只记「改过」，AI 写下的描述留着不动。
 *
 * @param {any} choice
 */
function touched(choice) {
  choice.touched = true
  if (choice.source === 'ai' || choice.source === 'saved') {
    choice.source = 'current'
  }
}

/**
 * @param {any} name
 */
function touch(name) {
  const choice = plan.value[name]
  if (choice) touched(choice)
}

/**
 * @param {any} name
 * @param {any} engineKey
 */
function setEngine(name, engineKey) {
  const choice = plan.value[name]
  if (!choice) return
  choice.engine = engineKey
  const modes = modesOf(engineKey)
  if (!modes.includes(choice.mode)) {
    choice.mode = modes.includes(defaultModeOf(engineKey))
      ? defaultModeOf(engineKey)
      : modes[0] || ''
  }
  if (choice.voice_id && !voiceCandidates(name).some(/** @param {any} voice */ (voice) => voice.id === choice.voice_id)) {
    choice.voice_id = ''
    choice.sample_file = ''
  }
  touched(choice)
}

/**
 * @param {any} name
 * @param {any} modeKey
 */
function setMode(name, modeKey) {
  const choice = plan.value[name]
  if (!choice) return
  choice.mode = modeKey
  if (modeKey === 'design') {
    choice.voice_id = ''
    choice.sample_file = ''
  } else if (choice.voice_id) {
    const still = voiceCandidates(name).some(/** @param {any} voice */ (voice) => voice.id === choice.voice_id)
    if (!still) {
      choice.voice_id = ''
      choice.sample_file = ''
    }
  }
  touched(choice)
}

/**
 * @param {any} name
 * @param {any} voiceId
 */
function setVoice(name, voiceId) {
  const choice = plan.value[name]
  if (!choice) return
  // 换了音色，原来钉住的样例属于上一条，留着会指向不存在的文件。
  if (voiceId !== choice.voice_id) choice.sample_file = ''
  choice.voice_id = voiceId
  choice.touched = true
  if (voiceId) {
    // 只改「这条是用户挑的」：描述属于角色，换音色不作废。
    choice.source = 'manual'
  } else if (choice.source === 'manual') {
    choice.source = 'current'
  }
}

/**
 * @param {any} name
 */
function choiceOf(name) {
  const choice = planOf(name)
  const design = choice.mode === 'design' ? choice.design_prompt.trim() : ''
  // 输入框里预填的是库里上次那段描述：用户没动过就不算定稿，交给模型
  // 在这次分配里直接写一句——否则旧描述会一直挡住新提示词。
  const own = design && design !== designPromptOf(name) ? design : ''
  return { engine: choice.engine, mode: choice.mode, design_prompt: own }
}

/**
 * @param {any} err
 */
function errorText(err) {
  return err instanceof Error ? err.message : String(err)
}

/**
 * @param {any} name
 */
async function matchOne(name) {
  if (busy.value || isNarrator(name)) return
  matching.value = name
  error.value = ''
  try {
    const payload = await runAssist('voice_assign', {
      speakers: [name],
      choices: { [name]: choiceOf(name) },
    })
    mergeProposal(payload.preview, [name])
  } catch (err) {
    error.value = errorText(err)
  } finally {
    matching.value = ''
  }
}

async function matchAll() {
  if (busy.value) return
  const names = aiRows.value.map((role) => role.name)
  if (!names.length) {
    say('每个角色都已有音色；要重挑某一行，点那一行的星星。', { kind: 'warn' })
    return
  }
  matching.value = 'all'
  error.value = ''
  try {
    const payload = await runAssist('voice_assign', {
      speakers: names,
      choices: Object.fromEntries(names.map(/** @param {any} name */ (name) => [name, choiceOf(name)])),
    })
    mergeProposal(payload.preview, names)
  } catch (err) {
    error.value = errorText(err)
  } finally {
    matching.value = ''
  }
}

/**
 * 把一次提案落回表格：没给建议的行保留用户已填的内容，只写一句原因。
 *
 * @param {any} preview
 * @param {any} names
 */
function mergeProposal(preview, names) {
  const rows = preview?.assignments || []
  const missed = preview?.unmatched || []
  const bySpeaker = Object.fromEntries(rows.map(/** @param {any} item */ (item) => [item.speaker, item]))
  for (const name of names) {
    const choice = plan.value[name]
    if (!choice) continue
    const row = bySpeaker[name]
    if (!row) {
      const miss = missed.find(/** @param {any} item */ (item) => item.speaker === name)
      // 没给建议不擦掉已经写着的描述：只在还没有描述时落一句说明。
      choice.reason =
        choice.reason || (miss?.reason ? `未匹配：${miss.reason}` : 'AI 没有给出建议')
      if (choice.source !== 'manual') choice.source = 'ai'
      continue
    }
    choice.engine = row.engine || choice.engine
    choice.reason = row.reason || choice.reason
    // AI 这次读到的角色画像：显示在角色栏里对照。
    choice.profile = String(row.profile || '')
    choice.note = String(row.note || '')
    // 与库里那句不同才算有新描述要存；音色没变时靠它让「应用」可点。
    // 落库的是画像 note（角色说明），重开弹框只带得回这一句。
    const description = choice.note || row.reason || ''
    choice.reason_dirty =
      Boolean(description) && description !== (currentOf(name)?.reason || '')
    if (row.mode === 'design') {
      choice.mode = 'design'
      choice.design_prompt = row.design_prompt || choice.design_prompt
      choice.voice_id = ''
      // 描述是这次 AI 写的（或沿用了用户原先填的），算这一次的建议；
      // 用户之后手改描述时 touch() 会把它打回「用户设置」。
      choice.touched = false
      choice.source = 'ai'
    } else {
      choice.voice_id = row.voice_id || ''
      choice.mode = row.mode || choice.mode
      choice.touched = false
      choice.source = 'ai'
    }
  }
  warnings.value = preview?.warnings || []
}

/**
 * @param {any} name
 */
function playable(name) {
  const voiceId = planOf(name).voice_id
  if (!voiceId) return false
  return (options.voices.find(/** @param {any} voice */ (voice) => voice.id === voiceId)?.samples || 0) > 0
}

//: 试听地址按音色记一份，用来让同一颗按钮在播放/停止之间切换。
const playUrls = new Map()

/**
 * @param {any} name
 */
async function play(name) {
  const choice = planOf(name)
  const voiceId = choice.voice_id
  if (!voiceId || !playable(name)) return
  const label = voiceOptionsFor(name).find(/** @param {any} item */ (item) => item.key === voiceId)?.label || voiceId
  try {
    const detail = await api.voice(voiceId)
    const samples = detail.samples || []
    // 这一行钉了哪条样例就播哪条；没钉（按情绪匹配）才播主样例。
    const pinned = choice.sample_file
      ? samples.find(/** @param {any} sample */ (sample) => sample.file === choice.sample_file)
      : null
    const target = pinned || samples.find(/** @param {any} sample */ (sample) => sample.is_main) || samples[0]
    if (!target?.audio_url) {
      say('这条音色没有可试听的样例。', { kind: 'warn' })
      return
    }
    // 播的就是这一段才切「停止」：换了样例会去播新的，不会误停上一段。
    if (playUrls.get(voiceId) === target.audio_url && store.player.url === target.audio_url) {
      actions.togglePlay()
      return
    }
    playUrls.set(voiceId, target.audio_url)
    actions.play({
      url: target.audio_url,
      label,
      sub: target.file,
      voice: detail,
    })
  } catch (err) {
    say(errorText(err), { kind: 'error' })
  }
}

async function apply() {
  const rows = changedRows.value.map((role) => {
    const choice = planOf(role.name)
    // 角色描述优先存画像 note：它写给用户看，重开弹框也靠它认识这个角色。
    const reason = choice.note || choice.reason || ''
    if (choice.mode === 'design') {
      return {
        speaker: role.name,
        engine: choice.engine,
        voice_id: '',
        mode: 'design',
        design_prompt: choice.design_prompt.trim(),
        reason,
      }
    }
    return {
      speaker: role.name,
      engine: choice.engine,
      voice_id: choice.voice_id,
      mode: choice.mode,
      // 钉住某条样例就是 fixed；否则沿用按情绪匹配。
      sample_mode: choice.sample_file ? 'fixed' : 'emotion',
      sample_file: choice.sample_file || '',
      reason,
    }
  })
  if (!rows.length) return
  applying.value = true
  error.value = ''
  try {
    const payload = await assignVoices(rows, true)
    if (payload) {
      emit('applied', payload)
      emit('close')
    }
  } finally {
    applying.value = false
  }
}

function close() {
  emit('close')
}
</script>

<style scoped src="./VoiceAssignPanel.css"></style>
