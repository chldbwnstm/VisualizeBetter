import { defineConfig } from '@playwright/test'

/**
 * [15] 성능 측정 harness — separate from the functional e2e suite on purpose.
 *
 * Kept out of `npx playwright test`, and out of CI, for a reason that has not
 * changed: every number here is decided by the GPU in the machine, and GPU
 * throughput varies by an order of magnitude across machines. A [15] verdict on a
 * runner with no GPU would fail where nothing is wrong — so the functional suite
 * is the gate ([13-B] CH2(5) added it to CI) and this one is run deliberately.
 *
 * ★ What did change: the [15] verdicts inside these specs are real `expect.soft`
 * assertions now, not strings printed into a console (see `perf/kpi.ts`). Running
 * this by hand now *tells you* when a target was missed instead of leaving it to
 * be spotted in the log.
 *
 *   uv run visualizebetter serve --port 8790 --no-open --data-dir .perfdata
 *   npx playwright test -c playwright.perf.config.ts
 *
 * ★ `--data-dir` is not optional: these specs call `clear_all`, and without it
 * serve opens the real user store. See docs/benchmarks.md for the full procedure
 * (the 100K fixture has to live in that directory anyway).
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
