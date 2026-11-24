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
    <!-- 房间：一张场景铺满工作区，她的话直接写在画面上，不再是一格一格的气泡。 -->
    <div
      ref="sceneEl"
      class="rc-scene has-image"
      :class="`is-bg-${backgroundKey || 'custom'}`"
      :style="[stageStyle, figureVars]"
    >
      <div class="rc-shade" aria-hidden="true" />
      <!-- 立绘：透明底，站在偏左的位置，人压在暗化之上，右边的台本仍比她高一层。
           画面上直接拖动换位置、滚轮改大小，位置与大小跟着角色保存。 -->
      <img
        v-if="portraitImage"
        class="rc-figure"
        :class="{ 'is-dragging': Boolean(portraitDrag) }"
        :src="portraitImage"
        :style="figureStyle"
        alt=""
        aria-hidden="true"
        title="拖动换位置，滚轮调大小"
        @pointerdown="startPortraitDrag"
        @wheel.prevent="onPortraitWheel"
      />

      <!-- 正在说的那一句落在她旁边：说完就散，画面回到干净的状态。 -->
      <Transition name="rc-bubble">
        <div v-if="bubbleText" class="rc-bubble" role="status">
          <p class="rc-bubble-text">{{ bubbleText }}</p>
        </div>
      </Transition>
      <header v-if="char" class="rc-plate" title="滚轮切换角色" @wheel.prevent="onPlateWheel">
        <div class="rc-kicker"><b aria-hidden="true" />角色</div>
        <div class="rc-name-row">
          <h1 class="rc-name">{{ char.name }}</h1>
          <!-- 换人不铺一排名字：当前角色名旁边一颗键，点开才列。 -->
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
              <ChevronsUpDown :size="15" :stroke-width="1.8" />
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
                  <Check v-if="item.id === char.id" :size="13" :stroke-width="1.8" />
                </button>
                <button type="button" class="rc-roster-row is-add" role="menuitem" @click="onAddCharacter">
                  <Plus :size="13" :stroke-width="1.8" />
                  新建角色
                </button>
              </div>
            </Transition>
          </span>
        </div>
        <p class="rc-intro">{{ introLine(char) }}</p>
        <div class="rc-plate-acts">
          <button
            type="button"
            class="rc-chip"
            aria-haspopup="dialog"
            :aria-expanded="sheetOpen"
            @click="openSheet"
          >
            <PenLine :size="13" :stroke-width="1.8" aria-hidden="true" />
            编辑角色
          </button>
          <button
            type="button"
            class="rc-chip"
            :class="{ 'is-quiet': !char.voice_id }"
            :title="cellVoiceTitle"
            aria-haspopup="dialog"
            :aria-expanded="sheetOpen"
            @click="openSheet"
          >
            <span v-if="state.speakingId" class="rc-bars" aria-hidden="true">
              <i /><i /><i />
            </span>
            <Volume2 v-else :size="13" :stroke-width="1.8" aria-hidden="true" />
            {{ char.voice_id ? char.voice_name : '未指定音色' }}
          </button>
        </div>
      </header>

      <section v-if="char && !readHidden" class="rc-read">
        <div class="rc-frost" aria-hidden="true" />

        <header class="rc-read-head">
          <div class="rc-read-title">
            <h2>{{ sessionTitle() }}</h2>
            <span class="rc-read-meta">
              {{ sessionDate(current?.created) }} · {{ sessionCount() }} 条
            </span>
          </div>
          <div class="rc-read-acts">
            <button
              type="button"
              class="rc-tool"
              :title="`记录：共 ${sessions.length} 份`"
              aria-haspopup="dialog"
              :aria-expanded="tocOpen"
              @click="openToc"
            >
              <List :size="14" :stroke-width="1.8" aria-hidden="true" />
              <span class="rc-tool-label">记录</span>
              <span class="rc-tool-n">{{ sessions.length }}</span>
            </button>
            <button
              type="button"
              class="rc-tool is-icon"
              title="新的一份记录"
              aria-label="新的一份记录"
              @click="onNewSession"
            >
              <Plus :size="15" :stroke-width="1.8" />
            </button>
            <button
              type="button"
              class="rc-tool is-icon"
              title="收起对话，只看画面"
              aria-label="收起对话"
              @click="hideRead"
            >
              <PanelRightClose :size="15" :stroke-width="1.8" />
            </button>
            <button
              type="button"
              class="rc-tool is-icon"
              :title="isFullscreen ? '退出全屏' : '全屏'"
              :aria-label="isFullscreen ? '退出全屏' : '全屏'"
              @click="toggleFullscreen"
            >
              <Minimize2 v-if="isFullscreen" :size="15" :stroke-width="1.8" />
              <Maximize2 v-else :size="15" :stroke-width="1.8" />
            </button>
          </div>
        </header>

        <div ref="chatEl" class="rc-script">
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
                  <Play :size="9" :stroke-width="1.8" fill="currentColor" />
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
                  <Loader2 v-if="message.id === state.synthesizingId" class="rc-spin" :size="10" />
                  <Volume2 v-else :size="10" :stroke-width="1.8" />
                </button>
                <span class="rc-tag">
                  {{ message.audio_url ? '已合成' : state.synthesizingId === message.id ? '合成中' : '没有语音' }}
                </span>
              </div>
              <span
                v-else-if="message.audio_error"
                class="rc-tag is-warn"
                :title="message.audio_error"
              >
                这一句没有语音
              </span>
            </div>

            <div v-if="state.busy === 'thinking'" class="rc-typing" role="status">
              <i /><i /><i />
            </div>
          </template>

          <div v-else class="rc-blank">
            <p class="rc-blank-line">还没有开始。</p>
            <p class="rc-blank-sub">写一句，{{ char.name }} 就接住了。</p>
          </div>

          <div v-if="state.liveText" class="rc-line user is-live">
            <span class="rc-who">你</span>
            <p class="rc-said">{{ state.liveText }}</p>
          </div>
        </div>

        <div class="rc-compose">
          <div v-if="state.chatError" class="rc-inline-error">
            <AlertCircle :size="13" :stroke-width="1.8" aria-hidden="true" />
            {{ state.chatError }}
          </div>
          <div class="rc-compose-in">
            <textarea
              ref="inputEl"
              v-model="state.draft"
              class="rc-input"
              rows="1"
              maxlength="1000"
              placeholder="写一句，或按住麦克风说"
              aria-label="写一句，或按住麦克风说"
              @keydown.enter.exact="onEnter"
              @input="autosize"
            />
            <button
              type="button"
              class="rc-send"
              :disabled="!canSend"
              :title="canSend ? '发送（Enter）' : '先写一句'"
              aria-label="发送"
              @click="sendDraft"
            >
              <Loader2 v-if="state.busy === 'thinking'" class="rc-spin" :size="15" />
              <ArrowUp v-else :size="16" :stroke-width="2" />
            </button>
          </div>
          <div class="rc-compose-foot">
            <button
              type="button"
              class="rc-mic"
              :class="{ 'is-on': state.listening === 'listening', 'is-hearing': state.micSpeaking }"
              :disabled="!hostStore.asr?.ready || state.listening === 'connecting'"
              :title="listenTitle"
              @click="listenToggle"
            >
              <Loader2
                v-if="state.listening === 'connecting'"
                class="rc-spin"
                :size="13"
              />
              <Mic v-else :size="13" :stroke-width="1.8" />
              {{ state.listening === 'listening' ? '停止对话' : '对话' }}
            </button>
            <span class="rc-hint">{{ busyHint }}</span>
            <!-- 对话模型挂在整份状态上，就在输入框旁边选：换角色不用再翻设置。 -->
            <span ref="modelWrapEl" class="rc-model-wrap">
              <button
                type="button"
                class="rc-model"
                :class="{ 'is-open': modelOpen, 'is-warn': Boolean(providerNote()) }"
                title="对话模型"
                aria-haspopup="dialog"
                :aria-expanded="modelOpen"
                @click="toggleModel"
              >
                <Cpu :size="12" :stroke-width="1.8" aria-hidden="true" />
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
            <span class="rc-key">Enter 发送</span>
          </div>
        </div>

        <!-- 记录：压在台本这一层上，她的名字与人设仍露在外面。 -->
        <Transition name="rc-toc">
          <div v-if="tocOpen" class="rc-toc" aria-label="聊天记录">
            <header class="rc-toc-head">
              <h2>记录</h2>
              <span class="rc-toc-count">{{ sessions.length }} 份</span>
              <button
                type="button"
                class="rc-tool is-icon"
                title="收起"
                aria-label="收起记录"
                @click="tocOpen = false"
              >
                <X :size="15" :stroke-width="1.8" />
              </button>
            </header>
            <div class="rc-toc-body">
              <div
                v-for="row in orderedSessions"
                :key="row.id"
                class="rc-toc-row"
                :class="{ 'is-active': row.id === current?.id }"
              >
                <button type="button" class="rc-toc-main" @click="openSession(row.id)">
                  <span class="rc-toc-date">{{ sessionDate(row.created) }}</span>
                  <span class="rc-toc-title">{{ sessionTitle(row) }}</span>
                  <span class="rc-toc-n">{{ sessionCount(row) }} 条</span>
                </button>
                <div v-if="confirmId === row.id" class="rc-toc-confirm">
                  <span>删掉这一份？</span>
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
                  title="删除这一份记录"
                  aria-label="删除这一份记录"
                  @click="confirmId = row.id"
                >
                  <Trash2 :size="13" :stroke-width="1.8" />
                </button>
              </div>
            </div>
            <footer class="rc-toc-foot">
              <button type="button" class="sh-btn is-soft is-sm" @click="onNewSession">
                <Plus :size="13" :stroke-width="1.8" />
                新的一份记录
              </button>
            </footer>
          </div>
        </Transition>
      </section>

      <!-- 对话收起来以后：右边只留一枚轻量的「<」把台本叫回来。 -->
      <button
        v-if="char && readHidden"
        type="button"
        class="rc-read-open"
        title="展开对话"
        aria-label="展开对话"
        @click="showRead"
      >
        <ChevronLeft :size="20" :stroke-width="1.6" aria-hidden="true" />
      </button>

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
        <aside v-if="sheetOpen && char" class="rc-sheet" role="dialog" aria-label="角色设置">
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
                    : '删除这个角色与它的全部记录'
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
  ArrowUp,
  Check,
  ChevronLeft,
  ChevronsUpDown,
  Cpu,
  ImageUp,
  List,
  Loader2,
  Maximize2,
  Mic,
  Minimize2,
  PanelRightClose,
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
import {
  active,
  addCharacter,
  BACKGROUNDS,
  backgroundOf,
  backgroundStyle,
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

const rootEl = ref(/** @type {any} */ (null))
const sceneEl = ref(/** @type {any} */ (null))
const chatEl = ref(/** @type {any} */ (null))
const inputEl = ref(/** @type {any} */ (null))
const isFullscreen = ref(false)
/** 台本可以整块收起来，只留画面；这一页里不落盘，翻页回来再展开。 */
const readHidden = ref(false)
/** 角色设置由插件状态管：音色卡「创建」在浮层里把它打开，工作区这边跟着出现。 */
const sheetOpen = computed({
  get: () => state.sheetOpen,
  set: (value) => {
    state.sheetOpen = value
  },
})
const tocOpen = ref(false)
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

/** 气泡要跟着人走：按立绘的站位与大小算出"她旁边"在哪儿，落成一个百分比。 */
const figureVars = computed(() => {
  const x = (char.value?.portrait_x ?? PORTRAIT_X) * 100
  const scale = char.value?.portrait_scale ?? PORTRAIT_SCALE
  return { '--rc-bubble-left': `${x + 26 + 8 * scale}%` }
})

/** 正在放的那一句：气泡只在她说话时出现，播放一结束（speakingId 清空）就散。 */
const bubbleText = computed(() => {
  if (!state.speakingId) return ''
  const line = messages.value.find((row) => row.id === state.speakingId)
  if (!line || line.role !== 'assistant') return ''
  return String(line.text || '').trim()
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

/** 名牌第二枚胶囊的悬停说明：说清"这个声音现在能不能出声"。 */
const cellVoiceTitle = computed(() =>
  char.value?.voice_id ? '这条音色会在每句回复后念出来' : '还没有音色：用左栏音色卡右端「创建」新建一个角色'
)

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
  tocOpen.value = false
  // 从名牌打开的是"改这个角色"，不是刚才那次新建。
  state.sheetNew = false
  state.personaError = ''
  sheetOpen.value = true
}

/** 收起台本：先关掉记录抽屉，别让它留在一个看不见的层里。 */
function hideRead() {
  tocOpen.value = false
  readHidden.value = true
}

function showRead() {
  readHidden.value = false
  nextTick(() => inputEl.value?.focus())
}

function openToc() {
  sheetOpen.value = false
  confirmId.value = ''
  tocOpen.value = true
}

function onNewSession() {
  newSession()
  tocOpen.value = false
  confirmId.value = ''
  nextTick(() => inputEl.value?.focus())
}

/** @param {string} id */
function openSession(id) {
  setSession(id)
  tocOpen.value = false
  confirmId.value = ''
  nextTick(() => {
    scrollToBottom()
    inputEl.value?.focus()
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
  if (!window.confirm(`删除角色「${char.value.name}」和它的全部记录？`)) return
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

/** 打开角色设置（或换了角色 / 换了音色）就重取一次样例：固定样例那一档要用它列清单。 */
watch(
  () => [sheetOpen.value, char.value?.voice_id],
  ([open, id]) => {
    if (!open || !id) return
    void loadVoiceSamples(true)
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

/** @param {KeyboardEvent} event */
function onKeydown(event) {
  if (event.key !== 'Escape') return
  if (sheetOpen.value) {
    sheetOpen.value = false
    return
  }
  if (tocOpen.value) tocOpen.value = false
}

onMounted(() => {
  void nextTick(scrollToBottom)
  document.addEventListener('fullscreenchange', syncFullscreen)
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('pointerdown', onDocPointer)
})

onBeforeUnmount(() => {
  document.removeEventListener('fullscreenchange', syncFullscreen)
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('pointerdown', onDocPointer)
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

/* 底色压暗按设计稿走：左边几乎不动（她的画面是全的），越往右越暗——字落在那一侧。
   只压到"字读得出来"为止：右侧再重就成了黑幕，房间的材质全没了。 */
.rc-shade {
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    linear-gradient(
      90deg,
      color-mix(in srgb, var(--sh-media-bg) 34%, transparent) 0%,
      color-mix(in srgb, var(--sh-media-bg) 12%, transparent) 16%,
      transparent 34%,
      color-mix(in srgb, var(--sh-media-bg) 12%, transparent) 48%,
      color-mix(in srgb, var(--sh-media-bg) 30%, transparent) 62%,
      color-mix(in srgb, var(--sh-media-bg) 46%, transparent) 78%,
      color-mix(in srgb, var(--sh-media-bg) 60%, transparent) 100%
    ),
    linear-gradient(
      0deg,
      color-mix(in srgb, var(--sh-media-bg) 52%, transparent) 0%,
      transparent 30%
    ),
    linear-gradient(
      180deg,
      color-mix(in srgb, var(--sh-media-bg) 36%, transparent) 0%,
      transparent 22%
    );
}

/* 房间底：永远是两张内置图之一（或用户自己传的那张），不再有"没设图就一块纯黑"的档。 */

/* ---- 立绘 ---------------------------------------------------------- */

/* 人站在左边：透明底、脚贴地面，投一层影让它落在房间里而不是浮在上面。
   位置与大小由角色状态给（可拖动、可滚轮缩放），这里只留兜底值。 */
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
  filter: drop-shadow(0 18px 30px rgb(0 0 0 / 42%));
}

.rc-figure.is-dragging {
  cursor: grabbing;
}

/* ---- 气泡 ---------------------------------------------------------- */
/* 她说话时，这一句落在人旁边：薄薄一层雾面，说完就散。
   不铺实心底色——背景仍看得见，才像话真的飘在房间里。 */
.rc-bubble {
  position: absolute;
  z-index: 2;
  /* 跟着立绘的站位走：人往右站，气泡也往右让，始终停在她身侧。 */
  left: clamp(170px, var(--rc-bubble-left, 36%), 48%);
  bottom: clamp(150px, 60%, 520px);
  min-width: 240px;
  max-width: min(26%, 360px);
  padding: 15px 18px 16px;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 20%, transparent);
  border-radius: 20px 22px 22px 6px;
  background:
    linear-gradient(
      150deg,
      color-mix(in srgb, var(--sh-media-text) 16%, transparent) 0%,
      color-mix(in srgb, var(--sh-media-text) 4%, transparent) 48%,
      transparent 100%
    ),
    color-mix(in srgb, var(--sh-media-bg) 30%, transparent);
  box-shadow:
    0 22px 60px color-mix(in srgb, var(--sh-media-bg) 40%, transparent),
    inset 0 1px 0 color-mix(in srgb, var(--sh-media-text) 16%, transparent);
  color: var(--rc-ink);
  pointer-events: none;
  backdrop-filter: blur(16px) saturate(1.12);
}

/* 朝她那一侧的尖角：一小块同材质的雾面，不是实心三角。 */
.rc-bubble::after {
  position: absolute;
  bottom: 16px;
  left: -7px;
  width: 14px;
  height: 14px;
  border-bottom: 1px solid color-mix(in srgb, var(--sh-media-text) 18%, transparent);
  border-left: 1px solid color-mix(in srgb, var(--sh-media-text) 18%, transparent);
  border-radius: 0 0 0 4px;
  background: color-mix(in srgb, var(--sh-media-bg) 26%, transparent);
  content: '';
  transform: rotate(45deg);
}

.rc-bubble-text {
  display: -webkit-box;
  overflow: hidden;
  margin: 0;
  font-size: 15px;
  line-height: 1.9;
  letter-spacing: 0.01em;
  overflow-wrap: anywhere;
  text-shadow: 0 1px 16px color-mix(in srgb, var(--sh-media-bg) 70%, transparent);
  text-wrap: pretty;
  white-space: pre-wrap;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 9;
}

.rc-bubble-enter-active {
  transition: opacity 0.3s var(--sh-ease), transform 0.4s var(--sh-ease);
}

.rc-bubble-leave-active {
  transition: opacity 0.24s var(--sh-ease), transform 0.24s var(--sh-ease);
}

.rc-bubble-enter-from,
.rc-bubble-leave-to {
  opacity: 0;
  transform: translateY(10px) scale(0.985);
}

/* 晨光那张整体偏亮：右半再加一道暗化，白字才压得住（比夜场轻一档）。 */
.is-bg-dawn .rc-shade {
  background:
    linear-gradient(
      90deg,
      color-mix(in srgb, var(--sh-media-bg) 30%, transparent) 0%,
      color-mix(in srgb, var(--sh-media-bg) 14%, transparent) 18%,
      color-mix(in srgb, var(--sh-media-bg) 24%, transparent) 38%,
      color-mix(in srgb, var(--sh-media-bg) 40%, transparent) 56%,
      color-mix(in srgb, var(--sh-media-bg) 54%, transparent) 74%,
      color-mix(in srgb, var(--sh-media-bg) 68%, transparent) 100%
    ),
    linear-gradient(0deg, color-mix(in srgb, var(--sh-media-bg) 46%, transparent) 0%, transparent 26%);
}

/* ---- 名牌 ---------------------------------------------------------- */

.rc-plate {
  position: absolute;
  top: 30px;
  left: 32px;
  z-index: 2;
  display: flex;
  width: min(330px, 34%);
  flex-direction: column;
  gap: var(--sh-gap-3);
}

/* 名牌落在左上角：垫一层从左上散开的暗雾，字才读得出来。 */
.rc-plate::before {
  position: absolute;
  z-index: -1;
  inset: -34px -70px -26px -46px;
  background: radial-gradient(
    120% 120% at 12% 10%,
    rgb(0 0 0 / 52%) 0%,
    rgb(0 0 0 / 28%) 46%,
    rgb(0 0 0 / 8%) 68%,
    transparent 80%
  );
  content: '';
  pointer-events: none;
}

.rc-kicker {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
  color: color-mix(in srgb, var(--sh-warn) 42%, var(--sh-media-text));
  font-size: 10.5px;
  letter-spacing: 0.16em;
}

.rc-kicker b {
  width: 16px;
  height: 1px;
  background: currentColor;
}

.rc-name {
  margin: 0;
  color: var(--rc-ink);
  font-family: var(--sh-font-serif);
  font-size: 40px;
  font-weight: 500;
  letter-spacing: 0.03em;
  line-height: 1.06;
  text-wrap: balance;
  text-shadow: 0 2px 28px color-mix(in srgb, var(--sh-media-bg) 80%, transparent);
}

.rc-intro {
  margin: 0;
  max-width: 26ch;
  color: var(--rc-sub);
  font-size: 12.5px;
  line-height: 1.9;
  text-wrap: pretty;
}

.rc-plate-acts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
}

.rc-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 11px;
  border: 1px solid color-mix(in srgb, var(--sh-media-text) 22%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--sh-media-bg) 34%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 88%, transparent);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  backdrop-filter: blur(8px);
  transition: border-color var(--sh-t) var(--sh-ease), color var(--sh-t) var(--sh-ease),
    background var(--sh-t) var(--sh-ease);
}

.rc-chip:hover {
  border-color: color-mix(in srgb, var(--sh-accent) 60%, transparent);
  color: var(--rc-ink);
  background: color-mix(in srgb, var(--sh-media-bg) 52%, transparent);
}

.rc-chip.is-quiet {
  color: color-mix(in srgb, var(--sh-media-text) 64%, transparent);
}

/* 正在出声：三根呼吸的条子，替掉那颗喇叭。 */
.rc-bars {
  display: inline-flex;
  align-items: flex-end;
  gap: 2px;
  height: 11px;
}

.rc-bars i {
  display: block;
  width: 2px;
  border-radius: 1px;
  background: var(--rc-accent);
  animation: rc-bars 1s var(--sh-ease) infinite;
}

.rc-bars i:nth-child(1) {
  height: 5px;
}
.rc-bars i:nth-child(2) {
  height: 10px;
  animation-delay: 0.14s;
}
.rc-bars i:nth-child(3) {
  height: 7px;
  animation-delay: 0.28s;
}

@keyframes rc-bars {
  0%,
  100% {
    transform: scaleY(0.45);
    opacity: 0.7;
  }
  50% {
    transform: scaleY(1);
    opacity: 1;
  }
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
  z-index: 5;
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

/* ---- 台本 ---------------------------------------------------------- */

.rc-read {
  position: relative;
  z-index: 2;
  display: flex;
  width: auto;
  /* 台本收窄、整块贴右：左边留给她，字只占一条很窄的竖带。 */
  max-width: 24%;
  min-width: 0;
  height: 100%;
  flex: 1 1 auto;
  margin-left: auto;
  flex-direction: column;
  /* 这一列自己有断点：窄到 380 以下时页头与输入行要换排法。 */
  container-type: inline-size;
  container-name: read;
}

/* 台本收起来时留在右边的那枚「<」：没有边框，只是一道轻的提示。 */
.rc-read-open {
  position: absolute;
  top: 50%;
  right: 6px;
  z-index: 2;
  display: grid;
  width: 30px;
  height: 62px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: color-mix(in srgb, var(--sh-media-text) 46%, transparent);
  cursor: pointer;
  place-items: center;
  transform: translateY(-50%);
  transition: color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease);
}

.rc-read-open:hover {
  background: color-mix(in srgb, var(--sh-media-text) 8%, transparent);
  color: var(--sh-media-text);
}

/* 磨砂只从右半边起：左边她的画面是清楚的，越往右越虚。 */
.rc-frost {
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  backdrop-filter: blur(var(--sh-blur)) saturate(0.96);
  mask-image: linear-gradient(90deg, transparent 0, black 34%, black 100%);
}

/* 台本这一列里，除了磨砂底与记录抽屉，其余都压在底之上。 */
.rc-read > :not(.rc-frost, .rc-toc) {
  position: relative;
  z-index: 1;
}

.rc-read-head {
  display: flex;
  flex: none;
  align-items: flex-end;
  gap: 12px;
  padding: 24px 24px 10px 22px;
}

.rc-read-title {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}

.rc-read-title h2 {
  margin: 0;
  overflow: hidden;
  color: var(--rc-ink);
  font-family: var(--sh-font-serif);
  font-size: 20px;
  font-weight: 500;
  letter-spacing: 0.01em;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-shadow: 0 2px 20px color-mix(in srgb, var(--sh-media-bg) 74%, transparent);
}

.rc-read-meta {
  color: var(--rc-sub);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.rc-read-acts {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: auto;
}

.rc-tool {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 30px;
  padding: 0 9px;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: none;
  color: color-mix(in srgb, var(--sh-media-text) 76%, transparent);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: background var(--sh-t) var(--sh-ease), color var(--sh-t) var(--sh-ease);
}

.rc-tool:hover {
  background: color-mix(in srgb, var(--sh-media-text) 12%, transparent);
  color: var(--rc-ink);
}

.rc-tool.is-icon {
  width: 30px;
  padding: 0;
  justify-content: center;
}

.rc-tool-n {
  color: color-mix(in srgb, var(--sh-media-text) 52%, transparent);
  font-variant-numeric: tabular-nums;
}

.rc-script {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 6px 24px 12px 22px;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--sh-media-text) 22%, transparent) transparent;
  mask-image: linear-gradient(to bottom, transparent 0, black 24px);
}

.rc-script::-webkit-scrollbar {
  width: 7px;
}

.rc-script::-webkit-scrollbar-thumb {
  border-radius: 4px;
  background: color-mix(in srgb, var(--sh-media-text) 20%, transparent);
}

.rc-line {
  max-width: 56ch;
  margin: 0 0 22px;
  animation: rc-rise 0.4s var(--sh-ease) both;
}

.rc-line.user {
  padding-left: 18px;
  border-left: 2px solid color-mix(in srgb, var(--sh-accent) 44%, transparent);
}

@keyframes rc-rise {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.rc-who {
  display: block;
  margin-bottom: 7px;
  color: color-mix(in srgb, var(--sh-warn) 42%, var(--sh-media-text));
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.16em;
}

.rc-line.user .rc-who {
  color: color-mix(in srgb, var(--sh-accent) 34%, var(--sh-media-text));
}

.rc-line.is-error .rc-who {
  color: var(--rc-danger);
}

.rc-said {
  margin: 0;
  color: var(--rc-ink);
  font-size: 15.5px;
  line-height: 2.02;
  letter-spacing: 0.01em;
  text-wrap: pretty;
  text-shadow:
    0 1px 18px color-mix(in srgb, var(--sh-media-bg) 80%, transparent),
    0 0 2px color-mix(in srgb, var(--sh-media-bg) 50%, transparent);
}

.rc-line.user .rc-said {
  color: color-mix(in srgb, var(--sh-media-text) 92%, transparent);
  font-size: 14.5px;
  line-height: 1.95;
}

.rc-line.is-error .rc-said {
  color: var(--rc-danger);
  font-size: 13px;
  line-height: 1.9;
}

.rc-line.is-live .rc-said {
  color: color-mix(in srgb, var(--sh-media-text) 60%, transparent);
}

/* 正在念的那一句：字提亮到暖色，和旁边的白字区分开。 */
.rc-line.is-playing .rc-said {
  color: var(--rc-warm);
  text-shadow: 0 1px 22px color-mix(in srgb, var(--sh-warn) 26%, transparent);
}

.rc-line-acts {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 22px;
  margin-top: 4px;
  opacity: 0;
  transition: opacity var(--sh-t) var(--sh-ease);
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
  background: color-mix(in srgb, var(--sh-media-bg) 40%, transparent);
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
  color: color-mix(in srgb, var(--sh-media-text) 50%, transparent);
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
  height: 24px;
  margin: 0 0 20px;
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
  align-items: flex-start;
  justify-content: center;
  gap: 6px;
  animation: rc-rise 0.4s var(--sh-ease-out) both;
}

.rc-blank-line {
  margin: 0;
  color: color-mix(in srgb, var(--sh-media-text) 90%, transparent);
  font-size: 15px;
  text-shadow: 0 1px 16px color-mix(in srgb, var(--sh-media-bg) 85%, transparent);
}

.rc-blank-sub {
  margin: 0;
  max-width: 34ch;
  color: color-mix(in srgb, var(--sh-media-text) 68%, transparent);
  font-size: 12.5px;
  line-height: 1.8;
  text-shadow: 0 1px 14px color-mix(in srgb, var(--sh-media-bg) 85%, transparent);
}

.rc-blank.is-scene {
  flex: 1;
  min-height: 0;
  padding: 32px;
}

/* ---- 输入 ---------------------------------------------------------- */

.rc-compose {
  flex: none;
  margin: 0 24px 24px 22px;
  padding-top: 9px;
  border-top: 1px solid color-mix(in srgb, var(--sh-media-text) 16%, transparent);
}

.rc-inline-error {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 8px;
  color: var(--rc-danger);
  font-size: 12px;
  line-height: 1.6;
}

.rc-compose-in {
  display: flex;
  align-items: flex-end;
  gap: 10px;
}

.rc-input {
  flex: 1;
  min-height: 30px;
  max-height: 132px;
  padding: 5px 0;
  border: 0;
  outline: none;
  background: transparent;
  color: var(--rc-ink);
  font: inherit;
  font-size: 14.5px;
  line-height: 1.7;
  resize: none;
  scrollbar-width: thin;
  text-shadow: 0 1px 12px color-mix(in srgb, var(--sh-media-bg) 78%, transparent);
}

.rc-input::placeholder {
  color: color-mix(in srgb, var(--sh-media-text) 46%, transparent);
}

.rc-send {
  display: grid;
  flex: none;
  place-items: center;
  width: 32px;
  height: 32px;
  border: 1px solid transparent;
  border-radius: var(--sh-r-xs);
  background: var(--sh-accent);
  color: var(--sh-accent-contrast);
  cursor: pointer;
  transition: filter var(--sh-t) var(--sh-ease), opacity var(--sh-t) var(--sh-ease);
}

.rc-send:hover:not(:disabled) {
  filter: brightness(1.06);
}

.rc-send:disabled {
  background: color-mix(in srgb, var(--sh-media-text) 16%, transparent);
  color: color-mix(in srgb, var(--sh-media-text) 42%, transparent);
  cursor: default;
}

.rc-compose-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 7px;
}

.rc-mic {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 9px;
  border: 0;
  border-radius: var(--sh-r-2xs);
  background: none;
  color: color-mix(in srgb, var(--sh-media-text) 62%, transparent);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
  transition: color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease);
}

.rc-mic:hover:not(:disabled) {
  background: color-mix(in srgb, var(--sh-media-text) 10%, transparent);
  color: var(--rc-ink);
}

.rc-mic:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.rc-mic.is-on {
  color: var(--rc-accent);
}

.rc-mic.is-hearing {
  background: color-mix(in srgb, var(--sh-accent) 22%, transparent);
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

.rc-hint {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: color-mix(in srgb, var(--sh-media-text) 46%, transparent);
  font-size: 11.5px;
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rc-key {
  flex: none;
  color: color-mix(in srgb, var(--sh-media-text) 34%, transparent);
  font-size: 10.5px;
  letter-spacing: 0.04em;
}

/* 对话模型：输入框旁边的一枚胶囊，点开才露出服务与模型两栏。 */
.rc-model-wrap {
  position: relative;
  display: inline-flex;
  flex: none;
}

.rc-model {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 230px;
  height: 26px;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: var(--sh-r-2xs);
  background: none;
  color: color-mix(in srgb, var(--sh-media-text) 62%, transparent);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
  transition: color var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    border-color var(--sh-t) var(--sh-ease);
}

.rc-model:hover,
.rc-model.is-open {
  border-color: color-mix(in srgb, var(--sh-media-text) 18%, transparent);
  background: color-mix(in srgb, var(--sh-media-text) 10%, transparent);
  color: var(--rc-ink);
}

.rc-model.is-warn {
  color: var(--rc-warm);
}

.rc-model-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 弹层贴在胶囊上方：这里离页面底部很近，往下铺会被切掉。 */
.rc-model-pop {
  position: absolute;
  right: 0;
  bottom: calc(100% + 8px);
  z-index: 6;
  display: flex;
  width: 286px;
  flex-direction: column;
  gap: var(--sh-gap-2);
  padding: 13px 14px 11px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-radius-md);
  background: var(--sh-panel);
  box-shadow: var(--sh-shadow-3);
}

.rc-model-pop .rc-note {
  margin: 0;
  color: var(--sh-text-3);
}

/* ---- 记录（压在台本上） -------------------------------------------- */

.rc-toc {
  position: absolute;
  inset: 0;
  z-index: 3;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--sh-line);
  background: var(--sh-panel);
}

.rc-toc-head {
  display: flex;
  flex: none;
  align-items: center;
  gap: 10px;
  padding: 22px 20px 10px 30px;
}

.rc-toc-head h2 {
  margin: 0;
  color: var(--sh-text);
  font-size: 17px;
  font-weight: 600;
}

.rc-toc-count {
  color: var(--sh-text-4);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.rc-toc-head .rc-tool {
  margin-left: auto;
  color: var(--sh-text-3);
}

.rc-toc-head .rc-tool:hover {
  background: var(--sh-chip);
  color: var(--sh-text);
}

.rc-toc-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 4px 14px 12px;
}

.rc-toc-row {
  display: flex;
  align-items: center;
  gap: 8px;
  border-radius: var(--sh-r-md);
  transition: background var(--sh-t) var(--sh-ease);
}

.rc-toc-row:hover {
  background: var(--sh-chip);
}

.rc-toc-row.is-active {
  background: var(--sh-accent-soft);
}

.rc-toc-main {
  display: grid;
  flex: 1;
  min-width: 0;
  grid-template-columns: 72px minmax(0, 1fr) auto;
  align-items: baseline;
  gap: 10px;
  padding: 11px 4px 11px 14px;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.rc-toc-date {
  color: var(--sh-text-4);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.rc-toc-title {
  overflow: hidden;
  color: var(--sh-text-2);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rc-toc-row.is-active .rc-toc-title {
  color: var(--sh-accent);
  font-weight: 600;
}

.rc-toc-n {
  color: var(--sh-text-4);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.rc-toc-del {
  display: grid;
  flex: none;
  place-items: center;
  width: 26px;
  height: 26px;
  margin-right: 6px;
  border: 0;
  border-radius: var(--sh-r-xs);
  background: none;
  color: var(--sh-text-4);
  cursor: pointer;
  opacity: 0.5;
  transition: opacity var(--sh-t) var(--sh-ease), background var(--sh-t) var(--sh-ease),
    color var(--sh-t) var(--sh-ease);
}

.rc-toc-row:hover .rc-toc-del {
  opacity: 1;
}

.rc-toc-del:hover {
  background: var(--sh-danger-soft);
  color: var(--sh-danger);
}

.rc-toc-del:disabled {
  cursor: default;
  opacity: 0.3;
}

.rc-toc-del:disabled:hover {
  background: none;
  color: var(--sh-text-4);
}

.rc-toc-confirm {
  display: flex;
  flex: none;
  align-items: center;
  gap: 6px;
  padding-right: 10px;
  color: var(--sh-text-3);
  font-size: 11.5px;
}

.rc-toc-confirm button {
  height: 24px;
  padding: 0 8px;
  border: 1px solid var(--sh-line);
  border-radius: var(--sh-r-2xs);
  background: var(--sh-panel);
  color: var(--sh-text-2);
  font: inherit;
  font-size: 11.5px;
  cursor: pointer;
}

.rc-toc-confirm .rc-danger {
  border-color: color-mix(in srgb, var(--sh-danger) 40%, transparent);
  background: var(--sh-danger-soft);
  color: var(--sh-danger);
}

.rc-toc-confirm button:disabled {
  cursor: default;
  opacity: 0.45;
}

.rc-toc-foot {
  flex: none;
  padding: 10px 20px 16px 30px;
  border-top: 1px solid var(--sh-line-soft);
}

.rc-toc-enter-active,
.rc-toc-leave-active {
  transition: transform 0.3s var(--sh-ease), opacity 0.2s var(--sh-ease);
}

.rc-toc-enter-from,
.rc-toc-leave-to {
  transform: translateY(-8%);
  opacity: 0;
}

/* ---- 角色设置（从名牌处铺开） -------------------------------------- */

.rc-scrim {
  position: absolute;
  inset: 0;
  z-index: 3;
  border: 0;
  background: color-mix(in srgb, var(--sh-media-bg) 34%, transparent);
  cursor: default;
}

.rc-sheet {
  position: absolute;
  top: 22px;
  left: 22px;
  z-index: 4;
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

/* 试听：不改变选择，只把声音递到播放条。 */
.rc-voice-play {
  display: grid;
  flex: none;
  place-items: center;
  width: 28px;
  height: 28px;
  margin-left: auto;
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
  color: #fff;
  cursor: pointer;
  transition: border-color var(--sh-t) var(--sh-ease), box-shadow var(--sh-t) var(--sh-ease);
}

.rc-bg-pick span {
  position: absolute;
  inset: auto 0 0;
  padding: 14px 8px 5px;
  background: linear-gradient(180deg, transparent, rgb(0 0 0 / 62%));
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
  background: linear-gradient(180deg, transparent, rgb(0 0 0 / 72%));
  color: #fff;
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
/* 这一页夹在导航、分类栏与音色列之后：视口 1440 时房间可能只有 690。
   所以断点不看视口，按房间自己的宽度（container-name: room）算。 */

/* 台本收窄成一条竖带以后，页头与输入行走另一套排法：
   按钮只留图标，提示与快捷键收起来，把宽度全让给字。 */
@container read (max-width: 400px) {
  .rc-read-head {
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 6px;
    padding: 20px 16px 8px 16px;
  }

  .rc-read-acts {
    margin-left: 0;
  }

  .rc-tool-label {
    display: none;
  }

  .rc-script {
    padding: 4px 16px 10px 16px;
  }

  .rc-said,
  .rc-line.user .rc-said {
    font-size: 14px;
    line-height: 1.9;
  }

  .rc-compose {
    margin: 0 16px 18px 16px;
  }

  .rc-compose-foot {
    flex-wrap: wrap;
    row-gap: 4px;
  }

  .rc-hint,
  .rc-key {
    display: none;
  }

  .rc-model {
    max-width: 100%;
  }
}

@container room (max-width: 1120px) {
  /* 房间窄、台本又占去一半时，把人物往左裁：她的脸不能落在字下面。 */
  .rc-scene {
    background-position: 62% 32%;
  }

  .rc-plate {
    top: 24px;
    left: 28px;
    width: min(286px, 40%);
  }

  .rc-name {
    font-size: 34px;
  }

  .rc-read {
    max-width: 28%;
  }

  .rc-read-head {
    padding: 22px 28px 10px 28px;
  }

  .rc-script {
    padding: 6px 28px 12px 28px;
  }

  .rc-compose {
    margin: 0 28px 22px 28px;
  }

  .rc-sheet {
    top: 14px;
    left: 14px;
  }
}

/* 房间不够两栏：名牌排到台本上方，背景（如果有图）铺满整块，由台本自己压暗。 */
@container room (max-width: 900px) {
  .rc-scene {
    flex-direction: column;
    background-color: var(--sh-media-bg);
  }

  .rc-plate {
    position: relative;
    top: auto;
    bottom: auto;
    left: auto;
    width: auto;
    gap: var(--sh-gap-2);
    padding: 22px 22px 18px;
  }

  /* 窄档没有两栏，立绘没地方站：整张收起来，只留房间；气泡也没有依附的人。 */
  .rc-figure,
  .rc-bubble,
  .rc-plate::before {
    display: none;
  }

  .rc-name {
    font-size: 28px;
  }

  .rc-intro {
    max-width: 60ch;
  }

  .rc-roster {
    top: calc(100% + 8px);
    bottom: auto;
    padding-top: 0;
  }

  .rc-read {
    width: 100%;
    max-width: none;
    height: auto;
    flex: 1;
    margin-left: 0;
  }

  /* 台本落在实底上：这一档不再用磨砂，避免整张脸被虚化。 */
  .rc-frost {
    display: none;
  }

  /* 有图时台本压一层向下的暗化保证可读；没图时场景本身是暗底，不必再压。 */
  .rc-scene.has-image .rc-read {
    background: linear-gradient(
      180deg,
      color-mix(in srgb, var(--sh-media-bg) 34%, transparent) 0,
      color-mix(in srgb, var(--sh-media-bg) 88%, transparent) 96px,
      color-mix(in srgb, var(--sh-media-bg) 94%, transparent) 100%
    );
  }

  .rc-read-head {
    padding: 10px 20px 6px 22px;
  }

  .rc-script {
    padding: 4px 20px 8px 22px;
  }

  .rc-compose {
    margin: 0 20px 14px 22px;
  }
}

@container room (max-width: 560px) {
  .rc-read-head {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }

  .rc-read-acts {
    margin-left: 0;
  }

  .rc-key,
  .rc-hint {
    display: none;
  }

  .rc-sheet {
    top: 8px;
    left: 8px;
    width: calc(100% - 16px);
    max-height: calc(100% - 16px);
  }
}

/* 房间很宽：画面与台本一起上一个档，字不贴着一条窄线。 */
@container room (min-width: 1400px) {
  .rc-plate {
    top: 40px;
    left: 46px;
    width: 340px;
  }

  .rc-name {
    font-size: 48px;
  }

  .rc-intro {
    max-width: 28ch;
    font-size: 13.5px;
  }

  .rc-read {
    max-width: min(24%, 640px);
  }

  .rc-read-head {
    padding: 26px 26px 12px 24px;
  }

  .rc-read-title h2 {
    font-size: 22px;
  }

  .rc-script {
    padding: 6px 26px 12px 24px;
  }

  .rc-said {
    font-size: 15px;
  }

  .rc-line.user .rc-said {
    font-size: 14.5px;
  }

  .rc-compose {
    margin: 0 26px 28px 24px;
  }
}

/* 再宽一档：台本跟着长，字仍按 56ch 折行，多出来的是可读的留白。 */
@container room (min-width: 1700px) {
  .rc-read {
    max-width: min(22%, 700px);
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
  top: 44px;
  left: 56px;
  width: 360px;
}

.rc.is-fullscreen .rc-name,
.rc:fullscreen .rc-name {
  font-size: 52px;
}

.rc.is-fullscreen .rc-said,
.rc:fullscreen .rc-said {
  font-size: 16.5px;
}

.rc.is-fullscreen .rc-compose,
.rc:fullscreen .rc-compose {
  margin-bottom: 34px;
}
</style>
