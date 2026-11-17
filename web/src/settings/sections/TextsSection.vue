<template>
  <SettingsSection id="texts" title="文案" :show-actions="false">
    <template #icon><Quote :size="16" :stroke-width="1.8" /></template>

    <SettingCard title="文案分组" keywords="文案 句子 分组 生成样例 试听 诗句 文学 烂梗 默认">
      <template #icon><Type :size="16" :stroke-width="1.8" /></template>
      <template #head>
        <ActionButton variant="ghost" size="sm" aria-haspopup="dialog" @click="openDialog">
          <Pencil :size="14" :stroke-width="1.8" />
          编辑
        </ActionButton>
      </template>
      <div class="texts-preview">
        <span class="texts-preview-meta">{{ summary }}</span>
        <div v-if="groupPreview.length" class="texts-preview-chips">
          <span v-for="(group, at) in groupPreview" :key="at" class="sh-chip">
            {{ group.name || '未命名' }}
            <span class="texts-preview-count">· {{ group.lines.filter((line) => line.trim()).length }}</span>
          </span>
          <span v-if="groupRest" class="sh-chip">+{{ groupRest }}</span>
        </div>
      </div>
    </SettingCard>

    <Teleport to="body">
      <Transition name="vh-modal">
        <div v-if="dialogOpen" class="vh-root" @click.self="closeDialog">
          <div class="vh-modal sh-dialog" role="dialog" aria-modal="true" aria-label="文案分组">
            <header class="sh-dialog-head">
              <h2 class="sh-dialog-title">文案分组</h2>
              <button
                class="sh-btn is-icon"
                title="重新读取"
                aria-label="重新读取"
                :disabled="loading"
                @click="reload"
              >
                <RefreshCw :size="15" :stroke-width="1.8" />
              </button>
              <button class="sh-btn is-icon" title="关闭" aria-label="关闭" @click="closeDialog">
                <X :size="16" :stroke-width="1.8" />
              </button>
            </header>

            <div class="sh-dialog-body">
              <div v-if="groups.length" class="sh-list texts-list">
                <div v-for="(group, at) in groups" :key="at" class="texts-group">
                  <div class="texts-group-head">
                    <!-- 组名是标题不是表单：平时看是一行字，点进去才是改名（同资源管理器重命名）。 -->
                    <input
                      v-model="group.name"
                      class="texts-name"
                      type="text"
                      placeholder="未命名分组"
                      title="点这里改组名"
                      :aria-label="`第 ${at + 1} 组的组名`"
                    />
                    <button
                      type="button"
                      class="sh-btn is-icon is-sm"
                      :title="`删掉「${group.name || '这一组'}」`"
                      :aria-label="`删掉第 ${at + 1} 组`"
                      @click="groups.splice(at, 1)"
                    >
                      <X :size="14" :stroke-width="1.8" />
                    </button>
                  </div>
                  <div class="texts-lines">
                    <div v-for="(line, index) in group.lines" :key="index" class="texts-line">
                      <textarea
                        class="sh-field texts-line-field"
                        rows="1"
                        :value="line"
                        :aria-label="`${group.name || '这一组'} 第 ${index + 1} 句`"
                        placeholder="念出来的一句话"
                        @input="group.lines[index] = ($event.target as HTMLTextAreaElement).value"
                      ></textarea>
                      <button
                        type="button"
                        class="sh-btn is-icon is-sm"
                        :title="`删掉第 ${index + 1} 句`"
                        :aria-label="`删掉第 ${index + 1} 句`"
                        @click="group.lines.splice(index, 1)"
                      >
                        <X :size="14" :stroke-width="1.8" />
                      </button>
                    </div>
                  </div>
                  <button
                    type="button"
                    class="sh-btn is-ghost is-sm texts-add"
                    @click="group.lines.push('')"
                  >
                    <Plus :size="13" :stroke-width="1.8" />
                    加一句
                  </button>
                </div>
              </div>
              <div v-else class="sh-empty">
                <Quote :size="20" :stroke-width="1.8" />
                <strong>{{ error || '文案库是空的' }}</strong>
                <span>{{ error ? '启动本地服务后再试一次。' : '加一组，生成样例时才有得挑。' }}</span>
              </div>
            </div>

            <footer class="sh-dialog-foot">
              <ActionButton variant="ghost" size="sm" @click="addGroup">
                <Plus :size="14" :stroke-width="1.8" />
                添加分组
              </ActionButton>
              <ActionButton variant="ghost" size="sm" @click="reset">恢复默认</ActionButton>
              <ActionButton variant="soft" size="sm" :loading="saving" @click="save">
                保存文案
              </ActionButton>
            </footer>
          </div>
        </div>
      </Transition>
    </Teleport>
  </SettingsSection>
</template>

