<!-- 模型：清单来自上游 model_specs，安装/停止/删除全部转发实例的官方管理接口。 -->
<template>
  <PanelCard title="模型">
    <template #head>
      <span class="sh-chip is-quiet">{{ data.catalog.count }} 个模型</span>
      <span v-if="data.catalog.refreshed" class="sh-chip is-ok">已刷新</span>
      <ActionButton size="sm" variant="ghost" :loading="state.busy === 'catalog'" @click="refreshCatalog">
        刷新清单
      </ActionButton>
    </template>

    <div class="acp-filters">
      <div class="acp-cats" role="tablist" aria-label="模型分类">
        <button
          type="button"
          class="acp-cat"
          :class="{ 'is-on': state.category === 'all' }"
          role="tab"
          :aria-selected="state.category === 'all'"
          @click="state.category = 'all'"
        >全部 {{ data.models.length }}</button>
        <button
          v-for="row in data.categories"
          :key="row.key"
          type="button"
          class="acp-cat"
          :class="{ 'is-on': state.category === row.key }"
          role="tab"
          :aria-selected="state.category === row.key"
          @click="state.category = row.key"
        >{{ row.label }} {{ row.count }}</button>
      </div>
      <input
        class="sh-field acp-search"
        :value="state.keyword"
        placeholder="搜索名称、说明或族名"
        spellcheck="false"
        aria-label="搜索模型"
        @input="onKeyword"
      />
      <select
        class="sh-field acp-mirror"
        :value="data.mirror"
        aria-label="下载镜像"
        :disabled="state.busy === 'mirror'"
        @change="onMirror"
      >
        <option
          v-for="row in data.mirrors"
          :key="row.id"
          :value="row.id"
          :disabled="row.available === false"
        >{{ row.label }}</option>
      </select>
    </div>

    <p v-if="mirrorNote" class="acp-banner" :class="{ 'is-warn': !data.runtime.model_manager }">
      {{ mirrorNote }}
    </p>
    <p v-if="blockedNote" class="acp-banner">{{ blockedNote }}</p>
    <p v-if="data.catalog.note" class="acp-banner is-warn">{{ data.catalog.note }}</p>

    <div class="acp-list">
      <article v-for="item in visible" :key="item.family" class="acp-model">
        <div class="acp-model-main">
          <div class="acp-model-top">
            <span class="acp-model-name">{{ item.name }}</span>
            <span class="sh-chip is-quiet">{{ item.family }}</span>
            <span class="sh-chip">{{ item.category_label }}</span>
            <span v-if="item.status !== 'supported'" class="sh-chip is-warn">{{ item.status_label }}</span>
            <span v-if="loadedModel(item)" class="sh-chip is-ok">已加载</span>
            <span v-if="item.installed" class="sh-chip is-ok">已安装</span>
          </div>
          <p class="acp-model-desc" :class="{ 'is-open': expanded.has(item.family) }">{{ item.description }}</p>
          <div class="acp-model-meta">
            <span v-if="item.languages.length">{{ item.languages.length }} 种语言</span>
            <span v-if="item.modes.length">{{ item.modes.map(modeLabel).join(' / ') }}</span>
            <span v-if="item.tags.length">{{ item.tags.slice(0, 3).join(' · ') }}</span>
            <select
              v-if="item.packages.length > 1"
              class="sh-field acp-pack"
              :value="picked(item)"
              :aria-label="`${item.name} 模型包`"
              @change="onPack(item.family, $event)"
            >
              <option v-for="pkg in item.packages" :key="pkg.id" :value="pkg.id">{{ packLabel(pkg) }}</option>
            </select>
            <span v-else class="acp-pack-static">{{ packLabel(item.packages[0]) }}</span>
            <button
              v-if="item.description.length > 150"
              type="button"
              class="acp-link"
              @click="toggleExpanded(item.family)"
            >{{ expanded.has(item.family) ? '收起' : '全文' }}</button>
            <a v-if="item.docs.length" :href="docUrl(item)" target="_blank" rel="noreferrer">说明 ↗</a>
          </div>
        </div>
        <div class="acp-model-side">
          <div v-if="activeJob(item)" class="acp-job">
            <div class="acp-progress"><span :style="progressStyle(activeJob(item))" /></div>
            <span class="sh-hint">{{ jobText(activeJob(item)) }}</span>
            <ActionButton
              size="sm"
              :loading="state.busy === `cancel:${activeJob(item).id}`"
              @click="cancelInstall(activeJob(item).id)"
            >停止</ActionButton>
          </div>
          <template v-else-if="current(item).installed">
            <div class="acp-actions">
              <ActionButton
                size="sm"
                :loading="state.busy === `load:${current(item).id}` || state.busy === `unload:${loadedModel(item)?.id}`"
                :disabled="!data.service.reachable"
                @click="toggleLoad(item)"
              >{{ loadedModel(item) ? '卸载' : '加载' }}</ActionButton>
              <template v-if="state.confirmDelete === current(item).id">
                <ActionButton
                  size="sm"
                  variant="danger"
                  :loading="state.busy === `delete:${current(item).id}`"
                  @click="removePackage(current(item).id)"
                >确认删除</ActionButton>
                <ActionButton size="sm" variant="ghost" @click="state.confirmDelete = ''">取消</ActionButton>
              </template>
              <ActionButton
                v-else
                size="sm"
                variant="ghost"
                @click="state.confirmDelete = current(item).id"
              >删除</ActionButton>
            </div>
          </template>
          <template v-else>
            <div class="acp-actions">
              <ActionButton
                size="sm"
                variant="primary"
                :loading="state.busy === `install:${current(item).id}`"
                :disabled="!data.can_install"
                :title="data.can_install ? '' : '没有可用的 audio.cpp 程序或服务'"
                @click="install(current(item).id)"
              >安装</ActionButton>
              <ActionButton
                v-if="lastJob(item)"
                size="sm"
                variant="ghost"
                :loading="state.busy === `clean:${current(item).id}`"
                title="清理未完成的下载"
                @click="cleanPackage(current(item).id)"
              >清理</ActionButton>
            </div>
            <p v-if="lastJob(item)" class="acp-last-error">
              {{ lastJob(item).message || '上次下载失败' }}
            </p>
          </template>
        </div>
      </article>
    </div>

    <p v-if="!filtered.length" class="acp-empty">没有匹配的模型。</p>

    <div v-if="filtered.length > visible.length" class="acp-more">
      <ActionButton size="sm" @click="shown += 60">显示更多（{{ filtered.length - visible.length }}）</ActionButton>
    </div>
  </PanelCard>
