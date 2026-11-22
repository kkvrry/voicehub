// @ts-check
// 右键菜单目录：对象类型 → 菜单项。组件只传上下文与动作，
// 文案、图标、顺序与危险色集中在这里，各处叫同一个名字。
import {
  AudioLines,
  Check,
  Eye,
  EyeOff,
  Filter,
  Focus,
  FolderOpen,
  ImagePlus,
  ListMusic,
  Mic,
  Play,
  Power,
  PowerOff,
  Square,
  Trash2,
} from 'lucide-vue-next'
import { copyItem } from './shared'

/**
 * @typedef {import('../composables/contextMenu').MenuItem} MenuItem
 */

/** 生产方专属菜单由各自的插件目录注册；宿主目录只留通用对象。 */
/** @type {Record<string, (ctx: any) => (MenuItem | null)[][]>} */
const EXTRA = {}

/** @param {Record<string, (ctx: any) => (MenuItem | null)[][]>} menus */
export function registerMenus(menus) {
  Object.assign(EXTRA, menus)
}

/** @type {Record<string, (ctx: any) => (MenuItem | null)[][]>} */
const MENUS = {
  'voice.card': (ctx) => {
    const manage = /** @type {(MenuItem | null)[]} */ ([
      ctx.open
        ? {
            key: 'open',
            label: '打开音色卡',
            icon: Focus,
            run: ctx.open,
          }
        : null,
      {
        key: 'play-all',
        label: '顺序播放全部样例',
        icon: ListMusic,
        disabled: Number(ctx.sampleCount) === 1,
        reason: '这条音色只有一条样例',
        run: ctx.playAll,
      },
      ctx.folder
        ? {
            key: 'folder',
            label: '在资源管理器里打开',
            icon: FolderOpen,
            run: ctx.openFolder,
          }
        : null,
      ctx.isLocal
        ? {
            key: 'avatar',
            label: ctx.avatarGenerated ? '更换头像' : '查找头像',
            icon: ImagePlus,
            run: ctx.pickAvatar,
          }
        : null,
      {
        key: 'hide',
        label: ctx.hidden ? '取消隐藏' : '隐藏',
        icon: ctx.hidden ? Eye : EyeOff,
        run: ctx.toggleHidden,
      },
    ])
    return [
      manage,
      [
        copyItem(ctx.name, '复制音色名'),
        copyItem(ctx.id, '复制音色 ID', 'copy-id'),
      ],
      [{ key: 'remove', label: '移除音色', icon: Trash2, danger: true, run: ctx.remove }],
    ]
  },

  'sample.row': (ctx) => {
    const groups = /** @type {(MenuItem | null)[][]} */ ([
      [
        {
          key: 'play',
          label: ctx.playing ? '停止播放' : '试听这一段',
          icon: ctx.playing ? Square : Play,
          disabled: !ctx.hasAudio,
          reason: '这一条没有可播放的音频',
          run: ctx.play,
        },
        {
          key: 'main',
          label: ctx.isMain ? '主样例' : '设为主样例',
          icon: Check,
          disabled: ctx.isMain,
          reason: '这一条已经是主样例',
          run: ctx.setMain,
        },
      ],
    ])
    const use = /** @type {MenuItem[]} */ ([])
    if (ctx.useLabel) {
      use.push({
        key: 'use',
        label: ctx.useLabel,
        icon: AudioLines,
        disabled: Boolean(ctx.useDisabled),
        run: ctx.use,
      })
    }
    if (ctx.canRecognize) {
      use.push({ key: 'recognize', label: '识别文字', icon: Mic, run: ctx.recognize })
    } else if (ctx.recognizing) {
      use.push({
        key: 'recognize',
        label: '识别中…',
        icon: Mic,
        disabled: true,
        run: ctx.recognize,
      })
    }
    if (use.length) groups.push(use)
    groups.push([
      copyItem(ctx.fileName, '复制文件名'),
      { key: 'remove', label: '移除样例', icon: Trash2, danger: true, run: ctx.remove },
    ])
    return groups
  },

  'log.line': (ctx) => [
    [copyItem(ctx.text, '复制这条日志')],
    [
      {
        key: 'source',
        label: '只看此来源',
        icon: Filter,
        disabled: !ctx.source,
        reason: '这条日志没有来源',
        run: ctx.onlySource,
      },
      {
        key: 'level',
        label: '只看此级别',
        icon: Filter,
        disabled: !ctx.level,
        reason: '这条日志没有级别',
        run: ctx.onlyLevel,
      },
    ],
  ],

  'engine.row': (ctx) => [
    [
      {
        key: ctx.enabled ? 'disable' : 'enable',
        label: ctx.enabled ? '停用' : '启用',
        icon: ctx.enabled ? PowerOff : Power,
        disabled: Boolean(ctx.busy),
        reason: '正在切换',
        run: ctx.toggle,
      },
    ],
  ],
}

/**
 * @param {string} type
 * @param {any} context
 * @returns {MenuItem[][]}
 */
export function buildMenu(type, context) {
  const builder = MENUS[type] || EXTRA[type]
  if (!builder) return []
  return builder(context || {}).map((group) =>
    /** @type {MenuItem[]} */ (group.filter(Boolean))
  )
}