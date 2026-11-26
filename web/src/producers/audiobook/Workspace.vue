<template>
  <section class="app-panel producer-panel">

    <div v-if="activeJob" class="producer-job" role="status" aria-live="polite">
      <span class="job-kind">{{ jobLabel }}</span>
      <span class="job-count">
        {{ state.job.done }}/{{ state.job.total }}
        <template v-if="state.job.failed">· 失败 {{ state.job.failed }}</template>
      </span>
      <span class="job-message">{{ state.job.message }}</span>
      <button class="sh-btn is-sm is-ghost" @click="cancel">取消</button>
    </div>
    <!-- 同步操作使用即时说明；合并期间叠加后端阶段进度，保留逐章计数。 -->
    <div v-else-if="syncBusy" class="producer-job" role="status" aria-live="polite">
      <span class="job-kind">{{ syncBusy.kind }}</span>
      <span v-if="busySeconds" class="job-count">已等待 {{ busySeconds }} 秒</span>
      <span class="job-message">{{ syncBusy.message }}</span>
      <button
        v-if="canStopMerge"
        class="sh-btn is-sm is-ghost"
        :disabled="state.mergeStopping"
        title="停止当前合并；已完成章节保留，当前章不写入成品"
        @click="stopMerge"
      >
        {{ state.mergeStopping ? '正在停止…' : '停止' }}
      </button>
    </div>

    <!-- 还没有项目时只留一个入口：导入表单放在对话框里，不占工作区。 -->
    <div v-if="!detail" class="panel-body producer-empty">
      <div class="empty">
        <span class="empty-icon"><BookOpen :size="20" :stroke-width="1.8" /></span>
        <div class="empty-title">还没有书稿</div>
        <div class="empty-note">
          TXT / Markdown / EPUB / MOBI / AZW3 都可以；SRT 字幕会按时间轴直接成项目（不分章）。
        </div>
        <div class="empty-actions">
          <ActionButton variant="primary" @click="openProjectManager">导入书稿</ActionButton>
        </div>
      </div>
    </div>

    <!-- 有项目就永远停在这五张步骤卡上：整张卡就是入口，所有操作都从卡上弹出。 -->
    <template v-else>
      <section class="producer-flow" aria-label="制作流程">
        <header class="producer-flow-head">
          <span class="producer-flow-title">制作流程</span>
          <span class="producer-flow-note">按顺序完成；任何一步都可以从这里重新打开</span>
        </header>
        <div class="producer-steps" :style="{ '--producer-step-count': steps.length }">
          <button
            v-for="(step, index) in steps"
            :key="step.key"
            type="button"
            class="producer-step"
            :class="{ 'is-blocked': stepBlocked(step.key) }"
            :disabled="busy || stepBlocked(step.key)"
            :title="stepTitle(step.key)"
            :aria-label="`${stepLabel(step)}：${stepTitle(step.key)}`"
            @click="openStep(step.key)"
          >
            <span class="step-icon">
              <component :is="stepIcon(step.key)" :size="16" :stroke-width="1.8" />
            </span>
            <span class="step-main">
              <span class="step-title-row">
                <span class="step-ord">{{ stepOrdinal(index) }}</span>
                <span class="step-label">{{ stepLabel(step) }}</span>
              </span>
              <span class="step-note" :title="step.note">{{ step.note }}</span>
            </span>
          </button>
        </div>
      </section>

      <div class="panel-body producer-body">
        <div class="producer-main">
          <div v-if="!chapter" class="content-hint">
            {{ detail.chapters.length ? '从右侧目录选一章继续。' : '还没有章节：点「分章及文本处理」卡片上的启动按钮开始。' }}
          </div>
          <div v-if="chapter" class="chapter-head">
            <div class="chapter-name" title="滚轮切换章节" @wheel.prevent="stepChapter">
              {{ chapter.title }}
            </div>
            <!-- 逐行试听：播的是当前显示队列（筛选后的行），不是整章硬编码。 -->
            <button
              v-if="rows.length"
              type="button"
              class="chapter-play"
              :disabled="!rowTracks.length"
              :title="playQueueHint"
              aria-label="按顺序播放当前显示的行音频"
              @click="playChapterRows"
            >
              <Play :size="12" :stroke-width="1.8" fill="currentColor" />
            </button>
            <div class="chapter-meta">{{ rows.length }} 行 · {{ stageLabel(chapter.stage) }}</div>
            <div class="view-switch" role="group" aria-label="显示方式">
              <button
                type="button"
                class="view-switch-btn"
                :class="{ on: view === 'table' }"
                :disabled="!rows.length"
                :title="rows.length ? '按表格编辑' : '本章还没有拆句'"
                aria-label="表格显示"
                :aria-pressed="view === 'table'"
                @click="setView('table')"
              >
                <Table2 :size="14" :stroke-width="1.8" />
                <span>表格</span>
              </button>
              <button
                type="button"
                class="view-switch-btn"
                :class="{ on: view === 'chapter' }"
                title="按章节阅读"
                aria-label="章节显示"
                :aria-pressed="view === 'chapter'"
                @click="setView('chapter')"
              >
                <AlignLeft :size="14" :stroke-width="1.8" />
                <span>章节</span>
              </button>
            </div>
            <button v-if="chapter.audio" class="sh-btn is-sm is-ghost" @click="playChapter">
              <Play :size="14" :stroke-width="1.8" />
              试听成品
            </button>
          </div>

          <div
            v-if="chapter && view === 'chapter'"
            class="chapter-source"
            :style="tableBoxHeight ? { minHeight: `${tableBoxHeight}px` } : undefined"
          >
            <div class="chapter-source-head">
              <span class="chapter-source-meta">
                {{
                  rows.length
                    ? filterActive
                      ? `按行映射 · 显示 ${filteredRows.length} / ${rows.length} 行 · 只读`
                      : `按行映射 · ${rows.length} 行 · 只读`
                    : '本章还没有拆句 · 显示章节正文'
                }}
              </span>
            </div>
            <!-- 阅读视图也吃同一条筛选链：看到的就是试听会播的那一批行。 -->
            <div
              v-if="filteredRows.length"
              ref="chapterLines"
              class="chapter-source-lines"
            >
              <div
                v-for="row in filteredRows"
                :key="row.id"
                class="chapter-line"
                :class="{ 'is-dialogue': row.kind === 'dialogue' }"
                :data-row-id="row.id"
                @contextmenu="onRowMenu(row, $event)"
              >
                <span class="chapter-line-ord">{{ row.ordinal + 1 }}</span>
                <span class="chapter-line-speaker">{{ row.speaker || '旁白' }}</span>
                <span class="chapter-line-text">
                  <template v-for="(seg, index) in markSegments(row.text)" :key="index">
                    <span v-if="seg.mark" class="mark-token">{{ seg.text }}</span>
                    <template v-else>{{ seg.text }}</template>
                  </template>
                </span>
              </div>
            </div>
            <div v-else class="chapter-source-text">
              {{ rows.length ? '当前筛选下没有可显示的行。' : state.chapterText || '本章正文为空。' }}
            </div>
          </div>

          <template v-else-if="view === 'table'">
            <div v-if="rows.length" class="rows-tools">
              <div class="rows-tools-top">
                <div class="rows-filter">
                  <button
                    v-for="item in filters"
                    :key="item.key"
                    type="button"
                    class="filter-btn"
                    :class="{ 'is-active': filter === item.key }"
                    @click="filter = item.key"
                  >
                    {{ item.label }}
                    <span class="filter-count">{{ filterCount(item.key) }}</span>
                  </button>
                </div>
                <!-- 音色管理是全书一次性的事，不属于哪一步：摆在行表上方的最右侧，
                     任何时候（章节栏之外）都能开。 -->
                <button
                  type="button"
                  class="sh-btn is-sm is-ghost voice-tools-btn"
                  :disabled="running"
                  :title="assistTitle"
                  @click="voiceToolsOpen = true"
                >
                  <Sparkles :size="12" :stroke-width="1.8" />
                  音色管理
                </button>
              </div>

              <div class="rows-tools-actions">
                <select
                  v-model="speakerFilter"
                  class="sh-field field-sm speaker-filter"
                  aria-label="按角色筛选"
                >
                  <option value="">全部角色</option>
                  <option value="__narrator__">旁白</option>
                  <option v-for="name in roleNames" :key="name" :value="name">
                    {{ name === UNASSIGNED_SPEAKER ? '未分配' : name }}
                  </option>
                </select>
                <button
                  class="sh-btn is-sm is-ghost"
                  :disabled="!canSplit"
                  title="把光标放到一行中间，点这里从光标处拆开"
                  @click="splitSelected"
                >
                  <Scissors :size="14" :stroke-width="1.8" />
                  拆分
                </button>
                <button
                  class="sh-btn is-sm is-ghost"
                  :disabled="!canMerge"
                  :title="mergeHint"
                  @click="mergeSelected"
                >
                  <Combine :size="14" :stroke-width="1.8" />
                  合并
                </button>
                <button
                  class="sh-btn is-sm is-ghost"
                  :disabled="running || (!rows.length && !chapterCount)"
                  title="短于上限的旁白并进下一行；对白行不动。范围在弹窗里选当前章或全书"
                  @click="autoMerge"
                >
                  <Wand2 :size="14" :stroke-width="1.8" />
                  自动合并
                </button>
                <button
                  class="sh-btn is-sm is-danger"
                  :disabled="!selected.length && !caretRow"
                  title="删除选中的行，或光标所在的那一行"
                  @click="removeSelected"
                >
                  <Trash2 :size="14" :stroke-width="1.8" />
                  删除
                </button>
                <span class="spacer"></span>
                <select
                  v-model="markName"
                  class="sh-field field-sm mark-picker"
                  aria-label="插入标记"
                  data-wheel="off"
                  @change="onMarkChange"
                >
                  <option value="">插入标记…</option>
                  <optgroup v-for="group in markGroups" :key="group.label" :label="group.label">
                    <option v-for="mark in group.marks" :key="mark.name" :value="mark.name">
                      {{ mark.label }}
                    </option>
                  </optgroup>
                </select>
              </div>

              <div v-if="selected.length" class="rows-batch">
                <span class="batch-count">已选 {{ selected.length }} 行</span>
                <select v-model="batchSpeaker" class="sh-field field-sm batch-input">
                  <option value="">角色（不改）</option>
                  <option value="__narrator__">旁白</option>
                  <option v-for="name in roleNames" :key="name" :value="name">{{ name }}</option>
                </select>
                <select v-model="batchEmotion" class="sh-field field-sm batch-input">
                  <option value="">情绪（不改）</option>
                  <optgroup
                    v-for="group in emotionGroups"
                    :key="group.label"
                    :label="group.label"
                  >
                    <option v-for="item in group.words" :key="item" :value="item">{{ item }}</option>
                  </optgroup>
                </select>
                <button class="sh-btn is-sm is-soft" @click="applyBatch">应用</button>
                <button class="sh-btn is-sm is-ghost" @click="clearSelection">取消</button>
              </div>
            </div>

            <div
              v-if="rows.length"
              ref="rowsWrap"
              class="rows-wrap"
              :class="{ 'is-single': Boolean(expandedRow) }"
              @scroll="onScroll"
            >
              <table
                class="rows"
                :class="{ 'is-single': Boolean(expandedRow) }"
                :style="colWidths"
              >
                <thead>
                  <tr>
                    <th class="col-ord">#</th>
                    <th class="col-speaker">角色</th>
                    <th>文本</th>
                    <th class="col-emotion">
                      <!-- 有引导指令时可切：情绪 / 引导。两者都在时默认看引导指令。 -->
                      <button
                        v-if="hasInstruction"
                        type="button"
                        class="col-switch"
                        :title="emotionColumn === 'instruction' ? '当前显示引导指令；点一下看情绪' : '当前显示情绪；点一下看引导指令'"
                        @click="toggleColumn"
                      >
                        {{ emotionColumn === 'instruction' ? '引导' : '情绪' }}
                        <Repeat2 :size="11" :stroke-width="2" />
                      </button>
                      <template v-else>情绪</template>
                    </th>
                    <th class="col-audio" aria-label="音频"></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-if="virtual && topCount" class="spacer">
                    <td :colspan="5" :style="padStyle(topCount)" />
                  </tr>
                  <tr
                    v-for="row in visibleRows"
                    :key="row.id"
                    :data-row-id="row.id"
                    :class="{
                      'is-dialogue': row.kind === 'dialogue',
                      'is-failed': row.status === 'failed',
                      'is-stale': row.status === 'stale',
                      'is-selected': selected.includes(row.id),
                      'is-caret': caretRow?.id === row.id,
                    }"
                    @click="focusRow(row, $event)"
                    @contextmenu="onRowMenu(row, $event)"
                  >
                    <td class="col-ord">
                      <button
                        type="button"
                        class="ord-btn"
                        :class="{ 'is-selected': selected.includes(row.id) }"
                        :title="selected.includes(row.id) ? '取消选中' : '选中这一行；按住 Shift 点另一行可选整段'"
                        @click="toggle(row.id, $event)"
                      >
                        {{ String(row.ordinal + 1).padStart(2, '0') }}
                      </button>
                      <span v-if="row.start_ms" class="ord-time" :title="`原字幕时间 ${timeOf(row.start_ms)}`">
                        {{ timeOf(row.start_ms) }}
                      </span>
                      <!-- 导入时补不回来的字：方框与残标已经清掉，正文里留下的空缺得有人看见。 -->
                      <span
                        v-if="isBroken(row)"
                        class="ord-warn"
                        title="这一行原文有缺字（导入时字符已丢失），建议校对"
                      >
                        <TriangleAlert :size="11" :stroke-width="2" />
                      </span>
                    </td>
                    <td class="col-speaker">
                      <SpeakerField
                        :value="roleOf(row)"
                        :options="roleOptions"
                        :disabled="running"
                        @change="setRole(row, $event)"
                      />
                    </td>
                    <td class="row-text">
                      <div
                        class="mark-field"
                        :class="{ 'is-tall': expandedRow === row.id }"
                      >
                        <div
                          v-if="hasMarks(textOf(row))"
                          class="mark-mirror"
                          :class="{ 'is-tall': expandedRow === row.id }"
                          aria-hidden="true"
                        >
                          <span
                            v-for="(seg, index) in markSegments(textOf(row))"
                            :key="index"
                            :class="{ 'mark-hit': seg.mark }"
                          >{{ seg.text }}</span>
                        </div>
                        <textarea
                          v-if="expandedRow === row.id"
                          class="row-input row-text-input row-text-area"
                          data-app-menu
                          :value="textOf(row)"
                          :disabled="running"
                          @input="draftText(row, $event)"
                          @scroll="syncMarkScroll"
                          @click="trackCaret(row, $event)"
                          @keyup="trackCaret(row, $event)"
                          @select="trackCaret(row, $event)"
                          @change="onTextChange(row, $event)"
                        />
                        <input
                          v-else
                          class="row-input row-text-input"
                          data-app-menu
                          :value="textOf(row)"
                          :title="textOf(row)"
                          :disabled="running"
                          @input="draftText(row, $event)"
                          @scroll="syncMarkScroll"
                          @click="trackCaret(row, $event)"
                          @keyup="trackCaret(row, $event)"
                          @select="trackCaret(row, $event)"
                          @change="onTextChange(row, $event)"
                        />
                      </div>
                    </td>
                    <td class="col-emotion">
                      <button
                        v-if="emotionColumn === 'instruction'"
                        type="button"
                        class="instr-cell"
                        :class="{ 'is-fallback': !instructionOf(row) }"
                        :title="instructionTitle(row)"
                        @click="openParams(row)"
                      >
                        {{ instructionOf(row) || row.emotion || '—' }}
                      </button>
                      <EmotionField
                        v-else
                        :value="row.emotion"
                        :groups="emotionGroups"
                        :disabled="running"
                        @change="edit(row, 'emotion', $event)"
                      />
                    </td>
                    <td class="col-audio">
                      <!-- 状态只由图标本身表达：播放 / 生成 / 重试 / 转圈。
                           过期的行不删旧音频，仍可播放，故只换图标颜色，不另立文字。 -->
                      <div class="audio-cell">
                        <!-- 生成中优先于其他状态：已生成 / 过期的行重新生成时也要看得见。 -->
                        <button
                          v-if="isGenerating(row)"
                          type="button"
                          class="audio-btn is-generate is-busy"
                          disabled
                          title="正在生成这一行……"
                          aria-label="正在生成这一行"
                        >
                          <Loader2 class="spin" :size="14" :stroke-width="1.8" />
                        </button>
                        <button
                          v-else-if="row.status === 'failed'"
                          type="button"
                          class="audio-btn is-danger"
                          :title="row.error || '生成失败，点这里重试'"
                          :aria-label="row.error || '重试这一行'"
                          :disabled="busy"
                          @click="generate(row)"
                        >
                          <RotateCcw :size="14" :stroke-width="1.8" />
                        </button>
                        <!-- 过期的旧音频不删：能听、能对比，只是不再代表这一行现在的角色 / 情绪。
                             改回原样会自动恢复（不重合成），重新生成则覆盖它。 -->
                        <button
                          v-else-if="row.status === 'stale'"
                          type="button"
                          class="audio-btn is-stale"
                          :title="row.loudness_note ? `播放上一次的音频（已过期） · ${row.loudness_note}` : '播放上一次的音频（已过期）'"
                          aria-label="播放上一次的音频（已过期）"
                          @click="playRow(row)"
                        >
                          <Play :size="14" :stroke-width="1.8" />
                        </button>
                        <button
                          v-else-if="row.audio"
                          type="button"
                          class="audio-btn"
                          :title="row.loudness_note ? `播放这一行 · ${row.loudness_note}` : '播放这一行'"
                          aria-label="播放这一行"
                          @click="playRow(row)"
                        >
                          <Play :size="14" :stroke-width="1.8" />
                        </button>
                        <button
                          v-else
                          type="button"
                          class="audio-btn is-generate"
                          title="生成这一行的音频"
                          aria-label="生成这一行的音频"
                          :disabled="busy"
                          @click="generate(row)"
                        >
                          <Sparkles :size="14" :stroke-width="1.8" />
                        </button>

                        <button
                          type="button"
                          class="audio-params"
                          title="详细参数设置"
                          aria-label="详细参数设置"
                          :disabled="busy || isGenerating(row)"
                          @click.stop="openParams(row)"
                        >
                          <SlidersHorizontal :size="14" :stroke-width="1.8" />
                        </button>
                        <RowMenu
                          :disabled="busy || isGenerating(row)"
                          type="audiobook.row"
                          :context="rowMenuContext(row)"
                        />
                      </div>
                    </td>
                  </tr>
                  <tr v-if="virtual && bottomCount" class="spacer">
                    <td :colspan="5" :style="padStyle(bottomCount)" />
                  </tr>
                  <tr v-if="!filteredRows.length">
                    <td colspan="5" class="rows-empty">{{ emptyText }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </div>

        <!-- 右列是章节栏：原位置的角色只读表已收进「音色管理」弹窗，
             腾出的这一列给整本书的目录，选中哪章就在中间编辑。 -->
        <aside class="producer-side">
          <Rail />
        </aside>
      </div>
    </template>

    <ProjectDialog :open="state.projectOpen" @close="closeProjectManager" />
    <ConfirmDialog />
    <SplitOptionsDialog
      :open="optionsOpen"
      :can-resplit="Boolean(chapter && chapter.rows)"
      :rows="detail?.stats?.rows || 0"
      :chapters="detail?.stats?.chapters || 0"
      @close="optionsOpen = false"
      @run="startSentences"
      @resplit="resplit"
    />
    <VoiceToolsDialog
      :open="voiceToolsOpen"
      :assign-roles="assignableRoles"
      :merge-roles="mergeableRoles"
      :chapter-scope="chapterScope"
      @close="voiceToolsOpen = false"
      @merge="onMergeSpeakers"
    />
    <RowParamsDialog
      :open="Boolean(paramsRow)"
      :row="paramsRow"
      @close="paramsRow = null"
      @saved="onParamsSaved"
    />
    <MergeDialog :open="mergeOpen" @close="mergeOpen = false" />
    <QcDialog :open="qcOpen" @close="qcOpen = false" @run="startQc" />
    <GenerateDialog :open="generateOpen" @close="generateOpen = false" @run="startGenerate" />
    <AnalysisDialog
      :open="analysisOpen"
      @close="analysisOpen = false"
      @run="startAnalysis"
    />

    <Teleport :to="overlayTarget">
      <Transition name="vh-modal">
        <div v-if="state.splitOpen" class="vh-root" @click.self="closeSplit">
          <div class="vh-modal split-dialog">
            <button class="sh-btn is-icon split-dialog-close" title="关闭" @click="closeSplit">
              <X :size="16" :stroke-width="1.8" />
            </button>
            <SplitPanel />
          </div>
        </div>
      </Transition>
    </Teleport>
  </section>
</template>

<script setup>
// @ts-check
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  AlignLeft,
  BookOpen,
  Combine,
  Ear,
  Loader2,
  Mic,
  Play,
  Repeat2,
  RotateCcw,
  Scissors,
  SlidersHorizontal,
  Sparkles,
  Table2,
  Trash2,
  TriangleAlert,
  Wand2,
  X,
} from 'lucide-vue-next'
import { actions, say, store } from '../../store'
import ActionButton from '../../components/ui/ActionButton.vue'
import { hasMarks, markSegments } from '../../components/marks'
import AnalysisDialog from './AnalysisDialog.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import EmotionField from './EmotionField.vue'
import Rail from './Rail.vue'
import GenerateDialog from './GenerateDialog.vue'
import MergeDialog from './MergeDialog.vue'
import QcDialog from './QcDialog.vue'
import ProjectDialog from './ProjectDialog.vue'
import RowMenu from './RowMenu.vue'
import RowParamsDialog from './RowParamsDialog.vue'
import SplitOptionsDialog from './SplitOptionsDialog.vue'
import SplitPanel from './SplitPanel.vue'
import SpeakerField from './SpeakerField.vue'
import VoiceToolsDialog from './VoiceToolsDialog.vue'
import { confirmAction } from './confirm'
import { useSettingsStore } from '../../stores/settings'
import { namedSpeakersOf, rolesOf } from './assign'
import { chapterVisible, filterRows, isBroken, UNASSIGNED_SPEAKER } from './visible'
import {
  autoMergeAllRows,
  autoMergeRows,
  batchUpdateRows,
  cancelJob,
  closeProjectManager,
  closeSplit,
  deleteRow,
  deleteRows,
  editRow,
  loadAssistStatus,
  mergeRows,
  mergeSpeakers,
  openProjectManager,
  openSplit,
  refresh,
  resplitChapter,
  retryRow,
  selectChapter,
  runStep,
  splitRow,
  stopJobPolling,
  stopMerge,
  state,
} from './store'
import { overlayTarget } from '../../composables/overlayTarget'
import { canTakeOver, openObjectMenu } from '../../composables/contextMenu'

