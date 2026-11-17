// @ts-check
/** 弹层挂在哪儿：`.app-shell` 那一层。
 *
 * 为什么不能挂 body：皮肤与令牌按 `.app-shell` 选弹层（`look-*.css` 里
 * `:root[data-look=…] .app-shell .vh-root` 这一串），`--panel` / `--border` 这套别名
 * 也只在壳上（`style.css`：强调色在壳内被覆盖，别名写在上层就取不到用户选的那一档）。
 *
 * 为什么不能就地 `document.querySelector('.app-shell')`：App 级常驻弹层（引擎清单、
 * 指定音色）在 setup 那一刻就把 `to` 定死了，而壳不保证已经进入文档——挂载顺序是
 * "App 的子节点逐个 patch"，不是"壳先落地"。退到 body 就掉皮肤，绑到一个后来才
 * 出现的壳又绑不上。所以挂载点由壳自己登记，弹层跟着这份登记走。
 *
 * 还有一条**结构前提**：整个应用只有一个壳。壳若按视图重建（例如设置页自带一份
 * `AppChrome`），旧元素被丢掉之后，绑在它上面的弹层会画进一棵脱离文档的树——点按钮
 * 没反应，只有刷新页面才恢复。设置页现在是壳内的一屏，见 `App.vue`。
 */
import { computed, shallowRef } from 'vue'

//: 当前壳的根元素；空 = 不在应用里（设置页、或壳还没挂上）。
const host = shallowRef(/** @type {HTMLElement | null} */ (null))

/** `AppChrome` 用：挂上时登记根元素。 */
/** @param {HTMLElement | null} el */
export function setShellHost(el) {
  host.value = el || null
}

/** `AppChrome` 卸载时收回登记。只收回**自己那一棵**——换页时新壳可能已经挂上，
 *  一刀抹成 null 会把刚登记的挂载点一起丢掉。 */
/** @param {HTMLElement | null} el */
export function releaseShellHost(el) {
  if (!el || host.value === el) host.value = null
}

/** 常驻弹层用它当 `:to`：壳在就挂壳里，壳不在就退回 body（总比画不出来强）。 */
export const overlayTarget = computed(() => host.value || document.body)
