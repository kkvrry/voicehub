<template>
  <!-- 引导指令：预设说法**默认全部摆出来**，点一条把全文填进下面的输入框；
       再点同一条取消（清空）。那个框也能直接改。 -->
  <div class="instr-field">
    <div v-if="presetList.length" class="tag-pick">
      <button
        v-for="item in presetList"
        :key="item.label"
        type="button"
        class="mark-chip"
        :class="{ on: modelValue === item.text }"
        :title="item.text"
        @click="pick(item)"
      >
        {{ item.label }}
      </button>
    </div>
    <textarea
      class="field-textarea instr-area"
      :value="modelValue"
      placeholder="描述语气…"
      aria-label="引导指令"
      @input="onInput"
    ></textarea>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  /** ``[{label, text}]``：各台引擎声明过的现成说法（按短名去重）。 */
  presets: { type: Array, default: () => [] },
})
const emit = defineEmits(['update'])

// Array 型 prop 推出来是 unknown[]；按 ``[{ label, text }]`` 收窄一次。
const presetList = computed(() => /** @type {any[]} */ (props.presets))

/** @param {Event} event */
function onInput(event) {
  emit('update', /** @type {HTMLTextAreaElement} */ (event.target).value)
}

/**
 * 点同一颗 = 取消这一句（清空输入框）。
 *
 * @param {any} item
 */
function pick(item) {
  emit('update', props.modelValue === item.text ? '' : item.text)
}
</script>
