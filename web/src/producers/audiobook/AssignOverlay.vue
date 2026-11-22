<template>
  <!-- 全局「指定音色」：挂在壳上，与哪一页无关。
       播放条上那颗按钮在音色库、生成页、日志页都能点，不必先回创作页。 -->
  <AssignVoiceDialog
    :open="open"
    :voice="target"
    :roles="roles"
    :scope="chapterScope ? 'chapter' : 'role'"
    :engine-label="label"
    :assignments="assignments"
    :engines="engines"
    @close="close"
    @assign="submit"
  />
</template>

<script setup>
// @ts-check
import { computed, ref, watch } from 'vue'
import { say, store } from '../../store'
import AssignVoiceDialog from '../../components/VoiceAssignDialog.vue'
import {
  engineForAssign,
  engineLabelForVoice,
  enginesForVoice,
  namedSpeakersOf,
  rolesOf,
} from './assign'
import { assignChapterVoice, assignVoice, ensureDetail, state } from './store'

const target = ref(/** @type {any} */ (null))
const open = ref(false)

/** 没识别出角色时，弹窗与分配表一样按章列行：一行一章，旁白不再单列。 */
const chapterScope = computed(() => {
  const detail = state.detail
  return Boolean(detail && !namedSpeakersOf(detail).length && (detail.chapters || []).length)
})

/** 目标名单来自当前项目的详情：有角色按角色，没有就按章。 */
const roles = computed(() => {
  const detail = state.detail
  if (!detail) return []
  if (chapterScope.value) {
    return (detail.chapters || []).map(
      /** @param {any} chapter */ (chapter) => ({
        key: chapter.id,
        name: chapter.title,
        rows: Number(chapter.rows) || 0,
        chapter_id: chapter.id,
      })
    )
  }
  return rolesOf(detail).map(
    /** @param {any} item */ (item) => ({ ...item, key: item.name })
  )
})

/** 当前目标配了哪颗音色：章级映射里没单独指定的章，显示它沿用的那一颗。 */
const assignments = computed(() => {
  const detail = state.detail
  if (!detail) return {}
  if (!chapterScope.value) return detail.voices || {}
  const table = detail.chapter_voices || {}
  const fallback =
    (detail.chapters || [])
      .map(/** @param {any} chapter */ (chapter) => table[chapter.id])
      .find(/** @param {any} item */ (item) => item?.voice_id) || null
  return Object.fromEntries(
    (detail.chapters || [])
      .map(
        /** @param {any} chapter */ (chapter) => [chapter.id, table[chapter.id] || fallback]
      )
      .filter(/** @param {any[]} entry */ (entry) => entry[1])
  )
})
const engines = computed(() => enginesForVoice(target.value))
const label = computed(() => engineLabelForVoice(target.value))

/** 宿主把"要给角色或章节指定音色"的请求放在 `store.assignVoice`：取走即清空，
 *  免得换页或重挂时又弹一次。 */
watch(
  () => store.assignVoice,
  async (voice) => {
    if (!voice) return
    store.assignVoice = null
    // 已经开在这条音色上（弹窗里的试听也会走到播放条）：别再弹一次、把选择重置掉。
    if (open.value && target.value?.id === voice.id) return
    // 没打开过创作页时先取一次当前项目：角色在项目详情里，
    // 不能因为"没去过那一页"就把这件事判成不可用。
    const detail = state.detail || (await ensureDetail())
    if (!detail) {
      say('还没有有声书项目：先在创作页导入或新建一本。', { kind: 'warn' })
      return
    }
    target.value = voice
    open.value = true
  },
  { immediate: true }
)

function close() {
  open.value = false
  target.value = null
}

/** @param {any} payload */
async function submit(payload) {
  const voice = target.value
  if (!voice) return
  // 弹窗没选引擎时（平台音色只有一台）由这里补；判据在 assign.js 一处。
  const engine = payload.engine || engineForAssign(payload.key, voice, assignments.value)
  open.value = false
  target.value = null
  const picked = { ...voice, source: engine }
  if (chapterScope.value) await assignChapterVoice(payload.key, picked, payload)
  else await assignVoice(payload.key, picked, payload)
}
</script>
