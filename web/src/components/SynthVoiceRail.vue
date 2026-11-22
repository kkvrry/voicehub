<template>
  <!-- 生成页最左的卡片列：数据就是**左栏树当前选中的那一段**（`store.voices` 已按树查好）。
       搜索与性别 / 年龄 / 语言 / 情绪筛选统一在顶部搜索栏，这一列只负责把筛好的音色摆出来。 -->
  <aside class="synth-rail">
    <div class="rail-fixed">
      <!-- 这一栏的功能条：左边写它管什么，右边是三个动作。目录名与条数留给左栏树
           （树的选中态已经说明范围），这一行不重复它。 -->
      <div class="rail-head">
        <!-- 生成页这一栏管的是"这次用谁的声音"；创作页与处理页只是挑音色，写「音色」。 -->
        <span class="rail-title">{{ target === 'synth' && pickable ? '参考音频' : '音色' }}</span>
        <div class="rail-acts" role="group" aria-label="参考音频">
          <!-- 录一段：录下的就是这次的参考，落在下面那张冻结卡上。 -->
          <MicButton
            v-if="recordable"
            mode="audio"
            icon-only
            label="录一段"
            class="rail-act"
            @audio="onRecorded"
            @error="(message) => say(message)"
          />
          <button
            type="button"
            class="rail-act"
            :disabled="!playable.length"
            :title="
              playable.length
                ? `按列表顺序播放这 ${playable.length} 个音色的主样例`
                : '当前列表里没有可播放的样例'
            "
            aria-label="按顺序播放当前列表"
            @click.stop="playAllList"
          >
            <Play :size="14" :stroke-width="1.8" />
          </button>
          <button
            v-if="recordable"
            type="button"
            class="rail-act"
            :disabled="!designEngines.length"
            :title="
              designEngines.length
                ? '用一段描述生成音色，直接用作这次的音色'
                : '没有引擎声明音色设计能力'
            "
            aria-label="使用设计音色"
            @click.stop="designOpen = true"
          >
            <Sparkles :size="14" :stroke-width="1.8" />
          </button>
        </div>
      </div>
    </div>

    <!-- 冻结卡：这次用谁的那一段。置顶、不随列表滚走——「参考音频」那一格撤掉之后，
         它是"当前参考是谁"唯一的落点，所以不给它一个能被滚出去的位置。
         长相与列表里的卡一样（同一套行高、同一个头像一个底），"它是钉住的那张"由三处说：
         斜盖着的一枚「参考」章（水印，压在内容下面，不占横向位置）、一层朱砂淡底、
         左沿一道朱砂（后两处见 style.css 的 `.rail-frozen`：强调色是列表"选中"的语法，
         这里换成印章的朱砂，两者才分得开）。
         章**不看有没有样例**：这一栏说的是"这次用这条音色配音"，盖的是这件事，
         不是"它有一段参考片段"——设计音色没有片段，照样是这次用的那条。 -->
    <div
      v-if="target === 'synth' && pickable && reference"
      class="rail-card rail-frozen"
      :class="{ 'is-plain': !reference.canChange }"
      role="button"
      tabindex="0"
      :title="reference.hint"
      @click="openReference"
      @contextmenu="onReferenceMenu"
      @keydown.enter.prevent="openReference"
      @keydown.space.prevent="openReference"
    >
      <!-- 章：朱文（只有框和字，不铺底），框贴着两个字走——框比字略大一圈就够，
           留太多空白就读成"装饰框"了。字取隶书，古但认得出；两处缺墨是硬边小洞。 -->
      <svg
        class="rail-seal"
        viewBox="0 0 26 40"
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          <mask id="vh-seal-cut">
            <rect width="26" height="40" fill="#fff" />
            <rect x="17.6" y="5.6" width="2.4" height="2.4" fill="#000" />
            <rect x="5.8" y="30.8" width="2" height="2" fill="#000" />
          </mask>
        </defs>
        <g mask="url(#vh-seal-cut)">
          <rect class="seal-frame" x="1.1" y="1.1" width="23.8" height="37.8" rx="2.5" />
          <text class="seal-char" x="13" y="16">参</text>
          <text class="seal-char" x="13" y="33">考</text>
        </g>
      </svg>
      <component
        :is="reference.url ? 'button' : 'span'"
        :type="reference.url ? 'button' : undefined"
        class="rail-cover"
        :style="reference.coverStyle"
        :title="reference.url ? (reference.playing ? '暂停' : '试听这一段参考') : ''"
        :aria-label="reference.url ? '试听参考音频' : undefined"
        @click.stop="toggleReference"
      >
        <img v-if="reference.coverUrl" :src="reference.coverUrl" alt="" />
        <Mic v-else-if="!reference.isVoice" :size="14" />
        <template v-else>{{ reference.initial }}</template>
        <span v-if="reference.url" class="rail-cover-play" aria-hidden="true">
          <Pause v-if="reference.playing" :size="12" :stroke-width="1.8" fill="currentColor" />
          <Play v-else :size="12" :stroke-width="1.8" fill="currentColor" />
        </span>
      </component>
      <span class="rail-ident">
        <span class="rail-name-row">
          <span class="rail-name">{{ reference.name }}</span>
          <span
            v-if="reference.design"
            class="card-tag"
            :class="`tone-${toneOf('设计')}`"
            title="按描述生成的音色，引擎在右边选"
          >设计</span>
          <GenderPicker
            v-if="reference.gender"
            :model-value="reference.gender"
            :options="vocab.genders"
            readonly
          />
        </span>
        <!-- 与列表里的卡同一副长相：名字行 + 样例数，不摆来源与标签（理由见下）。 -->
        <span class="rail-sub">
          <span class="rail-samples">{{ reference.count }}</span>
        </span>
      </span>
      <!-- 冻结卡也要能换：弹窗一律从按钮进，卡片本体只负责"放这段声音"。
           设计音色两头都不摆：没有样例可换，删除也不在这里——它已经是库里的一条，
           要删去音色库那一组（左栏「设计音色」）打开它，那里才是管增删的地方。 -->
      <button
        v-if="reference.isVoice && reference.canChange"
        type="button"
        class="rail-assign"
        title="换一条样例当这次的参考音频"
        @click.stop="chooseSample(reference.voice)"
      >
        换样例
      </button>
    </div>

    <div ref="listEl" class="rail-list">
      <div
        v-if="padTop"
        class="rail-pad"
        :style="horizontal ? { width: `${padTop}px` } : { height: `${padTop}px` }"
        aria-hidden="true"
      />
      <div
        v-for="voice in rendered"
        :key="voice.id"
        class="rail-card"
        :class="{ on: voice.id === activeId }"
        role="button"
        tabindex="0"
        :aria-pressed="pickable ? voice.id === activeId : undefined"
        :title="voice.shown_name"
        @click="pick(voice)"
        @contextmenu="openMenu(voice, $event)"
        @keydown.enter.prevent="pick(voice)"
        @keydown.space.prevent="pick(voice)"
      >
        <button
          type="button"
          class="rail-cover"
          :style="coverStyle(voice)"
          :title="voice.sample_count ? '播放主样例' : voice.shown_name"
          :aria-label="voice.sample_count ? `播放 ${voice.shown_name} 的主样例` : voice.shown_name"
          @click.stop="play(voice)"
        >
          <CoverImage
            v-if="voice.cover_url && showAvatarImage"
            :src="voice.cover_url"
            width="30"
            height="30"
            alt=""
          />
          <template v-else>{{ initialOf(voice) }}</template>
          <span v-if="voice.sample_count" class="rail-cover-play" aria-hidden="true">
            <Pause v-if="playingId === voice.id" :size="12" :stroke-width="1.8" fill="currentColor" />
            <Play v-else :size="12" :stroke-width="1.8" fill="currentColor" />
          </span>
        </button>
        <span class="rail-ident">
          <!-- 与主页卡片同一套识别方式：名字（后面跟性别圆标、标签）、下面是来源与样例数。
               目录名不摆——扫列表要看的是"这条声音什么味道"。 -->
          <span class="rail-name-row">
            <span class="rail-name">{{ voice.shown_name }}</span>
            <!-- 设计音色：不写引擎名（它不属于哪一家），只在名字后面盖一枚「设计」。
                 放在名字后而不是下面那排标签里——多一行标签，这一行就比旁边的卡高。 -->
            <span
              v-if="actions.isDesignVoice(voice)"
              class="card-tag"
              :class="`tone-${toneOf('设计')}`"
              title="按描述生成的音色，引擎在右边选"
            >设计</span>
            <!-- 性别只是标识：这一列是挑音色用的，点一下就把标签改掉太容易误触，
                 生成页与创作页都按只读显示；要改去音色库的详情面板。 -->
            <GenderPicker
              v-if="voice.gender"
              :model-value="voice.gender"
              :options="vocab.genders"
              readonly
            />
          </span>
          <!-- 副行只留样例数：来源由左栏分组回答，标签在音色库的卡片上有。
               这一列按"整张卡"翻，卡高必须齐——多一行标签就会让一部分卡高出一截，
               翻起来必然露出半张；名字行里塞标签又会把名字挤成四个字加省略号。 -->
          <span class="rail-sub">
            <span class="rail-samples">{{ voice.sample_count || 0 }} 条样例</span>
          </span>
        </span>
        <!-- 创作页：这一列是"给角色或章节挑音色"，收藏让位给指定。
             生成页：选中由卡片红边表示；右端这颗管"挑哪一段"，点卡片本身则直接用主样例。 -->
        <button
          v-if="assignable"
          type="button"
          class="rail-assign"
          :title="`指定给${store.assignTarget || '角色'}`"
          @click.stop="assign(voice)"
        >
          指定
        </button>
        <button
          v-else-if="pickable"
          type="button"
          class="rail-assign"
          :title="target === 'pick' ? '把这条音色的样例加入处理序列' : '挑一段样例当这次的参考音频'"
          @click.stop="target === 'pick' ? addCard(voice) : chooseSample(voice)"
        >
          {{ target === 'pick' ? '添加' : '选样例' }}
        </button>
      </div>
      <div
        v-if="padBottom"
        class="rail-pad"
        :style="horizontal ? { width: `${padBottom}px` } : { height: `${padBottom}px` }"
        aria-hidden="true"
      />
      <p v-if="!visible.length" class="hint rail-note">
        {{ store.loading ? '加载中……' : filtering ? '没有匹配的音色' : '这个目录下还没有音色' }}
      </p>
    </div>

    <!-- 设计音色：不靠参考音频，用一段描述直接建一条音色，建完就是这次的音色。 -->
    <DesignVoiceDialog v-if="recordable" :open="designOpen" @close="designOpen = false" />
  </aside>
