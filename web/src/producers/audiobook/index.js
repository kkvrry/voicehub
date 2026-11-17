// @ts-check
// 插件的前端入口：宿主按目录名找到这一份，不认识它叫什么。
// `Workspace` 是工作区（步骤条与章节栏都由它摆），`Overlay` 是全局浮层（指定音色）；
// `store.open` 在打开插件时调用一次。
import AssignOverlay from './AssignOverlay.vue'
import Workspace from './Workspace.vue'
import * as store from './store'

export { AssignOverlay as Overlay, Workspace, store }
