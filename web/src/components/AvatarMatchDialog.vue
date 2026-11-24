<template>
  <Teleport :to="overlayTarget">
    <Transition name="vh-modal">
      <div v-if="match.open" class="vh-root" @click.self="close">
        <div
          ref="dialogEl"
          class="vh-modal sh-dialog avatar-match"
          role="dialog"
          aria-modal="true"
          aria-label="匹配头像"
          tabindex="-1"
        >
          <header class="sh-dialog-head">
            <h2 class="sh-dialog-title">匹配头像</h2>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="close">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="sh-dialog-body">
            <p class="am-note">
              当前列表缺头像的 {{ match.total }} 个音色，已有图的不覆盖；预计约
              {{ minutes }} 分钟，可在提示里停止。
            </p>

            <div class="am-group">
              <div class="am-cap">搜索来源</div>
              <AvatarSourceChips v-model="match.source" :sources="store.avatarSources" />
            </div>
          </div>

          <footer class="sh-dialog-foot">
            <span class="am-hint">{{ hint }}</span>
            <span class="spacer" />
            <ActionButton @click="close">取消</ActionButton>
            <ActionButton variant="primary" :disabled="!match.source" @click="start">
              开始匹配
            </ActionButton>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import { actions, store } from '../store'
import { overlayTarget } from '../composables/overlayTarget'
import ActionButton from './ui/ActionButton.vue'
import AvatarSourceChips from './AvatarSourceChips.vue'

const match = store.avatarMatch
const dialogEl = ref<HTMLElement | null>(null)

const minutes = computed(() => Math.max(1, Math.round((match.total * 3) / 60)))
const hint = computed(
  () => store.avatarSources.find((item) => item.key === match.source)?.hint || ''
)

function close() {
  actions.closeAvatarMatch()
}

function start() {
  void actions.startAvatarMatch()
}

/** @param {KeyboardEvent} event */
function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  event.preventDefault()
  close()
}

watch(
  () => match.open,
  (open) => {
    if (open) {
      window.addEventListener('keydown', onKeydown)
      void nextTick(() => dialogEl.value?.focus())
    } else {
      window.removeEventListener('keydown', onKeydown)
    }
  }
)

onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<style scoped>
.avatar-match {
  width: min(520px, 100%);
}
.am-note {
  margin: 0 0 16px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--sh-text-3);
}
.am-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.am-cap {
  font-size: 11.8px;
  font-weight: 600;
  color: var(--sh-text-3);
}
.am-hint {
  font-size: 11.8px;
  color: var(--sh-text-4);
}
.spacer {
  flex: 1;
}
</style>
