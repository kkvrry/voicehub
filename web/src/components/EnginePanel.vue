<template>
  <!-- 引擎面板——点左栏「引擎」那一行进来的页面（不是弹窗）。
       这个组件不认识任何一台引擎：页签照 `page.sections` 画、表单照 `settings_schema`
       画、音色照通道画、能力与映射照 `describe()` 画。加引擎不用改这里。 -->
  <section v-if="engine" class="app-panel engine-panel">
    <header class="panel-head">
      <img class="engine-icon lg" :src="engine.icon_url" alt="" />
      <div class="panel-head-main">
        <div class="engine-title">
          {{ engine.label }}
          <span class="badge gray">{{ engine.kind_label }}</span>
          <span class="badge gray">v{{ engine.version }}</span>
          <span v-if="engine.ready" class="badge green">已就绪</span>
          <span v-else class="badge warn">未就绪</span>
        </div>
        <div class="muted">{{ engine.kind_note }}</div>
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
      <!-- 启停是一个开关，不是一个菜单项：它有两态、随时可逆，而且停用之后这一页
           就该换走——藏在「⋯」里既难找，也说不出"这是可逆的"。 -->
      <span class="engine-switch">
        <span class="engine-switch-text">启用</span>
        <ToggleSwitch
          :model-value="Boolean(engine.enabled)"
          :label="switchLabel"
          :disabled="toggling"
          @update:model-value="onEnabledChange"
        />
      </span>
    </header>

    <!-- 只差密钥时不摆这句话：「未就绪」徽标已经说过，面板里再写一遍是噪声。
         缺模型 / 缺依赖这类原因仍然要摆（那不是一眼能看出来的）。 -->
    <div v-if="!engine.ready && !engine.needs_key" class="hint" data-tone="warn">
      {{ engine.note }}
    </div>

    <!-- 页签由后端给：哪一台有哪些页，前端不做判断。 -->
    <div class="tabs">
      <button
        v-for="section in sections"
        :key="section.key"
        class="tab"
        :class="{ active: activeSection === section.key }"
        @click="store.engineSection = section.key"
      >
        {{ section.label }}
      </button>
    </div>

    <div class="panel-body">
      <!-- ------------------------------ 设置 ------------------------------ -->
      <div v-if="activeSection === 'settings'">
        <NoteText v-if="schemaNote" class="schema-note" :text="schemaNote" />
        <div v-if="!fieldCount" class="hint">这台引擎不需要配置。</div>

        <SchemaFields
          :schema="engine.settings_schema"
          :values="values"
          :secrets-set="secretsSet"
          :cleared="cleared"
          id-prefix="vh-engine"
          @toggle-clear="toggleClear"
        />
      </div>

      <!-- ------------------------------ 音色 ------------------------------ -->
      <div v-else-if="activeSection === 'voices'" class="panel-stack">
        <PanelCard title="音色">
          <div v-if="channel.mode === 'own'" class="hint">
            这台引擎的音色在<b>统一音色库</b>里：共 <b>{{ ownCount }}</b> 条，
            它生成 / 克隆出来的音频就是那条音色自己的样例。
          </div>
          <div v-else-if="channel.mode === 'library'" class="hint">
            这台引擎没有自己的音色清单：合成时用的是<b>音色库的全部音色</b>（共
            <b>{{ libraryCount }}</b> 条），参考音频与文本取自所选音色。
          </div>
          <div v-else class="hint">这台引擎不供给音色。</div>

          <div class="row-actions">
            <ActionButton
              v-if="engine.capabilities.voice_listing"
              size="sm"
              @click="refresh"
            >
              拉取音色清单
            </ActionButton>
            <ActionButton size="sm" @click="openVoicesInLibrary">在全部音色中查看</ActionButton>
            <ActionButton
              v-if="ownCount"
              size="sm"
              variant="danger"
              :loading="clearing"
              @click="clearVoices"
            >
              清除音色（{{ ownCount }}）
            </ActionButton>
          </div>
        </PanelCard>

        <!-- 保存目录：只对**有平台音色**的引擎有意义（本地音色的样例跟着它自己的目录走）。 -->
        <PanelCard
          v-if="engine.capabilities.voice_listing"
          title="平台音色的样例保存目录"
        >
          <div class="folder-line">
            <input
              v-model="outputDraft"
              class="sh-field"
              :placeholder="engine.output?.default || '留空 = 默认位置'"
              spellcheck="false"
              aria-label="样例保存目录"
            />
            <ActionButton size="sm" :disabled="outputSaving" @click="saveOutput">
              {{ outputSaving ? '保存中……' : '保存' }}
            </ActionButton>
          </div>
          <div class="hint">
            生成 / 克隆的音频按 <b>音色名 / 文件</b> 存在这里，<b>留空 = 设置页「样例保存目录」
            下按引擎分一个子目录</b>。生效中：<span class="mono">{{ engine.output?.dir }}</span>
          </div>
        </PanelCard>
      </div>

      <!-- ------------------------------ 克隆 ------------------------------ -->
      <div v-else-if="activeSection === 'clone'" class="panel-stack">
        <PanelCard title="克隆音色">
          <div class="hint">
            从音色库里选一条已有样例当音源，换回一个可长期使用的平台音色。
            成功后会用下面的试听文本生成一条样例，存进这条音色的样例目录。
          </div>
          <div v-if="cloneModel" class="hint">
            使用模型：<span class="mono">{{ cloneModel }}</span>
          </div>
          <div class="clone-grid">
        <div class="field">
          <label for="vh-clone-source">音源音色 <span class="req-tag">必填</span></label>
          <select id="vh-clone-source" v-model="clone.source" @change="pickSource">
            <option value="">选一条有样例的音色</option>
            <option v-for="item in cloneSources" :key="item.id" :value="item.id">
              {{ item.shown_name }}（{{ item.sample_count }} 条样例）
            </option>
          </select>
          <div class="muted small">{{ clone.sourceLabel || '音源取自这条音色的主样例' }}</div>
        </div>
        <div class="field">
          <label for="vh-clone-name">音色名 <span class="req-tag">必填</span></label>
          <input id="vh-clone-name" v-model="clone.name" placeholder="留空 = 音源名 + 克隆" />
        </div>
        <div class="field">
          <label for="vh-clone-transcript">音源念的内容</label>
          <textarea
            id="vh-clone-transcript"
            v-model="clone.transcript"
            placeholder="选音源后自动带出；没有就点下面的「从音源识别」"
          ></textarea>
          <div v-if="store.asr.ready && clone.sourceFile" class="row-actions">
            <button class="btn mini" :disabled="clone.recognizing" @click="recognizeSource">
              {{ clone.recognizing ? '识别中……' : '从音源识别' }}
            </button>
          </div>
        </div>
        <div class="field is-wide">
          <label for="vh-clone-preview">试听文本</label>
          <textarea
            id="vh-clone-preview"
            v-model="clone.preview_text"
            placeholder="克隆成功后用它生成一条样例；留空就不生成"
          ></textarea>
        </div>
          </div>
        </PanelCard>
      </div>

      <!-- ------------------------------ 音色设计 ------------------------------ -->
      <div v-else-if="activeSection === 'design'" class="panel-stack">
        <PanelCard title="音色设计">
          <div class="hint">创建可反复指定的音色。描述每次随合成请求发送，平台不保存音色。</div>
          <div v-if="designModel" class="hint">
            使用模型：<span class="mono">{{ designModel }}</span>
          </div>
          <div class="clone-grid">
            <div class="field">
              <label for="vh-design-name">音色名 <span class="req-tag">必填</span></label>
              <input id="vh-design-name" v-model="design.name" placeholder="如：老者旁白" />
            </div>
            <div class="field is-wide">
              <label for="vh-design-prompt">音色描述 <span class="req-tag">必填</span></label>
              <textarea
                id="vh-design-prompt"
                v-model="design.prompt"
                placeholder="描述声线、年龄感与说话方式"
              ></textarea>
            </div>
          </div>
        </PanelCard>
      </div>

      <!-- --------------------------- 能力与参数 --------------------------- -->
      <div v-else class="panel-stack">
        <PanelCard v-if="capNotes.length" title="能力">
          <div v-for="note in capNotes" :key="note.key" class="cap-line">
            <span class="cap-label" :class="'tone-' + capabilityTone(note.key)">{{ note.label }}</span>
            <span class="cap-note">{{ note.note }}</span>
          </div>
        </PanelCard>

        <!-- 支持语言：语言与方言各一行，只摆名字，不写解释。
             方言里已经有的词（粤语）不再在语言那一行重复一次。 -->
        <PanelCard
          v-if="languageItems.length || languageDialects.length"
          title="支持语言"
          :desc="languageNote"
        >
          <div v-if="languageItems.length" class="cap-line">
            <span class="cap-label">语言</span>
            <span class="card-tags">
              <span
                v-for="item in languageItems"
                :key="item.code"
                class="card-tag"
                :class="'tone-' + languageTone(item.code)"
              >{{ item.name }}</span>
            </span>
          </div>
          <div v-if="languageDialects.length" class="cap-line">
            <span class="cap-label">方言</span>
            <span class="card-tags">
              <span
                v-for="name in languageDialects"
                :key="name"
                class="card-tag"
                :class="'tone-' + toneOf(name)"
              >{{ name }}</span>
            </span>
          </div>
        </PanelCard>

        <PanelCard v-if="mappingRows.length" title="参数映射">
          <table class="map-table param-map">
            <thead>
              <tr>
                <th>项目规范</th>
                <th>映射位置</th>
                <th>换算</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in mappingRows"
                :key="row.source"
                :class="{ 'is-unmapped': !row.target }"
              >
                <td>
                  <span class="map-title">
                    <span class="map-name">{{ row.label }}</span>
                    <span v-if="row.required" class="req-tag">必填</span>
                  </span>
                  <span v-if="row.label !== row.source" class="map-key mono">{{ row.source }}</span>
                </td>
                <td class="mono">{{ row.target || '—' }}</td>
                <td class="muted">{{ row.transform_label || '—' }}</td>
                <td class="map-note" :class="{ 'is-warning': row.warning }">
                  <TriangleAlert v-if="row.warning" :size="12" :stroke-width="1.8" />
                  <span>{{ row.note || '—' }}</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="constants.length" class="map-constants">
            固定参数：<span class="mono">{{ constantsText }}</span>
          </div>
          <ul v-if="cautions.length" class="map-cautions">
            <li v-for="item in cautions" :key="item">
              <TriangleAlert :size="12" :stroke-width="1.8" />
              <span>{{ item }}</span>
            </li>
          </ul>
        </PanelCard>

        <!-- 引擎对参考音频的要求：由插件在 ``synthesis_spec()`` 里声明，核心照声明
             自动转码 / 补逐字稿。这里只把声明画成关键词标签，不写解释长文。 -->
        <PanelCard title="引擎要求">
          <div class="req-tags">
            <span
              v-for="tag in requirementTags"
              :key="tag.key"
              class="req-tag-item"
              :class="tag.tone"
            >
              <span class="req-tag-name">{{ tag.label }}</span>
              <span class="req-tag-value">{{ tag.value }}</span>
            </span>
          </div>
          <p v-if="autoNote" class="req-auto">{{ autoNote }}</p>
        </PanelCard>

        <!-- 语气词标签表：界面上明摆着"这台认哪几个、怎么写"，
             因为那正是"插进正文的 [[笑]] 会不会被念出来"的全部依据。
             「来源」一列跨引擎比出来：通用 = 支持标签的那几台都认。 -->
        <PanelCard v-if="markRows.length" title="语气词标签">
          <table class="map-table tag-map">
            <thead>
              <tr>
                <th>规范写法</th>
                <th>引擎写法</th>
                <th>来源</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in markRows" :key="row.group + ':' + row.name">
                <td class="mono">[[{{ row.name }}]]</td>
                <td class="mono">{{ row.native }}</td>
                <td class="muted">{{ row.origin }}</td>
              </tr>
            </tbody>
          </table>
          <p class="map-footnote">未登记的标签会在发送前从正文中移除。</p>
        </PanelCard>
      </div>
    </div>

    <!-- 底栏常驻：跟着内容滚出去的话，「保存」就成了藏在页面最下面的按钮。 -->
    <div v-if="['settings', 'clone', 'design'].includes(activeSection)" class="panel-foot">
      <span class="muted small">{{ footNote }}</span>
      <ActionButton v-if="activeSection === 'settings'" :disabled="saving" @click="resetDefaults">
        恢复默认
      </ActionButton>
      <ActionButton
        v-if="activeSection === 'settings'"
        variant="primary"
        :loading="saving"
        :disabled="!canSave"
        @click="save"
      >
        保存
      </ActionButton>
      <ActionButton
        v-else-if="activeSection === 'clone'"
        variant="primary"
        :loading="cloning"
        :disabled="!canCloneSubmit"
        @click="submitClone"
      >
        {{ cloning ? '克隆中（可能要一会儿）……' : '开始克隆' }}
      </ActionButton>
      <ActionButton
        v-else
        variant="primary"
        :loading="designing"
        :disabled="!canDesignSubmit"
        @click="submitDesign"
      >
        {{ designing ? '创建中……' : '创建音色' }}
      </ActionButton>
    </div>
  </section>

  <!-- 取不到那台引擎时说什么（否则就是一片空白，点错行的人只会以为页面坏了）：
       三种情形——一台都没添加（有适配清单）、真的没有适配的插件、`engineKey` 已不存在。
       居中一张虚线的"空槽"比抬头 + 右上按钮更说明问题：这一页现在**没有内容**，
       而唯一能做的动作就在这张卡片里。 -->
  <section v-else class="app-panel engine-panel">
    <div class="engine-empty">
      <div class="engine-empty-card">
        <span class="engine-empty-mark" aria-hidden="true">
          <Cpu :size="20" :stroke-width="1.8" />
        </span>
        <h2 class="engine-empty-title">{{ emptyTitle }}</h2>
        <p class="engine-empty-note">{{ emptyNote }}</p>
        <ActionButton v-if="canAdd" variant="primary" @click="actions.openEngineCatalog()">
          引擎清单
        </ActionButton>
      </div>
    </div>
  </section>
