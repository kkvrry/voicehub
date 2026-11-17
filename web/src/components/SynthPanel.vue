<template>
  <div class="synth">
    <div class="synth-body">
      <!-- 最左：音色卡片列。数据就是左栏树当前选中的那一大段（点目录即筛），
           点一张卡 = 选为这次生成的音色，同时弹出它的详情——参考音频在详情里挑，
           挑完这张卡置顶冻结在这一列头上。 -->
      <SynthVoiceRail recordable />

      <!-- 中：正文 + 语气词 + 情绪 + 引擎 + 生成 + 产物。
           「参考音频」不在这里占一段：它在卡片列头上那张冻结卡里（点卡 → 详情 → 使用），
           少掉的这一块让合成结果直接上移一屏。 -->
      <div class="synth-col-main">
        <div class="editor-head">
          <h1 class="editor-title">{{ groupLabel('text') }}</h1>
          <!-- 语音输入那颗跟在标题后面：它属于"这一格"，不占框里的地方。 -->
          <MicButton
            mode="text"
            icon-only
            label="语音输入"
            :disabled="!store.asr?.ready"
            disabled-hint="先去「引擎 → 识别」配一档"
            @text="insertText"
            @error="say"
          />
          <span class="spacer"></span>
          <!-- 文案库：挑一句 / 随机一句，直接填进正文。 -->
          <TextLibrary @pick="fillText" />
        </div>
        <TextEditor ref="editor" v-model="store.synth.text" :marks="false" />
        <!-- 语气词贴着正文、情绪紧跟其后：都是"这句话怎么说"，合到一块面板里两行。 -->
        <!-- 语气词与情绪合到一块面板：都是"这句话怎么说"，一块里两行，省掉一层边框与那一道间距。 -->
        <div class="chip-stack">
          <MarksInline :groups="markGroups" :silent="markSilent" @insert="insertMark" />
          <div class="emotion-line">
            <div class="marks-head">
              <span class="marks-cap">情绪</span>
              <UnsupportedEngines
                :names="emotionSilent"
                feature="情绪"
                effect="也不支持引导指令，这一项不会生效"
              />
            </div>
            <EmotionField
              :model-value="store.synth.values.emotion || ''"
              :options="emotionOptions"
              @update="(value) => onField('emotion', value)"
            />
          </div>
        </div>

        <section class="synth-block">
          <!-- 块头就是这一批的操作条：计数 → 全部播放 → 全选 → 生成。
               生成键原先贴在栏底，滚到结果就看不见了；收进块头，写正文时手一直够得着。 -->
          <div class="engine-head">
            <h2 class="block-title">引擎</h2>
            <span class="engine-count">{{ store.synth.engines.length }} / {{ store.engines.length }}</span>
            <span class="spacer"></span>
            <button
              v-if="playable.length > 1"
              class="btn mini"
              title="按顺序播放这些音频（播完一条接一条）"
              @click="playAllResults(0)"
            >
              <Play :size="12" :stroke-width="1.8" />
              全部播放
            </button>
            <button
              class="engine-all"
              :disabled="!store.engines.length"
              :title="allPicked ? '取消全部' : '全部选中'"
              @click="toggleAll"
            >
              {{ allPicked ? '清空' : '全选' }}
            </button>
            <button class="btn primary" :disabled="!canRun" :title="reason || undefined" @click="run">
              {{ store.synthRunning ? '生成中' : `生成（${store.synth.engines.length} 台）` }}
            </button>
          </div>

          <div v-if="!store.engines.length" class="hint">
            尚未启用引擎。在「引擎」页的清单里启用一台后，这里才列出它。
          </div>
          <!-- 一台一行：名字与图标是开关（勾选框只剩键盘与读屏那一层），
               规格、波形与右侧动作各做各的——读规格时不会顺手改掉勾选。
               类型标签（本地/云端）收进 title；波形可点、可键盘定位。 -->
          <div v-else class="engine-list">
            <div
              v-for="row in engineRows"
              :key="row.engine.key"
              class="engine-item"
              :class="{
                picked: isPicked(row.engine.key),
                'has-bar': hasBar(row.job),
                'is-playing': row.job && playingJob(row.job),
              }"
            >
              <label class="engine-row">
                <input
                  class="engine-check"
                  type="checkbox"
                  :checked="isPicked(row.engine.key)"
                  @change="toggleEngine(row.engine.key)"
                />
                <img
                  v-if="row.engine.icon_url"
                  class="engine-icon"
                  :src="row.engine.icon_url"
                  alt=""
                  :title="engineTitle(row.engine)"
                />
                <span class="engine-body">
                  <span class="engine-line">
                    <span class="engine-pick-label" :title="engineTitle(row.engine)">
                      {{ row.engine.label }}
                    </span>
                    <span
                      v-if="!row.engine.ready"
                      class="engine-dot"
                      :title="row.engine.note"
                    ></span>
                    <!-- 规格就在这一行的明处；种子收进悬停，时长常显：
                         多台引擎对比时"多长"比"随机种子"有用。 -->
                    <span
                      v-if="row.job"
                      class="engine-meta"
                      :class="{ 'is-fail': row.job.state === 'failed' }"
                      :title="metaTitle(row.job)"
                      @click.stop.prevent
                    >
                      <template v-if="row.job.state === 'running'">生成中</template>
                      <template v-else-if="row.job.state === 'failed'">失败</template>
                      <template v-else-if="isCurrent(row.job)">
                        <span class="engine-clock">
                          {{ clock(store.player.position) }} / {{ clock(store.player.duration) }}
                        </span>
                        <span class="engine-sep">·</span>
                        <span class="engine-spec">{{ row.job.detail }}</span>
                      </template>
                      <template v-else>
                        <template v-if="row.job.stale">
                          <span class="engine-stale">未重跑</span>
                          <span class="engine-sep">·</span>
                        </template>
                        <span class="engine-spec">{{ row.job.detail }}</span>
                        <template v-if="durationOf(row.job)">
                          <span class="engine-sep">·</span>
                          <span class="engine-clock">{{ clock(durationOf(row.job)) }}</span>
                        </template>
                      </template>
                    </span>
                  </span>
                  <!-- 生成中先占住同一条轨位（细轨），产物落地就换成波形，行不跳版；
                       失败没有东西可放，整条不出现——空着一条反而像少了什么。
                       波形也是一条进度：可点、可键盘走（左右键 5%），悬停报时间。 -->
                  <span
                    v-if="hasBar(row.job)"
                    class="engine-bar"
                    :role="row.job.audio_url ? 'slider' : undefined"
                    :tabindex="row.job.audio_url ? 0 : undefined"
                    :aria-label="row.job.audio_url ? `试听定位：${row.engine.label}` : undefined"
                    :aria-valuemin="row.job.audio_url ? 0 : undefined"
                    :aria-valuemax="row.job.audio_url ? 100 : undefined"
                    :aria-valuenow="row.job.audio_url ? Math.round(barFraction(row.job) * 100) : undefined"
                    :aria-valuetext="row.job.audio_url ? barValueText(row.job) : undefined"
                    @click.stop.prevent="onBar(row.job, $event)"
                    @pointermove="onBarHover(row.job, $event)"
                    @pointerleave="barHover = null"
                    @keydown.left.prevent="nudgeBar(row.job, -0.05)"
                    @keydown.right.prevent="nudgeBar(row.job, 0.05)"
                    @keydown.space.stop.prevent
                  >
                    <WaveBars
                      v-if="row.job.audio_url"
                      :url="row.job.audio_url"
                      :progress="progressOf(row.job)"
                      @duration="rememberDuration(row.job.audio_url, $event)"
                    />
                    <span v-else class="engine-track"><i></i></span>
                    <span
                      v-if="barHover?.url === row.job.audio_url && durationOf(row.job)"
                      class="engine-bar-tip"
                      aria-hidden="true"
                      :style="{ '--x': `${barHover.frac * 100}%` }"
                    >
                      {{ clock(barHover.frac * durationOf(row.job)) }}
                    </span>
                  </span>
                </span>
                <!-- 右侧动作区：撤下与播放同一列；失败行把灰着的播放键换成「只重跑这台」。 -->
                <span class="engine-actions" @click.stop.prevent>
                  <button
                    v-if="row.job?.audio_url"
                    type="button"
                    class="engine-drop"
                    title="撤下这条（文件仍在缓存，不删除）"
                    aria-label="撤下这条"
                    @click.stop.prevent="dismissResult(row.job)"
                  >
                    <X :size="14" :stroke-width="1.8" />
                  </button>
                  <button
                    v-if="row.job?.state === 'failed'"
                    type="button"
                    class="engine-retry"
                    title="只重跑这台（用当前正文与参数）"
                    aria-label="只重跑这台"
                    @click.stop.prevent="retryResult(row.job)"
                  >
                    <RotateCcw :size="12" :stroke-width="1.8" />
                  </button>
                  <button
                    v-else-if="row.job"
                    type="button"
                    class="engine-play"
                    :class="{ 'is-ghost': !row.job.audio_url }"
                    :disabled="!row.job.audio_url"
                    :aria-label="playingJob(row.job) ? '暂停' : '播放'"
                    @click.stop.prevent="toggleResult(row.job)"
                  >
                    <Square v-if="playingJob(row.job)" :size="9" fill="currentColor" />
                    <Play v-else :size="11" fill="currentColor" />
                  </button>
                </span>
              </label>
              <!-- 引擎原文的报错不藏进 title：那是唯一说清"缺什么"的一句话。 -->
              <p v-if="row.job?.state === 'failed'" class="engine-err">{{ row.job.detail }}</p>
              <p v-for="note in visibleNotes(row.job)" :key="note" class="engine-note">{{ note }}</p>
            </div>
          </div>

          <!-- 这两句说的是"按下去会发生什么"，属于这一块，不是页脚。 -->
          <div v-if="warning || refNote" class="engine-hints">
            <span v-if="warning" class="hint" data-tone="warn">{{ warning }}</span>
            <span v-if="refNote" class="hint">{{ refNote }}</span>
          </div>
        </section>
      </div>

      <!-- 右：参数。按"用得多的在上"排：三个滑块 → 引导指令 → 种子 → 静默/语言/输出。
           分组标题与格子用不同的分量（标题是分类，格子是它的选项），别再一锅粥。 -->
      <aside class="synth-side">
        <section class="synth-block">
          <h2 class="block-title">合成参数</h2>
          <ParamFields
            :fields="speedFields"
            :values="store.synth.values"
            :extra-options="extraOptions"
            @update="onField"
          />
        </section>

        <section class="synth-block">
          <div class="block-title-row is-tags">
            <h2 class="block-title">引导指令</h2>
            <UnsupportedEngines
              :names="instructionSilent"
              feature="引导指令"
              effect="这一项不会发给它"
            />
          </div>
          <InstructionField
            :model-value="store.synth.values.instruction || ''"
            :presets="instructionPresets"
            @update="(value) => onField('instruction', value)"
          />
          <ParamFields
            :fields="cfgFields"
            :values="store.synth.values"
            :extra-options="extraOptions"
            @update="onField"
          />
        </section>

        <section class="synth-block">
          <h2 class="block-title">种子</h2>
          <SeedField
            :model-value="store.synth.values.seed ?? ''"
            @update="(value) => onField('seed', value)"
          />
        </section>

        <section class="synth-block">
          <h2 class="block-title">语言</h2>
          <ParamFields
            :fields="fieldsByKeys(['language', 'reference_language'])"
            :values="store.synth.values"
            :extra-options="extraOptions"
            @update="onField"
          />
        </section>

        <!-- 静默归进输出：它们都是"这条音频长什么样"，前后留白属于成品的一部分。 -->
        <section class="synth-block">
          <h2 class="block-title">输出</h2>
          <ParamFields
            :fields="fieldsByKeys([
              'format',
              'channels',
              'sample_rate',
              'bitrate',
              'lead_silence',
              'lag_silence',
            ])"
            :values="store.synth.values"
            :extra-options="extraOptions"
            @update="onField"
          />
        </section>
      </aside>
    </div>
  </div>
