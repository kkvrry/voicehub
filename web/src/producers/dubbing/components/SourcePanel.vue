<template>
  <aside class="dub-col" aria-label="源与角色">
    <div class="dub-col-head">
      <span class="t">源</span>
      <span class="n">{{ project?.kind === 'video' ? '视频' : '音频' }}</span>
    </div>

    <div class="src">
      <div class="src-name" :title="project?.source_name">{{ project?.source_name }}</div>
      <div class="src-meta">
        <span>{{ durationLabel }}</span>
        <span>第 {{ (project?.stream_index ?? 0) + 1 }} 条音轨</span>
      </div>
    </div>

    <div class="dub-col-head">
      <span class="t">角色</span>
      <button type="button" class="dub-mini" title="新建角色" aria-label="新建角色" @click="create">
        <Plus :size="14" :stroke-width="1.8" />
      </button>
    </div>

    <div class="cast">
      <div v-for="role in roles" :key="role.id" class="role">
        <div class="role-top">
          <span class="role-dot" :style="{ background: role.color }"></span>
          <input
            class="role-name"
            :value="role.name"
            :aria-label="`角色名：${role.name}`"
            @change="rename(role, $event.target.value)"
          />
          <select
            class="role-mode"
            :value="role.mode"
            :aria-label="`${role.name} 的处理方式`"
            @change="setMode(role, $event.target.value)"
          >
            <option value="tts">配音</option>
            <option value="original">原声</option>
          </select>
          <button
            type="button"
            class="dub-mini"
            title="删除角色"
            aria-label="删除角色"
            @click="remove(role)"
          >
            <Trash2 :size="13" :stroke-width="1.8" />
          </button>
        </div>

        <select
          v-if="role.mode === 'tts'"
          class="role-voice"
          :value="role.engine && role.voice_id ? `${role.engine}|${role.voice_id}` : ''"
          :aria-label="`${role.name} 的音色`"
          @change="setVoice(role, $event.target.value)"
        >
          <option value="">选择音色…</option>
          <option v-for="voice in voices" :key="voice.key" :value="voice.key">
            {{ voice.label }}
          </option>
        </select>
        <div v-else class="role-note">保留原声，不重新合成</div>

        <div class="role-sub">{{ roleSummary(role) }}</div>
      </div>
      <p v-if="!roles.length" class="dub-empty">还没有角色。</p>
    </div>

    <div class="stats">
      <div><b>{{ stats.total }}</b><span>段</span></div>
      <div><b>{{ stats.ready }}</b><span>已生成</span></div>
      <div><b>{{ stats.stale }}</b><span>过期</span></div>
      <div><b>{{ stats.failed }}</b><span>失败</span></div>
    </div>
  </aside>
</template>

<script setup>
// 左列：源信息与角色表。角色是对象——换音色只改这里，逐段读它决定谁来配音。
import { computed } from 'vue'
import { Plus, Trash2 } from 'lucide-vue-next'
import {
  createRole,
  cues,
  patchRole,
  project,
  removeRole,
  roles,
  state,
} from '../store'

const voices = computed(() =>
  state.voices.map((voice) => ({
    key: `${voice.engines[0]}|${voice.id}`,
    label: voice.shown_name || voice.name,
  }))
)

const stats = computed(() => state.detail?.stats || { total: 0, ready: 0, stale: 0, failed: 0 })

const durationLabel = computed(() => {
  const total = Math.round((project.value?.duration_ms || 0) / 1000)
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
})

function roleSummary(role) {
  const count = cues.value.filter((cue) => (cue.role_id || roles.value[0]?.id) === role.id).length
  if (role.mode !== 'tts') return `${count} 段 · 不合成`
  if (!role.voice_id) return `${count} 段 · 未选音色`
  return `${count} 段 · ${role.voice_name || '已选音色'}`
}

async function create() {
  const name = window.prompt('角色名', `角色 ${roles.value.length + 1}`)
  if (!name || !name.trim()) return
  await createRole(name.trim())
}

async function rename(role, value) {
  const name = String(value || '').trim()
  if (!name || name === role.name) return
  await patchRole(role, { name })
}

function setMode(role, mode) {
  if (mode === role.mode) return
  void patchRole(role, { mode })
}

function setVoice(role, key) {
  if (!key) {
    void patchRole(role, { engine: '', voice_id: '', voice_name: '' })
    return
  }
  const [engine, voiceId] = key.split('|')
  const voice = voices.value.find((item) => item.key === key)
  void patchRole(role, {
    engine,
    voice_id: voiceId,
    voice_name: voice?.label || '',
  })
}

async function remove(role) {
  if (!window.confirm(`删除角色「${role.name}」？它名下的段落会退回旁白。`)) return
  await removeRole(role)
}
</script>

<style scoped>
.src {
  flex: none;
  margin: 0 12px 8px;
  padding: 9px 11px;
  border-radius: var(--sh-r-sm);
  background: var(--sh-chip);
}

.src-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--sh-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.src-meta {
  display: flex;
  gap: 10px;
  margin-top: 3px;
  font-size: 10.5px;
  color: var(--sh-text-3);
}

.cast {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0 8px 8px;
}

.role {
  padding: 7px 6px 8px;
  border-radius: var(--sh-r-xs);
}

.role:hover { background: var(--sh-hover); }

.role-top {
  display: flex;
  align-items: center;
  gap: 6px;
}

.role-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
}

.role-name {
  flex: 1;
  min-width: 0;
  height: 22px;
  padding: 0 4px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--sh-text);
  font: inherit;
  font-size: 12px;
  font-weight: 600;
}

.role-name:hover,
.role-name:focus {
  border-color: var(--sh-line);
  background: var(--sh-field);
  outline: none;
}

.role-mode {
  height: 22px;
  padding: 0 2px;
  border: 1px solid var(--sh-line);
  border-radius: 5px;
  background: var(--sh-chip);
  color: var(--sh-text-2);
  font: inherit;
  font-size: 10.5px;
}

.role-voice {
  width: 100%;
  height: 24px;
  margin-top: 5px;
  padding: 0 4px;
  border: 1px solid var(--sh-line);
  border-radius: 5px;
  background: var(--sh-field);
  color: var(--sh-text-2);
  font: inherit;
  font-size: 11px;
}

.role-note {
  margin-top: 5px;
  font-size: 10.5px;
  color: var(--sh-text-4);
}

.role-sub {
  margin-top: 4px;
  font-size: 10.5px;
  color: var(--sh-text-3);
}

.stats {
  flex: none;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  margin: 0 12px 12px;
  border-radius: var(--sh-r-xs);
  border: 1px solid var(--sh-line);
  background: var(--sh-line-soft);
  overflow: hidden;
}

.stats div {
  display: flex;
  align-items: baseline;
  gap: 5px;
  padding: 7px 10px;
  background: var(--sh-panel);
}

.stats b {
  font-family: var(--sh-mono, ui-monospace, monospace);
  font-size: 13px;
  color: var(--sh-text);
}

.stats span {
  font-size: 10.5px;
  color: var(--sh-text-3);
}

.dub-empty {
  padding: 4px 8px;
  font-size: 11px;
  color: var(--sh-text-4);
}
</style>
