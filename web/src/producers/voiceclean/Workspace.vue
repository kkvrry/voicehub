<template>
  <section
    class="app-panel vc"
    @dragenter.prevent="onDragEnter"
    @dragover.prevent
    @dragleave.prevent="onDragLeave"
    @drop.prevent="onDrop"
  >
    <header class="panel-head">
      <div class="panel-head-main">
        <div class="vc-head-line">
          <div class="engine-title">音色处理</div>
          <button
            type="button"
            class="vc-icon-btn vc-head-play"
            :disabled="!targetSamples.length"
            title="按顺序试听处理序列"
            aria-label="按顺序试听处理序列"
            @click="playQueue"
          >
            <Play :size="14" :stroke-width="1.8" fill="currentColor" />
          </button>
        </div>
      </div>
      <div class="vc-head-acts">
        <div
          v-if="headLabel"
          class="vc-head-state"
          :class="{ 'is-live': running, 'is-ok': job?.state === 'applied' }"
        >
          <LoaderCircle v-if="running" class="vc-spin" :size="14" :stroke-width="1.8" />
          <Check v-else-if="job?.state === 'applied'" :size="14" :stroke-width="2" />
          {{ headLabel }}
        </div>
        <button
          type="button"
          class="sh-btn is-ghost vc-head-file"
          :disabled="running || busy !== ''"
          title="选择电脑里的音频"
          @click="onPickFile"
        >
          <LoaderCircle v-if="busy === 'upload'" class="vc-spin" :size="14" :stroke-width="1.8" />
          <Upload v-else :size="14" :stroke-width="1.8" />
          {{ busy === 'upload' ? '上传中…' : '选择文件' }}
        </button>
      </div>
    </header>

    <div v-if="error" class="vc-error vc-body">{{ error }}</div>
    <div v-else-if="!loaded" class="muted vc-body">载入中…</div>

    <template v-else>
      <div class="vc-body">
        <div v-if="!voice && state.loadingVoice" class="muted vc-loading">读取样例…</div>
        <div v-else-if="!voice" class="vc-empty" :class="{ 'is-drag': dragging }">
          <span class="vc-empty-icon"><AudioLines :size="24" :stroke-width="1.6" /></span>
          <span class="vc-empty-title">从左侧选一个音色，或放入一个文件</span>
          <span class="vc-empty-hint">
            在左栏点卡片把它的样例放进处理序列；卡右「添加」把整张卡追加进来，可以混着多个音色。也可以把音频拖到这里，或从电脑选一个。
          </span>
          <button
            type="button"
            class="sh-btn is-soft vc-empty-pick"
            :disabled="busy !== ''"
            @click="onPickFile"
          >
            <Upload :size="14" :stroke-width="1.8" />
            {{ busy === 'upload' ? '上传中…' : '选择电脑里的文件' }}
          </button>
          <span class="vc-empty-formats">WAV · FLAC · MP3 · M4A · AAC · OGG · OPUS</span>
        </div>

        <template v-else>
          <!-- 处理链：模型步骤可勾选，收尾步骤可跳过。模型来自运行中的 audio.cpp 实例。 -->
          <section class="vc-chain">
            <div class="vc-chain-head">
              <span class="vc-chain-title">处理链</span>
              <span v-if="chainSummary" class="vc-chain-sum">{{ chainSummary }}</span>
              <div class="vc-chain-acts">
                <button
                  v-if="!running"
                  type="button"
                  class="sh-btn is-primary vc-chain-run"
                  :disabled="!canStart"
                  :title="startHint"
                  @click="onStart"
                >
                  {{ startLabel }}
                </button>
                <span v-else class="vc-chain-live">
                  <LoaderCircle class="vc-spin" :size="14" :stroke-width="1.8" />
                  {{ state.job?.finished || 0 }} / {{ state.job?.total || 0 }}
                </span>
              </div>
            </div>
            <div class="vc-step-grid">
              <div
                v-for="step in steps"
                :key="step.key"
                class="vc-step"
                :class="{ on: pickOn(step), missing: !hasReady(step), locked: lockedEdit }"
                role="button"
                :tabindex="hasReady(step) && !lockedEdit ? 0 : -1"
                :aria-pressed="pickOn(step)"
                :aria-disabled="!hasReady(step)"
                @click="onToggleStep(step, $event)"
                @keydown.enter.prevent="onToggleStep(step, $event)"
                @keydown.space.prevent="onToggleStep(step, $event)"
              >
                <div class="vc-step-top">
                  <span class="vc-step-no">{{ step.order }}</span>
                  <span class="vc-step-name">{{ step.label }}</span>
                  <span class="vc-step-state" :class="{ on: pickOn(step) }">
                    <Check v-if="pickOn(step)" :size="12" :stroke-width="2" />
                    {{ stepState(step) }}
                  </span>
                </div>
                <button
                  type="button"
                  class="vc-step-model"
                  :class="{ 'is-empty': !currentModel(step) }"
                  :disabled="lockedEdit || !step.models?.length"
                  @click.stop="openModelForm(step, $event)"
                >
                  <span class="vc-step-model-name">
                    {{ currentModel(step)?.label || '选择模型' }}
                  </span>
                  <ChevronDown :size="12" :stroke-width="1.8" />
                </button>
                <span
                  v-if="stepJob(step)"
                  class="vc-step-note"
                  :class="{ 'is-warn': isFailedModelJob(stepJob(step)) }"
                >
                  {{ jobLabel(stepJob(step)) }}
                </span>
                <span v-else class="vc-step-note">{{ noteOf(step) }}</span>
              </div>

              <div
                class="vc-step is-core"
                :class="{ on: state.normalize, locked: lockedEdit }"
                role="button"
                :tabindex="lockedEdit ? -1 : 0"
                :aria-pressed="state.normalize"
                @click="onToggleNormalize"
                @keydown.enter.prevent="onToggleNormalize"
                @keydown.space.prevent="onToggleNormalize"
              >
                <div class="vc-step-top">
                  <span class="vc-step-no">{{ core.order }}</span>
                  <span class="vc-step-name">{{ core.label }}</span>
                  <span class="vc-step-state" :class="{ on: state.normalize }">
                    <Check v-if="state.normalize" :size="12" :stroke-width="2" />
                    {{ state.normalize ? '已启用' : '已跳过' }}
                  </span>
                </div>
                <span class="vc-step-note">{{ core.note }}</span>
              </div>
            </div>

            <Popover
              :open="Boolean(activeStep)"
              :anchor="modelAnchor"
              :width="366"
              label="选择模型"
              @close="closeModelForm"
            >
              <div v-if="activeStep" class="vc-model-form">
                <div class="vc-model-form-head">
                  <span class="vc-model-form-title">{{ activeStep.label }}</span>
                  <span class="vc-model-form-hint">未下载的直接下载；下载后自动加载。</span>
                </div>
                <div class="vc-model-list">
                  <div
                    v-for="model in activeStep.models"
                    :key="model.id"
                    class="vc-model-row"
                    :class="{
                      on: pickOf(activeStep).model === model.id,
                      ready: model.downloaded,
                    }"
                  >
                    <button
                      type="button"
                      class="vc-model-pick"
                      :disabled="!model.downloaded || lockedEdit"
                      @click="chooseModel(activeStep, model)"
                    >
                      <span class="vc-model-name">
                        {{ model.label }}
                        <span v-if="model.recommended" class="vc-model-tag">推荐</span>
                      </span>
                      <span class="vc-model-note">{{ model.note }}</span>
                    </button>
                    <div class="vc-model-side">
                      <template v-if="isActiveModelJob(model.job)">
                        <span class="vc-model-job">{{ jobLabel(model.job) }}</span>
                      </template>
                      <template v-else-if="isFailedModelJob(model.job)">
                        <span
                          class="vc-model-job is-error"
                          :title="model.job.message"
                        >
                          {{ model.job.message || '失败' }}
                        </span>
                        <button
                          v-if="!model.downloaded"
                          type="button"
                          class="sh-btn is-soft vc-model-download"
                          :disabled="modelActive || lockedEdit"
                          @click="downloadModel(activeStep, model)"
                        >
                          <Download :size="12" :stroke-width="1.8" />
                          重试
                        </button>
                      </template>
                      <template v-else>
                        <button
                          v-if="!model.downloaded"
                          type="button"
                          class="sh-btn is-soft vc-model-download"
                          :disabled="modelActive || lockedEdit"
                          @click="downloadModel(activeStep, model)"
                        >
                          <Download :size="12" :stroke-width="1.8" />
                          下载
                        </button>
                        <span
                          v-else-if="pickOf(activeStep).model === model.id"
                          class="vc-model-current"
                        >
                          当前
                        </span>
                        <span v-else class="vc-model-pickable">选择</span>
                      </template>
                    </div>
                  </div>
                </div>
              </div>
            </Popover>
          </section>

          <!-- 目标音色：头像、名字、样例选择、波形与裁剪都在这一张卡里。
               序列里有多张音色卡时，顶上排成一行页签：每张卡一个窗口，切换不混行。 -->
          <section class="vc-target">
            <div v-if="windows.length > 1" class="vc-tabs" role="tablist" aria-label="处理序列里的音色卡">
              <div
                v-for="win in windows"
                :key="win.voiceId"
                class="vc-tab"
                :class="{ on: win.active }"
                role="tab"
                :aria-selected="win.active"
                :tabindex="win.active ? 0 : -1"
                @click="openWindow(win.voiceId)"
                @keydown.enter.prevent="openWindow(win.voiceId)"
                @contextmenu="onTabMenu(win, $event)"
              >
                <span class="vc-tab-name">{{ win.name }}</span>
                <span class="vc-tab-count">{{ win.count }}</span>
                <button
                  type="button"
                  class="vc-tab-x"
                  :disabled="lockedSelect"
                  :title="`把「${win.name}」移出处理序列`"
                  :aria-label="`把「${win.name}」移出处理序列`"
                  @click.stop="onCloseWindow(win.voiceId)"
                >
                  <X :size="12" :stroke-width="1.8" />
                </button>
              </div>
            </div>
            <div class="vc-target-head">
              <button
                v-if="!isFile"
                type="button"
                class="vc-avatar vc-avatar-play"
                :class="{ 'is-playing': voicePlaying }"
                :style="avatarStyle"
                :disabled="!voice?.main_audio_url"
                title="按顺序播放这条音色的样例"
                aria-label="按顺序播放这条音色的样例"
                @click="playVoiceQueue"
              >
                {{ initial }}
                <span class="vc-avatar-play-hint" aria-hidden="true">
                  <Pause v-if="voicePlaying" :size="16" :stroke-width="1.8" fill="currentColor" />
                  <Play v-else :size="16" :stroke-width="1.8" fill="currentColor" />
                </span>
              </button>
              <span v-else class="vc-avatar" :style="avatarStyle">
                <FileAudio :size="20" :stroke-width="1.8" />
              </span>
              <div class="vc-target-ident">
                <div class="vc-target-line">
                  <span class="vc-target-name">{{ voice.shown_name }}</span>
                  <span v-if="isFile" class="vc-target-tag">电脑文件</span>
                  <span v-if="voice.is_design" class="vc-target-tag">设计</span>
                  <span v-if="!isLocal" class="vc-target-tag is-warn">平台音色</span>
                </div>
                <div class="vc-target-sub">{{ targetSub() }}</div>
              </div>
              <div class="vc-target-acts">
                <button
                  v-if="isFile"
                  type="button"
                  class="sh-btn is-soft vc-mini-btn"
                  :disabled="running || busy !== ''"
                  @click="onPickFile"
                >
                  换个文件
                </button>
                <button
                  type="button"
                  class="vc-icon-btn vc-target-close"
                  title="清除目标"
                  aria-label="清除目标"
                  :disabled="running || busy !== ''"
                  @click="onClearTarget"
                >
                  <X :size="14" :stroke-width="1.8" />
                </button>
              </div>
            </div>

            <div class="vc-audios">
              <template v-for="(sample, position) in targetSamples" :key="sample.key">
                <div
                  class="vc-audio"
                  :class="{ 'is-trim': editor === sample.key }"
                  @click="onAudioClick(sample, $event)"
                  @contextmenu="onSampleMenu(sample, $event)"
                >
                  <div class="vc-audio-main">
                    <button
                      type="button"
                      class="vc-audio-play"
                      :class="{ on: playingSample(sample) }"
                      :disabled="!sample.audio_url"
                      :title="playingSample(sample) ? '暂停' : '试听原声'"
                      :aria-label="playingSample(sample) ? '暂停' : '试听原声'"
                      @click="playSample(sample)"
                    >
                      <Pause v-if="playingSample(sample)" :size="13" :stroke-width="1.8" fill="currentColor" />
                      <Play v-else :size="13" :stroke-width="1.8" fill="currentColor" />
                    </button>
                    <div class="vc-audio-text">
                      <div class="vc-audio-title" :title="sample.file">{{ sampleTitle(sample) }}</div>
                      <div class="vc-audio-meta">
                        <span v-if="sample.is_main && !isFile" class="vc-mini-tag">主样例</span>
                        <span v-if="isFile" class="vc-mini-tag">电脑文件</span>
                        <span v-if="emotionOf(sample)" class="vc-mini-tag">{{ emotionOf(sample) }}</span>
                        <span v-if="languageOf(sample)" class="vc-mini-tag">{{ languageOf(sample) }}</span>
                        <span v-if="isTrimmed(sample)" class="vc-mini-tag is-cut">已裁剪</span>
                        <span class="vc-audio-file">
                          {{ formatOf(sample) }}<template v-if="targetSamples.length > 1"> · 第 {{ Number(position) + 1 }} 条</template>
                        </span>
                      </div>
                    </div>
                  </div>

                  <span class="vc-audio-dur">{{ rowDuration(sample) }}</span>

                  <span class="vc-audio-acts">
                    <span
                      v-if="statusOf(sample).label"
                      class="vc-status"
                      :class="`is-${statusOf(sample).key}`"
                      :title="statusOf(sample).hint || ''"
                    >
                      {{ statusOf(sample).label }}
                    </span>
                    <span v-if="canCompare(sample)" class="vc-ab">
                      <button
                        type="button"
                        :class="{ on: playingSide(itemFor(sample.key).index, 'origin') }"
                        @click="play(itemFor(sample.key).index, 'origin')"
                      >
                        原声
                      </button>
                      <button
                        type="button"
                        :class="{ on: playingSide(itemFor(sample.key).index, 'result') }"
                        :disabled="itemFor(sample.key).state === 'failed'"
                        @click="play(itemFor(sample.key).index, 'result')"
                      >
                        处理
                      </button>
                    </span>
                    <button
                      type="button"
                      class="vc-icon-btn"
                      :disabled="lockedSelect || state.windows.length <= 1"
                      title="只处理这张卡"
                      aria-label="只处理这张卡"
                      @click="onOnlyRow(sample.key)"
                    >
                      <Focus :size="14" :stroke-width="1.8" />
                    </button>
                    <button
                      type="button"
                      class="vc-icon-btn vc-audio-remove"
                      :disabled="lockedSelect"
                      :title="isFile ? '清除这个文件' : '移出处理序列'"
                      :aria-label="isFile ? '清除这个文件' : '移出处理序列'"
                      @click="onRemoveRow(sample.key)"
                    >
                      <X :size="14" :stroke-width="1.8" />
                    </button>
                  </span>

                  <div
                    v-if="editor === sample.key && editingSample"
                    class="vc-trim"
                    @keydown="onTrimKey(sample, $event)"
                  >
                    <div class="vc-trim-bar">
                      <span class="vc-trim-stats">{{ trimStats(sample) }}</span>
                      <span class="vc-trim-undo">
                        <button
                          type="button"
                          class="vc-icon-btn"
                          :disabled="!canUndo(sample.key)"
                          title="撤销（Ctrl+Z）"
                          aria-label="撤销裁剪"
                          @click="undoTrim(sample.key)"
                        >
                          <Undo2 :size="14" :stroke-width="1.8" />
                        </button>
                        <button
                          type="button"
                          class="vc-icon-btn"
                          :disabled="!canRedo(sample.key)"
                          title="前进（Ctrl+Shift+Z）"
                          aria-label="前进裁剪"
                          @click="redoTrim(sample.key)"
                        >
                          <Redo2 :size="14" :stroke-width="1.8" />
                        </button>
                      </span>
                    </div>
                    <Waveform
                      :url="sample.audio_url"
                      :duration="sample.duration"
                      :segments="segmentsOf(sample)"
                      :selection="selectionOf(sample.key) || undefined"
                      :revision="state.waveRevision"
                      editable
                      @select="onSelect(sample, $event)"
                    />
                    <span class="vc-trim-hint">{{ trimHint(sample) }}</span>
                    <div class="vc-trim-bar">
                      <button
                        type="button"
                        class="sh-btn is-soft"
                        :disabled="!hasSelection(sample)"
                        title="删除选中的这一段音频（Delete）"
                        @click="deleteSelection(sample)"
                      >
                        <Trash2 :size="14" :stroke-width="1.8" />
                        删除选区
                      </button>
                      <button
                        type="button"
                        class="sh-btn is-ghost"
                        :disabled="!hasSelection(sample)"
                        title="只留下选中的这一段"
                        @click="keepSelection(sample)"
                      >
                        仅保留选区
                      </button>
                      <button
                        type="button"
                        class="sh-btn is-ghost"
                        :disabled="!isTrimmed(sample)"
                        @click="resetTrim(sample.key)"
                      >
                        恢复原样
                      </button>
                      <span class="vc-trim-spacer"></span>
                      <button
                        type="button"
                        class="sh-btn is-soft"
                        :disabled="!sample.audio_url"
                        @click="onPreview(sample)"
                      >
                        <Pause v-if="isPreviewing(sample)" :size="14" :stroke-width="1.8" fill="currentColor" />
                        <Play v-else :size="14" :stroke-width="1.8" fill="currentColor" />
                        {{ isPreviewing(sample) ? '暂停结果' : '试听结果' }}
                      </button>
                      <button
                        type="button"
                        class="sh-btn is-ghost"
                        :disabled="!hasSelection(sample)"
                        @click="onPreviewSelection(sample)"
                      >
                        <Pause
                          v-if="isPreviewing(sample, 'selection')"
                          :size="14"
                          :stroke-width="1.8"
                          fill="currentColor"
                        />
                        <Play v-else :size="14" :stroke-width="1.8" fill="currentColor" />
                        {{ isPreviewing(sample, 'selection') ? '暂停选区' : '试听选区' }}
                      </button>
                    </div>
                  </div>
                </div>
              </template>

              <div v-if="state.loadingVoice" class="vc-hint vc-samples-empty">读取样例…</div>
              <div v-else-if="!targetSamples.length" class="vc-hint vc-samples-empty">
                没有可处理的音频。
              </div>
            </div>

            <div v-if="!isLocal" class="vc-warn-line">
              平台音色的样例在引擎目录里，换掉会让平台侧的参考音频指向被替换的文件。请选本地音色。
            </div>
          </section>
        </template>
      </div>

      <footer class="vc-dock">
        <div class="vc-dock-card">
          <div class="vc-dock-in">
            <div class="vc-dock-sum">
              <span class="vc-dock-title">{{ dockTitle }}</span>
              <span class="vc-dock-sub">{{ dockSub }}</span>
              <div v-if="running" class="vc-progress">
                <i :style="{ width: `${percent}%` }"></i>
              </div>
            </div>
            <div class="vc-dock-acts">
              <template v-if="running">
                <button type="button" class="sh-btn is-ghost" @click="onCancel">取消</button>
                <button type="button" class="sh-btn is-primary" disabled>处理中…</button>
              </template>
              <template v-else-if="dockMode === 'apply'">
                <button type="button" class="sh-btn is-ghost" :disabled="busy !== ''" @click="onResetRun">
                  返回编辑
                </button>
                <button
                  v-if="savableCount"
                  type="button"
                  class="sh-btn is-soft"
                  :disabled="busy !== ''"
                  title="把结果另存为这条音色的新样例，原样例保留"
                  @click="onSave"
                >
                  <BookPlus :size="14" :stroke-width="1.8" />
                  {{ busy === 'save' ? '入库中…' : `入库 ${savableCount} 条` }}
                </button>
                <template v-if="applicableCount">
                  <button
                    v-if="confirmApply"
                    type="button"
                    class="sh-btn is-ghost"
                    :disabled="busy !== ''"
                    @click="cancelApply"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    class="sh-btn"
                    :class="confirmApply ? 'is-danger' : 'is-primary'"
                    :disabled="busy !== ''"
                    :title="confirmApply ? '再点一次才会覆盖原样例' : '覆盖原样例，需要再点一次确认'"
                    @click="onApply"
                  >
                    {{
                      busy === 'apply'
                        ? '替换中…'
                        : confirmApply
                          ? `确认替换 ${applicableCount} 条`
                          : `替换 ${applicableCount} 条`
                    }}
                  </button>
                </template>
              </template>
              <template v-else-if="dockMode === 'download'">
                <button type="button" class="sh-btn is-ghost" :disabled="busy !== ''" @click="onResetRun">
                  重新处理
                </button>
                <button
                  type="button"
                  class="sh-btn is-primary"
                  :title="downloadName()"
                  :disabled="busy !== ''"
                  @click="onDownload"
                >
                  下载处理结果
                </button>
              </template>
              <button
                v-else-if="dockMode === 'saved'"
                type="button"
                class="sh-btn is-primary"
                :disabled="busy !== ''"
                @click="onResetRun"
              >
                继续编辑
              </button>
              <button
                v-else-if="dockMode === 'applied'"
                type="button"
                class="sh-btn is-primary"
                :disabled="busy !== ''"
                @click="onResetRun"
              >
                继续编辑
              </button>
              <button
                v-else-if="dockMode === 'retry'"
                type="button"
                class="sh-btn is-primary"
                :disabled="busy !== ''"
                @click="onResetRun"
              >
                重新处理
              </button>
              <span v-else class="vc-dock-hint">
                在「处理链」生成结果后，在这里{{ isFile ? '下载' : '确认替换或入库' }}。
              </span>
            </div>
          </div>
          <div v-if="confirmApply" class="vc-dock-note is-warn">
            替换会覆盖原样例，原件进回收站；再点一次确认。
          </div>
          <div v-if="dockNote" class="vc-dock-note" :class="{ 'is-warn': dockWarn }">{{ dockNote }}</div>
        </div>
      </footer>
    </template>

    <input
      ref="fileInput"
      class="vc-file-input"
      type="file"
      accept=".wav,.flac,.mp3,.m4a,.aac,.ogg,.opus"
      @change="onFileChange"
    />

    <div v-if="dragging" class="vc-drop" aria-hidden="true">
      <span class="vc-drop-icon"><Upload :size="26" :stroke-width="1.7" /></span>
      <span class="vc-drop-title">松开以处理这个文件</span>
      <span class="vc-drop-hint">WAV · FLAC · MP3 · M4A · AAC · OGG · OPUS</span>
    </div>
  </section>
