<!-- 插件配置表单：**只认 pydantic 的 JSON Schema**，不认识任何一家引擎或模型。
     引擎页与对话模型页共用这一份——两处各写一遍的话，机密掩码、词表外取值、
     「留空即不改」这些规则迟早只在一处被修好。

     分组也一样：**组名由插件在 ``ui_hints(group=...)`` 里声明**，这里只负责
     按声明把字段分进卡片。没有一个字段声明分组时退回一张平表，
     对话模型那种三四格的表单不必凭空多出几张卡。

     ``layout="rows"`` 给设置页用：卡片、行表、控件宽度也由这里出，调用方只传
     schema 与值——版式只有这一份产地，生产方与设置页各画一套必然会偏。 -->
<template>
  <div class="schema-form" :class="`is-${layout}`">
    <template v-if="layout === 'rows'">
      <SettingCard
        v-for="group in groups"
        :key="group.name || 'flat'"
        :title="groupTitle(group)"
        :keywords="groupKeywords(group)"
        :bare="bare"
      >
        <template v-if="$slots['group-icon']" #icon>
          <slot name="group-icon" :title="group.title" />
        </template>

        <SettingRow
          v-for="field in group.fields"
          :key="field.key"
          :title="field.label"
          :desc="field.description"
          :keywords="rowKeywords(field)"
          :stacked="field.widget === 'textarea' || field.open"
          :class="{ 'is-disabled': !field.enabled }"
        >
          <template #badge>
            <span v-if="field.required" class="req-tag">必填</span>
            <span v-if="field.secret" class="secret-tag">机密</span>
            <span v-if="field.badge" class="sh-chip">{{ field.badge }}</span>
          </template>

          <div class="schema-row-control">
            <!-- 字段声明 open：指向服务本机的一个目录，行尾给一枚「打开」，
                 与设置页「存储位置」同一套样式；没有声明就不多出这枚按钮。 -->
            <div class="schema-control-line" :class="{ 'is-contents': !field.open }">
              <SchemaFieldControl
                :field="field"
                :values="values"
                :providers="providers"
                :models="models"
                :model-note="modelNote"
                :secrets-set="secretsSet"
                :cleared="cleared"
                :id-prefix="idPrefix"
                :disabled="!field.enabled"
                segmented
                @channel="onChannel"
              />
              <ActionButton
                v-if="field.open"
                variant="ghost"
                size="sm"
                @click="$emit('open-field', field.key)"
              >
                <FolderOpen :size="14" :stroke-width="1.8" />
                打开
              </ActionButton>
            </div>

            <!-- 已存的机密：空串在这套接口里表示"不改"，"真的抹掉"得单独给个去处。 -->
            <div v-if="field.secret && secretsSet.has(field.key)" class="secret-row">
              <span v-if="cleared.includes(field.key)" class="muted small">保存后这一项会被清空。</span>
              <span v-else class="muted small">已保存（留空即不改）。</span>
              <button class="btn mini" @click="$emit('toggle-clear', field.key)">
                {{ cleared.includes(field.key) ? '撤销' : '清除' }}
              </button>
            </div>
          </div>

          <!-- 依赖别的格子才生效：置灰并说清条件，不把无效项画成"改了有用"。 -->
          <template #below>
            <p v-if="!field.enabled" class="field-note">{{ field.disabledNote }}</p>
          </template>
        </SettingRow>
      </SettingCard>

      <div v-if="!shownCount" class="muted small">这个插件没有配置项。</div>
    </template>

    <!-- 网格表单不套卡片：引擎页与对话模型页都只是一张平面上的表单。
         套一层卡片，在纸墨 / 素瓷里那圈边就是"整张表被包起来"的样子，
         同一页里的两个插件也会一个有线、一个没有。分组只留标题。 -->
    <template v-else>
      <section
        v-for="group in groups"
        :key="group.name || 'flat'"
        class="schema-group"
        :class="{ 'is-flat': !group.title }"
      >
        <h3 v-if="group.title" class="schema-group-title">{{ group.title }}</h3>
        <div class="schema-grid" :class="{ 'is-flat': !group.title }">
          <fieldset
            v-for="field in group.fields"
            :key="field.key"
            class="schema-field"
            :class="{ 'is-wide': field.wide, 'is-switch': field.widget === 'switch' }"
            :disabled="!field.enabled"
          >
            <label :for="fieldId(field.key)">
              {{ field.label }}
              <span v-if="field.required" class="req-tag">必填</span>
              <span v-if="field.secret" class="secret-tag">机密</span>
            </label>

            <div class="schema-control-line" :class="{ 'is-contents': !field.open }">
              <SchemaFieldControl
                :field="field"
                :values="values"
                :providers="providers"
                :models="models"
                :model-note="modelNote"
                :secrets-set="secretsSet"
                :cleared="cleared"
                :id-prefix="idPrefix"
                @channel="onChannel"
              />
              <ActionButton
                v-if="field.open"
                variant="ghost"
                size="sm"
                @click="$emit('open-field', field.key)"
              >
                <FolderOpen :size="14" :stroke-width="1.8" />
                打开
              </ActionButton>
            </div>

            <!-- 已存的机密：空串在这套接口里表示"不改"，"真的抹掉"得单独给个去处。 -->
            <div v-if="field.secret && secretsSet.has(field.key)" class="secret-row">
              <span v-if="cleared.includes(field.key)" class="muted small">保存后这一项会被清空。</span>
              <span v-else class="muted small">已保存（留空即不改）。</span>
              <button class="btn mini" @click="$emit('toggle-clear', field.key)">
                {{ cleared.includes(field.key) ? '撤销' : '清除' }}
              </button>
            </div>

            <!-- 依赖别的格子才生效：置灰并说清条件，不把无效项画成"改了有用"。 -->
            <p v-if="!field.enabled" class="field-note">{{ field.disabledNote }}</p>
          </fieldset>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