</template>

<script setup>
// @ts-check
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { Play, RotateCcw, Square, X } from 'lucide-vue-next'
import { store, actions, say } from '../store'
import { formatClock } from '../utils/format'
import EmotionField from './EmotionField.vue'
import InstructionField from './InstructionField.vue'
import MarksInline from './MarksInline.vue'
import ParamFields from './ParamFields.vue'
import SeedField from './SeedField.vue'
import SynthVoiceRail from './SynthVoiceRail.vue'
import TextEditor from './TextEditor.vue'
import TextLibrary from './TextLibrary.vue'
import UnsupportedEngines from './UnsupportedEngines.vue'
import MicButton from './ui/MicButton.vue'
import WaveBars from './ui/WaveBars.vue'

/** 勾选的那几台引擎——"认不认这条指令""这一格有哪些候选"都从它们身上问。 */
const pickedEngines = computed(() =>
  store.engines.filter(/** @param {any} e */ (e) => store.synth.engines.includes(e.key))
)

/** 候选（``value_choices``）——按参数键分的表，由勾选的引擎合并而成。 */
const extraOptions = computed(() => actions.choicesOf(pickedEngines.value))

/** 语气词标签那几组：通用在前、勾选的引擎在后。分组由后端算、按勾选收口。 */
const markGroups = computed(() => actions.markGroupsOf(pickedEngines.value))

