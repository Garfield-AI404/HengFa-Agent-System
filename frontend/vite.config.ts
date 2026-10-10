import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 前端构建配置：独立 Vite 工程，产物为静态 dist。
// 开发环境通过 VITE_API_BASE_URL 访问 FastAPI（见 .env.example）。
export default defineConfig({
  plugins: [react()],
  server: {
    // 固定 IPv4 地址：保证 Playwright（baseURL 127.0.0.1）与本地工具可稳定访问
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    // 2026-08-28：dev 同源代理，cookie 跨 host 落库问题修复——
    // 请求走同源 localhost:5173/api → vite proxy → 127.0.0.1:18000，
    // 登录 Set-Cookie 落在 localhost（同站），localhost/127.0.0.1 均可用；
    // 与生产 nginx 同源反代架构一致（构建产物为相对路径 /api/v1）。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18000',
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