</template>

<script setup>
// @ts-check
// 工作区只有一屏：处理链 → 目标音色与样例列表 → 底栏动作。
// 不另设结果表：处理结果回到同一条样例上，状态与 A/B 都在行内。
// 模型与地址来自运行中的 audio.cpp 实例；没有实例时仍可只裁剪。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  AudioLines,
  BookPlus,
  Check,
  ChevronDown,
  Download,
  FileAudio,
  Focus,
  LoaderCircle,
  Pause,
  Play,
  Redo2,
  Trash2,
  Undo2,
  Upload,
  X,
} from 'lucide-vue-next'
import { actions, labelOf, say, store, vocab } from '../../store'
import Popover from '../../components/ui/Popover.vue'
import { useSettingsStore } from '../../store/settings'
import { avatarFill } from '../../composables/avatarColor'
import { canTakeOver, openObjectMenu } from '../../composables/contextMenu'
import Waveform from './Waveform.vue'
import {
  activateWindow,
  allRows,
  applySelected,
  cancel,
  canRedo,
  canUndo,
  clearTarget,
  closeWindow,
  currentModel,
  deleteSelection,
  downloadModel,
  downloadName,
  downloadResult,
  enabledSteps as enabledStepNames,
  hasSelection,
  isActiveModelJob,
  isFailedModelJob,
  isFileTarget,
  isLocalVoice,
  isRunning,
  isTrimmed,
  itemFor,
  jobForTarget,
  keepSelection,
  keptSeconds,
  loadTarget,
  modelActive,
  onlyThis,
  pickOf,
  play,
  playingSide,
  playSample,
  previewSelection,
  previewTrim,
  queueRows,
  readyModels,
  refresh,
  rowKey,
  redoTrim,
  removeFromQueue,
  reset as resetWorkspace,
  resetRun,
  resetTrim,
  restore,
  saveSelected,
  segmentsOf,
  selectModel,
  selectionOf,
  setSelection,
  start,
  state,
  statusOf,
  stopPreview,
  targetSub,
  targetVoice,
  toggleNormalize,
  toggleStep,
  undoTrim,
  uploadSource,
  windowViews,
} from './store'