const plugin = computed(() => store.producers.find(/** @param {any} item */ (item) => item.key === store.producer.key))
/** 外观偏好：密度决定行高，行高的量法见 `measure()`。 */
const settings = useSettingsStore()
const optionsOpen = ref(false)
const mergeOpen = ref(false)
const qcOpen = ref(false)
const analysisOpen = ref(false)
const generateOpen = ref(false)
const voiceToolsOpen = ref(false)
const viewMode = ref('table')
//: 表格框现在的像素高度。切到原文时沿用它，两种视图的框大小一致。
const tableBoxHeight = ref(0)

/**
 * @param {any} mode
 */
function setView(mode) {
  if (mode === viewMode.value) return
  if (mode === 'chapter') {
    // 表格当前的第一行就是两边的锚：切过去后从同一行接着读。
    const anchor = anchorRowId()
    // 离开表格之前先量：挂载后行表可能已经滚到别处，但框高没变。
    measure()
    viewMode.value = mode
    void nextTick(() => scrollChapterTo(anchor))
    return
  }
  viewMode.value = mode
  void nextTick(() => {
    measure()
    // 表格是重新挂载的，滚动位置归零：把离开前的位置放回去，虚拟窗口才对得上。
    if (rowsWrap.value) rowsWrap.value.scrollTop = scrollTop.value
  })
}

