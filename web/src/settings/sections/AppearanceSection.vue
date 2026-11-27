<template>
  <SettingsSection id="appearance" title="外观主题">
    <template #icon><Palette :size="16" :stroke-width="1.8" /></template>

    <SettingCard
      title="风格与主题"
      keywords="风格 纸墨 素瓷 青釉 瓷 玻璃 舞台 扁平 简约 视觉语言 主题 浅色 深色 跟随系统 强调色 背景 动效"
    >
      <template #icon><SunMoon :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="视觉语言"
        keywords="原色 经典 基础 扁平 简约 纸墨 素瓷 青釉 瓷 玻璃 舞台 暖灯 衬线 视觉语言"
      >
        <SegmentedControl v-model="look" :options="lookOptions" label="界面风格" />
      </SettingRow>

      <SettingRow title="主题模式" keywords="浅色 深色 深色模式 跟随系统">
        <SegmentedControl v-model="themeMode" :options="themeOptions" label="主题模式" />
      </SettingRow>

      <SettingRow
        v-if="!fixedLook"
        title="强调色"
        keywords="强调色 主色 配色 蓝色 紫色 青色"
      >
        <div class="sh-swatches" role="radiogroup" aria-label="强调色">
          <button
            v-for="item in accentOptions"
            :key="item.value"
            type="button"
            class="sh-swatch"
            role="radio"
            :aria-checked="store.appearance.accent === item.value"
            :aria-label="item.label"
            :title="item.label"
            :style="{ background: item.gradient, color: item.tint }"
            @click="store.appearance.accent = item.value"
          />
        </div>
      </SettingRow>

      <SettingRow
        v-if="!fixedLook"
        title="背景环境光"
        keywords="背景 光晕 环境光 渐变 动效"
      >
        <ToggleSwitch v-model="store.appearance.ambient" label="背景环境光" />
      </SettingRow>

      <SettingRow
        title="减弱动效"
        keywords="动效 动画 过渡 减弱 无障碍"
      >
        <ToggleSwitch v-model="store.appearance.reduceMotion" label="减弱动效" />
      </SettingRow>
    </SettingCard>

    <SettingCard :title="fixedLook ? '布局' : '质感与布局'" keywords="玻璃 毛玻璃 模糊 密度 紧凑 圆角 布局">
      <template #icon><Layers :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        v-if="!fixedLook"
        title="玻璃强度"
        keywords="毛玻璃 模糊 backdrop blur 透明度 材质"
        wide
      >
        <RangeSlider v-model="store.appearance.glass" :min="0" :max="100" label="玻璃强度" unit="%" />
      </SettingRow>

      <SettingRow
        v-if="!fixedLook"
        title="卡片圆角"
        keywords="圆角 半径 形状 卡片"
        wide
      >
        <RangeSlider v-model="store.appearance.radius" :min="10" :max="26" label="卡片圆角" unit=" px" />
      </SettingRow>

      <SettingRow title="界面密度" keywords="密度 紧凑 宽松 间距">
        <SegmentedControl v-model="density" :options="densityOptions" label="界面密度" />
      </SettingRow>

      <SettingRow
        title="侧栏自动隐藏"
        keywords="侧栏 导航 自动隐藏 收起 悬停 边缘 宽度"
      >
        <ToggleSwitch v-model="store.appearance.navAutoHide" label="侧栏自动隐藏" />
      </SettingRow>

      <SettingRow
        title="隐藏侧栏文字"
        keywords="侧栏 导航 文字 标签 图标 极简 窄"
      >
        <ToggleSwitch v-model="store.appearance.navHideLabels" label="隐藏侧栏文字" />
      </SettingRow>
    </SettingCard>

    <SettingCard title="头像显示" keywords="头像 封面 首字母 大小 形状 显示 隐藏">
      <template #icon><UserRound :size="16" :stroke-width="1.8" /></template>

      <SettingRow
        title="显示头像"
        keywords="头像 封面 图片 首字母 显示 隐藏"
      >
        <ToggleSwitch v-model="store.appearance.avatar.show" label="显示头像" />
      </SettingRow>

      <SettingRow
        title="作用范围"
        keywords="头像 范围 音色库 生成页 详情 首字母"
      >
        <div class="avatar-scopes">
          <span class="avatar-scope">
            <ToggleSwitch
              v-model="store.appearance.avatar.rail"
              label="生成页左列"
              :disabled="!store.appearance.avatar.show"
            />
            生成页左列
          </span>
          <span class="avatar-scope">
            <ToggleSwitch
              v-model="store.appearance.avatar.cards"
              label="音色库卡片"
              :disabled="!store.appearance.avatar.show"
            />
            音色库卡片
          </span>
          <span class="avatar-scope">
            <ToggleSwitch
              v-model="store.appearance.avatar.detail"
              label="详情弹窗"
              :disabled="!store.appearance.avatar.show"
            />
            详情弹窗
          </span>
        </div>
      </SettingRow>

      <SettingRow
        title="大小"
        keywords="头像 大小 尺寸"
      >
        <SegmentedControl v-model="avatarSize" :options="avatarSizeOptions" label="头像大小" />
      </SettingRow>

      <SettingRow
        title="形状"
        keywords="头像 形状 圆形 圆角 方形"
      >
        <SegmentedControl v-model="avatarShape" :options="avatarShapeOptions" label="头像形状" />
      </SettingRow>

    </SettingCard>
  </SettingsSection>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Layers, Palette, SunMoon, UserRound } from 'lucide-vue-next'