<script setup lang="ts">
/** 文案库：生成样例时"挑一句现成的"那几组句子。
 *
 *  它从前**写死在前端**（`presets.js`），加一句要改代码重构建；而"念什么"是
 *  每个人自己的口味。现在这是设置里的一份配置：分组增删、改完保存，立刻生效。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Pencil, Plus, Quote, RefreshCw, Type, X } from 'lucide-vue-next'
import { actions, say, store as appStore } from '../../store'
import ActionButton from '../../components/ui/ActionButton.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingsSection from '../ui/SettingsSection.vue'

interface TextGroup {
  name: string
  lines: string[]
}

const groups = ref<TextGroup[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')

/** 哪条文案正开着弹窗：编辑态与浏览态分开，卡上只留数量。 */
const dialogOpen = ref(false)

const summary = computed(() => {
  if (!groups.value.length) return '尚未添加'
  const lines = groups.value.reduce((total, group) => total + group.lines.filter((line) => line.trim()).length, 0)
  return `${groups.value.length} 组 · ${lines} 句`
})

/** 卡上列出前几组：光看「6 组 · 48 句」不知道是哪几组。 */
const groupPreview = computed(() => groups.value.slice(0, 5))
const groupRest = computed(() => Math.max(0, groups.value.length - 5))

function openDialog() {
  dialogOpen.value = true
  // 遮罩盖住页面，滚轮仍会滚到背后的设置页。
  document.body.style.overflow = 'hidden'
  document.addEventListener('keydown', onKeydown)
}

function closeDialog() {
  dialogOpen.value = false
  document.body.style.overflow = ''
  document.removeEventListener('keydown', onKeydown)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  event.stopPropagation()
  closeDialog()
}

onBeforeUnmount(() => {
  document.body.style.overflow = ''
  document.removeEventListener('keydown', onKeydown)
})

/** 界面上改的是**草稿**：保存之前后端那一份没动（与目录那一栏同一条规矩）。 */
function draft() {
  groups.value = (appStore.texts || []).map((row) => ({
    name: String(row.name || ''),
    lines: [...(row.lines || [])].map(String),
  }))
}

async function reload() {
  loading.value = true
  error.value = ''
  try {
    await actions.loadTexts()
    draft()
  } catch (err) {
    error.value = String((err as Error)?.message || err)
  } finally {
    loading.value = false
  }
}

function addGroup() {
  groups.value.push({ name: '', lines: [''] })
}

async function save() {
  saving.value = true
  try {
    await actions.saveTexts(groups.value)
    draft()
    say('文案已保存')
  } catch (err) {
    say(String((err as Error)?.message || err))
  } finally {
    saving.value = false
  }
}

/** 恢复默认会覆盖这里写的东西，所以先问一句。 */
async function reset() {
  if (!window.confirm('换成出厂那一份？\n\n现在这几组会被覆盖。')) return
  try {
    await actions.resetTexts()
    draft()
    say('已恢复默认文案')
  } catch (err) {
    say(String((err as Error)?.message || err))
  }
}

onMounted(reload)
</script>

<style scoped>
.texts-preview {
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.texts-preview-meta {
  font-size: 12px;
  color: var(--sh-text-3);
}
.texts-preview-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.texts-preview-chips .sh-chip {
  height: 22px;
  padding: 0 9px;
  font-size: 11px;
  font-weight: 500;
}
.texts-preview-count {
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}
.sh-dialog-foot {
  justify-content: flex-end;
}
/* 弹窗里不再分组套卡：组名当小标题，句子一行一条，靠间距分节。 */
.texts-list {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.texts-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.texts-group-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.texts-name {
  flex: 1;
  min-width: 0;
  padding: 2px 0;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  font: inherit;
  font-size: 12.6px;
  font-weight: 650;
  color: var(--sh-text-1);
  transition: border-color var(--sh-t-fast), background var(--sh-t-fast);
}
.texts-name:hover {
  background: var(--sh-hover);
}
.texts-name:focus {
  outline: none;
  border-color: var(--sh-accent-ring);
  background: var(--sh-soft);
}
.texts-name::placeholder {
  color: var(--sh-text-4);
  font-weight: 400;
}
.texts-lines {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.texts-line {
  display: flex;
  align-items: flex-start;
  gap: 6px;
}
/* 句子是正文不是表单：平时没有边框，指着或聚焦时才成形。 */
.texts-line-field {
  height: auto;
  min-height: 30px;
  padding: 5px 8px;
  font-size: 12.4px;
  line-height: 1.55;
  resize: none;
  border-color: transparent;
  background: transparent;
  field-sizing: content;
}
.texts-line-field:hover {
  border-color: var(--sh-line-soft);
}
.texts-line-field:focus {
  border-color: var(--sh-accent-ring);
  background: var(--sh-field);
}
/* 删除键弱显：看得到入口，又不与正文抢眼。 */
.texts-line .sh-btn {
  opacity: 0.3;
  transition: opacity var(--sh-t-fast) var(--sh-ease);
}
.texts-line:hover .sh-btn,
.texts-line:focus-within .sh-btn {
  opacity: 1;
}
.texts-add {
  align-self: flex-start;
  margin-top: 2px;
  padding-inline: 6px;
  color: var(--sh-text-4);
}
</style>
