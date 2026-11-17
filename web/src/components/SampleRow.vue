<template>
  <!-- 一条样例 = 一根长条，里面是两段：左边「这段在说什么」（说话内容一行 + 情绪与标签一行），
       右边「波形 / 秒数 / 播放 / 主样例 / 更多」一串控制。
       控制串整体贴右、`align-items: center`，所以文字不会比圆钮低半行——从前那颗 ▶
       靠 `margin-top` 顶住，正是让文字看起来"往下沉"的原因。
       整条仍然可点即播（不必非去点那颗圆钮）。 -->
  <div
    class="sample"
    :class="{ 'is-main': sample.is_main, 'is-playing': playing }"
    role="button"
    tabindex="0"
    :aria-label="playing ? '停止播放' : '播放这一段'"
    :title="sample.file"
    @click="togglePlay"
    @keydown.enter.prevent="togglePlay"
    @keydown.space.prevent="togglePlay"
  >
    <!-- 左：两行文字。两行都从同一条左边界起，间距收紧，整块在条内垂直居中。 -->
    <div class="sample-body">
      <!-- 说话内容：与描述同一套无框编辑，点进去才变成可改的。
           没识别过的语音只摆一句「暂未识别」（灰色占位字，与其它说明文同一档），
           不再写一整句"这段音频念的那句话"——那是在问用户填空，而不是在说状态。 -->
      <InlineText
        class="sample-text"
        :model-value="text"
        placeholder="暂未识别"
        aria-label="说话内容"
        :maxlength="300"
        @change="saveText"
        @click.stop
        @keydown.stop
      />

      <!-- 第二行只有"这条声音什么味道"：情绪 + 这段自己的标签（「＋」那颗是加标签的入口）。 -->
      <div class="sample-meta" @click.stop>
        <!-- 情绪与角色卡上的「语调 / 音色」是同一个框（TagChips）：点一下加、再点一下去掉。
             一段声音常常不是一种情绪能概括的（「严肃、轻蔑」），都标上，
             按情绪找样例时命中任意一种都算。 -->
        <TagChips
          :model-value="emotionWords"
          :groups="emotionGroups"
          :tone-of="emotionTone"
          label="情绪"
          add-label="情绪"
          @change="saveEmotion"
        />
        <TagChips :model-value="tags" label="标签" add-label="标签" @change="saveTags" />
      </div>

      <!-- 生成这段时用的参数：只显示当时发出去的什么，不能改——它是来路的记录。 -->
      <div v-if="paramsText" class="sample-params">{{ paramsText }}</div>
    </div>

    <!-- 右：控制串。顺序 = 波形 → 秒数 → 播放 → 主样例 → 更多。
         每一项都 `flex: none`，从左到右这条线就站得住，互不推挤。 -->
    <div class="sample-side">
      <WaveBars v-if="audioUrl" :url="audioUrl" />
      <!-- 秒数紧跟波形：先说"多长"，再说"怎么放"。 -->
      <span class="sample-dur" @click.stop>{{ durationText }}</span>
      <button
        type="button"
        class="sample-play-btn"
        :class="{ 'is-on': playing }"
        :aria-label="playing ? '停止播放' : '播放这一段'"
        :title="playing ? '停止' : '播放'"
        @click.stop="togglePlay"
      >
        <Play v-if="!playing" :size="14" fill="currentColor" />
        <Square v-else :size="12" fill="currentColor" />
      </button>
      <!-- 主样例：**一颗按钮两副长相**——亮着就是主样例，灰的就是没指定。
           设别的会自动换过去，所以不需要"取消指定"这第二个动作。
           宽度给最小值：两副长相的字数不同，盒子一样宽，播放钮才不会被推来推去。
           生成页点开这张详情是为了"这次用哪一段当参考"：同一个位置改说「使用」，
           点它就把这一条钉成参考音频（卡片列顶上一张冻结卡），主样例那条规矩不动。 -->
      <button
        v-if="inSynth || multiple"
        type="button"
        class="sample-main set-main"
        :class="{ 'is-on': inSynth ? chosen : sample.is_main }"
        :disabled="inSynth ? chosen : sample.is_main"
        :aria-pressed="inSynth ? chosen : sample.is_main"
        :title="
          inSynth
            ? chosen
              ? '这一条就是这次生成的参考音频'
              : '用这一条当这次生成的参考音频'
            : sample.is_main
              ? '这一条就是主样例（设别的会自动换过去）'
              : '只指定音色名时，引擎默认用这一条做参考'
        "
        @click.stop="inSynth ? useReference() : setMain()"
      >
        <Check v-if="inSynth ? chosen : sample.is_main" :size="12" :stroke-width="1.8" />
        <span v-if="inSynth">{{ chosen ? '使用中' : '使用' }}</span>
        <span v-else>{{ sample.is_main ? '主样例' : '设为主样例' }}</span>
      </button>
      <!-- 「更多」：识别与移除都收在这里。菜单**不再只在识别引擎就绪时出现**——
           「移除」是本地动作，与识别引擎配没配好无关；没就绪时就只剩那一项。 -->
      <button
        ref="moreEl"
        class="sample-more"
        :disabled="working"
        title="更多操作"
        aria-label="更多操作"
        :aria-expanded="menuOpen"
        @click.stop="menuOpen = !menuOpen"
      >
        <MoreHorizontal :size="16" />
      </button>
      <Popover :open="menuOpen" :anchor="moreEl" :width="124" label="更多操作" @close="menuOpen = false">
        <!-- 两项一个长相：都是两个字、都是普通字色。移除确实动磁盘，但危险那句话
             在确认框里说，不在菜单上染色——菜单上两条不同色，看着像两类动作。 -->
        <button
          v-if="asrReady"
          type="button"
          class="sample-menu-item"
          :disabled="working"
          title="用语音识别听出这段音频念的话"
          @click="recognize"
        >
          {{ working ? '识别中…' : '识别' }}
        </button>
        <button
          type="button"
          class="sample-menu-item"
          title="从这条音色里移除这一段（文件进回收站，可还原）"
          @click="remove"
        >
          移除
        </button>
      </Popover>
    </div>
  </div>
