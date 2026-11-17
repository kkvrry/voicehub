<template>
  <aside class="sider">
    <div class="sider-brand">
      <span class="logo">♪</span>
      <span>VoiceHub</span>
    </div>
    <div class="sider-body">
      <!-- 换页面的事都收进左侧导航了（音色库 / 生成 / 引擎 / 日志 / 设置）；
           这一列只回答"我有哪些音色"。 -->
      <div
        class="tree-item"
        :class="{ active: isActive({ kind: 'all' }) }"
        @click="pick({ kind: 'all' })"
      >
        <span>全部音色</span>
        <!-- 重扫就在"全部音色"后面：它管的就是这一整份列表，放到脚注里反而离得远。
             只留图标，文字说明交给 title / aria-label。 -->
        <button
          type="button"
          class="tree-rescan"
          :class="{ 'is-spinning': rescanning }"
          title="重扫全部根目录"
          aria-label="重扫全部根目录"
          @click.stop="rescan"
        >
          <RefreshCw :size="12" :stroke-width="1.8" />
        </button>
        <!-- 库里一共有多少条（含各引擎清单），不是各扫描根相加——后者漏掉平台音色。 -->
        <span class="count">{{ totalCount }}</span>
      </div>

      <!-- 一台引擎自己的那一层音色（云端清单）。**这是音色来源，不是引擎管理入口**：
           本地还没配扫描目录时，这里是唯一能点开的"文件夹"。
           引擎面板的入口只在「引擎」页，不在这条清单里。 -->
      <div
        v-for="engine in levelRows"
        :key="'level-' + engine.source"
        class="tree-item engine-voices"
        :class="{ active: isActive({ kind: 'platform', name: engine.source }) }"
        :title="levelTitle(engine)"
        @click="pick({ kind: 'platform', name: engine.source })"
      >
        <img v-if="engine.icon_url" class="engine-icon" :src="engine.icon_url" alt="" />
        <span v-else class="engine-icon placeholder">◻</span>
        <span class="engine-label">{{ engine.label }}</span>
        <span v-if="engine.kind_label" class="badge platform">{{ engine.kind_label }}</span>
        <span class="count">{{ engine.total }}</span>
      </div>

      <!-- 设计音色：一条**跨引擎**的来源，所以既不挂在某台引擎下面，也不是扫描根。
           它有自己的文件夹形态（点一下只看这一组），但标识另设：一枚圆角小牌里放 ✦，
           与引擎的方形图标、扫描根的线框文件夹都不同，扫一眼就分得清。 -->
      <div
        v-if="designCount !== null"
        class="tree-item tree-design"
        :class="{ active: isActive({ kind: 'design' }) }"
        title="设计音色：按一句描述现场生成，不占磁盘目录；支持音色设计的引擎都能用"
        @click="pick({ kind: 'design' })"
      >
        <span class="design-mark" aria-hidden="true">
          <Sparkles :size="11" :stroke-width="1.9" />
        </span>
        <span class="tree-name">设计音色</span>
        <span class="count">{{ designCount }}</span>
      </div>

      <!-- 只有用户的扫描根。引擎的"音色保存目录"不在这里——它属于那台引擎，
           在引擎面板的「音色」分区里说。左栏这一列说的只是"我有哪些音色"。 -->
      <template v-if="store.tree.roots.length">
        <div class="tree-section">扫描目录</div>
      </template>
      <div v-for="root in store.tree.roots" :key="root.path" class="tree-branch">
        <div
          class="tree-item tree-node tree-root"
          :class="{
            active: isActive({ kind: 'local', root: root.path }),
            missing: !root.exists,
            'engine-dir': Boolean(root.engine),
          }"
          :title="rootTitle(root)"
          :style="{ '--indent': '10px', paddingLeft: '10px' }"
          @click="pick({ kind: 'local', root: root.path })"
        >
          <!-- 有子目录才给折角；没有的留同宽空位，名字才对得齐。 -->
          <button
            v-if="root.children && root.children.length"
            type="button"
            class="tree-caret"
            :class="{ 'is-open': !isCollapsed(root.path) }"
            :aria-expanded="!isCollapsed(root.path)"
            :aria-label="isCollapsed(root.path) ? `展开 ${rootName(root.path)}` : `收起 ${rootName(root.path)}`"
            @click.stop="toggleRoot(root.path)"
          >
            <ChevronRight :size="12" :stroke-width="1.8" />
          </button>
          <span v-else class="tree-caret-space" aria-hidden="true" />
          <!-- 引擎指定的音色保存目录：图标、名字、形态徽标全照那台引擎来——
               只写文件夹名的话，它看起来就像一个普通目录。 -->
          <template v-if="root.engine">
            <img
              v-if="engineIcon(root.engine.key)"
              class="engine-icon"
              :src="engineIcon(root.engine.key)"
              alt=""
            />
            <span v-else class="engine-icon placeholder">◻</span>
            <span class="engine-label">{{ root.engine.label }}</span>
            <span v-if="root.engine.kind_label" class="badge platform">
              {{ root.engine.kind_label }}
            </span>
          </template>
          <template v-else>
            <Folder :size="14" :stroke-width="1.8" class="tree-folder" aria-hidden="true" />
            <span class="tree-name">{{ rootName(root.path) }}</span>
          </template>
          <span class="count">{{ root.total }}</span>
        </div>
        <template v-if="!isCollapsed(root.path)">
          <CategoryNode
            v-for="(node, index) in root.children"
            :key="root.path + '/' + node.path"
            :node="node"
            :root="root.path"
            :depth="1"
            :is-last="index === root.children.length - 1"
          />
        </template>
      </div>
      <div v-if="!store.tree.roots.length" class="tree-item" @click="openAppSettings()">
        <span style="color: var(--muted)">尚未配置扫描目录，点击添加</span>
      </div>

      <!-- 引擎的管理入口不在这里：它属于「引擎」页（第二列是插件清单）。
           这一列只回答"我有哪些音色"，所以引擎只在上面作为音色来源出现。 -->

      <!-- 停用的引擎：不再参与生成，但库里可能留着它的音色。不出现的话那些音色
           会凭空消失，所以单列一段，点一下 = 只看它们。 -->
      <template v-if="disabledRows.length">
        <div class="tree-divider"></div>
        <div class="tree-section">已停用的引擎</div>
        <div
          v-for="engine in disabledRows"
          :key="'off-' + engine.source"
          class="tree-item orphan"
          :class="{ active: isActive({ kind: 'platform', name: engine.source }) }"
          :title="`已停用：${engine.total} 条音色仍在库里。点一下只看这些音色。`"
          @click="showLeftovers(engine)"
        >
          <img v-if="engine.icon_url" class="engine-icon" :src="engine.icon_url" alt="" />
          <span v-else class="engine-icon placeholder">◻</span>
          <span class="engine-label">{{ engine.label }}</span>
          <span class="badge platform">已停用</span>
          <span class="count">{{ engine.total }}</span>
        </div>
      </template>

      <!-- 插件已经不在了的来源（音色还留在库里）：没有配置、没有清单、没有面板，
           不混在音色里，单列一段，点一下 = 只看它们留下的音色。
           只有存在时才渲染。 -->
      <template v-if="orphanRows.length">
        <div class="tree-divider"></div>
        <div class="tree-section">已卸载的引擎</div>
        <div
          v-for="engine in orphanRows"
          :key="engine.source"
          class="tree-item orphan"
          :class="{ active: isActive({ kind: 'platform', name: engine.source }) }"
          :title="engine.kind_note"
          @click="showLeftovers(engine)"
        >
          <span class="engine-icon placeholder">◻</span>
          <span class="engine-label">{{ engine.label }}</span>
          <span class="badge platform">{{ engine.kind_label }}</span>
        </div>
      </template>
    </div>
  </aside>
