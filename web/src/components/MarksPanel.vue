<template>
  <!-- 生成页右栏的语气词面板：分组与写法跟正文下面那一排**同一份实现**
       （分组由后端算好，这里不认识任何标签名字）。摆在正文右边，
       写一段、右边点一下，手不用来回横跳。 -->
  <div class="mark-bar marks-panel">
    <div v-for="group in items" :key="group.key || 'common'" class="mark-group">
      <span class="mark-group-label">{{ group.label }}</span>
      <div class="mark-group-items">
        <button
          v-for="mark in group.marks"
          :key="group.key + ':' + mark.name"
          type="button"
          class="mark-btn"
          :class="{ off: mark.missing.length, own: Boolean(group.key) }"
          :title="markTitle(mark, group)"
          @click="emit('insert', mark)"
        >
          {{ mark.label }}
        </button>
        <MarkCustom v-if="group.any" @insert="emit('insert', $event)" />
      </div>
    </div>
    <p v-if="!items.length" class="hint">没有可用的语气词标签</p>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import MarkCustom from './MarkCustom.vue'

/** 语气词标签只负责"递出去"，插在哪儿由正文那格说了算（TextEditor 暴露 insert）。 */
const props = defineProps({ groups: { type: Array, default: () => [] } })
const emit = defineEmits(['insert'])

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const items = computed(() => /** @type {any[]} */ (props.groups))

/**
 * 悬停时说什么：先写清它是什么，再说这次有谁会收不到它。
 *
 * @param {any} mark
 * @param {any} group
 */
function markTitle(mark, group) {
  const what = mark.arg ? `[[${mark.name}:0.4]]` : `[[${mark.name}]]`
  const lines = [what]
  if (group.note) lines.push(group.note)
  if (mark.missing.length) lines.push(`${mark.missing.join('、')} 不认它，会从正文里删掉`)
  return lines.join('\n')
}
</script>
