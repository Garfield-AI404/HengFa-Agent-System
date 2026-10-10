import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Vitest 单元测试配置：jsdom 环境 + React 插件 + jest-dom 匹配器。
// 用例位于 tests/unit/，与 Playwright e2e（tests 目录之外）分离。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/unit/**/*.{test,spec}.{ts,tsx}', 'tests/integration/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
})
