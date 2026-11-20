import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 后端端口：与 `app/infra/config.py` 的 DEFAULT_PORT 保持一致（改端口时两处一起改）。
// 后端被指到别的端口时，起 dev server 前设一下 VOICEHUB_PORT 即可，不用改这里。
const backend = `http://127.0.0.1:${process.env.VOICEHUB_PORT || 17865}`

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': backend,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // 依赖与业务代码分开打包：依赖不随业务改动变化，升级后可复用浏览器缓存。
        // 图标单独成块：lucide 按图标拆模块，变化频率与运行时不同。
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('lucide-vue-next')) return 'icons'
          return 'vendor'
        },
      },
    },
  },
})
