// @ts-check
// 生产方界面插件的注册表。
//
// 目录约定：`web/src/producers/<key>/index.js` 导出 `{ Workspace, Overlay, store }`，
// 与后端 `app/producers/builtin/<key>/` 同名。宿主只按目录名找，不 import 任何一家；
// 懒加载让插件界面单独成 chunk，未安装/未打开时不进首屏。
const loaders = import.meta.glob('./*/index.js')

/** @type {Map<string, any>} */
const cache = new Map()

/** 已带界面的插件 key（诊断与测试用；正常流程不需要）。 */
export function producerKeys() {
  return Object.keys(loaders).map((path) => path.split('/')[1])
}

/** 取一个插件的界面模块；没有对应目录时返回 null。 */
/** @param {string} key */
export async function producerModule(key) {
  if (!key) return null
  if (cache.has(key)) return cache.get(key)
  const load = loaders[`./${key}/index.js`]
  if (!load) return null
  const module = await load()
  cache.set(key, module)
  return module
}
