<template>
  <SettingsSection id="shortcuts" title="快捷键">
    <template #icon><Keyboard :size="16" :stroke-width="1.8" /></template>

    <SettingCard title="全局按键" keywords="快捷键 组合键 键位 重录 键盘">
      <template #icon><Command :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        v-for="action in actionsList"
        :key="action"
        :title="SHORTCUT_LABELS[action]"
        :desc="descriptions[action]"
        :keywords="`快捷键 ${SHORTCUT_LABELS[action]}`"
      >
        <div class="sc-control">
          <button
            type="button"
            class="sc-chip"
            :class="{ 'is-recording': pending === action }"
            :aria-label="`重录：${SHORTCUT_LABELS[action]}`"
            :aria-pressed="pending === action"
            @click="pending === action ? stopRecord() : startRecord(action)"
          >
            <template v-if="pending === action">
              <span class="sc-pulse" aria-hidden="true" />
              按下组合键…
            </template>
            <template v-else>
              <kbd v-for="part in prettyCombo(store.shortcuts[action])" :key="part" class="sh-kbd">
                {{ part }}
              </kbd>
            </template>
          </button>
          <button
            v-if="pending === action"
            type="button"
            class="sh-btn is-icon"
            title="取消录制"
            aria-label="取消录制"
            @click="stopRecord"
          >
            <X :size="14" :stroke-width="1.8" />
          </button>
        </div>
      </SettingRow>

      <div v-if="error" class="sc-error" role="alert">
        <AlertTriangle :size="14" :stroke-width="1.8" />
        {{ error }}
      </div>

      <template #note>
        <span v-if="duplicates.length" class="sc-error-inline">
          有重复键位：{{ duplicates.join('、') }}
        </span>
        <span v-else>Esc 取消录制；单键只支持符号与功能键。</span>
      </template>
      <template #foot>
        <ActionButton variant="ghost" size="sm" @click="resetShortcuts">
          <RotateCcw :size="14" :stroke-width="1.8" />
          恢复默认键位
        </ActionButton>
      </template>
    </SettingCard>

    <SettingCard v-if="capture" title="系统内录" keywords="内录 系统声音 录制 热键">
      <template #icon><Mic :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        :title="capture.label"
        :desc="capture.hint"
        :keywords="`内录 ${capture.label}`"
      >
        <div class="sc-control">
          <template v-if="captureAvailable">
            <kbd v-for="part in prettyCombo(capture.hotkey.text)" :key="part" class="sh-kbd">
              {{ part }}
            </kbd>
          </template>
          <span v-if="captureNote" class="sc-note">{{ captureNote }}</span>
        </div>
      </SettingRow>

      <template #note>由托盘注册，作用于整个系统。</template>
    </SettingCard>

    <SettingCard title="按键规则" keywords="规则 说明 Ctrl Alt Shift 系统 冲突">
      <template #icon><Info :size="16" :stroke-width="1.8" /></template>

      <div class="sc-rules">
        <div class="sc-rule">
          <kbd class="sh-kbd">Ctrl</kbd>
          <kbd class="sh-kbd">Alt</kbd>
          <kbd class="sh-kbd">Shift</kbd>
          <span>修饰键可以叠加；字母键必须带至少一个修饰键。</span>
        </div>
        <div class="sc-rule">
          <kbd class="sh-kbd">/</kbd>
          <kbd class="sh-kbd">Esc</kbd>
          <kbd class="sh-kbd">F1–F12</kbd>
          <span>允许单独使用，但只在不处于输入状态时生效。</span>
        </div>
        <div class="sc-rule">
          <kbd class="sh-kbd">Ctrl</kbd><span>+</span><kbd class="sh-kbd">,</kbd>
          <span>打开设置页；同一组合键只能绑定一个动作。</span>
        </div>
      </div>
    </SettingCard>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { AlertTriangle, Command, Info, Keyboard, Mic, RotateCcw, X } from 'lucide-vue-next'
import { api } from '../../api'
import { SHORTCUT_LABELS, useSettingsStore } from '../../stores/settings'
import type { ShortcutAction } from '../../stores/settings'
import { comboFromEvent, findDuplicates, isSafeBareKey, recorder } from '../keymap'
import { prettyCombo } from '../utils'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'
import ActionButton from '../../components/ui/ActionButton.vue'

