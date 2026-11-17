<template>
  <SettingsSection
    id="services"
    title="服务"
    :show-actions="false"
  >
    <template #icon><ServerCog :size="16" :stroke-width="1.8" /></template>

    <div v-if="producers.length" class="service-grid">
      <ProducerCard v-for="plugin in producers" :key="plugin.key" :plugin="plugin" />
    </div>

    <div v-else class="sh-empty">
      <ServerCog :size="20" :stroke-width="1.8" />
      <strong>没有可用的服务</strong>
      <span>后端启动时扫描 app/producers/builtin/。</span>
    </div>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { ServerCog } from 'lucide-vue-next'
import { actions, store } from '../../store'
import SettingsSection from '../ui/SettingsSection.vue'
import ProducerCard from './ProducerCard.vue'

interface ProducerRow {
  key: string
  label: string
  order?: number
  [key: string]: unknown
}

const producers = computed(() =>
  ([...(store.producers || [])] as ProducerRow[]).sort(
    (left, right) =>
      (left.order || 0) - (right.order || 0) || left.key.localeCompare(right.key)
  )
)

onMounted(() => {
  if (!store.producers.length) void actions.loadProducers()
})
</script>

<style scoped>
.service-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
</style>
