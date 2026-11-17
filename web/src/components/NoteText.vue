<template>
  <!-- 插件写的说明怎么显示：强调要真的是强调，分段要真的是分段（小标题 + 条目）。 -->
  <div class="note-text">
    <template v-for="(block, index) in blocks" :key="index">
      <div v-if="block.head" class="note-head" v-html="render(block.head)"></div>
      <ul v-if="block.items.length" class="note-items">
        <li v-for="(item, at) in block.items" :key="at" v-html="render(item)"></li>
      </ul>
      <p v-if="block.text" class="note-para" v-html="render(block.text)"></p>
    </template>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'

const props = defineProps({
  text: { type: String, default: '' },
})

/** 说明里那一小撮标记 → HTML。
 *  这些说明是引擎自己写的，原样输出就是满屏星号。只认 `**强调**` 与 `` `代码` ``，
 *  不做通用 Markdown。
 *  先转义再替换：文本不该有 HTML 能力（哪天某台引擎从远端抄一段说明进来就是注入口）。
 */
/** @param {string} text */
function render(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/``([^`]+)``/g, '<code>$1</code>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

/** 一整段说明 → 「小标题 / 条目 / 正文」三种块。
 *
 *  规则是从**引擎自己写下来的形状**里读出来的（两台引擎的 NOTES 都是这个样子）：
 *
 *  - 以 `·` 开头 → 条目（跟在上面那个小标题底下）；
 *  - 以 `：` 结尾 → 小标题（"不认的参数（…）："、"两条实操上的坑："）；
 *  - 其余 → 正文段落（一句话的说明、字段描述这类，**不许加粗**）。
 *
 *  最后那条是这次踩出来的：单行说明也当小标题的话，能力清单、字段描述、形态说明
 *  会**全部变成粗体**——满屏都是重点，等于没有重点。所以强调只给"明说自己是标题"
 *  的那种行（以冒号收尾）。
 */
const blocks = computed(() => {
  const out = []
  for (const raw of String(props.text ?? '').split('\n')) {
    const line = raw.trim()
    if (!line) continue
    if (line.startsWith('·')) {
      const item = line.replace(/^·\s*/, '')
      const last = out[out.length - 1]
      if (last && last.items) last.items.push(item)
      else out.push({ head: '', items: [item], text: '' })
      continue
    }
    if (/[：:]$/.test(line)) out.push({ head: line, items: [], text: '' })
    else out.push({ head: '', items: [], text: line })
  }
  return out
})
</script>