const editor = ref('')
/** 替换的二次确认：第一次点击只进确认态，第二次才覆盖。 */
const confirmApply = ref(false)
let confirmTimer = 0
const settings = useSettingsStore()
const fileInput = ref(/** @type {HTMLInputElement|null} */ (null))
const dragging = ref(false)
let dragDepth = 0

const loaded = computed(() => state.loaded)
const error = computed(() => state.error)
const voice = computed(() => targetVoice())
const isFile = computed(() => isFileTarget())
const isLocal = computed(() => isLocalVoice())
const avatarStyle = computed(() => ({
  background: avatarFill(voice.value?.shown_name || '?', settings.appearance.look),
}))
// 行表是当前窗口（这张音色卡）的样例；整个序列按音色卡分窗口。
const targetSamples = computed(() => queueRows())
const selectedRows = computed(() => allRows())
const windows = computed(() => windowViews())
const selectedCount = computed(() => selectedRows.value.length)
const steps = computed(() => state.data?.steps || [])
const core = computed(
  () => state.data?.core || { order: 3, label: '统一声道与峰值', note: '' }
)
const activeSteps = computed(() => enabledStepNames())
const job = computed(() => jobForTarget())
const running = computed(() => isRunning() || state.job?.state === 'running')
const busy = computed(() => state.busy || '')
const lockedEdit = computed(() => running.value || Boolean(job.value) || busy.value !== '')
const lockedSelect = computed(() => running.value || busy.value === 'apply')
const editingSample = computed(
  () => targetSamples.value.find((/** @type {any} */ row) => row.key === editor.value) || null
)
const selectedSeconds = computed(() =>
  selectedRows.value.reduce(
    (/** @type {number} */ sum, /** @type {any} */ row) => sum + keptSeconds(row),
    0
  )
)
const percent = computed(() => {
  const current = job.value || (state.job?.state === 'running' ? state.job : null)
  if (!current?.total) return 0
  return Math.round((current.finished / current.total) * 100)
})
const queuedKeys = computed(
  () => new Set(targetSamples.value.map((/** @type {any} */ row) => row.key))
)
const applicableCount = computed(
  () =>
    (job.value?.items || []).filter(
      (/** @type {any} */ row) =>
        queuedKeys.value.has(rowKey(row.voice_id, row.file)) &&
        row.state === 'done' &&
        row.replaceable
    ).length
)
/** 可入库的条目：完成即可，时长偏差不拦——入库是新样例，不涉及对齐。 */
const savableCount = computed(
  () =>
    (job.value?.items || []).filter(
      (/** @type {any} */ row) =>
        queuedKeys.value.has(rowKey(row.voice_id, row.file)) && row.state === 'done'
    ).length
)
/** 电脑文件的结果是否已经可下载。 */
const downloadable = computed(
  () =>
    (job.value?.items || []).some(
      (/** @type {any} */ row) => row.state === 'done' && row.replaceable
    )
)
/** 选中的行里有没有裁剪改动：没有处理步骤时，这就是唯一要做的事。 */
const trimEdits = computed(() => selectedRows.value.some((/** @type {any} */ row) => isTrimmed(row)))
const canStart = computed(
  () =>
    Boolean(voice.value) &&
    isLocal.value &&
    selectedCount.value > 0 &&
    (activeSteps.value.length > 0 || trimEdits.value) &&
    !running.value &&
    !job.value &&
    busy.value === ''
)
const chainSummary = computed(
  () => activeSteps.value.join(' · ') || (trimEdits.value ? '仅裁剪' : '')
)
const startLabel = computed(() => {
  if (busy.value === 'run') return '提交中…'
  if (job.value) return job.value.state === 'cancelled' ? '已取消' : '结果已生成'
  return activeSteps.value.length ? '开始处理' : trimEdits.value ? '应用裁剪' : '开始处理'
})
const startHint = computed(() => {
  if (!voice.value) return '先从左侧选一个音色，或放入一个文件'
  if (!isLocal.value) return '平台音色的样例不在本机'
  if (!selectedCount.value) return '处理序列里还没有音频'
  if (job.value) return '结果已经生成：先在下方应用，或返回编辑'
  if (!activeSteps.value.length && !trimEdits.value) return '选择处理步骤，或先在波形上裁剪'
  if (busy.value) return '正在忙，稍后再试'
  return ''
})
const headLabel = computed(() => {
  if (running.value) return `${state.job?.finished || 0} / ${state.job?.total || 0}`
  if (job.value?.state === 'applied') return '已替换'
  if (job.value?.state === 'saved') return '已入库'
  if (job.value?.state === 'done') return '已处理'
  if (job.value?.state === 'cancelled') return '已取消'
  return ''
})
const dockMode = computed(() => {
  if (running.value) return 'running'
  const current = job.value
  if (!current) return 'idle'
  if (current.scope?.kind === 'file') {
    if (current.state === 'done') return downloadable.value ? 'download' : 'retry'
    if (current.state === 'cancelled') return 'retry'
    return 'idle'
  }
  if (current.state === 'applied') return 'applied'
  if (current.state === 'saved') return 'saved'
  if (current.state === 'done') {
    return applicableCount.value || savableCount.value ? 'apply' : 'retry'
  }
  if (current.state === 'cancelled') return 'retry'
  return 'idle'
})
const dockTitle = computed(() => {
  if (!voice.value) return '从左侧选一个音色，或放入一个文件'
  if (running.value) {
    const current = state.job
    return job.value
      ? `处理中 ${current?.finished || 0} / ${current?.total || 0}`
      : '另一条音色的任务还在处理'
  }
  if (job.value?.state === 'applied') {
    return `已替换 ${state.applied?.replaced ?? 0} 条`
  }
  if (job.value?.state === 'saved') {
    return `已入库 ${state.saved?.saved ?? 0} 条`
  }
  if (job.value?.state === 'done') {
    const failed = job.value.failed ? ` · ${job.value.failed} 条失败` : ''
    return `处理完成 ${job.value.finished} / ${job.value.total}${failed}`
  }
  if (job.value?.state === 'cancelled') return '已取消，未写入音色库'
  return `处理序列 ${selectedCount.value} 条${selectedCount.value ? ` · 约 ${selectedSeconds.value.toFixed(1)} s` : ''}`
})
const dockSub = computed(() => {
  if (!voice.value) return '点卡片选中音色，卡右「添加」把样例加入处理序列；也可以把音频拖进来，或从电脑选一个。'
  if (running.value) {
    const current = state.job?.current
    return job.value
      ? current
        ? `正在处理 ${current}`
        : '正在准备第一条…'
      : '先等它结束或取消，再处理这一条。'
  }
  if (isFile.value) {
    if (job.value?.state === 'done') {
      return downloadable.value
        ? '文件不写回音色库；点「下载处理结果」，或重新处理。'
        : '没有可下载的结果：看行内原因，或重新处理。'
    }
    if (job.value?.state === 'cancelled') return '停在中途，未完成的结果不会保留。'
    if (!activeSteps.value.length && !trimEdits.value) {
      return '选择处理步骤，或先在波形上裁剪。'
    }
    return `${chainSummary.value} · 生成后下载`
  }
  if (job.value?.state === 'applied') return '原件在回收站；要再处理一轮就点「继续编辑」。'
  if (job.value?.state === 'saved') {
    return '原样例还在，结果已作为新样例入库；要再处理一轮就点「继续编辑」。'
  }
  if (job.value?.state === 'done') {
    if (applicableCount.value) {
      return '入库另存一条新样例；替换覆盖原样例（原件进回收站）。'
    }
    return savableCount.value
      ? '时长与原样例不一致，不能替换；可以入库为新样例。'
      : '没有可写入的条目：看下行内原因，或重新处理。'
  }
  if (job.value?.state === 'cancelled') return '停在中途，未完成的条目不会写入音色库。'
  if (!activeSteps.value.length && !trimEdits.value) {
    return '选择处理步骤，或先展开裁剪；生成结果后在这里确认。'
  }
  return `${chainSummary.value} · 生成结果后在这里确认`
})
const appliedNote = computed(() => {
  const payload = state.applied
  if (!payload) return ''
  const failures = payload.failures?.length || 0
  if (!failures) return '原件已放进回收站。'
  const reasons = (payload.failures || [])
    .map((/** @type {any} */ row) => row.reason)
    .filter(Boolean)
    .slice(0, 2)
    .join('；')
  return `${failures} 条没有替换${reasons ? `：${reasons}` : ''}`
})
const savedNote = computed(() => {
  const payload = state.saved
  if (!payload) return ''
  const files = (payload.files || [])
    .map((/** @type {any} */ row) => String(row.file || ''))
    .filter(Boolean)
  const head = files.length
    ? `已入库：${files.slice(0, 3).join('、')}${files.length > 3 ? '…' : ''}`
    : ''
  const failures = payload.failures?.length || 0
  if (!failures) return head
  return `${head ? `${head}；` : ''}${failures} 条没有入库`
})
const dockNote = computed(() => appliedNote.value || savedNote.value)
const dockWarn = computed(() =>
  Boolean(state.applied?.failures?.length || state.saved?.failures?.length)
)
const initial = computed(() => (voice.value?.shown_name || '?').slice(0, 1))

