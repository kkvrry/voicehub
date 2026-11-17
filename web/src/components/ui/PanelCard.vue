<!-- 应用页的玻璃卡片：引擎设置、日志、生成参数、章节面板共用这一份。
     样式直接落在设置页那套 `.sh-card` 上——两处各写一套「半透明 + 高光边 +
     柔和阴影」，迟早只有一处被改到。 -->
<template>
  <section class="sh-card panel-card" :class="{ 'is-flush': flush }">
    <header v-if="title || $slots.head" class="sh-card-head">
      <div v-if="$slots.icon" class="sh-card-icon"><slot name="icon" /></div>
      <div class="sh-card-head-main">
        <h3 v-if="title" class="sh-card-title">{{ title }}</h3>
        <p v-if="desc" class="panel-card-desc">{{ desc }}</p>
      </div>
      <div v-if="$slots.head" class="sh-card-head-side"><slot name="head" /></div>
    </header>
    <div class="sh-card-body"><slot /></div>
    <footer v-if="$slots.foot" class="sh-card-foot"><slot name="foot" /></footer>
  </section>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    title?: string
    desc?: string
    /** 正文自带内边距（表格类内容贴边更整齐）。 */
    flush?: boolean
  }>(),
  { title: '', desc: '', flush: false }
)
</script>

<style scoped>
.panel-card {
  min-width: 0;
}
.panel-card-desc {
  margin: 4px 0 0;
  font-size: 11.9px;
  line-height: 1.62;
  color: var(--sh-text-3);
  max-width: 68ch;
}
.panel-card.is-flush :deep(.sh-card-body) {
  padding: 0;
}
.panel-card.is-flush :deep(.sh-card-head) {
  padding-bottom: 10px;
}
</style>