</template>

<script setup>
// @ts-check
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { store, actions, labelOf, say, vocab } from '../store'
import { Cpu, TriangleAlert } from 'lucide-vue-next'
import { api } from '../api'
import { formatBytes } from '../utils/format'
import NoteText from './NoteText.vue'
import ActionButton from './ui/ActionButton.vue'
import PanelCard from './ui/PanelCard.vue'
import SchemaFields from './ui/SchemaFields.vue'
import ToggleSwitch from './ui/ToggleSwitch.vue'
import { capabilityTone, languageTone, toneOf } from './ui/tones'

const engine = computed(() => actions.engineOf(store.engineKey))

// ---- 启停：面板里的这个开关只有"关掉"一路（开着的才看得到这一页）----
const toggling = ref(false)
const switchLabel = computed(
  () => `${engine.value?.enabled ? '停用' : '启用'}「${engine.value?.label || ''}」`
)

/**
 * @param {any} value
 */
async function onEnabledChange(value) {
  if (value === Boolean(engine.value?.enabled)) return
  if (value) return // 这一页只对已启用的引擎画；"开"没有可做的事
  const target = engine.value
  if (!target) return
  toggling.value = true
  try {
    // 停用后的落点（换到下一台）在 store 里定：清单里关开关也走同一条路。
    await actions.disableEngine(target.key)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    toggling.value = false
  }
}