</template>

<script setup>
// @ts-check
import { computed, reactive, ref, watch } from 'vue'
import ActionButton from '../../../components/ui/ActionButton.vue'
import PanelCard from '../../../components/ui/PanelCard.vue'
import {
  cancelInstall,
  cleanPackage,
  install,
  loadModel,
  refreshCatalog,
  removePackage,
  setMirror,
  state,
  unloadModel,
} from '../store'

const data = computed(() => state.data)
const shown = ref(60)
const picks = reactive(/** @type {Record<string, string>} */ ({}))

/** @param {Event} event */
function onKeyword(event) {
  state.keyword = /** @type {HTMLInputElement} */ (event.target).value
}

/** @param {Event} event */
function onMirror(event) {
  void setMirror(/** @type {HTMLSelectElement} */ (event.target).value)
}

/**
 * @param {string} family
 * @param {Event} event
 */
function onPack(family, event) {
  picks[family] = /** @type {HTMLSelectElement} */ (event.target).value
}
const expanded = reactive(new Set())

watch(
  () => [state.keyword, state.category],
  () => {
    shown.value = 60
  }
)

const mirrorNote = computed(() => {
  if (data.value?.mirror !== 'modelscope') return ''
  if (data.value?.endpoint) {
    return '已接入外部服务时不做本地安装：先断开，回到实例视图再用 ModelScope。'
  }
  if (!data.value?.runtime?.model_manager) {
    return 'ModelScope 下载需要官方 tools/model_manager_v2.py：当前可执行文件旁边没有这份工具，请切回 HF 镜像，或把可执行文件指向完整运行目录。'
  }
  return 'ModelScope 走官方 Python 管理器，只覆盖镜像仓库（HereIsMark/audio.cpp-gguf）里已有的包；没有镜像的模型（如 SenseVoice）请切回 HF。'
})

