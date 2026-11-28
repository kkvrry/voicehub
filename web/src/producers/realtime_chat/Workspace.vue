<template>
  <section
    ref="rootEl"
    class="app-panel rc"
    :class="{
      'is-fullscreen': isFullscreen,
      'is-speaking': Boolean(state.speakingId),
      'is-listening': state.listening === 'listening',
    }"
  >
    <!-- 房间：一张场景铺满工作区，立绘、气泡与悬浮输入栏落在画面上，不遮挡人物与背景。 -->
    <div
      ref="sceneEl"
      class="rc-scene has-image"
      :class="[`is-bg-${backgroundKey || 'custom'}`, { 'has-history': historyOpen }]"
      :style="[stageStyle, stageVars]"
    >
      <div class="rc-shade" aria-hidden="true" />
      <!-- 立绘：透明底，可拖动换位置、滚轮改大小，位置与大小跟着角色保存。 -->
      <div
        v-if="portraitImage"
        class="rc-figure-ground"
        :style="figureGroundStyle"
        aria-hidden="true"
      />
      <img
        v-if="portraitImage"
        ref="figureEl"
        class="rc-figure"
        :class="{ 'is-dragging': Boolean(portraitDrag), 'is-grounded': (char?.portrait_y ?? 0) <= 0.03 }"
        :src="portraitImage"
        :style="figureStyle"
        alt=""
        aria-hidden="true"
        title="拖动换位置，滚轮调大小"
        @load="onPortraitLoad"
        @pointerdown="startPortraitDrag"
        @wheel.prevent="onPortraitWheel"
      />

      <!-- 气泡跟随立绘位置与大小实时移动，自适应左右翻边与上下收边界，保证完整显示。 -->
      <Transition name="rc-bubble">
        <div
          v-if="bubbleVisible"
          ref="bubbleEl"
          class="rc-bubble"
          :class="[
            `is-tail-${bubbleLayout.tail}`,
            {
              'is-thinking': bubbleThinking,
              'is-speaking': Boolean(state.speakingId),
            },
          ]"
          :style="bubbleLayout.style"
          role="status"
        >
          <div v-if="bubbleThinking" class="rc-bubble-dots" aria-label="正在想">
            <i /><i /><i />
          </div>
          <template v-else>
            <div class="rc-bubble-scroll">
              <p class="rc-bubble-text">{{ bubbleText }}</p>
            </div>
            <button
              v-if="canDismissBubble"
              type="button"
              class="rc-bubble-close"
              title="收起气泡"
              aria-label="收起气泡"
              @click="dismissBubble"
            >
              <X :size="12" :stroke-width="1.8" />
            </button>
          </template>
        </div>
      </Transition>

      <header
        v-if="char"
        ref="plateEl"
        class="rc-plate"
        :class="{ 'is-open': rosterOpen }"
        title="滚轮切换角色"
        @wheel.prevent="onPlateWheel"
      >
        <div class="rc-name-row">
          <span ref="switchEl" class="rc-switch-wrap">
            <button
              type="button"
              class="rc-switch"
              :title="`切换角色（共 ${characters.length} 个，滚轮也能翻）`"
              aria-label="切换角色"
              aria-haspopup="menu"
              :aria-expanded="rosterOpen"
              @click="rosterOpen = !rosterOpen"
            >
              <ChevronsUpDown :size="14" :stroke-width="1.8" />
            </button>
            <Transition name="rc-menu">
              <div v-if="rosterOpen" class="rc-roster" role="menu">
                <button
                  v-for="item in characters"
                  :key="item.id"
                  type="button"
                  class="rc-roster-row"
                  :class="{ 'is-active': item.id === char.id }"
                  role="menuitemradio"
                  :aria-checked="item.id === char.id"
                  @click="switchChar(item.id)"
                >
                  <span class="rc-roster-name">{{ item.name }}</span>
                  <Check v-if="item.id === char.id" :size="14" :stroke-width="1.8" />
                </button>
                <button type="button" class="rc-roster-row is-add" role="menuitem" @click="onAddCharacter">
                  <Plus :size="14" :stroke-width="1.8" />
                  新建角色
                </button>
              </div>
            </Transition>
          </span>
          <h1 class="rc-name">{{ char.name }}</h1>
          <button
            type="button"
            class="rc-edit"
            title="编辑角色"
            aria-label="编辑角色"
            aria-haspopup="dialog"
            :aria-expanded="sheetOpen"
            @click="openSheet"
          >
            <PenLine :size="14" :stroke-width="1.8" />
          </button>
        </div>
        <p class="rc-intro">{{ introLine(char) }}</p>
      </header>

      <!-- 右上角轻量入口：只留历史记录开关与全屏，不遮挡人物与背景。 -->
      <div v-if="char && !historyOpen" class="rc-corner-acts">
        <button
          type="button"
          class="rc-corner-btn"
          :title="`查看历史对话（共 ${sessions.length} 个）`"
          aria-label="查看历史对话"
          :aria-expanded="historyOpen"
          @click="openHistory"
        >
          <History :size="16" :stroke-width="1.8" aria-hidden="true" />
          <span class="rc-corner-label">历史</span>
          <span v-if="sessions.length" class="rc-corner-n">{{ sessions.length }}</span>
        </button>
        <button
          type="button"
          class="rc-corner-btn is-icon"
          :title="isFullscreen ? '退出全屏' : '全屏'"
          :aria-label="isFullscreen ? '退出全屏' : '全屏'"
          @click="toggleFullscreen"
        >
          <Minimize2 v-if="isFullscreen" :size="16" :stroke-width="1.8" />
          <Maximize2 v-else :size="16" :stroke-width="1.8" />
        </button>
      </div>

      <!-- 底部居中悬浮输入栏：语音图标、浅浅的输入框与模型选择收成一条轻薄雾面胶囊，不挡人物与场景。 -->
      <div v-if="char" class="rc-dock">
        <div v-if="state.chatError || state.liveText || busyHint" class="rc-dock-meta">
          <div v-if="state.chatError" class="rc-dock-pill is-error" role="alert">
            <AlertCircle :size="14" :stroke-width="1.8" aria-hidden="true" />
            <span>{{ state.chatError }}</span>
          </div>
          <div v-if="state.liveText" class="rc-dock-pill is-live">
            <span class="rc-dock-live-dot" aria-hidden="true" />
            <span>{{ state.liveText }}</span>
          </div>
          <div v-else-if="busyHint" class="rc-dock-pill">
            <span>{{ busyHint }}</span>
          </div>
        </div>

        <div
          class="rc-dock-bar"
          :class="{
            'is-active': Boolean(state.draft.trim()) || Boolean(state.listening) || modelOpen,
            'is-listening': state.listening === 'listening',
          }"
        >
          <button
            type="button"
            class="rc-mic"
            :class="{
              'is-on': state.listening === 'listening',
              'is-hearing': state.micSpeaking,
            }"
            :disabled="!hostStore.asr?.ready || state.listening === 'connecting'"
            :title="listenTitle"
            :aria-label="state.listening === 'listening' ? '停止语音对话' : '开始语音对话'"
            @click="listenToggle"
          >
            <Loader2
              v-if="state.listening === 'connecting'"
              class="rc-spin"
              :size="16"
              :stroke-width="1.8"
            />
            <Mic v-else :size="16" :stroke-width="1.8" />
          </button>

          <textarea
            ref="inputEl"
            v-model="state.draft"
            class="rc-input"
            rows="1"
            maxlength="1000"
            placeholder="说点什么，或点麦克风语音对话…"
            aria-label="输入对话内容"
            @keydown.enter.exact="onEnter"
            @input="autosize"
          />

          <!-- 模型选择：紧凑胶囊，向上展开，不占用画面视线。 -->
          <span ref="modelWrapEl" class="rc-model-wrap">
            <button
              type="button"
              class="rc-model"
              :class="{ 'is-open': modelOpen, 'is-warn': Boolean(providerNote()) }"
              :title="modelFullHint()"
              aria-label="选择对话模型"
              aria-haspopup="dialog"
              :aria-expanded="modelOpen"
              @click="toggleModel"
            >
              <Cpu :size="14" :stroke-width="1.8" aria-hidden="true" />
              <span class="rc-model-name">{{ modelHint() }}</span>
            </button>
            <Transition name="rc-menu">
              <div v-if="modelOpen" class="rc-model-pop" role="dialog" aria-label="对话模型">
                <label class="rc-field is-flat">
                  <span>服务</span>
                  <select
                    :value="state.data?.llm_provider || ''"
                    class="sh-field"
                    @change="onProviderChange"
                  >
                    <option value="">默认 · {{ defaultProviderLabel }}</option>
                    <option v-for="item in state.providers" :key="item.key" :value="item.key">
                      {{ item.label }}{{ item.ready ? '' : '（未就绪）' }}
                    </option>
                  </select>
                </label>
                <label class="rc-field is-flat">
                  <span>模型</span>
                  <select
                    v-if="state.models.length"
                    :value="state.data?.llm_model || ''"
                    class="sh-field"
                    @change="onModelChange"
                  >
                    <option value="">服务默认模型</option>
                    <option v-for="item in state.models" :key="item.id" :value="item.id">
                      {{ item.label || item.id }}
                    </option>
                  </select>
                  <input
                    v-else
                    class="sh-field"
                    :value="state.data?.llm_model || ''"
                    placeholder="留空使用服务配置的模型"
                    autocomplete="off"
                    @input="onModelInput"
                  />
                </label>
                <p v-if="state.modelsBusy" class="rc-note">读取模型清单…</p>
                <p v-else-if="state.modelsReason" class="rc-note">{{ state.modelsReason }}</p>
                <p v-if="providerNote()" class="rc-note is-warn">{{ providerNote() }}</p>
              </div>
            </Transition>
          </span>

          <button
            type="button"
            class="rc-send"
            :disabled="!canSend"
            :title="canSend ? '发送（Enter）' : '先写一句'"
            aria-label="发送"
            @click="sendDraft"
          >
            <Loader2 v-if="state.busy === 'thinking'" class="rc-spin" :size="16" :stroke-width="1.8" />
            <ArrowUp v-else :size="16" :stroke-width="1.8" />
          </button>
        </div>
      </div>

      <!-- 右侧精简历史抽屉：呼出直接显示对话列表，点选某段对话直接查看消息详情。 -->
      <Transition name="rc-history">
        <aside v-if="char && historyOpen" ref="historyEl" class="rc-history" aria-label="历史对话">
          <header class="rc-history-head">
            <button
              v-if="historyView === 'messages'"
              type="button"
              class="rc-tool is-icon"
              title="返回对话列表"
              aria-label="返回对话列表"
              @click="historyView = 'list'"
            >
              <ArrowLeft :size="14" :stroke-width="1.8" />
            </button>
            <div class="rc-history-title">
              <h2>{{ historyView === 'list' ? '历史对话' : sessionTitle() }}</h2>
              <span class="rc-history-meta">
                {{ historyView === 'list' ? `${sessions.length} 个` : `${sessionCount()} 条` }}
              </span>
            </div>
            <div class="rc-history-acts">
              <button
                type="button"
                class="rc-tool is-icon"
                title="新建对话"
                aria-label="新建对话"
                @click="onNewSession"
              >
                <Plus :size="14" :stroke-width="1.8" />
              </button>
              <button
                type="button"
                class="rc-tool is-icon"
                :title="isFullscreen ? '退出全屏' : '全屏'"
                :aria-label="isFullscreen ? '退出全屏' : '全屏'"
                @click="toggleFullscreen"
              >
                <Minimize2 v-if="isFullscreen" :size="14" :stroke-width="1.8" />
                <Maximize2 v-else :size="14" :stroke-width="1.8" />
              </button>
              <button
                type="button"
                class="rc-tool is-icon"
                title="收起历史对话"
                aria-label="收起历史对话"
                @click="closeHistory"
              >
                <X :size="14" :stroke-width="1.8" />
              </button>
            </div>
          </header>

          <!-- 直接展示对话列表：不再嵌套在二级按钮里。 -->
          <div v-if="historyView === 'list'" class="rc-toc-body">
            <div
              v-for="row in orderedSessions"
              :key="row.id"
              class="rc-toc-row"
              :class="{ 'is-active': row.id === current?.id }"
            >
              <button type="button" class="rc-toc-main" @click="openSession(row.id)">
                <span class="rc-toc-title">{{ sessionTitle(row) }}</span>
                <span class="rc-toc-sub">
                  {{ sessionDate(row.created) }} · {{ sessionCount(row) }} 条
                  <template v-if="row.id === current?.id"> · 当前</template>
                </span>
              </button>
              <div v-if="confirmId === row.id" class="rc-toc-confirm">
                <button
                  type="button"
                  class="rc-danger"
                  :disabled="Boolean(state.busy)"
                  @click="dropSession(row.id)"
                >
                  删除
                </button>
                <button type="button" @click="confirmId = ''">取消</button>
              </div>
              <button
                v-else
                type="button"
                class="rc-toc-del"
                :disabled="Boolean(state.busy)"
                title="删除对话"
                aria-label="删除对话"
                @click="confirmId = row.id"
              >
                <Trash2 :size="14" :stroke-width="1.8" />
              </button>
            </div>
          </div>

          <!-- 选中某段对话后的消息流。 -->
          <div v-else ref="chatEl" class="rc-script">
            <template v-if="messages.length">
              <div class="rc-stamp">{{ sessionDate(current?.created) }}</div>
              <div
                v-for="(message, index) in messages"
                :key="message.id"
                class="rc-line"
                :class="[
                  message.role,
                  {
                    'is-error': message.error,
                    'is-playing': message.id === state.speakingId,
                  },
                ]"
              >
                <span v-if="speakerOf(index)" class="rc-who">{{ speakerOf(index) }}</span>
                <p class="rc-said">{{ message.text }}</p>
                <div v-if="hasAudioActs(message)" class="rc-line-acts">
                  <button
                    v-if="message.audio_url"
                    type="button"
                    class="rc-mini"
                    :class="{ 'is-on': message.id === state.speakingId }"
                    title="播放这一句"
                    aria-label="播放这一句"
                    @click="playMessage(message)"
                  >
                    <Play :size="12" :stroke-width="1.8" fill="currentColor" />
                  </button>
                  <button
                    v-else
                    type="button"
                    class="rc-mini"
                    :disabled="Boolean(state.synthesizingId)"
                    title="合成这一句"
                    aria-label="合成这一句"
                    @click="retryAudio(message)"
                  >
                    <Loader2
                      v-if="message.id === state.synthesizingId"
                      class="rc-spin"
                      :size="12"
                      :stroke-width="1.8"
                    />
                    <Volume2 v-else :size="12" :stroke-width="1.8" />
                  </button>
                  <span v-if="state.synthesizingId === message.id" class="rc-tag">合成中</span>
                  <span v-else-if="message.audio_error" class="rc-tag is-warn" :title="message.audio_error">
                    无语音
                  </span>
                </div>
                <span
                  v-else-if="message.audio_error"
                  class="rc-tag is-warn"
                  :title="message.audio_error"
                >
                  无语音
                </span>
              </div>

              <div v-if="state.busy === 'thinking'" class="rc-typing" role="status">
                <i /><i /><i />
              </div>
            </template>

            <div v-else class="rc-blank">
              <p class="rc-blank-line">暂无聊天记录</p>
            </div>

            <div v-if="state.liveText" class="rc-line user is-live">
              <span class="rc-who">你</span>
              <p class="rc-said">{{ state.liveText }}</p>
            </div>
          </div>
        </aside>
      </Transition>

      <div v-if="!char" class="rc-blank is-scene">
        <p class="rc-blank-line">{{ state.error ? '对话状态没读出来。' : '载入中…' }}</p>
        <p v-if="state.error" class="rc-blank-sub">{{ state.error }}</p>
        <button v-if="state.error" type="button" class="sh-btn is-ghost is-sm" @click="refresh">
          再试一次
        </button>
      </div>

      <!-- 角色设置：从名牌那儿就地铺开，盖住名牌与画面左半，不换页。 -->
      <button
        v-if="sheetOpen"
        type="button"
        class="rc-scrim"
        tabindex="-1"
        aria-label="收起角色设置"
        @click="sheetOpen = false"
      />
      <Transition name="rc-sheet">
        <aside v-if="sheetOpen && char" class="rc-sheet" role="dialog" aria-modal="true" aria-label="角色设置">
          <header class="rc-sheet-head">
            <h2>{{ state.sheetNew ? '新建角色' : '角色设置' }}</h2>
            <span class="rc-save" :class="{ 'is-busy': state.saving }">
              {{ state.saving ? '保存中…' : '已保存' }}
            </span>
            <button
              type="button"
              class="rc-sheet-close"
              title="收起"
              aria-label="收起角色设置"
              @click="sheetOpen = false"
            >
              <X :size="15" :stroke-width="1.8" />
            </button>
          </header>

          <div class="rc-sheet-body">
            <label class="rc-field">
              <span>名字</span>
              <input
                v-model="char.name"
                class="sh-field"
                maxlength="24"
                autocomplete="off"
                @input="scheduleSave"
              />
            </label>

            <label class="rc-field">
              <span>介绍</span>
              <textarea
                v-model="char.intro"
                class="sh-field rc-intro-in"
                rows="2"
                maxlength="160"
                placeholder="一句话：她是谁、此刻在哪儿。用音色卡创建时自动填入音色描述。"
                @input="scheduleSave"
              />
            </label>

            <div class="rc-field">
              <div class="rc-field-head">
                <span>人设</span>
                <button
                  type="button"
                  class="rc-ai"
                  :disabled="state.personaBusy || !char.name.trim()"
                  :title="
                    char.name.trim()
                      ? '按名字和介绍让对话模型写一段人设'
                      : '先给角色起个名字'
                  "
                  @click="onPersona"
                >
                  <Loader2 v-if="state.personaBusy" class="rc-spin" :size="11" />
                  <Sparkles v-else :size="11" :stroke-width="1.8" />
                  {{ state.personaBusy ? '写人设中…' : 'AI 生成' }}
                </button>
              </div>
              <textarea
                v-model="char.persona"
                class="sh-field rc-persona-in"
                rows="4"
                maxlength="1200"
                placeholder="告诉模型她怎么说话、和你的关系……"
                @input="scheduleSave"
              />
              <p v-if="state.personaError" class="rc-note is-warn">{{ state.personaError }}</p>
            </div>

            <section class="rc-sheet-sec">
              <h3>声音</h3>
              <div class="rc-voice">
                <span class="rc-voice-avatar" :style="avatarStyle">{{ voiceInitial }}</span>
                <div class="rc-voice-main">
                  <strong>{{ voiceLabel(char) }}</strong>
                </div>
                <button
                  v-if="previewUrl"
                  type="button"
                  class="rc-voice-play"
                  :class="{ 'is-on': voicePlaying }"
                  :title="voicePlaying ? '停止试听' : '试听这条音色'"
                  aria-label="试听这条音色"
                  @click="previewVoice"
                >
                  <Square v-if="voicePlaying" :size="9" fill="currentColor" />
                  <Play v-else :size="11" fill="currentColor" />
                </button>
                <button
                  v-if="char.voice_id"
                  type="button"
                  class="rc-voice-clear"
                  title="清除音色"
                  aria-label="清除音色"
                  @click="clearVoice"
                >
                  <X :size="12" :stroke-width="1.8" />
                </button>
              </div>

              <div class="rc-field rc-voice-picker">
                <span>选择音色</span>
                <VoicePicker
                  v-model="pickedVoiceId"
                  :voices="hostStore.allVoices || []"
                  placeholder="输入名称、标签或音色 ID 检索…"
                />
              </div>

              <div v-if="char.voice_id" class="rc-field is-flat">
                <span>合成引擎</span>
                <!-- 收成一行：只显示现在用的那台；展开才列候选，绿点 = 此刻真能用。 -->
                <div class="rc-engine-row">
                  <i
                    class="rc-engine-dot"
                    :class="{ 'is-ok': currentEngineReady }"
                    aria-hidden="true"
                  />
                  <select
                    :value="char.voice_engine || 'auto'"
                    class="sh-field"
                    aria-label="合成引擎"
                    @change="onEngineChange"
                  >
                    <option value="auto">
                      自动{{ autoEngine ? ` · ${engineLabel(autoEngine)}` : '（没有可用的本地引擎）' }}
                    </option>
                    <option v-if="pinnedEngineLabel" :value="char.voice_engine" disabled>
                      {{ pinnedEngineLabel }}（不可用）
                    </option>
                    <option
                      v-for="item in engineOptions"
                      :key="item.key"
                      :value="item.key"
                      :disabled="!item.available"
                    >
                      {{ item.label }}{{ item.kind === 'cloud' ? '（按量计费）' : '' }}{{ item.available ? '' : '（不可用）' }}
                    </option>
                  </select>
                </div>
                <p
                  v-if="!autoEngine && !engineOptions.some((item) => item.available)"
                  class="rc-note is-warn"
                >
                  没有可用的引擎：本地引擎要在「引擎」页启起来并有实例在跑；云端引擎要配好密钥。
                </p>
              </div>

              <template v-if="char.voice_id && voiceIsLocal">
                <div class="rc-field-head rc-sample-head">
                  <span>样例</span>
                  <span v-if="state.voiceSamplesBusy" class="rc-sample-count">读取中…</span>
                  <span v-else class="rc-sample-count">{{ samples.length }} 条</span>
                </div>
                <SegmentedControl
                  v-model="sampleMode"
                  label="样例策略"
                  :options="sampleModes"
                  block
                />
                <!-- 样例多了也不把纸拉长：收进下拉，一行只显示钉住的那条。 -->
                <label v-if="sampleMode === 'fixed' && samples.length" class="rc-field is-flat rc-sample-pick">
                  <select
                    class="sh-field"
                    :value="char.voice_sample_file"
                    aria-label="用哪一条样例"
                    @change="onSampleChange"
                  >
                    <option v-for="item in samples" :key="item.file" :value="item.file">
                      {{ sampleName(item) }}<template v-if="sampleMeta(item)"> · {{ sampleMeta(item) }}</template>
                    </option>
                  </select>
                </label>
                <p
                  v-else-if="sampleMode === 'fixed' && !state.voiceSamplesBusy"
                  class="rc-note"
                >
                  这个音色没有样例可选：合成时会退回主样例。
                </p>
              </template>
            </section>

            <section class="rc-sheet-sec">
              <div class="rc-sec-head">
                <h3>立绘</h3>
                <button
                  v-if="portraitOf(char)"
                  type="button"
                  class="rc-mini-btn"
                  title="回到默认站位与大小"
                  @click="resetPortraitTransform"
                >
                  <RotateCcw :size="12" :stroke-width="1.8" aria-hidden="true" />
                  复位
                </button>
              </div>
              <div class="rc-art-grid">
                <button
                  type="button"
                  class="rc-art is-none"
                  :class="{ 'is-active': !portraitOf(char) }"
                  :aria-pressed="!portraitOf(char)"
                  title="不站人，只留房间"
                  @click="setPortrait('')"
                >
                  不站人
                </button>
                <button
                  v-for="item in PORTRAITS"
                  :key="item.key"
                  type="button"
                  class="rc-art"
                  :class="{ 'is-active': char.portrait === `builtin:${item.key}` }"
                  :aria-pressed="char.portrait === `builtin:${item.key}`"
                  :title="`${item.label}（会顺手换上配好的房间）`"
                  @click="setPortrait(item.key)"
                >
                  <img :src="item.image" :alt="item.label" loading="lazy" />
                  <span>{{ item.label }}</span>
                </button>
              </div>
              <p class="rc-note">
                画面上直接拖动立绘换位置，滚轮改大小；位置与大小跟着这个角色保存。
              </p>
            </section>

            <section class="rc-sheet-sec">
              <div class="rc-sec-head">
                <h3>背景</h3>
                <label
                  class="sh-btn is-ghost is-sm rc-upload"
                  :class="{ 'is-busy': state.backgroundBusy }"
                  :title="hasCustomBackground ? '换一张自己的图' : '用自己的图替换这间房'"
                >
                  <ImageUp :size="13" :stroke-width="1.8" />
                  {{ state.backgroundBusy ? '上传中…' : '自定义背景' }}
                  <input
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    :disabled="state.backgroundBusy"
                    @change="onBackgroundFile"
                  />
                </label>
              </div>
              <div class="rc-bg-picks">
                <button
                  v-for="item in BACKGROUNDS"
                  :key="item.key"
                  type="button"
                  class="rc-bg-pick"
                  :class="{ 'is-active': backgroundKey === item.key }"
                  :style="{ backgroundImage: `url(${item.image})` }"
                  :aria-pressed="backgroundKey === item.key"
                  :title="item.label"
                  @click="setBackground(`builtin:${item.key}`)"
                >
                  <span>{{ item.label }}</span>
                </button>
              </div>
              <p v-if="hasCustomBackground" class="rc-note">现在用的是你上传的那张</p>
            </section>
          </div>

          <footer class="rc-sheet-foot">
            <span class="rc-save"><b />{{ state.saving ? '保存中…' : '已保存' }}</span>
            <button
              type="button"
              class="rc-sheet-del"
              :disabled="characters.length <= 1 || Boolean(state.busy)"
              :title="
                characters.length <= 1
                  ? '至少留一个角色'
                  : state.busy
                    ? '等这一轮对话结束再删'
                    : '删除角色与它的全部对话'
              "
              @click="onDeleteCharacter"
            >
              <Trash2 :size="13" :stroke-width="1.8" />
              删除角色
            </button>
          </footer>
        </aside>
      </Transition>
    </div>
  </section>
