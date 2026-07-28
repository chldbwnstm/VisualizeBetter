#!/usr/bin/env python3
"""Generate the 100K-scale fixture the performance harness imports.

``frontend/perf/perf100k.spec.ts`` expects a file named ``perf100k.json`` to be
sitting in the running server's data directory; it then loads it through the same
``import_from_file`` path a user's own bulk import takes. The fixture itself was
never committed, which meant the published numbers had no reproduction path — this
script is that path.

Deterministic by construction: the only randomness is a seeded ``random.Random``,
so the same arguments always produce byte-identical output and two runs on
different machines measure the same graph.

Usage
-----
    python scripts/gen_perf100k.py --out perf100k.json
    python scripts/gen_perf100k.py --nodes 10000 --edges 20000 --out small.json

Then place the file in the data directory ``visualizebetter serve`` is using
(``--data-dir``, or the platform default) and run the perf spec. See
``docs/benchmarks.md`` for the full procedure.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

# Shapes chosen to look like a real code graph rather than a uniform mesh: a few
# hub modules with many dependants, most nodes with one or two. Layout and
# rendering cost depend on that skew, so a uniform random graph would measure
# something the product never renders.
TYPES = ("module", "class", "function", "service", "table")
RELATIONS = ("imports", "calls", "extends", "reads", "writes")
LAYERS = ("backend", "frontend", "infra")


def build(nodes: int, edges: int, seed: int) -> dict:
    rng = random.Random(seed)
    hub_count = max(1, nodes // 500)

    node_rows = []
    for index in range(nodes):
        node_rows.append(
            {
                "id": f"n{index}",
                "label": f"{TYPES[index % len(TYPES)]}_{index}",
                "type": TYPES[index % len(TYPES)],
                "layer": LAYERS[index % len(LAYERS)],
                "properties": {"loc": rng.randint(10, 2000), "pkg": f"pkg{index % 200}"},
                "tags": ["generated"],
            }
        )

    seen: set[tuple[str, str, str]] = set()
    edge_rows = []
    while len(edge_rows) < edges:
        # 30% of edges point at a hub — that is what makes the degree
        # distribution skewed rather than flat.
        target_index = rng.randrange(hub_count) if rng.random() < 0.3 else rng.randrange(nodes)
        source_index = rng.randrange(nodes)
        if source_index == target_index:
            continue
        relation = RELATIONS[(source_index + target_index) % len(RELATIONS)]
        key = (f"n{source_index}", f"n{target_index}", relation)
        if key in seen:
            continue
        seen.add(key)
        edge_rows.append(
            {
                "source": key[0],
                "target": key[1],
                "relation": relation,
                "weight": round(rng.uniform(0.1, 1.0), 3),
            }
        )

    return {"nodes": node_rows, "edges": edge_rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--nodes", type=int, default=100_000)
    parser.add_argument("--edges", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=20260729, help="fixed by default so runs are comparable")
    parser.add_argument("--out", type=Path, default=Path("perf100k.json"))
    args = parser.parse_args()

    payload = build(args.nodes, args.edges, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # separators without spaces: the fixture is machine input, and at this size
    # the whitespace alone is tens of megabytes.
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    size_mb = args.out.stat().st_size / 1e6
    print(
        f"{args.out}: {len(payload['nodes']):,} nodes, {len(payload['edges']):,} edges,"
        f" {size_mb:.1f} MB (seed {args.seed})"
    )


if __name__ == "__main__":
    main()