// @ts-check
import { computed, onMounted, ref, watch } from 'vue'
import { FolderOpen } from 'lucide-vue-next'
import { api } from '../../api'
import ActionButton from './ActionButton.vue'
import SchemaFieldControl from './SchemaFieldControl.vue'
import SettingCard from '../../settings/ui/SettingCard.vue'
import SettingRow from '../../settings/ui/SettingRow.vue'

const props = defineProps({
  /** 插件声明的那份 ``settings_schema``（JSON Schema）。 */
  schema: { type: Object, default: () => ({}) },
  /** 表单值（就地改）；由调用方决定初值与保存时机。 */
  values: { type: Object, required: true },
  /** 已存机密的字段名集合。 */
  secretsSet: { type: Object, default: () => new Set() },
  /** 本次要清空的机密字段名。 */
  cleared: { type: Array, default: () => [] },
  /** 控件 id 前缀：同一页出现两张表单时 id 不能撞。 */
  idPrefix: { type: String, default: 'vh-plugin' },
  /** grid = 自带 PanelCard 的网格表单；rows = 设置页的一行一项。 */
  layout: { type: String, default: 'grid' },
  /** 归属方的名字（插件名之类）：并入行表搜索词，搜它也能落到位。 */
  keywords: { type: String, default: '' },
  /** 只画这几组；空 = 全画。生产方的设置页只留目录一组，其余参数在创作页卡片上。 */
  includeGroups: { type: Array, default: () => [] },
  /** 外层已经是弹窗：分组不画卡片壳，只留标题与行表。 */
  bare: { type: Boolean, default: false },
})

defineEmits(['toggle-clear', 'open-field'])

const requiredSet = computed(() => new Set(props.schema?.required || []))

const baseFields = computed(() =>
  Object.entries(props.schema?.properties || {}).map(([key, spec]) =>
    describeField(key, spec, requiredSet.value.has(key), props.values[key])
  )
)

/** 第二遍补「这一格现在生不生效」：依赖项与它的说法都在同一份 schema 里。 */
const fields = computed(() =>
  baseFields.value.map((field) => {
    const rule = field.enabledWhen
    if (!rule) return { ...field, enabled: true, disabledNote: '' }
    const wanted = Array.isArray(rule.equals) ? rule.equals : [rule.equals]
    const enabled = wanted.some(
      /** @param {any} value */
      (value) => String(props.values[rule.field] ?? '') === String(value ?? '')
    )
    const control = baseFields.value.find(/** @param {any} item */ (item) => item.key === rule.field)
    const labels = wanted.map(/** @param {any} value */ (value) => {
      const choice = control?.choices?.find(/** @param {any} pair */ (pair) => String(pair[0]) === String(value))
      if (choice?.[1]) return choice[1]
      if (control?.widget === 'switch') return String(value) === 'true' ? '开启' : '关闭'
      return String(value)
    })
    const note = enabled
      ? ''
      : `仅在「${control?.label || rule.field}」为「${labels.join(' / ')}」时生效。`
    return { ...field, enabled, disabledNote: note }
  })
)

/**
 * 按插件声明的 ``group`` 收成卡片；顺序照字段声明顺序，不另排。
 *
 * 弹窗里只有一个分组时不再写组名：抬头已经是插件名，多一行只是重复。
 */
/**
 * @param {any} group
 */
