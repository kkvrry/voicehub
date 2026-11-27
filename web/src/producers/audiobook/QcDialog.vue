<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="$emit('close')">
        <div ref="panelEl" class="vh-modal qc-dialog" role="dialog" aria-modal="true" aria-labelledby="vh-qc-title">
          <header class="qc-head">
            <div>
              <h2 id="vh-qc-title" class="qc-title">预检</h2>
              <p class="qc-note">{{ summary }}</p>
            </div>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="$emit('close')">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="qc-body">
            <p class="qc-hint">{{ hint }}</p>
          </div>

          <footer class="qc-foot">
            <button class="sh-btn is-ghost" @click="$emit('close')">关闭</button>
            <button class="sh-btn is-primary" :disabled="!canRun" @click="$emit('run')">
              开始预检
            </button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import { loadQcReport } from './store'
import { overlayTarget } from '../../composables/overlayTarget'
import { useModalA11y } from '../../composables/modalA11y'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['close', 'run'])
const panelEl = ref(/** @type {any} */ (null))
useModalA11y({ open: () => props.open, panel: panelEl, onClose: () => emit('close') })

const report = ref(/** @type {any} */ (null))

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    report.value = await loadQcReport()
  }
)

const reason = computed(() => String(report.value?.reason || ''))
const summary = computed(() => {
  const data = report.value
  if (!data) return '读取结果……'
  const counts = data.counts || {}
  return [
    `已检 ${data.checked || 0} 行`,
    `疑似 ${counts.suspect || 0} 行`,
    `未检 ${data.pending || 0} 行`,
  ].join(' · ')
})
const hint = computed(() => {
  if (reason.value) return reason.value
  const engine = report.value?.engine || ''
  return engine
    ? `识别引擎：${engine}；只检指纹变过的行，合成串行，不与生成争用显存。`
    : '尚未选择识别引擎。'
})
const canRun = computed(() => Boolean(report.value) && !reason.value && report.value.audio > 0)
</script>

<style scoped>
.qc-dialog {
  width: min(520px, 92vw);
}
.qc-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 18px 20px 0;
}
.qc-title {
  margin: 0;
  font-size: 15px;
  font-weight: 500;
}
.qc-note {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}
.qc-body {
  padding: 14px 20px;
}
.qc-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}
.qc-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 0 20px 18px;
}
</style>