</template>

<script setup>
// @ts-check
import { computed, ref } from 'vue'
import { Mic, Pause, Play, Sparkles } from 'lucide-vue-next'
import { actions, say, store, vocab } from '../store'
import { useSettingsStore } from '../stores/settings'
import { useAvatarImage } from '../composables/avatar'
import { avatarFill } from '../composables/avatarColor'
import { useCardPaging } from '../composables/cardPaging'
import { openObjectMenu } from '../composables/contextMenu'
import { useRailWindow } from '../composables/railWindow'
import { voiceMenuContext } from '../composables/voiceMenu'
import CoverImage from './ui/CoverImage.vue'
import DesignVoiceDialog from './DesignVoiceDialog.vue'
import GenderPicker from './ui/GenderPicker.vue'
import MicButton from './ui/MicButton.vue'
import { toneOf } from './ui/tones'

const showAvatarImage = useAvatarImage('rail')
const settings = useSettingsStore()

const props = defineProps({
  /** 生成页：点卡片=选中这条音色，并直接用它自己的主样例当参考音频；
   *  卡片右端的「选样例」才开详情挑别的一条。处理页同理，只是落到 `store.pick`，
   *  那颗按钮叫「添加」，把整张卡（全部样例）加入处理序列。 */
  pickable: { type: Boolean, default: true },
  /** 选中落到哪一份：`synth` = 生成页的参考音频，`pick` = 生产方工作区的处理目标。 */
  target: { type: String, default: 'synth' },
  /** 创作页：卡片右上角是「指定」而不是收藏。 */
  assignable: { type: Boolean, default: false },
  /** 生成页：列头摆一颗「录一段」（录下的就是这次的参考音频）。 */
  recordable: { type: Boolean, default: false },
})

