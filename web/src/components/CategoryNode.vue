<template>
  <!-- 左栏分类树的**一个节点**（递归组件）：一个节点 = 磁盘上一个真实的分组文件夹。
       点它按那条相对路径的前缀筛，子层跟着收进来。**只有目录才是节点**——直接放在
       扫描根那一层的音色不属于任何节点，点根那一行就能看到它们。
       层级靠三样东西说清：缩进、左侧那道竖线、以及可收起的折角。 -->
  <div class="tree-branch">
    <div
      class="tree-item sub tree-node has-guide"
      :class="{ active: isActive, 'is-last': isLast, 'is-leaf': !hasChildren }"
      :style="{ '--indent': indent + 'px', paddingLeft: indent + 'px' }"
      @click="pick"
    >
      <button
        v-if="hasChildren"
        type="button"
        class="tree-caret"
        :class="{ 'is-open': !collapsed }"
        :aria-expanded="!collapsed"
        :aria-label="collapsed ? `展开 ${node.name}` : `收起 ${node.name}`"
        @click.stop="collapsed = !collapsed"
      >
        <ChevronRight :size="12" :stroke-width="1.8" />
      </button>
      <span v-else class="tree-caret-space" aria-hidden="true" />
      <Folder :size="14" :stroke-width="1.8" class="tree-folder" aria-hidden="true" />
      <span class="tree-name">{{ node.name }}</span>
      <span class="count">{{ node.count }}</span>
    </div>
    <template v-if="hasChildren && !collapsed">
      <CategoryNode
        v-for="(child, index) in children"
        :key="child.path"
        :node="child"
        :root="root"
        :depth="depth + 1"
        :is-last="index === children.length - 1"
      />
    </template>
  </div>
</template>

<script setup>
// @ts-check
import { computed, ref } from 'vue'
import { ChevronRight, Folder } from 'lucide-vue-next'
import { store, actions } from '../store'

const props = defineProps({
  /** 树节点：{ name, path, count, children }（后端 usecase.tree 组好）。 */
  node: { type: Object, required: true },
  /** 这个节点挂在哪个扫描根下（筛选时与 path 一起回传）。 */
  root: { type: String, default: '' },
  /** 第几层：根目录下的分类是 1，每往下一层多 16px 缩进。 */
  depth: { type: Number, default: 1 },
  /** 同层里的最后一个：竖线在这里收成「└」，不再往下拖。 */
  isLast: { type: Boolean, default: false },
})

const isActive = computed(
  () =>
    store.selection.kind === 'local' &&
    (store.selection.root || '') === props.root &&
    (store.selection.category || '') === props.node.path
)

const children = computed(() => props.node.children || [])
const hasChildren = computed(() => children.value.length > 0)
const collapsed = ref(false)
const indent = computed(() => 12 + props.depth * 16)

function pick() {
  actions.select({ kind: 'local', root: props.root, category: props.node.path })
}
</script>
