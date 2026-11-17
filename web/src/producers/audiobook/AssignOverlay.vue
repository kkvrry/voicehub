<template>
  <!-- 全局「指定音色」：挂在壳上，与哪一页无关。
       播放条上那颗按钮在音色库、生成页、日志页都能点，不必先回创作页。 -->
  <AssignVoiceDialog
    :open="open"
    :voice="target"
    :roles="roles"
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
import AssignVoiceDialog from './AssignVoiceDialog.vue'
import { engineForAssign, engineLabelForVoice, enginesForVoice, rolesOf } from './assign'
import { assignVoice, ensureDetail, state } from './store'

const target = ref(/** @type {any} */ (null))
const open = ref(false)

/** 角色名单来自当前项目的详情：弹窗开给哪本书，就是这本书里的角色。 */
const roles = computed(() => rolesOf(state.detail))
const assignments = computed(() => state.detail?.voices || {})
const engines = computed(() => enginesForVoice(target.value))
const label = computed(() => engineLabelForVoice(target.value))

/** 宿主把"要给角色指定音色"的请求放在 `store.assignVoice`：取走即清空，
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
  const engine = payload.engine || engineForAssign(payload.speaker, voice, assignments.value)
  open.value = false
  target.value = null
  await assignVoice(payload.speaker, { ...voice, source: engine }, payload)
}
</script>
