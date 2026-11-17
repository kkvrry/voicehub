<template>
  <div ref="anchorEl" class="vtag-field" @click.stop>
    <button
      v-for="tag in tags"
      :key="tag"
      type="button"
      class="vchip"
      :class="[`tone-${toneOf(tag)}`]"
      :title="`点击编辑「${tag}」`"
      @click.stop="toggle"
    >
      {{ tag }}
    </button>
    <button
      type="button"
      class="vchip is-blank"
      :class="{ 'is-icon': modelValue.length && !addLabel }"
      :title="modelValue.length ? `添加/删除${addLabel || label}` : `添加${addLabel || label}`"
      @click.stop="toggle"
    >
      <Plus :size="12" :stroke-width="2" />
      <span v-if="addLabel || !modelValue.length">{{ addLabel || label }}</span>
    </button>

    <!-- 宽度按"一屏能摆几行"定：词表一长（情绪那 27 个），窄框会把它们挤成一列小字。 -->
    <Popover :open="open" :anchor="anchorEl" :width="320" :label="label" @close="close">
      <div class="vpop-head">{{ label }}<span v-if="modelValue.length" class="vpop-hint">点一下移除</span></div>
      <div v-if="modelValue.length" class="vpop-grid">
        <button
          v-for="tag in tags"
          :key="tag"
          type="button"
          class="vchip is-active"
          :class="[`tone-${toneOf(tag)}`]"
          @click="remove(tag)"
        >
          {{ tag
          }}<span class="vchip-x"><X :size="11" :stroke-width="2" /></span>
        </button>
      </div>
      <!-- 固定候选（语调 / 音色）：点一下加、再点一下去掉，与自填词落在同一串标签里。 -->
      <template v-for="group in groupList" :key="group.label">
        <div class="vpop-sub">{{ group.label }}</div>
        <div class="vpop-grid">
          <button
            v-for="pair in group.options"
            :key="pair[0]"
            type="button"
            class="vchip"
            :class="[`tone-${toneOf(pair[0])}`, { 'is-active': has(pair[0]) }]"
            @click="toggleTag(pair[0])"
          >
            {{ pair[1] }}
          </button>
        </div>
      </template>
      <form class="vpop-custom" @submit.prevent="add">
        <input
          ref="inputEl"
          v-model="draft"
          type="text"
          maxlength="20"
          :placeholder="`新${label}，回车添加`"
        />
      </form>
    </Popover>
  </div>
</template>

<script setup>
// @ts-check
/** 自由标签：一串自己打的词（「活泼」「俏皮」「御姐」…）。
 *
 *  颜色**不是硬编码的灰**：同一个词在情绪胶囊上是什么色，在这里就该是什么色
 *  —— 一个词两处两个长相，比没有颜色更让人以为是两回事。默认走 `tones.js` 那一个
 *  统一解析（语义词表 → 按词散列），所以自己打的词也有颜色，而且同一个词永远是同一个色。
 *  调用方要另一套判断，就把 `tone-of` 传进来。
 */
import { computed, nextTick, ref } from 'vue'
import { Plus, X } from 'lucide-vue-next'
import Popover from './Popover.vue'
import { toneOf as defaultToneOf } from './tones'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  label: { type: String, default: '标签' },
  /** 「＋」那颗上常显的字。默认只在空的时候写名字（一行里只有它一簇时够用）；
   *  同一行摆着两簇（样例上的情绪与标签）就得一直写着，否则两颗「＋」分不清谁是谁。 */
  addLabel: { type: String, default: '' },
  toneOf: { type: Function, default: defaultToneOf },
  /** 固定候选：`[{ label, options: [[值, 说法], …] }]`；选中的仍在 `modelValue` 里。 */
  groups: { type: Array, default: () => [] },
})
const emit = defineEmits(['change'])

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const tags = computed(() => /** @type {string[]} */ (props.modelValue))
const groupList = computed(() => /** @type {any[]} */ (props.groups))

const open = ref(false)
const anchorEl = ref(/** @type {any} */ (null))
const inputEl = ref(/** @type {any} */ (null))
const draft = ref('')

async function toggle() {
  open.value = !open.value
  if (!open.value) return
  draft.value = ''
  await nextTick()
  inputEl.value?.focus()
}

function close() {
  open.value = false
}

function add() {
  const value = draft.value.trim()
  if (!value) return
  draft.value = ''
  if (!props.modelValue.includes(value)) emit('change', [...props.modelValue, value])
}

/**
 * @param {any} tag
 */
function remove(tag) {
  emit(
    'change',
    props.modelValue.filter(/** @param {any} item */ (item) => item !== tag)
  )
}

/**
 * @param {any} tag
 */
function has(tag) {
  return props.modelValue.includes(tag)
}

/**
 * 候选点一下：有就去掉，没有就加到末尾（与手打的一条路，只是不用打字）。
 *
 * @param {any} tag
 */
function toggleTag(tag) {
  emit('change', has(tag) ? props.modelValue.filter(/** @param {any} item */ (item) => item !== tag) : [...props.modelValue, tag])
}
</script>
