<template>
  <!-- 文案库：从现成句子里挑一句填进正文，或者随机来一句。
       库本身是服务端的一份配置（设置页可改），这里只负责"挑"与"填"。 -->
  <div class="text-lib">
    <button
      ref="triggerEl"
      type="button"
      class="editor-tool"
      :class="{ on: open }"
      title="从文案库填入"
      @click="open = !open"
    >
      <BookOpen :size="14" />
      <span>文案</span>
    </button>
    <button type="button" class="editor-tool" title="随机一句" aria-label="随机一句" @click="random">
      <Dices :size="14" />
    </button>

    <Popover :open="open" :anchor="triggerEl" :width="360" label="文案库" @close="open = false">
      <div class="tlib-head">
        文案库
        <span class="spacer"></span>
        <button type="button" class="tlib-random" @click="random">随机一句</button>
      </div>
      <div class="tlib-body">
        <div v-for="group in groups" :key="group.name" class="tlib-group">
          <div class="tlib-name">{{ group.name }}</div>
          <button
            v-for="line in group.lines"
            :key="line"
            type="button"
            class="tlib-line"
            @click="pick(line)"
          >
            {{ line }}
          </button>
        </div>
        <p v-if="!groups.length" class="hint">文案库为空</p>
      </div>
    </Popover>
  </div>
</template>

<script setup>
// @ts-check
import { computed, onMounted, ref } from 'vue'
import { BookOpen, Dices } from 'lucide-vue-next'
import { actions, say, store } from '../store'
import Popover from './ui/Popover.vue'

const emit = defineEmits(['pick'])
const open = ref(false)
const triggerEl = ref(/** @type {any} */ (null))

/** 只把有句子的组摆出来。 */
const groups = computed(() => (store.texts || []).filter((group) => (group.lines || []).length))

// 生成页第一次用到时才取（列表页不多拉一次）。
onMounted(() => {
  void actions.ensureTexts()
})

/** @param {string} text */
function pick(text) {
  open.value = false
  emit('pick', text)
}

function random() {
  const lines = groups.value.flatMap((group) => group.lines || [])
  if (!lines.length) {
    say('文案库为空')
    return
  }
  pick(lines[Math.floor(Math.random() * lines.length)])
}
</script>
