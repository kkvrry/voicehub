<template>
  <!-- 引擎清单：适配到的全部引擎与它们的启停状态。
       启用 = 出现在音色来源、生成页与生产方候选里；停用 = 只是不再出现，配置与
       库内音色都留着。所以这里是**开关**，不是"添加/删除"——两态随时可逆。 -->
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="store.catalogOpen" class="vh-root" @click.self="close">
        <div
          ref="cardEl"
          class="vh-modal catalog-card"
          role="dialog"
          aria-modal="true"
          aria-label="引擎清单"
          tabindex="-1"
        >
          <header class="catalog-head">
            <div class="catalog-head-main">
              <h2 class="catalog-title">引擎清单</h2>
              <p class="catalog-note">{{ summary }}</p>
            </div>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="close">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <ul v-if="rows.length" class="catalog-list">
            <li
              v-for="row in rows"
              :key="row.key"
              class="catalog-row"
              :class="{ 'is-on': row.enabled }"
              @contextmenu="openRowMenu(row, $event)"
            >
              <img v-if="row.icon_url" class="catalog-icon" :src="row.icon_url" alt="" />
              <span v-else class="catalog-icon is-placeholder" aria-hidden="true">◻</span>
              <span class="catalog-text">
                <span class="catalog-name">
                  {{ row.label }}
                  <span class="catalog-kind">{{ row.kind_label }}</span>
                </span>
                <span class="catalog-sub" :class="{ 'is-warn': showNote(row) }">
                  {{ showNote(row) ? row.note : row.kind_note }}
                </span>
              </span>
              <!-- 已启用的才谈得上"去哪配"：入口贴着它的开关，不另设第三处。 -->
              <button
                v-if="row.enabled"
                type="button"
                class="catalog-open"
                :title="`打开「${row.label}」的设置`"
                @click="configure(row)"
              >
                设置
              </button>
              <ToggleSwitch
                :model-value="row.enabled"
                :label="`启用 ${row.label}`"
                :disabled="busyKey === row.key"
                @update:model-value="onEnabled(row, $event)"
              />
            </li>
          </ul>

          <p v-else class="catalog-empty">没有适配的引擎。</p>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import { actions, say, store } from '../store'
import { overlayTarget } from '../composables/overlayTarget'
import { openObjectMenu } from '../composables/contextMenu'
import ToggleSwitch from './ui/ToggleSwitch.vue'

/** 这一层常驻：挂的是**壳登记的那个元素**，不是挂载时 query 出来的一个——
 *  壳会被整个换掉（进设置页），绑死旧元素就再也画不出来（见 composables/overlayTarget）。 */
const cardEl = ref(/** @type {any} */ (null))
const busyKey = ref('')

const rows = computed(() => store.engineCatalog || [])

/** 抬头那句：适配几台、启用几台。两个数字都在清单里，不让用户自己数。 */
const summary = computed(() => {
  const total = rows.value.length
  const on = rows.value.filter(/** @param {any} row */ (row) => row.enabled).length
  if (!total) return ''
  if (!on) return `适配 ${total} 台，都未启用。启用的才会出现在音色来源、生成页与生产方候选里。`
  if (on === total) return `适配 ${total} 台，全部启用。`
  return `适配 ${total} 台，启用 ${on} 台。`
})

/**
 * 未就绪且不是"只差密钥"时，把原因摆出来——只差密钥的那句在形态说明里已经说过。
 *
 * @param {any} row
 */
function showNote(row) {
  return !row.ready && !row.needs_key && Boolean(row.note)
}

/**
 * 开关：就地生效，留在清单里。连开两台是常事，切走反而断了手。
 *
 * @param {any} row
 * @param {any} value
 */
/**
 * 模板里的箭头函数在类型检查下会被当成属性访问；转成方法。
 * @param {any} row
 * @param {boolean} value
 */
function onEnabled(row, value) {
  void setEnabled(row, value)
}