/** 勾选里完全不认语气词的引擎名：面板上提一句，不逐颗变灰。 */
const markSilent = computed(() =>
  actions.unsupportedMarkEngines(pickedEngines.value).map((engine) => engine.label)
)

/** 情绪 / 引导指令：按插件声明筛出勾选里不支持的引擎，标题旁挂禁用标签。
 *  情绪看「情绪参数或引导指令」两路——后者是前者的下位表达。 */
const emotionSilent = computed(() =>
  actions.unsupportedEmotionEngines(pickedEngines.value).map((engine) => engine.label)
)
const instructionSilent = computed(() =>
  actions
    .unsupportedCapabilityEngines(pickedEngines.value, 'instruction_control')
    .map((engine) => engine.label)
)

/**
 * 规范里的一格（按 key 取，保留它的中文名/单位/区间）。
 *
 * @param {any} keys
 */
function fieldsByKeys(keys) {
  return keys
    .map(/** @param {any} key */ (key) => store.spec.fields.find((field) => field.key === key))
    .filter(Boolean)
}

/** 中栏"情绪"的候选（规范给的值 → 说法；第一项是空值 = 不指定）。 */
const emotionOptions = computed(
  () => store.spec.fields.find((field) => field.key === 'emotion')?.options || []
)

/** 右栏三个滑块。 */
const speedFields = computed(() => fieldsByKeys(['speed', 'volume', 'pitch']))
/** CFG 跟着引导指令走（有指令才需要配强度）。 */
const cfgFields = computed(() => fieldsByKeys(['guidance_scale']))