/** 表格当前第一行的 id：滚动距离减掉表头再除以实测行高。 */
function anchorRowId() {
  const list = filteredRows.value
  if (!list.length) return ''
  const at = Math.max(0, scrollTop.value - headH.value)
  const index = Math.min(list.length - 1, Math.floor(at / (rowH.value || 1)))
  return list[index]?.id || ''
}

/**
 * 把章节阅读列表滚到某一行：用视口坐标差算，容器有内边距也不会偏。
 *
 * @param {any} rowId
 */
function scrollChapterTo(rowId) {
  const wrap = chapterLines.value
  if (!wrap || !rowId) return
  const line = wrap.querySelector(`[data-row-id="${rowId}"]`)
  if (!line) return
  const top =
    line.getBoundingClientRect().top - wrap.getBoundingClientRect().top + wrap.scrollTop
  wrap.scrollTop = Math.max(0, top)
}

/**
 * 步骤卡的名字与图标由前端定：插件只给流水线语义，不规定界面怎么叫。
 *
 * @type {Record<string, any>}
 */
const STEP_META = {
  chapters: { label: '分章', icon: BookOpen },
  sentences: { label: '语句拆分', icon: Scissors },
  analysis: { label: '对话分析', icon: Sparkles },
  audio: { label: 'TTS 生成', icon: Mic },
  qc: { label: '预检', icon: Ear },
  merge: { label: '合并音频', icon: Combine },
}

/**
 * @param {any} step
 */
function stepLabel(step) {
  return STEP_META[step.key]?.label || step.label
}

/**
 * @param {string} key
 */
function stepIcon(key) {
  return STEP_META[key]?.icon || BookOpen
}

/**
 * @param {any} index
 */
function stepOrdinal(index) {
  return String(index + 1).padStart(2, '0')
}

/**
 * @param {string} key
 */