</template>

<script setup>
// @ts-check
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  AlertCircle,
  ArrowLeft,
  ArrowUp,
  Check,
  ChevronsUpDown,
  Cpu,
  History,
  ImageUp,
  Loader2,
  Maximize2,
  Mic,
  Minimize2,
  PenLine,
  Play,
  Plus,
  RotateCcw,
  Sparkles,
  Square,
  Trash2,
  Volume2,
  X,
} from 'lucide-vue-next'
import { actions as hostActions, say, store as hostStore } from '../../store'
import SegmentedControl from '../../components/ui/SegmentedControl.vue'
import VoicePicker from '../../components/VoicePicker.vue'
import {
  active,
  addCharacter,
  assignVoiceToActive,
  BACKGROUNDS,
  backgroundOf,
  backgroundStyle,
  dismissBubble,
  engineChoices,
  engineLabel,
  generatePersona,
  isLocalVoice,
  loadVoiceSamples,
  patchActive,
  resolveAutoEngine,
  resolveEngine,
  introLine,
  listenStop,
  listenToggle,
  messages,
  modelFullHint,
  modelHint,
  newSession,
  playMessage,
  PORTRAITS,
  PORTRAIT_SCALE,
  PORTRAIT_SCALE_MAX,
  PORTRAIT_SCALE_MIN,
  PORTRAIT_X,
  PORTRAIT_X_MAX,
  PORTRAIT_X_MIN,
  PORTRAIT_Y,
  PORTRAIT_Y_MAX,
  portraitOf,
  portraitSrc,
  providerNote,
  refresh,
  removeCharacter,
  removeSession,
  resetPortraitTransform,
  retryAudio,
  scheduleSave,
  sendDraft,
  sessions,
  session,
  sessionCount,
  sessionDate,
  sessionTitle,
  setActive,
  setBackground,
  setModel,
  setPortrait,
  setProvider,
  setSession,
  state,
  syncModels,
  syncSpeaking,
  uploadBackground,
  voiceLabel,
} from './store'

