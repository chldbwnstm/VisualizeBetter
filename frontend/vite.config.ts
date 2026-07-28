/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // cosmos.gl does `import it from "gl-bench"`, but gl-bench's `browser`
      // field points at a UMD bundle that exports nothing, so the bundler fails
      // on the missing default. Its ESM build has `export default GLBench` —
      // point at that. (gl-bench is cosmos's optional FPS monitor, config
      // `showFPSMonitor`.)
      'gl-bench': 'gl-bench/dist/gl-bench.module.js',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    // e2e/ and perf/ are Playwright's; their *.spec.ts match vitest's default
    // include and would otherwise be collected here and fail on Playwright-only
    // globals (test.describe.configure, async describe blocks).
    exclude: ['**/node_modules/**', '**/dist/**', 'e2e/**', 'perf/**'],
  },
})