/** 空态三句：有适配清单但一台没添加 / 一台适配都没有 / 当前这台已不在清单里。 */
const canAdd = computed(() => !store.engines.length && store.engineCatalog.length > 0)
const emptyTitle = computed(() => {
  if (store.engines.length) return '引擎不存在'
  return store.engineCatalog.length ? '尚未启用引擎' : '没有适配的引擎'
})
const emptyNote = computed(() => {
  if (store.engines.length) {
    return '这台已不在工作清单里（可能刚被停用，或插件已卸载）。它留下的音色仍在音色库里。'
  }
  return store.engineCatalog.length
    ? '从引擎清单里启用一台。启用的才会出现在音色来源、生成页与生产方候选里。'
    : '在 app/engines/builtin/ 下建一个目录（与对话模型同一种装法），重启即出现。'
})

onMounted(() => {
  if (!store.engineCatalog.length) void actions.loadEngineCatalog().catch(() => {})
})

/** 页签由后端给：前端不认识任何一台引擎，只有一个取不到就退回设置的兜底。 */
const sections = computed(
  () =>
    engine.value?.page?.sections || [
      { key: 'settings', label: '设置', note: '' },
      { key: 'voices', label: '音色', note: '' },
      { key: 'about', label: '能力与参数', note: '' },
    ]
)

