<template>
  <div class="card" :class="{ 'is-hidden': voice.hidden }" @click="open">
    <button
      class="icon-btn fav-corner"
      :class="{ lit: voice.favorite }"
      :title="voice.favorite ? '取消收藏' : '收藏'"
      @click.stop="toggleFavorite"
    >
      <Star :size="14" :stroke-width="1.8" :fill="voice.favorite ? 'currentColor' : 'none'" />
    </button>

    <div class="card-head">
      <!-- 封面即播放键：整颗头像是一颗按钮，点它放主样例（与生成页左列同一套）。
           原来封面上还有一颗 hover 才现的 ▶、卡片底部还有一颗「试听」——
           三个入口做同一件事，扫列表时反倒不知点哪儿。现在只剩头像一个，
           播放徽标 hover / 键盘聚焦才浮现，播放中常亮。 -->
      <button
        type="button"
        class="cover-wrap"
        :class="{ bare: !voice.sample_count }"
        :title="voice.sample_count ? (isPlayingMain ? '暂停' : '播放主样例') : voice.shown_name"
        :aria-label="
          voice.sample_count
            ? isPlayingMain
              ? '暂停播放主样例'
              : `播放 ${voice.shown_name} 的主样例`
            : undefined
        "
        @click.stop="playMain"
      >
        <!-- lazy：几百张卡时屏幕外的封面不必跟着首屏一起拉。
             封面由后端给（可能是平台图），加载失败也不影响卡片其余信息。 -->
        <CoverImage
          v-if="voice.cover_url && showAvatarImage"
          class="cover"
          :src="voice.cover_url"
          width="58"
          height="58"
          alt=""
        />
        <!-- 圆标与本地音色同一套（首字母色块），平台音色也不例外：清单动辄几百条，
             靠"名字首字母 + 稳定底色"才扫得动。"这条从哪来"由引擎徽标与
             「生成样例」段说。 -->
        <span v-else class="cover" :style="{ background: coverColor }">{{ initial }}</span>
        <span v-if="voice.sample_count" class="cover-play" :class="{ playing: isPlayingMain }" aria-hidden="true">
          <Pause v-if="isPlayingMain" :size="14" :stroke-width="1.8" fill="currentColor" />
          <Play v-else :size="14" :stroke-width="1.8" fill="currentColor" />
        </span>
      </button>

      <div class="card-ident">
        <div class="card-name">
          <span class="card-name-text" :title="voice.shown_name">{{ voice.shown_name }}</span>
          <!-- 设计音色：名字后一枚「设计」，不写引擎——它不是哪一家的音色，
               凡声明了音色设计能力的引擎都能用它。 -->
          <span
            v-if="isDesign"
            class="card-tag"
            :class="`tone-${toneOf('设计')}`"
            title="按描述生成的音色，引擎在合成时选"
          >设计</span>
          <!-- 性别贴在名字后面一颗小圆标：分男女是这个库最常用的筛选维度，
               也不该藏在详情里。语言 / 年龄 / 标签留到详情改，卡片上不摆，
               否则每张卡都要两行 chip，名字反倒看不清。
               没标性别的卡片**不摆那颗「＋」**：它在卡片上太像"待办"，
               标性别去详情里做。 -->
          <GenderPicker
            v-if="voice.gender"
            :model-value="voice.gender"
            :options="vocab.genders"
            @change="onGenderChange(voice, $event)"
          />
        </div>
        <div class="card-sub">
          <!-- 只报样例数。来源不再摆在卡上：左栏已经按来源分了组（各引擎 / 设计音色），
               卡上再挂一枚引擎名是同一句话说两遍，几百张卡还都重复同一个词。
               真要确认"从哪来"，详情头那一枚还在。 -->
          <span class="card-count">{{ voice.sample_count }} 条样例</span>
        </div>
      </div>
    </div>

    <!-- 标签直接摆出来：扫列表时要看的是"这条声音什么味道"，不是它落在哪个目录。
         只读展示（点卡片开详情再改），配色与详情、筛选条共用同一套。
         设计音色的「设计」已挪到名字后面，这里不再重复。 -->
    <div v-if="tags.length" class="card-tags">
      <span
        v-for="(tag, index) in tags"
        :key="`${tag}-${index}`"
        class="card-tag"
        :class="`tone-${toneOf(tag)}`"
        :title="tag"
      >{{ tag }}</span>
    </div>

    <div v-if="voice.description" class="card-desc">{{ voice.description }}</div>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import { Pause, Play, Star } from 'lucide-vue-next'
import { store, actions, vocab } from '../store'
import { useAvatarImage } from '../composables/avatar'
import CoverImage from './ui/CoverImage.vue'
import GenderPicker from './ui/GenderPicker.vue'
import { toneOf } from './ui/tones'

const props = defineProps({ voice: { type: Object, required: true } })

const showAvatarImage = useAvatarImage('cards')
const initial = computed(() => (props.voice.shown_name || '?').slice(0, 1))

/** 设计音色：本体的全部就是那段描述。 */
const isDesign = computed(() => actions.isDesignVoice(props.voice))
/** 下方标签：设计音色的「设计」已经贴到名字后，这里去掉，免得多占一行。 */
const tags = computed(() =>
  (props.voice.tags || []).filter(
    /** @param {string} tag */
    (tag) => !isDesign.value || tag !== '设计'
  )
)

/**
 * 卡上改性别：模板箭头函数在类型检查里会被当成属性访问，改成方法最省事。
 * @param {any} voice
 * @param {string} value
 */
function onGenderChange(voice, value) {
  void actions.setPrefs(voice, { gender: value })
}
/** 卡片上的 ▶ 放主样例，地址由后端给（`main_audio_url`）——不在这里自己拼：
 *  文件名里可能有 `#`，前端拼第二份迟早出现"卡片能播、详情播不了"。 */
const mainUrl = computed(() => props.voice.main_audio_url || '')
/** 主样例正在放才写「暂停」（再点由 `play` 转成暂停/继续）。 */
const isPlayingMain = computed(
  () => store.player.playing && store.player.url === mainUrl.value
)

// 没封面时按名字散一个稳定的底色（同一音色永远同色）。
const coverColor = computed(() => {
  let hash = 0
  for (const ch of props.voice.shown_name) hash = (hash * 31 + ch.codePointAt(0)) % 360
  return `hsl(${hash}, 55%, 62%)`
})

async function open() {
  await actions.openDetail(props.voice)
}

function playMain() {
  // 有多条样例时顺带把整份样例排成队列：播放条上因此能切「上一段 / 下一段」。
  actions.playVoice(props.voice)
}

async function toggleFavorite() {
  await actions.setPrefs(props.voice, { favorite: !props.voice.favorite })
}
</script>