async function openStep(key) {
  if (key === 'chapters') {
    openSplit()
    return
  }
  if (key === 'sentences') {
    optionsOpen.value = true
    return
  }
  if (key === 'analysis') {
    const count = detail.value?.stats.dialogue || 0
    if (!count) {
      say('没有需要对白的行，先拆句。', { kind: 'warn' })
      return
    }
    analysisOpen.value = true
    return
  }
  if (key === 'audio') {
    const count = detail.value?.stats.rows || 0
    if (!count) {
      say('还没有可生成的行，先拆句。', { kind: 'warn' })
      return
    }
    generateOpen.value = true
    return
  }
  if (key === 'qc') {
    if (!detail.value?.stats?.audio) {
      say('还没有逐行音频：先生成音频。', { kind: 'warn' })
      return
    }
    qcOpen.value = true
    return
  }
  if (key === 'merge') {
    mergeOpen.value = true
  }
}

/** 拆全部章节：与「重拆本章」同一套切行规则，范围覆盖全部章节。 */
async function startSentences() {
  optionsOpen.value = false
  const rows = Number(detail.value?.stats?.rows || 0)
  if (!rows) {
    run('sentences')
    return
  }
  const chapters = Number(detail.value?.stats?.chapters || 0)
  const answer = await confirmAction({
    title: '重拆全部章节',
    message:
      `${chapters} 章按当前选项重新切行。文本未变的行保留角色、情绪与音频；` +
      '文本改过的行重建，需要重新分析、生成。',
    confirmText: '重拆',
  })
  if (!answer) return
  run('sentences')
}

function startAnalysis() {
  analysisOpen.value = false
  run('analysis')
}

function startGenerate() {
  generateOpen.value = false
  run('audio')
}

function startQc() {
  qcOpen.value = false
  run('qc')
}

const detail = computed(() => state.detail)
/** 制作流程：字幕项目导入时就带着行与时间码，「分章 / 拆句」两步没有意义，直接不摆。 */
const steps = computed(() => {
  const all = plugin.value?.steps || []
  if (detail.value?.kind !== 'subtitle') return all
  return all.filter(
    /** @param {any} step */
    (step) => !['chapters', 'sentences'].includes(step.key)
  )
})
const rows = computed(() => state.rows)
/** 没拆句就没有行可编辑：这时按章节正文显示，表格视图留白没有意义。 */
const view = computed(() => (rows.value.length ? viewMode.value : 'chapter'))

// ---- 列宽：角色 / 情绪按本页最长内容定宽，短则收、长则放，各有上限 ----
//: 一格内容占的横向空间里，边框、内边距、箭头这些固定量（px）。
//: 角色格已不再画人物图标，固定量比从前少一截；下限跟着收，平板才排得下整行。
const COL_BASE = { speaker: 50, emotion: 48 }
/** @type {Record<string, [number, number]>} */
const COL_LIMIT = { speaker: [84, 168], emotion: [84, 168] }

/**
 * 引导指令存在行参数里：有就拿来显示，没有退回情绪。
 *
 * @param {any} row
 */
function instructionOf(row) {
  return String(row?.params?.instruction || '').trim()
}
const hasInstruction = computed(() => state.rows.some(/** @param {any} row */ (row) => instructionOf(row)))
/** 列模式：空 = 自动（有引导指令就显示它）；点表头可强制切换。 */
const columnMode = ref('')
const emotionColumn = computed(
  () => columnMode.value || (hasInstruction.value ? 'instruction' : 'emotion')
)

function toggleColumn() {
  columnMode.value = emotionColumn.value === 'instruction' ? 'emotion' : 'instruction'
}

/**
 * 引导指令那一格的悬停说明：有就显示全文，没有就说清这是情绪兜底。
 *
 * @param {any} row
 */
function instructionTitle(row) {
  const instruction = instructionOf(row)
  if (instruction) return `${instruction}（点这里改行参数）`
  const emotion = row.emotion ? `显示的是情绪「${row.emotion}」` : '还没有引导指令'
  return `没有引导指令，${emotion}；点这里补一条`
}

/**
 * 单个字符的大致宽度：中文按 1em，西文按 0.55em（12px 字号下约 12.4 / 6.8）。
 *
 * @param {any} ch
 */
function charWidth(ch) {
  return /[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]/.test(ch) ? 12.4 : 6.8
}

/**
 * @param {any} text
 * @param {any} base
 * @param {[number, number]} limits
 */
function fitWidth(text, base, limits) {
  const [low, high] = limits
  const span = [...(text || '')].reduce((sum, ch) => sum + charWidth(ch), 0)
  return Math.min(high, Math.max(low, Math.round(base + span + 8)))
}

const colWidths = computed(() => {
  const widestRole = state.rows.reduce((best, row) => {
    const name = roleOf(row)
    return name.length > best.length ? name : best
  }, '旁白')
  const widestEmotion = state.rows.reduce((best, row) => {
    const value =
      emotionColumn.value === 'instruction'
        ? instructionOf(row)
        : row.emotion || ''
    return value.length > best.length ? value : best
  }, '—')
  return {
    '--ws-speaker-w': `${fitWidth(widestRole, COL_BASE.speaker, COL_LIMIT.speaker)}px`,
    '--ws-emotion-w': `${fitWidth(widestEmotion, COL_BASE.emotion, COL_LIMIT.emotion)}px`,
  }
})
const voices = computed(() => store.allVoices)
const emotions = computed(() => state.overview.emotions || [])
/** 情绪候选的分组摆法（后端按词表算好）：下拉与行内情绪格都读这一份。 */
const emotionGroups = computed(() =>
  state.overview.emotion_groups?.length
    ? state.overview.emotion_groups
    : [{ label: '情绪', words: emotions.value }]
)
const chapter = computed(
  () => detail.value?.chapters.find(/** @param {any} item */ (item) => item.id === state.chapterId) || null
)
/** 有任务在跑（含预检）。任务条读它，预检的进度才看得见。 */
const activeJob = computed(
  () => Boolean(state.job && ['running', 'cancelling'].includes(state.job.state))
)
/** 独占任务在跑：预检只读音频，不挡生成与合并。 */
const running = computed(() => Boolean(activeJob.value && state.job.exclusive !== 0))
const busy = computed(() => Boolean(state.busy) || running.value)
const jobLabel = computed(() => {
  const kind = state.job?.kind
  return (
    (/** @type {Record<string, string>} */ ({
      audio: '生成',
      analysis: '分析',
      qc: '预检',
    })[kind]) || '任务'
  )
})

/**
 * 同步操作的即时说明；后台任务由上面的 job 条负责。
 *
 * @type {Record<string, any>}
 */
const BUSY_TEXT = {
  create: { kind: '创建', message: '正在创建项目……' },
  'split-preview': { kind: '分章', message: '正在生成分章预览……' },
  split: { kind: '分章', message: '正在切分章节……' },
  'text-preview': { kind: '文本', message: '正在生成文本处理预览……' },
  'text-apply': { kind: '文本', message: '正在应用文本处理……' },
  'delete-chapter': { kind: '章节', message: '正在删除章节……' },
  resplit: { kind: '拆句', message: '正在重新拆句……' },
  sentences: { kind: '拆句', message: '正在拆句……' },
  analysis: { kind: '分析', message: '正在分析对话……' },
  audio: { kind: '生成', message: '正在启动生成任务……' },
  qc: { kind: '预检', message: '正在回读行音频……' },
  merge: { kind: '合并', message: '正在合并本章：拼接音频、写字幕与 BGM……' },
  'merge-all': { kind: '合并', message: '正在逐章合并……' },
  export: { kind: '导出', message: '正在打包 ZIP……' },
  'export-m4b': { kind: '导出', message: '正在封装 M4B……' },
}
const canStopMerge = computed(() => ['merge', 'merge-all'].includes(state.busy))
const syncBusy = computed(() => {
  const progress = state.mergeProgress?.message
  const stopping = state.mergeStopping ? ' · 正在停止…' : ''
  if (state.busy === 'merge-all' && state.mergeBatch) {
    const at = Math.min(state.mergeBatch.done + 1, state.mergeBatch.total)
    const chapterProgress = `正在逐章合并 ${at}/${state.mergeBatch.total}：${state.mergeBatch.title}`
    return {
      kind: '合并',
      message: (progress ? `${chapterProgress} · ${progress}` : chapterProgress) + stopping,
    }
  }
  if (state.busy === 'merge' && progress) return { kind: '合并', message: progress + stopping }
  return BUSY_TEXT[state.busy] || null
})
const busySeconds = ref(0)
let busyTimer = 0
watch(
  () => state.busy,
  (value) => {
    if (busyTimer) window.clearInterval(busyTimer)
    busyTimer = 0
    busySeconds.value = 0
    if (!value) return
    const started = Date.now()
    busyTimer = window.setInterval(() => {
      busySeconds.value = Math.floor((Date.now() - started) / 1000)
    }, 1000)
  }
)

