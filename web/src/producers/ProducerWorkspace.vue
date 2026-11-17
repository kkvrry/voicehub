<template>
  <component :is="workspace" v-if="workspace" />
  <section v-else class="app-panel">
    <header class="panel-head">
      <div class="panel-head-main">
        <div class="engine-title">{{ title }}</div>
        <div v-if="reason === 'missing'" class="muted">
          插件界面文件不在；重装该插件后重新载入。
        </div>
      </div>
    </header>
  </section>
</template>

<script setup>
// @ts-check
// 宿主的工作区插槽：按 `store.producer.key` 找目录，不 import 任何一家插件。
// 插件目录被删掉时这里显示一句说明，构建与其余页面不受影响。
import { computed, ref, shallowRef, watch } from 'vue'
import { store } from '../store'
import { producerModule } from './registry'

const workspace = shallowRef(null)
const reason = ref('')

const title = computed(() =>
  reason.value === 'missing'
    ? '工作区未安装'
    : reason.value === 'none'
      ? '未选择生产方'
      : '载入中'
)

let token = 0

watch(
  () => store.producer.key,
  async (key) => {
    const mine = ++token
    workspace.value = null
    reason.value = key ? '' : 'none'
    if (!key) return
    const module = await producerModule(key)
    if (mine !== token) return
    if (!module?.Workspace) {
      reason.value = 'missing'
      return
    }
    workspace.value = module.Workspace
  },
  { immediate: true }
)
</script>
