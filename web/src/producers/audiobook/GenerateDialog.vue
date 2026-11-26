<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="$emit('close')">
        <div class="vh-modal generate-dialog" role="dialog" aria-modal="true" aria-label="TTS 生成">
          <header class="generate-head">
            <div>
              <h2 class="generate-title">TTS 生成</h2>
              <p class="generate-note">
                {{ rows }} 行待合成；已经是最新的行会跳过。
              </p>
            </div>
            <button class="sh-btn is-icon" title="关闭" @click="$emit('close')">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="generate-body">
            <section class="generate-group">
              <div class="generate-group-title">输出</div>
              <div class="generate-pair">
                <label class="generate-field" for="generate-concurrency">
                  合成并发
                  <input
                    id="generate-concurrency"
                    v-model.number="concurrency"
                    class="sh-field"
                    type="number"
                    min="1"
                    max="16"
                  />
                </label>
              </div>
              <p class="generate-hint">
                并发只对没有自带并发设置的引擎生效：云端引擎在各自设置里限流，本机引擎按声明只跑一条。
              </p>
              <label class="generate-check">
                <input v-model="qcEnabled" type="checkbox" />
                生成后预检
              </label>
              <p class="generate-hint">
                串行执行，不与生成争用显存；疑似行入行表的「疑似」档，不阻断合并。
              </p>
            </section>
          </div>

          <footer class="generate-foot">
            <button class="sh-btn is-ghost" @click="$emit('close')">取消</button>
            <button class="sh-btn is-soft" :disabled="saving" @click="save(false)">只保存</button>
            <button class="sh-btn is-primary" :disabled="saving" @click="save(true)">
              {{ saving ? '保存中……' : '保存并生成' }}
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
import { state } from './store'
import { overlayTarget } from '../../composables/overlayTarget'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['close', 'run'])

const plugin = computed(() => store.producers.find(/** @param {any} item */ (item) => item.key === store.producer.key))
const schema = computed(() => plugin.value?.settings_schema?.properties || {})
const rows = computed(() => state.detail?.stats?.rows || 0)

const concurrency = ref(4)
const qcEnabled = ref(false)
const saving = ref(false)

/**
 * 候选与默认值都取插件 schema：前端不另抄一份清单与上限。
 *
 * @param {string} key
 * @param {any} fallback
 */
function defaultOf(key, fallback) {
  const spec = schema.value[key] || {}
  const value = 'default' in spec ? spec.default : undefined
  return value === undefined || value === null || value === '' ? fallback : value
}

/**
 * @param {any} value
 * @param {string} key
 * @param {any} fallback
 */
function clamp(value, key, fallback) {
  const spec = schema.value[key] || {}
  const min = Number.isFinite(spec.minimum) ? spec.minimum : 1
  const max = Number.isFinite(spec.maximum) ? spec.maximum : 16
  const number = Number(value)
  const base = Number.isFinite(number) ? number : Number(defaultOf(key, fallback))
  return Math.round(Math.min(max, Math.max(min, base)))
}

watch(
  () => props.open,
  (open) => {
    if (!open) return
    const values = plugin.value?.values || {}
    concurrency.value = Number(values.concurrency) || Number(defaultOf('concurrency', 4))
    qcEnabled.value = Boolean(values.qc_enabled)
  }
)

/**
 * @param {any} thenRun
 */
async function save(thenRun) {
  const current = plugin.value
  if (!current) return
  saving.value = true
  try {
    await actions.saveProducerSettings(current.key, {
      concurrency: clamp(concurrency.value, 'concurrency', 4),
      qc_enabled: qcEnabled.value,
    })
    say('合成设置已保存')
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
.generate-check {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 10px 0 0;
  font-size: 13px;
}
.generate-check input {
  accent-color: var(--color-accent, #534ab7);
}
.generate-dialog {
  width: min(520px, 100%);
  padding: 18px 20px 16px;
  gap: 12px;
}
.generate-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.generate-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  color: var(--sh-text);
}
.generate-note {
  margin: 3px 0 0;
  font-size: 11.8px;
  color: var(--sh-text-4);
}
.generate-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.generate-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 13px 13px;
  border: 1px solid var(--sh-line-soft);
  border-radius: 12px;
  background: var(--sh-glass-soft);
}
.generate-group-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--sh-text-4);
}
.generate-pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 10px;
}
.generate-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
.generate-field .sh-field {
  width: 100%;
}
.generate-hint {
  margin: 0;
  font-size: 11.2px;
  line-height: 1.55;
  color: var(--sh-text-4);
}
.generate-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