/** @param {any} step */
function hasReady(step) {
  return readyModels(step).length > 0
}

/** @param {any} step */
function pickOn(step) {
  return Boolean(pickOf(step).enabled)
}

/** @param {any} step */
function noteOf(step) {
  if (!step.models?.length) return step.reason || step.note
  const model = currentModel(step)
  return model?.note || step.note
}

/** @param {any} job */
function jobLabel(job) {
  const state = String(job?.state || '')
  if (state === 'downloading') {
    const progress = Math.round(Number(job?.progress) || 0)
    return progress > 0 ? `下载中 ${progress}%` : '下载中'
  }
  if (state === 'queued') return '等待下载'
  if (state === 'loading') return '加载中'
  if (isFailedModelJob(job)) return String(job?.message || '失败')
  return '已就绪'
}

/** 卡片上只展示当前模型的失败，或任何模型的进行中任务；完成的任务不占说明行。 */
/** @param {any} step */
function stepJob(step) {
  const chosen = currentModel(step)
  if (chosen?.job && (isActiveModelJob(chosen.job) || isFailedModelJob(chosen.job))) {
    return chosen.job
  }
  for (const model of step.models || []) {
    if (isActiveModelJob(model.job)) return model.job
  }
  return null
}

/** @param {any} step */
function stepState(step) {
  if (!step.models?.length) return '无模型'
  if (!hasReady(step)) {
    return (step.models || []).some((/** @type {any} */ row) => isActiveModelJob(row.job))
      ? '下载中'
      : '待下载'
  }
  if (!currentModel(step)) return '待选择'
  return pickOn(step) ? '已启用' : '未启用'
}

