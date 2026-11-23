// @ts-check
// 实时对话插件的前端入口。宿主按目录名找这一份；左栏用宿主那条音色栏，
// Overlay 接住卡片右端的「指定」。
import Workspace from './Workspace.vue'
import Overlay from './Overlay.vue'
import * as store from './store'

export { Workspace, Overlay, store }
