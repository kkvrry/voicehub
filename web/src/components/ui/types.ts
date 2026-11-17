// 公共控件类型（从 .vue 里导出类型会让引用方依赖 SFC 编译器，单独放一个文件更稳）。
import type { Component } from 'vue'

export interface SegmentOption {
  value: string | number
  label: string
  icon?: Component
  title?: string
  disabled?: boolean
}
