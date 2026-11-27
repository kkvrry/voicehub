<template>
  <Teleport to="body">
    <Transition name="vh-modal">
      <div v-if="open" class="vh-root" @click.self="close">
        <div ref="panelEl" class="vh-modal sh-dialog" role="dialog" aria-modal="true" :aria-label="plugin.label">
          <header class="sh-dialog-head">
            <h2 class="sh-dialog-title">{{ plugin.label }}</h2>
            <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="close">
              <X :size="16" :stroke-width="1.8" />
            </button>
          </header>

          <div class="sh-dialog-body">
            <ProducerConfig ref="form" :plugin="plugin" />
          </div>

          <footer class="sh-dialog-foot">
            <template v-if="resetAsk">
              <span class="plugin-dialog-hint">恢复本页默认？</span>
              <ActionButton variant="ghost" size="sm" @click="resetAsk = false">取消</ActionButton>
              <ActionButton variant="danger" size="sm" :loading="saving" @click="doReset">
                确认恢复
              </ActionButton>
            </template>
            <template v-else>
              <span v-if="dirty" class="plugin-dialog-hint">有未保存的改动</span>
              <ActionButton variant="ghost" size="sm" :disabled="!dirty" @click="resetAsk = true">
                恢复默认
              </ActionButton>
              <ActionButton
                variant="primary"
                size="sm"
                :loading="saving"
                :disabled="!dirty"
                @click="save"
              >
                保存
              </ActionButton>
            </template>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
/** 插件设置的唯一出口：抬头只写插件名，正文是插件自述的 schema 表单，动作固定三颗。
 *
 *  各类插件（生产方、引擎……）都用这一个弹窗：表单由 ``SchemaFields`` 按 schema 生成，
 *  这里不写任何插件分支；换成另一份 schema 也不用改这个文件。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import ActionButton from '../../components/ui/ActionButton.vue'
import ProducerConfig from './ProducerConfig.vue'
import { useModalA11y } from '../../composables/modalA11y'

/** 插件自描述的整包：表单只认 schema，其余字段透传给表单组件。 */
interface PluginSchema {
  key: string
  label: string
  [key: string]: unknown
}

const props = defineProps<{
  plugin: PluginSchema
  open: boolean
}>()

const emit = defineEmits<{ close: [] }>()

const form = ref<{
  dirty: boolean
  saving: boolean
  save: () => Promise<void>
  doReset: () => Promise<void>
} | null>(null)

const resetAsk = ref(false)
const dirty = computed(() => Boolean(form.value?.dirty))
const saving = computed(() => Boolean(form.value?.saving))

function close() {
  resetAsk.value = false
  emit('close')
}

async function save() {
  await form.value?.save()
}

async function doReset() {
  resetAsk.value = false
  await form.value?.doReset()
}

const panelEl = ref<HTMLElement | null>(null)
useModalA11y({ open: () => props.open, panel: panelEl, onClose: () => close() })

watch(
  () => props.open,
  (open) => {
    document.body.style.overflow = open ? 'hidden' : ''
  },
  { immediate: true }
)

onBeforeUnmount(() => {
  document.body.style.overflow = ''
})
</script>

<style scoped>
.plugin-dialog-hint {
  margin-right: auto;
  font-size: 11.5px;
  color: var(--sh-text-4);
}
</style>