// ---- 步骤卡：没有可做的对象时直接禁用，别让人点进去才发现是空跑 ----------

/**
 * @param {string} key
 */
function stepBlocked(key) {
  const stats = detail.value?.stats
  if (!stats) return true
  if (key === 'sentences') return !stats.chapters
  if (key === 'analysis') return !stats.rows || !stats.dialogue
  if (key === 'audio') return !stats.rows
  if (key === 'qc' || key === 'merge') return !stats.audio
  return false
}

/**
 * 悬停说明：能点时说这一步做什么，不能点时说到底缺什么。
 *
 * @param {string} key
 */
function stepTitle(key) {
  const stats = detail.value?.stats || {}
  if (key === 'chapters') return '导入原文并切成章节'
  if (key === 'sentences') {
    return stepBlocked(key) ? '还没有章节：先分章。' : '按句 / 按段落 / 不拆，把正文切成行'
  }
  if (key === 'analysis') {
    if (!stats.rows) return '还没有拆句：分析的对象是每一行。'
    if (!stats.dialogue) return '没有对白行，分析没有可改的对象。'
    return '识别对白角色与情绪'
  }
  if (key === 'audio') {
    return stepBlocked(key) ? '还没有可生成的行：先拆句。' : '按角色音色逐行生成音频'
  }
  if (key === 'qc') {
    return stepBlocked(key) ? '还没有逐行音频：先生成音频。' : '回读已生成的音频，找出念错、漏读与截断'
  }
  if (key === 'merge') {
    return stepBlocked(key) ? '还没有逐行音频：先生成音频。' : '把逐行音频合成章节成品'
  }
  return ''
}

// ---- 行表：筛选 / 多选 / 虚拟滚动 -------------------------------------

// 行表的筛选放在生产方 store 里：章节栏的灯要跟着这同一档亮，不另存一份。
const filter = computed({
  get: () => state.rowFilter || 'all',
  set: (value) => {
    state.rowFilter = value
  },
})
// 只留"要人动手"的几类：失败、缺字、预检疑似。未定角色、待生成都能从行上直接
// 看出来，且每个角色在音色管理里已按行数列出，不另立两个统计档。
const filters = [
  { key: 'all', label: '全部' },
  { key: 'failed', label: '失败' },
  { key: 'broken', label: '需校对' },
  { key: 'suspect', label: '疑似' },
]
// 章节栏要读同一份角色筛选，所以与 rowFilter 一样放在生产方 store 里。
const speakerFilter = computed({
  get: () => state.speakerFilter || '',
  set: (value) => {
    state.speakerFilter = value
  },
})
const selected = ref(/** @type {string[]} */ ([]))
//: 连选的起点：Shift 点另一行时，从这一行到那一行整段选中。
const selectAnchor = ref('')
const batchSpeaker = ref('')
const batchEmotion = ref('')
const markName = ref('')
const caret = ref({ rowId: '', at: 0 })
/** 角色候选 = **正文里真出现过的说话人**（`detail.speakers`），不是音色映射表。
 *  `voices` 里可能留着上一轮分析（或手动指定过）的名字，那些行早没了：
 *  把它们摆进右栏和下拉，等于告诉用户"正文里有这个人"。名单只有一处产地。 */
const roleNames = computed(() =>
  (detail.value?.speakers || [])
    .map(/** @param {any} item */ (item) => item.speaker)
    .filter(/** @param {any} name */ (name) => name && name !== '旁白')
)
/** 角色候选：旁白固定在第一项，其余按后端给的顺序（出现次数）。手输不受此限。 */
const roleOptions = computed(() => ['旁白', ...roleNames.value])

const markGroups = computed(() => {
  const groups = []
  const common = state.marks?.common || []
  if (common.length) groups.push({ label: '通用', marks: common })
  for (const item of state.marks?.engines || []) {
    if (item.marks?.length) groups.push({ label: item.label, marks: item.marks })
  }
  return groups
})
const rowsWrap = ref(/** @type {any} */ (null))
const chapterLines = ref(/** @type {any} */ (null))
const scrollTop = ref(0)
const viewportH = ref(540)
//: 表头高度：sticky 表头也占滚动位置，算"第几行到了顶"要先减掉它。
const headH = ref(40)
//: 实测行高。它由密度（`--ws-row-h`）决定，**不能写死**：写死的那一版在宽松
//: 密度（58px）下，滚到底会把起点算过表尾，可见行数越来越少，最后一屏全空。
const rowH = ref(49)
const VIRTUAL_AT = 300

//: 状态档与角色这两道筛选收在 `visible.js` 里：表格、试听队列、章节栏与滚轮换章
//: 共用同一条链路，而不是各写一份判据。
const filteredRows = computed(() =>
  filterRows(state.rows, { filter: filter.value, speaker: speakerFilter.value })
)
const filterActive = computed(() => filter.value !== 'all' || Boolean(speakerFilter.value))

const virtual = computed(() => filteredRows.value.length > VIRTUAL_AT)
const start = computed(() => {
  if (!virtual.value) return 0
  const at = Math.max(0, scrollTop.value - headH.value)
  const index = Math.floor(at / rowH.value) - 8
  // 算过表尾就停在最后一行：宁可少画几行，也不许整张表空白。
  return Math.min(Math.max(0, index), Math.max(0, filteredRows.value.length - 1))
})
const end = computed(() =>
  virtual.value
    ? Math.min(filteredRows.value.length, start.value + Math.ceil(viewportH.value / rowH.value) + 16)
    : filteredRows.value.length
)
const visibleRows = computed(() => filteredRows.value.slice(start.value, end.value))
const topCount = computed(() => (virtual.value ? start.value : 0))
const bottomCount = computed(() =>
  virtual.value ? filteredRows.value.length - end.value : 0
)
/**
 * 占位高度 = 行数 × 实测行高。占位与起点必须用同一个数，否则滚到底会错位。
 *
 * @param {any} count
 */
function padStyle(count) {
  return { height: `${Math.round(count * rowH.value)}px` }
}
const emptyText = computed(() => {
  if (speakerFilter.value) return '这个角色在本章没有行。'
  if (filter.value === 'failed') return '没有失败的行。'
  if (filter.value === 'broken') return '这一章没有缺字的行。'
  if (filter.value === 'suspect') return '这一章没有疑似读错的行。'
  return '这一章还没有拆句。'
})

/**
 * @param {any} kind
 */
function filterCount(kind) {
  if (kind === 'failed') return state.rows.filter(/** @param {any} row */ (row) => row.status === 'failed').length
  if (kind === 'broken') return state.rows.filter(isBroken).length
  if (kind === 'suspect') return state.rows.filter(/** @param {any} row */ (row) => row.qc === 'suspect').length
  return state.rows.length
}


/**
 * @param {any} rowId
 * @param {any} event
 */
function toggle(rowId, event) {
  if (event?.shiftKey && selectAnchor.value) {
    const rows = filteredRows.value
    const from = rows.findIndex(/** @param {any} row */ (row) => row.id === selectAnchor.value)
    const to = rows.findIndex(/** @param {any} row */ (row) => row.id === rowId)
    // 起点被筛掉了就当普通点击：连选只在同一条可见列表里成立。
    if (from >= 0 && to >= 0) {
      selected.value = rows
        .slice(Math.min(from, to), Math.max(from, to) + 1)
        .map(/** @param {any} row */ (row) => row.id)
      return
    }
  }
  selectAnchor.value = rowId
  selected.value = selected.value.includes(rowId)
    ? selected.value.filter((id) => id !== rowId)
    : [...selected.value, rowId]
}

function clearSelection() {
  selected.value = []
  selectAnchor.value = ''
}

/**
 * @param {any} event
 */
function onScroll(event) {
  scrollTop.value = event.target.scrollTop
}

/** 量三样：视口高、表头高、**真实行高**。行高由密度与字号决定，只读 CSS 变量
 *  拿不到"内容把行撑高"的那种情况，所以再退一步量一行。 */
function measure() {
  const wrap = rowsWrap.value
  if (!wrap) return
  const height = wrap.clientHeight || 0
  viewportH.value = height || 540
  if (height) tableBoxHeight.value = height
  const head = wrap.querySelector("thead")
  if (head) headH.value = head.getBoundingClientRect().height || headH.value
  const row = wrap.querySelector("tbody tr:not(.spacer)")
  rowH.value =
    row?.getBoundingClientRect().height ||
    parseFloat(getComputedStyle(wrap).getPropertyValue("--ws-row-h")) ||
    rowH.value
}

// 行表加载完再量一次：挂载时表里还没有行，容器高度会被量小。
watch(() => state.rows.length, measure, { flush: 'post' })
// 密度改的是行高：不重量一次，占位还按旧行高铺，滚到后面照样错位。
watch(() => settings.appearance.density, () => void nextTick(measure))
// 窗口变窄变高都会改视口高：不跟着量，虚拟表只画半屏。
function onResize() {
  measure()
}

