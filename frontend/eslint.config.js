import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'

// ESLint 9+/10 扁平配置：基础 recommended 规则 + typescript-eslint recommended
// + react-hooks recommended（exhaustive-deps / rules-of-hooks）。
export default tseslint.config(
  {
    ignores: [
      'dist',
      'node_modules',
      'coverage',
      'test-results',
      'playwright-report',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  {
    // Node 环境脚本（scripts/*.mjs、配置文件）：注册 Node 全局变量。
    files: ['**/*.{js,mjs}'],
    languageOptions: {
      globals: globals.node,
    },
  },
)