/** 当前页签：**换引擎之后上一个页签可能不存在**（那台没有克隆），退回第一块。 */
const activeSection = computed(() =>
  sections.value.some(/** @param {any} item */ (item) => item.key === store.engineSection)
    ? store.engineSection
    : sections.value[0]?.key || 'settings'
)

/** 克隆页与设计页使用的模型：由插件在 ``page.sections[].meta`` 里声明，前端不写死。 */
const cloneModel = computed(
  () => sections.value.find(/** @param {any} item */ (item) => item.key === 'clone')?.meta?.model || ''
)
const designModel = computed(
  () => sections.value.find(/** @param {any} item */ (item) => item.key === 'design')?.meta?.model || ''
)

// ---- 设置表单 ----
const values = ref(/** @type {Record<string, any>} */ ({}))
const cleared = ref(/** @type {string[]} */ ([]))
const saving = ref(false)

const secretsSet = computed(() => new Set(engine.value?.secrets_set || []))
const schemaNote = computed(() => engine.value?.settings_schema?.description || '')
const capNotes = computed(() => engine.value?.capability_notes || [])
const mappingRows = computed(() => engine.value?.mapping || [])

/** 「支持语言」那一栏：语言与方言分开摆，名字后面不跟解释。
 *  两处同名的（粤语既是语言代码又是方言）由后端去过重，前端不再判一次。 */
