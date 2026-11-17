<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="close">
        <div
          class="vh-modal voice-tools"
          role="dialog"
          aria-modal="true"
          aria-label="音色管理"
        >
          <header class="vt-head">
            <h2 class="vt-title">音色管理</h2>
            <SegmentedControl v-model="tab" label="音色栏目" :options="tabs" />
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="close">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <!-- 两栏都常驻：切过去看一眼再回来，AI 刚算好的分配表还在。 -->
          <VoiceAssignPanel
            v-show="tab === 'assign'"
            :open="open"
            :roles="assignRoles"
            @applied="onApplied"
            @close="close"
          />
          <VoiceMergePanel
            v-show="tab === 'merge'"
            :open="open"
            :roles="mergeRoles"
            @merge="$emit('merge', $event)"
            @cancel="close"
          />
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import SegmentedControl from '../../components/ui/SegmentedControl.vue'
import VoiceAssignPanel from './VoiceAssignPanel.vue'
import VoiceMergePanel from './VoiceMergePanel.vue'
import { overlayTarget } from '../../composables/overlayTarget'

const props = defineProps({
  open: { type: Boolean, default: false },
  assignRoles: { type: Array, default: () => [] },
  mergeRoles: { type: Array, default: () => [] },
})
const emit = defineEmits(['close', 'applied', 'merge'])

const tabs = [
  { label: '音色分配', value: 'assign' },
  { label: '角色合并', value: 'merge' },
]
const tab = ref('assign')

watch(
  () => props.open,
  (open) => {
    if (open) tab.value = 'assign'
  }
)

/** @param {any} payload */
function onApplied(payload) {
  emit('applied', payload)
  close()
}

function close() {
  emit('close')
}
</script>

<style scoped>
/* 分配与合并共用一个窗口尺寸：切换栏目时不跳宽窄与高矮。
   两栏内容各自在内部滚动，固定高度不会把底栏挤出屏。 */
.voice-tools {
  width: min(1020px, 100%);
  height: min(88vh, 860px);
  max-height: min(88vh, 860px);
  padding: 18px 20px 14px;
  gap: 12px;
}
.vt-head {
  flex: none;
  display: flex;
  align-items: center;
  gap: 14px;
}
.vt-title {
  flex: none;
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  color: var(--sh-text);
}
</style>
