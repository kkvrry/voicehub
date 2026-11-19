<template>
  <!-- 音色详情：居中对话框。
       抬头一段"这是谁"的概览——封面、名字（本地音色点一下就能改）、性别圆标、
       一排只写字的属性胶囊、一段没有框的描述；管理动作收在右上角一排图标里。
       正文只有一件事：听（音频样例 / 生成样例）。 -->
  <Transition name="vh-modal">
    <div v-if="voice" class="vh-root" @click.self="close">
      <div
        ref="dialogEl"
        class="vh-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="titleText"
        tabindex="-1"
      >
        <header class="vh-hero">
          <!-- 头像也是播放键，与卡片同一套：点它放主样例，徽标 hover / 键盘聚焦才浮现、
               播放中常亮。样例清单就在下面，但"先听一句"不该非要在清单里找点哪儿。 -->
          <button
            type="button"
            class="vh-cover-btn"
            :class="{ bare: !mainUrl }"
            :title="mainUrl ? (isPlayingMain ? '暂停' : '播放主样例') : titleText"
            :aria-label="
              mainUrl
                ? isPlayingMain
                  ? '暂停播放主样例'
                  : `播放 ${titleText} 的主样例`
                : titleText
            "
            @click="playMain"
          >
            <img
              v-if="voice.cover_url && showAvatarImage"
              class="vh-cover"
              :src="voice.cover_url"
              alt=""
            />
            <!-- 圆标与本地音色同一套（首字母色块）；"这条从哪来"由来源徽标说。 -->
            <span v-else class="vh-cover" :style="{ background: coverColor }">{{ initial }}</span>
            <span
              v-if="mainUrl"
              class="vh-cover-play"
              :class="{ playing: isPlayingMain }"
              aria-hidden="true"
            >
              <Pause v-if="isPlayingMain" :size="20" :stroke-width="1.8" fill="currentColor" />
              <Play v-else :size="20" :stroke-width="1.8" fill="currentColor" />
            </span>
          </button>

          <div class="vh-hero-main">
            <div class="vh-title-row">
              <!-- 名字就是名称本身：点一下就地改，不再另开一格「显示名」。
                   平台音色的名字来自平台，不给改。 -->
              <InlineText
                v-if="titleEditing"
                ref="titleEl"
                class="vh-title-input"
                :model-value="titleValue"
                :placeholder="voice.name"
                aria-label="名称"
                :maxlength="60"
                @change="saveTitle"
                @done="titleEditing = false"
              />
              <h2
                v-else
                class="vh-title"
                :class="{ 'is-editable': voice.is_local }"
                :title="voice.is_local ? '点击修改名称' : ''"
                @click="startTitleEdit"
              >
                {{ titleText }}
              </h2>
              <GenderPicker
                :model-value="form.gender"
                :options="vocab.genders"
                @change="(value) => pick('gender', value)"
              />
              <span v-if="voice.category" class="vh-pill">{{ voice.category }}</span>
              <span v-if="originPill" class="vh-pill is-ghost">{{ originPill }}</span>
            </div>
            <!-- 描述就长在名字下面：**没有输入框的样子**，读的时候是一段话，点进去才变成可改的。
                 从前这一行写的是「本地音色 · 3 条音频」——来源由上面的徽标说、条数由下面样例区的标题说，
                 同一件事说三遍的结果是名字底下最该有内容的那一行被占着。 -->
            <InlineText
              class="vh-lead"
              :model-value="form.description"
              placeholder="点击添加描述"
              multiline
              aria-label="描述"
              :maxlength="400"
              @change="(value) => commitText('description', value)"
            />
            <!-- 语言 / 年龄感 / 标签：**改就在这一行改**，只是一排字，没有点、没有箭头。 -->
            <div class="vh-traits">
              <ChipSelect
                :model-value="form.language"
                :options="vocab.languages"
                :tone-of="languageTone"
                label="语言"
                placeholder="＋ 语言"
                @change="(value) => pick('language', value)"
              />
              <ChipSelect
                :model-value="form.age"
                :options="vocab.ages"
                :tone-of="ageTone"
                label="年龄感"
                placeholder="＋ 年龄感"
                @change="(value) => pick('age', value)"
              />
              <TagChips
                :model-value="form.tags"
                :groups="tagGroups"
                @change="commitTags"
              />
            </div>
          </div>

          <!-- 右上角：三个管理动作 + 收藏 + 关闭。图标 + 悬停说明，不占正文一个字。 -->
          <div class="vh-actions">
            <button
              v-if="voice.folder"
              class="vh-icon"
              title="在资源管理器里打开"
              aria-label="在资源管理器里打开"
              @click="openFolder"
            >
              <FolderOpen :size="16" />
            </button>
            <!-- 头像由宿主保存（avatar.*）；点开先看候选，挑好才覆盖。 -->
            <button
              v-if="voice.is_local"
              class="vh-icon"
              :title="voice.avatar_generated ? '更换头像（先看候选）' : '查找头像（先看候选）'"
              :aria-label="voice.avatar_generated ? '更换头像' : '查找头像'"
              @click="avatarVoice = voice"
            >
              <ImagePlus :size="16" />
            </button>
            <!-- 隐藏与卡片上那颗是同一个组件（`ui/HideButton.vue`）。 -->
            <HideButton :hidden="voice.hidden" @toggle="toggleHidden" />
            <button class="vh-icon is-danger" :title="removeHint" aria-label="移除音色" @click="remove">
              <Trash2 :size="16" />
            </button>
            <span class="vh-actions-sep" aria-hidden="true"></span>
            <button
              class="vh-icon"
              :class="{ 'is-lit': voice.favorite }"
              :title="voice.favorite ? '取消收藏' : '收藏'"
              :aria-label="voice.favorite ? '取消收藏' : '收藏'"
              @click="toggleFavorite"
            >
              <Star :size="16" :fill="voice.favorite ? 'currentColor' : 'none'" />
            </button>
            <button class="vh-icon" title="关闭" aria-label="关闭" @click="close">
              <X :size="18" />
            </button>
          </div>
        </header>

        <div class="vh-body">
          <!-- 音频样例：**不再套一层卡片**。音频条自己带边框与白底，
               外面再垫一块底板就成了「底板 + 条」的双层容器，两层之间那圈填空题
               把本来就不宽的地方又吃掉一圈。标题行直接落在主内容区上。
               「生成样例」是这颗标题行最右端的一颗按钮：它属于"这份列表"这一个动作
               （往里面再加一条），所以跟标题同一行，不另占一段。 -->
          <section v-if="voice.samples.length || auditionEngine" class="vh-sec vh-sec-audio">
            <div class="vh-sec-head">
              <AudioLines :size="14" />
              <span v-if="voice.samples.length" class="vh-sec-title">
                音频样例（{{ voice.samples.length }}）
              </span>
              <span v-else class="vh-sec-title">音频样例</span>
              <span class="spacer"></span>
              <!-- 顺序播放：整份样例排成一条队列。卡片上不再摆这颗按钮，
                   这里的「音频样例（N）」是"这个音色有哪些音频"唯一的地方，
                   "连着听完"也就该在这里。 -->
              <button
                v-if="voice.samples.length > 1"
                class="icon-btn vh-seq-btn"
                title="顺序播放全部样例"
                aria-label="顺序播放全部样例"
                @click="actions.playAllSamples(voice)"
              >
                <ListMusic :size="14" :stroke-width="1.8" />
              </button>
              <!-- 调过参数就在按钮上点一颗小点：参数都收进窗口了，
                   这是唯一能看出"这次不是全默认"的地方。 -->
              <button
                v-if="auditionEngine"
                class="btn mini vh-gen-btn"
                :title="genHint"
                @click="auditionOpen = true"
              >
                生成样例
                <span v-if="changedParams" class="vh-gen-dot" aria-hidden="true"></span>
              </button>
            </div>
            <SampleList :voice="voice" />
          </section>

          <!-- 生成样例的窗口：文案 + 详细参数都在里面（平台音色与"落在引擎保存目录里的
               本地音色"才有入口——那段音频是那台引擎生成的）。 -->
          <AuditionPanel
            v-if="auditionOpen && auditionEngine"
            :voice="voice"
            :engine-key="auditionEngine"
            :voice-id="auditionVoiceId"
            @close="auditionOpen = false"
          />
        </div>
      </div>
      <AvatarPicker
        :voice="avatarVoice"
        @close="avatarVoice = null"
        @applied="onAvatarApplied"
      />
    </div>
  </Transition>
