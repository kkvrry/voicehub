<template>
  <div class="vgender" @click.stop>
    <!-- 只读态：同样的圆标，只是一段标记，不是能点开改值的控件。 -->
    <span
      v-if="readonly"
      class="vgender-btn is-static"
      :class="`tone-${tone}`"
      :title="text"
      :aria-label="`性别：${text}`"
    >
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <template v-if="modelValue === 'female'">
          <circle cx="8" cy="5.6" r="3.9" />
          <path d="M8 9.5V15M5.6 12.6h4.8" />
        </template>
        <template v-else-if="modelValue === 'male'">
          <circle cx="6.6" cy="9.4" r="3.9" />
          <path d="M9.6 6.4L14 2M14 2h-3.5M14 2v3.5" />
        </template>
        <template v-else-if="modelValue === 'other'">
          <circle cx="8" cy="8" r="4.9" />
          <circle cx="8" cy="8" r="1.5" class="fill" />
        </template>
        <template v-else>
          <path d="M8 4.4v7.2M4.4 8h7.2" />
        </template>
      </svg>
    </span>

    <button
      v-else
      ref="triggerEl"
      type="button"
      class="vgender-btn"
      :class="[`tone-${tone}`, { 'is-blank': !modelValue }]"
      :title="modelValue ? `${text} · 点击更换` : '未标性别 · 点击设置'"
      :aria-label="modelValue ? `性别：${text}` : '未标性别'"
      :aria-expanded="open"
      @click.stop="toggle"
    >
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <template v-if="modelValue === 'female'">
          <circle cx="8" cy="5.6" r="3.9" />
          <path d="M8 9.5V15M5.6 12.6h4.8" />
        </template>
        <template v-else-if="modelValue === 'male'">
          <circle cx="6.6" cy="9.4" r="3.9" />
          <path d="M9.6 6.4L14 2M14 2h-3.5M14 2v3.5" />
        </template>
        <template v-else-if="modelValue === 'other'">
          <circle cx="8" cy="8" r="4.9" />
          <circle cx="8" cy="8" r="1.5" class="fill" />
        </template>
        <template v-else>
          <!-- 未标只留一个十字：12px 里再画一圈虚线，缩到屏幕上就是一团糊的圆点。 -->
          <path d="M8 4.4v7.2M4.4 8h7.2" />
        </template>
      </svg>
    </button>

    <!-- v-if 懒挂载：几百张卡各带一个性别控件，浮层骨架（Teleport/监听）没必要
         常驻——点开的那一颗才挂。Popover 挂上来就知道 open 的用法见其 onMounted。 -->
    <Popover v-if="open" :open="open" :anchor="triggerEl" :width="184" label="性别" @close="close">
      <div class="vpop-head">性别</div>
      <div class="vgender-list">
        <button
          v-for="pair in optionPairs"
          :key="pair[0]"
          type="button"
          class="vgender-opt"
          :class="[`tone-${genderTone(pair[0])}`, { 'is-active': pair[0] === modelValue }]"
          @click="pick(pair[0])"
        >
          <span class="vgender-icon">
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <template v-if="pair[0] === 'female'">
                <circle cx="8" cy="5.6" r="3.9" />
                <path d="M8 9.5V15M5.6 12.6h4.8" />
              </template>
              <template v-else-if="pair[0] === 'male'">
                <circle cx="6.6" cy="9.4" r="3.9" />
                <path d="M9.6 6.4L14 2M14 2h-3.5M14 2v3.5" />
              </template>
              <template v-else>
                <circle cx="8" cy="8" r="4.9" />
                <circle cx="8" cy="8" r="1.5" class="fill" />
              </template>
            </svg>
          </span>
          {{ pair[1] }}
        </button>
      </div>
      <button v-if="modelValue" type="button" class="vpop-clear" @click="pick('')">
        清空性别
      </button>
    </Popover>
  </div>
</template>

<script setup>
// @ts-check
import { computed, ref } from 'vue'
import Popover from './Popover.vue'
import { genderTone } from './tones'

const props = defineProps({
  modelValue: { type: String, default: '' },
  options: { type: Array, default: () => [] },
  /** 只显示、不提供更改入口（生成页与创作页的音色栏用）。 */
  readonly: { type: Boolean, default: false },
})
const emit = defineEmits(['change'])

const open = ref(false)
const triggerEl = ref(/** @type {any} */ (null))
// Array 型 prop 推出来是 unknown[]；按 `[[值, 说法], …]` 收窄一次。
const optionPairs = computed(() => /** @type {any[][]} */ (props.options))
const tone = computed(() => (props.modelValue ? genderTone(props.modelValue) : 'gray'))
const text = computed(() => {
  const found = optionPairs.value.find((pair) => pair[0] === props.modelValue)
  return found ? found[1] : props.modelValue
})

function toggle() {
  open.value = !open.value
}

function close() {
  open.value = false
}

/**
 * @param {any} value
 */
function pick(value) {
  close()
  if (value !== props.modelValue) emit('change', value)
}
</script>

<style scoped>
/* 只读态沿用同一颗圆标，但不给指针、不响应悬停：它不是可点的控件。 */
.vgender-btn.is-static {
  cursor: default;
}
.vgender-btn.is-static:hover {
  border-color: transparent;
}
</style>