/**
 * @param {any} row
 */
function playRow(row) {
  actions.play({
    url: rowAudioUrl(row),
    label: row.text.slice(0, 24) || '这一行',
    sub: chapter.value?.title || '',
  })
}

/** 逐行试听队列 = 当前显示的行（同一条筛选链）+ 只收已经有音频的行
 *  （过期的旧音频也算，它是这一行现在唯一能听的东西）。筛了角色或状态，
 *  播放的就是眼前这一批；中间没有音频的行跳过。 */
const rowTracks = computed(() =>
  filteredRows.value
    .filter(/** @param {any} row */ (row) => row.audio)
    .map(/** @param {any} row */ (row) => ({
      url: rowAudioUrl(row),
      label: (row.text || '').slice(0, 24) || '这一行',
      sub: chapter.value?.title || '',
    }))
)

const playQueueHint = computed(() => {
  if (rowTracks.value.length) return `按顺序播放当前显示的 ${rowTracks.value.length} 行音频`
  return filterActive.value ? '当前筛选没有可播的行' : '本章还没有逐行音频'
})

function playChapterRows() {
  if (rowTracks.value.length) actions.playAll(rowTracks.value, 0)
}

//: 滚轮换章的节流：一次滚动是一串事件，不压住会连跳好几章。
let chapterWheelAt = 0

/** 章节是否通过当前筛选链，与行表、试听队列、章节栏读同一份判据。 */
function chapterContext() {
  return {
    filter: filter.value,
    speaker: speakerFilter.value,
    rows: state.rows,
    currentId: state.chapterId,
  }
}

/**
 * 滚轮换章：只停在筛选链还留着的章上，被排除的章跳过；到头就停下，不绕回。
 *
 * @param {any} event
 */
function stepChapter(event) {
  const list = detail.value?.chapters || []
  const from = list.findIndex(/** @param {any} item */ (item) => item.id === state.chapterId)
  const direction = event.deltaY > 0 ? 1 : -1
  if (from < 0 || !event.deltaY) return
  const now = Date.now()
  if (now - chapterWheelAt < 220) return
  const context = chapterContext()
  for (let index = from + direction; index >= 0 && index < list.length; index += direction) {
    if (chapterVisible(list[index], context)) {
      chapterWheelAt = now
      void selectChapter(list[index].id)
      return
    }
  }
}

function playChapter() {
  if (!chapter.value?.audio) return
  actions.play({
    url: chapterAudioUrl.value,
    label: chapter.value.title,
    sub: detail.value?.name || '',
  })
}

/**
 * @param {any} name
 */
function markDef(name) {
  for (const group of markGroups.value) {
    const found = group.marks.find(/** @param {any} mark */ (mark) => mark.name === name)
    if (found) return found
  }
  return null
}

/**
 * @param {any} value
 */
async function insertMark(value) {
  const mark = markDef(value)
  markName.value = ''
  if (!mark) return
  const row =
    state.rows.find(/** @param {any} item */ (item) => item.id === caret.value.rowId) ||
    (selected.value.length === 1
      ? state.rows.find(/** @param {any} item */ (item) => item.id === selected.value[0])
      : null)
  if (!row) {
    say('先把光标放到一行文本里。')
    return
  }
  const mapping =
    (chapterScope.value && detail.value?.chapter_voices?.[row.chapter_id]) ||
    detail.value?.voices?.[row.speaker || '旁白']
  const engine = mapping?.engine || ''
  const supported = engine ? (state.marks?.engine_marks?.[engine] || []).includes(mark.name) : null
  if (supported === false) {
    const answer = await confirmAction({
      title: '这个标签会被丢弃',
      message: `「${engine}」不支持「${mark.label}」，插入后合成时会被忽略。仍要插入？`,
      confirmText: '仍要插入',
      danger: false,
    })
    if (!answer) return
  }
  let text = `[[${mark.name}]]`
  if (mark.arg === '秒') {
    const answer = await confirmAction({
      title: `插入「${mark.label}」`,
      message: '停顿长度，单位秒。',
      confirmText: '插入',
      danger: false,
      input: { type: 'number', placeholder: '0.5' },
      value: '0.5',
    })
    if (!answer) return
    const seconds = Number(answer)
    if (!Number.isFinite(seconds) || seconds <= 0) {
      say('秒数要大于 0。')
      return
    }
    text = `[[${mark.name}:${seconds}]]`
  }
  const source = row.text || ''
  const caretHere = caret.value.rowId === row.id
  const at = caretHere ? caret.value.at : source.length
  const next = source.slice(0, at) + text + source.slice(at)
  const position = at + text.length
  markName.value = ''
  if (caretHere) {
    // 连着插两条时，第二条落在第一条后面，而不是又回到原位置。
    caret.value = { rowId: row.id, at: position }
  }
  await editRow(row.id, { text: next })
  if (!caretHere) return
  // 输入框在值更新后会把旧的光标偏移报回来：落库后把 DOM 光标放回新标签之后。
  await nextTick()
  const input = rowsWrap.value?.querySelector(`tr[data-row-id="${row.id}"] .row-input`)
  if (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement) {
    input.focus({ preventScroll: true })
    input.setSelectionRange(position, position)
    caret.value = { rowId: row.id, at: position }
  }
}

async function applyBatch() {
  const patch = {}
  if (batchSpeaker.value) {
    patch.speaker = batchSpeaker.value === '__narrator__' ? '旁白' : batchSpeaker.value
  }
  if (batchEmotion.value) patch.emotion = batchEmotion.value
  if (!Object.keys(patch).length) return
  await batchUpdateRows([...selected.value], patch)
  batchSpeaker.value = ''
  batchEmotion.value = ''
  clearSelection()
}

/**
 * @param {any} row
 */
function roleOf(row) {
  return !row.speaker || row.speaker === '旁白' ? '旁白' : row.speaker
}

/**
 * @param {any} row
 * @param {any} value
 */
function setRole(row, value) {
  const role = (value || '').trim() || '旁白'
  if (role === roleOf(row)) return
  void editRow(row.id, { speaker: role })
}

/**
 * @param {any} row
 * @param {any} event
 */
function trackCaret(row, event) {
  caret.value = { rowId: row.id, at: event.target.selectionStart ?? 0 }
}

// ---- 行文本：草稿 + 标记镜像 ---------------------------------------------

const drafts = ref(/** @type {Record<string, string>} */ ({}))

/** 不拆模式一章只有一行：把这一行摊成多行文本框，别让整章正文挤在一格输入框里。 */
const expandedRow = computed(() => {
  if (state.rows.length !== 1) return ''
  const row = state.rows[0]
  if (detail.value?.split_mode === 'none' || (row.text || '').length > 160) return row.id
  return ''
})

/**
 * @param {any} row
 */
function textOf(row) {
  return row.id in drafts.value ? drafts.value[row.id] : row.text
}

/**
 * @param {any} row
 * @param {any} event
 */
function draftText(row, event) {
  drafts.value = { ...drafts.value, [row.id]: event.target.value }
}

/**
 * `change` 才落库：每敲一下都发请求既慢，又会让整张行表跟着重排。
 *
 * @param {any} row
 * @param {any} value
 */
/**
 * @param {Event} event
 */
function onMarkChange(event) {
  insertMark(/** @type {HTMLSelectElement} */ (event.target).value)
}

/**
 * @param {any} row
 * @param {Event} event
 */
function onTextChange(row, event) {
  commitText(row, /** @type {HTMLTextAreaElement} */ (event.target).value)
}

/**
 * @param {any} row
 * @param {any} value
 */
function commitText(row, value) {
  const next = String(value ?? '')
  const draft = { ...drafts.value }
  delete draft[row.id]
  drafts.value = draft
  if (next === (row.text || '')) return
  void editRow(row.id, { text: next })
}

/**
 * 镜像与输入框各滚各的：手动对齐，滚动条只留在输入框那一层。
 *
 * @param {any} event
 */
function syncMarkScroll(event) {
  const field = event.target.closest?.('.mark-field')
  const mirror = field?.querySelector?.('.mark-mirror')
  if (!mirror) return
  mirror.scrollTop = event.target.scrollTop
  mirror.scrollLeft = event.target.scrollLeft
}

/** 光标所在行：必须是当前筛出来的行。
 *  行没了、被筛选藏起来了都算没有光标——那时「与下一行合并」只能靠猜。
 *  只勾选一行不构成光标：勾选是批量操作的选择集，点进行内（含文本格）才是光标。 */
const caretRow = computed(() => {
  if (!caret.value.rowId) return null
  return filteredRows.value.find(/** @param {any} item */ (item) => item.id === caret.value.rowId) || null
})
/** 光标所在行的下一行：合并只往下走一行，且那一行得在当前的筛选结果里
 *  ——并掉一行看不见的行，界面上没有任何痕迹。 */
