// @ts-check
// 音色处理专属右键菜单；打开处理工作区时注册到宿主唯一菜单目录。
import { AudioLines, Focus, Play, Scissors, Square, X } from 'lucide-vue-next'
import { copyItem } from '../../menus/shared'
import { registerMenus } from '../../menus/catalog'

/** @type {Record<string, (ctx: any) => any[][]>} */
const menus = {
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
    [
      copyItem(ctx.name, '复制音色名'),
      copyItem(ctx.id, '复制音色 ID', 'copy-id'),
    ],
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
    const ab = ctx.canCompare
      ? [
          { key: 'ab-origin', label: '播放原声', icon: Play, run: ctx.playOrigin },
          { key: 'ab-result', label: '播放处理结果', icon: AudioLines, run: ctx.playResult },
        ]
      : []
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
}

registerMenus(menus)