</template>

<script setup>
// @ts-check
import { computed, onMounted, ref, watch } from 'vue'
import { Check, MoreHorizontal, Play, Square } from 'lucide-vue-next'
import { api } from '../api'
import { store, actions, labelOf, say, vocab } from '../store'
import InlineText from './ui/InlineText.vue'
import Popover from './ui/Popover.vue'
import TagChips from './ui/TagChips.vue'
import WaveBars from './ui/WaveBars.vue'
import { emotionTone } from './ui/tones'

const props = defineProps({
  voice: { type: Object, required: true },
  sample: { type: Object, required: true },
  multiple: { type: Boolean, default: false },
})

const text = ref(props.sample.text)
const emotion = ref(props.sample.emotion)
const tags = ref([...(props.sample.tags || [])])
const working = ref(false)
const menuOpen = ref(false)
const moreEl = ref(/** @type {any} */ (null))

/** 识别引擎是否就绪——没就绪就不摆那个入口。
 *  用 `ready` 而不是 `current`：引擎配错地址时后者仍非空，点了只能报错。 */
const asrReady = computed(() => Boolean(store.asr.ready))

/** 这张详情是从生成页点开的、而且就是这次生成的那条音色：那颗按钮改说「使用」，
 *  点它 = 把这一条指定为这次的参考音频。
 *  判据看**打开的那条音色**，不看当前选中：从生成页点开 A 时若 `voiceId` 还停在 B，
 *  在这里点「使用」应当连音色一起落到 A（`pinReference` 会一起改）。 */
const inSynth = computed(
  () => store.view === 'synth' && Boolean(store.detail?.id) && store.detail.id === props.voice.id
)
/** 这一条是不是"这次的参考"。 */
const chosen = computed(
  () => inSynth.value && store.synth.values.reference_audio === props.sample.path
)

/** 听出这段音频念的话，填进「说话内容」。已写过字时先问一句（可能是手打的），
 *  空着就直接填——那种时候"识别一下"与"填上"是同一件事。 */
async function recognize() {
  menuOpen.value = false
  working.value = true
  try {
    const result = await api.transcribe(props.voice.id, props.sample.file)
    if (result.language_written) {
      await actions.reloadVoice(props.voice.id)
      const label =
        labelOf(vocab.languages, result.language_code) || result.language || result.language_code
      say(`识别完成 · 语种已写入音色：${label}`, { kind: 'success' })
    }
    if (!result.text) {
      say('未识别出文字，音频可能不含人声。')
      return
    }
    if (text.value.trim() && !window.confirm(`用识别结果替换「说话内容」？\n\n${result.text}`)) {
      return
    }
    text.value = result.text
    await actions.saveSample(props.sample.file, { text: result.text })
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    working.value = false
  }
}
watch(
  () => props.sample,
  (value) => {
    text.value = value.text
    emotion.value = value.emotion
    tags.value = [...(value.tags || [])]
  }
)

