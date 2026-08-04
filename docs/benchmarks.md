# Benchmarks — how the 100K numbers were measured

The README quotes performance figures at 100K nodes. This file is what makes them
checkable: the fixture generator, the exact procedure, the machine they came from,
and — importantly — what the harness does and does not guarantee.

## What the harness is

`frontend/perf/perf100k.spec.ts` is a **measurement probe that CI does not run.**

Every number here is decided by the GPU in the machine, and GPU throughput varies
by an order of magnitude across them. A CI runner has no GPU at all — it would
rasterize on SwiftShader, and that figure describes the harness rather than the
product. So this suite is run deliberately, on a machine with a GPU, and lives
behind its own Playwright config so a normal `npx playwright test` cannot pick it
up by accident.

**The targets are assertions.** Each verdict is an `expect.soft` assertion that
carries the criterion and the measured value, so a run tells you which target was
missed. They are soft on purpose: one miss does not truncate the remaining
measurements, which is what you need to see how far a regression reaches. (They
used to be strings printed to the console — a miss was reported and the run stayed
green, which meant a regression was only ever caught by someone reading the log.)

The cost of keeping this out of CI is still worth naming: **a performance
regression does not turn CI red.** Catching one means running this and reading the
failures. What CI does gate is the functional end-to-end suite (`frontend/e2e/`),
which runs headless on the runner's software renderer and checks that the app
works — not how fast it is.

## Reproducing

Requires the Quick start prerequisites (Python 3.11+, uv, Node 22) and a machine
with a working WebGL 2 context.

```bash
# 1. build the SPA — serve mounts it at / and the probe drives the real UI
cd frontend && npm ci && npm run build && cd ..

# 2. generate the fixture (deterministic — same seed, same bytes)
uv run python scripts/gen_perf100k.py --out perf100k.json

# 3. put it in the data directory the server will use
#    (any directory; pass the same one to serve)
mkdir -p .perfdata && mv perf100k.json .perfdata/

# 4. start the server against that directory
#    --data-dir is not optional: the probe calls clear_all, and without it the
#    server opens your real store and the probe wipes it.
uv run visualizebetter serve --port 8790 --no-open --data-dir .perfdata

# 5. in another shell, run the probe
cd frontend
npx playwright test -c playwright.perf.config.ts perf100k.spec --reporter=list
```

The port and the config both matter. The harness talks to `127.0.0.1:8790` unless
`VISUALIZEBETTER_URL` says otherwise, and the perf specs live outside the default
config's `testDir` — `npx playwright test perf/perf100k.spec.ts` finds no tests at
all.

The spec imports the fixture through the same `import_from_file` path a user's
bulk import takes, then measures the render from the `/graph.json` resync — the
route a real session actually follows, not a synthetic in-page load.

Smaller runs are useful while iterating:

```bash
uv run python scripts/gen_perf100k.py --nodes 10000 --edges 20000 --out small.json
```

The generated graph is skewed rather than uniform — a few hub nodes carry a large
share of the edges, most nodes have two or three. Layout and rendering cost depend
on that skew, so a uniform random graph would measure something the product never
draws.

## Measured

| | |
|---|---|
| CPU / GPU | NVIDIA RTX 4070 SUPER |
| OS | Windows 11 |
| Browser | Chromium via Playwright, `--use-angle=d3d11` |
| Graph | 100,000 nodes / 200,000 edges (generator defaults, seed 20260729) |

| Metric | Target | Measured |
|---|---|---|
| Static render, pan/zoom | ≥ 30 FPS | **59.9 FPS** median |
| Bulk import (100K) | — | **9.3 s** |
| Live push → on screen | < 100 ms | **73.9 ms** median |

These are one machine's numbers. They are quoted in the README as measured
figures because they were measured, not because they are a promise about yours —
which is the reason this page exists rather than a bare claim.

### Open: a re-measurement disagrees (2026-08-05)

Re-running the procedure above on the same machine and the same fixture seed, on
the day the targets became assertions, did not reproduce two of these rows:

| Metric | Recorded above | Re-measured (4 runs) |
|---|---|---|
| Static render, pan/zoom | 59.9 FPS | 59.9 FPS — reproduces exactly |
| Bulk import (100K) | 9.3 s | 12.6 – 14.5 s |
| Live push → on screen | 73.9 ms | 119.4 / 120.5 / 126.1 / 133.6 ms |

So the live-push row misses its < 100 ms target, and the perf suite now says so
out loud instead of printing it. The gap is not an artifact of a full snapshot
store — a clean data directory gives 119.4 ms — and the accompanying diagnostic
spec puts most of it in the client leg (server 32 ms vs client 88 ms), which grows
with graph size (9.3× from 100 nodes to 100K).

This is recorded rather than corrected because the cause is not yet known, and a
number should not be edited to match the most recent run any more than it should
be left standing when a measurement contradicts it. The rows above are what was
measured then; the table here is what was measured now.
