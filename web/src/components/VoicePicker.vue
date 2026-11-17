<template>
  <div ref="box" class="vpick">
    <div class="combo-box">
      <!-- 平时显示选中的那一条（点开才变成搜索词）：空框看着像"还能填别的"。 -->
      <input
        class="field-input vpick-input"
        :value="open ? query : shown"
        :placeholder="placeholder"
        aria-label="音色"
        @input="onInput"
        @focus="onFocus"
        @keydown.esc="close"
        @keydown.enter.prevent="takeActive"
        @keydown.down.prevent="move(1)"
        @keydown.up.prevent="move(-1)"
      />
      <button
        type="button"
        class="combo-caret"
        :title="`音色（${voices.length} 条）`"
        aria-label="音色"
        @mousedown.prevent
        @click="open ? close() : onFocus()"
      >
        ▾
      </button>
    </div>

    <!-- `up`：贴着滚动容器底边往下开会被裁掉，放不下就朝上开。 -->
    <ul v-if="open" class="aud-menu vpick-menu" :class="{ up: dropUp }">
      <li v-for="(voice, index) in rows" :key="voice.id">
        <button
          type="button"
          class="vpick-row"
          :class="{ on: voice.id === model, hot: index === active }"
          @mousedown.prevent="take(voice.id)"
        >
          <!-- 收藏排在最前；星标是"被钉住了"的记号，不是新的排序维度。 -->
          <span class="vpick-star">{{ voice.favorite ? '★' : '' }}</span>
          <span class="vpick-name">{{ voice.shown_name }}</span>
          <span v-if="voice.category" class="badge">{{ voice.category }}</span>
          <!-- 来源摆在最右边：同名音色分属两台引擎时靠它分清谁是谁。 -->
          <span class="vpick-src">{{ voice.source }}</span>
        </button>
      </li>

      <!-- 没匹配上但框里有字：让那句话能用——平台引擎的音色 id 就是一段字符串，
           库里不一定扫得进来。 -->
      <li v-if="raw">
        <button type="button" class="vpick-row raw" @mousedown.prevent="take(raw)">
          直接用「{{ raw }}」当音色 id
        </button>
      </li>
      <li v-if="!rows.length && !raw" class="vpick-note">没有匹配的音色</li>

      <li v-if="hiddenCount" class="vpick-foot">
        <button type="button" @mousedown.prevent="showHidden = !showHidden">
          {{ showHidden ? '收起已隐藏的' : `另有 ${hiddenCount} 条已隐藏` }}
        </button>
      </li>
    </ul>
  </div>
</template>

<script setup>
// @ts-check
/** 音色那一格：可搜索的挑选器（原生下拉在几百条里没法用）。
 *  打字就筛（多词全命中）；收藏钉在最前；隐藏的不摆但能翻出来；键盘可达（↑↓/回车/Esc）。
 *  框里的字可以直接当音色 id 用（末行）：库里没有、只有 id 的情况真实存在。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps({
  voices: { type: Array, default: () => [] },
  placeholder: { type: String, default: '搜索音色' },
})
const model = defineModel({ type: String, default: '' })

/** 一次最多摆几条：几百条全画出来，滚动条本身就成了新的"找不到"。 */
const MAX_ROWS = 60

const open = ref(false)
const query = ref('')
const active = ref(-1)
const showHidden = ref(false)
/** 下拉朝上开还是朝下开（估计的高度：菜单 232px + 底下那两行）。 */
const dropUp = ref(false)
const box = ref(/** @type {any} */ (null))

function needsToFlip() {
  const rect = box.value?.getBoundingClientRect()
  if (!rect) return false
  return rect.bottom + 296 > window.innerHeight
}

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const voiceList = computed(() => /** @type {any[]} */ (props.voices))
const selected = computed(() => voiceList.value.find((v) => v.id === model.value) || null)

/** 没点开时框里显示的那一句：库里认得出就写它的显示名，认不出（手打的 id）就原样写。 */
const shown = computed(() => selected.value?.shown_name || model.value || '')

/**
 * 搜索用的那一串：只在这一处拼，筛选与排序都拿它比。
 *
 * @param {any} voice
 */
function haystack(voice) {
  return [voice.shown_name, voice.name, voice.category, voice.source, ...(voice.tags || [])]
    .join(' ')
    .toLowerCase()
}

const matched = computed(() => {
  const words = query.value.trim().toLowerCase().split(/\s+/).filter(Boolean)
  return voiceList.value.filter(/** @param {any} voice */ (voice) => {
    if (voice.hidden && !showHidden.value) return false
    return words.every(/** @param {any} word */ (word) => haystack(voice).includes(word))
  })
})

/** 收藏先摆（稳定排序：收藏内部保持扫描顺序）。 */
const rows = computed(() => {
  const favorites = matched.value.filter((v) => v.favorite)
  const rest = matched.value.filter((v) => !v.favorite)
  return [...favorites, ...rest].slice(0, MAX_ROWS)
})

/** 库里被隐藏了几条。藏起来的东西得有个数，不然用户只觉得"音色少了"。 */
const hiddenCount = computed(() => voiceList.value.filter((v) => v.hidden).length)

/** 框里那句话能不能直接当 id 用：精确对上某条音色时不再提这一行。 */
const raw = computed(() => {
  const text = query.value.trim()
  if (!text) return ''
  const lower = text.toLowerCase()
  const exact = voiceList.value.some(
    (v) => v.id === text || (v.shown_name || '').toLowerCase() === lower || (v.name || '').toLowerCase() === lower
  )
  return exact ? '' : text
})

/**
 * @param {any} event
 */
function onInput(event) {
  query.value = event.target.value
  open.value = true
  active.value = -1
}

function onFocus() {
  open.value = true
  query.value = ''
  active.value = -1
  dropUp.value = needsToFlip()
}

function close() {
  open.value = false
  query.value = ''
  active.value = -1
}

/**
 * @param {number} step
 */
function move(step) {
  const count = rows.value.length
  if (!count) return
  active.value = (active.value + step + count) % count
}

function takeActive() {
  if (active.value >= 0 && rows.value[active.value]) take(rows.value[active.value].id)
  else if (raw.value) take(raw.value)
}

/**
 * @param {any} id
 */
function take(id) {
  model.value = id
  close()
}

/**
 * 点别处收起。不挂 blur：它比条目上的 mousedown 先到，"点一条"会变成"先关菜单"。
 *
 * @param {any} event
 */
function onDocClick(event) {
  if (open.value && box.value && !box.value.contains(event.target)) close()
}
onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>