/** 能走"音色设计"的引擎：能力由插件声明，界面不按引擎名判断。 */
const designEngines = computed(() =>
  (store.engines || []).filter((engine) => engine.capabilities?.voice_design)
)
const designOpen = ref(false)

/** 卡片列：滚动按整张卡翻（卡高取整、吸附在卡边），见 composables/cardPaging.js。 */
const listEl = ref(/** @type {any} */ (null))
useCardPaging(listEl)

const activeId = computed(() =>
  props.pickable ? (props.target === 'pick' ? store.pick.voiceId : store.synth.voiceId) : ''
)

/** 这一列只展示已经筛好的结果：搜索与性别 / 年龄 / 语言 / 情绪在顶部栏，查完再进来。 */
const visible = computed(() => store.voices)
/** 「全部播放」只收有主样例的：没有样例的进队列也只是空等。 */
const playable = computed(() => visible.value.filter(/** @param {any} voice */ (voice) => voice.main_audio_url))

function playAllList() {
  actions.playVoices(playable.value)
}

const filtering = computed(() =>
  Boolean(
    store.filters.q ||
      store.filters.gender ||
      store.filters.age ||
      store.filters.language ||
      store.filters.tag
  )
)

const playingId = computed(() => {
  const current = store.player
  if (!current.playing) return ''
  return visible.value.find(/** @param {any} voice */ (voice) => voice.main_audio_url === current.url)?.id || ''
})

