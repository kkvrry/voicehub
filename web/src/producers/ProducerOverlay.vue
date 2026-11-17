<template>
  <component :is="overlay" v-if="overlay" />
</template>

<script setup>
// @ts-check
// 全局浮层插槽：与工作区同一套目录约定，组件名 `Overlay`（可选）。
// 它不属于某一页——播放条上的「指定音色」在任何页面都能把弹窗叫出来。
import { computed, shallowRef, watch } from 'vue'
import { store } from '../store'
import { producerModule } from './registry'

/** 没打开过创作页时用清单里的第一家：浮层不该因为"还没进那一页"而缺席。 */
const key = computed(() => store.producer.key || store.producers[0]?.key || '')
const overlay = shallowRef(null)

let token = 0

watch(
  key,
  async (value) => {
    const mine = ++token
    overlay.value = null
    if (!value) return
    const module = await producerModule(value)
    if (mine !== token) return
    overlay.value = module?.Overlay || null
  },
  { immediate: true }
)
</script>