const blockedNote = computed(() => {
  if (data.value?.mirror === 'modelscope') return ''
  if (data.value?.can_manage) return ''
  if (data.value?.endpoint) {
    return '该服务未开放模型管理：清单可查，安装与删除在它自己的界面完成（服务页的「打开界面」）。'
  }
  if (!data.value?.runtime?.installed) {
    const external = (data.value?.instances || []).some(/** @param {any} row */ (row) => row.external)
    return external
      ? '服务页那台是别人起的进程，插件只能只读接入：要在这里装模型，先指定可执行文件（或下载运行包），由插件拉起一台。'
      : '在这装模型要用插件拉起的实例：先指定可执行文件，或下载运行包。'
  }
  return ''
})

const filtered = computed(() => {
  const rows = data.value?.models || []
  const keyword = state.keyword.trim().toLowerCase()
  return rows.filter(/** @param {any} row */ (row) => {
    if (state.category !== 'all' && row.category !== state.category) return false
    if (!keyword) return true
    const haystack = [row.name, row.family, row.description, row.summary, ...(row.tags || [])]
      .join(' ')
      .toLowerCase()
    return haystack.includes(keyword)
  })
})
const visible = computed(() => filtered.value.slice(0, shown.value))

/**
 * @param {any} family
 */
function toggleExpanded(family) {
  if (expanded.has(family)) expanded.delete(family)
  else expanded.add(family)
}

/**
 * @param {any} item
 */
function picked(item) {
  const remember = picks[item.family]
  if (remember && item.packages.some(/** @param {any} pkg */ (pkg) => pkg.id === remember)) return remember
  const installed = item.packages.find(/** @param {any} pkg */ (pkg) => pkg.installed)
  if (installed) return installed.id
  return (item.packages.find(/** @param {any} pkg */ (pkg) => pkg.default) || item.packages[0]).id
}

/**
 * @param {any} item
 */
function current(item) {
  return item.packages.find(/** @param {any} pkg */ (pkg) => pkg.id === picked(item)) || item.packages[0]
}

/**
 * @param {any} pkg
 */
function packLabel(pkg) {
  if (!pkg) return ''
  return [pkg.precision, pkg.format].filter(Boolean).join(' · ') || pkg.name
}

/**
 * @param {any} item
 */
function activeJob(item) {
  const running = item.packages
    .map(/** @param {any} pkg */ (pkg) => ({ ...pkg.job, id: pkg.id }))
    .find(
      /** @param {any} job */
      (job) => job.id && ['running', 'queued', 'cancelling'].includes(job.state)
    )
  return running || null
}

/**
 * @param {any} item
 */
function lastJob(item) {
  return item.packages.find(/** @param {any} pkg */ (pkg) => pkg.job && ['failed', 'cancelled'].includes(pkg.job.state))
}

/**
 * @param {any} job
 */
function jobText(job) {
  if (job.state === 'cancelling') return '正在停止'
  const percent = Math.round(Number(job.progress) || 0)
  if (job.total) {
    return `${percent}% · ${formatSize(job.downloaded)} / ${formatSize(job.total)}`
  }
  return `${percent}%`
}

/**
 * @param {any} job
 */
function progressStyle(job) {
  const value = Math.max(2, Math.min(100, Number(job.progress) || 0))
  return { width: `${value}%` }
}

/**
 * @param {number} bytes
 */
