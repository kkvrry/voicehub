// @ts-check
// 「引擎」这一页：两组插件同住一页——TTS（文本 → 音频）与对话（消息 → 消息）。
// 它们只在**怎么被调用**上不同，装/配/自检的形状是同一套；所以入口只有一个，
// 具体画哪一块由 `pluginKind` 决定。加一家插件不用改这里。
import { store } from './state'
import { loadEngines, openEngine } from './engine'
import { loadLlm, openLlm } from './llm'

/**
 * 从导航进「引擎」页时落在哪一组。
 * 记住上次看的那一组：常用对话模型的人不该每次都先被丢进 TTS 引擎。
 *
 * 判据是**已启用**的引擎，不是树里的行：树里还有「已停用」与「已卸载」两种行，
 * 它们都不算在用。一台都没启用时停在引擎页的空态——那里有「引擎清单」，
 * 不要因为"没东西可看"就跳去对话模型，那会让人以为引擎页坏了。
 */
export async function openPluginHub() {
  if (store.view === 'engine') return

  if (!store.engines.length) await loadEngines().catch(() => {})

  if (store.pluginKind === 'llm') {
    if (!store.llm.providers.length) await loadLlm().catch(() => {})
    if (store.llm.providers.length) {
      await openLlm(store.llm.key)
      return
    }
  }

  await openEngine(store.engines[0]?.key || '')
}