function groupTitle(group) {
  if (props.bare && groups.value.length === 1) return ''
  return group.title || '设置'
}

const groups = computed(() => {
  const wanted = new Set(props.includeGroups)
  /** @type {any[]} */
  const buckets = []
  for (const field of fields.value) {
    if (wanted.size && !wanted.has(field.group || '')) continue
    const name = field.group || ''
    let bucket = buckets.find(/** @param {any} item */ (item) => item.name === name)
    if (!bucket) {
      bucket = { name, title: name, fields: [] }
      buckets.push(bucket)
    }
    bucket.fields.push(field)
  }
  // 有分组时，没声明的那几格收进「其他」；全都没声明就保持一张平表。
  if (buckets.length > 1) {
    const loose = buckets.find(/** @param {any} item */ (item) => !item.name)
    if (loose) loose.title = '其他'
  }
  return buckets
})

/** 真正画出来的字段数：``includeGroups`` 过滤后可能一格都不剩。 */
const shownCount = computed(() => groups.value.reduce((sum, group) => sum + group.fields.length, 0))

// 通道 / 模型是动态候选：通道来自 app/llm 插件，模型再跟着选中的通道拉。
const providers = ref(/** @type {any[]} */ ([]))
const models = ref(/** @type {any[]} */ ([]))
const modelNote = ref('')
const channelField = computed(() =>
  fields.value.find((field) => field.widget === 'llm_provider')
)
const modelField = computed(() =>
  fields.value.find(
    /** @param {any} field */
    (field) => field.widget === 'llm_model'
  )
)
const channelValue = computed(() =>
  channelField.value ? props.values[channelField.value.key] : ''
)

async function loadProviders() {
  try {
    providers.value = await api.llmProviders()
  } catch {
    providers.value = []
  }
}

/**
 * @param {any} channel
 */
async function loadModels(channel) {
  models.value = []
  modelNote.value = ''
  if (!channel) return
  try {
    const result = await api.llmModels(channel)
    models.value = result.items || []
    if (!result.ok) modelNote.value = result.reason || '拉不到模型清单，用通道的默认模型。'
  } catch (error) {
    modelNote.value = String(error instanceof Error ? error.message : String(error))
  }
  // 拉不到清单（常见：还没填密钥）时退回通道设置里的模型候选，别让下拉空着。
  if (!models.value.length) {
    const provider = providers.value.find(/** @param {any} item */ (item) => item.key === channel)
    const choices = /** @type {any[]} */ (
      provider?.settings_schema?.properties?.model?.choices || []
    )
    models.value = choices.map(
      /** @param {any[]} pair */
      ([id, label]) => ({ id, label })
    )
  }
}

/**
 * @param {string} key
 * @param {any} value
 */
function onChannel(key, value) {
  props.values[key] = value
  const field = modelField.value
  if (field) props.values[field.key] = ''
}

onMounted(async () => {
  if (!channelField.value && !modelField.value) return
  await loadProviders()
  if (modelField.value && channelValue.value) void loadModels(channelValue.value)
})

watch(channelValue, (value, previous) => {
  if (value !== previous) void loadModels(value)
})

/**
 * 把 JSON Schema 的一格翻译成"怎么渲染"——**唯一的字段名判断是控件类型**。
 *
 * @param {string} key
 * @param {any} spec
 * @param {any} required
 * @param {any} current
 */
function describeField(key, spec, required, current) {
  const secret = Boolean(spec.secret)
  const choices = choicesOf(spec, current)
  let widget = spec.widget || ''
  if (!widget) {
    if (choices.length) widget = 'select'
    else if (secret) widget = 'password'
    else if (spec.type === 'boolean') widget = 'switch'
    else if (spec.type === 'integer' || spec.type === 'number') widget = 'number'
    else widget = 'text'
  }
  const type = spec.type === 'integer' ? 'number' : spec.type
  return {
    key,
    // 标签顺序：插件声明的说法 → pydantic 生成的标题 → 字段名本身（兜底）。
    label: spec.label || spec.title || key,
    description: spec.description || '',
    widget,
    secret,
    required,
    type,
    min: spec.minimum,
    max: spec.maximum,
    step: type === 'number' ? 'any' : undefined,
    choices,
    dependsOn: spec.depends_on || '',
    placeholder: spec.placeholder || '',
    open: spec.open === true,
    badge: spec.badge || '',
    enabledWhen: spec.enabled_when || null,
    group: spec.group || '',
    // 长文本与模型下拉独占一行：挤在半栏里读不全候选名。
    wide: widget === 'textarea' || widget === 'llm_model',
  }
}

