# 素材来源

## 立绘（`portraits/*.webp`）

十一张「大模型拟人」立绘由项目自备，素材采集自哔哩哔哩 UP 主主页：

<https://space.bilibili.com/4168597>

立绘的抠图（去白底、保留透明通道）由项目作者自己完成；本仓库只做了尺寸与格式整理：

- 原稿 3072×5504 的五张（chatgpt / claude / gemini / qwen / glm-wheelchair）：裁掉透明边、缩到高 2400、WebP q92；
- 1152×2048 及以下的三张（copilot / kimi / minimax / glm）：原文件字节原样复制，未重编码；
- deepseek：PNG 转 WebP q95，分辨率不变；
- claude-chibi（1505×2675）：裁掉透明边、缩到高 2400、WebP q92。

其中 `claude-chibi`（Claude 的 Q 版）与 `glm-wheelchair`（GLM 的轮椅版）是同一位画手的备选稿，
和其余九张一样作为可选立绘随插件分发。

版权归原作者所有，这里只作为「角色对话」内置的可选立绘随插件分发；
二次使用或再分发请遵守原作者的授权说明。

## 背景（`bg-*.jpg`）

四张房间背景（书房 / 雨夜 / 晨光 / 霓虹）为本插件专门生成的原创图，
生成后裁掉底部 120 px（去水印）并转 JPEG（质量 88），无第三方来源。