// @ts-check
// 右键菜单目录：对象类型 → 菜单项。组件只传上下文与动作，
// 文案、图标、顺序与危险色集中在这里，各处叫同一个名字。
import {
  AudioLines,
  Check,
  Combine,
  Copy,
  Eye,
  EyeOff,
  Filter,
  Focus,
  FolderOpen,
  ImagePlus,
  ListMusic,
  Merge,
  MessageSquare,
  Mic,
  Pencil,
  Play,
  Power,
  PowerOff,
  RefreshCw,
  Scissors,
  SlidersHorizontal,
  Sparkles,
  Square,
  Trash2,
  X,
} from 'lucide-vue-next'
import { say } from '../store'

/**
 * @typedef {import('../composables/contextMenu').MenuItem} MenuItem
 */

/** @param {string} text @param {string} label */
function copyItem(text, label) {
  const value = String(text || '').trim()
  if (!value) return null
  return {
    key: 'copy',
    label,
    icon: Copy,
    run: () => copyText(value),
  }
}

/** @param {string} text */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    say('已复制', { kind: 'success' })
    return
  } catch {
    //  clipboard 只在安全上下文可用；本地服务正常走上一支。
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  try {
    document.execCommand('copy')
    say('已复制', { kind: 'success' })
  } catch {
    say('复制失败')
  } finally {
    area.remove()
  }
}

/** @type {Record<string, (ctx: any) => (MenuItem | null)[][]>} */
const MENUS = {
  'voice.card': (ctx) => {
    const manage = /** @type {(MenuItem | null)[]} */ ([
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
      [copyItem(ctx.name, '复制音色名')],
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

  'audiobook.row': (ctx) => {
    const first = /** @type {MenuItem[]} */ ([
      {
        key: 'play',
        label: '播放这一行',
        icon: Play,
        disabled: !ctx.hasAudio,
        reason: '这一行还没有音频',
        run: ctx.play,
      },
      {
        key: 'generate',
        label:
          ctx.status === 'failed'
            ? '重试这一行'
            : ctx.status === 'audio' || ctx.status === 'stale'
              ? '重新生成'
              : '生成这一行',
        icon: ctx.status === 'failed' ? RefreshCw : Sparkles,
        disabled: ctx.busy || ctx.generating,
        reason: ctx.generating ? '正在生成这一行' : '任务进行中',
        run: ctx.generate,
      },
      {
        key: 'params',
        label: '行参数…',
        icon: SlidersHorizontal,
        disabled: ctx.busy || ctx.generating,
        reason: '任务进行中',
        run: ctx.params,
      },
    ])
    if (ctx.hasCaret) {
      const marks = ctx.marks || []
      first.push({
        key: 'marks',
        label: '插入语气词',
        icon: MessageSquare,
        disabled: !marks.length,
        reason: ctx.marksReason || '这一行没有可用的语气词',
        children: marks.length
          ? marks.map((/** @type {any} */ mark) => ({
              key: `mark:${mark.name}`,
              label: mark.label,
              run: () => ctx.insertMark(mark.name),
            }))
          : undefined,
      })
    }
    const edit = /** @type {MenuItem[]} */ ([])
    if (ctx.canSplit) {
      edit.push({ key: 'split', label: '从光标处拆分', icon: Scissors, run: ctx.split })
    }
    if (ctx.mergeLabel) {
      edit.push({ key: 'merge', label: ctx.mergeLabel, icon: Combine, run: ctx.merge })
    }
    const copy = /** @type {(MenuItem | null)[]} */ ([
      copyItem(ctx.text, '复制台词'),
      copyItem(ctx.speaker, '复制角色名'),
    ])
    const removeLabel =
      ctx.selectionCount > 1 ? `删除所选 ${ctx.selectionCount} 行` : '删除这一行'
    return [
      first,
      edit,
      copy,
      [{ key: 'remove', label: removeLabel, icon: Trash2, danger: true, disabled: ctx.busy || ctx.generating, reason: '任务进行中', run: ctx.remove }],
    ]
  },

  'audiobook.chapter': (ctx) => [
    [
      {
        key: 'play',
        label: '播放本章',
        icon: Play,
        disabled: !ctx.hasAudio,
        reason: '这一章还没有成品音频',
        run: ctx.play,
      },
    ],
    [
      { key: 'rename', label: '重命名章节', icon: Pencil, run: ctx.rename },
      copyItem(ctx.title, '复制章名'),
    ],
    [{ key: 'remove', label: '删除本章', icon: Trash2, danger: true, run: ctx.remove }],
  ],

  'voiceclean.tab': (ctx) => [
    [
      {
        key: 'play',
        label: '顺序试听这张卡',
        icon: Play,
        disabled: !ctx.hasAudio,
        reason: '这条音色没有可播的样例',
        run: ctx.play,
      },
      {
        key: 'close',
        label: '移出处理序列',
        icon: X,
        disabled: ctx.locked,
        reason: '正在处理中',
        run: ctx.close,
      },
    ],
    [copyItem(ctx.name, '复制音色名')],
  ],

  'voiceclean.row': (ctx) => {
    const play = [
      {
        key: 'play',
        label: ctx.playing ? '停止试听' : '试听原声',
        icon: ctx.playing ? Square : Play,
        disabled: !ctx.hasAudio,
        reason: '这一条没有可播放的音频',
        run: ctx.play,
      },
    ]
    const ab = /** @type {MenuItem[]} */ (
      ctx.canCompare
        ? [
            { key: 'ab-origin', label: '播放原声', icon: Play, run: ctx.playOrigin },
            { key: 'ab-result', label: '播放处理结果', icon: AudioLines, run: ctx.playResult },
          ]
        : []
    )
    const ops = [
      {
        key: 'only',
        label: '只处理这张卡',
        icon: Focus,
        disabled: ctx.locked || !ctx.canOnly,
        reason: ctx.canOnly ? '正在处理中' : '序列里只有这一张卡',
        run: ctx.only,
      },
      {
        key: 'trim',
        label: ctx.isTrim ? '收起裁剪' : '裁剪',
        icon: Scissors,
        disabled: ctx.trimDisabled,
        reason: ctx.trimReason,
        run: ctx.trim,
      },
    ]
    return [
      play,
      ab,
      ops,
      [
        copyItem(ctx.fileName, '复制文件名'),
        {
          key: 'remove',
          label: ctx.removeLabel,
          icon: X,
          disabled: ctx.locked,
          reason: '正在处理中',
          run: ctx.remove,
        },
      ],
    ]
  },

  'dubbing.cue': (ctx) => [
    [
      {
        key: 'play',
        label: '播放该段',
        icon: Play,
        disabled: !ctx.hasAudio,
        reason: '这一句还没有音频',
        run: ctx.play,
      },
      { key: 'split', label: '在播放头拆分', icon: Scissors, run: ctx.split },
      { key: 'merge', label: '与下一段合并', icon: Merge, run: ctx.merge },
    ],
    [{ key: 'remove', label: '删除该段', icon: Trash2, danger: true, run: ctx.remove }],
  ],

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
  const builder = MENUS[type]
  if (!builder) return []
  return builder(context || {}).map((group) =>
    /** @type {MenuItem[]} */ (group.filter(Boolean))
  )
}