/** 与音色库同一规矩：只挂滚动位置附近的一段（窗口化，不是分批挂全量）。
 *  计数（rail-count）与空态提示仍看全量 visible。 */
const { rendered, padTop, padBottom, horizontal } = useRailWindow(visible, listEl, {
  overscan: 6,
})

/**
 * @param {any} voice
 */
const initialOf = (voice) => (voice.shown_name || '?').slice(0, 1)

/**
 * 与卡片同一套：没封面时按名字散一个稳定底色（玻璃 / 素瓷为渐变）。
 *
 * @param {any} voice
 */
function coverStyle(voice) {
  if (voice.cover_url && showAvatarImage.value) return {}
  return { background: avatarFill(voice.shown_name, settings.appearance.look) }
}

/**
 * 点卡片 = 就用这条嗓子：选中它，并直接拿它的主样例当这次的参考音频
 * （顶上那张冻结卡会立刻出现）。要挑别的样例走卡片右端那颗「选样例」，
 * 弹窗只在那条路上出现——选音色是高频动作，不该每次都先开一张详情卡。
 *
 * 处理页（`target === 'pick'`）同一个手势，但落到 `store.pick`：那边没有冻结卡，
 * 选中的结果由工作区顶上那条音频显示。
 *
 * @param {any} voice
 */
function pick(voice) {
  if (!props.pickable) return
  if (props.target === 'pick') {
    actions.pickVoice(voice)
    return
  }
  // 已经是这条：不动。再点一下就把用户刚挑好的那条样例换回主样例，是纯损失。
  if (store.synth.voiceId === voice.id) return
  store.synth.voiceId = voice.id
  // 文件名留空 = 等样例取回后落主样例（判据只在 `loadSynthSamples` 一处）。
  store.synth.pinned = { voiceId: voice.id, file: '' }
}

/**
 * 卡片右端那颗按钮（生成页「选样例」、处理页「添加」）：开这条音色的详情，在里面挑具体某一段。
 *
 * @param {any} voice
 */
function chooseSample(voice) {
  void actions.openDetail(voice)
}

/**
 * 处理页的「添加」：把这张卡（整条音色）加入处理序列，不开详情。
 * 同一条音色再点一次也会把移出的样例补回来。
 *
 * @param {any} voice
 */
function addCard(voice) {
  actions.addPickVoice(voice)
}

/**
 * 点头像试听主样例：不改变当前选择，只把声音递到壳上的播放条。
 *
 * @param {any} voice
 */
function play(voice) {
  if (!voice.main_audio_url) return
  // 头像播放键排这一条音色自己的样例队列，从主样例开始；
  // 只供手动切上/下一段，放完当前这条不自动续播。
  actions.playVoiceCard(voice)
}

/**
 * 指定：交给当前生产方的工作区去问"给哪个角色"。
 *
 * @param {any} voice
 */
function assign(voice) {
  actions.requestAssign(voice)
}

/**
 * 小卡也走同一份音色菜单：管理动作与「打开音色卡」都在里面。
 *
 * @param {any} voice
 * @param {MouseEvent} event
 */