const char = active
const current = session
const characters = computed(() => state.data?.characters || [])
/** 目录里最新的一份排在最上：找的是"刚才那一段"。 */
const orderedSessions = computed(() => [...sessions.value].reverse())

const rootEl = ref(/** @type {HTMLElement|null} */ (null))
const sceneEl = ref(/** @type {HTMLElement|null} */ (null))
const plateEl = ref(/** @type {HTMLElement|null} */ (null))
const figureEl = ref(/** @type {HTMLImageElement|null} */ (null))
const bubbleEl = ref(/** @type {HTMLElement|null} */ (null))
const historyEl = ref(/** @type {HTMLElement|null} */ (null))
const chatEl = ref(/** @type {HTMLElement|null} */ (null))
const inputEl = ref(/** @type {HTMLTextAreaElement|null} */ (null))
const isFullscreen = ref(false)
/** 右侧精简历史抽屉默认收起；呼出时直接展示对话列表，点选进入对应消息流。 */
const historyOpen = ref(false)
/** @type {import('vue').Ref<'list'|'messages'>} */
const historyView = ref('list')
/** 角色设置由插件状态管：音色卡「创建」在浮层里把它打开，工作区这边跟着出现。 */
const sheetOpen = computed({
  get: () => state.sheetOpen,
  set: (value) => {
    state.sheetOpen = value
  },
})

/** 角色设置开着的时候，左栏那颗按钮是「指定」：点卡片给正在编辑的角色挑声音。
 *  收起后回到插件声明的「创建」——点卡片是新建一个角色。 */
watch(
  () => sheetOpen.value && Boolean(char.value),
  (editing) => {
    hostStore.railModeOverride = editing ? 'assign' : ''
  },
  { immediate: true }
)
const rosterOpen = ref(false)
const modelOpen = ref(false)
const switchEl = ref(/** @type {any} */ (null))
const modelWrapEl = ref(/** @type {any} */ (null))
const confirmId = ref('')

/** 换角色就是换屋里坐着的人：顺手收起下拉，别让它在下一张脸上还开着。 */
/** @param {string} id */
function switchChar(id) {
  rosterOpen.value = false
  setActive(id)
}

function onAddCharacter() {
  rosterOpen.value = false
  addCharacter()
}

/** 名牌上滚轮翻角色：一次滚动换一个人，触控板的惯性不连翻。 */
let wheelAcc = 0
let wheelAt = 0
/** @param {WheelEvent} event */
function onPlateWheel(event) {
  const list = characters.value
  const current = char.value
  if (list.length <= 1 || !current) return
  wheelAcc += event.deltaY
  const now = Date.now()
  if (Math.abs(wheelAcc) < 30 || now - wheelAt < 240) return
  const step = wheelAcc > 0 ? 1 : -1
  wheelAcc = 0
  wheelAt = now
  const at = list.findIndex((item) => item.id === current.id)
  const next = list[(at + step + list.length) % list.length]
  if (next) switchChar(next.id)
}

function toggleModel() {
  rosterOpen.value = false
  modelOpen.value = !modelOpen.value
  if (modelOpen.value) void syncModels()
}

/** @param {PointerEvent} event */
function onDocPointer(event) {
  const target = /** @type {Node|null} */ (event.target)
  if (rosterOpen.value && !(target && switchEl.value?.contains(target))) {
    rosterOpen.value = false
  }
  if (modelOpen.value && !(target && modelWrapEl.value?.contains(target))) {
    modelOpen.value = false
  }
}

function syncFullscreen() {
  isFullscreen.value = document.fullscreenElement === rootEl.value
}

async function toggleFullscreen() {
  const el = rootEl.value
  if (!el) return
  try {
    if (document.fullscreenElement === el) await document.exitFullscreen()
    else await el.requestFullscreen()
  } catch {
    say('浏览器未允许全屏，可按 F11。', { kind: 'warn' })
  }
}

const canSend = computed(() => Boolean(char.value && state.draft.trim() && !state.busy))

const defaultProviderLabel = computed(() => {
  const key = hostStore.llm?.key || ''
  const found = state.providers.find((item) => item.key === key)
  return found?.label || '宿主默认'
})

const voiceInitial = computed(() => (char.value?.voice_name || '声').slice(0, 1))
const stageStyle = computed(() => backgroundStyle(char.value))
const hasCustomBackground = computed(() => String(char.value?.background || '').startsWith('custom:'))
/** 当前选中的是哪张内置图：用了自己上传的图时两张都不亮。 */
const backgroundKey = computed(() =>
  hasCustomBackground.value ? '' : backgroundOf(char.value).key
)
/** 立绘图片地址；没选人就是空串，房间只剩背景。 */
const portraitImage = computed(() => portraitSrc(char.value))

/** @param {number} value @param {number} low @param {number} high */
function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value))
}

/** 立绘的站位与大小：x / y 是相对房间的比例，scale 是高度倍数。 */
const figureStyle = computed(() => {
  const target = char.value
  return {
    left: `${(target?.portrait_x ?? PORTRAIT_X) * 100}%`,
    bottom: `${(target?.portrait_y ?? PORTRAIT_Y) * 100}%`,
    height: `${(target?.portrait_scale ?? PORTRAIT_SCALE) * 100}%`,
  }
})

/** 立绘脚下的柔和接触投影，让人物贴合在房间地面上。 */
const figureGroundStyle = computed(() => {
  const target = char.value
  const px = target?.portrait_x ?? PORTRAIT_X
  const py = target?.portrait_y ?? PORTRAIT_Y
  const scale = target?.portrait_scale ?? PORTRAIT_SCALE
  const sw = Math.max(320, sceneSize.value.w)
  const sh = Math.max(280, sceneSize.value.h)
  const nat = portraitNatural.value
  const aspect = nat.w > 0 && nat.h > 0 ? nat.w / nat.h : 0.62
  const visW = Math.min(sw * 0.58, scale * sh * aspect)
  return {
    left: `${Math.round(px * sw + visW * 0.12)}px`,
    bottom: `${Math.max(0, Math.round(py * sh - 10))}px`,
    width: `${Math.round(visW * 0.76)}px`,
    opacity: clamp(1 - py * 2.2, 0.28, 0.9),
  }
})
const sceneSize = ref({ w: 960, h: 640 })
const plateBounds = ref({ right: 340, bottom: 120 })
const portraitNatural = ref({ w: 0, h: 0 })
const bubbleSize = ref({ w: 260, h: 76 })
const historyWidth = ref(0)

function onPortraitLoad() {
  const img = figureEl.value
  if (!img) return
  portraitNatural.value = { w: img.naturalWidth || 0, h: img.naturalHeight || 0 }
}

/** 窄屏下历史抽屉盖在画面上，不挤压中央输入栏；宽屏下居中区域自动让开历史抽屉。 */
const effectiveHistoryWidth = computed(() =>
  historyOpen.value && sceneSize.value.w > 640 ? historyWidth.value : 0
)

const stageVars = computed(() => ({
  '--rc-history-w': `${effectiveHistoryWidth.value}px`,
}))

/** 正在想：让气泡在立绘旁亮出三颗呼吸点，收起历史也能看见反馈。 */
const bubbleThinking = computed(() => state.busy === 'thinking')

/** 气泡文案：优先显示正在播放的那一句；其次显示本轮刚收到的回复（无语音或播完仍可读）。 */
const bubbleText = computed(() => {
  if (bubbleThinking.value) return ''
  const targetId = state.speakingId || state.lastReplyId
  if (!targetId) return ''
  const line = messages.value.find((row) => row.id === targetId)
  if (!line || line.role !== 'assistant' || line.error) return ''
  return String(line.text || '').trim()
})

const bubbleVisible = computed(() => bubbleThinking.value || Boolean(bubbleText.value))
const canDismissBubble = computed(
  () => !bubbleThinking.value && !state.speakingId && Boolean(state.lastReplyId)
)

/**
 * 气泡跟随立绘的实时几何：
 * - 按立绘的 portrait_x / portrait_y / portrait_scale 与真实宽高比算出可见轮廓；
 * - 锚定在立绘肩/头部高度，优先放右侧，右侧空间不足时自动翻到左侧；
 * - 依据气泡实测高度做上下收边界，并预留底部悬浮输入栏高度，保证任何字数都能完整显示。
 */
const bubbleLayout = computed(() => {
  const sw = Math.max(320, sceneSize.value.w)
  const sh = Math.max(280, sceneSize.value.h)
  const hw = effectiveHistoryWidth.value
  const stageW = Math.max(260, sw - hw)
  const safeLeft = 18
  const safeRight = Math.max(safeLeft + 200, stageW - 18)
  const safeTop = 20
  const dockReserve = 104
  const safeBottom = Math.max(safeTop + 120, sh - dockReserve)
  const maxBubbleH = Math.max(110, safeBottom - safeTop)
  const measuredH = Math.min(bubbleThinking.value ? 46 : Math.max(48, bubbleSize.value.h), maxBubbleH)

  if (!portraitImage.value) {
    const maxW = clamp(stageW - 64, 200, 460)
    const top = clamp(sh * 0.26, safeTop + 56, Math.max(safeTop + 56, safeBottom - measuredH))
    return {
      tail: 'none',
      style: {
        left: `${Math.round(stageW / 2)}px`,
        top: `${Math.round(top)}px`,
        maxWidth: `${Math.round(maxW)}px`,
        maxHeight: `${Math.round(maxBubbleH)}px`,
        transform: 'translateX(-50%)',
      },
    }
  }

  const px = char.value?.portrait_x ?? PORTRAIT_X
  const py = char.value?.portrait_y ?? PORTRAIT_Y
  const scale = char.value?.portrait_scale ?? PORTRAIT_SCALE
  const elLeft = px * sw
  const elHeight = scale * sh
  const elBottom = sh - py * sh
  const maxFigW = sw * 0.58
  const nat = portraitNatural.value
  const aspect = nat.w > 0 && nat.h > 0 ? nat.w / nat.h : 0.62
  let visW = elHeight * aspect
  let visH = elHeight
  if (visW > maxFigW) {
    visW = maxFigW
    visH = visW / aspect
  }
  const visLeft = elLeft
  const visRight = elLeft + visW
  const visTop = elBottom - visH

  // 头部/肩部锚点：立绘顶边被裁出上沿时从画面顶向下取，保证跟着人上下走。
  const visibleTop = Math.max(0, visTop)
  const anchorY = clamp(visibleTop + Math.min(visH * 0.16, 110), safeTop + 24, safeBottom - 44)

  // 立绘下半身裙摆通常比肩颈宽，向肩颈略微贴近一点，气泡才像贴着她在说话。
  const overlapRatio = aspect < 0.85 ? 0.13 : 0.08
  const overlap = Math.min(56, visW * overlapRatio)
  const rightStart = Math.max(safeLeft, visRight - overlap)
  const availRight = safeRight - rightStart
  const leftEnd = Math.min(safeRight, visLeft + overlap)
  const availLeft = leftEnd - safeLeft
  const preferRight = availRight >= 230 || availRight >= availLeft

  const desiredTop = anchorY - Math.min(34, measuredH * 0.32)

  if (preferRight) {
    const maxW = clamp(availRight, 180, 420)
    const left = clamp(rightStart, safeLeft, Math.max(safeLeft, safeRight - Math.min(maxW, 200)))
    const actualMaxW = clamp(safeRight - left, 160, 420)
    const minTop = left < plateBounds.value.right + 12 ? Math.max(safeTop, plateBounds.value.bottom + 14) : safeTop
    const top = clamp(desiredTop, minTop, Math.max(minTop, safeBottom - measuredH))
    const tailTop = clamp(anchorY - top, 20, Math.max(20, measuredH - 22))
    return {
      tail: 'left',
      style: {
        left: `${Math.round(left)}px`,
        top: `${Math.round(top)}px`,
        maxWidth: `${Math.round(actualMaxW)}px`,
        maxHeight: `${Math.round(Math.max(110, safeBottom - minTop))}px`,
        '--rc-tail-top': `${Math.round(tailTop)}px`,
      },
    }
  }

  const maxW = clamp(availLeft, 180, 420)
  const anchorRightEdge = clamp(leftEnd, safeLeft + Math.min(maxW, 200), safeRight)
  const actualMaxW = clamp(anchorRightEdge - safeLeft, 160, 420)
  const estLeft = anchorRightEdge - Math.min(actualMaxW, Math.max(180, bubbleSize.value.w))
  const minTop = estLeft < plateBounds.value.right + 12 ? Math.max(safeTop, plateBounds.value.bottom + 14) : safeTop
  const top = clamp(desiredTop, minTop, Math.max(minTop, safeBottom - measuredH))
  const tailTop = clamp(anchorY - top, 20, Math.max(20, measuredH - 22))
  return {
    tail: 'right',
    style: {
      right: `${Math.round(sw - anchorRightEdge)}px`,
      top: `${Math.round(top)}px`,
      maxWidth: `${Math.round(actualMaxW)}px`,
      maxHeight: `${Math.round(Math.max(110, safeBottom - minTop))}px`,
      '--rc-tail-top': `${Math.round(tailTop)}px`,
    },
  }
})