const languageInfo = computed(() => engine.value?.languages || {})
const languageItems = computed(() => languageInfo.value.items || [])
const languageDialects = computed(() => languageInfo.value.dialects || [])
const languageNote = computed(() => languageInfo.value.note || '')

/**
 * 参考音频那一侧的两条要求（引擎自己声明）。声明了只读某格式，中转站就会转好
 * 再发；声明了要逐字稿，就会先听一遍——所以要把"这是自动做的"讲清楚。
 *
 * 引擎对参考音频的要求：插件在 ``synthesis_spec()`` 里声明，核心照声明办事
 * （容器不符就转码、缺逐字稿就识别）。这里只把声明画成关键词，不写解释长文。
 */
const refSpec = computed(() => engine.value?.synthesis_spec || {})
const requirementTags = computed(() => {
  const spec = refSpec.value
  const requires = spec.requires || []
  const tags = [
    {
      key: 'reference',
      label: '参考音频',
      value: requires.includes('reference_audio')
        ? '必填'
        : (spec.reference_formats || []).length || spec.needs_transcript
          ? '可选'
          : '不需要',
      tone: requires.includes('reference_audio') ? 'is-required' : '',
    },
  ]
  if ((spec.reference_formats || []).length) {
    tags.push({
      key: 'formats',
      label: '参考音频格式',
      value: (spec.reference_formats || []).map(/** @param {any} item */ (item) => item.toUpperCase()).join(' / '),
      tone: '',
    })
  }
  if ((spec.reference_duration || []).length === 2) {
    const [low, high] = spec.reference_duration
    tags.push({
      key: 'duration',
      label: '参考音频时长',
      value: `${low}–${high} 秒`,
      tone: 'is-required',
    })
  }
  tags.push({
    key: 'transcript',
    label: '逐字稿',
    value: spec.needs_transcript ? '必需' : '不需要',
    tone: spec.needs_transcript ? 'is-required' : '',
  })
  // 文本上限是**限制**，不是能力：放在「引擎要求」里，与其他硬约束并排。
  const maxChars = Number(engine.value?.capabilities?.max_chars || 0)
  if (maxChars) {
    tags.push({
      key: 'max_chars',
      label: '文本上限',
      value: `单次 ${maxChars} 字`,
      tone: 'is-required',
    })
  }
  tags.push({
    key: 'output',
    label: '输出格式',
    value: (spec.formats || []).map(/** @param {any} item */ (item) => item.toUpperCase()).join(' / ') || '—',
    tone: '',
  })
  return tags
})
const autoNote = computed(() => {
  const steps = []
  if ((refSpec.value.reference_formats || []).length) steps.push('容器不符时自动转码')
  if ((refSpec.value.reference_duration || []).length === 2) steps.push('超长时自动截取')
  if (refSpec.value.needs_transcript) steps.push('缺逐字稿时自动识别')
  return steps.length ? `以上要求由核心在发送前自动处理：${steps.join('、')}。` : ''
})
const cautions = computed(() => engine.value?.mapping_cautions || [])
const constants = computed(() => Object.entries(engine.value?.mapping_constants || {}))

/** 语气词标签那张表：通用的在前、这家独有的在后，并注明来路。
 *  「来路」是跨引擎比出来的（后端在 `/api/v1/engines` 里给每条打了 `common` 标记），
 *  排序在后端就排好了，这里只照抄。 */
const markRows = computed(() =>
  (engine.value?.marks || []).map(/** @param {any} mark */ (mark) => ({
    group: mark.common ? 'common' : 'own',
    name: mark.name,
    native: mark.native,
    origin: mark.common ? '通用' : '本引擎独有',
  }))
)
const constantsText = computed(() =>
  constants.value.map(([key, value]) => `${key} = ${JSON.stringify(value)}`).join('、')
)

/** 底栏左边那句话：当前这一块在做什么，按钮哪颗在生效不用猜。 */
const footNote = computed(() => {
  if (activeSection.value === 'clone') {
    return '这一步会真的调引擎——要不要计费，看上方那行形态说明。'
  }
  if (activeSection.value === 'design') return '描述随音色入库；每次合成按它重新设计音色。'
  return '恢复默认只回落普通配置，密钥保留（要清密钥用上面那颗「清除」）。'
})

/** 表单有几格——不画控件，只用来判断"这台引擎是不是不用配置"。 */
const fieldCount = computed(
  () => Object.keys(engine.value?.settings_schema?.properties || {}).length
)

/**
 * @param {string} key
 */
