<template>
  <!-- 「引擎」页的第二列：三组插件（合成 / 对话 / 识别）。
       这一列只回答"装了哪些、现在看的是哪一个"——配置、能力、自检全在右栏。
       前端不认识任何一家：三组都照后端给的自描述画。 -->
  <div class="plugin-rail">
    <section class="plugin-group">
      <div class="plugin-group-head">
        <span>TTS</span>
        <!-- 已启用 / 适配：分子是这一列的行数，分母是适配清单的总数。
             两者相等时不写分母——那个数字不提供任何选择。 -->
        <span class="plugin-group-count">
          {{ ttsRows.length }}<template v-if="moreAvailable"> / {{ catalogTotal }}</template>
        </span>
      </div>
      <button
        v-for="row in ttsRows"
        :key="row.key"
        type="button"
        class="plugin-item"
        :class="{ 'is-active': isActiveTts(row.key) }"
        :title="engineTitle(row)"
        @click="pickEngine(row.key)"
        @contextmenu="openEngineMenu(row, $event)"
      >
        <img v-if="row.icon_url" class="plugin-icon" :src="row.icon_url" alt="" />
        <span v-else class="plugin-icon is-placeholder" aria-hidden="true">◻</span>
        <span class="plugin-name">{{ row.label }}</span>
        <span v-if="!row.ready" class="plugin-warn" title="未就绪">!</span>
      </button>
      <!-- 一台都没适配时只有这一句（那时没有清单可开）。 -->
      <div v-if="!catalogTotal" class="plugin-empty">尚未安装</div>
      <!-- 清单入口挂在组内而不是组头：它管的就是这一组的行，位置要说得出这件事。 -->
      <button
        v-if="catalogTotal"
        type="button"
        class="plugin-add"
        title="查看适配到的全部引擎，逐台启停"
        @click="actions.openEngineCatalog()"
      >
        <SlidersHorizontal :size="14" :stroke-width="1.8" aria-hidden="true" />
        <span>引擎清单</span>
      </button>
    </section>

    <section class="plugin-group">
      <div class="plugin-group-head">
        <span>对话</span>
        <span class="plugin-group-count">{{ llmRows.length }}</span>
      </div>
      <button
        v-for="row in llmRows"
        :key="row.key"
        type="button"
        class="plugin-item"
        :class="{ 'is-active': isActiveLlm(row.key) }"
        :title="row.note && !row.ready ? row.note : row.kind_note || row.label"
        @click="pickLlm(row.key)"
      >
        <img v-if="row.icon_url" class="plugin-icon" :src="row.icon_url" alt="" />
        <span v-else class="plugin-icon is-placeholder" aria-hidden="true">◻</span>
        <span class="plugin-name">{{ row.label }}</span>
        <span v-if="row.is_default" class="plugin-tag">默认</span>
        <span v-if="!row.ready" class="plugin-warn" title="未就绪">!</span>
      </button>
      <div v-if="!llmRows.length" class="plugin-empty">尚未安装</div>
    </section>

    <!-- 识别：方向相反（音频 → 文字），同样是可更换的插件。
         「当前」标的是实际在用的那一台——可以逐台配置，用的只有一台。 -->
    <section class="plugin-group">
      <div class="plugin-group-head">
        <span>识别</span>
        <span class="plugin-group-count">{{ asrRows.length }}</span>
      </div>
      <button
        v-for="row in asrRows"
        :key="row.key"
        type="button"
        class="plugin-item"
        :class="{ 'is-active': isActiveAsr(row.key) }"
        :title="engineTitle(row)"
        @click="pickAsr(row.key)"
      >
        <img v-if="row.icon_url" class="plugin-icon" :src="row.icon_url" alt="" />
        <span v-else class="plugin-icon is-placeholder" aria-hidden="true">◻</span>
        <span class="plugin-name">{{ row.label }}</span>
        <span v-if="row.key === store.asr.current" class="plugin-tag">当前</span>
        <span v-if="!row.ready" class="plugin-warn" title="未就绪">!</span>
      </button>
      <div v-if="!asrRows.length" class="plugin-empty">尚未安装</div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { SlidersHorizontal } from 'lucide-vue-next'
import { actions, say, store } from '../store'
import { openObjectMenu } from '../composables/contextMenu'

type Row = {
  key: string
  label?: string
  icon_url?: string
  ready?: boolean
  note?: string
  kind_note?: string
  is_default?: boolean
  needs_key?: boolean
}