const modelMenu = ref('')
const modelAnchor = ref(/** @type {any} */ (null))
const activeStep = computed(
  () => steps.value.find((/** @type {any} */ row) => row.key === modelMenu.value) || null
)

/** @param {any} step @param {MouseEvent} event */
function openModelForm(step, event) {
  if (lockedEdit.value) return
  modelMenu.value = step.key
  modelAnchor.value =
    event.currentTarget instanceof HTMLElement ? event.currentTarget : null
}

function closeModelForm() {
  modelMenu.value = ''
  modelAnchor.value = null
}

/** @param {any} step @param {any} model */
function chooseModel(step, model) {
  if (lockedEdit.value) return
  selectModel(step, model)
  closeModelForm()
}

/** @param {any} step @param {any} event */
function onToggleStep(step, event) {
  if (lockedEdit.value) return
  if (!currentModel(step) && readyModels(step).length > 1) {
    const card = event?.currentTarget
    const button =
      card instanceof HTMLElement ? card.querySelector('.vc-step-model') : null
    modelMenu.value = step.key
    modelAnchor.value =
      button instanceof HTMLElement ? button : card instanceof HTMLElement ? card : null
    return
  }
  toggleStep(step)
}

function onToggleNormalize() {
  if (lockedEdit.value) return
  toggleNormalize()
}

/** 切换窗口：收起正在编辑的裁剪，避免它落在别的卡上。 */
/** @param {string} voiceId */
function openWindow(voiceId) {
  if (voiceId === state.activeVoiceId) return
  closeEditor()
  stopPreview()
  activateWindow(voiceId)
}

/** 页签上的 X：把这张卡从序列里移除（连同它的裁剪记录）。 */
/** @param {string} voiceId */
function onCloseWindow(voiceId) {
  if (lockedSelect.value) return
  if (voiceId === state.activeVoiceId) {
    closeEditor()
    stopPreview()
  }
  closeWindow(voiceId)
}

/** 行尾 X：从处理序列移除；电脑文件没有序列，等于清除目标。 */
/** @param {string} key */
function onRemoveRow(key) {
  if (lockedSelect.value) return
  if (isFile.value) {
    onClearTarget()
    return
  }
  if (editor.value === key) closeEditor()
  removeFromQueue(key)
}

/** 行尾「只处理这张卡」：序列收成这一行所在的音色卡，卡内样例不动。 */
/** @param {string} key */
function onOnlyRow(key) {
  if (lockedSelect.value) return
  if (editor.value && editor.value !== key) closeEditor()
  onlyThis(key)
}

/** @param {string} voiceId */
function voiceCardOf(voiceId) {
  return state.cards[voiceId] || store.allVoices.find((/** @type {any} */ item) => item.id === voiceId) || null
}

/** 页签右键：关闭或整卡试听都不必先切到这张卡。 */
/** @param {any} win @param {MouseEvent} event */
function onTabMenu(win, event) {
  if (!canTakeOver(event)) return
  const card = voiceCardOf(win.voiceId)
  openObjectMenu(
    'voiceclean.tab',
    {
      name: win.name,
      id: win.voiceId,
      hasAudio: Boolean(card?.main_audio_url),
      locked: lockedSelect.value,
      play: () => {
        if (!card?.main_audio_url) return
        stopPreview()
        actions.playVoiceCard(card)
      },
      close: () => onCloseWindow(win.voiceId),
    },
    { event }
  )
}

/** 点音频条主体展开 / 收起裁剪；行上的按钮、A/B、时长与裁剪面板各管各的。 */
/** @param {any} sample @param {MouseEvent} event */
function onAudioClick(sample, event) {
  const target = event.target
  if (!(target instanceof Element)) return
  if (target.closest('button, .vc-ab, .vc-status, .vc-audio-dur, .vc-trim')) return
  if (itemFor(sample.key)) return
  toggleEditor(sample.key)
}

/** 样例行右键：试听、A/B、裁剪、移出都落在这一条上。 */
/** @param {any} sample @param {MouseEvent} event */
function onSampleMenu(sample, event) {
  if (!canTakeOver(event)) return
  const item = itemFor(sample.key)
  const index = item?.index ?? 0
  openObjectMenu(
    'voiceclean.row',
    {
      playing: playingSample(sample),
      hasAudio: Boolean(sample.audio_url),
      canCompare: canCompare(sample),
      canOnly: state.windows.length > 1 && !lockedSelect.value,
      trimDisabled: Boolean(item) || lockedEdit.value,
      trimReason: item ? '结果已生成，先应用或重置' : '正在处理中',
      isTrim: editor.value === sample.key,
      locked: lockedSelect.value,
      removeLabel: isFile.value ? '清除这个文件' : '移出处理序列',
      fileName: sample.file,
      play: () => playSample(sample),
      playOrigin: () => play(index, 'origin'),
      playResult: () => play(index, 'result'),
      only: () => onOnlyRow(sample.key),
      trim: () => toggleEditor(sample.key),
      remove: () => onRemoveRow(sample.key),
    },
    { event }
  )
}

/** 播放条正在放的正是这条音色的样例：头像上显示暂停。 */
const voicePlaying = computed(() => {
  const current = store.player.queue[store.player.index]
  return Boolean(
    store.player.playing &&
      voice.value?.id &&
      current?.voice?.id === voice.value.id
  )
})

/** 行所属的音色对象：播放条上的「收藏 / 指定」需要它。 */
/** @param {any} row */
function voiceOf(row) {
  return store.allVoices.find((/** @type {any} */ item) => item.id === row.voice_id) || null
}

/** 目标卡头像：按惯例就是这条音色的播放键，排它自己的样例队列（从主样例开始）。 */
function playVoiceQueue() {
  const current = voice.value
  if (!current?.main_audio_url) return
  stopPreview()
  actions.playVoiceCard(current)
}

/** 页头播放键：按页签与行序试听整个序列（原声），放完自动接下一段。 */
function playQueue() {
  const rows = selectedRows.value.filter((/** @type {any} */ row) => row.audio_url)
  if (!rows.length) return
  stopPreview()
  actions.playAll(
    rows.map((/** @type {any} */ row, /** @type {number} */ index) => ({
      url: row.audio_url,
      label: sampleTitle(row),
      sub: `${row.voice_name || '样例'} · 第 ${index + 1} 条`,
      voice: state.fileTarget ? null : voiceOf(row),
    }))
  )
}

/** 波形框选只改选区；删除与保留选区由下面的按钮提交。 */
/** @param {any} sample @param {{ start: number, end: number }} range */
function onSelect(sample, range) {
  setSelection(sample.key, range)
}

/** @param {any} sample */
function onPreview(sample) {
  previewTrim(sample)
}

/** @param {any} sample */
function onPreviewSelection(sample) {
  previewSelection(sample)
}

/** 裁剪面板的快捷键：Delete 删除选区，Ctrl/Cmd+Z 撤销，Ctrl/Cmd+Shift+Z 前进。 */
/** @param {any} sample @param {KeyboardEvent} event */
function onTrimKey(sample, event) {
  const meta = event.ctrlKey || event.metaKey
  const key = event.key.toLowerCase()
  if (meta && key === 'z') {
    event.preventDefault()
    if (event.shiftKey) redoTrim(sample.key)
    else undoTrim(sample.key)
    return
  }
  if (meta && key === 'y') {
    event.preventDefault()
    redoTrim(sample.key)
    return
  }
  if ((event.key === 'Delete' || event.key === 'Backspace') && hasSelection(sample)) {
    event.preventDefault()
    deleteSelection(sample)
  }
}

/** @param {string} file */
function toggleEditor(file) {
  if (lockedEdit.value) return
  if (editor.value === file) {
    closeEditor()
    return
  }
  stopPreview()
  editor.value = file
}

