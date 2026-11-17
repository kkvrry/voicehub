<template>
  <!-- 对话模型面板——「引擎」页右栏的「对话」那一组（与 TTS 引擎并列，不是另一页）。
       结构与引擎面板同一套：页签 + panel-body + 常驻底栏，表单共用 SchemaFields，
       所以字号、间距、按钮力度与 TTS 那边一致；换哪一家由第二列的清单决定。 -->
  <section class="app-panel llm-panel">
    <header class="panel-head">
      <img v-if="provider" class="engine-icon lg" :src="provider.icon_url" alt="" />
      <div class="panel-head-main">
        <div class="engine-title">
          {{ provider?.label || '对话模型' }}
          <span v-if="provider" class="badge gray">{{ provider.kind_label }}</span>
          <span v-if="provider" class="badge gray">v{{ provider.version }}</span>
          <span v-if="provider?.ready" class="badge green">已就绪</span>
          <span v-else-if="provider" class="badge warn">未就绪</span>
          <span v-if="provider?.is_default" class="badge gray">默认</span>
        </div>
        <div class="muted">{{ provider?.kind_note || '给消息回消息：分析、归纳、改写都走这一域。' }}</div>
      </div>
      <a
        v-if="provider?.homepage"
        class="engine-link"
        :href="provider.homepage"
        target="_blank"
        rel="noreferrer"
      >
        控制台 ↗
      </a>
    </header>

    <div v-if="!store.llm.providers.length" class="hint">
      没有装对话模型插件。与 TTS 引擎同一种装法：在 app/llm/builtin/ 下建一个目录，重启即出现。
    </div>

    <template v-else>
      <div class="tabs">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="tab"
          :class="{ active: activeSection === tab.key }"
          @click="activeSection = tab.key"
        >
          {{ tab.label }}
        </button>
      </div>

      <div class="panel-body">
        <div v-if="activeSection === 'settings'">
          <SchemaFields
            v-if="provider"
            :schema="provider.settings_schema"
            :values="values"
            :secrets-set="secretsSet"
            :cleared="cleared"
            id-prefix="vh-llm"
            @toggle-clear="toggleClear"
          />
          <div v-if="!fieldCount" class="hint">这家模型不需要配置。</div>
        </div>

        <!-- 能力与模型：能力是**模型本身会什么**（文本 / 推理 / 视觉 / 音频），
             彩色标签；平台清单里的每条也照平台声明的模态与思考档位标。 -->
        <div v-else>
          <div class="section">
            <div class="section-title">能力</div>
            <div v-if="abilities.length" class="ability-tags">
              <span
                v-for="ability in abilities"
                :key="ability.key"
                class="ability-tag"
                :data-ability="ability.key"
                :title="ability.note"
              >
                {{ ability.label }}
              </span>
            </div>
            <div v-else class="hint">这家模型没有声明能力。</div>
          </div>

          <div class="section">
            <div class="section-title">平台模型清单</div>
            <div class="row-actions">
              <button
                class="btn mini"
                :disabled="modelsLoading || !provider?.ready"
                @click="refreshModels"
              >
                {{ modelsLoading ? '拉取中……' : '拉取清单' }}
              </button>
            </div>
            <div v-if="provider?.ready && store.llm.modelsReason" class="muted small">
              {{ store.llm.modelsReason }}
            </div>
            <div v-for="model in store.llm.models" :key="model.id" class="model-row">
              <div class="model-row-id">
                <span class="mono">{{ model.id }}</span>
                <span v-if="model.label && model.label !== model.id" class="muted small">
                  {{ model.label }}
                </span>
              </div>
              <div class="model-row-side">
                <span
                  v-for="ability in model.abilities || []"
                  :key="ability.key"
                  class="ability-tag"
                  :data-ability="ability.key"
                >
                  {{ ability.label }}
                </span>
                <span v-if="model.context" class="model-context">{{ contextText(model.context) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="activeSection === 'settings'" class="panel-foot">
        <button class="btn" :disabled="saving" @click="resetDefaults">恢复默认</button>
        <button
          v-if="provider && !provider.is_default"
          class="btn"
          :disabled="saving"
          @click="makeDefault"
        >
          设为默认
        </button>
        <button class="btn" :disabled="store.llm.testing || !provider?.ready" @click="test">
          {{ store.llm.testing ? '测试中……' : '测试连通' }}
        </button>
        <button class="btn primary" :disabled="saving || !canSave" @click="save">
          {{ saving ? '保存中……' : '保存' }}
        </button>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { actions, currentLlm, say, store } from '../store'
import SchemaFields from './ui/SchemaFields.vue'

type Ability = { key: string; label: string; note?: string }

const provider = computed(() => currentLlm())

/** 与引擎面板同一副骨架：设置在最前，能力与平台清单放第二块。 */
const tabs = [
  { key: 'settings', label: '设置' },
  { key: 'abilities', label: '能力与模型' },
]
const activeSection = ref('settings')

const values = ref<Record<string, unknown>>({})
const cleared = ref<string[]>([])
const saving = ref(false)
const modelsLoading = ref(false)

const secretsSet = computed(() => new Set<string>(provider.value?.secrets_set || []))
const abilities = computed<Ability[]>(() => provider.value?.abilities || [])
const fieldCount = computed(
  () => Object.keys(provider.value?.settings_schema?.properties || {}).length
)

/** 表单初值 = schema 默认值，再被服务器上真正存着的值覆盖（机密是空的）。 */
function resetFrom(payload: Record<string, any> | null) {
  if (!payload) return
  const next: Record<string, unknown> = {}
  for (const [key, spec] of Object.entries(payload.settings_schema?.properties || {})) {
    next[key] = (spec as Record<string, unknown>).default ?? ''
  }
  Object.assign(next, payload.values || {})
  values.value = next
  cleared.value = []
}

const canSave = computed(() => {
  const schema = provider.value?.settings_schema || {}
  const required: string[] = Array.isArray(schema.required) ? schema.required : []
  return required.every((key) => {
    const value = values.value[key]
    if (schema.properties?.[key]?.secret) {
      return secretsSet.value.has(key) || Boolean(String(value || '').trim())
    }
    return value !== '' && value !== null && value !== undefined
  })
})

function toggleClear(key: string) {
  const index = cleared.value.indexOf(key)
  if (index >= 0) cleared.value.splice(index, 1)
  else {
    cleared.value.push(key)
    values.value[key] = ''
  }
}

async function save() {
  if (!provider.value) return
  saving.value = true
  try {
    const body: Record<string, unknown> = { ...values.value }
    if (cleared.value.length) body._clear = [...cleared.value]
    const saved = await actions.saveLlmSettings(provider.value.key, body)
    resetFrom(saved)
  } catch (error) {
    say(String((error as Error).message || error))
  } finally {
    saving.value = false
  }
}

async function resetDefaults() {
  if (!provider.value) return
  saving.value = true
  try {
    resetFrom(await actions.resetLlmSettings(provider.value.key))
  } catch (error) {
    say(String((error as Error).message || error))
  } finally {
    saving.value = false
  }
}

async function makeDefault() {
  if (!provider.value) return
  try {
    await actions.setDefaultLlm(provider.value.key)
  } catch (error) {
    say(String((error as Error).message || error))
  }
}

/** 自检结果弹一句：面板里不再常驻一行会过期的说明。 */
async function test() {
  if (!provider.value) return
  try {
    const result = await actions.testLlm(provider.value.key)
    say(result.ok ? `可用（${result.latency_ms} ms）` : result.note || '不可用')
  } catch (error) {
    say(String((error as Error).message || error))
  }
}

async function refreshModels() {
  modelsLoading.value = true
  try {
    await actions.loadLlmModels(provider.value?.key || '')
  } finally {
    modelsLoading.value = false
  }
}

/** 1M / 128K 这种写法：数量级比精确位数好读，这一列只是参考。 */
function contextText(tokens: number) {
  const n = Number(tokens) || 0
  if (!n) return ''
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M 上下文`
  return `${Math.round(n / 1024)}K 上下文`
}

/** 换一家才重画：清单每次刷新都会换对象，盯着对象会把用户点的页签一路弹回「设置」。 */
watch(
  () => provider.value?.key,
  () => {
    resetFrom(provider.value)
    activeSection.value = 'settings'
  },
  { immediate: true }
)

onMounted(() => {
  if (!store.llm.providers.length) void actions.loadLlm().catch(() => {})
})
</script>

<style scoped>
.ability-tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.ability-tag {
  display: inline-flex;
  align-items: center;
  height: 21px;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 650;
  line-height: 1;
  white-space: nowrap;
}
.ability-tag[data-ability='text'] {
  color: #1d4ed8;
  background: rgba(37, 99, 235, 0.11);
  border-color: rgba(37, 99, 235, 0.26);
}
.ability-tag[data-ability='reasoning'] {
  color: #6d28d9;
  background: rgba(124, 58, 237, 0.11);
  border-color: rgba(124, 58, 237, 0.26);
}
.ability-tag[data-ability='vision'] {
  color: #047857;
  background: rgba(5, 150, 105, 0.11);
  border-color: rgba(5, 150, 105, 0.26);
}
.ability-tag[data-ability='audio'] {
  color: #b45309;
  background: rgba(217, 119, 6, 0.12);
  border-color: rgba(217, 119, 6, 0.28);
}
:root[data-theme='dark'] .ability-tag[data-ability='text'] {
  color: #9dbcff;
  background: rgba(88, 132, 255, 0.16);
  border-color: rgba(88, 132, 255, 0.34);
}
:root[data-theme='dark'] .ability-tag[data-ability='reasoning'] {
  color: #c4b0ff;
  background: rgba(139, 92, 246, 0.16);
  border-color: rgba(139, 92, 246, 0.34);
}
:root[data-theme='dark'] .ability-tag[data-ability='vision'] {
  color: #7fe0c0;
  background: rgba(16, 185, 129, 0.15);
  border-color: rgba(16, 185, 129, 0.34);
}
:root[data-theme='dark'] .ability-tag[data-ability='audio'] {
  color: #f3bc7c;
  background: rgba(217, 119, 6, 0.16);
  border-color: rgba(217, 119, 6, 0.34);
}

.model-row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 9px 0;
  border-top: 1px solid var(--border);
}
.model-row-id {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.model-row-id .mono {
  font-size: 12.5px;
}
.model-row-side {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
.model-context {
  font-size: 11.5px;
  color: var(--muted);
  white-space: nowrap;
}
</style>
