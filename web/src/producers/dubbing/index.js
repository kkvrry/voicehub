// 插件的前端入口：宿主按目录名找到这一份。
// `store.open` 在打开插件时调用一次（恢复上次的项目）；没有 Overlay 与 Rail。
import Workspace from './Workspace.vue'
import * as store from './store'

export { Workspace, store }
