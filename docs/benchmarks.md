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

### If you change the render path, run this before you call it done

Touching any of these means the push→display measurement above is no longer known
to hold, and re-running `perf100k.spec.ts` is part of finishing the work:

- `frontend/src/views/OverviewCanvas.tsx`
- `frontend/src/views/graphAdapter.ts`
- `frontend/src/views/temporal.ts`
- `frontend/src/stores/graphStore.ts`
- anything rendered on every structural change (`frontend/src/components/TemporalScrubber.tsx`
  is the cautionary example)

This is not a general "be careful" note. A feature that never touched the renderer
put a full-graph scan into the render phase, the KPI verdict at the time was a
string printed to a console, and the two together hid a 60 ms regression for three
weeks. Either half alone would have caught it. The verdicts are assertions now;
this list is the other half.

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

# 4. check nobody is already on the port. A leftover `serve` answers happily, and
#    the probe would then measure someone else's graph in an unknown data
#    directory — silently, because every request succeeds. (This has happened.)
#    Windows: netstat -ano | findstr :8790     macOS/Linux: lsof -i :8790
#    Anything listening → kill it, or pass a free port here and in
#    VISUALIZEBETTER_URL for step 5.

# 5. start the server against that directory
#    --data-dir is not optional: the probe calls clear_all, and without it the
#    server opens your real store and the probe wipes it.
uv run visualizebetter serve --port 8790 --no-open --data-dir .perfdata

# 6. in another shell, run the probe
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
| Graph (current) | 100,000 nodes / **200,000 edges**, 27.8 MB — `scripts/gen_perf100k.py` defaults, seed 20260729 |
| Graph (as originally recorded) | 100,000 nodes / **100,000 edges**, 11.9 MB — a 2-regular fixture that was never committed |

> ⚠️ **This table is the original record, kept as written.** Two of its three rows
> did not reproduce, for two different reasons — one a measurement definition, one
> a real regression since fixed. Current numbers and the full account are in
> [§ Resolved](#resolved-why-the-live-push-figure-did-not-reproduce) directly below.
> The two import rows also describe different graphs; see there.

| Metric | Target | Measured (as recorded) |
|---|---|---|
| Static render, pan/zoom | ≥ 30 FPS | **59.9 FPS** median |
| Bulk import (100K) | < 30 s | **9.3 s** |
| Live push → on screen | < 100 ms | **73.9 ms** median |

These are one machine's numbers. They are quoted in the README as measured
figures because they were measured, not because they are a promise about yours —
which is the reason this page exists rather than a bare claim.

### Resolved: why the live-push figure did not reproduce

*(2026-08-05, closed 2026-08-06. The README links here; the heading is kept stable
so that link does not rot.)*

Re-running the procedure above on the same machine and the same fixture seed, on
the day the targets became assertions, did not reproduce two of these rows. Both
have now been explained, and one of them was a real regression that is fixed.

| Metric | Target | Recorded | Mid-investigation | Now | Verdict |
|---|---|---|---|---|---|
| Static render, pan/zoom | ≥ 30 FPS | 59.9 FPS | 59.9 FPS | **59.9 FPS** | passes |
| Bulk import (100K) | < 30 s | 9.3 s | 12.6 – 14.5 s | **14.6 s** | passes |
| Live push → on screen | < 100 ms | 73.9 ms | 119.4 – 134.0 ms | **91.5 ms** | passes |

**1. The 73.9 ms was measuring something smaller than "on screen".**

The old harness ended its clock on a DOM mutation — the node-count text changing.
That happens to land after the WebGL work only while React schedules the canvas
effect into the same task as the DOM commit; it is not a property of the app.
The original report's own arithmetic gives it away: server 53 ms + canvas effect
69 ms ≈ 122 ms for push→drawn, published as 73.9 ms. So 73.9 ms and 119 ms were
never the same quantity, and "restoring" 73.9 ms was never the goal.

The endpoint is now defined in the application rather than inferred by the
harness: `OverviewCanvas` stamps `window.__vbPainted` at the moment data has been
applied to the canvas, and the harness reads that. This makes the bar *harder*, not
easier — the WebGL work is now inside the measurement. The target stayed at
< 100 ms throughout.

**2. There was also a genuine regression, in an unexpected place.**

The temporal scrubber derived its track — the earliest and latest `created_at` —
by walking every node, edge and finding, inside a `useMemo` keyed on structural
changes. That is once per push, in React's render phase, immediately ahead of the
canvas work: 300,015 timestamp parses per push at 100K, scaling exactly linearly
(30,005 at 10K, 90,005 at 30K). The store now maintains those bounds as data
arrives, so a push costs O(1) there.

Two further costs were moved off the push path rather than removed: the label
position tracking (~16 ms) now runs on the label refresh's existing throttle, and
the camera re-framing (~17.5 ms) now runs on a trailing throttle. Both still
happen — labels and framing would be broken otherwise — just not inside the
push→display window.

Measured as an interleaved A/B in a single browser session, alternating the two
configurations against the same loaded graph (five rounds, 20 pushes each):

| | before | after |
|---|---|---|
| per-round median | 118.2 / 115.2 / 117.6 / 116.9 / 120.3 ms | 97.5 / 98.2 / 94.4 / 96.0 / 97.7 ms |
| median of medians | **117.6 ms** | **97.5 ms** |

Separate full harness runs then measured 91.5 ms. Paired same-session comparison
is the method here on purpose: run-to-run variance on this machine is wide enough
to hide a 60 ms effect, and comparing ranges across separate runs is what made one
of the two investigations reach the wrong conclusion in the first place.

**3. Bulk import: the published number came from a different graph.**

9.3 s was measured on a 100K-node / 100K-edge fixture that was never committed;
`scripts/gen_perf100k.py` generates 100K nodes / 200K edges. That is 1.5× the
entities, which is most of the difference. The criterion (< 30 s) was never in
danger either way; what was wrong was quoting a figure from a graph nobody else
could build. The committed generator is now the only fixture these numbers refer
to, and it is named in the Measured table above.

**What did not change: the target.** < 100 ms is what it was. The number moved to
meet the criterion, not the other way round.
