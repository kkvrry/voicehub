<template>
  <aside class="castbar" aria-label="角色">
    <div class="castbar-head">
      <span class="castbar-title">角色</span>
      <span class="castbar-count">{{ roles.length }} 位</span>
    </div>

    <div class="castbar-list">
      <button
        v-for="role in roles"
        :key="role.id"
        type="button"
        class="role-chip"
        :class="{ 'is-on': role.id === activeRoleId, 'is-keep': role.mode !== 'tts' }"
        :title="`${role.name}：${rollSummary(role)}`"
        @click="openRole(role, $event)"
      >
        <span class="role-main">
          <span class="role-name-row">
            <span class="role-name">{{ role.name }}</span>
            <span class="role-mode">{{ role.mode === 'tts' ? '配音' : '原声' }}</span>
          </span>
          <span class="role-voice" :title="voiceLabel(role)">{{ voiceLabel(role) }}</span>
        </span>
        <span class="role-count">{{ rollSummary(role) }}</span>
      </button>

      <button ref="addEl" type="button" class="role-add" @click="addOpen = true">
        <Plus :size="14" :stroke-width="1.8" />
        角色
      </button>
    </div>

    <Popover
      :open="Boolean(roleOpen)"
      :anchor="roleAnchor"
      :width="352"
      label="角色设置"
      @close="roleOpen = ''"
    >
      <template v-if="role">
        <div class="rf-head">
          <input
            class="rf-name sh-field"
            :value="role.name"
            aria-label="角色名"
            @change="onRename(role, $event)"
            @keydown.enter.prevent="blurTarget($event)"
          />
        </div>

        <div class="rf-row">
          <span class="rf-label">处理方式</span>
          <SegmentedControl
            :model-value="role.mode"
            :options="MODE_OPTIONS"
            label="处理方式"
            @update:model-value="onMode(role, $event)"
          />
        </div>

        <div v-if="role.mode === 'tts'" class="rf-row is-stack">
          <span class="rf-label">音色 <em>该角色 {{ roleCount(role) }} 段一起换</em></span>
          <p class="rf-voice">{{ voiceLabel(role) }}</p>
          <p class="rf-note">在左侧音色栏选一条音色，点卡片右端的「指定」交给这个角色。</p>
        </div>
        <p v-else class="rf-note">保留源文件的声音，这一段不进合成清单。</p>

        <p class="rf-summary">{{ rollSummary(role) }}</p>

        <footer class="rf-foot">
          <button type="button" class="sh-btn is-sm is-danger" @click="remove(role)">
            <Trash2 :size="14" :stroke-width="1.8" />
            删除角色
          </button>
          <span class="rf-hint">删除后它名下的段落退回旁白</span>
        </footer>
      </template>
    </Popover>

    <Popover
      :open="addOpen"
      :anchor="addEl"
      :width="264"
      label="新建角色"
      @close="addOpen = false"
    >
      <form class="rf-add" @submit.prevent="create">
        <label class="rf-label" for="dub-new-role">角色名</label>
        <input
          id="dub-new-role"
          v-model="newName"
          class="sh-field"
          :placeholder="`角色 ${roles.length + 1}`"
          autocomplete="off"
        />
        <div class="rf-add-actions">
          <button type="button" class="sh-btn is-sm is-ghost" @click="addOpen = false">取消</button>
          <button type="submit" class="sh-btn is-sm is-primary" :disabled="!newName.trim()">
            新建
          </button>
        </div>
      </form>
    </Popover>
  </aside>
</template>

<script setup>
// @ts-check
// 角色条：横向一排「这一段由谁来说」。角色的音色、处理方式、改名与删除都在
// 点开的同一张小卡里；工作区不再为角色单占一整列，横向空间留给字幕。
import { computed, nextTick, ref } from 'vue'
import { Plus, Trash2 } from 'lucide-vue-next'
import Popover from '../../../components/ui/Popover.vue'
import SegmentedControl from '../../../components/ui/SegmentedControl.vue'
import {
  activeRoleId,
  createRole,
  cues,
  patchRole,
  removeRole,
  roles,
} from '../store'

const MODE_OPTIONS = [
  { value: 'tts', label: '配音' },
  { value: 'original', label: '原声' },
]

const roleOpen = ref('')
const roleAnchor = ref(/** @type {any} */ (null))
const addOpen = ref(false)
const addEl = ref(/** @type {any} */ (null))
const newName = ref('')

const role = computed(
  () => roles.value.find((/** @type {any} */ item) => item.id === roleOpen.value) || null
)

/** @param {any} role */
function mine(role) {
  return cues.value.filter(
    (/** @type {any} */ cue) => (cue.role_id || roles.value[0]?.id) === role.id
  )
}

/** @param {any} role */
function roleCount(role) {
  return mine(role).length
}