function toggleClear(key) {
  const index = cleared.value.indexOf(key)
  if (index >= 0) cleared.value.splice(index, 1)
  else {
    cleared.value.push(key)
    values.value[key] = ''
  }
}

/**
 * 表单初值 = schema 的默认值，再被服务器上真正存着的值覆盖（机密是空的）。
 *
 * @param {any} payload
 */
function resetFrom(payload) {
  if (!payload) return
  const next = /** @type {Record<string, any>} */ ({})
  for (const [key, spec] of Object.entries(
    /** @type {Record<string, any>} */ (payload.settings_schema?.properties || {})
  )) {
    next[key] = spec.default ?? ''
  }
  Object.assign(next, payload.values || {})
  values.value = next
  cleared.value = []
}

/** 必填项为空就不让按保存，省一次"点了才知道少东西"的往返。 */
const canSave = computed(() => {
  const schema = engine.value?.settings_schema || {}
  const required = Array.isArray(schema.required) ? schema.required : []
  return required.every(/** @param {any} key */ (key) => {
    const value = values.value[key]
    if (schema.properties?.[key]?.secret) {
      return secretsSet.value.has(key) || Boolean(String(value || '').trim())
    }
    return value !== '' && value !== null && value !== undefined
  })
})

async function save() {
  saving.value = true
  try {
    const body = /** @type {Record<string, any>} */ ({ ...values.value })
    if (cleared.value.length) body._clear = [...cleared.value]
    const saved = await actions.saveEngineSettings(engine.value.key, body)
    resetFrom(saved) // 服务器回来的那份才是准的（机密重新变回空串）
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    saving.value = false
  }
}

/** 恢复默认：**后端落**（非机密回落、机密保留），回来的那一份重画表单。 */
async function resetDefaults() {
  saving.value = true
  try {
    const saved = await actions.resetEngineSettings(engine.value.key)
    resetFrom(saved)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    saving.value = false
  }
}

// ---- 通道（只读：由插件能力推导，不再让用户在这里改）----
const channel = computed(
  () =>
    engine.value?.channel || {
      mode: '',
      mode_label: '',
      mode_note: '',
      allowed_modes: [],
      default_mode: '',
    }
)

// ---- 音色（计数、拉清单、跳转）----
const engineRow = computed(() => store.tree.engines.find(/** @param {any} row */ (row) => row.key === engine.value?.key))
const ownCount = computed(() => engineRow.value?.total || 0)
/** 库桥那几家能用的音色 = 音色库里的全部本地音色（左栏各个根的合计）。 */
const libraryCount = computed(() => store.tree.roots.reduce((sum, root) => sum + root.total, 0))

async function refresh() {
  try {
    await actions.refreshEngine(engine.value.key)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  }
}

/** 「在全部音色中查看」：回音色库并按这台引擎筛一次；标题与左栏高亮说状态。 */
async function openVoicesInLibrary() {
  store.selection = { kind: 'platform', name: engine.value.key }
  await actions.openLibrary()
  await actions.loadVoices()
}

// ---- 清除这台引擎的音色条目 ----
const clearing = ref(false)

/** 只删库：平台账号的数据不变，已生成的样例在保存目录里，由用户自行处理。 */
async function clearVoices() {
  const value = engine.value
  if (!value || !ownCount.value) return
  const ok = window.confirm(
    `清除「${value.label}」在库里的 ${ownCount.value} 个音色条目？\n\n` +
      '只删库内条目；平台侧数据不变。已生成的样例仍在音色保存目录里，需要时手工删除。'
  )
  if (!ok) return
  clearing.value = true
  try {
    await actions.clearEngineVoices(value.key)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    clearing.value = false
  }
}

// ---- 保存目录 ----
const outputDraft = ref('')
const outputSaving = ref(false)

function resetOutputDraft() {
  const output = engine.value?.output || {}
  outputDraft.value = output.custom ? output.dir : ''
}

async function saveOutput() {
  outputSaving.value = true
  try {
    const wanted = outputDraft.value.trim()
    const output = engine.value?.output || {}
    const current = output.custom ? output.dir : ''
    if (wanted === current) {
      say('保存目录未变更')
      return
    }
    // 改目录前先问一句：旧目录里可能有这台引擎生成过的东西，不搬的话它们就从库里消失。
    let migrate = false
    try {
      const preview = await actions.previewEngineOutput(engine.value.key, wanted)
      if (preview.needed) migrate = confirmMigrate(preview)
    } catch (error) {
      // 预览失败（旧目录读不动之类）不该挡住保存——照不搬处理，用户仍能改配置。
      say(`无法检查旧目录：${error instanceof Error ? error.message : String(error)}`)
    }
    const saved = await actions.saveEngineOutput(engine.value.key, wanted, migrate)
    resetOutputDraft()
    reportMove(saved)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    outputSaving.value = false
  }
}