</template>

<script setup>
// @ts-check
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { AudioLines, FolderOpen, ImagePlus, ListMusic, Pause, Play, Star, Trash2, X } from 'lucide-vue-next'
import { api } from '../api'
import { store, actions, vocab, say } from '../store'
import { useSettingsStore } from '../stores/settings'
import { useAvatarImage } from '../composables/avatar'
import { avatarFill } from '../composables/avatarColor'
import AvatarPicker from './AvatarPicker.vue'
import ChipSelect from './ui/ChipSelect.vue'
import GenderPicker from './ui/GenderPicker.vue'
import HideButton from './ui/HideButton.vue'
import InlineText from './ui/InlineText.vue'
import TagChips from './ui/TagChips.vue'
import SampleList from './SampleList.vue'
import AuditionPanel from './AuditionPanel.vue'
import { ageTone, languageTone } from './ui/tones'

const voice = computed(() => store.detail)
const showAvatarImage = useAvatarImage('detail')
const dialogEl = ref(/** @type {any} */ (null))
const titleEl = ref(/** @type {any} */ (null))
/** 名字正在就地编辑（点击标题进入）：编辑时那一行只留输入框，免得别的胶囊把光标挤走。 */
const titleEditing = ref(false)
/** 「生成样例」那个窗口开没开（声明在下面 watch 之前：它是 immediate 的，会先跑一趟）。 */
const auditionOpen = ref(false)
/** 正在挑头像的那条音色（关掉选图弹层就置空）。 */
const avatarVoice = ref(/** @type {any} */ (null))