function closeEditor() {
  editor.value = ''
  stopPreview()
}

function onStart() {
  void start()
}

function onCancel() {
  void cancel()
}

/** 替换不可撤销（原件只能从回收站还原），所以第一次点击只进确认态，
 *  第二次才真的覆盖；8 秒没有后续动作自动退回。 */
function onApply() {
  if (busy.value !== '') return
  if (!confirmApply.value) {
    armApplyConfirm()
    return
  }
  cancelApply()
  void applySelected()
}

function onSave() {
  if (busy.value !== '') return
  cancelApply()
  void saveSelected()
}

function armApplyConfirm() {
  confirmApply.value = true
  if (confirmTimer) window.clearTimeout(confirmTimer)
  confirmTimer = window.setTimeout(() => {
    confirmTimer = 0
    confirmApply.value = false
  }, 8000)
}

function cancelApply() {
  if (confirmTimer) {
    window.clearTimeout(confirmTimer)
    confirmTimer = 0
  }
  confirmApply.value = false
}

function onDownload() {
  downloadResult()
}

function onClearTarget() {
  cancelApply()
  void clearTarget()
}

/** 点「选择文件」只负责开系统文件窗；校验与上传在 chooseFile。 */
function onPickFile() {
  if (running.value || busy.value) return
  fileInput.value?.click()
}

/** @param {Event} event */
function onFileChange(event) {
  const input = /** @type {HTMLInputElement|null} */ (event.target)
  const file = input?.files?.[0]
  if (input) input.value = ''
  if (file) chooseFile(file)
}

const AUDIO_FILE = /\.(wav|flac|mp3|m4a|aac|ogg|opus)$/i

/** @param {File} file */
function chooseFile(file) {
  if (running.value || busy.value) return
  if (!AUDIO_FILE.test(file.name)) {
    say('支持 WAV / FLAC / MP3 / M4A / AAC / OGG / OPUS 音频。', { kind: 'warn' })
    return
  }
  void uploadSource(file)
}

/** @param {DragEvent} event */
function hasFiles(event) {
  return Boolean(event.dataTransfer?.types?.includes('Files'))
}

/** @param {DragEvent} event */
function onDragEnter(event) {
  if (running.value || busy.value || !hasFiles(event)) return
  dragDepth += 1
  dragging.value = true
}

function onDragLeave() {
  if (!dragging.value) return
  dragDepth = Math.max(0, dragDepth - 1)
  if (!dragDepth) dragging.value = false
}

/** @param {DragEvent} event */
function onDrop(event) {
  dragDepth = 0
  dragging.value = false
  if (running.value || busy.value) return
  const file = event.dataTransfer?.files?.[0]
  if (file) chooseFile(file)
}

function onResetRun() {
  cancelApply()
  closeEditor()
  void resetRun()
}

/** @param {any} sample */
function sampleTitle(sample) {
  const text = String(sample.text || '').trim()
  if (text) return text
  const base = String(sample.file || '').replace(/\.[^.]+$/, '')
  const parts = base.split('#')
  const content = parts.length > 2 ? parts.slice(2).join('#') : base
  return content.trim() || base || '样例'
}

/** @param {any} sample */
function emotionOf(sample) {
  const value = String(sample.emotion || '').trim()
  if (value) return value
  const base = String(sample.file || '').replace(/\.[^.]+$/, '')
  if (!base.includes('#')) return ''
  return base.split('#')[0].replace(/[（(].*$/, '').trim()
}

/** @param {any} sample */
function languageOf(sample) {
  const explicit = String(sample.language || '').trim()
  const fromFile = String(sample.file || '').match(/#([A-Za-z]{2,3}(?:-[A-Za-z]+)?)#/)?.[1] || ''
  const code = explicit || fromFile
  return code ? labelOf(vocab.languages, code) : ''
}

/** @param {any} sample */
function formatOf(sample) {
  const match = String(sample.file || '').match(/\.([^.]+)$/)
  return (match?.[1] || '音频').toUpperCase()
}

/** 行尾时长：只有裁过的行摆「原长 → 结果」的对比，两位小数才看得出裁剪量。
 *  没裁的行只给一个数：样例元数据是容器时长（MP3/AAC 的编码垫片比解码后的
 *  PCM 长 0.05 s 左右），拿它当「前」会让没裁的行也显得少了 0.05 s。 */
/** @param {any} sample */
function rowDuration(sample) {
  const full = Number(sample.duration) || 0
  const item = itemFor(sample.key)
  const result = Number(item?.result_seconds) || 0
  if (result) {
    if (!item?.trimmed) return `${result.toFixed(1)} s`
    if (full) return `${full.toFixed(2)} → ${result.toFixed(2)} s`
    return `${result.toFixed(1)} s`
  }
  const kept = keptSeconds(sample)
  if (isTrimmed(sample) && full && Math.abs(full - kept) > 0.005) {
    return `${full.toFixed(2)} → ${kept.toFixed(2)} s`
  }
  return kept ? `${kept.toFixed(1)} s` : '—'
}

/** @param {any} sample */
function trimStats(sample) {
  const duration = Number(sample.duration) || 0
  const kept = keptSeconds(sample)
  const count = segmentsOf(sample).length
  const pieces = count > 1 ? `（${count} 段）` : ''
  if (!duration) return `保留 ${kept.toFixed(2)} s${pieces}`
  const cut = Math.max(0, duration - kept)
  return `原长 ${duration.toFixed(2)} s · 保留 ${kept.toFixed(2)} s${pieces} · 去掉 ${cut.toFixed(2)} s`
}

/** 裁剪面板底部的提示：有选区时报选区，没有就说怎么选。 */
/** @param {any} sample */
function trimHint(sample) {
  const range = selectionOf(sample.key)
  if (!range) return '在波形上拖动框选要删除的区间；方向键 ±10 ms，Shift ±100 ms。'
  const length = Math.max(0, range.end - range.start)
  return `选区 ${range.start.toFixed(2)}–${range.end.toFixed(2)} s · ${length.toFixed(2)} s`
}

/** @param {any} sample */
function canCompare(sample) {
  const item = itemFor(sample.key)
  return item?.state === 'done' || item?.state === 'applied' || item?.state === 'saved'
}

/** @param {any} sample */
function playingSample(sample) {
  return Boolean(store.player.playing && store.player.url === sample.audio_url)
}

/** @param {any} sample @param {string} [mode] */
function isPreviewing(sample, mode = 'result') {
  return state.preview === sample.key && state.previewMode === mode && state.previewing
}

// 只认左栏动作的计数：页签切换会改 pick 的音色，但那是打开已有窗口，不重新加载。
watch(
  () => store.pick.rev,
  () => {
    editor.value = ''
    stopPreview()
    void loadTarget()
  }
)

watch(job, (value) => {
  cancelApply()
  if (value) {
    editor.value = ''
    stopPreview()
  }
})

// 壳上的播放条一响，裁剪选区的试听就停：两路声音同时出来听不出差别。
watch(
  () => store.player.playing,
  (value) => {
    if (value) stopPreview()
  }
)

onMounted(() => {
  void refresh()
  void restore()
})

onBeforeUnmount(() => {
  cancelApply()
  resetWorkspace()
})
</script>

<style scoped>
.vc {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0;
  min-height: 0;
}
.vc-head-acts {
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
}
.vc-head-file {
  height: 28px;
  padding: 0 10px;
  font-size: 11.5px;
}
.vc-head-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
/* 页头、正文、底栏共用同一条内容列：各自收到 --sh-page-max 并居中，
   否则宽屏上只有正文收窄，标题与底栏留在面板两端。 */
.panel-head {
  width: 100%;
  max-width: var(--sh-page-max);
  margin-inline: auto;
}
.vc-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  width: 100%;
  max-width: var(--sh-page-max);
  margin-inline: auto;
  padding: 6px var(--sh-gutter) 26px;
  display: flex;
  flex-direction: column;
  gap: var(--sh-gap-3);
}
.vc-error {
  color: var(--sh-danger);
}
.vc-head-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--sh-text-3);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.vc-head-state.is-live {
  color: var(--sh-accent);
}
.vc-head-state.is-ok {
  color: var(--sh-ok);
}
.vc-spin {
  animation: vc-spin 1.1s linear infinite;
}
@keyframes vc-spin {
  to { transform: rotate(360deg); }
}