</template>

<script setup>
// @ts-check
import { computed, reactive, ref } from 'vue'
import { ChevronRight, Folder, RefreshCw, Sparkles } from 'lucide-vue-next'
import { store, actions } from '../store'
import CategoryNode from './CategoryNode.vue'

/** 哪些扫描根被收起来了：只影响「看哪些音色」这一列，不动选择本身。 */
const collapsedRoots = reactive(new Set())

/**
 * @param {any} path
 */
function isCollapsed(path) {
  return collapsedRoots.has(path)
}

/**
 * @param {any} path
 */
function toggleRoot(path) {
  if (collapsedRoots.has(path)) collapsedRoots.delete(path)
  else collapsedRoots.add(path)
}

const totalCount = computed(
  () =>
    // 后端给的"库里一共多少条"；老后端没这个字段时退回累加各扫描根。
    store.tree.total ?? store.tree.roots.reduce((sum, root) => sum + root.total, 0)
)

/** 设计音色那一行：老后端没有这个字段时整行不出现（不摆一行点开是空的）。
 *  0 条照样出现——它是一组固定的来源，不是"扫出来的东西"；空着也是在说
 *  "这里能放设计音色"。 */
const designCount = computed(() =>
  typeof store.tree.design === 'number' ? store.tree.design : null
)

