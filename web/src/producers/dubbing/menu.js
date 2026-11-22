// @ts-check
// 配音段落专属右键菜单；打开配音插件时注册到宿主唯一菜单目录。
import { Merge, Play, RefreshCw, Scissors, Trash2 } from 'lucide-vue-next'
import { registerMenus } from '../../menus/catalog'

/** @type {Record<string, (ctx: any) => any[][]>} */
const menus = {
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
      {
        key: 'synth',
        label: '重配这一段',
        icon: RefreshCw,
        disabled: !ctx.canSynth,
        reason: ctx.synthReason || '这一段不需要重新配音',
        run: ctx.synth,
      },
    ],
    [
      { key: 'split', label: '在播放头拆分', icon: Scissors, run: ctx.split },
      { key: 'merge', label: '与下一段合并', icon: Merge, run: ctx.merge },
    ],
    [{ key: 'remove', label: '删除该段', icon: Trash2, danger: true, run: ctx.remove }],
  ],
}

registerMenus(menus)
