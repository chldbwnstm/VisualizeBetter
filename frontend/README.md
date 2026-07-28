# VisualizeBetter frontend

The React + TypeScript + Vite single-page app served by `visualizebetter serve`.
It is built from source and **not committed** — `frontend/dist/` is gitignored, so
a fresh clone has to build it once before the browser UI works.

Requires Node.js 22 (the version CI uses).

```bash
npm ci             # install exactly what package-lock.json pins
npm run build      # tsc -b && vite build  ->  frontend/dist/
npm test           # vitest run
npm run lint       # oxlint
npm run dev        # Vite dev server with HMR, for frontend work only
```

`npm run dev` serves the UI on Vite's own port and expects `visualizebetter serve`
to be running separately for the API and the WebSocket feed. For anything other
than frontend development, prefer `npm run build` plus `uv run visualizebetter
serve`, which serves the built bundle and the API from one origin.

Playwright end-to-end and performance specs live in `e2e/` and `perf/`; see
`../docs/benchmarks.md` for how the 100K performance numbers were measured.
