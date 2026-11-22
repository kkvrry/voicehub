// @ts-check
// 创作页专属右键菜单。宿主目录只留通用对象，插件打开时自己注册。
import {
  Combine,
  MessageSquare,
  Pencil,
  Play,
  RefreshCw,
  Scissors,
  SlidersHorizontal,
  Sparkles,
  Trash2,
} from 'lucide-vue-next'
import { copyItem } from '../../menus/shared'
import { registerMenus } from '../../menus/catalog'

/** @type {Record<string, (ctx: any) => any[][]>} */
const menus = {
  'audiobook.row': (ctx) => {
    const first = /** @type {any[]} */ ([])
    first.push({
      key: 'play',
      label: '播放这一行',
      icon: Play,
      disabled: !ctx.hasAudio,
      reason: '这一行还没有音频',
      run: ctx.play,
    })
    first.push({
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
    })
    first.push({
      key: 'params',
      label: '行参数…',
      icon: SlidersHorizontal,
      disabled: ctx.busy || ctx.generating,
      reason: '任务进行中',
      run: ctx.params,
    })
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
    const edit = /** @type {any[]} */ ([])
    if (ctx.canSplit) {
      edit.push({ key: 'split', label: '从光标处拆分', icon: Scissors, run: ctx.split })
    }
    if (ctx.mergeLabel) {
      edit.push({ key: 'merge', label: ctx.mergeLabel, icon: Combine, run: ctx.merge })
    }
    const copy = [copyItem(ctx.text, '复制台词'), copyItem(ctx.speaker, '复制角色名')]
    const removeLabel =
      ctx.selectionCount > 1 ? `删除所选 ${ctx.selectionCount} 行` : '删除这一行'
    return [
      first,
      edit,
      copy,
      [
        {
          key: 'remove',
          label: removeLabel,
          icon: Trash2,
          danger: true,
          disabled: ctx.busy || ctx.generating,
          reason: '任务进行中',
          run: ctx.remove,
        },
      ],
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
}

registerMenus(menus)