/** 正在拖的那一次：记下起点与房间尺寸，按位移换算比例。 */
/** @type {any} */
const portraitDrag = ref(null)

/** @param {PointerEvent} event */
function startPortraitDrag(event) {
  const target = char.value
  const scene = sceneEl.value
  if (!target?.portrait || !scene || event.button !== 0) return
  const rect = scene.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  event.preventDefault()
  portraitDrag.value = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    x: target.portrait_x ?? PORTRAIT_X,
    y: target.portrait_y ?? PORTRAIT_Y,
    width: rect.width,
    height: rect.height,
  }
  window.addEventListener('pointermove', movePortrait)
  window.addEventListener('pointerup', endPortrait, { once: true })
  window.addEventListener('pointercancel', endPortrait, { once: true })
}

/** @param {PointerEvent} event */
function movePortrait(event) {
  const drag = portraitDrag.value
  const target = char.value
  if (!drag || !target || event.pointerId !== drag.pointerId) return
  const dx = (event.clientX - drag.startX) / drag.width
  // 屏幕向下 = 人往低处走，正好与 bottom 相反。
  const dy = (drag.startY - event.clientY) / drag.height
  target.portrait_x = clamp(drag.x + dx, PORTRAIT_X_MIN, PORTRAIT_X_MAX)
  target.portrait_y = clamp(drag.y + dy, PORTRAIT_Y, PORTRAIT_Y_MAX)
}

function endPortrait() {
  if (!portraitDrag.value) return
  portraitDrag.value = null
  window.removeEventListener('pointermove', movePortrait)
  window.removeEventListener('pointerup', endPortrait)
  window.removeEventListener('pointercancel', endPortrait)
  // 松手才落盘：拖动过程中不每帧写一次状态文件。
  scheduleSave()
}

/** 滚轮改大小：一次一格，边界与后端一致。 */
/** @param {WheelEvent} event */
function onPortraitWheel(event) {
  const target = char.value
  if (!target?.portrait) return
  const step = event.deltaY > 0 ? -0.04 : 0.04
  target.portrait_scale = clamp(
    (target.portrait_scale ?? PORTRAIT_SCALE) + step,
    PORTRAIT_SCALE_MIN,
    PORTRAIT_SCALE_MAX
  )
  scheduleSave()
}

const avatarStyle = computed(() => {
  const cover = char.value?.voice_cover || ''
  if (cover) {
    return {
      backgroundImage: `url("${cover}")`,
      backgroundSize: 'cover',
      backgroundPosition: 'center',
    }
  }
  return {}
})

/** 文件名 → 样例名：去掉序号与扩展名（与音色详情、指定弹窗同一套说法）。 */
/** @param {any} item */
function sampleName(item) {
  return String(item?.file || '')
    .replace(/\.[^.]+$/, '')
    .replace(/^\d+[_-]?/, '')
}

/** 样例副行：情绪 / 主样例 / 时长。 */
/** @param {any} item */
function sampleMeta(item) {
  const bits = []
  if (item?.emotion) bits.push(item.emotion)
  if (item?.is_main) bits.push('主样例')
  if (Number(item?.duration) > 0) bits.push(`${Number(item.duration).toFixed(1)}s`)
  return bits.join(' · ')
}

/** 角色设置里引擎候选：含云端，带上"此刻有没有绿点"；自动档另按本地判据挑。 */
const engineOptions = computed(() => engineChoices(char.value))
/** 自动档此刻落在哪台可用引擎；空 = 一台能跑的都没有。 */
const autoEngine = computed(() => resolveAutoEngine(char.value))
/** 这一句实际会发给哪台（钉住的不可用时也会回落到自动）。 */
const currentEngine = computed(() => resolveEngine(char.value))
/** 当前这条链路是不是真有绿点：下拉左边那一点就说明这件事。 */
const currentEngineReady = computed(() =>
  engineOptions.value.some((item) => item.key === currentEngine.value && item.available)
)
/** 钉住了一台已经不在候选里的引擎（停用 / 没装）：显式列出来，别让下拉空着。 */
const pinnedEngineLabel = computed(() => {
  const key = String(char.value?.voice_engine || '')
  if (!key || key === 'auto') return ''
  return engineOptions.value.some((item) => item.key === key) ? '' : engineLabel(key)
})

/** @param {Event} event */
function onEngineChange(event) {
  patchActive({ voice_engine: /** @type {HTMLSelectElement} */ (event.target).value || 'auto' })
}

/** 本地音色才有「固定样例 / 按情绪匹配」这一层。 */
const voiceIsLocal = computed(() => isLocalVoice(char.value))
const samples = computed(() => (Array.isArray(state.voiceSamples) ? state.voiceSamples : []))
const sampleModes = [
  { value: 'emotion', label: '按情绪匹配' },
  { value: 'fixed', label: '固定样例' },
]
const sampleMode = computed({
  get: () => char.value?.voice_sample_mode || 'emotion',
  set: (value) => {
    void applySampleMode(value)
  },
})

/**
 * 切样例策略。
 *
 * 「固定」必须**立刻钉住一条**：服务端认这个不变量（fixed 却没有文件就退回
 * emotion），要是先只改档位，保存回来后档位又被拨回去，看着就像"固定样例选不了"。
 * 所以先取样例，再挑主样例（没有就第一条）一起写进去。
 *
 * @param {string} value
 */
async function applySampleMode(value) {
  const target = char.value
  if (!target) return
  if (value !== 'fixed') {
    patchActive({ voice_sample_mode: 'emotion', voice_sample_file: '' })
    return
  }
  // 打开设置时已经取过这一份就用它，别为一次点击再等一趟网络。
  const cached = state.voiceSamplesFor === target.voice_id && samples.value.length
  const list = cached ? samples.value : await loadVoiceSamples(true)
  if (char.value?.id !== target.id) return
  const pinned = String(target.voice_sample_file || '')
  const pick =
    list.find((item) => item.file === pinned) ||
    list.find((item) => item.is_main) ||
    list[0] ||
    null
  if (!pick) {
    say('这个音色没有样例可选，仍然按情绪匹配。', { kind: 'warn' })
    patchActive({ voice_sample_mode: 'emotion', voice_sample_file: '' })
    return
  }
  patchActive({ voice_sample_mode: 'fixed', voice_sample_file: String(pick.file) })
}

/** 试听地址：固定样例就用钉住的那条，否则用主样例（与指定弹窗同一口径）。 */
const previewUrl = computed(() => {
  const target = char.value
  if (!target?.voice_id) return ''
  if (target.voice_sample_mode === 'fixed' && target.voice_sample_file) {
    const hit = samples.value.find((item) => item.file === target.voice_sample_file)
    if (hit?.audio_url) return hit.audio_url
  }
  return (
    samples.value.find((item) => item.is_main)?.audio_url ||
    samples.value[0]?.audio_url ||
    ''
  )
})
const voicePlaying = computed(
  () => Boolean(previewUrl.value) && hostStore.player?.url === previewUrl.value && hostStore.player.playing
)

function previewVoice() {
  if (!previewUrl.value) return
  if (hostStore.player?.url === previewUrl.value) {
    hostActions.togglePlay()
    return
  }
  const sub = char.value?.voice_sample_mode === 'fixed' ? '固定样例' : '主样例'
  hostActions.play({ url: previewUrl.value, label: char.value?.voice_name || '', sub })
}

/** @param {any} item */
function pickSample(item) {
  if (!item?.file) return
  patchActive({ voice_sample_mode: 'fixed', voice_sample_file: String(item.file) })
}

/** 固定样例的下拉：选一条就钉住它（档位同时回到 fixed）。 */
/** @param {Event} event */
function onSampleChange(event) {
  pickSample({ file: /** @type {HTMLSelectElement} */ (event.target).value })
}

function onPersona() {
  void generatePersona(char.value)
}

/** 输入框右下的那句话：正在发生的事优先，其次才是模型名。 */
const busyHint = computed(() => {
  if (state.busy === 'thinking') return '正在想…'
  if (state.busy === 'speaking') return '正在合成语音…'
  if (state.listening === 'connecting') return '正在打开麦克风…'
  if (state.listening === 'listening') return state.micSpeaking ? '在听你说…' : '麦克风开着，说完一句自动发送'
  // 平时不重复模型名：它就在右下那枚胶囊里。
  return ''
})

/** 换人时才写名字：连续几句都是同一个人说话时，只有第一句带名字。
 *  出错的那一行永远写"出错"，免得读者以为那句话是角色说的。 */
/** @param {number} index */
function speakerOf(index) {
  const message = messages.value[index]
  if (!message) return ''
  if (message.error) return '出错'
  if (!startsTurn(index)) return ''
  return message.role === 'user' ? '你' : char.value?.name || ''
}

/** 这一行要不要那枚播放 / 合成的小按钮：只给说出来的话，且得真有事可做。 */
/** @param {any} message */
function hasAudioActs(message) {
  if (!message || message.role !== 'assistant' || message.error) return false
  return Boolean(message.audio_url || char.value?.voice_id)
}

/** @param {number} index */
function startsTurn(index) {
  if (index === 0) return true
  return messages.value[index - 1]?.role !== messages.value[index]?.role
}