/* ---------------- 空态 ---------------- */
.vc-empty {
  min-height: 320px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 24px;
  border: 1px dashed var(--sh-line);
  border-radius: var(--sh-radius-card);
  background: var(--sh-panel);
  text-align: center;
  transition: border-color var(--sh-t-fast) var(--sh-ease),
    background var(--sh-t-fast) var(--sh-ease);
}
.vc-empty.is-drag {
  border-color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.vc-empty-icon {
  width: 48px;
  height: 48px;
  display: grid;
  place-items: center;
  border-radius: 15px;
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
  box-shadow: var(--sh-inset-hi);
  margin-bottom: 2px;
}
.vc-empty-title {
  font-size: 14px;
  font-weight: 650;
  color: var(--sh-text);
}
.vc-empty-hint {
  max-width: 440px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--sh-text-4);
}
.vc-empty-pick {
  margin-top: 6px;
  height: 32px;
  padding: 0 14px;
}
.vc-empty-formats {
  font-size: 9.5px;
  letter-spacing: 0.03em;
  color: var(--sh-text-4);
}

/* ---------------- 处理链 ---------------- */
.vc-chain {
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-radius-inner);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-1), var(--sh-inset-hi);
  padding: 11px 13px 13px;
}
.vc-chain-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 9px;
}
.vc-chain-title {
  font-size: 12px;
  font-weight: 650;
  color: var(--sh-text-2);
}
.vc-chain-sum {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10.5px;
  color: var(--sh-text-4);
}
.vc-chain-acts {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.vc-chain-run {
  flex: none;
  height: 30px;
  padding: 0 12px;
  font-size: 11.5px;
}
.vc-chain-live {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--sh-accent);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.vc-step-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 9px;
}
.vc-step {
  position: relative;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 9px 11px 10px 14px;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-md);
  background: var(--sh-glass-soft);
  cursor: pointer;
  transition: background var(--sh-t-fast) var(--sh-ease),
    border-color var(--sh-t-fast) var(--sh-ease),
    box-shadow var(--sh-t-fast) var(--sh-ease);
}
.vc-step::before {
  content: '';
  position: absolute;
  left: 0;
  top: 9px;
  bottom: 9px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: transparent;
}
.vc-step:hover {
  background: var(--sh-hover);
}
.vc-step.on {
  border-color: var(--sh-accent-ring);
  background: var(--sh-accent-soft);
  box-shadow: var(--sh-inset-hi);
}
.vc-step.on::before {
  background: var(--sh-accent);
}
.vc-step.missing {
  cursor: default;
}
.vc-step.missing:hover {
  background: var(--sh-glass-soft);
}
.vc-step.locked {
  cursor: default;
}
.vc-step.locked:hover {
  background: var(--sh-glass-soft);
}
.vc-step.on.locked:hover {
  background: var(--sh-accent-soft);
}
.vc-step-top {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.vc-step-no {
  flex: none;
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border-radius: 6px;
  font-size: 10.5px;
  font-weight: 700;
  color: var(--sh-text-3);
  background: var(--sh-chip);
}
.vc-step.on .vc-step-no {
  color: var(--sh-accent);
  background: var(--sh-accent-soft-2);
}
.vc-step-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
  font-weight: 650;
  color: var(--sh-text);
}
.vc-step-state {
  margin-left: auto;
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 6px;
  border: 1px solid var(--sh-line-soft);
  border-radius: 999px;
  font-size: 9.5px;
  line-height: 1.7;
  color: var(--sh-text-4);
  white-space: nowrap;
}
.vc-step-state.on {
  color: var(--sh-accent);
  background: var(--sh-accent-soft-2);
  border-color: var(--sh-accent-ring);
}
.vc-step-model {
  width: 100%;
  min-width: 0;
  height: 28px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-xs);
  background: var(--sh-panel);
  color: var(--sh-text);
  font-size: 11.5px;
  cursor: pointer;
}
.vc-step-model:hover {
  border-color: var(--sh-accent-ring);
  color: var(--sh-accent);
}
.vc-step-model.is-empty {
  color: var(--sh-text-4);
}
.vc-step-model-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}
.vc-step-note {
  min-height: 2.2em;
  font-size: 10.5px;
  line-height: 1.5;
  color: var(--sh-text-4);
}
.vc-step-note.is-warn {
  color: var(--sh-warn);
}
.vc-step.is-core {
  border-style: dashed;
}
.vc-step.is-core.on {
  border-style: solid;
}

/* 模型表单：弹层里列出这一步的全部候选，下载按钮在行尾。 */
.vc-model-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 12px 10px;
}
.vc-model-form-head {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 0 2px 2px;
}
.vc-model-form-title {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--sh-text);
}
.vc-model-form-hint {
  font-size: 10.5px;
  line-height: 1.45;
  color: var(--sh-text-4);
}
.vc-model-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: min(56vh, 430px);
  overflow-y: auto;
}
.vc-model-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  padding: 7px 8px;
  border-radius: var(--sh-r-xs);
}
.vc-model-row:hover {
  background: var(--sh-hover);
}
.vc-model-row.on {
  background: var(--sh-accent-soft);
}
.vc-model-pick {
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.vc-model-pick:disabled {
  cursor: default;
}
.vc-model-name {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 650;
  color: var(--sh-text);
}
.vc-model-tag {
  padding: 0 5px;
  border-radius: 999px;
  font-size: 9px;
  line-height: 1.6;
  font-weight: 500;
  color: var(--sh-accent);
  background: var(--sh-accent-soft-2);
}
.vc-model-note {
  font-size: 10.5px;
  line-height: 1.45;
  color: var(--sh-text-4);
}
.vc-model-side {
  min-width: 50px;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 3px;
}
.vc-model-job {
  font-size: 10px;
  color: var(--sh-accent);
  white-space: nowrap;
}
.vc-model-job.is-error {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--sh-warn);
}
.vc-model-download {
  height: 24px;
  padding: 0 8px;
  font-size: 10.5px;
}
.vc-model-current {
  font-size: 9.5px;
  color: var(--sh-accent);
}
.vc-model-pickable {
  font-size: 10px;
  color: var(--sh-accent);
}

