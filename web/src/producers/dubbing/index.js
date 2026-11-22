// 插件的前端入口：宿主按目录名找到这一份。
// `store.open` 在打开插件时调用一次（恢复上次的项目）；
// `Overlay` 接住左栏音色卡的「指定」。
import Workspace from './Workspace.vue'
import Overlay from './Overlay.vue'
import * as store from './store'
import './menu'

export { Workspace, Overlay, store }
