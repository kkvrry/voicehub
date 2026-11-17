<template>
  <article
    v-show="!hidden"
    class="sh-card service-card"
    :class="{ 'is-off': !enabled }"
    :data-plugin="plugin.key"
  >
    <header class="service-head">
      <span
        v-if="plugin.icon_url"
        class="service-avatar has-icon"
        :style="{ '--glyph': `url(${plugin.icon_url})` }"
        aria-hidden="true"
      />
      <span v-else class="service-avatar" aria-hidden="true">
        <Package :size="19" :stroke-width="1.8" />
      </span>

      <div class="service-id">
        <div class="service-name-line">
          <span class="service-name">{{ plugin.label }}</span>
          <span class="service-version">v{{ plugin.version || '—' }}</span>
        </div>
        <p v-if="description" class="service-desc">{{ description }}</p>
        <span class="service-state" :class="{ 'is-on': enabled }">
          <span class="service-dot" aria-hidden="true" />
          {{ enabled ? '运行中' : '已停用' }}
        </span>
      </div>

      <ToggleSwitch
        class="service-toggle"
        :model-value="enabled"
        :disabled="pending"
        :label="`启用 ${plugin.label}`"
        @update:model-value="toggle"
      />
    </header>

    <footer v-if="enabled" class="service-actions">
      <ActionButton
        v-if="plugin.settings_schema"
        variant="ghost"
        size="sm"
        aria-haspopup="dialog"
        @click="openDialog"
      >
        <Settings2 :size="14" :stroke-width="1.8" aria-hidden="true" />
        设置
      </ActionButton>
      <a
        v-if="plugin.homepage"
        class="sh-btn is-ghost is-sm"
        :href="plugin.homepage"
        target="_blank"
        rel="noreferrer"
      >
        <ExternalLink :size="14" :stroke-width="1.8" aria-hidden="true" />
        主页
      </a>
    </footer>

    <!-- 弹窗由插件名与 schema 驱动：同类插件共用这一个壳。 -->
    <PluginDialog :plugin="plugin" :open="dialogOpen" @close="closeDialog" />
  </article>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ExternalLink, Package, Settings2 } from 'lucide-vue-next'
import { actions, say, store } from '../../store'
import { useCardId, useSearch, useSectionMeta, type SearchRow } from '../search'
import ActionButton from '../../components/ui/ActionButton.vue'
import ToggleSwitch from '../../components/ui/ToggleSwitch.vue'
import PluginDialog from './PluginDialog.vue'

interface ProducerRow {
  key: string
  label: string
  description?: string
  version?: string
  homepage?: string
  icon_url?: string
  enabled?: boolean
  settings_schema?: Record<string, unknown> | null
  settings_group?: { key: string; label: string }
  [key: string]: unknown
}

const props = defineProps<{ plugin: ProducerRow }>()

const search = useSearch()
const section = useSectionMeta()
const cardId = useCardId()
const dialogOpen = ref(false)
const pending = ref(false)

const enabled = computed(() => !store.disabledProducers.includes(props.plugin.key))

/** 卡片的一句话说明由插件自描述；老后端没有这个字段就不写说明。 */
const description = computed(() => props.plugin.description || '')

// 每张卡给自己登记一条搜索项：按插件名 / 说明 / 关键词命中，卡与分区据此显隐。
const row = reactive<SearchRow>({
  id: `plugin-${props.plugin.key}`,
  section: section.id,
  card: cardId,
  title: props.plugin.label,
  desc: description.value,
  keywords: `插件 生产方 ${props.plugin.label} ${props.plugin.key} 启用 停用 工作区 设置`,
  context: `${section.title} 生产方插件`,
})

onMounted(() => search?.rows.push(row))
onBeforeUnmount(() => {
  const index = search?.rows.indexOf(row) ?? -1
  if (index >= 0) search?.rows.splice(index, 1)
})

watch(
  () => [props.plugin.label, description.value],
  () => {
    row.title = props.plugin.label
    row.desc = description.value
  }
)

/** 搜索命中卡里的配置字段时也留住这张卡，并把配置展开——否则命中的行藏在收起的抽屉里。 */
const configMatch = computed(() => {
  const tokens = (search?.query.value || '').trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (!tokens.length) return false
  const properties = (props.plugin.settings_schema?.properties || {}) as Record<
    string,
    { label?: string; title?: string; description?: string }
  >
  const haystack = Object.entries(properties)
    .map(([key, spec]) => `${key} ${spec.label || ''} ${spec.title || ''} ${spec.description || ''}`)
    .join(' ')
    .toLowerCase()
  return tokens.every((token) => haystack.includes(token))
})

watch(configMatch, (matched) => {
  if (matched) openDialog()
})

const hidden = computed(() => {
  const query = search?.query.value.trim() || ''
  if (!query || !search) return false
  return !search.matches(row) && !configMatch.value
})

/** 卡片被搜索筛掉时，弹窗不能留在屏幕上。 */
watch(hidden, (gone) => {
  if (gone) closeDialog()
})

async function toggle(value: boolean) {
  if (pending.value) return
  pending.value = true
  const ok = await actions.setProducerEnabled(props.plugin.key, Boolean(value))
  pending.value = false
  if (!ok) return
  if (!value) closeDialog()
  say(`${props.plugin.label} 已${value ? '启用' : '停用'}`)
}

function openDialog() {
  dialogOpen.value = true
}

function closeDialog() {
  dialogOpen.value = false
}
</script>

<style scoped>
/* 一张卡一项服务：头像 + 名称/说明/状态 + 开关；动作在底部，展开配置时整行加宽。 */
.service-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 18px;
  transition: border-color var(--sh-t-fast) var(--sh-ease), opacity var(--sh-t-fast) var(--sh-ease);
}
.service-card:hover {
  border-color: var(--sh-line-strong);
}
.service-card.is-off {
  border-style: dashed;
  opacity: 0.72;
}
.service-card.is-off:hover {
  opacity: 0.92;
}
.service-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.service-toggle {
  position: absolute;
  top: 15px;
  right: 17px;
}
.service-avatar {
  flex: none;
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border-radius: 11px;
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}
.service-avatar.has-icon::before {
  content: '';
  width: 20px;
  height: 20px;
  background: currentColor;
  -webkit-mask: var(--glyph) center / contain no-repeat;
  mask: var(--glyph) center / contain no-repeat;
}
.service-card.is-off .service-avatar {
  background: var(--sh-hover);
  color: var(--sh-text-4);
}
.service-id {
  flex: 1;
  min-width: 0;
}
.service-name-line {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 7px;
  /* 右上角留给开关；说明文字用整行宽，不跟着让位。 */
  padding-right: 42px;
}
.service-name {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--sh-text);
}
.service-version {
  font-size: 10.5px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}
.service-desc {
  margin: 6px 0 0;
  font-size: 11.6px;
  line-height: 1.55;
  color: var(--sh-text-3);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.service-state {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-top: 7px;
  font-size: 10.8px;
  color: var(--sh-text-4);
}
.service-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.service-state.is-on {
  color: var(--sh-ok);
}
.service-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin-top: auto;
  padding-top: 9px;
  border-top: 1px solid var(--sh-line-soft);
}
.service-actions .sh-btn.is-sm {
  padding-inline: 8px;
}
</style>