/** 行右键：只给开关这一件事，和行上的 ToggleSwitch 同一个动作。 */
/**
 * @param {any} row
 * @param {MouseEvent} event
 */
function openRowMenu(row, event) {
  openObjectMenu(
    'engine.row',
    {
      enabled: Boolean(row.enabled),
      busy: busyKey.value === row.key,
      toggle: () => setEnabled(row, !row.enabled),
    },
    { event }
  )
}

/**
 * @param {any} row
 * @param {boolean} value
 */
async function setEnabled(row, value) {
  if (busyKey.value) return
  busyKey.value = row.key
  try {
    await (value ? actions.enableEngine(row.key) : actions.disableEngine(row.key))
  } catch (error) {
    say(String(error instanceof Error ? error.message : String(error)))
  } finally {
    busyKey.value = ''
  }
}

/**
 * @param {any} row
 */
async function configure(row) {
  store.catalogOpen = false
  await actions.openEngine(row.key)
}

function close() {
  if (busyKey.value) return
  store.catalogOpen = false
}

/**
 * @param {any} event
 */
function onKeydown(event) {
  if (event.key === 'Escape' && store.catalogOpen) {
    event.preventDefault()
    close()
  }
}

watch(
  () => store.catalogOpen,
  async (open) => {
    if (open) {
      window.addEventListener('keydown', onKeydown)
      await nextTick()
      cardEl.value?.focus()
    } else {
      window.removeEventListener('keydown', onKeydown)
    }
  }
)

onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<style scoped>
.catalog-card {
  width: min(560px, 100%);
  max-height: min(78vh, 720px);
}
.catalog-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 18px 18px 12px 22px;
  border-bottom: 1px solid var(--sh-line);
}
.catalog-head-main {
  flex: 1 1 auto;
  min-width: 0;
}
.catalog-title {
  margin: 0;
  font-size: 14.5px;
  font-weight: 650;
  color: var(--sh-text);
}
.catalog-note {
  margin: 4px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--sh-text-4);
}
.catalog-list {
  margin: 0;
  padding: 6px 8px 10px;
  overflow-y: auto;
  list-style: none;
}
.catalog-row {
  display: flex;
  align-items: center;
  gap: 11px;
  min-height: 54px;
  padding: 6px 12px;
  border-radius: var(--sh-r-md);
}
.catalog-icon {
  flex: none;
  width: 24px;
  height: 24px;
  border-radius: 6px;
  object-fit: contain;
}
.catalog-icon.is-placeholder {
  display: grid;
  place-items: center;
  background: var(--sh-chip-bg, rgba(128, 140, 160, 0.14));
  color: var(--sh-text-4);
  font-size: 11px;
}
.catalog-text {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.catalog-name {
  display: flex;
  align-items: baseline;
  gap: 7px;
  font-size: 13px;
  font-weight: 600;
  color: var(--sh-text);
}
.catalog-kind {
  font-size: 10.5px;
  font-weight: 500;
  color: var(--sh-text-4);
}
.catalog-sub {
  overflow: hidden;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--sh-text-4);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.catalog-sub.is-warn {
  color: var(--sh-warn, #d98a1a);
}
/* 停用的那一行整体压低一档：名字仍读得清，但不是当下的工作集。 */
.catalog-row:not(.is-on) .catalog-name,
.catalog-row:not(.is-on) .catalog-icon {
  opacity: 0.62;
}
.catalog-open {
  flex: none;
  padding: 2px 4px;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: transparent;
  color: var(--sh-text-3);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
  transition: color var(--sh-t-fast) var(--sh-ease);
}
.catalog-open:hover {
  color: var(--sh-accent);
}
.catalog-open:focus-visible {
  outline: 2px solid var(--sh-accent);
  outline-offset: 1px;
}
.catalog-empty {
  margin: 0;
  padding: 26px 22px 30px;
  font-size: 12.5px;
  color: var(--sh-text-3);
  text-align: center;
}
</style>
