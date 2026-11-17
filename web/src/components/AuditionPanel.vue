<template>
  <!-- 生成样例 = 详情页标题行右端的一颗按钮 + 这里这个窗口。
       从前它常驻在正文里（标题 + 齿轮 + 一行"挑一句/自己写"+ 生成），把上面那份
       样例区往下顶；而它其实是"想生成才走"的一步，不是每次都要看的东西。
       现在按钮在标题行最右端，点开就是这里：**文案与参数在同一个窗口**，
       不要"先开参数窗口再回上一格写句子"那样两跳。 -->
  <!-- 与生成页那几个对话框同一处加载：`--panel` / `--mask-strong` 这些旧变量名定义在
       .app-shell 上（见 style.css 顶部注释），挂在 body 上整块弹窗连遮罩一起丢背景，
       变成"纯透明"。shell 是 fixed + inset:0 且不含 transform，fixed 子元素不被它裁剪。 -->
  <Teleport :to="overlayTarget">
    <div class="modal-mask" @click.self="emit('close')">
      <div class="modal params audition" role="dialog" aria-modal="true" aria-label="生成样例">
        <h3>生成样例 · {{ engine?.label || voice.source }}</h3>

        <!-- 文案：挑一句现成的，或者就在框里改。下拉是**自己画的**（`<input list>`
             的原生下拉会按框里现在的值过滤候选，而框里预填着一整句，点开就只剩那一条）。
             句子按**分组**摆（诗句 / 哲学 / 文学…），那一组由用户在设置页里定——
             前端不认识任何一个组名，也不自带任何一句。 -->
        <div class="aud-pick-box">
          <input
            v-model="store.audition.text"
            class="aud-pick"
            placeholder="输入文本"
            aria-label="文案"
            @focus="openList = true"
            @blur="openList = false"
            @keydown.escape.stop="onEscape"
          />
          <button
            ref="caretEl"
            type="button"
            class="aud-caret"
            :title="`现成的句子（${lineCount} 句 · ${store.texts.length} 组）`"
            aria-label="现成的句子"
            @mousedown.prevent
            @click="openList = !openList"
          >
            ▾
          </button>
          <!-- 弹层挂在 body 上：窗口本身要滚，绝对定位的菜单会被窗口裁掉。 -->
          <Popover
            :open="openList"
            :anchor="caretEl"
            :width="360"
            label="现成的句子"
            @close="openList = false"
          >
            <div class="aud-menu">
              <template v-for="group in store.texts" :key="group.name">
                <div v-if="(group.lines || []).length" class="aud-menu-group">
                  {{ group.name }}
                </div>
                <button
                  v-for="line in group.lines || []"
                  :key="`${group.name}:${line}`"
                  type="button"
                  :class="{ on: line === store.audition.text }"
                  @mousedown.prevent="pick(line)"
                >
                  {{ line }}
                </button>
              </template>
              <div v-if="!lineCount" class="aud-menu-empty">
                文案库是空的，去「设置 → 文案」加几句。
              </div>
            </div>
          </Popover>
        </div>

        <!-- 详细参数：只画这台引擎认得的那几格，画法与生成页同一个组件。 -->
        <ParamFields
          v-if="fields.length"
          :fields="fields"
          :values="store.audition.values"
          :extra-options="extraOptions"
          :presets="presets"
          @update="onField"
        />
        <div v-else class="hint">这台引擎没有可调的参数。</div>

        <div v-if="job && job.state === 'failed'" class="hint" data-tone="warn">
          {{ job.detail }}
        </div>
        <!-- 生成成功不另画播放器：刚落下的文件就是这条音色的一条样例，
             已在上面那份列表里；这里只报结果存在哪。 -->
        <div v-if="job && job.state === 'ok'" class="hint">
          {{ job.detail }}
          <span v-if="job.saved_rel"> · {{ job.saved_rel }}</span>
        </div>
        <!-- "替你做了什么"与"哪几件没做到"：用户在哪一个入口按下生成，
             就该在哪一个入口看到同一句交代。 -->
        <div v-for="note in (job?.notes || [])" :key="note" class="job-note">{{ note }}</div>

        <div class="modal-foot">
          <button class="btn" @click="emit('close')">关闭</button>
          <button class="btn primary" :disabled="!canRun" :title="reason" @click="run">
            {{ store.audition.running ? '生成中' : '生成' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, onMounted, ref, watch } from 'vue'
import { store, actions } from '../store'
import ParamFields from './ParamFields.vue'
import Popover from './ui/Popover.vue'
import { overlayTarget } from '../composables/overlayTarget'

const props = defineProps({
  voice: { type: Object, required: true },
  //: 这一段归哪台引擎。平台音色留空即可（由 `source` 推出）；
  //: 落在引擎保存目录里的音色是本地音色，必须显式给（后端的 `voice.engine`）。
  engineKey: { type: String, default: '' },
  //: 拿哪条音色去合成：平台音色留空 = 用它自己；引擎目录里那种要给对应的引擎音色 id。
  voiceId: { type: String, default: '' },
})
const emit = defineEmits(['close'])

/** 弹窗挂哪儿：壳登记的那个元素（`--panel` 那套别名只在壳上，body 上没有）。 */

/** 现成句子菜单开没开。 */
const openList = ref(false)
const caretEl = ref(/** @type {any} */ (null))

/**
 * 选一句现成的：填进输入框，顺手收起菜单。
 * @param {string} line
 */
function pick(line) {
  store.audition.text = line
  openList.value = false
}

/** 框里的 Esc：先收菜单，菜单已经收着才关窗口。
 *  （`stop` 掉了冒泡，不然详情那一层的 Esc 会连人带窗一起关。） */
function onEscape() {
  if (openList.value) {
    openList.value = false
    return
  }
  emit('close')
}

/** 这一块归哪台引擎（平台音色的 ``source`` 就是它的 key；引擎目录里那些由调用方给）。 */
const engine = computed(
  () => store.engines.find((e) => e.key === (props.engineKey || props.voice.source)) || null
)

/** 真正发给引擎的那个音色 id。 */
const targetId = computed(() => props.voiceId || props.voice.id)

/** 文案库一共几句（那颗箭头的悬停要说）：组名与句子都是用户在设置页定的，
 *  这里只数数，不认识任何一句。 */
const lineCount = computed(() =>
  store.texts.reduce((total, group) => total + (group.lines || []).length, 0)
)

/** 空着一句话都不给发，所以进来就先站好一句（第一组第一句是短句，听音色本身）。
 *  一句都没有时不硬填：文案库是用户自己的，空了就在框里自己打一句。
 *  窗口里不再摆语气词标签：正文是"挑一句/改一句"，插入标签是写正文时的动作，
 *  那件事在生成页做（那里有 `TextEditor`）——这里不重复一份不同的入口。 */
watch(
  () => props.voice?.id,
  () => {
    openList.value = false
    if (!store.audition.text) store.audition.text = actions.firstText()
  },
  { immediate: true }
)

/** 这台引擎认得的那几格（判据与按钮上那颗小点同一个：`actions.auditionFields`）。 */
const fields = computed(() => actions.auditionFields(engine.value))

/** 候选（``value_choices``）——按参数键分的表，由勾选的引擎合并而成。 */
const extraOptions = computed(() => actions.choicesOf(engine.value ? [engine.value] : []))

/** 引导指令预设：用户词表的生效值（后端算好，不按引擎分组）。 */
const presets = computed(() => ({ instruction: store.lexicon.instructions?.effective || [] }))

/** 指令预设不在引擎自描述里现拿了：开窗口时确保词表已加载（其余页面可能先取过）。 */
onMounted(() => {
  void actions.ensureLexicon()
})

const job = computed(() => store.audition.job)

const canRun = computed(() => {
  if (store.audition.running) return false
  return Boolean(engine.value && targetId.value && store.audition.text.trim())
})

const reason = computed(() => {
  if (!engine.value) return `「${props.voice.source}」这台引擎没装上`
  // 引擎目录里的音色可能**对着一条已经不在库里**的音色（平台条目被删过）：
  // 那时合成没有目标，说清楚比让用户点下去再看引擎报"音色 id 无效"强。
  if (!targetId.value) {
    return '该音频对应的引擎音色已不在库中，请在其引擎面板点击 ⟳ 重新拉取清单。'
  }
  if (!store.audition.text.trim()) return '先挑一句要念的话'
  return ''
})

/**
 * 空串 = 没要求（那一格不进请求体），存法由 store 那一条统一决定。
 * @param {string} key
 * @param {any} value
 */
function onField(key, value) {
  store.audition.values[key] = value
}

function run() {
  // 引擎与音色 id 显式传下去：`voice` 可能是引擎目录里那条本地音色，推不出该发给谁。
  actions.runAudition(props.voice, { engineKey: engine.value?.key || '', voiceId: targetId.value })
}
</script>
