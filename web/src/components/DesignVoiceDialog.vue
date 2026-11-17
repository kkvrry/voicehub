<template>
  <!-- 设计音色：不靠参考音频，用一段描述建一条音色。
       「音色设计」这一路本来只能去引擎面板做，做完再回来挑；这里把它挪到用的地方：
       建完立刻成为这次的音色（描述随请求发送，平台不保存音色）。
       描述不绑引擎，凡声明了 `voice_design` 的引擎都能按它合成，所以这里不问引擎。 -->
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="close">
        <div class="vh-modal design-card" role="dialog" aria-modal="true" aria-label="使用设计音色">
          <header class="design-head">
            <div>
              <h2 class="design-title">使用设计音色</h2>
              <p class="design-note">
                描述每次随合成请求发送，平台不保存音色，同一描述不保证每次音色一致；
                建好的这条会进音色库，可反复指定。
              </p>
            </div>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="close">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="design-body">
            <label class="design-row">
              <span class="design-label">名称</span>
              <input
                v-model="name"
                class="sh-field"
                type="text"
                maxlength="40"
                placeholder="如：老者旁白"
              />
            </label>

            <div class="design-row is-block">
              <span class="design-label">描述</span>
              <div class="design-field">
                <textarea
                  v-model="prompt"
                  class="sh-field"
                  rows="3"
                  maxlength="300"
                  placeholder="性别与年龄、音色质感、语气、语速；如：六十岁男声，沙哑低沉，语速偏慢"
                ></textarea>
                <!-- 框里没写字也能点：名称与草稿都会被拿去生成，用户只需改。 -->
                <button
                  type="button"
                  class="design-act"
                  :class="{ 'is-busy': writing }"
                  :disabled="busy || writing || !canWrite"
                  title="AI 写描述"
                  aria-label="让 AI 写音色描述"
                  @click="writePrompt"
                >
                  <Loader2 v-if="writing" class="spin" :size="13" :stroke-width="2" />
                  <Sparkles v-else :size="13" :stroke-width="1.9" />
                </button>
              </div>
            </div>
          </div>

          <footer class="design-foot">
            <span class="hint">{{ error }}</span>
            <button class="sh-btn is-ghost" :disabled="busy" @click="close">取消</button>
            <button class="sh-btn is-primary" :disabled="!canSubmit || busy" @click="submit">
              {{ busy ? '创建中…' : '创建并使用' }}
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
import { Loader2, Sparkles, X } from 'lucide-vue-next'
import { actions, say, store } from '../store'
import { overlayTarget } from '../composables/overlayTarget'

const props = defineProps({
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])


/** 只有声明了 `voice_design` 的引擎能走这一路。 */
const engines = computed(() =>
  (store.engines || []).filter((engine) => engine.capabilities?.voice_design)
)

/**
 * 落哪台引擎：设计音色只是一句描述，各台都是「按描述现场生成」，选谁都一样——
 * 这里只在库内记一处来源，优先用生成页已勾选的那台，再退回第一台可设计的。
 */
const engineKey = computed(() => {
  const checked = store.synth.engines || []
  const hit = engines.value.find((engine) => checked.includes(engine.key))
  return (hit || engines.value[0])?.key || ''
})

const name = ref('')
const prompt = ref('')
const busy = ref(false)
const writing = ref(false)
const error = ref('')

const canWrite = computed(() => Boolean(name.value.trim() || prompt.value.trim()))
const canSubmit = computed(
  () => Boolean(engineKey.value) && Boolean(name.value.trim()) && Boolean(prompt.value.trim())
)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    error.value = ''
    busy.value = false
    writing.value = false
  }
)

function close() {
  if (busy.value) return
  emit('close')
}

/** AI 写一句：名称与已写的草稿一起交给模型，回的那一句直接填进框里再改。 */
async function writePrompt() {
  if (busy.value || writing.value || !canWrite.value) return
  writing.value = true
  error.value = ''
  try {
    prompt.value = await actions.suggestDesignPrompt(name.value.trim(), prompt.value.trim())
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    writing.value = false
  }
}

/** 建完就直接用：新音色立刻成为这次的音色，卡片列顶上那张冻结卡写明它是设计音色。 */
async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  try {
    const meta = await actions.cloneVoice(engineKey.value, {
      name: name.value.trim(),
      design_prompt: prompt.value.trim(),
    })
    const voiceId = meta?.voice_id || meta?.id || ''
    if (!voiceId) throw new Error('创建成功但没有拿到音色 id')
    // 先钉住再换音色：`loadSynthSamples` 会照钉子复原，反过来会被它清掉。
    store.synth.pinned = { voiceId, file: '' }
    store.synth.voiceId = voiceId
    await actions.loadAllVoices(true)
    name.value = ''
    prompt.value = ''
    say(`已创建「${meta.name}」，这次就用它`)
    emit('close')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.design-card {
  width: min(520px, 100%);
  padding: 20px 22px 16px;
  gap: 12px;
}
.design-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.design-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  color: var(--sh-text);
}
.design-note {
  margin: 3px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--sh-text-4);
}
.design-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.design-row {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
}
.design-row.is-block {
  align-items: start;
}
.design-row.is-block .design-label {
  padding-top: 7px;
}
.design-label {
  font-size: 12.2px;
  color: var(--sh-text-2);
}
/* `.sh-field` 给输入框的是一行的高度；描述要写得下两三句，这里按行撑开。 */
.design-field {
  position: relative;
}
.design-row textarea.sh-field {
  min-height: 78px;
  /* 右上一颗 AI 钮要压在框里，首行文字给它让出位置。 */
  padding: 7px 40px 7px 10px;
  line-height: 1.6;
  resize: vertical;
}
.design-act {
  position: absolute;
  top: 5px;
  right: 5px;
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--sh-text-2);
  cursor: pointer;
  transition: background var(--sh-t-fast) var(--sh-ease), color var(--sh-t-fast) var(--sh-ease);
}
.design-act:hover:not(:disabled) {
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}
.design-act.is-busy {
  color: var(--sh-accent);
}
.design-act:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.spin {
  animation: design-spin 0.9s linear infinite;
}
@keyframes design-spin {
  to {
    transform: rotate(360deg);
  }
}
.design-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--sh-line-soft);
}
.design-foot .hint {
  margin: 0 auto 0 0;
  color: var(--sh-danger);
}
</style>
