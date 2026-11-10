import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前端一律请求相对路径 /api/...
// 开发期由下面的代理转发到 FastAPI（默认 127.0.0.1:8000），
// 浏览器看来是同源请求 —— 因此开发期完全不会触发 CORS。
// 后端虽然也配了 CORS_ORIGINS 兜底，但走代理时用不到。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/__tests__/**/*.spec.js'],
  },
})
