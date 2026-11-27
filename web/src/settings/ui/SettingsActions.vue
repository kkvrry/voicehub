<template>
  <!-- 保存与恢复默认：挂在当前分组抬头的右上角（原来放「即时生效」那类标签的位置）。
       按钮自己的可用状态就是"有没有未保存的改动"，不再另写一行状态字。 -->
  <div class="sh-sec-actions">
    <template v-if="confirmReset">
      <span class="sh-sec-confirm">恢复全部默认？</span>
      <ActionButton variant="ghost" size="sm" @click="confirmReset = false">取消</ActionButton>
      <ActionButton variant="danger" size="sm" @click="doReset">确认恢复</ActionButton>
    </template>
    <template v-else>
      <ActionButton variant="ghost" size="sm" @click="confirmReset = true">恢复默认</ActionButton>
      <ActionButton
        variant="primary"
        size="sm"
        :loading="settings.saving"
        :disabled="!settings.dirty"
        :title="settings.dirty ? '保存并同步到本地服务' : '没有需要保存的改动'"
        @click="save"
      >
        <Save v-if="!settings.saving" :size="14" :stroke-width="1.8" aria-hidden="true" />
        保存设置
      </ActionButton>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { Save } from 'lucide-vue-next'
import { useSettingsStore } from '../../store/settings'
import ActionButton from '../../components/ui/ActionButton.vue'

const settings = useSettingsStore()
const confirmReset = ref(false)

async function save() {
  confirmReset.value = false
  await settings.save()
}

function doReset() {
  confirmReset.value = false
  settings.resetAll()
}
</script>