/** 角色卡的两组固定标签：语调（怎么念）、音色（声音质地）。候选来自后端词表，
 *  选中的与自填词一样落进 `tags`——筛选、AI 匹配、卡片上的颜色都不必另认一套。 */
const tagGroups = computed(() =>
  [
    { label: '语调', options: vocab.tones },
    { label: '音色', options: vocab.timbres },
  ].filter(/** @param {any} group */ (group) => group.options.length)
)

const KEYS = ['display', 'language', 'gender', 'age', 'tags', 'description']
const form = reactive(Object.fromEntries(KEYS.map(/** @param {any} key */ (key) => [key, ''])))
const saved = reactive(Object.fromEntries(KEYS.map(/** @param {any} key */ (key) => [key, ''])))
form.tags = []
saved.tags = []

/**
 * 点标签选完之后：先把表单挪过去（控件是受控的，不回写会弹回旧值），再走统一的提交。
 *
 * @param {string} key
 * @param {any} value
 */
async function pick(key, value) {
  form[key] = value
  await commit(key, value)
}

watch(
  voice,
  (value) => {
    titleEditing.value = false
    auditionOpen.value = false
    // 关掉详情或换一条音色时，上一张的选图弹层不能跟过来。
    avatarVoice.value = null
    if (!value) return
    for (const key of KEYS) {
      const field = key === 'tags' ? [...(value.tags || [])] : value[key] || ''
      form[key] = field
      saved[key] = key === 'tags' ? [...(value.tags || [])] : value[key] || ''
    }
    // 打开时把焦点收进对话框：Esc 能立刻关，读屏也从头念。
    void nextTick(() => dialogEl.value?.focus())
  },
  { immediate: true }
)

/**
 * 值有没有变。数组按元素比——用 join 拼分隔符去比，分隔符一旦出现在某个值里
 * 就会把"变了"误判成"没变"。
 *
 * @param {any} a
 * @param {any} b
 */
const sameValue = (a, b) => {
  if (!Array.isArray(a)) return a === b
  const other = b || []
  return a.length === other.length && a.every((value, index) => value === other[index])
}

/**
 * 改完就存，不摆「保存修改」按钮（一格就是一个字段、一次一个请求）。
 * 只有真变了才发；发之前先把 `saved` 挪上去，防止请求回来前重复提交。
 *
 * @param {string} key
 * @param {any} value
 */
async function commit(key, value) {
  if (sameValue(value, saved[key])) return
  const before = saved[key]
  saved[key] = Array.isArray(value) ? [...value] : value
  try {
    await actions.saveVoice({ [key]: value })
  } catch (error) {
    saved[key] = before
    say(error instanceof Error ? error.message : String(error))
  }
}

/**
 * 标签是"加一颗/删一颗"，没有"打完了"的时刻，不等 blur——变了就存。
 *
 * @param {any} next
 */
function commitTags(next) {
  form.tags = next
  commit('tags', next)
}

/**
 * 就地编辑的一段字（描述 / 名称）：先把受控值挪过去，再走同一条提交路径。
 *
 * @param {string} key
 * @param {any} value
 */
async function commitText(key, value) {
  form[key] = value
  await commit(key, value)
}

/** 名字：点标题进入编辑；存完退出。存完不改 `form.display` 以外的状态——
 *  标题那一段显示的是 `shown_name`（后端会把空显示名回落到文件夹名）。 */
async function startTitleEdit() {
  if (!voice.value?.is_local) return
  titleEditing.value = true
  await nextTick()
  titleEl.value?.focus()
}

/**
 * @param {any} value
 */
async function saveTitle(value) {
  await commitText('display', value)
  titleEditing.value = false
}

const titleText = computed(() => voice.value?.shown_name || '')

/** 点头像播的那一段：后端给的 `main_audio_url`，与卡片同一个字段、
 *  同一条地址——前端不自己拼（文件名里可能有 `#`）。 */
const mainUrl = computed(() => voice.value?.main_audio_url || '')
const isPlayingMain = computed(
  () => store.player.playing && store.player.url === mainUrl.value
)