/* ---------------- 目标与样例 ---------------- */
.vc-target {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 13px 14px 14px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-radius-card);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-1), var(--sh-inset-hi);
}
/* 页签：每张音色卡一个窗口；只在一张以上时出现。 */
.vc-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  overflow-x: auto;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--sh-line-soft);
}
.vc-tab {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 4px 0 10px;
  border: 1px solid var(--sh-line-soft);
  border-radius: var(--sh-r-xs);
  background: var(--sh-chip);
  color: var(--sh-text-3);
  cursor: pointer;
}
.vc-tab:hover {
  border-color: var(--sh-line);
  background: var(--sh-hover);
}
.vc-tab.on {
  border-color: var(--sh-accent-ring);
  background: var(--sh-accent-soft);
  color: var(--sh-text);
}
.vc-tab-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11.5px;
  font-weight: 600;
}
.vc-tab-count {
  flex: none;
  font-size: 10px;
  color: var(--sh-text-4);
  font-variant-numeric: tabular-nums;
}
.vc-tab.on .vc-tab-count {
  color: var(--sh-accent);
}
.vc-tab-x {
  flex: none;
  width: 18px;
  height: 18px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--sh-text-4);
  cursor: pointer;
}
.vc-tab-x:hover {
  color: var(--sh-danger);
  background: var(--sh-danger-soft);
}
.vc-tab-x:disabled {
  opacity: 0.4;
  cursor: default;
}
.vc-target-head {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}
.vc-avatar {
  flex: none;
  width: 46px;
  height: 46px;
  display: grid;
  place-items: center;
  border-radius: 14px;
  color: #fff;
  font-size: 19px;
  font-weight: 700;
  box-shadow: var(--sh-inset-hi), 0 8px 18px -14px var(--sh-accent);
}
/* 头像即播放键：与主页 / 左栏同一个手势——悬停盖一层遮罩，中央一颗播放键。 */
.vc-avatar-play {
  position: relative;
  overflow: hidden;
  padding: 0;
  border: 0;
  cursor: pointer;
}
.vc-avatar-play-hint {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  border-radius: inherit;
  color: #fff;
  background: var(--sh-scrim);
  opacity: 0;
  transition: opacity var(--sh-t-fast) var(--sh-ease);
}
.vc-avatar-play:hover .vc-avatar-play-hint,
.vc-avatar-play:focus-visible .vc-avatar-play-hint,
.vc-avatar-play.is-playing .vc-avatar-play-hint {
  opacity: 1;
}
.vc-avatar-play.is-playing .vc-avatar-play-hint {
  background: var(--sh-scrim-on);
}
.vc-avatar-play:disabled {
  cursor: default;
  opacity: 0.6;
}
.vc-avatar-play:disabled .vc-avatar-play-hint {
  display: none;
}
.vc-target-ident {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.vc-target-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
}
.vc-target-name {
  font-size: 16px;
  font-weight: 700;
  color: var(--sh-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vc-target-tag {
  flex: none;
  padding: 0 6px;
  border-radius: var(--sh-r-2xs);
  font-size: 9.5px;
  line-height: 1.7;
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.vc-target-tag.is-warn {
  color: var(--sh-warn);
  background: var(--sh-warn-soft);
}
.vc-target-sub {
  font-size: 11px;
  color: var(--sh-text-4);
}
.vc-target-acts {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
.vc-mini-btn {
  height: 26px;
  padding: 0 9px;
  font-size: 11px;
}
.vc-target-close:hover {
  color: var(--sh-danger);
  background: var(--sh-danger-soft);
  border-color: var(--sh-line);
}
.vc-audios {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}
.vc-audio {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 86px auto;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 7px 9px;
  border: 1px solid transparent;
  border-radius: var(--sh-r-md);
  transition: background var(--sh-t-fast) var(--sh-ease),
    border-color var(--sh-t-fast) var(--sh-ease);
}
.vc-audio:hover {
  background: var(--sh-hover);
}
/* 展开裁剪：这一行自己变成裁剪面板，标题行、波形与工具同属一张卡。 */
.vc-audio.is-trim,
.vc-audio.is-trim:hover {
  border-color: var(--sh-accent-ring);
  background: linear-gradient(180deg, var(--sh-accent-soft), var(--sh-panel) 58%);
  box-shadow: var(--sh-inset-hi);
}
.vc-audio-main {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
  cursor: pointer;
}
/* 结果已生成时裁剪锁住：主体不再摆出可点的样子。 */
.vc-audio:has(.vc-status) .vc-audio-main {
  cursor: default;
}
.vc-audio-play {
  flex: none;
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 1px solid var(--sh-line-soft);
  border-radius: 9px;
  color: var(--sh-text-3);
  background: var(--sh-chip);
  cursor: pointer;
}
.vc-audio-play:hover,
.vc-audio-play.on {
  color: var(--sh-accent);
  background: var(--sh-accent-soft-2);
  border-color: var(--sh-accent-ring);
}
.vc-audio-play:disabled {
  opacity: 0.4;
  cursor: default;
}
.vc-audio-text {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.vc-audio-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--sh-text);
}
.vc-audio-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  font-size: 10.5px;
  color: var(--sh-text-4);
}
.vc-mini-tag {
  padding: 0 5px;
  border-radius: 4px;
  line-height: 1.7;
  color: var(--sh-text-3);
  background: var(--sh-chip);
  white-space: nowrap;
}
.vc-mini-tag.is-cut {
  color: var(--sh-warn);
  background: var(--sh-warn-soft);
}
.vc-audio-file {
  white-space: nowrap;
}
.vc-audio-dur {
  text-align: right;
  white-space: nowrap;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--sh-text-3);
}
.vc-audio-acts {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 5px;
  min-width: 0;
}
.vc-icon-btn {
  flex: none;
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 1px solid var(--sh-line);
  border-radius: 7px;
  background: transparent;
  color: var(--sh-text-3);
  cursor: pointer;
}
.vc-icon-btn:hover {
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
  border-color: var(--sh-accent-ring);
}
.vc-icon-btn.on {
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
  border-color: var(--sh-accent-ring);
}
.vc-icon-btn:disabled {
  opacity: 0.4;
  cursor: default;
}
.vc-audio-remove:hover {
  color: var(--sh-danger);
  background: var(--sh-danger-soft);
  border-color: var(--sh-line);
}
.vc-status {
  flex: none;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 9.5px;
  line-height: 1.7;
  color: var(--sh-text-4);
  background: var(--sh-chip);
  white-space: nowrap;
}
.vc-status.is-ready {
  color: var(--sh-text-3);
}
.vc-status.is-running {
  color: var(--sh-accent);
  background: var(--sh-accent-soft-2);
}
.vc-status.is-queued {
  color: var(--sh-text-3);
}
.vc-status.is-done {
  color: var(--sh-ok);
  background: var(--sh-ok-soft);
}
.vc-status.is-applied {
  color: var(--sh-ok);
  background: var(--sh-ok-soft);
}
.vc-status.is-saved {
  color: var(--sh-ok);
  background: var(--sh-ok-soft);
}
.vc-status.is-failed {
  color: var(--sh-danger);
  background: var(--sh-danger-soft);
}
.vc-status.is-stale {
  color: var(--sh-warn);
  background: var(--sh-warn-soft);
}
.vc-status.is-idle {
  color: var(--sh-text-4);
}
.vc-ab {
  flex: none;
  display: inline-flex;
  height: 26px;
  border: 1px solid var(--sh-line);
  border-radius: 7px;
  overflow: hidden;
}
.vc-ab button {
  border: 0;
  padding: 0 7px;
  background: transparent;
  color: var(--sh-text-3);
  font: inherit;
  font-size: 10.5px;
  cursor: pointer;
}
.vc-ab button + button {
  border-left: 1px solid var(--sh-line-soft);
}
.vc-ab button:hover {
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.vc-ab button.on {
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
}
.vc-ab button:disabled {
  opacity: 0.4;
  cursor: default;
}
.vc-samples-empty {
  padding: 6px 2px;
}
.vc-warn-line {
  padding: 6px 9px;
  border-radius: var(--sh-r-xs);
  font-size: 11px;
  line-height: 1.5;
  color: var(--sh-warn);
  background: var(--sh-warn-soft);
}

/* ---------------- 裁剪面板（展开在样例行内） ---------------- */
.vc-trim {
  grid-column: 1 / -1;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  padding: 3px 2px;
  cursor: default;
}
.vc-trim-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
}
.vc-trim-bar .sh-btn {
  height: 28px;
  padding: 0 10px;
  font-size: 11px;
}
.vc-trim-stats {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10.5px;
  color: var(--sh-text-3);
  font-variant-numeric: tabular-nums;
}
.vc-trim-undo {
  margin-left: auto;
  display: inline-flex;
  gap: 4px;
}
.vc-trim-hint {
  font-size: 10px;
  line-height: 1.5;
  color: var(--sh-text-4);
}
.vc-trim-spacer {
  flex: 1;
}

/* ---------------- 底栏 ---------------- */
/* 底栏与正文同一条内容列：外面只留同一档 gutter，里面是一张与页面卡片同语言的浮卡。 */
.vc-dock {
  flex: none;
  box-sizing: border-box;
  width: 100%;
  max-width: var(--sh-page-max);
  margin-inline: auto;
  padding: 0 var(--sh-gutter) 12px;
}
.vc-dock-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 9px 12px 10px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-radius-inner);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-1), var(--sh-inset-hi);
}
.vc-dock-in {
  display: flex;
  align-items: center;
  gap: 16px;
  min-width: 0;
}
.vc-dock-sum {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.vc-dock-title {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--sh-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.vc-dock-sub {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10.5px;
  color: var(--sh-text-4);
}
.vc-dock-acts {
  flex: none;
  display: flex;
  align-items: center;
  gap: 7px;
}
.vc-dock-acts .sh-btn {
  height: 34px;
  padding: 0 15px;
}
.vc-dock-hint {
  font-size: 11px;
  color: var(--sh-text-4);
  white-space: nowrap;
}
.vc-dock-note {
  font-size: 10.5px;
  color: var(--sh-ok);
}
.vc-dock-note.is-warn {
  color: var(--sh-warn);
}
.vc-progress {
  height: 4px;
  margin-top: 4px;
  border-radius: 2px;
  background: var(--sh-line-soft);
  overflow: hidden;
}
.vc-progress i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--sh-accent), var(--sh-accent-2));
  transition: width 0.2s linear;
}

/* ---------------- 拖入文件 ---------------- */
.vc-file-input {
  display: none;
}
.vc-drop {
  position: absolute;
  inset: 8px;
  z-index: 20;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 2px dashed var(--sh-accent);
  border-radius: var(--sh-radius-card);
  background: var(--sh-panel);
  pointer-events: none;
  text-align: center;
}
.vc-drop-icon {
  width: 52px;
  height: 52px;
  display: grid;
  place-items: center;
  border-radius: 16px;
  color: var(--sh-accent);
  background: var(--sh-accent-soft);
  box-shadow: var(--sh-inset-hi);
}
.vc-drop-title {
  font-size: 14px;
  font-weight: 650;
  color: var(--sh-text);
}
.vc-drop-hint {
  font-size: 10px;
  letter-spacing: 0.03em;
  color: var(--sh-text-4);
}

/* 四张步骤卡在 1440 以上排一行（每卡约 195px）；再窄就分两行，不然下拉里的
   模型名会被截掉。 */
@media (max-width: 1439px) {
  .vc-step-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 1180px) {
  .vc-audio {
    grid-template-columns: minmax(0, 1fr) 74px auto;
  }
}
@media (max-width: 1000px) {
  .vc-body {
    padding-block: 6px 20px;
  }
  .vc-step-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .vc-target-head {
    flex-wrap: wrap;
  }
}
</style>
