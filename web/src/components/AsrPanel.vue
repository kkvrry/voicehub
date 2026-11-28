<template>
  <!-- 识别引擎面板——「引擎」页右栏的「识别」那一组（与 TTS、对话并列，不是设置页里的一栏）。
       这一页不认识任何一家识别引擎：表单照 settings_schema 画、能力照 returns 画、
       语言清单照 language_groups 画、输入格式与流式照 input / streaming 画、
       请求与返回格式照 api_format 画。换模型 = 换一个插件目录，这里一行不用改。 -->
  <section v-if="engine" class="app-panel asr-panel">
    <header class="panel-head">
      <img class="engine-icon lg" :src="engine.icon_url" alt="" />
      <div class="panel-head-main">
        <div class="engine-title">
          {{ engine.label }}
          <span class="badge gray">{{ engine.vendor }}</span>
          <span class="badge gray">v{{ engine.version }}</span>
          <span v-if="isCurrent" class="badge accent">当前</span>
          <span v-if="engine.ready" class="badge green">已就绪</span>
          <span v-else class="badge warn">未就绪</span>
        </div>
      </div>
      <a
        v-if="engine.homepage"
        class="engine-link"
        :href="engine.homepage"
        target="_blank"
        rel="noreferrer"
      >
        官方文档 ↗
      </a>
    </header>

    <!-- 只差密钥时不摆这句话：「未就绪」徽标已经说过，面板里再写一遍是噪声。 -->
    <div v-if="!engine.ready && !engine.needs_key" class="hint" data-tone="warn">
      {{ engine.note }}
    </div>

    <div class="tabs">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="tab"
        :class="{ active: activeSection === tab.key }"
        @click="activeSection = tab.key"
      >
        {{ tab.label }}
      </button>
    </div>

    <div class="panel-body">
      <!-- ------------------------------ 设置 ------------------------------ -->
      <div v-if="activeSection === 'settings'">
        <NoteText v-if="schemaNote" class="schema-note" :text="schemaNote" />
        <div v-if="!fieldCount" class="hint">这台引擎不需要配置。</div>
        <SchemaFields
          v-else
          :schema="engine.settings_schema"
          :values="values"
          :secrets-set="secretsSet"
          :cleared="cleared"
          id-prefix="vh-asr"
          @toggle-clear="toggleClear"
        />
      </div>

      <!-- --------------------------- 能力与参数 --------------------------- -->
      <div v-else class="panel-stack">
        <!-- 能力全表：**没有的也列出来**——"不回报情绪"正是要核对的那一条。
             只列有的，界面就分不清"这台没有"和"这家没声明"。表格由插件的
             能力声明生成，前端不认识任何一家模型。 -->
        <PanelCard title="能力">
          <table class="map-table cap-table">
            <thead>
              <tr>
                <th>项目</th>
                <th>声明</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in capabilityRows" :key="row.key" :class="{ 'is-off': !row.has }">
                <td><span class="cap-name">{{ row.label }}</span></td>
                <td>
                  <span class="cap-mark" :class="{ on: row.has }">
                    <Check v-if="row.has" :size="13" :stroke-width="2.2" aria-hidden="true" />
                    <Minus v-else :size="13" :stroke-width="2.2" aria-hidden="true" />
                    {{ row.has ? '支持' : '不支持' }}
                  </span>
                </td>
                <td class="map-note">{{ row.note }}</td>
              </tr>
            </tbody>
          </table>
        </PanelCard>

        <!-- 语言覆盖：插件登记到逐项（代码 + 名称），界面不写死任何一份清单。
             合计已在两个组头的徽标上，不再单起一行重复一遍。 -->
        <PanelCard v-if="languageGroups.length" title="语言覆盖">
          <div class="asr-lang-grid">
            <section v-for="group in languageGroups" :key="group.key" class="asr-lang-group">
              <div class="asr-lang-head">
                <span class="asr-lang-title">{{ group.label }}</span>
                <span class="badge gray">{{ group.items.length }}</span>
              </div>
              <p v-if="group.note" class="asr-lang-note">{{ group.note }}</p>
              <table class="map-table lang-table">
                <thead>
                  <tr>
                    <th>语言</th>
                    <th>代码</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in group.items" :key="item.name">
                    <td>{{ item.name }}</td>
                    <td class="mono">{{ item.code || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </section>
          </div>
        </PanelCard>

        <!-- 请求与返回的格式由插件声明（api_format）：协议换了只改插件，
             界面不做第二份解释。 -->
        <PanelCard title="请求与返回">
          <div class="asr-format">
            <span class="asr-format-key">请求</span>
            <span class="asr-format-value">{{ engine.api_format?.request || '—' }}</span>
          </div>
          <div class="asr-format">
            <span class="asr-format-key">返回</span>
            <span class="asr-format-value">{{ engine.api_format?.response || '—' }}</span>
          </div>
          <div class="asr-format">
            <span class="asr-format-key">输入</span>
            <span class="asr-format-value">
              {{ inputFormats }}<template v-if="engine.input?.note">；{{ engine.input.note }}</template>
            </span>
          </div>
          <div class="asr-format">
            <span class="asr-format-key">流式</span>
            <span class="asr-format-value">
              {{ engine.streaming?.supported ? '支持' : '不启用'
              }}<template v-if="engine.streaming?.note">；{{ engine.streaming.note }}</template>
            </span>
          </div>
        </PanelCard>
      </div>
    </div>

    <!-- 底栏常驻：跟着内容滚出去的话，「保存」就成了藏在页面最下面的按钮。
         探测与「设为当前」与对话模型那边同一套位置——都在底栏，不占正文。 -->
    <div v-if="activeSection === 'settings'" class="panel-foot">
      <span v-if="probeResult" class="asr-probe" :class="{ bad: !probeResult.ok }">
        {{ probeResult.message }}
      </span>
      <ActionButton :disabled="saving" @click="resetDefaults">恢复默认</ActionButton>
      <ActionButton v-if="!isCurrent" :disabled="saving" @click="useEngine">设为当前</ActionButton>
      <ActionButton :loading="probing" @click="probeEngine">测试连接</ActionButton>
      <ActionButton
        variant="primary"
        :loading="saving"
        :disabled="!canSave"
        :title="canSave ? '保存这台引擎的设置' : '没有需要保存的改动'"
        @click="save"
      >
        保存
      </ActionButton>
    </div>
  </section>

  <!-- 取不到那台时说什么（否则就是一片空白，点错行的人只会以为页面坏了）：
       插件被卸了、或一台识别引擎都没装。 -->
  <section v-else class="app-panel asr-panel">
    <header class="panel-head">
      <div class="panel-head-main">
        <div class="engine-title">
          {{ asr.engines.length ? '识别引擎不存在' : '没有可用的识别引擎' }}
        </div>
        <div class="muted">
          {{
            asr.engines.length
              ? '插件可能已被卸载，重启后这一行会消失。'
              : '识别引擎也是插件：在 app/engines/builtin/ 下建一个目录（与 TTS 引擎同一种装法），重启即出现。'
          }}
        </div>
      </div>
    </header>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, Minus } from 'lucide-vue-next'
import { actions, say, store } from '../store'
import { asrEngineOf } from '../store/asr'
import ActionButton from './ui/ActionButton.vue'
import NoteText from './NoteText.vue'
import PanelCard from './ui/PanelCard.vue'
import SchemaFields from './ui/SchemaFields.vue'

/** 一台识别引擎的自描述（后端 `/api/v1/asr` 那一行，已掩码）。
 *  形状与合成引擎的 describe 平行：前端照字段画，不认识任何一家。 */
interface AsrEngineRow {
  key: string
  label: string
  vendor: string
  version: string
  homepage: string
  kind: string
  kind_label: string
  kind_note: string
  icon_url: string
  ready: boolean
  note: string
  needs_key: boolean
  values: Record<string, unknown>
  secrets_set: string[]
  settings_schema: Record<string, any>
  capabilities?: {
    languages?: string
    returns_language?: boolean
    returns_emotion?: boolean
    returns_events?: boolean
    timestamps?: boolean
  }
  returns?: { key: string; label: string; note: string; has: boolean }[]
  language_groups?: {
    key: string
    label: string
    note: string
    items: { code: string; name: string }[]
  }[]
  input?: { formats?: string[]; note?: string }
  streaming?: { supported?: boolean; note?: string }
  api_format?: { request: string; response: string }
}

const asr = store.asr as unknown as {
  engines: AsrEngineRow[]
  current: string
  ready: boolean
  note: string
}

/** 正在看的这一台：左栏点哪行看哪台（与 TTS 面板同一个 engineKey，pluginKind 区分种类）。 */
const engine = computed<AsrEngineRow | null>(
  () => asrEngineOf(store.engineKey) as unknown as AsrEngineRow | null
)
const isCurrent = computed(() => engine.value?.key === asr.current)

const tabs = [
  { key: 'settings', label: '设置' },
  { key: 'about', label: '能力与参数' },
]
const activeSection = ref('settings')

// ---- 设置表单（与引擎面板同一套语义：服务器回来的那份才是准的）----
const values = ref<Record<string, unknown>>({})
const cleared = ref<string[]>([])
const saving = ref(false)

const secretsSet = computed(() => new Set<string>(engine.value?.secrets_set || []))
const schemaNote = computed(() => engine.value?.settings_schema?.description || '')
const capabilityRows = computed(() => engine.value?.returns || [])
const languageGroups = computed(() => engine.value?.language_groups || [])
const inputFormats = computed(() => {
  const formats = engine.value?.input?.formats || []
  return formats.length ? formats.map((f) => f.toUpperCase()).join(' / ') : '—'
})
const fieldCount = computed(
  () => Object.keys(engine.value?.settings_schema?.properties || {}).length
)

/** 一次性的探测结果：按 key 记住，切换引擎时再看它自己的。 */
const probes = ref<Record<string, { ok: boolean; message: string }>>({})
const probeResult = computed(() =>
  engine.value ? probes.value[engine.value.key] || null : null
)

/** 探测进行中：记着正在问哪一台（'' = 没在问）。与 `probes` 分工——
 *  一个是"正在问"，一个是"问完的结果"。 */
const probePending = ref('')
const probing = computed(() => probePending.value !== '')

function toggleClear(key: string) {
  const index = cleared.value.indexOf(key)
  if (index >= 0) cleared.value.splice(index, 1)
  else {
    cleared.value.push(key)
    values.value[key] = ''
  }
}

/** 表单初值 = schema 的默认值，再被服务器上真正存着的值覆盖（机密是空的）。 */
function resetFrom(payload: Record<string, any> | null) {
  if (!payload) return
  const next: Record<string, unknown> = {}
  for (const [key, spec] of Object.entries(payload.settings_schema?.properties || {})) {
    next[key] = (spec as { default?: unknown }).default ?? ''
  }
  Object.assign(next, payload.values || {})
  values.value = next
  cleared.value = []
}

/** 必填项为空就不让按保存，省一次"点了才知道少东西"的往返。 */
const canSave = computed(() => {
  const schema = engine.value?.settings_schema || {}
  const required = Array.isArray(schema.required) ? schema.required : []
  return required.every((key: string) => {
    const value = values.value[key]
    if (schema.properties?.[key]?.secret) {
      return secretsSet.value.has(key) || Boolean(String(value || '').trim())
    }
    return value !== '' && value !== null && value !== undefined
  })
})

async function save() {
  if (!engine.value) return
  saving.value = true
  try {
    const body: Record<string, unknown> = { ...values.value }
    if (cleared.value.length) body._clear = [...cleared.value]
    const saved = await actions.saveAsrSettings(engine.value.key, body)
    resetFrom(saved) // 服务器回来的那份才是准的（机密重新变回空串）
    say(`「${engine.value.label}」已保存`)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    saving.value = false
  }
}

/** 恢复默认：**后端落**（非机密回落、机密保留），回来的那一份重画表单。 */
async function resetDefaults() {
  if (!engine.value) return
  saving.value = true
  try {
    const saved = await actions.saveAsrSettings(engine.value.key, { _reset: true })
    resetFrom(saved)
    say('已恢复默认（密钥保留）')
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    saving.value = false
  }
}

async function probeEngine() {
  if (!engine.value) return
  probePending.value = engine.value.key
  try {
    const result = await actions.probeAsr(engine.value.key)
    probes.value = {
      ...probes.value,
      [engine.value.key]: { ok: result.ok, message: result.message },
    }
  } catch (error) {
    probes.value = {
      ...probes.value,
      [engine.value.key]: { ok: false, message: error instanceof Error ? error.message : String(error) },
    }
  } finally {
    probePending.value = ''
  }
}

async function useEngine() {
  if (!engine.value) return
  try {
    await actions.setAsrCurrent(engine.value.key)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  }
}

/** 换一台就重画表单：上一台的输入不该留在这一台上。 */
watch(
  () => engine.value?.key,
  () => resetFrom(engine.value),
  { immediate: true }
)

// 保存成功后 `store.asr` 换新：机密变回空串——但表单里的用户输入不能被冲掉，
// 只在"当前没有未保存改动"时才跟着后端走。
watch(
  () => engine.value?.values,
  () => {
    if (!saving.value) resetFrom(engine.value)
  }
)
</script>

<style scoped>
/* 探测结果落在底栏左侧：与刚点的那颗按钮同一行，不用往正文里找。 */
.asr-probe {
  margin-right: auto;
  min-width: 0;
  max-width: 58%;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--sh-text-3);
  overflow-wrap: anywhere;
}
.asr-probe.bad {
  color: var(--sh-danger);
}
.panel-foot :deep(.sh-btn) {
  flex: none;
}
/* 请求与返回：左边一个键、右边一句话。键宽固定，长句子整块换行。 */
.asr-format {
  display: grid;
  grid-template-columns: 44px 1fr;
  gap: 10px;
  padding: 7px 0;
  font-size: 12.5px;
  line-height: 1.65;
}
.asr-format + .asr-format {
  border-top: 1px dashed var(--sh-line);
}
.asr-format-key {
  font-weight: 600;
  color: var(--sh-text-3);
}
.asr-format-value {
  color: var(--sh-text-2);
  word-break: break-all;
}
/* 能力表：能力名 / 声明 / 说明。声明列只占一个小格，说明吃掉余下宽度。 */
.map-table.cap-table th:nth-child(1),
.map-table.cap-table td:nth-child(1) {
  width: 30%;
}
.map-table.cap-table th:nth-child(2),
.map-table.cap-table td:nth-child(2) {
  width: 18%;
}
.map-table.cap-table th:nth-child(3),
.map-table.cap-table td:nth-child(3) {
  width: auto;
}
.cap-table th,
.cap-table td,
.lang-table th,
.lang-table td {
  padding: 6px 8px 6px 0;
}
.cap-table tbody tr + tr,
.lang-table tbody tr + tr {
  border-top: 1px dashed var(--sh-line);
}
.cap-table tr.is-off .cap-name {
  color: var(--sh-text-3);
}
.cap-name {
  font-weight: 600;
  color: var(--sh-text);
}
/* 勾选态用文字 + 图标：只靠颜色区分，打印或色弱时就分不清。 */
.cap-mark {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 7px;
  border-radius: 4px;
  background: var(--sh-chip-bg, rgba(128, 140, 160, 0.14));
  color: var(--sh-text-3);
  font-size: 11px;
  font-weight: 600;
  line-height: 18px;
}
.cap-mark.on {
  background: var(--sh-ok-soft);
  color: var(--sh-ok);
}
/* 语言覆盖：两张表并排（30 种语言 / 22 种方言）。 */
.asr-lang-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px 24px;
}
.asr-lang-group {
  min-width: 0;
}
.asr-lang-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.asr-lang-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--sh-text);
}
.asr-lang-note {
  margin: 2px 0 6px;
  font-size: 11.5px;
  color: var(--sh-text-3);
}
.map-table.lang-table th:nth-child(1),
.map-table.lang-table td:nth-child(1) {
  width: auto;
}
.map-table.lang-table th:nth-child(2),
.map-table.lang-table td:nth-child(2) {
  width: 30%;
}
@media (max-width: 760px) {
  .asr-lang-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
