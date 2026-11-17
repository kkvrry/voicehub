<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="voice" class="vh-root" @click.self="close">
        <div
          ref="dialogEl"
          class="vh-modal avatar-picker"
          role="dialog"
          aria-modal="true"
          :aria-label="`选择 ${title} 的头像`"
          tabindex="-1"
        >
          <header class="ap-head">
            <div>
              <h2 class="ap-title">选择「{{ title }}」的头像</h2>
              <p class="ap-note">各来源裁好的图都在这里，点一张就保存；旧头像留在 avatar-prev。</p>
            </div>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="close">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div v-if="loading" class="ap-state">
            <Loader2 :size="18" class="sh-spin" />
            正在从各来源查找…
          </div>
          <div v-else-if="error" class="ap-state is-error">{{ error }}</div>
          <template v-else>
            <p v-if="notice" class="ap-notice">{{ notice }}</p>
            <div class="ap-grid">
              <button
                v-for="item in items"
                :key="item.token"
                type="button"
                class="ap-card"
                :disabled="Boolean(applying)"
                :title="`使用 ${item.source} 的这张`"
                @click="use(item)"
              >
                <img :src="item.preview" alt="" />
                <span class="ap-source">{{ item.source }}</span>
                <span v-if="applying === item.token" class="ap-busy">
                  <Loader2 :size="18" class="sh-spin" />
                </span>
              </button>
            </div>
          </template>

          <p class="ap-foot">
            都认不出本人？在音色目录放一张 <code>cover.png</code> 后重新扫描；想回到上一张，
            把 <code>avatar-prev.*</code> 改名成 <code>avatar.png</code>。
          </p>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Loader2, X } from 'lucide-vue-next'
import { api } from '../api'
import { overlayTarget } from '../composables/overlayTarget'

const props = defineProps({ voice: { type: Object, default: null } })
const emit = defineEmits(['close', 'applied'])

/** 弹层挂进壳里（`overlayTarget`）：`--panel` 这套别名只在 `.app-shell` 上。 */

const dialogEl = ref(/** @type {HTMLElement | null} */ (null))
const items = ref(/** @type {any[]} */ ([]))
const loading = ref(false)
const error = ref('')
const notice = ref('')
const applying = ref('')
/** 只认最后一次请求：换音色时上一批结果晚到，不能把界面填成别人的。 */
let requestId = 0

const title = computed(() => props.voice?.shown_name || props.voice?.name || '')

function close() {
  if (applying.value) return
  emit('close')
}

/** @param {KeyboardEvent} event */
function onKeydown(event) {
  if (event.key !== 'Escape') return
  event.preventDefault()
  close()
}

async function load() {
  const voice = props.voice
  if (!voice) return
  const id = ++requestId
  items.value = []
  error.value = ''
  notice.value = ''
  loading.value = true
  try {
    const payload = await api.avatarCandidates(voice.id)
    if (id !== requestId) return
    items.value = payload.data || []
    if (!items.value.length) error.value = '没有找到可用的候选图。'
  } catch (err) {
    if (id !== requestId) return
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    if (id === requestId) loading.value = false
  }
}

/** @param {any} item */
async function use(item) {
  const voice = props.voice
  if (!voice || applying.value) return
  applying.value = item.token
  try {
    const result = await api.applyAvatar(voice.id, item.token)
    emit('applied', result)
    emit('close')
  } catch (err) {
    // 应用失败不换掉候选：换一张重试，或关掉重开。
    notice.value = err instanceof Error ? err.message : String(err)
  } finally {
    applying.value = ''
  }
}

watch(
  () => props.voice?.id,
  (id) => {
    if (!id) return
    void load()
    void nextTick(() => dialogEl.value?.focus())
  },
  { immediate: true }
)

watch(
  () => Boolean(props.voice),
  (open) => {
    if (open) window.addEventListener('keydown', onKeydown)
    else window.removeEventListener('keydown', onKeydown)
  },
  { immediate: true }
)

onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<style scoped>
.avatar-picker {
  width: min(720px, 100%);
  padding: 20px 22px 16px;
  gap: 14px;
}
.ap-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.ap-title {
  margin: 0;
  font-size: 16px;
  font-weight: 650;
  color: var(--sh-text);
}
.ap-note {
  margin: 4px 0 0;
  font-size: 11.8px;
  color: var(--sh-text-4);
}
.ap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(128px, 1fr));
  gap: 10px;
  overflow-y: auto;
  padding-right: 2px;
}
.ap-card {
  position: relative;
  aspect-ratio: 1;
  padding: 0;
  border: 1px solid var(--sh-line);
  border-radius: 14px;
  overflow: hidden;
  background: var(--sh-glass-soft);
  cursor: pointer;
  transition: border-color 140ms ease, transform 140ms ease;
}
.ap-card img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.ap-card:hover:not(:disabled) {
  border-color: var(--sh-accent);
  transform: translateY(-1px);
}
.ap-card:disabled {
  cursor: default;
  opacity: 0.65;
}
.ap-source {
  position: absolute;
  left: 7px;
  bottom: 7px;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.58);
  color: #fff;
  font-size: 10.5px;
  line-height: 1.5;
}
.ap-busy {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  background: rgba(0, 0, 0, 0.35);
  color: #fff;
}
.ap-state {
  min-height: 180px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--sh-text-3);
  font-size: 13px;
}
.ap-state.is-error {
  color: var(--danger);
}
.ap-notice {
  margin: 0;
  padding: 8px 11px;
  border-radius: var(--sh-r-xs);
  background: var(--warn-soft, rgba(255, 170, 0, 0.12));
  color: var(--warn, #a15c00);
  font-size: 12px;
  line-height: 1.6;
}
.ap-foot {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--sh-text-4);
}
.ap-foot code {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--sh-glass-soft);
}
</style>
