import { defineConfig } from '@playwright/test'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

/**
 * Real-browser E2E ([23-F] TASK 7 완료검증).
 *
 * Runs against a real `visualizebetter serve`: the SPA is the built dist, the WebSocket
 * is real, and cosmos.gl gets an actual WebGL context. jsdom cannot check any of
 * that — a canvas that never paints would pass every unit test.
 *
 *   cd frontend && npm run build && npx playwright test
 *
 * ★ [13-B] CH2(5): Playwright starts the server itself now. Two reasons.
 *
 * 1. This suite is a **CI gate** (`.github/workflows/ci.yml`). A gate whose setup
 *    lives in a task report is not a gate — it has to be one command.
 * 2. The previous instructions were `serve --port 8790 --no-open`, with no
 *    `--data-dir`. That opens the **real user store** (%LOCALAPPDATA%/visualizebetter
 *    or the XDG equivalent) — and this suite calls `clear_all` in `beforeEach`.
 *    Running the docs as written wiped whatever graph the developer had. The
 *    server now gets a throwaway directory under the OS temp dir.
 *
 * `npm run build` still has to have run: serve mounts dist at / ([9-A]) and
 * without it the SPA is a 503 build-instructions page.
 *
 * Point at an already-running server with VISUALIZEBETTER_URL — the managed
 * server is skipped entirely then, since that URL may not be ours to start.
 */
const externalUrl = process.env.VISUALIZEBETTER_URL
const port = 8790
// Deliberately not mkdtemp: one stable throwaway directory keeps repeat runs from
// littering temp, and the suite clears the graph itself between tests anyway.
const dataDir = process.env.VISUALIZEBETTER_E2E_DATA ?? join(tmpdir(), 'visualizebetter-e2e')

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
    baseURL: externalUrl ?? `http://127.0.0.1:${port}`,
    headless: true,
  },
  webServer: externalUrl
    ? undefined
    : {
        command: `uv run visualizebetter serve --port ${port} --no-open --data-dir "${dataDir}"`,
        cwd: '..',
        // /graph.json, not /: the SPA mount is what the suite is here to check, so
        // readiness must not depend on it. A missing dist should fail a test with
        // the 503's own explanation, not time out as "server never came up".
        url: `http://127.0.0.1:${port}/graph.json`,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        stdout: 'pipe',
        stderr: 'pipe',
      },
})