import { useSettingsStore } from '../../store/settings'
import type { AccentKey, AvatarShape, AvatarSize, Density, Look, ThemeMode } from '../../store/settings'
import type { SegmentOption } from '../types'
import SettingsSection from '../ui/SettingsSection.vue'
import SettingCard from '../ui/SettingCard.vue'
import SettingRow from '../ui/SettingRow.vue'
import ToggleSwitch from '../../components/ui/ToggleSwitch.vue'
import SegmentedControl from '../../components/ui/SegmentedControl.vue'
import RangeSlider from '../../components/ui/RangeSlider.vue'

const store = useSettingsStore()

const themeOptions: SegmentOption[] = [
  { value: 'light', label: '浅色' },
  { value: 'dark', label: '深色' },
  { value: 'system', label: '跟随系统' },
]

const lookOptions: SegmentOption[] = [
  { value: 'classic', label: '原色' },
  { value: 'paper', label: '纸墨' },
  { value: 'celadon', label: '素瓷' },
  { value: 'glass', label: '玻璃' },
  { value: 'stage', label: '舞台' },
]

const densityOptions: SegmentOption[] = [
  { value: 'compact', label: '紧凑' },
  { value: 'cozy', label: '标准' },
  { value: 'roomy', label: '宽松' },
]

const AVATAR_SIZE_OPTIONS: SegmentOption[] = [
  { value: 'auto', label: '跟随' },
  { value: 'sm', label: '小' },
  { value: 'lg', label: '大' },
]

const AVATAR_SHAPE_OPTIONS: SegmentOption[] = [
  { value: 'auto', label: '跟随' },
  { value: 'round', label: '圆形' },
  { value: 'soft', label: '圆角' },
  { value: 'square', label: '方形' },
]

const accentOptions: Array<{ value: AccentKey; label: string; gradient: string; tint: string }> = [
  {
    value: 'azure',
    label: '冰蓝',
    gradient: 'linear-gradient(135deg, #2f7bff, #7c6cff)',
    tint: 'rgba(47, 123, 255, 0.55)',
  },
  {
    value: 'indigo',
    label: '靛蓝',
    gradient: 'linear-gradient(135deg, #4b5cf5, #7a6cff)',
    tint: 'rgba(75, 92, 245, 0.55)',
  },
  {
    value: 'violet',
    label: '紫晶',
    gradient: 'linear-gradient(135deg, #7b5cf0, #b06cff)',
    tint: 'rgba(123, 92, 240, 0.55)',
  },
  {
    value: 'teal',
    label: '青碧',
    gradient: 'linear-gradient(135deg, #0f9bb8, #3fc9b0)',
    tint: 'rgba(15, 155, 184, 0.55)',
  },
]

const look = computed<Look>({
  get: () => store.appearance.look,
  set: (value) => {
    store.appearance.look = String(value) as Look
  },
})

const fixedLook = computed(() => store.appearance.look !== 'glass')

const themeMode = computed<ThemeMode>({
  get: () => store.appearance.theme,
  set: (value) => {
    store.appearance.theme = String(value) as ThemeMode
  },
})

const density = computed<Density>({
  get: () => store.appearance.density,
  set: (value) => {
    store.appearance.density = String(value) as Density
  },
})

const avatarSize = computed<AvatarSize>({
  get: () => store.appearance.avatar.size,
  set: (value) => {
    store.appearance.avatar.size = String(value) as AvatarSize
  },
})

const avatarShape = computed<AvatarShape>({
  get: () => store.appearance.avatar.shape,
  set: (value) => {
    store.appearance.avatar.shape = String(value) as AvatarShape
  },
})

const avatarSizeOptions = computed<SegmentOption[]>(() =>
  AVATAR_SIZE_OPTIONS.map((item) => ({ ...item, disabled: !store.appearance.avatar.show }))
)

const avatarShapeOptions = computed<SegmentOption[]>(() =>
  AVATAR_SHAPE_OPTIONS.map((item) => ({ ...item, disabled: !store.appearance.avatar.show }))
)
</script>

<style scoped>
.avatar-scopes {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 10px 18px;
}
.avatar-scope {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--sh-text-3);
  white-space: nowrap;
}
</style>
