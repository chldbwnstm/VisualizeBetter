import { defineConfig } from '@playwright/test'

/**
 * [15] 성능 측정 harness — separate from the functional e2e suite on purpose.
 *
 * Kept out of `npx playwright test` so the 13-test functional gate stays fast and
 * its pass/fail meaning stays clean: these tests measure and report, they do not
 * gate on hitting a target.
 *
 *   uv run visualizebetter serve --port 8790 --no-open
 *   npx playwright test -c playwright.perf.config.ts
 */
export default defineConfig({
  testDir: './perf',
  // 10K ingest + several timed windows; nothing here is quick.
  timeout: 240_000,
  expect: { timeout: 60_000 },
  fullyParallel: false,
  workers: 1,
  // No retries, deliberately. A retried measurement reports the luckiest run,
  // which is the opposite of what this harness is for.
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.VISUALIZEBETTER_URL ?? 'http://127.0.0.1:8790',
    headless: true,
    launchOptions: {
      // ★ Without these, headless Chromium rasterizes WebGL on SwiftShader (CPU)
      // and every frame number below measures a software renderer. [15]'s premise
      // is that FPS is decided by cosmos.gl on the GPU, so a SwiftShader figure
      // would not be a measurement of the thing being claimed — it would be a
      // measurement of the harness. Verified to bind the real adapter; the suite
      // asserts it rather than trusting it.
      args: ['--use-angle=d3d11', '--ignore-gpu-blocklist'],
    },
  },
})
