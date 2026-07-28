# Benchmarks — how the 100K numbers were measured

The README quotes performance figures at 100K nodes. This file is what makes them
checkable: the fixture generator, the exact procedure, the machine they came from,
and — importantly — what the harness does and does not guarantee.

## What the harness is

`frontend/perf/perf100k.spec.ts` is a **measurement probe, not a CI gate.**

It renders on a real GPU through a real `visualizebetter serve`, and GPU
throughput varies by an order of magnitude across machines. Turning the
measurements into assertions would make the suite fail on hardware where nothing
is wrong, so the spec asserts only that the probe *ran* (frames were sampled,
the expected number of latency samples came back) and reports the numbers as
output. The pass/fail judgement against the target is printed as text for a human
to read.

That is a deliberate trade, and the cost is worth naming: **a performance
regression will not turn CI red.** Catching one means running this by hand and
comparing to the table below.

## Reproducing

Requires the Quick start prerequisites (Python 3.11+, uv, Node 22) and a machine
with a working WebGL 2 context.

```bash
# 1. generate the fixture (deterministic — same seed, same bytes)
uv run python scripts/gen_perf100k.py --out perf100k.json

# 2. put it in the data directory the server will use
#    (any directory; pass the same one to serve)
mkdir -p .perfdata && mv perf100k.json .perfdata/

# 3. start the server against that directory
uv run visualizebetter serve --port 8765 --data-dir .perfdata

# 4. in another shell, run the probe
cd frontend
npx playwright test perf/perf100k.spec.ts --reporter=list
```

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
