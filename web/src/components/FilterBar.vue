<template>
  <header class="lib-head">
    <h1>
      <!-- 标题就写选中了谁（「MiniMax」「原神」「全部音色」），与本地分类同一套；
           不写「只看「xxx」」那一圈：范围由左栏高亮说，列表里也只有这一批。
           有标签时标题兼作开关，点它展开／收起标签条。 -->
      <button
        v-if="store.tagCloud.length"
        type="button"
        class="lib-title-toggle"
        :aria-expanded="!collapsed"
        :aria-label="
          collapsed
            ? store.filters.tag
              ? `${title}，展开标签，当前筛选 ${store.filters.tag}`
              : `${title}，展开标签`
            : `${title}，收起标签`
        "
        :title="collapsed ? '展开标签' : '收起标签'"
        @click="toggleTagbar"
      >
        {{ title }}
        <ChevronDown v-if="collapsed" :size="14" :stroke-width="1.8" aria-hidden="true" />
        <ChevronUp v-else :size="14" :stroke-width="1.8" aria-hidden="true" />
      </button>
      <template v-else>{{ title }}</template>
      <!-- 播的必须是眼前这个列表的顺序（分帧挂载只影响画，不影响顺序）。 -->
      <button
        type="button"
        class="lib-play"
        :disabled="!playable.length"
        :title="
          playable.length
            ? `按列表顺序播放这 ${playable.length} 个音色的主样例`
            : '当前列表里没有可播放的样例'
        "
        aria-label="按顺序播放当前列表"
        @click="playAllList"
      >
        <Play :size="12" :stroke-width="1.8" fill="currentColor" />
      </button>
      <span class="result-count">共 {{ store.voices.length }} 个音色</span>
    </h1>
    <div class="lib-tools">
      <!-- 常用筛选就地摆一排；顶栏的筛选弹层是同一份状态，这里多一条直接入口。 -->
      <select
        v-model="store.filters.gender"
        class="sh-field lib-select"
        aria-label="性别"
        @change="load"
      >
        <option value="">性别不限</option>
        <option v-for="pair in vocab.genders" :key="pair[0]" :value="pair[0]">{{ pair[1] }}</option>
      </select>
      <select v-model="store.filters.age" class="sh-field lib-select" aria-label="年龄" @change="load">
        <option value="">年龄不限</option>
        <option v-for="pair in vocab.ages" :key="pair[0]" :value="pair[0]">{{ pair[1] }}</option>
      </select>
      <select
        v-model="store.filters.language"
        class="sh-field lib-select"
        aria-label="语言"
        @change="load"
      >
        <option value="">语言不限</option>
        <option v-for="pair in vocab.languages" :key="pair[0]" :value="pair[0]">
          {{ pair[1] }}
        </option>
      </select>
      <!-- 清除筛选紧挨着它要清的那一排：先清筛选，再决定看不看隐藏的。 -->
      <ActionButton v-if="hasFilters" size="sm" @click="clearFilters">清除筛选</ActionButton>
      <!-- 开关用在「一开就多出一批」的地方；勾选框是另一种时代的控件 -->
      <label class="lib-switch">
        <ToggleSwitch v-model="store.showHidden" label="显示已隐藏" />
        <span>显示已隐藏</span>
      </label>
    </div>
  </header>
  <div v-if="store.tagCloud.length && !collapsed" class="lib-tagbar">
    <!-- 「全部」占着原来那枚写着「标签」的文字：选了标签之后，这里依旧是同一行字，
         点它就从任何一枚标签回到整份列表——不必再去找刚点过的那一枚。 -->
    <button
      type="button"
      class="tag-chip is-all"
      :class="{ active: !store.filters.tag }"
      :aria-pressed="!store.filters.tag"
      title="不限标签"
      @click="showAll"
    >
      全部
    </button>
    <button
      v-for="entry in store.tagCloud"
      :key="entry.tag"
      class="tag-chip"
      :class="{ active: store.filters.tag === entry.tag }"
      :aria-pressed="store.filters.tag === entry.tag"
      :title="store.filters.tag === entry.tag ? '再点一下取消' : `筛出 ${entry.count} 个`"
      @click="pickTag(entry.tag)"
    >
      {{ entry.tag }}<span class="tag-count">{{ entry.count }}</span>
    </button>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import { ChevronDown, ChevronUp, Play } from 'lucide-vue-next'
import { actions, store, visibleVoices, vocab } from '../store'
import { useSettingsStore } from '../store/settings'
import ActionButton from './ui/ActionButton.vue'
import ToggleSwitch from './ui/ToggleSwitch.vue'

const settings = useSettingsStore()

/** 标签条收起没有：偏好存在外观设置里，换页、刷新后保持。 */
const collapsed = computed(() => settings.appearance.tagBarCollapsed)

function toggleTagbar() {
  settings.appearance.tagBarCollapsed = !collapsed.value
}

const title = computed(() => {
  const selection = store.selection
  // 选中引擎时标题写引擎名字（中文名只有后端那一份），不加「只看」那一圈。
  if (selection.kind === 'platform') {
    const row = store.tree.engines.find((item) => item.source === selection.name)
    return row ? row.label : selection.name
  }
  if (selection.kind === 'local') return selection.category || '本地音色'
  if (selection.kind === 'design') return '设计音色'
  return '全部音色'
})

const hasFilters = computed(() => {
  const f = store.filters
  return Boolean(f.q || f.gender || f.age || f.language || f.tag)
})

/** 能放进队列的那些：没有主样例的音色进了队列也只是空等。 */
const playable = computed(() => visibleVoices().filter((voice) => voice.main_audio_url))

function playAllList() {
  actions.playVoices(playable.value)
}

async function load() {
  await actions.loadVoices()
}

/**
 * 点一次筛上、再点一次取消。
 * @param {string} tag
 */
async function pickTag(tag) {
  store.filters.tag = store.filters.tag === tag ? '' : tag
  await actions.loadVoices()
}

/** 「全部」= 不筛标签。已经是全部时什么都不做（免得白跑一次请求）。 */
async function showAll() {
  if (!store.filters.tag) return
  store.filters.tag = ''
  await actions.loadVoices()
}

async function clearFilters() {
  store.filters.q = ''
  store.filters.gender = ''
  store.filters.age = ''
  store.filters.language = ''
  store.filters.tag = ''
  await actions.loadVoices()
}
</script>
