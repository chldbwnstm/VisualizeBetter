import { defineConfig } from '@playwright/test'

/**
 * Real-browser E2E ([23-F] TASK 7 완료검증).
 *
 * Runs against a real `visualizebetter serve`: the SPA is the built dist, the WebSocket
 * is real, and cosmos.gl gets an actual WebGL context. jsdom cannot check any of
 * that — a canvas that never paints would pass every unit test.
 *
 * The server is started outside Playwright (see the task report) because it needs
 * uv on PATH and this machine's SSL_CERT_FILE cleared; `npm run build` must have
 * run first, since serve mounts dist at / ([9-A]).
 *
 *   uv run visualizebetter serve --port 8790 --no-open
 *   npx playwright test
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  // Arrival waits ([8-C] push → WS → paint) cross a real process boundary and a
  // GPU. 15s was tight enough that a slow round trip read as a product failure;
  // the residual flakes were all "the data never came" against a path already
  // proven correct. Waiting longer costs nothing when the assertion passes — the
  // timeout only elapses on a genuine failure.
  expect: { timeout: 30_000 },
  // One worker, one test at a time: the suite shares a single `serve` process,
  // so parallel tests contend over the same graph state.
  fullyParallel: false,
  workers: 1,
  // No retries — the suite must be honestly green ([15] "flaky > none"). The last
  // residual flake (KI-1) was a real product bug: a cosmos.gl init-race crash that,
  // absent an error boundary, unmounted the whole app. It was root-fixed (readiness
  // gate + error boundary), verified by 18 consecutive clean full-suite runs at
  // retries:0. A retry backstop here would only re-hide the next such regression.
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.VISUALIZEBETTER_URL ?? 'http://127.0.0.1:8790',
    headless: true,
  },
})