/**
 * 正文是中文还是英文：官方要求指令语言与正文一致，预设按它排先后。
 *
 * @param {any} text
 */
function textLanguage(text) {
  const raw = String(text || '')
  const latin = (raw.match(/[A-Za-z]/g) || []).length
  const cjk = (raw.match(/[\u3400-\u4dbf\u4e00-\u9fff]/g) || []).length
  if (!latin && !cjk) return ''
  return latin > cjk ? 'en' : 'zh'
}

/** 引导指令的现成说法：用户词表的生效值——各引擎默认的并集加上用户自己加的，
 *  由后端一次算清（``GET /api/v1/vocab``）。不按引擎分组：指令是自由文本，
 *  不支持的引擎发请求时那一项会被剔掉。
 *  正文是中文就先把中文说法摆前面，英文同理；同一语言内保持后端顺序。 */
const instructionPresets = computed(() => {
  const items = store.lexicon.instructions?.effective || []
  const target = textLanguage(store.synth.text)
  if (!target) return items
  /** @param {string} text */
  const rank = (text) => (textLanguage(text) === target ? 0 : 1)
  return [...items].sort((a, b) => rank(a.text) - rank(b.text))
})

/**
 * @param {string} key
 */
function groupLabel(key) {
  return store.spec.groups.find((g) => g.key === key)?.label || ''
}

