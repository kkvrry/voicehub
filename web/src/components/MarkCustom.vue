<template>
  <!-- 认任意标签的引擎（MiMo 这类"标签随你写"）：组尾一格输入，回车插进正文。
       分组与"这家认不认任意标签"都由后端算好（`group.any`），这里不认识任何引擎。 -->
  <input
    v-model="text"
    class="mark-custom"
    type="text"
    maxlength="16"
    placeholder="自定义"
    title="输入标签名，回车插入"
    aria-label="自定义语气词标签"
    @keydown.enter.prevent="commit"
  />
</template>

<script setup>
// @ts-check
import { ref } from 'vue'

const emit = defineEmits(['insert'])
const text = ref('')

/** 冒号是参数分隔符、方括号是标签边界：名字里带上它们就解析不出来了，写作时剔掉。 */
function commit() {
  const name = text.value.replace(/[[\]:]/g, '').trim()
  if (!name) return
  emit('insert', { name, label: name, arg: '' })
  text.value = ''
}
</script>