function autosize() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 132)}px`
}

/** Enter 发送；输入法组字期间的 Enter 是确认候选词，不能当发送。 */
/** @param {KeyboardEvent} event */
function onEnter(event) {
  if (event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  sendDraft()
}

function scrollToBottom() {
  const el = chatEl.value
  if (el) el.scrollTop = el.scrollHeight
}

function openSheet() {
  // 从名牌打开的是"改这个角色"，不是刚才那次新建。
  state.sheetNew = false
  state.personaError = ''
  sheetOpen.value = true
}

/** 角色设置里的音色检索选择：支持输入关键词搜索音色库，或直接输入音色 ID。 */
const pickedVoiceId = computed({
  get: () => char.value?.voice_id || '',
  set: (id) => {
    void onPickVoice(id)
  },
})

/** @param {string} id */
async function onPickVoice(id) {
  const target = char.value
  if (!target) return
  const clean = String(id || '').trim()
  if (!clean) {
    clearVoice()
    return
  }
  const hit = (hostStore.allVoices || []).find((item) => item.id === clean)
  if (hit) {
    await assignVoiceToActive(hit)
    await loadVoiceSamples(true)
    return
  }
  patchActive({
    voice_id: clean,
    voice_name: clean,
    voice_engine: 'auto',
    voice_engines: [],
    voice_cover: '',
    voice_local: false,
    voice_sample_mode: 'emotion',
    voice_sample_file: '',
  })
  await loadVoiceSamples(true)
}

function clearVoice() {
  if (!char.value) return
  patchActive({
    voice_id: '',
    voice_name: '',
    voice_engine: '',
    voice_engines: [],
    voice_cover: '',
    voice_local: false,
    voice_sample_mode: 'emotion',
    voice_sample_file: '',
  })
  state.voiceSamples = []
  state.voiceSamplesFor = ''
}

/** 打开右侧精简历史抽屉：默认直接显示对话列表。 */
function openHistory() {
  confirmId.value = ''
  historyView.value = 'list'
  historyOpen.value = true
}

/** 收起历史抽屉：把画面完整还给人物与背景。 */
function closeHistory() {
  confirmId.value = ''
  historyOpen.value = false
}

function onNewSession() {
  newSession()
  confirmId.value = ''
  historyView.value = 'messages'
  nextTick(() => {
    scrollToBottom()
    inputEl.value?.focus()
  })
}

/** @param {string} id */
function openSession(id) {
  setSession(id)
  confirmId.value = ''
  historyView.value = 'messages'
  nextTick(() => {
    scrollToBottom()
  })
}

/** @param {string} id */
function dropSession(id) {
  if (state.busy) return
  removeSession(id)
  confirmId.value = ''
  nextTick(scrollToBottom)
}

function onDeleteCharacter() {
  if (!char.value) return
  if (characters.value.length <= 1 || state.busy) return
  if (!window.confirm(`确认删除「${char.value.name}」？`)) return
  removeCharacter(char.value.id)
  sheetOpen.value = false
}

/** @param {Event} event */
function onBackgroundFile(event) {
  const input = /** @type {HTMLInputElement} */ (event.target)
  const file = input.files?.[0]
  if (file) void uploadBackground(file)
  input.value = ''
}

/** @param {Event} event */
function onProviderChange(event) {
  setProvider(/** @type {HTMLSelectElement} */ (event.target).value)
}

/** @param {Event} event */
function onModelChange(event) {
  setModel(/** @type {HTMLSelectElement} */ (event.target).value)
}

/** @param {Event} event */
function onModelInput(event) {
  setModel(/** @type {HTMLInputElement} */ (event.target).value)
}

/** 「对话」按钮的悬浮说明：没配引擎说去哪儿配，开着说怎么停。 */
const listenTitle = computed(() => {
  if (!hostStore.asr?.ready) return '先去「引擎 → 识别」配一档'
  if (state.listening === 'listening') return '麦克风常开，说完一段自动发送；点这里停止'
  return '麦克风常开，说完一段自动发送'
})

watch(
  () => [messages.value.length, state.busy],
  async () => {
    await nextTick()
    scrollToBottom()
  }
)

watch(
  () => current.value?.id,
  async () => {
    await nextTick()
    scrollToBottom()
    autosize()
  }
)

watch(
  () => state.focusTick,
  async () => {
    await nextTick()
    inputEl.value?.focus()
  }
)

/** 打开角色设置时顺带确保全量音色清单就位，并重取一次样例。 */
watch(
  () => [sheetOpen.value, char.value?.voice_id],
  ([open, id]) => {
    if (!open) return
    if (!hostStore.allVoices?.length) void hostActions.loadAllVoices()
    if (id) void loadVoiceSamples(true)
  },
  { immediate: true }
)

/** 纸收起来以后，下一次从名牌打开时标题回到「角色设置」。 */
watch(sheetOpen, (open) => {
  if (!open) state.sheetNew = false
})

// 播放条是全应用共用的：这里只把"正在放的是哪一句"认出来，别的不碰。
watch(
  () => [hostStore.player?.playing, hostStore.player?.url, state.speakingId],
  () => syncSpeaking()
)

/** 发送清空草稿后把输入框高度收回到单行。 */
watch(
  () => state.draft,
  (val) => {
    if (!val && inputEl.value) inputEl.value.style.height = ''
  }
)

/** @param {KeyboardEvent} event */
function onKeydown(event) {
  if (event.key !== 'Escape') return
  if (sheetOpen.value) {
    sheetOpen.value = false
    return
  }
  if (modelOpen.value) {
    modelOpen.value = false
    return
  }
  if (historyOpen.value) {
    if (historyView.value === 'messages') {
      historyView.value = 'list'
      return
    }
    historyOpen.value = false
    return
  }
  if (canDismissBubble.value) dismissBubble()
}

/** @type {ResizeObserver|null} */
let sceneObserver = null
/** @type {ResizeObserver|null} */
let bubbleObserver = null
/** @type {ResizeObserver|null} */
let historyObserver = null

function measureScene() {
  const el = sceneEl.value
  if (!el) return
  sceneSize.value = { w: el.clientWidth || 960, h: el.clientHeight || 640 }
  const plate = plateEl.value
  if (plate) {
    const sr = el.getBoundingClientRect()
    const pr = plate.getBoundingClientRect()
    plateBounds.value = {
      right: Math.max(0, pr.right - sr.left),
      bottom: Math.max(0, pr.bottom - sr.top),
    }
  }
}

watch(
  () => [plateEl.value, char.value?.name, char.value?.intro],
  () => void nextTick(measureScene),
  { flush: 'post' }
)

watch(
  () => figureEl.value,
  (img) => {
    if (!img) {
      portraitNatural.value = { w: 0, h: 0 }
      return
    }
    if (img.complete && img.naturalWidth) onPortraitLoad()
  },
  { flush: 'post' }
)

watch(
  () => bubbleEl.value,
  (el) => {
    bubbleObserver?.disconnect()
    bubbleObserver = null
    if (!el) return
    bubbleSize.value = { w: el.offsetWidth || 260, h: el.offsetHeight || 76 }
    if (typeof ResizeObserver !== 'undefined') {
      bubbleObserver = new ResizeObserver(() => {
        if (!bubbleEl.value) return
        bubbleSize.value = {
          w: bubbleEl.value.offsetWidth || 260,
          h: bubbleEl.value.offsetHeight || 76,
        }
      })
      bubbleObserver.observe(el)
    }
  },
  { flush: 'post' }
)

watch(
  () => historyEl.value,
  (el) => {
    historyObserver?.disconnect()
    historyObserver = null
    if (!el) {
      historyWidth.value = 0
      return
    }
    historyWidth.value = el.offsetWidth || 0
    if (typeof ResizeObserver !== 'undefined') {
      historyObserver = new ResizeObserver(() => {
        historyWidth.value = historyEl.value?.offsetWidth || 0
      })
      historyObserver.observe(el)
    }
  },
  { flush: 'post' }
)

onMounted(() => {
  void nextTick(() => {
    measureScene()
    scrollToBottom()
  })
  if (typeof ResizeObserver !== 'undefined' && sceneEl.value) {
    sceneObserver = new ResizeObserver(measureScene)
    sceneObserver.observe(sceneEl.value)
  }
  document.addEventListener('fullscreenchange', syncFullscreen)
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('pointerdown', onDocPointer)
})

onBeforeUnmount(() => {
  sceneObserver?.disconnect()
  bubbleObserver?.disconnect()
  historyObserver?.disconnect()
  document.removeEventListener('fullscreenchange', syncFullscreen)
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('pointerdown', onDocPointer)
  // 音色栏的动作改由页内状态决定：离开这一页就交回插件的声明值。
  hostStore.railModeOverride = ''
  // 拖到一半离开页面：去掉监听，位置已经写在角色上，下次打开照旧。
  endPortrait()
  // 离开页面就收麦：没说完的冲出来发掉，不留后台采集。
  void listenStop()
})
</script>

<style scoped>
/* 房间即工作区：场景（用户传的一张图，或界面底色）铺满，名牌与台本直接落在画面上。
 * 图是**内容**，不随外观压平；属于外观层的只有台本那层磨砂与名牌胶囊上的毛玻璃。 */
.rc {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  padding: 10px 0 12px 14px;
  /* 这一页把右槽让出来：房间贴着内容区的右沿，多出来的宽度全给画面。 */
  margin-right: calc(-1 * var(--sh-gutter));
  /* 房间按**自己的宽度**排版：它夹在导航、分类栏与音色列之后，
     视口再宽，屋里也可能只剩六百来像素。断点全部走 @container。 */
  container-type: inline-size;
  container-name: room;
}

.rc-scene {
  --rc-ink: var(--sh-media-text);
  --rc-sub: color-mix(in srgb, var(--sh-media-text) 68%, transparent);
  --rc-line: color-mix(in srgb, var(--sh-media-text) 16%, transparent);
  --rc-accent: color-mix(in srgb, var(--sh-accent) 52%, var(--sh-media-text));
  --rc-warm: color-mix(in srgb, var(--sh-warn) 45%, var(--sh-media-text));
  --rc-danger: color-mix(in srgb, var(--sh-danger) 55%, var(--sh-media-text));

  position: relative;
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-radius-card);
  background-color: var(--sh-media-bg);
  /* 一张画铺满整块：cover 是设计稿的取法，窗口再宽也不拼第二张。 */
  background-position: 26% 34%;
  background-size: cover;
  background-repeat: no-repeat;
  box-shadow: var(--sh-shadow-2);
  isolation: isolate;
}

/* 轻度环境暗角：只托住左上名牌与底部悬浮栏的可读性，把人物与背景清晰留出来。 */
.rc-shade {
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    linear-gradient(
      0deg,
      color-mix(in srgb, var(--sh-media-bg) 34%, transparent) 0%,
      color-mix(in srgb, var(--sh-media-bg) 10%, transparent) 16%,
      transparent 28%
    ),
    linear-gradient(
      180deg,
      color-mix(in srgb, var(--sh-media-bg) 28%, transparent) 0%,
      transparent 18%
    );
}

/* ---- 立绘 ---------------------------------------------------------- */

/* 人站在房间里：透明底、脚贴地面，带柔和地面接触影与场景色温/明度协调，避免贴图感。 */
.rc-figure-ground {
  position: absolute;
  z-index: 1;
  height: 36px;
  border-radius: 50%;
  background: radial-gradient(
    ellipse at center,
    color-mix(in srgb, var(--sh-media-bg) 68%, transparent) 0%,
    color-mix(in srgb, var(--sh-media-bg) 28%, transparent) 48%,
    transparent 74%
  );
  filter: blur(8px);
  pointer-events: none;
}

.rc-figure {
  position: absolute;
  bottom: 0;
  left: 2%;
  z-index: 1;
  height: 100%;
  max-width: 58%;
  object-fit: contain;
  object-position: bottom left;
  cursor: grab;
  touch-action: none;
  user-select: none;
  filter:
    drop-shadow(0 16px 28px color-mix(in srgb, var(--sh-media-bg) 56%, transparent))
    drop-shadow(0 2px 10px color-mix(in srgb, var(--sh-media-bg) 34%, transparent))
    brightness(0.94)
    contrast(1.02)
    saturate(0.95);
  transition: filter var(--sh-t) var(--sh-ease);
}

.rc-figure.is-grounded {
  mask-image: linear-gradient(to top, transparent 0%, rgb(0 0 0 / 82%) 3.5%, black 9%);
}

.is-bg-dawn .rc-figure {
  filter:
    drop-shadow(0 14px 24px color-mix(in srgb, var(--sh-media-bg) 34%, transparent))
    brightness(0.99)
    saturate(1.02);
}

.rc-figure.is-dragging {
  cursor: grabbing;
}

/* ---- 气泡 ---------------------------------------------------------- */
/* 气泡紧跟立绘位置与大小，按空间自动翻边并在上下安全边界内完整展示全部文字。 */
.rc-bubble {
  position: absolute;
  z-index: 3;
  display: flex;
  width: max-content;
  min-width: 140px;
  flex-direction: column;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 22%, transparent);
  border-radius: var(--sh-r-lg);
  background:
    linear-gradient(
      150deg,
      color-mix(in srgb, var(--sh-media-text) 16%, transparent) 0%,
      color-mix(in srgb, var(--sh-media-text) 4%, transparent) 48%,
      transparent 100%
    ),
    color-mix(in srgb, var(--sh-media-bg) 42%, transparent);
  box-shadow:
    var(--sh-shadow-2),
    inset 0 1px 0 color-mix(in srgb, var(--sh-media-text) 18%, transparent);
  color: var(--rc-ink);
  backdrop-filter: blur(18px) saturate(1.14);
  transition: border-color var(--sh-t) var(--sh-ease);
}

.rc-bubble.is-speaking {
  border-color: color-mix(in srgb, var(--sh-warn) 36%, color-mix(in srgb, var(--sh-media-text) 22%, transparent));
}

.rc-bubble.is-thinking {
  min-width: 0;
  padding: 12px 16px;
}

/* 指向立绘的尖角：随立绘头部锚点上下对齐，自动随左右翻边切换方向。 */
.rc-bubble::after {
  position: absolute;
  top: var(--rc-tail-top, 26px);
  width: 12px;
  height: 12px;
  background: color-mix(in srgb, var(--sh-media-bg) 42%, transparent);
  content: '';
  pointer-events: none;
  transform: translateY(-50%) rotate(45deg);
}

.rc-bubble.is-tail-left::after {
  left: -7px;
  border-bottom: 1px solid color-mix(in srgb, var(--sh-media-text) 20%, transparent);
  border-left: 1px solid color-mix(in srgb, var(--sh-media-text) 20%, transparent);
  border-radius: 0 0 0 3px;
}

.rc-bubble.is-tail-right::after {
  right: -7px;
  border-top: 1px solid color-mix(in srgb, var(--sh-media-text) 20%, transparent);
  border-right: 1px solid color-mix(in srgb, var(--sh-media-text) 20%, transparent);
  border-radius: 0 3px 0 0;
}

.rc-bubble.is-tail-none::after {
  display: none;
}

.rc-bubble-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 13px 20px 14px 18px;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--sh-media-text) 24%, transparent) transparent;
}

.rc-bubble-scroll::-webkit-scrollbar {
  width: 5px;
}

.rc-bubble-scroll::-webkit-scrollbar-thumb {
  border-radius: 4px;
  background: color-mix(in srgb, var(--sh-media-text) 24%, transparent);
}

.rc-bubble-text {
  margin: 0;
  font-size: 14.5px;
  line-height: 1.78;
  letter-spacing: 0.01em;
  overflow-wrap: anywhere;
  text-shadow: 0 1px 14px color-mix(in srgb, var(--sh-media-bg) 70%, transparent);
  text-wrap: pretty;
  white-space: pre-wrap;
}

.rc-bubble-close {
  position: absolute;
  top: 6px;
  right: 6px;
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: color-mix(in srgb, var(--sh-media-bg) 42%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 72%, transparent);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--sh-t-fast) var(--sh-ease), background var(--sh-t-fast) var(--sh-ease),
    color var(--sh-t-fast) var(--sh-ease);
}

.rc-bubble:hover .rc-bubble-close,
.rc-bubble:focus-within .rc-bubble-close {
  opacity: 1;
}

.rc-bubble-close:hover {
  background: color-mix(in srgb, var(--sh-media-text) 18%, transparent);
  color: var(--rc-ink);
}

.rc-bubble-close:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px var(--sh-accent-ring);
}

.rc-bubble-dots {
  display: flex;
  align-items: center;
  gap: 5px;
  height: 16px;
}

.rc-bubble-dots i {
  display: block;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--sh-media-text) 80%, transparent);
  animation: rc-dot 1.2s infinite;
}

.rc-bubble-dots i:nth-child(2) {
  animation-delay: 0.16s;
}

.rc-bubble-dots i:nth-child(3) {
  animation-delay: 0.32s;
}

.rc-bubble-enter-active {
  transition: opacity 0.24s var(--sh-ease), transform 0.3s var(--sh-ease);
}

.rc-bubble-leave-active {
  transition: opacity 0.2s var(--sh-ease), transform 0.2s var(--sh-ease);
}

.rc-bubble-enter-from,
.rc-bubble-leave-to {
  opacity: 0;
  transform: translateY(8px) scale(0.98);
}

/* 晨光整体偏亮：底部多补一点柔和过渡，保证悬浮栏文字清晰。 */
.is-bg-dawn .rc-shade {
  background:
    linear-gradient(
      0deg,
      color-mix(in srgb, var(--sh-media-bg) 42%, transparent) 0%,
      color-mix(in srgb, var(--sh-media-bg) 14%, transparent) 18%,
      transparent 30%
    ),
    linear-gradient(
      180deg,
      color-mix(in srgb, var(--sh-media-bg) 34%, transparent) 0%,
      transparent 20%
    );
}

/* ---- 名牌 ---------------------------------------------------------- */

.rc-plate {
  position: absolute;
  top: 28px;
  left: 28px;
  z-index: 2;
  display: flex;
  width: min(320px, 34%);
  flex-direction: column;
  gap: var(--sh-gap-2);
}

.rc-plate.is-open {
  z-index: 6;
}

/* 名牌落在左上角：垫一层从左上散开的暗雾，字才读得出来。 */
.rc-plate::before {
  position: absolute;
  z-index: -1;
  inset: -28px -64px -22px -38px;
  background: radial-gradient(
    120% 120% at 12% 10%,
    color-mix(in srgb, var(--sh-media-bg) 52%, transparent) 0%,
    color-mix(in srgb, var(--sh-media-bg) 26%, transparent) 46%,
    color-mix(in srgb, var(--sh-media-bg) 8%, transparent) 68%,
    transparent 80%
  );
  content: '';
  pointer-events: none;
}

.rc-name {
  margin: 0;
  color: var(--rc-ink);
  font-family: var(--sh-font-serif);
  font-size: 36px;
  font-weight: 500;
  letter-spacing: 0.03em;
  line-height: 1.08;
  text-wrap: balance;
  text-shadow: 0 2px 24px color-mix(in srgb, var(--sh-media-bg) 80%, transparent);
}

.rc-intro {
  margin: 0;
  max-width: 26ch;
  color: var(--rc-sub);
  font-size: 12.5px;
  line-height: 1.8;
  text-wrap: pretty;
}

/* 编辑角色：名字后面只留一颗图标，与切角色那颗同一副长相。 */
.rc-edit {
  display: inline-grid;
  place-items: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 22%, transparent);
  border-radius: var(--sh-r-xs);
  background: color-mix(in srgb, var(--sh-media-bg) 34%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 74%, transparent);
  cursor: pointer;
  transition: border-color var(--sh-t) var(--sh-ease), color var(--sh-t) var(--sh-ease),
    background var(--sh-t) var(--sh-ease);
}

.rc-edit:hover,
.rc-edit[aria-expanded='true'] {
  border-color: color-mix(in srgb, var(--sh-accent) 56%, transparent);
  background: color-mix(in srgb, var(--sh-media-bg) 52%, transparent);
  color: var(--rc-ink);
}

.rc-edit:focus-visible,
.rc-switch:focus-visible,
.rc-corner-btn:focus-visible,
.rc-mic:focus-visible,
.rc-model:focus-visible,
.rc-send:focus-visible,
.rc-tool:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px var(--sh-accent-ring);
}

.rc-name-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.rc-switch-wrap {
  position: relative;
  display: inline-flex;
}

.rc-switch {
  display: inline-grid;
  place-items: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 22%, transparent);
  border-radius: var(--sh-r-xs);
  background: color-mix(in srgb, var(--sh-media-bg) 34%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 74%, transparent);
  cursor: pointer;
  transition: border-color var(--sh-t) var(--sh-ease), color var(--sh-t) var(--sh-ease),
    background var(--sh-t) var(--sh-ease);
}

.rc-switch:hover,
.rc-switch[aria-expanded='true'] {
  border-color: color-mix(in srgb, var(--sh-accent) 56%, transparent);
  background: color-mix(in srgb, var(--sh-media-bg) 52%, transparent);
  color: var(--rc-ink);
}

.rc-roster {
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  z-index: 6;
  display: flex;
  width: max-content;
  min-width: 190px;
  max-width: 300px;
  flex-direction: column;
  padding: 6px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-md);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-2);
}

.rc-roster-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  border: 0;
  border-radius: var(--sh-r-2xs);
  background: none;
  color: var(--sh-text-2);
  font: inherit;
  font-size: 12.5px;
  text-align: left;
  cursor: pointer;
}

.rc-roster-row:hover {
  background: var(--sh-chip);
  color: var(--sh-text);
}

.rc-roster-row.is-active {
  color: var(--sh-accent);
}

.rc-roster-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rc-roster-row.is-add {
  margin-top: 4px;
  border-top: 1px solid var(--sh-line-soft);
  border-radius: 0 0 var(--sh-r-2xs) var(--sh-r-2xs);
  color: var(--sh-text-3);
}

.rc-menu-enter-active,
.rc-menu-leave-active {
  transition: opacity 0.16s var(--sh-ease), transform 0.18s var(--sh-ease);
}

.rc-menu-enter-from,
.rc-menu-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

/* ---- 右上角精简入口 ------------------------------------------------ */

.rc-corner-acts {
  position: absolute;
  top: 20px;
  right: 20px;
  z-index: 4;
  display: flex;
  align-items: center;
  gap: var(--sh-gap-2);
}

.rc-corner-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 34px;
  padding: 0 12px;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 18%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--sh-media-bg) 32%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 84%, transparent);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  opacity: 0.86;
  backdrop-filter: blur(12px) saturate(1.1);
  transition: opacity var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    border-color var(--sh-t) var(--sh-ease), color var(--sh-t) var(--sh-ease);
}

.rc-corner-btn.is-icon {
  width: 34px;
  padding: 0;
}

.rc-corner-btn:hover {
  opacity: 1;
  border-color: color-mix(in srgb, var(--sh-media-text) 30%, transparent);
  background: color-mix(in srgb, var(--sh-media-bg) 48%, transparent);
  color: var(--rc-ink);
}

.rc-corner-n {
  padding: 0 5px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--sh-media-text) 14%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 82%, transparent);
  font-size: 11px;
  line-height: 1.5;
  font-variant-numeric: tabular-nums;
}

/* ---- 居中悬浮输入栏（浅浅的对话框 + 语音 + 模型） ------------------ */

.rc-dock {
  position: absolute;
  bottom: 20px;
  left: calc((100% - var(--rc-history-w, 0px)) / 2);
  z-index: 4;
  display: flex;
  width: min(560px, calc(100% - var(--rc-history-w, 0px) - 36px));
  flex-direction: column;
  align-items: center;
  gap: var(--sh-gap-2);
  pointer-events: none;
  transform: translateX(-50%);
  transition: left var(--sh-t-slow) var(--sh-ease), width var(--sh-t-slow) var(--sh-ease);
}

.rc-dock-meta {
  display: flex;
  max-width: 100%;
  flex-direction: column;
  align-items: center;
  gap: var(--sh-gap-1);
}

.rc-dock-pill {
  display: inline-flex;
  max-width: 100%;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 14%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--sh-media-bg) 42%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 86%, transparent);
  font-size: 12px;
  line-height: 1.45;
  backdrop-filter: blur(12px);
  pointer-events: auto;
}

.rc-dock-pill span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rc-dock-pill.is-error {
  border-color: color-mix(in srgb, var(--sh-danger) 42%, transparent);
  background: color-mix(in srgb, var(--sh-media-bg) 56%, var(--sh-danger) 14%);
  color: var(--rc-danger);
}

.rc-dock-pill.is-live {
  border-color: color-mix(in srgb, var(--sh-accent) 42%, transparent);
  color: var(--rc-ink);
}

.rc-dock-live-dot {
  width: 6px;
  height: 6px;
  flex: none;
  border-radius: 50%;
  background: var(--sh-accent);
  animation: rc-mic-pulse 1.1s ease-in-out infinite;
}

.rc-dock-bar {
  display: flex;
  width: 100%;
  align-items: flex-end;
  gap: var(--sh-gap-2);
  padding: 6px 8px;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 16%, transparent);
  border-radius: 26px;
  background: color-mix(in srgb, var(--sh-media-bg) 26%, transparent);
  box-shadow:
    0 12px 34px color-mix(in srgb, var(--sh-media-bg) 24%, transparent),
    inset 0 1px 0 color-mix(in srgb, var(--sh-media-text) 14%, transparent);
  opacity: 0.82;
  pointer-events: auto;
  backdrop-filter: blur(14px) saturate(1.14);
  transition: opacity var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    border-color var(--sh-t) var(--sh-ease), box-shadow var(--sh-t) var(--sh-ease);
}

.rc-dock-bar:hover,
.rc-dock-bar:focus-within,
.rc-dock-bar.is-active {
  opacity: 1;
  border-color: color-mix(in srgb, var(--sh-media-text) 28%, transparent);
  background: color-mix(in srgb, var(--sh-media-bg) 44%, transparent);
  box-shadow:
    0 14px 38px color-mix(in srgb, var(--sh-media-bg) 34%, transparent),
    inset 0 1px 0 color-mix(in srgb, var(--sh-media-text) 20%, transparent);
}

.rc-dock-bar.is-listening {
  border-color: color-mix(in srgb, var(--sh-accent) 52%, transparent);
}

.rc-mic {
  display: grid;
  flex: none;
  place-items: center;
  width: 34px;
  height: 34px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 50%;
  background: color-mix(in srgb, var(--sh-media-text) 8%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 78%, transparent);
  cursor: pointer;
  transition: color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    border-color var(--sh-t) var(--sh-ease), box-shadow var(--sh-t) var(--sh-ease);
}

.rc-mic:hover:not(:disabled) {
  background: color-mix(in srgb, var(--sh-media-text) 16%, transparent);
  color: var(--rc-ink);
}

.rc-mic:disabled {
  cursor: not-allowed;
  opacity: 0.42;
}

.rc-mic.is-on {
  border-color: color-mix(in srgb, var(--sh-accent) 56%, transparent);
  background: color-mix(in srgb, var(--sh-accent) 24%, transparent);
  color: var(--rc-ink);
}

.rc-mic.is-hearing {
  background: color-mix(in srgb, var(--sh-accent) 36%, transparent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--sh-accent) 22%, transparent);
  color: var(--rc-ink);
}

.rc-mic.is-hearing :where(svg) {
  animation: rc-mic-pulse 1.1s ease-in-out infinite;
}

@keyframes rc-mic-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}

.rc-input {
  flex: 1;
  min-width: 0;
  min-height: 34px;
  max-height: 108px;
  padding: 6px 4px;
  border: 0;
  outline: none;
  background: transparent;
  color: var(--rc-ink);
  font: inherit;
  font-size: 14px;
  line-height: 1.55;
  resize: none;
  scrollbar-width: thin;
  text-shadow: 0 1px 12px color-mix(in srgb, var(--sh-media-bg) 78%, transparent);
}

.rc-input::placeholder {
  color: color-mix(in srgb, var(--sh-media-text) 56%, transparent);
}

.rc-send {
  display: grid;
  flex: none;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 1px solid transparent;
  border-radius: 50%;
  background: var(--sh-accent);
  color: var(--sh-accent-contrast);
  cursor: pointer;
  transition: filter var(--sh-t) var(--sh-ease), opacity var(--sh-t) var(--sh-ease),
    background var(--sh-t) var(--sh-ease);
}

.rc-send:hover:not(:disabled) {
  filter: brightness(1.06);
}

.rc-send:disabled {
  background: color-mix(in srgb, var(--sh-media-text) 14%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 46%, transparent);
  cursor: default;
}

/* 对话模型：收在输入栏右侧的一枚紧凑胶囊，向上展开。 */
.rc-model-wrap {
  position: relative;
  display: inline-flex;
  flex: none;
  align-items: center;
}

.rc-model {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 152px;
  height: 30px;
  margin-bottom: 2px;
  padding: 0 10px;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 12%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--sh-media-text) 7%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 76%, transparent);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
  transition: color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    border-color var(--sh-t) var(--sh-ease);
}

.rc-model:hover,
.rc-model.is-open {
  border-color: color-mix(in srgb, var(--sh-media-text) 24%, transparent);
  background: color-mix(in srgb, var(--sh-media-text) 14%, transparent);
  color: var(--rc-ink);
}

.rc-model.is-warn {
  border-color: color-mix(in srgb, var(--sh-warn) 38%, transparent);
  color: var(--rc-warm);
}

.rc-model-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rc-model-pop {
  position: absolute;
  right: 0;
  bottom: calc(100% + 12px);
  z-index: 6;
  display: flex;
  width: 280px;
  flex-direction: column;
  gap: var(--sh-gap-2);
  padding: 13px 14px 11px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-md);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-2);
}

.rc-model-pop .rc-note {
  margin: 0;
  color: var(--sh-text-3);
}

/* ---- 右侧精简历史抽屉 ---------------------------------------------- */

.rc-history {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 5;
  display: flex;
  width: clamp(260px, 26%, 340px);
  flex-direction: column;
  border-left: 1px solid color-mix(in srgb, var(--sh-media-text) 16%, transparent);
  background: color-mix(in srgb, var(--sh-media-bg) 34%, transparent);
  box-shadow: -12px 0 36px color-mix(in srgb, var(--sh-media-bg) 22%, transparent);
  backdrop-filter: blur(var(--sh-blur)) saturate(1.1);
}

.rc-history-enter-active,
.rc-history-leave-active {
  transition: transform 0.24s var(--sh-ease), opacity 0.2s var(--sh-ease);
}

.rc-history-enter-from,
.rc-history-leave-to {
  transform: translateX(100%);
  opacity: 0;
}

.rc-history-head {
  display: flex;
  flex: none;
  align-items: center;
  gap: var(--sh-gap-2);
  padding: 14px 12px 10px 16px;
  border-bottom: 1px solid color-mix(in srgb, var(--sh-media-text) 10%, transparent);
}

.rc-history-title {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: var(--sh-gap-2);
}

.rc-history-title h2 {
  margin: 0;
  color: var(--rc-ink);
  font-size: 13.5px;
  font-weight: 600;
  white-space: nowrap;
}

.rc-history-meta {
  color: var(--rc-sub);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.rc-history-acts {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: auto;
}

.rc-tool {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  height: 28px;
  padding: 0 7px;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: none;
  color: color-mix(in srgb, var(--sh-media-text) 74%, transparent);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
  transition: background var(--sh-t) var(--sh-ease), color var(--sh-t) var(--sh-ease);
}

.rc-tool:hover {
  background: color-mix(in srgb, var(--sh-media-text) 12%, transparent);
  color: var(--rc-ink);
}

.rc-tool.is-icon {
  width: 28px;
  padding: 0;
}

.rc-tool-n {
  color: color-mix(in srgb, var(--sh-media-text) 60%, transparent);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.rc-script {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 14px 18px 16px;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--sh-media-text) 22%, transparent) transparent;
}

.rc-script::-webkit-scrollbar {
  width: 6px;
}

.rc-script::-webkit-scrollbar-thumb {
  border-radius: 4px;
  background: color-mix(in srgb, var(--sh-media-text) 20%, transparent);
}

.rc-stamp {
  margin: 0 0 14px;
  color: color-mix(in srgb, var(--sh-media-text) 48%, transparent);
  font-size: 11px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.rc-line {
  margin: 0 0 14px;
  animation: rc-rise 0.3s var(--sh-ease) both;
}

.rc-line.user {
  padding-left: 10px;
  border-left: 2px solid color-mix(in srgb, var(--sh-accent) 46%, transparent);
}

@keyframes rc-rise {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.rc-who {
  display: block;
  margin-bottom: 3px;
  color: color-mix(in srgb, var(--sh-warn) 44%, var(--sh-media-text));
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.08em;
}

.rc-line.user .rc-who {
  color: color-mix(in srgb, var(--sh-accent) 40%, var(--sh-media-text));
}

.rc-line.is-error .rc-who {
  color: var(--rc-danger);
}

.rc-said {
  margin: 0;
  color: var(--rc-ink);
  font-size: 13.5px;
  line-height: 1.75;
  overflow-wrap: anywhere;
  text-wrap: pretty;
  white-space: pre-wrap;
}

.rc-line.user .rc-said {
  color: color-mix(in srgb, var(--sh-media-text) 88%, transparent);
  font-size: 13px;
}

.rc-line.is-error .rc-said {
  color: var(--rc-danger);
  font-size: 12.5px;
}

.rc-line.is-live .rc-said {
  color: color-mix(in srgb, var(--sh-media-text) 62%, transparent);
}

/* 正在念的那一句：字提亮到暖色。 */
.rc-line.is-playing .rc-said {
  color: var(--rc-warm);
}

.rc-line-acts {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 22px;
  margin-top: 4px;
  opacity: 0;
  transition: opacity var(--sh-t-fast) var(--sh-ease);
}

.rc-line:hover .rc-line-acts,
.rc-line:focus-within .rc-line-acts,
.rc-line.is-playing .rc-line-acts {
  opacity: 1;
}

.rc-mini {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 20%, transparent);
  border-radius: 50%;
  background: color-mix(in srgb, var(--sh-media-bg) 36%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 82%, transparent);
  cursor: pointer;
  transition: border-color var(--sh-t) var(--sh-ease), color var(--sh-t) var(--sh-ease);
}

.rc-mini:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--sh-accent) 60%, transparent);
  color: var(--rc-ink);
}

.rc-mini.is-on {
  border-color: color-mix(in srgb, var(--sh-warn) 56%, transparent);
  color: var(--rc-warm);
}

.rc-mini:disabled {
  cursor: progress;
  opacity: 0.55;
}

.rc-tag {
  color: color-mix(in srgb, var(--sh-media-text) 56%, transparent);
  font-size: 10.5px;
  cursor: default;
}

.rc-tag.is-warn {
  color: color-mix(in srgb, var(--sh-warn) 58%, var(--sh-media-text));
  cursor: help;
}

/* 三个点：她在想。 */
.rc-typing {
  display: flex;
  align-items: center;
  gap: 5px;
  height: 20px;
  margin: 0 0 14px;
}

.rc-typing i {
  display: block;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--sh-media-text) 74%, transparent);
  animation: rc-dot 1.2s infinite;
}

.rc-typing i:nth-child(2) {
  animation-delay: 0.16s;
}

.rc-typing i:nth-child(3) {
  animation-delay: 0.32s;
}

@keyframes rc-dot {
  0%,
  60%,
  100% {
    opacity: 0.3;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

.rc-blank {
  display: flex;
  height: 100%;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  animation: rc-rise 0.3s var(--sh-ease-out) both;
}

.rc-blank-line {
  margin: 0;
  color: color-mix(in srgb, var(--sh-media-text) 62%, transparent);
  font-size: 13px;
}

.rc-blank-sub {
  margin: 0;
  max-width: 34ch;
  color: color-mix(in srgb, var(--sh-media-text) 68%, transparent);
  font-size: 12.5px;
  line-height: 1.8;
}

.rc-blank.is-scene {
  flex: 1;
  min-height: 0;
  padding: 32px;
}

/* ---- 历史对话列表（与半透明历史抽屉统一） ---------------------------- */

.rc-toc-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px 10px 14px;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--sh-media-text) 22%, transparent) transparent;
}

.rc-toc-row {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 4px;
  border: 1px solid transparent;
  border-radius: var(--sh-r-sm);
  transition: background var(--sh-t) var(--sh-ease), border-color var(--sh-t) var(--sh-ease);
}

.rc-toc-row:hover {
  background: color-mix(in srgb, var(--sh-media-text) 10%, transparent);
}

.rc-toc-row.is-active {
  border-color: color-mix(in srgb, var(--sh-accent) 32%, transparent);
  background: color-mix(in srgb, var(--sh-accent) 18%, transparent);
}

.rc-toc-main {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
  padding: 9px 6px 9px 10px;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.rc-toc-title {
  overflow: hidden;
  color: var(--rc-ink);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rc-toc-row.is-active .rc-toc-title {
  color: var(--rc-ink);
  font-weight: 600;
}

.rc-toc-sub {
  color: var(--rc-sub);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.rc-toc-del {
  display: grid;
  flex: none;
  place-items: center;
  width: 26px;
  height: 26px;
  margin-right: 4px;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: none;
  color: color-mix(in srgb, var(--sh-media-text) 64%, transparent);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    color var(--sh-t) var(--sh-ease);
}

.rc-toc-row:hover .rc-toc-del,
.rc-toc-row:focus-within .rc-toc-del {
  opacity: 1;
}

.rc-toc-del:hover {
  background: color-mix(in srgb, var(--sh-danger) 24%, transparent);
  color: var(--rc-danger);
}

.rc-toc-del:disabled {
  cursor: default;
  opacity: 0.3;
}

.rc-toc-del:disabled:hover {
  background: none;
  color: color-mix(in srgb, var(--sh-media-text) 50%, transparent);
}

.rc-toc-confirm {
  display: flex;
  flex: none;
  align-items: center;
  gap: 4px;
  padding-right: 6px;
  color: var(--rc-sub);
  font-size: 11px;
}

.rc-toc-confirm button {
  height: 24px;
  padding: 0 7px;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 20%, transparent);
  border-radius: var(--sh-r-2xs);
  background: color-mix(in srgb, var(--sh-media-bg) 42%, transparent);
  color: var(--rc-ink);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.rc-toc-confirm .rc-danger {
  border-color: color-mix(in srgb, var(--sh-danger) 48%, transparent);
  background: color-mix(in srgb, var(--sh-danger) 24%, transparent);
  color: var(--rc-danger);
}

.rc-toc-confirm button:disabled {
  cursor: default;
  opacity: 0.45;
}

/* ---- 角色设置（从名牌处铺开） -------------------------------------- */

.rc-scrim {
  position: absolute;
  inset: 0;
  z-index: 6;
  border: 0;
  background: color-mix(in srgb, var(--sh-media-bg) 34%, transparent);
  cursor: default;
}

.rc-sheet {
  position: absolute;
  top: 22px;
  left: 22px;
  z-index: 7;
  display: flex;
  width: min(372px, calc(100% - 44px));
  max-height: calc(100% - 44px);
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-lg);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-2);
}

.rc-sheet-head {
  display: flex;
  flex: none;
  align-items: center;
  gap: 9px;
  padding: 14px 12px 10px 18px;
}

.rc-sheet-head h2 {
  margin: 0;
  color: var(--sh-text);
  font-size: 12.5px;
  font-weight: 600;
}

.rc-save {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-left: auto;
  color: var(--sh-text-4);
  font-size: 11px;
}

.rc-save b {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--sh-ok);
}

.rc-save.is-busy b {
  background: var(--sh-warn);
}

.rc-sheet-close {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: none;
  color: var(--sh-text-4);
  cursor: pointer;
}

.rc-sheet-close:hover {
  background: var(--sh-chip);
  color: var(--sh-text);
}

.rc-sheet-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 2px 18px 6px;
  scrollbar-width: thin;
}

.rc-field {
  display: block;
  margin-bottom: var(--sh-gap-3);
}

.rc-field.is-flat {
  margin-bottom: 0;
}

.rc-field > span {
  display: block;
  margin-bottom: 5px;
  color: var(--sh-text-3);
  font-size: 11.5px;
}

/* 字段标题行：左边是字段名，右边挂一颗小动作（人设的 AI 生成、样例条数）。 */
.rc-field-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 5px;
}

.rc-field-head > span {
  color: var(--sh-text-3);
  font-size: 11.5px;
}

.rc-ai {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 4px;
  height: 21px;
  padding: 0 8px;
  border: 1px solid var(--sh-line);
  border-radius: 999px;
  background: var(--sh-field);
  color: var(--sh-text-3);
  font: inherit;
  font-size: 10.5px;
  cursor: pointer;
  transition: border-color var(--sh-t-fast) var(--sh-ease), color var(--sh-t-fast) var(--sh-ease);
}

.rc-ai:hover:not(:disabled) {
  border-color: var(--sh-accent-ring);
  color: var(--sh-accent);
}

.rc-ai:disabled {
  cursor: default;
  opacity: 0.55;
}

/* 试听 / 清除音色 */
.rc-voice-play,
.rc-voice-clear {
  display: grid;
  flex: none;
  place-items: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--sh-line);
  border-radius: 50%;
  background: var(--sh-field);
  color: var(--sh-text-3);
  cursor: pointer;
  transition: border-color var(--sh-t-fast) var(--sh-ease), color var(--sh-t-fast) var(--sh-ease);
}

.rc-voice-play:hover,
.rc-voice-play.is-on {
  border-color: var(--sh-accent);
  color: var(--sh-accent);
}

.rc-voice-clear:hover {
  border-color: color-mix(in srgb, var(--sh-danger) 40%, transparent);
  color: var(--sh-danger);
}

.rc-voice-picker {
  position: relative;
  z-index: 4;
}

.rc-voice-picker :deep(.vpick-input) {
  width: 100%;
  height: 36px;
  box-sizing: border-box;
  padding: 0 32px 0 11px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  background: var(--sh-field);
  color: var(--sh-text);
  font: inherit;
  font-size: 12.8px;
  transition: border-color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    box-shadow var(--sh-t) var(--sh-ease);
}

.rc-voice-picker :deep(.vpick-input::placeholder) {
  color: var(--sh-text-4);
}

.rc-voice-picker :deep(.vpick-input:hover) {
  border-color: color-mix(in srgb, var(--sh-accent) 34%, var(--sh-line));
}

.rc-voice-picker :deep(.vpick-input:focus) {
  outline: none;
  border-color: var(--sh-accent);
  background: var(--sh-field-focus);
  box-shadow: 0 0 0 3px var(--sh-accent-ring);
}

.rc-voice-picker :deep(.combo-caret) {
  right: 4px;
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: var(--sh-r-xs);
  color: var(--sh-text-4);
  font-size: 11px;
}

.rc-voice-picker :deep(.combo-caret:hover) {
  background: var(--sh-chip);
  color: var(--sh-text);
}

.rc-voice-picker :deep(.vpick-menu) {
  padding: 6px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-md);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-2);
}

.rc-voice-picker :deep(.vpick-row) {
  padding: 7px 9px;
  border-radius: var(--sh-r-2xs);
  color: var(--sh-text-2);
  font-size: 12.5px;
}

.rc-voice-picker :deep(.vpick-row:hover),
.rc-voice-picker :deep(.vpick-row.hot) {
  background: var(--sh-chip);
  color: var(--sh-text);
}

.rc-voice-picker :deep(.vpick-row.on) {
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
}

.rc-voice-picker :deep(.vpick-src),
.rc-voice-picker :deep(.vpick-note) {
  color: var(--sh-text-4);
}

.rc-voice-picker :deep(.vpick-foot) {
  border-top-color: var(--sh-line-soft);
}

/* 样例：只占一行下拉，样例再多也不把这张纸拉长。 */
.rc-sample-head {
  margin-top: 10px;
}

.rc-sample-count {
  color: var(--sh-text-4);
  font-size: 10.5px;
}

.rc-sample-pick {
  margin-top: 8px;
}

.rc-intro-in {
  min-height: 46px;
  resize: vertical;
  line-height: 1.65;
}

.rc-persona-in {
  min-height: 82px;
  resize: vertical;
  line-height: 1.65;
}

.rc-sheet-sec {
  padding: 12px 0;
  border-top: 1px solid var(--sh-line-soft);
}

.rc-sheet-sec h3 {
  margin: 0 0 9px;
  color: var(--sh-text-3);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.06em;
}

.rc-voice {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: var(--sh-gap-3);
}

.rc-voice-avatar {
  display: grid;
  flex: none;
  place-items: center;
  width: 38px;
  height: 38px;
  overflow: hidden;
  border: 1px solid var(--sh-line);
  border-radius: 50%;
  background: var(--sh-accent-soft);
  color: var(--sh-accent);
  font-size: 15px;
  font-weight: 600;
}

.rc-voice-main {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}

.rc-voice-main strong {
  overflow: hidden;
  color: var(--sh-text);
  font-size: 12.5px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 分区标题行：标题在左，动作（复位 / 自定义背景）在右。 */
.rc-sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 9px;
}

.rc-sec-head h3 {
  margin: 0;
}

.rc-mini-btn {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 8px;
  border: 1px solid var(--sh-line);
  border-radius: 999px;
  background: var(--sh-field);
  color: var(--sh-text-3);
  font: inherit;
  font-size: 10.5px;
  cursor: pointer;
  transition: border-color var(--sh-t-fast) var(--sh-ease), color var(--sh-t-fast) var(--sh-ease);
}

.rc-mini-btn:hover {
  border-color: var(--sh-accent-ring);
  color: var(--sh-accent);
}

/* 合成引擎：只占一行——下拉左边一颗绿点表示"现在这条真有实例在跑"。 */
.rc-engine-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rc-engine-row .sh-field {
  min-width: 0;
  flex: 1;
}

.rc-engine-dot {
  width: 6px;
  height: 6px;
  flex: none;
  border-radius: 50%;
  background: var(--sh-line);
}

.rc-engine-dot.is-ok {
  background: var(--sh-ok);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--sh-ok) 20%, transparent);
}

/* 四张内置图：点一下就换，选中那张描一道强调色。 */
.rc-bg-picks {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin: 8px 0;
}

.rc-bg-pick {
  position: relative;
  height: 62px;
  overflow: hidden;
  padding: 0;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  background-color: var(--sh-media-bg);
  background-position: center;
  background-size: cover;
  color: var(--sh-media-text);
  cursor: pointer;
  transition: border-color var(--sh-t) var(--sh-ease), box-shadow var(--sh-t) var(--sh-ease);
}

.rc-bg-pick span {
  position: absolute;
  inset: auto 0 0;
  padding: 14px 8px 5px;
  background: linear-gradient(180deg, transparent, color-mix(in srgb, var(--sh-media-bg) 68%, transparent));
  font-size: 11px;
  text-align: left;
}

.rc-bg-pick:hover {
  border-color: var(--sh-accent-ring);
}

.rc-bg-pick.is-active {
  border-color: var(--sh-accent);
  box-shadow: inset 0 0 0 1px var(--sh-accent);
}

/* 立绘：十一张加「不站人」按宽度自动分行，宽的时候一行六个、两行收工。
   窄档不写死列数——格子最小 72px，排不下就换行，别把名字挤成一条缝。 */
.rc-art-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
  gap: 6px;
  margin-bottom: 8px;
}

.rc-art {
  position: relative;
  height: 76px;
  overflow: hidden;
  padding: 0;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-sm);
  background: color-mix(in srgb, var(--sh-media-bg) 90%, var(--sh-text) 10%);
  cursor: pointer;
  transition: border-color var(--sh-t) var(--sh-ease), box-shadow var(--sh-t) var(--sh-ease);
}

.rc-art img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: top center;
}

.rc-art span {
  position: absolute;
  inset: auto 0 0;
  padding: 12px 3px 3px;
  background: linear-gradient(180deg, transparent, color-mix(in srgb, var(--sh-media-bg) 74%, transparent));
  color: var(--sh-media-text);
  font-size: 10px;
  line-height: 1.2;
  text-align: center;
}

.rc-art:hover {
  border-color: var(--sh-accent-ring);
}

.rc-art.is-active {
  border-color: var(--sh-accent);
  box-shadow: inset 0 0 0 1px var(--sh-accent);
}

.rc-art.is-none {
  display: grid;
  place-items: center;
  padding: 0 6px;
  color: var(--sh-text-3);
  font: inherit;
  font-size: 11px;
  line-height: 1.4;
  text-align: center;
}

.rc-upload {
  position: relative;
  overflow: hidden;
  cursor: pointer;
}

.rc-upload input {
  position: absolute;
  inset: 0;
  width: 100%;
  opacity: 0;
  cursor: pointer;
}

.rc-upload:focus-within {
  outline: 2px solid var(--sh-accent-ring);
  outline-offset: 2px;
}

.rc-upload.is-busy {
  cursor: progress;
  opacity: 0.7;
}

.rc-note {
  margin: 6px 0 0;
  color: var(--sh-text-4);
  font-size: 11.5px;
  line-height: 1.55;
}

.rc-note.is-warn {
  color: var(--sh-warn);
}

.rc-sheet-foot {
  display: flex;
  flex: none;
  align-items: center;
  gap: 10px;
  padding: 10px 14px 12px 18px;
  border-top: 1px solid var(--sh-line-soft);
}

.rc-sheet-del {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  margin-left: auto;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: var(--sh-r-xs);
  background: none;
  color: var(--sh-text-3);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}

.rc-sheet-del:hover:not(:disabled) {
  border-color: color-mix(in srgb, var(--sh-danger) 36%, transparent);
  background: var(--sh-danger-soft);
  color: var(--sh-danger);
}

.rc-sheet-del:disabled {
  cursor: default;
  opacity: 0.45;
}

.rc-sheet-enter-active,
.rc-sheet-leave-active {
  transition: opacity 0.18s var(--sh-ease), transform 0.22s var(--sh-ease);
}

.rc-sheet-enter-from,
.rc-sheet-leave-to {
  opacity: 0;
  transform: translateY(8px) scale(0.99);
}

.rc-spin {
  animation: rc-spin 0.9s linear infinite;
}

@keyframes rc-spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---- 房间宽度 ------------------------------------------------------ */
/* 这一页夹在导航、分类栏与音色列之后：按房间自己的宽度（container-name: room）排版。 */

@container room (max-width: 960px) {
  .rc-plate {
    top: 22px;
    left: 22px;
    width: min(260px, 42%);
  }

  .rc-name {
    font-size: 30px;
  }

  .rc-history {
    width: clamp(248px, 32%, 310px);
  }
}

/* 较窄档：历史抽屉以轻浮层盖在右侧，模型胶囊收成纯图标，把宽度全让给输入框。 */
@container room (max-width: 640px) {
  .rc-plate {
    top: 16px;
    left: 16px;
    width: min(220px, 56%);
  }

  .rc-name {
    font-size: 24px;
  }

  .rc-intro {
    display: none;
  }

  .rc-corner-acts {
    top: 14px;
    right: 14px;
  }

  .rc-corner-label {
    display: none;
  }

  .rc-dock {
    bottom: 14px;
    width: calc(100% - 28px);
  }

  .rc-model-name {
    display: none;
  }

  .rc-model {
    width: 30px;
    padding: 0;
    justify-content: center;
  }

  .rc-model-pop {
    right: -40px;
    width: min(270px, calc(100vw - 40px));
  }

  .rc-history {
    width: min(300px, 86%);
    box-shadow: var(--sh-shadow-2);
  }

  .rc-sheet {
    top: 10px;
    left: 10px;
    width: calc(100% - 20px);
    max-height: calc(100% - 20px);
  }
}

@container room (min-width: 1400px) {
  .rc-plate {
    top: 38px;
    left: 38px;
    width: 340px;
  }

  .rc-name {
    font-size: 44px;
  }

  .rc-intro {
    max-width: 28ch;
    font-size: 13px;
  }

  .rc-dock {
    bottom: 26px;
    width: min(600px, calc(100% - var(--rc-history-w, 0px) - 48px));
  }
}

/* 手机：壳已经把导航横到顶上，这里只把留白与圆角收掉。 */
@media (max-width: 760px) {
  .rc {
    padding: 0;
  }

  .rc-scene {
    border-radius: 0;
    border-left: 0;
    border-right: 0;
  }

  .rc-sheet {
    top: 8px;
    left: 8px;
    width: calc(100% - 16px);
    max-height: calc(100% - 16px);
  }
}

/* 全屏：房间铺满整块屏，记录与设置照旧压在它上面。 */
.rc.is-fullscreen,
.rc:fullscreen {
  padding: 0;
}

.rc.is-fullscreen .rc-scene,
.rc:fullscreen .rc-scene {
  border-radius: 0;
  border: 0;
}

.rc.is-fullscreen .rc-plate,
.rc:fullscreen .rc-plate {
  top: 40px;
  left: 48px;
  width: 360px;
}

.rc.is-fullscreen .rc-name,
.rc:fullscreen .rc-name {
  font-size: 48px;
}

.rc.is-fullscreen .rc-dock,
.rc:fullscreen .rc-dock {
  bottom: 30px;
}
</style>