/** 正文那格：语气词要插进它的光标处，所以留一个 ref。 */
const editor = ref(/** @type {any} */ (null))

/**
 * @param {any} mark
 */
function insertMark(mark) {
  editor.value?.insert?.(mark)
}

/**
 * 标题行那颗语音输入：识别出来的话插到正文光标处（规矩在 TextEditor 里）。
 *
 * @param {any} text
 */
function insertText(text) {
  editor.value?.insertText?.(text)
}

/**
 * 文案库填入：整段替换正文（它是一条完整的句子，不是插在字中间）。
 *
 * @param {any} text
 */
function fillText(text) {
  const value = String(text || '').trim()
  if (!value) return
  store.synth.text = value
  say('已填入文案')
}

/** 这一条用的是哪一号种子：引擎在 notes 里报 `seed=…`（随机时是抽中的那个数）。
 *  它是复现用的，收进悬停；常显的那一格让给时长。 */
const SEED_NOTE = /^seed=(-?\d+)$/
/**
 * @param {any} job
 */
function seedOf(job) {
  const hit = (job.notes || []).find(
    /** @param {string} note */
    (note) => SEED_NOTE.test(note)
  )
  return hit ? hit.slice('seed='.length) : ''
}

/**
 * 规格这一行的悬停：种子不再常显，但复现时仍要找得到。
 *
 * @param {any} job
 */
function metaTitle(job) {
  const seed = seedOf(job)
  return seed ? `种子 ${seed}` : ''
}

/** 结果里只说"结果"：格式、体积、时长在行上，种子在悬停，剩下的只有"哪几件没发出去"。
 *  转码/补逐字稿/引擎路由说明都属于过程，已经写进日志，不在界面上铺开。 */
const KEEP_NOTE = /^未发送：/
/**
 * @param {any} job
 */
function visibleNotes(job) {
  return (job?.notes || []).filter(
    /** @param {string} note */
    (note) => KEEP_NOTE.test(note)
  )
}

// 换音色 = 换一份可选音频，所以名单要重新取（同时作废上一份选择）。
watch(() => store.synth.voiceId, () => actions.loadSynthSamples())
onMounted(() => actions.loadSynthSamples())

/**
 * 每一格改动都落到 ``synth.values``：空串 = 没要求（那一项不进请求体）。
 *
 * @param {string} key
 * @param {any} value
 */
function onField(key, value) {
  store.synth.values[key] = value
}

/**
 * 这一台挂着的产物：`synthJobs` 按 key 对齐——可能是这一轮的，也可能是上一轮
 * 留下的（没勾选它就不会被重跑，结果也不清）。没有就是还没跑过。
 *
 * @param {string} key
 */
function jobOf(key) {
  return store.synthJobs.find((job) => job.key === key) || null
}

/** 引擎行 = 引擎 + 它自己的产物，模板一遍画完（row.engine / row.job）。 */
const engineRows = computed(() =>
  store.engines.map((engine) => ({ engine, job: jobOf(engine.key) }))
)

/**
 * @param {string} key
 */
function isPicked(key) {
  return store.synth.engines.includes(key)
}

/** 全选与清空共用一颗按钮：全选中时它才是「清空」，其余时候永远是「全选」。 */
const allPicked = computed(
  () => store.engines.length > 0 && store.synth.engines.length === store.engines.length
)

function toggleAll() {
  store.synth.engines = allPicked.value ? [] : store.engines.map((engine) => engine.key)
}

/**
 * @param {string} key
 */
function toggleEngine(key) {
  const picked = store.synth.engines
  const at = picked.indexOf(key)
  if (at >= 0) picked.splice(at, 1)
  else picked.push(key)
}

/**
 * 行上不再写「本地 / 云端」：类型以及它意味着的事（要不要密钥、计不计费）放在悬停里说。
 *
 * @param {any} engine
 */
