<template>
  <!-- 音色弹窗的「合并」栏：勾选几个角色，挑一个统一的名字。外壳在 VoiceToolsDialog。 -->
  <div class="voice-panel merge-panel">
    <div class="merge-body">
      <div class="merge-bar">
        <span class="merge-note">勾选要合并的几个，再选一个统一用的名字。</span>
        <span class="merge-count-all">{{ picked.length }} / {{ roles.length }} 已选</span>
      </div>

      <!-- 与「分配」共用一套标签：勾选只是给标签盖个章，不分成两筐——
           合并本来就是随手挑几个，先分组反而要先想"它现在在哪一边"。 -->
      <div v-if="roles.length" class="merge-picks" role="group" aria-label="角色">
        <label
          v-for="item in roleList"
          :key="item.name"
          class="pick-chip"
          :class="{ on: picked.includes(item.name) }"
          :title="`${item.name} · ${item.rows} 行`"
        >
          <input v-model="picked" class="merge-check" type="checkbox" :value="item.name" />
          <span class="pick-box" aria-hidden="true">
            <Check :size="9" :stroke-width="3" />
          </span>
          <span class="pick-name">{{ item.name }}</span>
          <span class="pick-rows">{{ item.rows }} 行</span>
        </label>
      </div>
      <p v-else class="merge-hint">还没有可合并的角色：正文里至少要有两个说话人。</p>

      <div class="merge-row">
        <span class="merge-label">合并为</span>
        <EngineSelect
          :value="choice"
          :options="targetOptions"
          :disabled="picked.length < 2"
          :wheel="false"
          label="合并后的角色名"
          @change="choice = $event"
        />
      </div>
      <p class="merge-hint">
        合并后行表里的说话人统一成这个名字；这些行已生成的音频作废，重新生成时按新角色配音。
      </p>
    </div>

    <footer class="merge-foot">
      <span class="merge-spacer" />
      <button class="sh-btn is-ghost" @click="$emit('cancel')">取消</button>
      <button class="sh-btn is-primary" :disabled="!canSubmit" @click="submit">合并</button>
    </footer>
  </div>
</template>

<script setup>
// @ts-check
import { computed, ref, watch } from 'vue'
import { Check } from 'lucide-vue-next'
import EngineSelect from '../../components/EngineSelect.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  /** 可合并的角色：`[{ name, rows }]`，调用方已经把「旁白」去掉。 */
  roles: { type: Array, default: () => [] },
})
const emit = defineEmits(['cancel', 'merge'])

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const roleList = computed(() => /** @type {any[]} */ (props.roles))
const picked = ref(/** @type {string[]} */ ([]))
const choice = ref('')

const canSubmit = computed(() => picked.value.length >= 2 && picked.value.includes(choice.value))
const targetOptions = computed(() => picked.value.map(/** @param {any} name */ (name) => ({ key: name, label: name })))

/** 打开时清空：上一次的选择不该带进来。 */
watch(
  () => props.open,
  (open) => {
    if (!open) return
    picked.value = []
    choice.value = ''
  }
)

/** 取消勾选当前目标时，自动落到还选着的第一个（保证"合并为"永远是已勾的）。 */
watch(picked, (names) => {
  if (!names.includes(choice.value)) choice.value = names[0] || ''
})

function submit() {
  if (!canSubmit.value) return
  emit('merge', { names: [...picked.value], target: choice.value })
}
</script>

<style scoped>
.voice-panel {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}
.merge-body {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 10px;
  min-height: 0;
}
.merge-bar {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}
.merge-note {
  font-size: 11.6px;
  color: var(--sh-text-3);
}
.merge-count-all {
  flex: none;
  font-size: 10.8px;
  color: var(--sh-text-4);
}
/* 标签墙自己滚，弹窗高度与「分配」一致：切换栏目时不跳尺寸 */
.merge-picks {
  display: flex;
  flex: 1 1 auto;
  flex-wrap: wrap;
  align-content: flex-start;
  gap: 6px;
  min-height: 0;
  overflow-y: auto;
  padding: 8px;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-sm);
  background: var(--sh-field);
}
.pick-chip {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 5px 10px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  background: var(--sh-panel);
  color: var(--sh-text-2);
  font-size: 12px;
  cursor: pointer;
  transition: border-color var(--sh-t-fast) var(--sh-ease),
    background var(--sh-t-fast) var(--sh-ease), color var(--sh-t-fast) var(--sh-ease);
}
.pick-chip:hover {
  border-color: var(--sh-line-strong);
  background: var(--sh-field-focus);
}
.pick-chip.on {
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
  color: var(--sh-text);
}
.pick-chip.on .pick-name {
  color: var(--sh-accent);
}
/* 勾选框不额外占一行：native checkbox 藏起来，方框跟着标签走，键盘仍能操作 */
.merge-check {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}
.merge-check:focus-visible + .pick-box {
  outline: 2px solid var(--sh-accent-ring);
  outline-offset: 1px;
}
.pick-box {
  flex: none;
  width: 13px;
  height: 13px;
  display: grid;
  place-items: center;
  border: 1px solid var(--sh-line-strong);
  border-radius: var(--sh-r-2xs);
  background: var(--sh-field);
  color: var(--sh-accent-contrast);
}
.pick-box svg {
  opacity: 0;
}
.pick-chip.on .pick-box {
  border-color: var(--sh-accent);
  background: var(--sh-accent);
}
.pick-chip.on .pick-box svg {
  opacity: 1;
}
.pick-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}
.pick-rows {
  flex: none;
  font-size: 10.5px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}
.merge-row {
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
}
.merge-label {
  font-size: 12.2px;
  color: var(--sh-text-2);
}
.merge-hint {
  margin: 0;
  font-size: 10.8px;
  line-height: 1.5;
  color: var(--sh-text-4);
}
.merge-foot {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--sh-line-soft);
}
.merge-spacer {
  flex: 1;
}
</style>