/** @param {any} role */
function rollSummary(role) {
  const list = mine(role)
  if (!role || role.mode !== 'tts') return `${list.length} 段 · 原声`
  if (!role.voice_id) return `${list.length} 段 · 未选音色`
  const ready = list.filter(
    (/** @type {any} */ cue) => cue.dub && cue.audio && cue.state === 'ready'
  ).length
  return `${list.length} 段 · ${ready} 已配音`
}

/** @param {any} role */
function voiceLabel(role) {
  if (!role || role.mode !== 'tts') return '保留源文件的声音'
  if (!role.voice_id) return '未选音色'
  return role.voice_name || `${role.engine} / ${role.voice_id}`
}

/** @param {any} role @param {MouseEvent} event */
function openRole(role, event) {
  roleAnchor.value = event.currentTarget
  roleOpen.value = roleOpen.value === role.id ? '' : role.id
}

/** @param {any} role @param {Event} event */
async function onRename(role, event) {
  const target = /** @type {HTMLInputElement | null} */ (event.target)
  const name = String(target?.value || '').trim()
  if (!name || name === role.name) return
  await patchRole(role, { name })
}

/** @param {Event} event */
function blurTarget(event) {
  /** @type {HTMLInputElement | null} */ (event.target)?.blur()
}

/** @param {any} role @param {any} value */
function onMode(role, value) {
  const mode = String(value)
  if (mode === role.mode) return
  void patchRole(role, { mode })
}

async function create() {
  const value = newName.value.trim()
  if (!value) return
  const created = await createRole(value)
  if (created) {
    newName.value = ''
    addOpen.value = false
    await nextTick()
    roleOpen.value = created.id
  }
}

/** @param {any} role */
async function remove(role) {
  if (!window.confirm(`删除角色「${role.name}」？它名下的段落会退回旁白。`)) return
  roleOpen.value = ''
  await removeRole(role)
}
</script>

<style scoped>
.castbar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  margin: 0 22px 10px;
  padding: 7px 10px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-md);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-1);
}

.castbar-head {
  flex: none;
  display: flex;
  align-items: baseline;
  gap: 7px;
  max-width: 238px;
  padding-right: 10px;
  border-right: 1px solid var(--sh-line-soft);
}

.castbar-title {
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--sh-text-3);
}

.castbar-count {
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.castbar-list {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: none;
}

.castbar-list::-webkit-scrollbar { display: none; }

.role-chip {
  flex: none;
  display: flex;
  align-items: center;
  gap: 7px;
  max-width: 230px;
  height: 42px;
  padding: 4px 9px 4px 8px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-xs);
  background: var(--sh-field);
  text-align: left;
}

.role-chip:hover {
  border-color: var(--sh-accent-ring);
  background: var(--sh-hover);
}

.role-chip.is-on {
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
}

.role-chip.is-keep { opacity: 0.82; }

.role-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.role-name-row {
  display: flex;
  align-items: baseline;
  gap: 5px;
  min-width: 0;
}

.role-name {
  max-width: 108px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
  color: var(--sh-text);
}

.role-mode {
  flex: none;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--sh-chip);
  color: var(--sh-text-3);
  font-size: 9.5px;
  line-height: 14px;
}

.role-voice {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10px;
  color: var(--sh-text-4);
}

.role-count {
  flex: none;
  margin-left: 2px;
  font-size: 10px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}

.role-add {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 10px;
  border: 1px dashed var(--sh-line);
  border-radius: 999px;
  color: var(--sh-text-3);
  font-size: 11.5px;
}

.role-add:hover {
  border-color: var(--sh-accent-ring);
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
}

.rf-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.rf-name {
  flex: 1;
  min-width: 0;
  height: 30px;
  font-weight: 600;
}

.rf-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}

.rf-row.is-stack {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
}

.rf-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--sh-text-3);
}

.rf-label em {
  margin-left: 5px;
  font-style: normal;
  font-weight: 400;
  color: var(--sh-text-4);
}

.rf-voice {
  margin: 0;
  padding: 6px 8px;
  border-radius: var(--sh-r-xs);
  background: var(--sh-field);
  font-size: 11.5px;
  color: var(--sh-text-2);
  word-break: break-all;
}

.rf-note {
  margin: 8px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--sh-text-4);
}

.rf-summary {
  margin: 11px 0 0;
  padding-top: 9px;
  border-top: 1px solid var(--sh-line-soft);
  font-size: 11px;
  color: var(--sh-text-3);
  font-variant-numeric: tabular-nums;
}

.rf-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}

.rf-hint {
  min-width: 0;
  font-size: 10px;
  color: var(--sh-text-4);
  line-height: 1.4;
}

.rf-add {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.rf-add input { height: 30px; }

.rf-add-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 3px;
}
</style>