function engineTitle(engine) {
  const lines = [engine.kind_note || engine.kind_label]
  if (!engine.ready && engine.note) lines.push(engine.note)
  return lines.filter(Boolean).join('\n')
}

/**
 * 这一行现在有没有"一条音频"：生成中占着轨位，出了产物换成波形，失败就什么都不放。
 * 行高跟着它走（`has-bar`）——没有音频的行不该先占一格的空。
 *
 * @param {any} job
 */
function hasBar(job) {
  return Boolean(job) && (Boolean(job.audio_url) || job.state === 'running')
}

/**
 * 播放键不再有任何提示文字：原先挂在悬停里的那几句，现在各归各位——
 * 格式、体积、时长进了上行规格；"没重跑"升成条内字样；归档路径留在下方播放条
 * （它本来就在显示），行里不重复一遍。
 *
 * @param {any} job
 */
function isCurrent(job) {
  return Boolean(job.audio_url) && store.player.url === job.audio_url
}

/**
 * 已播比例：只对播放条上正放（或停着）的那一条算，别的行没有位置可讲。
 *
 * @param {any} job
 */
function progressOf(job) {
  if (!isCurrent(job) || !(store.player.duration > 0)) return 0
  return Math.min(Math.max(store.player.position / store.player.duration, 0), 1)
}

/**
 * 规格那一格窄，分钟不补零（`3:07`）；其余口径与播放条一致。
 *
 * @param {any} seconds
 */
function clock(seconds) {
  return formatClock(seconds, { padded: false })
}

/** 每条音频的时长：WaveBars 解码画波形时顺手报的，不再单独探一次元数据。
 *  波形没解出来（格式怪、文件坏了）就没有时长可显示，行里就不占这一格。 */
const durations = reactive(/** @type {Record<string, number>} */ ({}))
/**
 * @param {string} url
 * @param {any} seconds
 */
function rememberDuration(url, seconds) {
  const value = Number(seconds) || 0
  if (!url) return
  if (value > 0) durations[url] = value
  else delete durations[url]
}

/**
 * 这一条的时长：正在放的那条以播放器为准（元数据已到），其余看解码结果。
 *
 * @param {any} job
 */
function durationOf(job) {
  if (!job?.audio_url) return 0
  if (store.player.url === job.audio_url && store.player.duration > 0) {
    return store.player.duration
  }
  return durations[job.audio_url] || 0
}

/**
 * 波形的定位比例：当前这条给已播位置，其余从 0 起。
 *
 * @param {any} job
 */
function barFraction(job) {
  return isCurrent(job) ? progressOf(job) : 0
}

/**
 * 读屏念的是"现在在哪 / 一共多长"，不是一句百分比。
 *
 * @param {any} job
 */
function barValueText(job) {
  const total = durationOf(job)
  if (!total) return ''
  return `${clock(barFraction(job) * total)} / ${clock(total)}`
}

/** 悬停在哪一格就在波形上报哪一刻（与底部播放条的 --hx 气泡同一套）。 */
const barHover = ref(/** @type {any} */ (null))
/**
 * @param {any} job
 * @param {any} event
 */
function onBarHover(job, event) {
  if (!job.audio_url || !durationOf(job)) {
    barHover.value = null
    return
  }
  const box = event.currentTarget.getBoundingClientRect()
  const frac = box.width ? (event.clientX - box.left) / box.width : 0
  barHover.value = { url: job.audio_url, frac: Math.min(Math.max(frac, 0), 1) }
}

/**
 * 键盘左右各走 5%：没在放的那条从这里起播，当前这条就地定位。
 *
 * @param {any} job
 * @param {any} delta
 */
function nudgeBar(job, delta) {
  if (!job.audio_url) return
  const next = Math.min(Math.max(barFraction(job) + delta, 0), 1)
  if (isCurrent(job)) actions.seekFraction(next)
  else actions.playAll([trackOf(job)], 0, next)
}

