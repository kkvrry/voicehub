<template>
  <!-- 值与角标都按 `values` 走；候选与预设由调用方给（规范只说"这一格是什么"，
       引擎才知道"可以填什么"）。 -->
  <div class="field-grid">
    <div
      v-for="field in fieldList"
      :key="field.key"
      class="field"
      :class="{ 'field-wide': isWide(field) }"
    >
      <span class="field-label">
        {{ field.label }}
        <span v-if="field.unit" class="field-unit">{{ field.unit }}</span>
      </span>
      <!-- voice / reference 必须由调用方给插槽（各要一份名单）；其余照兜底画，
           新加一项参数这里自动就有。 -->
      <slot :name="`w-${field.widget}`" :field="field">
        <!-- 数值格＝滑块＋读数：不必先手打一个数字才能调，拖动即微调。 -->
        <div v-if="field.widget === 'number'" class="field-range-row">
          <input
            type="range"
            class="field-range"
            :min="field.min"
            :max="field.max"
            :step="field.step"
            :value="shownValue(field)"
            :aria-label="field.label"
            @input="onRange(field, $event)"
          />
          <span class="field-readout">{{ shownValue(field) }}</span>
          <!-- 没动过：滑块停在默认位置、挂「默认」角标，这一格不参与请求；
               动过：角标换成归位按钮。 -->
          <button
            v-if="!isDefault(field)"
            type="button"
            class="field-reset"
            :title="`${field.label}恢复默认值`"
            @click="emit('update', field.key, '')"
          >
            <RotateCcw :size="12" :stroke-width="1.8" />
          </button>
          <span v-else class="field-badge">默认</span>
        </div>
        <!-- 种子：填一个数，或者要随机——不是滑块（0 到 21 亿的行程停不到想要的数）。
             `-1` 是"随机"的约定，由插件在发出去之前抽一个真的种子并报回来。 -->
        <div v-else-if="field.widget === 'seed'" class="field-seed">
          <input
            type="number"
            class="field-input"
            :min="field.min"
            :max="field.max"
            :step="field.step"
            :value="raw(field) ?? ''"
            placeholder="留空 = 由引擎决定"
            :aria-label="field.label"
            @change="onSeed(field, $event)"
          />
          <button
            type="button"
            class="field-btn"
            title="每次生成都不一样（这一格发 -1，引擎那边抽一个真的种子）"
            @click="emit('update', field.key, -1)"
          >
            随机
          </button>
          <!-- 状态角标与归位：填了 -1 标「随机」，没填过标「默认」，填了具体的数不挂。
               归位按钮与角标是两件事，不能写成 else 链——否则填了 -1 时归位按钮
               会被角标占位吃掉，"点了随机之后回不去"。 -->
          <span v-if="raw(field) === -1" class="field-badge">随机</span>
          <span v-else-if="isDefault(field)" class="field-badge">默认</span>
          <button
            v-if="!isDefault(field)"
            type="button"
            class="field-reset"
            :title="`${field.label}恢复默认值`"
            @click="emit('update', field.key, '')"
          >
            <RotateCcw :size="12" :stroke-width="1.8" />
          </button>
        </div>
        <!-- 候选 + 手输：候选 = 规范自带的 + 选中引擎声明的，在这里合成一份。 -->
        <ComboField
          v-else-if="field.widget === 'combo'"
          :field="field"
          :rows="rows(field)"
          :value="raw(field)"
          @update="(key, value) => emit('update', key, value)"
        />
        <!-- 长文本（语音引导）：预设摆成常驻的一排而不是下拉——
             "有哪些说法"看一眼就知道最有用，藏进下拉要先想到去点它。 -->
        <div v-else-if="field.widget === 'textarea'" class="field-area">
          <div v-if="presets[field.key]?.length" class="field-presets">
            <button
              v-for="item in presets[field.key]"
              :key="item.label"
              type="button"
              class="tag-chip"
              :class="{ active: raw(field) === item.text }"
              :title="item.text"
              @click="emit('update', field.key, raw(field) === item.text ? '' : item.text)"
            >
              {{ item.label }}
            </button>
          </div>
          <textarea
            class="field-textarea"
            :value="raw(field) ?? ''"
            :placeholder="field.placeholder || ''"
            :aria-label="field.label"
            @input="onUpdate(field.key, $event)"
          ></textarea>
        </div>
        <!-- 有候选的一律是下拉（候选不是白名单那条只对 combo 成立）。
             候选同样合成一份。绑 `?? ''` 而不是 v-model：没填过时值是 undefined，
             浏览器会"一项都没选中"，框里一片空白。 -->
        <select
          v-else-if="rows(field).length"
          class="filter-select"
          :value="raw(field) ?? ''"
          :aria-label="field.label"
          @change="onUpdate(field.key, $event)"
        >
          <option v-for="pair in rows(field)" :key="pair[0]" :value="pair[0]">
            {{ pair[1] }}
          </option>
        </select>
        <input
          v-else
          class="field-input"
          :value="raw(field) ?? ''"
          :aria-label="field.label"
          @change="onUpdate(field.key, $event)"
        />
      </slot>
    </div>
  </div>