/**
 * @param {string} key
 */
function engineIcon(key) {
  // 停用的引擎不在 `store.engines` 里，图标要从适配清单取——否则那一行只剩一个方框。
  return actions.engineIconOf(key)
}

/**
 * 悬停时说什么。引擎指定的保存目录要先说清它属于哪台引擎——它的文件夹名常常
 * 与引擎同名，光摆路径会像用户自己加的扫描根。
 *
 * @param {any} root
 */
function rootTitle(root) {
  if (!root.exists) return `目录不存在：${root.path}`
  if (root.engine) return `${root.engine.label} 的音色保存目录：${root.path}`
  return root.path
}

/** 已启用的引擎：它们自带的音色清单是这一列的"文件夹"。 */
const engineRows = computed(() => store.tree.engines.filter(/** @param {any} row */ (row) => row.enabled))
/** 有清单能力的那几台才有这一层（判据是 `can_list`，不是通道）。
 *  本地音色不走这里——它们由下面的扫描根列。 */
const levelRows = computed(() => engineRows.value.filter((engine) => engine.can_list))
/**
 * 「全部音色」里这一层悬停时说什么：这层是什么、有几条、以及它是引擎给的。
 *
 * @param {any} engine
 */
function levelTitle(engine) {
  return `${engine.label} 的全部音色：${engine.total} 条（引擎自带清单）`
}

/** 插件已经没了的来源：点一下只是筛音色，所以单列。判据是后端给的 `orphan`。 */
const orphanRows = computed(() => store.tree.engines.filter(/** @param {any} row */ (row) => row.orphan))
/** 用户停用了、但库里还有它的音色：同样只是筛音色。`orphan` 先排掉——它也没启用。 */
const disabledRows = computed(() =>
  store.tree.engines.filter(/** @param {any} row */ (row) => !row.enabled && !row.orphan)
)

/**
 * 选中标记在哪一页都摆：这一列的选择**就是**右侧（生成页是那一列音色卡）
 * 当前筛的是哪一组，换页并不改变这件事。
 *
 * @param {any} node
 */
function isActive(node) {
  const current = store.selection
  if (node.kind !== current.kind) return false
  if (node.kind === 'local') {
    return (
      (node.root || '') === (current.root || '') &&
      (node.category || '') === (current.category || '')
    )
  }
  if (node.kind === 'platform') return node.name === current.name
  return true
}

/**
 * @param {any} path
 */
function rootName(path) {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || path
}

/**
 * @param {any} node
 */
async function pick(node) {
  await actions.select(node)
}

/** 打开「设置」。实现在 store 里（一个产地）。 */
function openAppSettings() {
  actions.openSettings()
}

/**
 * 已卸载的来源：唯一有意义的动作是看它留下的音色。
 *
 * @param {any} engine
 */
async function showLeftovers(engine) {
  await actions.select({ kind: 'platform', name: engine.source })
}

/** 重扫期间图标转起来：目录多的时候要几秒，不给反馈会以为没点上。 */
const rescanning = ref(false)

async function rescan() {
  if (rescanning.value) return
  rescanning.value = true
  try {
    await actions.rescan()
  } finally {
    rescanning.value = false
  }
}
</script>
