<template>
  <!-- 与导入对话框同一处加载：`--sh-*` 令牌在 .app-shell 上，body 上拿不到。 -->
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div
        v-if="confirmState.open"
        class="vh-root confirm-root"
        @click.self="resolveConfirm(false)"
      >
        <div
          ref="card"
          class="vh-modal confirm-card"
          role="alertdialog"
          aria-modal="true"
          :aria-label="confirmState.title"
          tabindex="-1"
          @keydown.esc.stop="resolveConfirm(false)"
        >
          <h2 class="confirm-title">{{ confirmState.title }}</h2>
          <p v-if="confirmState.message" class="confirm-message">{{ confirmState.message }}</p>
          <input
            v-if="confirmState.input"
            ref="field"
            v-model="confirmState.value"
            class="sh-field"
            :type="confirmState.input.type"
            :placeholder="confirmState.input.placeholder || ''"
            @keydown.enter.prevent="resolveConfirm(true)"
          />
          <footer class="confirm-actions">
            <button class="sh-btn is-ghost" @click="resolveConfirm(false)">取消</button>
            <template v-if="confirmState.actions">
              <button
                v-for="action in confirmState.actions"
                :key="action.value"
                class="sh-btn"
                :class="action.danger ? 'is-danger' : 'is-primary'"
                :disabled="action.disabled"
                @click="resolveConfirm(action.value)"
              >
                {{ action.label }}
              </button>
            </template>
            <button
              v-else
              class="sh-btn"
              :class="confirmState.danger ? 'is-danger' : 'is-primary'"
              @click="resolveConfirm(true)"
            >
              {{ confirmState.confirmText }}
            </button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
// @ts-check
import { nextTick, ref, watch } from 'vue'
import { confirmState, resolveConfirm } from './confirm'
import { overlayTarget } from '../../composables/overlayTarget'

const card = ref(/** @type {HTMLElement | null} */ (null))
const field = ref(/** @type {HTMLInputElement | null} */ (null))

// 弹出来就把焦点收进卡片：有输入框就落在输入框，Esc 能关、回车即确认。
watch(
  () => confirmState.open,
  async (open) => {
    if (!open) return
    await nextTick()
    if (field.value) {
      field.value.focus()
      field.value.select?.()
    } else {
      card.value?.focus()
    }
  }
)
</script>

<style scoped>
/* 确认框可能开在别的弹层（分章预览、参数窗口）之上：统一抬高一层。 */
.confirm-root {
  z-index: 50;
}
.confirm-card {
  width: min(420px, 100%);
  padding: 22px 22px 18px;
  gap: 10px;
}
.confirm-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--sh-text);
}
.confirm-message {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--sh-text-3);
}
.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 6px;
}
</style>