/** TTS 引擎：自描述那一包里已经有图标、就绪态与中文形态说明。 */
const ttsRows = computed<Row[]>(() => (store.engines || []) as Row[])
/** 适配清单的总数：分母只用来表示"还有得选"，不参与任何判断。 */
const catalogTotal = computed(() => store.engineCatalog.length)
/** 还有没启用的才写分母；全启用了那个数字不提供任何选择。 */
const moreAvailable = computed(() => catalogTotal.value > ttsRows.value.length)
const llmRows = computed<Row[]>(() => store.llm.providers as Row[])
/** 识别引擎：同样一套自描述（`store.asr`，后端 /api/v1/asr 那一包）。 */
const asrRows = computed<Row[]>(() => (store.asr?.engines || []) as Row[])

const isActiveTts = (key: string) =>
  store.pluginKind === 'tts' && store.engineKey === key
const isActiveLlm = (key: string) =>
  store.pluginKind === 'llm' && store.llm.key === key
const isActiveAsr = (key: string) =>
  store.pluginKind === 'asr' && store.engineKey === key

/** 悬停说明：只差密钥时不复述（徽标已经说了），其余未就绪原因照原话。
 *  三组插件共用：识别引擎的自描述里同样有 kind_note 与 note（见 asr_base.describe）。 */
function engineTitle(row: Row) {
  if (!row.ready && !row.needs_key && row.note) return row.note
  return row.kind_note || row.label || ''
}

function pickEngine(key: string) {
  if (isActiveTts(key)) return
  void actions.openEngine(key)
}

/** 这一列只摆已启用的引擎：右键能做的只有停用。 */
function openEngineMenu(row: Row, event: MouseEvent) {
  openObjectMenu(
    'engine.row',
    {
      enabled: true,
      toggle: async () => {
        try {
          await actions.disableEngine(row.key)
        } catch (error) {
          say(String(error instanceof Error ? error.message : String(error)))
        }
      },
    },
    { event }
  )
}

function pickLlm(key: string) {
  if (isActiveLlm(key)) return
  void actions.openLlm(key)
}

function pickAsr(key: string) {
  if (isActiveAsr(key)) return
  void actions.openAsrEngine(key)
}

/** 这一列要数出「对话」与「识别」各有几家：清单进页面时现取一次（本机接口，很快）。
 *  失败不弹：右栏会照常说明"没装"或"取不到"，比一个 toast 更接近事实。 */
onMounted(() => {
  if (!store.llm.providers.length) void actions.loadLlm().catch(() => {})
  if (!store.asr.engines.length) void actions.loadAsr().catch(() => {})
  if (!store.engineCatalog.length) void actions.loadEngineCatalog().catch(() => {})
})
</script>

<style scoped>
.plugin-rail {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
  padding: 12px 10px 16px;
  overflow-y: auto;
}
.plugin-group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.plugin-group-head {
  display: flex;
  align-items: baseline;
  padding: 2px 10px 6px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--sh-text-4);
}
.plugin-group-count {
  margin-left: auto;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0;
  color: var(--sh-text-4);
}
.plugin-item {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  min-height: 38px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: var(--sh-r-md);
  background: transparent;
  color: var(--sh-text-2);
  font: inherit;
  font-size: 13px;
  font-weight: 550;
  text-align: left;
  cursor: pointer;
  transition: color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    border-color var(--sh-t) var(--sh-ease);
}
.plugin-item:hover {
  color: var(--sh-text);
  background: var(--sh-hover);
}
.plugin-item.is-active {
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
  border-color: var(--sh-glass-border);
  font-weight: 650;
}
.plugin-icon {
  width: 18px;
  height: 18px;
  flex: none;
  border-radius: 5px;
  object-fit: contain;
}
.plugin-icon.is-placeholder {
  display: grid;
  place-items: center;
  background: var(--sh-chip-bg, rgba(128, 140, 160, 0.14));
  color: var(--sh-text-4);
  font-size: 10px;
}
.plugin-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.plugin-tag {
  flex: none;
  padding: 0 5px;
  border: 1px solid currentColor;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  line-height: 15px;
  opacity: 0.75;
}
.plugin-warn {
  display: grid;
  place-items: center;
  flex: none;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--sh-warn, #d98a1a);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  cursor: help;
}
.plugin-empty {
  padding: 2px 10px 4px;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
/* 「引擎清单」与行同高同左边界：它是这一组多出来的一行，不是另一类控件。 */
.plugin-add {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  min-height: 34px;
  margin-top: 2px;
  padding: 0 10px;
  border: 0;
  border-radius: var(--sh-r-md);
  background: transparent;
  color: var(--sh-text-3);
  font: inherit;
  font-size: 12.5px;
  text-align: left;
  cursor: pointer;
  transition: color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease);
}
.plugin-add:hover {
  color: var(--sh-accent);
  background: var(--sh-hover);
}
.plugin-add:focus-visible {
  outline: 2px solid var(--sh-accent);
  outline-offset: -2px;
}
</style>