const caretNext = computed(() => {
  const row = caretRow.value
  if (!row) return null
  const index = state.rows.findIndex(/** @param {any} item */ (item) => item.id === row.id)
  const next = index >= 0 ? state.rows[index + 1] || null : null
  return next && filteredRows.value.includes(next) ? next : null
})
const canSplit = computed(() => {
  const row = caretRow.value
  return Boolean(row) && caret.value.at > 0 && caret.value.at < (row.text || '').length
})
/** 选中的行在章内必须相邻：合并只是按顺序接文本，中间隔着的行会被跳过。
 *  不相邻的选中仍可用于批量改角色 / 情绪 / 删除。 */
const selectionContiguous = computed(() => {
  if (selected.value.length < 2) return false
  const picked = new Set(selected.value)
  /** @type {number[]} */
  const indexes = []
  state.rows.forEach((row, index) => {
    if (picked.has(row.id)) indexes.push(index)
  })
  return (
    indexes.length === picked.size &&
    indexes[indexes.length - 1] - indexes[0] === indexes.length - 1
  )
})
const canMerge = computed(() => selectionContiguous.value || Boolean(caretNext.value))
const mergeHint = computed(() => {
  if (selected.value.length >= 2) {
    return selectionContiguous.value ? '把选中的相邻行并成一行' : '只能合并相邻的行'
  }
  if (caretNext.value) {
    return `合并第 ${caretRow.value.ordinal + 1}、${caretNext.value.ordinal + 1} 行`
  }
  return '把光标放进一行并与下一行合并；或选中相邻两行以上'
})

function splitSelected() {
  const row = caretRow.value
  if (!row || !caret.value.at) {
    say('把光标放到这一行要拆开的位置。')
    return
  }
  clearSelection()
  void splitRow(row.id, caret.value.at)
}

/**
 * 点行内空白处算「光标在这行」；点行上的控件（行号、角色、情绪、音频按钮）不算
 * ——那些格子有自己的焦点，顺手留下光标会让下一次「合并」并掉没被指的那一行。
 * 文本格由 `trackCaret` 直接记光标位置。
 *
 * @param {any} row
 * @param {any} event
 */
function focusRow(row, event) {
  const target = event?.target
  if (target?.closest?.('button, a, select, input, textarea, [role="button"], [role="combobox"]')) return
  if (caret.value.rowId === row.id) return
  caret.value = { rowId: row.id, at: String(textOf(row) || '').length }
}

async function mergeSelected() {
  let ids = []
  if (selected.value.length >= 2) {
    if (!selectionContiguous.value) {
      say('只能合并相邻的行。')
      return
    }
    ids = [...selected.value]
  } else if (caretRow.value && caretNext.value) {
    ids = [caretRow.value.id, caretNext.value.id]
  } else {
    say('把光标放进一行，或选中相邻两行以上。')
    return
  }
  const pair = ids.length === 2 && selected.value.length < 2
  const answer = await confirmAction({
    title: pair ? '与下一行合并' : '合并选中行',
    message: `${ids.length} 行并成一行；这些行的旧音频会失效。`,
    confirmText: '合并',
    danger: false,
  })
  if (!answer) return
  clearSelection()
  void mergeRows(ids)
}

/**
 * @param {any} row
 */
async function removeRow(row) {
  const answer = await confirmAction({
    title: '删除这一行',
    message: '删除这一行；它的音频同时失效。',
    confirmText: '删除',
  })
  if (!answer) return
  selected.value = selected.value.filter((id) => id !== row.id)
  void deleteRow(row.id)
}

async function removeSelected() {
  if (!selected.value.length) {
    if (caretRow.value) await removeRow(caretRow.value)
    else say('把光标放进一行，或选中要删的行。')
    return
  }
  const count = selected.value.length
  const answer = await confirmAction({
    title: '删除选中行',
    message: `删除选中的 ${count} 行；这些行的旧音频同时失效。`,
    confirmText: '删除',
  })
  if (!answer) return
  const ids = [...selected.value]
  clearSelection()
  void deleteRows(ids)
}

/** 这一行落在哪台引擎上：按章音色优先，其次按说话人；与插入标记同一判据。 */
/** @param {any} row */
function engineOfRow(row) {
  const mapping =
    (chapterScope.value && detail.value?.chapter_voices?.[row.chapter_id]) ||
    detail.value?.voices?.[row.speaker || '旁白']
  return String(mapping?.engine || '')
}

/** 引擎自己登记的标签：只列发得出去的，不再算通用交集。 */
/** @param {any} row */
function marksOfRow(row) {
  const engine = engineOfRow(row)
  if (!engine) return { marks: [], reason: '这一行还没有分配音色' }
  const names = state.marks?.engine_marks?.[engine] || []
  const defs = new Map()
  for (const mark of state.marks?.common || []) defs.set(mark.name, mark)
  for (const group of state.marks?.engines || []) {
    for (const mark of group.marks || []) {
      if (!defs.has(mark.name)) defs.set(mark.name, mark)
    }
  }
  const marks = names
    .map((/** @type {string} */ name) => defs.get(name))
    .filter(Boolean)
  return { marks, reason: marks.length ? '' : '这台引擎没有声明语气词或已停用' }
}

/** 行菜单的上下文：右键与行尾「⋯」共用一份，批量项跟着眼前的选中走。 */
/** @param {any} row */
function rowMenuContext(row) {
  const inSelection = selected.value.includes(row.id)
  const count = inSelection ? selected.value.length : 0
  const mergeBySelection =
    inSelection && selected.value.length >= 2 && selectionContiguous.value
  const mergeByCaret = caret.value.rowId === row.id && Boolean(caretNext.value)
  const caretHere = caret.value.rowId === row.id
  const markInfo = caretHere ? marksOfRow(row) : { marks: [], reason: '' }
  return {
    status: row.status,
    busy: busy.value,
    generating: isGenerating(row),
    hasAudio: Boolean(row.audio),
    canSplit:
      caretHere &&
      caret.value.at > 0 &&
      caret.value.at < (textOf(row) || '').length,
    hasCaret: caretHere,
    marks: markInfo.marks,
    marksReason: markInfo.reason,
    insertMark: (/** @type {string} */ name) => insertMark(name),
    mergeLabel: mergeBySelection
      ? `合并所选 ${selected.value.length} 行`
      : mergeByCaret
        ? '与下一行合并'
        : '',
    selectionCount: count,
    text: textOf(row),
    speaker: roleOf(row),
    play: () => playRow(row),
    generate: () => generate(row),
    params: () => openParams(row),
    split: splitSelected,
    merge: mergeSelected,
    remove: () => {
      if (count > 1) void removeSelected()
      else void removeRow(row)
    },
  }
}

/** 右键不改已选中的行；点在选外先收成这一行，菜单范围与眼前的选中一致。 */
/** @param {any} row @param {MouseEvent} event */
function onRowMenu(row, event) {
  if (!canTakeOver(event)) return
  const target = event.target
  const field =
    target instanceof Element ? target.closest('.row-input') : null
  // 在行文本里右键：菜单里的语气词就插在这里，而不是整行末尾。
  if (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement) {
    caret.value = { rowId: row.id, at: field.selectionStart ?? 0 }
  } else if (!selected.value.includes(row.id)) {
    if (selected.value.length) clearSelection()
    if (caret.value.rowId !== row.id) {
      caret.value = { rowId: row.id, at: String(textOf(row) || '').length }
    }
  }
  openObjectMenu('audiobook.row', rowMenuContext(row), { event })
}

/** 自动合并：短于上限的旁白行并入下一行；对白行不参与。范围与上限在同一弹窗里给。 */
const autoMergeMax = ref(40)
const chapterCount = computed(() => detail.value?.chapters?.length || 0)

async function autoMerge() {
  if (!state.chapterId && !chapterCount.value) return
  const answer = await confirmAction({
    title: '自动合并旁白行',
    message: `短于上限的旁白行并入下一行，到达上限即停；对白行不动。全书共 ${chapterCount.value} 章。`,
    input: { type: 'number', placeholder: String(autoMergeMax.value) },
    value: String(autoMergeMax.value),
    actions: [
      { value: 'chapter', label: '合并当前章', disabled: rows.value.length < 2 },
      { value: 'book', label: '合并全书', disabled: !chapterCount.value },
    ],
  })
  if (!answer) return
  const cap = Math.round(Number(answer.value))
  if (!Number.isFinite(cap) || cap < 2) {
    say('字数上限要不小于 2。')
    return
  }
  autoMergeMax.value = cap
  if (answer.action === 'book') await autoMergeAllRows(cap)
  else await autoMergeRows(state.chapterId, cap)
}

watch(filter, () => {
  scrollTop.value = 0
  if (rowsWrap.value) rowsWrap.value.scrollTop = 0
  clearSelection()
})