</template>

<script setup>
// @ts-check
import { computed } from 'vue'
import { RotateCcw } from 'lucide-vue-next'
/** 规范里那些参数格的唯一画法（生成页与「参数设置」窗口共用）。
 *
 * 值不进这个组件：改动 emit 出去（update(key, value)），存哪由调用方决定；
 * 空串是"没要求"，由调用方组装请求体。
 *
 * 两样东西不来自规范、由调用方给：`extraOptions`（引擎声明的候选）、
 * `presets`（某一格的现成说法）。
 */
import ComboField from './ComboField.vue'

const props = defineProps({
  fields: { type: Array, required: true },
  // 见下方 fieldList：Array 型 prop 在类型上只是 unknown[]。
  values: { type: Object, required: true },
  //: ``{参数键: [值, …]}``——引擎声明的候选，与规范自带的合起来才是那一格的全部候选。
  extraOptions: { type: Object, default: () => ({}) },
  //: ``{参数键: [{label, text}, …]}``——某一格的现成说法（长文本那一格用）。
  presets: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update'])

// Array 型 prop 推出来是 unknown[]；按真实形状收窄一次。
const fieldList = computed(() => /** @type {any[]} */ (props.fields))

/**
 * 文本 / 下拉 / 数字都从这一条走：模板里直接取 `$event.target.value` 在类型上
 * 是 EventTarget，取不到 value。
 * @param {string} key
 * @param {Event} event
 */
function onUpdate(key, event) {
  emit('update', key, /** @type {HTMLInputElement} */ (event.target).value)
}

/**
 * @param {any} field
 */
const raw = (field) => props.values[field.key]

/**
 * 没填过（空串 / 压根没这个键）＝ **这一格不参与请求**（"空着 = 没要求"）。
 *
 * @param {any} field
 */
function isDefault(field) {
  const value = raw(field)
  return value === '' || value === undefined || value === null
}

/**
 * 滑块要显示的数：没动过就停在该项的默认位置，动过就显示动到哪儿了。
 *
 * @param {any} field
 */
function shownValue(field) {
  if (!isDefault(field)) return raw(field)
  return field.default ?? field.min ?? 0
}

/**
 * 一行的宽度：长文本与没有候选的格子占一整行，有候选的挤在网格里。
 *
 * @param {any} field
 */
const isWide = (field) => field.widget === 'textarea' || !field.options.length

/**
 * 这一格的候选 = 规范自带的 + 引擎声明的（值去重，先出现的说了算）。
 *
 * @param {any} field
 */
function rows(field) {
  const pairs = [...field.options]
  const seen = new Set(pairs.map(/** @param {any} pair */ (pair) => pair[0]))
  for (const value of props.extraOptions[field.key] || []) {
    if (seen.has(value)) continue
    seen.add(value)
    pairs.push([value, value])
  }
  return pairs
}

/**
 * @param {any} field
 * @param {any} event
 */
function onRange(field, event) {
  emit('update', field.key, Number(event.target.value))
}

/**
 * 种子那一格：读进来是字符串，发出去要是一个数。清空 = 回到"没要求"，
 * 与"填了 0"是两回事，空串原样送出去、不拿 0 补上。
 *
 * @param {any} field
 * @param {any} event
 */
function onSeed(field, event) {
  const raw = event.target.value
  if (raw === '') return emit('update', field.key, '')
  const value = Number(raw)
  emit('update', field.key, Number.isFinite(value) ? value : '')
}
</script>
