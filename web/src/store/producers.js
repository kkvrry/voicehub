// @ts-check
// 生产方插件的通用部分：清单、打开、配置。
//
// 书籍相关的状态与动作（项目、章节、行、合成）在插件自己的
// `producers/<key>/store.js` 里，宿主 store 不认识它们。
import { api } from '../api'
import { producerModule } from '../producers/registry'
import { store } from './state'
import { say } from './shell'

function known(key = store.producer.key) {
  return store.producers.find((item) => item.key === key) || null
}

/** 启停状态以后端为准：停用是真的不启动，不是本机藏入口。
 *  清单里每个插件带 `enabled`；这里只把它摊平成导航与设置分组要用的 key 清单。 */
function syncDisabled() {
  store.disabledProducers = (store.producers || [])
    .filter((plugin) => plugin.enabled === false)
    .map((plugin) => plugin.key)
}

/** @param {string} key */
export function producerEnabled(key) {
  const plugin = known(key)
  return plugin ? plugin.enabled !== false : !store.disabledProducers.includes(key)
}

/** 启用 / 停用：后端负责停服务与持久化，界面按返回的插件整包更新。
 *  返回 false 表示没切成（失败原因已由 `say` 说过）。 */
/**
 * @param {string} key
 * @param {boolean} enabled
 */
export async function setProducerEnabled(key, enabled) {
  try {
    const saved = await api.setProducerEnabled(key, enabled)
    const index = store.producers.findIndex((item) => item.key === key)
    if (index >= 0) store.producers[index] = { ...store.producers[index], ...saved }
    syncDisabled()
    return true
  } catch (error) {
    say(error instanceof Error ? error.message : '切换插件失败', { kind: 'warn' })
    return false
  }
}

/** 拉插件清单（导航项与设置分组都由它决定）。后端没有这个域时按"没装"处理。 */
export async function loadProducers() {
  try {
    store.producers = await api.producers()
  } catch {
    store.producers = []
  }
  syncDisabled()
  return store.producers
}

/** 打开某个生产方的工作区；界面与初始化由插件自己提供。 */
/** @param {string} key */
export async function openProducer(key) {
  if (!store.producers.length) await loadProducers()
  if (!known(key)) {
    say('未安装该插件')
    return
  }
  if (!producerEnabled(key)) {
    say('这个插件已停用：到「设置 → 插件」里重新启用。', { kind: 'warn' })
    return
  }
  store.view = 'producer'
  store.producer.key = key
  const module = await producerModule(key)
  if (!module?.store?.open) {
    say('插件界面未安装')
    return
  }
  await module.store.open()
}

/** 左侧音色卡的「指定 / 创建」：把这条音色交给当前生产方的工作区。
 *  `mode` 由栏的用法决定——`create` = 用这条音色新建一个角色，`assign` = 指定给已有目标。 */
/**
 * @param {any} voice
 * @param {string} [mode]
 */
export function requestAssign(voice, mode = 'assign') {
  store.assignMode = mode === 'create' ? 'create' : 'assign'
  store.assignVoice = voice
}

/** 保存插件配置：后端回掩码后的整包，直接替换清单里那一项。 */
/**
 * @param {string} key
 * @param {any} values
 */
export async function saveProducerSettings(key, values) {
  const saved = await api.saveProducer(key, values)
  const index = store.producers.findIndex((item) => item.key === key)
  if (index >= 0) store.producers[index] = saved
  return saved
}