/**
 * 撤下这一条：只把它移出这一批（「全部播放」与行内波形都不再认它），
 * 生成音频的文件仍在缓存里，不在这里删。
 *
 * @param {any} job
 */
function dismissResult(job) {
  actions.dismissSynthJob(job)
}

/**
 * 失败的那一行只重跑这一台，用当前正文与参数。
 *
 * @param {any} job
 */
function retryResult(job) {
  actions.retrySynthJob(job)
}

/**
 * 点波形哪一格就从哪儿听：只放这一条；整批连着听在块头「全部播放」。
 *
 * @param {any} job
 * @param {any} event
 */
function onBar(job, event) {
  if (!job.audio_url) return
  const box = event.currentTarget.getBoundingClientRect()
  const frac = box.width ? (event.clientX - box.left) / box.width : 0
  if (isCurrent(job)) {
    actions.seekFraction(frac)
    return
  }
  actions.playAll([trackOf(job)], 0, frac)
}

/** 灰态要给理由——"点了没反应"是最难查的一类界面问题。 */
const reason = computed(() => {
  if (!store.synth.text.trim()) return '请输入文本'
  if (!store.synth.voiceId) return '请选择音色'
  // 按"手上真有的那几台"算，不按勾选表：勾过的那台可能已被停用（另一个窗口，
  // 或刚在「引擎」页关掉）。勾选表里留着它，按钮就永远是亮的，点了才发现没有引擎。
  if (!pickedEngines.value.length) return '请至少选择一台引擎'
  return ''
})

/** 没就绪的引擎不拦着跑——它自己会说出缺什么，这里只提一句。 */
const warning = computed(() => {
  const notReady = pickedEngines.value.filter((engine) => !engine.ready)
  if (!notReady.length) return ''
  const names = notReady.map((engine) => engine.label || engine.key).join('、')
  return `${names} 尚未就绪`
})

const canRun = computed(() => !reason.value && !store.synthRunning)

/** 这次要用的参考音频：显式挑过就用挑的，否则是主样例（与那一格判据一致）。 */
const refSample = computed(() => {
  const path = store.synth.values.reference_audio
  if (path) return store.synth.samples.find((s) => s.path === path) || null
  return store.synth.samples.find((s) => s.is_main) || null
})

/** 要逐字稿的那几台（引擎自己声明）。 */
const wantsTranscript = computed(() =>
  pickedEngines.value.filter(/** @param {any} e */ (e) => e.synthesis_spec?.needs_transcript === true)
)

const refNote = computed(() => {
  const names = wantsTranscript.value.map(/** @param {any} e */ (e) => e.label)
  if (!names.length) return ''
  const sample = refSample.value
  if (!sample || sample.text) return ''
  return `缺逐字稿：发送前自动识别「${sample.file}」。`
})

async function run() {
  await actions.runSynthesis()
}

/** 「全部播放」的队列：页面上能听的那几条（含上一轮留下的）。 */
const playable = computed(() =>
  store.synthJobs
    .filter((job) => job.audio_url)
    .map((job) => ({
      url: job.audio_url,
      label: job.label,
      sub: job.saved_rel || '生成的音频',
    }))
)

/**
 * 这一行对应的曲目：单条播放与「全部播放」共用同一个形状。
 *
 * @param {any} job
 */
function trackOf(job) {
  return { url: job.audio_url, label: job.label, sub: job.saved_rel || '生成的音频' }
}

/**
 * 这一条是不是底栏正在放的那一条（暂停也算当前这条）。
 *
 * @param {any} job
 */
function playingJob(job) {
  return Boolean(job.audio_url) && store.player.url === job.audio_url && store.player.playing
}

/**
 * 点某一行的播放钮 = 只放这一条；整批连着听在块头「全部播放」。
 * 正在放的就是它 = 暂停/继续。
 *
 * @param {any} job
 */
function toggleResult(job) {
  if (!job.audio_url) return
  if (store.player.url === job.audio_url) {
    actions.togglePlay()
    return
  }
  actions.playAll([trackOf(job)], 0)
}

/**
 * @param {any} start
 */
function playAllResults(start) {
  actions.playAll(playable.value, start)
}
</script>
