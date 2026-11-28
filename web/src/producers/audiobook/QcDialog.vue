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
            <section class="qc-group">
              <div class="qc-group-title">预检步骤</div>
              <label class="qc-check">
                <input v-model="qcAcoustic" type="checkbox" />
                <span>1. 声学前置检查</span>
              </label>
              <p class="qc-hint">检查静音/无声、尾音截断与时长异常（不依赖识别引擎）。</p>

              <label class="qc-check" :class="{ 'is-disabled': Boolean(reason) }">
                <input v-model="qcAsr" type="checkbox" :disabled="Boolean(reason)" />
                <span>2. ASR 回读比对</span>
              </label>
              <p class="qc-hint">{{ asrHint }}</p>
            </section>

            <section class="qc-group">
              <div class="qc-group-title">合并策略</div>
              <label class="qc-check">
                <input v-model="qcBlockMerge" type="checkbox" />
                <span>疑似行阻断合并</span>
              </label>
              <p class="qc-hint">开启后，章节存在预检「疑似」行时禁止合并成品；关闭则仅提示。</p>
            </section>
          </div>

          <footer class="qc-foot">
            <button class="sh-btn is-ghost" @click="$emit('close')">关闭</button>
            <button class="sh-btn is-soft" :disabled="saving" @click="save(false)">只保存</button>
            <button class="sh-btn is-primary" :disabled="!canRun || saving" @click="save(true)">
              {{ saving ? '保存中……' : '开始预检' }}
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
import { actions, say, store } from '../../store'
import { loadQcReport } from './store'
import { overlayTarget } from '../../composables/overlayTarget'
import { useModalA11y } from '../../composables/modalA11y'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['close', 'run'])
const panelEl = ref(/** @type {any} */ (null))
useModalA11y({ open: () => props.open, panel: panelEl, onClose: () => emit('close') })

const plugin = computed(() => store.producers.find(/** @param {any} item */ (item) => item.key === store.producer.key))
const report = ref(/** @type {any} */ (null))
const qcAcoustic = ref(true)
const qcAsr = ref(true)
const qcBlockMerge = ref(false)
const saving = ref(false)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    const values = plugin.value?.values || {}
    qcAcoustic.value = values.qc_acoustic !== undefined ? Boolean(values.qc_acoustic) : true
    qcAsr.value = values.qc_asr !== undefined ? Boolean(values.qc_asr) : true
    qcBlockMerge.value = Boolean(values.qc_block_merge)
    report.value = await loadQcReport()
    if (report.value) {
      if (values.qc_acoustic === undefined && report.value.acoustic !== undefined) {
        qcAcoustic.value = Boolean(report.value.acoustic)
      }
      if (values.qc_asr === undefined && report.value.asr !== undefined) {
        qcAsr.value = Boolean(report.value.asr)
      }
      if (values.qc_block_merge === undefined && report.value.block_merge !== undefined) {
        qcBlockMerge.value = Boolean(report.value.block_merge)
      }
    }
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
const asrHint = computed(() => {
  if (reason.value) return reason.value
  const engine = report.value?.engine || ''
  return engine
    ? `识别引擎：${engine}；作为最后一步转写音频并与原文音节比对。`
    : '作为最后一步转写音频并与原文音节比对。'
})
const canRun = computed(() => {
  if (!report.value || report.value.audio <= 0) return false
  return qcAcoustic.value || (qcAsr.value && !reason.value)
})

/**
 * @param {boolean} thenRun
 */
async function save(thenRun) {
  const current = plugin.value
  if (!current) return
  saving.value = true
  try {
    await actions.saveProducerSettings(current.key, {
      qc_acoustic: qcAcoustic.value,
      qc_asr: qcAsr.value,
      qc_block_merge: qcBlockMerge.value,
    })
    say('预检设置已保存')
    emit('close')
    if (thenRun) emit('run')
  } catch (error) {
    say(String(error instanceof Error ? error.message : String(error)), { kind: 'error' })
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.qc-dialog {
  width: min(520px, 100%);
  padding: 18px 20px 16px;
  gap: 12px;
}
.qc-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.qc-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  color: var(--sh-text);
}
.qc-note {
  margin: 3px 0 0;
  font-size: 11.8px;
  color: var(--sh-text-4);
}
.qc-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.qc-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 13px 13px;
  border: 1px solid var(--sh-line-soft);
  border-radius: 12px;
  background: var(--sh-glass-soft);
}
.qc-group-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--sh-text-4);
}
.qc-check {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 13px;
  color: var(--sh-text);
}
.qc-check.is-disabled {
  color: var(--sh-text-4);
}
.qc-check input {
  accent-color: var(--sh-accent);
}
.qc-hint {
  margin: 0 0 2px 22px;
  font-size: 11.8px;
  line-height: 1.5;
  color: var(--sh-text-4);
}
.qc-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
