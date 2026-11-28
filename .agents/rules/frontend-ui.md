---
trigger: model_decision
description: >-
  修改 web/ 前端页面、组件、样式主题、Store 状态、轮询/SSE、浏览器录音或界面文案时激活。
---

# 前端与界面规范

完整设计细节见 [docs/design.md](../../docs/design.md) §11。开发与评审硬规则：

## 1. 令牌、组件与图标

- 颜色、圆角、间距仅引用 `--sh-*` 变量，不写十六进制色值或 `var(--x, #hex)` 兜底（主题/外观层与插件图标 SVG 除外）。
- 优先使用 `web/src/components/ui/` 公共控件；原生控件仅用 `.sh-btn` / `.sh-field`，不新增第三套类名。
- 图标只用 `lucide-vue-next`（尺寸 12 / 14 / 16 / 18 / 20 / 24，描边 1.8）；图标按钮必须带 `title` 与 `aria-label`。
- 焦点态单层：控件自绘 ring + `outline: none`；大列表保持分帧挂载与 `content-visibility: auto`。

## 2. 壳、弹层与菜单

- 唯一壳 `AppChrome`；弹层只走 `<Teleport :to="overlayTarget">`，键盘与焦点复用 `composables/modalA11y.js`。
- 右键菜单只有一份：`ui/ContextMenu.vue` + `menus/catalog.js`；生产方专属对象由插件调用 `registerMenus()` 注册，宿主不写插件名分支。
- 生产方工作区由 `voice_rail` / `rail_mode` / `nav_section` 自描述；`voice_rail=false` 时一并隐藏左栏音色卡片、结构树与搜索。

## 3. 状态、轮询与文案

- 启停或保存引擎设置后，`store/engine.js` 必须重新拉取语气词分组，不得沿用会话缓存。
- 长操作立即显示状态与进度；轮询与 SSE 日志流须带退避、上限及组件卸载清理。
- 并发取 `min(设置, max_concurrency)`；多引擎批另有总闸。
- 文案简洁专业、结论优先；不写口语、免责自述与界面自身形态说明，映射用 `=`。修改界面文案前先 grep `scripts/shot.mjs` 与 `scripts/smoke.py` 同步更新。

## 4. 类型与验证

- 新增前端逻辑模块优先 `.ts` 与 `<script setup lang="ts">`；延续或修改 JS 模块时保持 `// @ts-check`。禁止关闭检查或堆 `any`。
- 涉及界面样式或主题改动时，在 `npm run typecheck && npm run build` 之外运行 `scripts/shot.mjs` 抽检受影响的外观（`classic / paper / celadon / glass / stage`）与浅/深色主题。