const store = useSettingsStore()

const actionsList = Object.keys(SHORTCUT_LABELS) as ShortcutAction[]

/** 只写按键名看不出来的部分：命令入口与作用范围；其余留空，标题已经说清。 */
const descriptions: Record<ShortcutAction, string> = {
  openSettings: '',
  toggleTheme: '',
  focusSearch: '在框内输入 / 可打开命令。',
  openFilters: '只在音色库、生成与生产方页有效。',
  backToLibrary: '',
  saveSettings: '',
}

const pending = ref<ShortcutAction | ''>('')
const error = ref('')

const duplicates = computed(() => findDuplicates(store.shortcuts))

/** 内录：由托盘注册全局热键，页面只显示组合键与可用性。 */
const capture = ref<null | Record<string, any>>(null)
const captureAvailable = computed(() => capture.value?.available !== false)
const captureNote = computed(() => {
  const info = capture.value
  if (!info) return ''
  if (!info.available) return info.reason || '不可用'
  if (info.hotkey_ok === false) return info.hotkey_reason || '热键不可用'
  if (info.hotkey_ok === null) return '托盘启动后可用'
  return ''
})

onMounted(async () => {
  try {
    capture.value = await api.capture()
  } catch {
    // 旧后端没有这个接口：整块不显示，设置页其余部分照常。
    capture.value = null
  }
})

function startRecord(action: ShortcutAction) {
  stopRecord()
  pending.value = action
  recorder.action = action
  error.value = ''
  window.addEventListener('keydown', onCapture, true)
}

function stopRecord() {
  pending.value = ''
  recorder.action = ''
  window.removeEventListener('keydown', onCapture, true)
}

function onCapture(event: KeyboardEvent) {
  if (!pending.value) return
  event.preventDefault()
  event.stopPropagation()
  if (event.key === 'Escape') {
    stopRecord()
    return
  }
  const combo = comboFromEvent(event)
  if (!combo) return
  if (!isSafeBareKey(combo)) {
    error.value = '字母与数字需要配合 Ctrl / Alt / Shift 使用。'
    return
  }
  const duplicate = actionsList.find(
    (action) => action !== pending.value && store.shortcuts[action] === combo
  )
  if (duplicate) {
    error.value = `与「${SHORTCUT_LABELS[duplicate]}」重复，换一个组合键。`
    return
  }
  store.shortcuts[pending.value] = combo
  error.value = ''
  stopRecord()
}

function resetShortcuts() {
  store.resetSection('shortcuts')
  error.value = ''
  stopRecord()
}

onBeforeUnmount(stopRecord)
</script>

<style scoped>
.sc-control {
  display: flex;
  align-items: center;
  gap: 6px;
}
.sc-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 32px;
  padding: 0 9px;
  border-radius: var(--sh-r-sm);
  border: 1px dashed var(--sh-line);
  background: var(--sh-glass-soft);
  color: var(--sh-text-2);
  font: inherit;
  font-size: 11.5px;
  font-weight: 600;
  cursor: pointer;
  transition: border-color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    box-shadow var(--sh-t) var(--sh-ease);
}
.sc-chip:hover {
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.sc-chip.is-recording {
  border-style: solid;
  border-color: var(--sh-accent);
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
  box-shadow: 0 0 0 3px var(--sh-accent-ring);
}
.sc-pulse {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--sh-accent);
  animation: sc-blink 1s ease-in-out infinite;
}
@keyframes sc-blink {
  50% {
    opacity: 0.25;
  }
}
.sc-error {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 4px 0 2px;
  font-size: 11.8px;
  color: var(--sh-danger);
}
.sc-error-inline {
  color: var(--sh-danger);
}
.sc-note {
  font-size: 11.8px;
  color: var(--sh-text-3);
}
.sc-rules {
  display: flex;
  flex-direction: column;
  gap: 11px;
  padding: 8px 0 10px;
}
.sc-rule {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
  color: var(--sh-text-3);
  line-height: 1.6;
}
.sc-rule span {
  margin-left: 2px;
}
</style>