/**
 * 问用户旧目录里的东西要不要一起搬。默认不搬——真要搬是大批量文件操作。
 *
 * @param {any} preview
 */
function confirmMigrate(preview) {
  const extra = preview.reason ? `\n${preview.reason}` : ''
  return window.confirm(
    `音色保存目录要改到：\n${preview.target}\n\n` +
      `旧目录里还有 ${preview.entries} 项（${preview.audio} 段音频，${formatBytes(preview.bytes)}）：\n${preview.source}\n\n` +
      `要把它们一起搬过去吗？${extra}\n\n` +
      `选「取消」= 只改设置（旧文件留在原地，库里会看不到它们）。`
  )
}

/**
 * 搬完回一句实话：搬了几个、哪些没搬成（报具体路径，比"迁移失败"有用）。
 *
 * @param {any} saved
 */
function reportMove(saved) {
  const moved = saved?.moved
  if (!moved) return
  const parts = [`已搬 ${moved.moved} 项`]
  if (moved.renamed?.length) parts.push(`重名保留 ${moved.renamed.length} 项`)
  if (moved.failed?.length) {
    parts.push(
      `没搬成 ${moved.failed.length} 项：${moved.failed
        .map(
          /** @param {any[]} pair */
          (pair) => pair[0]
        )
        .join('、')}`
    )
  }
  say(parts.join('；'))
}

// ---- 克隆 ----
const clone = reactive({
  source: '',
  sourceFile: '',
  sourceLabel: '',
  name: '',
  transcript: '',
  preview_text: '',
  recognizing: false,
})
const cloneSources = ref(/** @type {any[]} */ ([]))
const cloning = ref(false)

const canCloneSubmit = computed(() => Boolean(clone.sourceFile) && Boolean(clone.name.trim()))

/** 可当音源的音色：库里所有**有样例**的（本地 + 平台都行）。 */
async function loadCloneSources() {
  try {
    const list = await api.voices({})
    cloneSources.value = (list || []).filter(/** @param {any} item */ (item) => item.sample_count > 0)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  }
}

function resetCloneForm() {
  Object.assign(clone, {
    source: '',
    sourceFile: '',
    sourceLabel: '',
    name: '',
    transcript: '',
    preview_text: '',
    recognizing: false,
  })
}

/** 选中音源：取它的主样例，带出正文；没有正文且识别可用时顺手识别。 */
async function pickSource() {
  clone.sourceFile = ''
  clone.sourceLabel = ''
  clone.transcript = ''
  if (!clone.source) return
  try {
    const detail = await api.voice(clone.source)
    const samples = detail.samples || []
    const sample = samples.find(/** @param {any} item */ (item) => item.is_main) || samples[0]
    if (!sample) {
      say('这条音色没有样例，换一条。')
      return
    }
    clone.sourceFile = sample.file
    clone.sourceLabel = `${detail.shown_name} / ${sample.file}`
    clone.transcript = sample.text || ''
    if (!clone.name.trim()) clone.name = `${detail.shown_name} 克隆`
    // 试听文本用文案库里第一句（与生成样例同一份）：它是挑来听音色的，
    // 不是音源说过的话——拿音源原话去复刻，生成的样例只会重复一遍原音。
    await actions.ensureTexts()
    if (!clone.preview_text.trim()) clone.preview_text = actions.firstText()
    if (!clone.transcript && store.asr.ready) await recognizeSource()
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  }
}

async function recognizeSource() {
  if (!clone.source || !clone.sourceFile) return
  clone.recognizing = true
  try {
    const result = await api.transcribe(clone.source, clone.sourceFile)
    clone.transcript = result.text || ''
    if (!clone.preview_text.trim() && clone.transcript) clone.preview_text = clone.transcript
    if (result.language_written) {
      await actions.reloadVoice(clone.source).catch(() => {})
      const label =
        labelOf(vocab.languages, result.language_code) || result.language || result.language_code
      say(`识别完成 · 语种已写入音源音色：${label}`, { kind: 'success' })
    }
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    clone.recognizing = false
  }
}