watch(speakerFilter, () => {
  scrollTop.value = 0
  if (rowsWrap.value) rowsWrap.value.scrollTop = 0
  clearSelection()
})

watch(
  () => `${store.producer.key}:${state.detail?.id || ''}:${state.chapterId}`,
  () => {
    // 筛选档与角色是跨章条件：切章只清行内编辑态。
    scrollTop.value = 0
    caret.value = { rowId: '', at: 0 }
    drafts.value = {}
    columnMode.value = ''
    clearSelection()
    measure()
  }
)

/** 角色表：旁白固定第一行，其余按出现次数（判据在 `assign.js`，与全局浮层共用）。 */
const roles = computed(() => rolesOf(detail.value))

/** 可合并的角色：旁白不是角色，不参与；少于两个时按钮不出现。 */
const mergeableRoles = computed(() => roles.value.filter(/** @param {any} item */ (item) => item.name !== '旁白'))

/** @type {Record<string, string>} */
const STAGE_LABELS = {
  chapters: '分章',
  sentences: '拆句',
  analysis: '分析',
  audio: '生成',
  merged: '合并',
}

/**
 * @param {any} stage
 */
function stageLabel(stage) {
  return STAGE_LABELS[stage] || stage
}

/**
 * 字幕行的原时间码：`mm:ss.SSS`（过一小时才带小时）。
 *
 * @param {any} ms
 */
function timeOf(ms) {
  const total = Math.max(0, Math.round(ms))
  const hours = Math.floor(total / 3600000)
  const minutes = Math.floor((total % 3600000) / 60000)
  const seconds = Math.floor((total % 60000) / 1000)
  const millis = total % 1000
  const tail = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(millis).padStart(3, '0')}`
  return hours ? `${hours}:${tail}` : tail
}

const chapterAudioUrl = computed(() => {
  if (!detail.value || !chapter.value?.audio) return ''
  return `/api/v1/producers/${store.producer.key}/projects/${detail.value.id}/chapters/${chapter.value.id}/audio`
})

const voiceOf = computed(() => detail.value?.voices || {})
const chapterVoiceOf = computed(() => detail.value?.chapter_voices || {})

/** 没有识别出角色时，分配表按章列行：旁白没有角色可挑，读者只关心每章是谁念。 */
const chapterScope = computed(() => {
  const value = detail.value
  return Boolean(value && !namedSpeakersOf(value).length && (value.chapters || []).length)
})

/** 未单独指定过的章沿用的那颗音色：按章序取第一个已有音色的章。 */
const chapterDefaultVoice = computed(() => {
  for (const item of detail.value?.chapters || []) {
    const voice = chapterVoiceOf.value[item.id]
    if (voice?.voice_id) return voice
  }
  return null
})

/** 还没指定音色的角色（或章）；旁白也算一行——它在分配表里，只是不交给 AI。 */
const unassignedRoles = computed(() => {
  if (chapterScope.value) {
    return assignableRoles.value.filter(/** @param {any} item */ (item) => !item.current?.voice_id)
  }
  return roles.value.filter(/** @param {any} item */ (item) => !voiceOf.value[item.name]?.voice_id)
})

/** 分配弹窗的完整表：已指定音色的也在里面，用来调整。
 *  有角色时旁白同样是一行——它是全书最主要的那颗音色，只是不参与 AI 匹配；
 *  没有角色时一行一章，未单独指定的章显示沿用的那颗音色。 */
const assignableRoles = computed(() => {
  if (chapterScope.value) {
    return (detail.value?.chapters || []).map(
      /** @param {any} item */ (item) => ({
        key: item.id,
        chapter_id: item.id,
        name: item.title,
        rows: Number(item.rows) || 0,
        current: chapterVoiceOf.value[item.id] || chapterDefaultVoice.value || null,
      })
    )
  }
  return roles.value.map(/** @param {any} item */ (item) => ({
    key: item.name,
    name: item.name,
    rows: item.rows,
    current: voiceOf.value[item.name] || null,
  }))
})

/** 音色管理按钮的悬停说明：还差几个角色 / 章没指定，一眼看得出来。
 *  对话模型没配好只影响弹窗里的 AI 匹配，不影响手动指定，所以不在这里拦。 */
const assistTitle = computed(() => {
  if (running.value) return '任务运行中，等它结束再调整音色'
  const open = unassignedRoles.value.length
  if (chapterScope.value) {
    return open
      ? `音色管理（${open} 章还没指定音色）· 可逐章指定`
      : `音色管理（${assignableRoles.value.length} 章已指定音色）· 可逐章调整`
  }
  return open
    ? `音色管理（${open} 个角色还没指定音色）· 打开后可手动改或让 AI 匹配`
    : `音色管理（${assignableRoles.value.length} 个角色已指定）· 可手动改或让 AI 重新匹配`
})

/**
 * 合并角色：后端把行上的 speaker 统一改掉，前端只回填详情。
 *
 * @param {any} payload
 */
async function onMergeSpeakers(payload) {
  await mergeSpeakers(payload.names, payload.target)
  voiceToolsOpen.value = false
}

/**
 * @param {string} key
 */
function run(key) {
  void runStep(key)
}

function cancel() {
  void cancelJob()
}

// ---- 单行生成与行参数 ----------------------------------------------------

//: 正在单独生成的行：按钮换成转圈，避免连点重复提交。
//: 按行记而不是一个全局 id——一行在排队时，别的行仍可以点。
const generating = ref(/** @type {string[]} */ ([]))
//: 打开着参数弹窗的那一行（null = 弹窗关着）。
const paramsRow = ref(/** @type {any} */ (null))

/**
 * @param {any} row
 */
function isGenerating(row) {
  return generating.value.includes(row.id)
}

/**
 * 生成一行：待生成按钮、失败重试、菜单里的「重新生成」都走这一条。
 *
 * @param {any} row
 */
async function generate(row) {
  if (busy.value || isGenerating(row)) return
  generating.value = [...generating.value, row.id]
  const ordinal = row.ordinal + 1
  const again = row.status === 'audio' || row.status === 'stale'
  try {
    const next = await retryRow(row.id)
    // 重取回来的那一行才是结果：成功就报一声，失败的原因由 store 那边报。
    if (next?.status === 'audio') {
      say(again ? `第 ${ordinal} 行已重新生成` : `第 ${ordinal} 行已生成`, { kind: 'success' })
    }
  } finally {
    generating.value = generating.value.filter((id) => id !== row.id)
  }
}

/**
 * @param {any} row
 */
function openParams(row) {
  paramsRow.value = row
}

/**
 * 参数存完直接按新参数生成一次——改参数的目的就是重做这一行。
 *
 * @param {any} rowId
 */
async function onParamsSaved(rowId) {
  paramsRow.value = null
  const row = state.rows.find(/** @param {any} item */ (item) => item.id === rowId)
  if (row) await generate(row)
}

/**
 * @param {any} row
 * @param {any} field
 * @param {any} value
 */
function edit(row, field, value) {
  void editRow(row.id, { [field]: value })
}

async function resplit() {
  optionsOpen.value = false
  if (!chapter.value) return
  const count = Number(chapter.value.rows || 0)
  const answer = await confirmAction({
    title: '重拆本章',
    message: count
      ? `「${chapter.value.title}」${count} 行按当前选项重新切分：文本未变的行保留角色、情绪与音频；改过的行重建。`
      : '会按章节正文重新切行。',
    confirmText: '重拆',
  })
  if (!answer) return
  void resplitChapter()
}

/**
 * @param {any} row
 */
function rowAudioUrl(row) {
  const base = `/api/v1/producers/${store.producer.key}/projects/${detail.value.id}/rows/${row.id}/audio`
  // 带上版本号：地址不变时播放器会把这次点击当成「同一段，暂停/继续」，
  // 新音频根本装不进去；浏览器也可能拿旧缓存。
  // 版本取「音频文件的写入时刻」（`audio_version`）而不是参数指纹：
  // 重新生成同一行时参数没变、指纹也没变，只有文件被重写了。
  const version = row.audio_version || (row.audio_key || '').slice(0, 10)
  return version ? `${base}?v=${version}` : base
}

watch(
  () => plugin.value?.key,
  () => {
    if (!voices.value.length) void actions.loadAllVoices()
  },
  { immediate: true }
)

onMounted(() => {
  measure()
  window.addEventListener('resize', onResize)
  if (!store.producers.length) void actions.loadProducers()
  if (!voices.value.length) void actions.loadAllVoices()
  if (!detail.value && store.producer.key) void refresh()
  void loadAssistStatus()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (busyTimer) window.clearInterval(busyTimer)
  // 任务本身在后端继续跑；前端不该在别的页面也不停地问进度。
  stopJobPolling()
})
</script>

<style scoped src="./Workspace.css"></style>