function openMenu(voice, event) {
  openObjectMenu(
    'voice.card',
    voiceMenuContext(voice, { open: () => actions.openDetail(voice) }),
    { event }
  )
}

/** 顶上冻结的是一段录音时不挂菜单：它不是库里的音色，没有可管理的东西。 */
/** @param {MouseEvent} event */
function onReferenceMenu(event) {
  if (!reference.value?.isVoice || !reference.value.voice) return
  openMenu(reference.value.voice, event)
}

/**
 * 录了一段：录音顶掉样例，顶上那张卡跟着变成它。
 *
 * @param {any} payload
 */
function onRecorded(payload) {
  actions.setReferenceRecording(payload)
}

/** 顶上那张冻结卡：录音优先（它一定是"刚做的那个动作"），否则是点过的那条音色。
 *  两者都缺就不出现——空着的一块底板只会让人以为少了个控件。
 *
 *  **字段一律取音色本身，不等样例加载**：`synth.samples` 是异步取回来的，若按
 *  "样例到没到"来换文案，点下去会先闪一帧别的字（旧版就是这样：先闪"设计·无参考音频"
 *  再变成参考，行高跟着 55→48 跳一下，整列像抖了一下）。名字、来源、条数、标签
 *  全在音色对象上，第一时间就能摆稳；只有"能不能放这段"要等样例。 */
const reference = computed(() => {
  const { synth } = store
  const rec = synth.recording
  if (rec) {
    return {
      isVoice: false,
      name: rec.name,
      source: '临时录音',
      count: rec.text?.trim() || '录音',
      tags: [],
      gender: '',
      hint: playingUrl(rec.audio_url) ? '暂停' : '试听这段录音',
      coverStyle: { background: 'var(--accent)' },
      coverUrl: '',
      initial: '',
      playing: playingUrl(rec.audio_url),
      url: rec.audio_url,
      voice: null,
      canChange: false,
    }
  }
  const pinned = synth.pinned
  if (!pinned) return null
  const voice =
    store.allVoices.find(/** @param {any} item */ (item) => item.id === pinned.voiceId) ||
    store.voices.find(/** @param {any} item */ (item) => item.id === pinned.voiceId) ||
    null
  if (!voice) return null
  const sample = synth.samples.find(/** @param {any} item */ (item) => item.file === pinned.file) || null
  const url = sample?.audio_url || ''
  // 这条音色有没有一条样例可指（设计音色没有）：它决定"能不能换样例"与"点了能不能试听"，
  // 不决定盖不盖章——章说的是"这次用它配音"。
  const design = actions.isDesignVoice(voice)
  const hasSamples = (voice.sample_count || 0) > 0
  return {
    isVoice: true,
    name: voice.shown_name,
    design,
    count: `${voice.sample_count || 0} 条样例`,
    gender: voice.gender,
    coverStyle: coverStyle(voice),
    coverUrl: voice.cover_url && showAvatarImage.value ? voice.cover_url : '',
    initial: initialOf(voice),
    voice,
    canChange: hasSamples,
    hint: hasSamples
      ? `${voice.shown_name} · ${pinned.file || '主样例'}（这次的参考音频；点一下试听，右端可换一条）`
      : `${voice.shown_name} · 描述随请求发送，没有参考片段`,
    playing: playingUrl(url),
    url,
  }
})

/**
 * 底栏正在放的是不是这一段（暂停也算当前这条）。
 *
 * @param {string} url
 */
function playingUrl(url) {
  return Boolean(url) && store.player.url === url && store.player.playing
}

/** 冻结卡：点卡片本体就放这一段（它就是"这次的参考"）；设计音色没有可放的，
 *  退到打开详情。换样例走右端那颗按钮。 */
function openReference() {
  const ref = reference.value
  if (!ref) return
  if (ref.url) {
    toggleReference()
    return
  }
  // 样例还在路上（刚点下去那几十毫秒）：先不动作，别顺手弹出一个详情
  if (ref.isVoice && ref.canChange) return
  if (ref.voice) void actions.openDetail(ref.voice)
}

function toggleReference() {
  const ref = reference.value
  if (!ref?.url) return
  if (store.player.url === ref.url) {
    actions.togglePlay()
    return
  }
  actions.play({ url: ref.url, label: ref.name, sub: ref.source || '参考', voice: ref.voice || undefined })
}
</script>