const audioUrl = computed(() => props.sample.audio_url)
/** 底栏正在放的是**这一段**（暂停时也算"当前这条"）：长条据此显示 ■/▶。 */
const isCurrent = computed(() => store.player.url === audioUrl.value && Boolean(audioUrl.value))
const playing = computed(() => isCurrent.value && store.player.playing)
const durationText = computed(() =>
  props.sample.duration > 0 ? `${props.sample.duration.toFixed(1)}s` : '时长未知'
)

/** 生成这一段时用的参数（后端已把中文名翻好）。本机录音是空数组，整行不出现。 */
const paramsText = computed(() =>
  (props.sample.params || []).map(/** @param {any} row */ (row) => `${row.label} ${row.value}`).join(' · ')
)

function togglePlay() {
  // 底栏要说得出"这是谁的哪一段"——用说话内容，不用文件名。
  if (audioUrl.value) {
    actions.play({
      url: audioUrl.value,
      label: text.value.trim() || props.sample.file,
      sub: `${props.voice.shown_name}${props.sample.is_main ? ' · 主样例' : ''}`,
      // 这一行永远属于抽屉里的那条音色：播放条上的收藏 / 指定也认它。
      voice: props.voice,
    })
  }
}

/** 情绪候选：生效词表分组摆（与创作页、角色卡上的语调同一个来源），自填词从弹层输入框进。 */
const emotionGroups = computed(() =>
  (store.lexicon.emotions?.groups || []).map(/** @param {any} group */ (group) => ({
    label: group.label,
    options: (group.words || []).map(/** @param {string} word */ (word) => [word, word]),
  }))
)

/** 样例上的情绪存成一串（「严肃、轻蔑」），这个框吃的是数组：拆与拼都按后端那个分隔符走。 */
const EMOTION_SEP = '、'

const emotionWords = computed(() =>
  emotion.value
    .split(EMOTION_SEP)
    .map(/** @param {string} word */ (word) => word.trim())
    .filter(Boolean)
)

/** 词表按需取一次（有缓存）：音色库这边不经过生成页，得自己开口。 */
onMounted(() => {
  void actions.ensureLexicon()
})

/**
 * 说话内容：停手才存（无框编辑组件负责触发）。
 *
 * @param {any} value
 */
async function saveText(value) {
  text.value = value
  if (value === props.sample.text) return
  await actions.saveSample(props.sample.file, { text: value })
}

/**
 * 情绪是点选出来的离散值，选中即存，不等失焦。
 * 可多选：每点一下存一次，存的是整串（「严肃、轻蔑」），后端按 `、` 拆开比对。
 *
 * @param {any} words
 */
async function saveEmotion(words) {
  const value = words.join(EMOTION_SEP)
  emotion.value = value
  await actions.saveSample(props.sample.file, { emotion: value })
}

/**
 * 标签是"加一颗/删一颗"的动作，不等 blur——变了就存。
 *
 * @param {any} next
 */
async function saveTags(next) {
  tags.value = next
  await actions.saveSample(props.sample.file, { tags: next })
}

/** 指定主样例：后端本来就会把别的清掉，所以这里只有"设"这一个动作。 */
async function setMain() {
  await actions.setMain(props.sample.file)
}

/** 生成页的「使用」：这一条就是这次生成的参考音频，卡片列顶上立刻钉住它。
 *  选完就关掉详情：这一步的收尾是回生成页看那张冻结卡，不是留在详情里看别的东西
 *  （要换一条，点冻结卡又回到这张详情，不用再从头找）。 */
function useReference() {
  actions.pinReference(props.voice, props.sample)
  actions.closeDetail()
}

/** 移除这一段：文件进回收站（可还原），所以点之前把那句话摆出来——
 *  删的是磁盘上的东西，不是一个能一键撤销的表单字段。 */
async function remove() {
  menuOpen.value = false
  const where = props.sample.is_main ? '\n\n它现在是主样例，移除后会自动落到另一条。' : ''
  const ok = window.confirm(
    `从「${props.voice.shown_name}」移除这段样例？\n\n${props.sample.file}${where}\n\n文件会移到回收站（可还原）。`
  )
  if (!ok) return
  try {
    await actions.removeSample(props.sample.file)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  }
}
</script>