async function submitClone() {
  cloning.value = true
  try {
    const sourceName = cloneSources.value.find(/** @param {any} item */ (item) => item.id === clone.source)?.shown_name || ''
    const meta = await actions.cloneVoice(engine.value.key, {
      name: clone.name.trim() || `${sourceName || '未命名'} 克隆`,
      transcript: clone.transcript.trim(),
      source_voice_id: clone.source,
      source_file: clone.sourceFile,
    })
    const preview = clone.preview_text.trim()
    if (preview) {
      try {
        // 与卡片上的「生成样例」同一条路：落进这条音色的样例目录并挂回它自己。
        await api.synthesize({
          engine: engine.value.key,
          voice_id: meta.voice_id || meta.id,
          text: preview,
          persist: 'sample',
        })
        say(`已克隆「${meta.name}」，样例已生成`)
      } catch (error) {
        say(`已克隆「${meta.name}」，但试听样例没生成：${error instanceof Error ? error.message : String(error)}`)
      }
    }
    resetCloneForm()
    await actions.refresh()
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    cloning.value = false
  }
}

// ---- 音色设计 ----
const design = reactive({ name: '', prompt: '' })
const designing = ref(false)
const canDesignSubmit = computed(
  () => Boolean(design.name.trim()) && Boolean(design.prompt.trim())
)

/** 不上传音源：描述交给引擎，凭据由后端写进库内音色的 payload。 */
async function submitDesign() {
  designing.value = true
  try {
    await actions.cloneVoice(engine.value.key, {
      name: design.name.trim(),
      design_prompt: design.prompt.trim(),
    })
    Object.assign(design, { name: '', prompt: '' })
    await actions.refresh()
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  } finally {
    designing.value = false
  }
}

/** 换一台引擎就重画：表单、保存目录、克隆与设计表单都属于上一台。 */
watch(
  () => engine.value?.key,
  () => {
    if (!engine.value) return
    resetFrom(engine.value)
    resetOutputDraft()
    resetCloneForm()
    Object.assign(design, { name: '', prompt: '' })
    loadCloneSources()
    if (
      !sections.value.some(
        /** @param {any} s */
        (s) => s.key === activeSection.value
      )
    ) {
      store.engineSection = sections.value[0]?.key || 'settings'
    }
  },
  { immediate: true }
)

// 保存成功后 `store.engines` 换新：机密变回空串、保存目录也是新值——
// 但表单里的用户输入不能被冲掉，只在"当前没有未保存改动"时才跟着后端走。
watch(
  () => engine.value?.values,
  () => {
    if (!saving.value && !outputSaving.value && engine.value) {
      resetFrom(engine.value)
      resetOutputDraft()
    }
  }
)
</script>

<style scoped>
/* 空态：一张居中的虚线空槽。这一页没有引擎时要回答的是"现在能做什么"，
   而不是把一句说明摆在抬头、把按钮推到远端。 */
.engine-empty {
  display: grid;
  flex: 1;
  place-items: center;
  padding: 40px 22px;
}
.engine-empty-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  width: min(400px, 100%);
  padding: 30px 28px 26px;
  border: 1px dashed var(--sh-line);
  border-radius: var(--sh-radius-card);
  text-align: center;
}
.engine-empty-mark {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  margin-bottom: 4px;
  border-radius: 12px;
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}
.engine-empty-title {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  color: var(--sh-text);
}
.engine-empty-note {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--sh-text-3);
}

/* 抬头右端的启停开关：文字是给"这个开关管什么"的一句话，开关本体仍由控件自绘。
   与「官方文档」同高，两者都是这一页的操作，不再有第三颗「⋯」。 */
.engine-switch {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 7px;
}
.engine-switch-text {
  font-size: 12px;
  color: var(--sh-text-3);
}

/* 克隆表单：两列字段，长文本独占一行 */
.clone-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 18px;
  margin-top: 14px;
}
/* 网格内间距由 gap 统一控制：抵消 `.app-panel .field + .field` 的 12px 上间距，
   否则第二列整体下沉，与首列控件对不齐。 */
.clone-grid .field,
.clone-grid .field + .field {
  margin: 0;
}
.clone-grid .field.is-wide {
  grid-column: 1 / -1;
}
.clone-grid .field label {
  font-size: 11.8px;
  font-weight: 600;
  color: var(--sh-text-3);
}
.clone-grid .field input,
.clone-grid .field select,
.clone-grid .field textarea {
  border-color: var(--sh-line);
  background: var(--sh-field);
}
/* 卡片里的表格：贴边（卡片用 flush），行高比设置页略松一点，方便逐行核对。 */
/* 卡片正文自带 20px 内边距：表格贴在这层内边距里，不再单独补边 */
.panel-stack :deep(.sh-card-body) table {
  width: 100%;
}
.panel-stack :deep(.map-table th) {
  padding: 6px 6px 8px;
}
.panel-stack :deep(.map-table td) {
  padding: 6px;
}
@media (max-width: 760px) {
  .clone-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