function formatSize(bytes) {
  const value = Number(bytes) || 0
  if (!value) return '0 MB'
  return value >= 1073741824 ? `${(value / 1073741824).toFixed(1)} GB` : `${(value / 1048576).toFixed(0)} MB`
}

/**
 * @param {any} mode
 */
function modeLabel(mode) {
  return (/** @type {Record<string, string>} */ ({ offline: '离线', streaming: '流式' })[mode]) || mode
}

/**
 * @param {any} item
 */
function docUrl(item) {
  const path = String(item.docs[0] || '').replace(/^\.?\//, '')
  return `https://github.com/0xShug0/audio.cpp/blob/main/${path}`
}

/**
 * @param {any} item
 */
function loadedModel(item) {
  return (data.value?.loaded || []).find(/** @param {any} row */ (row) => row.family === item.family) || null
}

/**
 * @param {any} item
 */
async function toggleLoad(item) {
  const loaded = loadedModel(item)
  if (loaded) {
    await unloadModel(loaded.id)
    return
  }
  await loadModel(current(item).id)
}
</script>

<style scoped>
.acp-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 2px 0 10px;
}
.acp-cats {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  flex: 1 1 320px;
  min-width: 0;
}
.acp-cat {
  padding: 4px 8px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  color: var(--sh-text-3);
  font-size: 11.5px;
  cursor: pointer;
}
.acp-cat:hover {
  background: var(--sh-hover);
}
.acp-cat.is-on {
  border-color: var(--sh-border, rgba(128, 140, 160, 0.28));
  background: var(--sh-hover);
  color: var(--sh-text);
}
.acp-search {
  flex: 0 1 220px;
  min-width: 150px;
}
.acp-mirror {
  flex: 0 0 190px;
}
.acp-banner {
  margin: 0 0 8px;
  padding: 7px 10px;
  border-radius: var(--sh-radius-sm, 6px);
  background: var(--sh-hover);
  color: var(--sh-text-3);
  font-size: 12px;
  line-height: 1.6;
}
.acp-banner.is-warn {
  color: var(--sh-warn);
}
.acp-list {
  display: flex;
  flex-direction: column;
}
.acp-model {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 9px 0;
  border-bottom: 1px solid var(--sh-border, rgba(128, 140, 160, 0.14));
}
.acp-model-main {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.acp-model-top {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.acp-model-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--sh-text);
}
.acp-model-desc {
  margin: 0;
  font-size: 11.9px;
  line-height: 1.55;
  color: var(--sh-text-3);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.acp-model-desc.is-open {
  display: block;
  overflow: visible;
}
.acp-link {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--sh-text-3);
  font-size: 11px;
  text-decoration: underline dotted;
  cursor: pointer;
}
.acp-model-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  font-size: 11px;
  color: var(--sh-text-4);
}
.acp-model-meta a {
  color: inherit;
  text-decoration: underline dotted;
}
.acp-model-side {
  flex: none;
  width: 156px;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}
.acp-pack {
  width: 128px;
  height: 24px;
  padding: 0 6px;
  font-size: 11px;
}
.acp-pack-static {
  font-size: 11px;
  color: var(--sh-text-4);
}
.acp-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.acp-last-error {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--sh-warn);
  text-align: right;
  word-break: break-word;
}
.acp-job {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
}
.acp-progress {
  width: 100%;
  height: 3px;
  border-radius: 2px;
  background: var(--sh-hover);
  overflow: hidden;
}
.acp-progress span {
  display: block;
  height: 100%;
  background: var(--sh-accent);
  transition: width 0.3s linear;
}
.acp-empty {
  margin: 14px 0;
  font-size: 12px;
  color: var(--sh-text-4);
}
.acp-more {
  display: flex;
  justify-content: center;
  padding-top: 10px;
}
@media (max-width: 720px) {
  .acp-model {
    flex-direction: column;
  }
  .acp-model-side {
    width: 100%;
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
  }
}
</style>
