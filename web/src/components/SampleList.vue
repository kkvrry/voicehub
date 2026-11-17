<template>
  <div class="sample-list">
    <SampleRow
      v-for="sample in voice.samples"
      :key="sample.file"
      :voice="voice"
      :sample="sample"
      :multiple="voice.samples.length > 1"
    />
    <!-- 空态按"它有没有目录"分：本地音色是自己的目录里没有音频；平台音色压根
         没有目录，它在磁盘上一段音频都没有，要听只能现生成（右边那颗按钮）。 -->
    <div v-if="!voice.samples.length" class="hint">
      {{ voice.is_local ? '这个音色的目录里没有音频文件。' : '还没有生成过样例。' }}
    </div>
  </div>
</template>

<script setup>
// @ts-check
import SampleRow from './SampleRow.vue'

defineProps({ voice: { type: Object, required: true } })
</script>
