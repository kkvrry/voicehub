<template>
  <aside class="sider audio-rail">
    <div class="sider-body">
      <!-- 这一列只回答「哪一类音频」：全部 + 三类来源。生成物在盘上按引擎分目录，
           所以「生成」下面再列一层引擎——点引擎即把范围收到那台引擎。 -->
      <div
        class="tree-item"
        :class="{ active: store.audio.source === 'all' }"
        @click="pick('all')"
      >
        <AudioLines :size="14" :stroke-width="1.8" />
        <span class="tree-name">全部音频</span>
        <span class="count">{{ counts.all }}</span>
      </div>

      <div class="tree-section">来源</div>
      <template v-for="item in SOURCES" :key="item.key">
        <div
          class="tree-item"
          :class="{
            active: store.audio.source === item.key && !(item.key === 'engine' && store.audio.engine),
          }"
          :title="item.hint"
          @click="pick(item.key)"
        >
          <component :is="item.icon" :size="14" :stroke-width="1.8" />
          <span class="tree-name">{{ item.label }}</span>
          <span class="count">{{ counts[item.key] }}</span>
        </div>

        <!-- 引擎那一层：目录名就是引擎名，不再另取一套叫法。没有产物的引擎不列。 -->
        <div
          v-for="engine in item.key === 'engine' ? engines : []"
          :key="engine.label"
          class="tree-item sub"
          :class="{ active: store.audio.engine === engine.label }"
          @click="pickEngine(engine.label)"
        >
          <img v-if="iconOf(engine.label)" class="engine-icon" :src="iconOf(engine.label)" alt="" />
          <span v-else class="engine-dot" aria-hidden="true"></span>
          <span class="tree-name">{{ engine.label }}</span>
          <span class="count">{{ engine.count }}</span>
        </div>
      </template>
    </div>
  </aside>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import { AudioLines, Mic, Volume2, Sparkles } from 'lucide-vue-next'
import { actions, store } from '../store'
import { SOURCES as RAW_SOURCES, engineCounts, sourceCounts } from '../store/audio'

/** @type {Record<string, any>} */
const ICONS = { record: Mic, loopback: Volume2, engine: Sparkles }
const SOURCES = RAW_SOURCES.map((item) => ({ ...item, icon: ICONS[item.key] }))

const counts = computed(() => sourceCounts())
const engines = computed(() => engineCounts())

/** 引擎图标按目录名找：产物目录用的是 label，不是 key。 */
/** @param {string} label */
function iconOf(label) {
  const found = [...store.engines, ...store.engineCatalog].find((item) => item.label === label)
  return found?.icon_url || ''
}

/** @param {string} key */
function pick(key) {
  actions.pickSource(key)
}

/** @param {string} label */
function pickEngine(label) {
  actions.pickEngine(label)
}
</script>

<style scoped>
.audio-rail :deep(.tree-item) {
  gap: 8px;
}
.audio-rail :deep(.tree-item > svg) {
  color: var(--sh-text-3);
  flex: none;
}
.audio-rail :deep(.tree-item.active > svg) {
  color: var(--sh-accent);
}
.audio-rail :deep(.tree-item.sub) {
  padding-left: 30px;
  font-size: 13px;
}
.engine-dot {
  width: 14px;
  height: 14px;
  flex: none;
  border-radius: 4px;
  border: 1px solid var(--sh-line);
}
</style>
