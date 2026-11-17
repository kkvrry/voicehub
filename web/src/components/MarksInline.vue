<template>
  <!-- 正文下面那排语气词：**一台引擎一行**（通用在最上），品牌名加粗并带引擎图标，
       标签是统一的小圆角 chip。点一下插进正文光标处。
       「语气词」自己占一行，分组名不再留固定列，标签从面板左侧排起。 -->
  <div class="chip-panel marks-inline">
    <div class="marks-head">
      <span class="marks-cap">语气词</span>
      <!-- 完全不支持标签的勾选引擎：跟着标题摆成禁用标签，不再另起一句"不生效"。 -->
      <UnsupportedEngines
        :names="silent"
        feature="语气词"
        effect="发给它的标签会被删掉"
      />
    </div>
    <div class="marks-flow">
      <div
        v-for="group in items"
        :key="group.key || 'common'"
        class="marks-group"
        :class="{ 'is-common': !group.key }"
      >
        <span class="marks-brand">
          <img v-if="iconOf(group.key)" class="marks-brand-icon" :src="iconOf(group.key)" alt="" />
          <Layers
            v-else-if="!group.key"
            class="marks-brand-icon marks-brand-common"
            :size="13"
            :stroke-width="1.8"
            aria-hidden="true"
          />
          {{ group.label }}
        </span>
        <div class="marks-items">
          <button
            v-for="mark in group.marks"
            :key="group.key + ':' + mark.name"
            type="button"
            class="mark-chip"
            :title="markTitle(mark, group)"
            @click="emit('insert', mark)"
          >
            {{ mark.label }}
          </button>
          <MarkCustom v-if="group.any" @insert="emit('insert', $event)" />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import { Layers } from 'lucide-vue-next'
import { actions } from '../store'
import MarkCustom from './MarkCustom.vue'
import UnsupportedEngines from './UnsupportedEngines.vue'

/** 只负责"递出去"：插在哪儿由正文那格说了算（TextEditor 暴露 insert）。 */
const props = defineProps({
  groups: { type: Array, default: () => [] },
  /** 勾选里完全不支持语气词的引擎名：面板上说一句，不逐颗标灰。 */
  silent: { type: Array, default: () => [] },
})
const emit = defineEmits(['insert'])

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const items = computed(() => /** @type {any[]} */ (props.groups))

/**
 * 组名旁边那枚小图标：品牌认得出来，比只写名字更醒目。
 *
 * @param {string} key
 */
function iconOf(key) {
  return key ? actions.engineOf(key)?.icon_url || '' : ''
}

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