/**
 * 行表搜索词：字段名、说明、候选项与归属方都算，搜「MP3」「码率」「插件名」都能落到位。
 *
 * @param {any} field
 */
function rowKeywords(field) {
  const choices = field.choices.map(/** @param {any} pair */ (pair) => pair[1]).join(' ')
  return [props.keywords, field.label, field.description, field.key, choices].join(' ')
}

/**
 * 卡片级搜索词：归属方、组名与该组各字段标签。
 *
 * @param {any} group
 */
function groupKeywords(group) {
  return [
    props.keywords,
    group.title,
    ...group.fields.map(
      /** @param {any} field */
      (field) => field.label
    ),
  ].join(' ')
}

/**
 * `choices` 优先，退回 `enum`。`current` 不在候选里时补一条：config.json 是人手
 * 也能改的文件，`<select>` 遇到值不在选项里会渲染成空白，像配置被清掉了。
 *
 * @param {any} spec
 * @param {any} current
 */
function choicesOf(spec, current) {
  let pairs = []
  if (Array.isArray(spec.choices) && spec.choices.length) {
    pairs = spec.choices.map(/** @param {any} pair */ (pair) => [pair[0], pair[1]])
  } else if (Array.isArray(spec.enum) && spec.enum.length) {
    pairs = spec.enum.map(/** @param {any} value */ (value) => [value, value === '' ? '（默认）' : String(value)])
  }
  if (!pairs.length || current === undefined || current === null || current === '') return pairs
  if (pairs.some(/** @param {any} pair */ (pair) => pair[0] === current)) return pairs
  return [...pairs, [current, `${current}（词表外）`]]
}

/**
 * @param {string} key
 */
function fieldId(key) {
  return `${props.idPrefix}-${key}`
}
</script>

<style scoped>
.schema-form {
  display: flex;
  flex-direction: column;
  gap: var(--sh-card-gap);
}
/* 引擎页的网格表单：宽度跟随所在页面列（引擎页整列已收在 980，内容 920），
   不再自带 860 上限——表单、页头链接与底栏按钮才对得上同一条右边界。 */
.schema-form.is-grid {
  gap: 26px;
}
/* 分组只靠标题分节：一行小标题 + 间距，不画容器。 */
.schema-group-title {
  margin: 0 0 13px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.01em;
  color: var(--sh-text);
}
.schema-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 18px;
}
.schema-grid.is-flat {
  grid-template-columns: minmax(0, 1fr);
  gap: 14px;
}
.schema-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  margin: 0;
  border: 0;
  padding: 0;
}
.schema-field.is-wide {
  grid-column: 1 / -1;
}
/* 开关：标签在左、开关在右——开关本身的形态就是状态，不再另写一行「已开启」。 */
.schema-field.is-switch {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
}
.schema-field.is-switch > label {
  margin: 0;
}
.schema-field.is-switch :deep(.sh-toggle) {
  grid-column: 2;
  grid-row: 1;
}
.schema-field[disabled] {
  opacity: 0.58;
}
.schema-field label {
  font-size: 11.8px;
  font-weight: 600;
  color: var(--sh-text-3);
  line-height: 1.5;
}

/* 设置页行表：只画卡片间距与文本控件列宽。行高、间距、控件宽度全部取设置页
   同一套令牌，密度改一次两边一起变。 */
.schema-form.is-rows {
  gap: var(--sh-card-gap);
}
.schema-form.is-rows :deep(.sh-row:not(.sh-row--stack) .sh-field) {
  width: var(--sh-control-w);
  max-width: 100%;
}
/* 上下布局的行（长文本）：控件铺满整行。 */
.schema-form.is-rows :deep(.sh-row--stack .schema-row-control) {
  width: 100%;
}
.schema-row-control {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  min-width: 0;
}
/* 字段声明 open 时的那一行：控件 + 「打开」；没声明时 display:contents，
   让包裹层对布局隐形，引擎页与对话模型页的格子一像素都不挪。 */
.schema-control-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.schema-control-line:not(.is-contents) > :first-child {
  flex: 1;
  min-width: 0;
}
/* 目录类字段（声明 open）跟着「存储位置」用等宽：一眼看得出这是路径不是句子。 */
.schema-control-line:not(.is-contents) :deep(.sh-field) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.schema-control-line.is-contents {
  display: contents;
}
.schema-form :deep(.sh-row.is-disabled) {
  opacity: 0.58;
}
.secret-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.field-note {
  margin: 2px 0 0;
  font-size: 11.2px;
  line-height: 1.55;
  color: var(--sh-text-4);
}
@media (max-width: 760px) {
  .schema-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
