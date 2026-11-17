// @ts-check
// store 的唯一入口——组件、main.js 只认这里导出的名字。
// 约定：每个域模块导出的就是它那一面 action（私有的一律不 export），铺开即成 actions。
// 域之间直接 import 函数、允许成环：均为 `export async function` 声明，求值发生在
// 调用那一刻而非模块初始化那一刻；换成 `const fn = () => …` 会踩 TDZ。
import { labelOf, store, vocab } from './state'
import {
  applyTheme,
  initFilters,
  initTheme,
  openSettings,
  say,
  setNoticePaused,
  setThemeMode,
} from './shell'
import * as library from './library'
import * as avatar from './avatar'
import * as engine from './engine'
import * as lexicon from './lexicon'
import * as llm from './llm'
import * as producers from './producers'
import * as plugins from './plugins'
import * as synth from './synth'
import * as asr from './asr'
import * as texts from './texts'
import * as log from './log'
import * as playback from './playback'

export const actions = {
  ...library,
  ...avatar,
  ...engine,
  ...lexicon,
  ...llm,
  ...producers,
  ...plugins,
  ...synth,
  ...asr,
  ...texts,
  ...log,
  ...playback,
  openSettings,
  setThemeMode,
  setNoticePaused,
}

export { applyTheme, initFilters, initTheme, labelOf, say, setThemeMode, store, vocab }
export { visibleVoices } from './library'
export { currentLlm, llmOf } from './llm'
