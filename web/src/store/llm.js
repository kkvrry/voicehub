// @ts-check
// 对话模型这一域：插件清单、配置的存/恢复默认、默认模型、连通自检、平台模型清单。
// 与 engine.js 同形——前端不认识任何一家模型，中文说法全在后端。
import { api } from '../api'
import { store } from './state'
import { say } from './shell'

/** 拉全部对话模型（已掩码）。没装插件时是空表，不是错误。 */
export async function loadLlm() {
  const providers = await api.llmProviders()
  store.llm.providers = providers
  const known = providers.some((item) => item.key === store.llm.key)
  if (!known) {
    // 选谁：默认那家 → 第一家就绪的 → 第一家。后端已算好 `is_default`。
    const fallback =
      providers.find((item) => item.is_default) ||
      providers.find((item) => item.ready) ||
      providers[0]
    store.llm.key = fallback?.key || ''
  }
  return providers
}

export function llmOf(key = store.llm.key) {
  return store.llm.providers.find((item) => item.key === key) || null
}

/** 当前看的那家（插件的说明文字挂在它上面）。 */
export function currentLlm() {
  return llmOf(store.llm.key)
}

/** 打开对话模型的面板：「引擎」页右栏的另一组（与 TTS 引擎并列，不是另一页）。 */
export async function openLlm(key = '') {
  store.view = 'engine'
  store.pluginKind = 'llm'
  if (key) store.llm.key = key
  await Promise.allSettled([loadLlm(), loadLlmModels()])
}

/**
 * 保存配置。``_clear`` / ``_reset`` 这两个框架指令由调用方决定要不要带。
 * @param {string} key
 * @param {any} values
 */
export async function saveLlmSettings(key, values) {
  const saved = await api.saveLlmSettings(key, values)
  await loadLlm()
  say(saved.ready ? `「${saved.label}」已保存` : `已保存——${saved.note}`)
  return saved
}

/** @param {string} key */
export async function resetLlmSettings(key) {
  const saved = await api.saveLlmSettings(key, { _reset: true })
  await loadLlm()
  say(`「${saved.label}」已恢复默认（密钥保留）`)
  return saved
}

/**
 * 选默认模型：之后所有不带 ``provider`` 的调用都落到它。
 * @param {string} key
 */
export async function setDefaultLlm(key) {
  const result = await api.setDefaultLlm(key)
  await loadLlm()
  say(`默认模型：${llmOf(result.key)?.label || result.key || '自动'}`)
  return result
}

/**
 * 连通自检：失败也是业务结果（后端给 ok:false + 原话），由调用方弹一句。
 * @param {string} key
 */
export async function testLlm(key) {
  store.llm.testing = true
  try {
    return await api.testLlm(key)
  } finally {
    store.llm.testing = false
  }
}

/**
 * 平台模型清单。拉不到把理由留在 ``modelsReason``，不弹 toast（那是这一块的说明）。
 * @param {string} [key]
 */
export async function loadLlmModels(key = store.llm.key) {
  store.llm.models = []
  store.llm.modelsReason = ''
  if (!key) return
  try {
    const result = await api.llmModels(key)
    store.llm.models = result.items || []
    store.llm.modelsReason = result.reason || ''
  } catch (error) {
    store.llm.modelsReason = error instanceof Error ? error.message : String(error)
  }
}

/**
 * 给其他页面用的一次补全：不带 `provider` 就落到默认模型——与后端
 * `POST /llm/chat` 的口径一致，调用方不需要知道装的是哪一家。
 */
/**
 * @param {any[]} messages
 * @param {Record<string, any>} [options]
 */
export async function chatLlm(messages, options = {}) {
  return api.llmChat({ ...options, messages })
}