function playMain() {
  if (!mainUrl.value) return
  // 与卡片、左栏头像同一条路：排这条音色自己的样例队列，从主样例开始，
  // 上/下一条只由用户点，播完当前这条不自动续播。
  actions.playVoiceCard(voice.value)
}
/** 点开标题时，输入框里先放**现在显示的名字**（没单独设过就是文件夹名）：
 *  空着一个框、把名字塞进占位词，看起来像"这条没有名字"。 */
const titleValue = computed(() => form.display || titleText.value)
const initial = computed(() => (titleText.value || '?').slice(0, 1))
const settings = useSettingsStore()
const coverColor = computed(() =>
  avatarFill(titleText.value, settings.appearance.look)
)
/** 来源徽标：平台音色挂引擎名，引擎目录里生成的本地音色挂那台引擎。
 *  普通本地音色不挂——"本地"这件事由这一颗与下面的样例区各自说过了。
 *  设计音色也不挂引擎：它只是那段描述，用哪家现由合成时选。
 *  （原来名字底下还有一行「本地音色 · 3 条音频」，与徽标、与样例区标题重复，已删。） */
const originPill = computed(() => {
  const value = voice.value
  if (!value) return ''
  // 设计音色不挂来源：分类那一颗已经写着「设计音色」，再挂引擎名是误导
  if (actions.isDesignVoice(value)) return ''
  if (!value.is_local) return actions.engineOf(value.source)?.label || value.source
  return value.engine?.label ? `${value.engine.label} 生成` : ''
})

/** 「生成样例」这一段归哪台引擎：平台音色看 `source`；引擎保存目录里的音色看
 *  `voice.engine`。拿不到引擎就不画这一段。 */
const auditionEngine = computed(() => {
  const value = voice.value
  if (!value) return ''
  if (!value.is_local) return value.source || ''
  return value.engine?.key || ''
})

/** 拿哪条音色去合成：平台音色用它自己；引擎目录里那种用对应的引擎音色 id。 */
const auditionVoiceId = computed(() => {
  const value = voice.value
  if (!value) return ''
  if (!value.is_local) return value.id
  return value.engine?.voice_id || ''
})

/** 这一次不是全默认：窗口里那几格有几格被要求过。
 *  参数都收进窗口了，这是标题行上唯一能看出"这次会带着参数发"的地方。 */
const changedParams = computed(() => {
  const key = auditionEngine.value
  const engine = store.engines.find(/** @param {any} item */ (item) => item.key === key) || null
  return actions.countSet(actions.auditionFields(engine), store.audition.values)
})

const genHint = computed(() =>
  changedParams.value ? `生成样例（已调 ${changedParams.value} 项参数）` : '生成样例'
)

/** 「移除」对两类音色都从库里去掉记录，但本地音色还要动磁盘——那句话点之前说清。 */
const removeHint = computed(() =>
  voice.value?.is_local
    ? '移除音色（文件夹会移到回收站，可还原）'
    : '移除音色（从库里去掉这条记录；生成的音频留在它自己的目录里）'
)

/** 在资源管理器里打开这条音色的文件夹。服务不在本机、或者有目录被删了都会报错，照实说。 */
async function openFolder() {
  const value = voice.value
  if (!value) return
  try {
    await api.revealVoice(value.id)
  } catch (error) {
    say(error instanceof Error ? error.message : String(error))
  }
}

async function remove() {
  const value = voice.value
  if (!value) return
  const where = value.is_local ? `\n\n${value.folder}` : ''
  const ok = window.confirm(`移除「${value.shown_name}」？${where}`)
  if (!ok) return
  await actions.removeVoice(value)
}

/**
 * 选图弹层里点了「用这张」：重取这一条，卡片与详情一起换图。
 *
 * @param {any} result
 */
async function onAvatarApplied(result) {
  avatarVoice.value = null
  say(`头像已更新（来源：${result.source || '图片搜索'}）`, { kind: 'success' })
  await actions.reloadVoice(result.voice?.id || voice.value?.id)
}

function close() {
  actions.closeDetail()
}

async function toggleFavorite() {
  await actions.setPrefs(voice.value, { favorite: !voice.value.favorite })
}

async function toggleHidden() {
  await actions.setPrefs(voice.value, { hidden: !voice.value.hidden })
}

/**
 * Esc 关窗：焦点可能已经在某个输入框里，所以挂在 window 上。
 * 「生成样例」的窗口比详情晚一层：先关它，别一下子把两条一起关掉。
 *
 * @param {any} event
 */
function onKeydown(event) {
  if (!voice.value) return
  if (event.key !== 'Escape') return
  event.preventDefault()
  if (auditionOpen.value) {
    auditionOpen.value = false
    return
  }
  close()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>
