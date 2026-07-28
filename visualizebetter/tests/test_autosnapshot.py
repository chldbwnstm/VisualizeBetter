"""Completion verification for TASK 5 — 자동 스냅샷 ([23-C], [5-E]).

Covers: periodic tick saves only when dirty, rolling GC keeps the newest 20 auto
snapshots and spares manual ones, snapshot_before writes one auto snapshot, and
start()/stop() drive the asyncio task.
"""

import asyncio

import pytest

from visualizebetter.graph.core import Graph
from visualizebetter.graph.snapshots import (
    DEFAULT_AUTO_INTERVAL_SECONDS,
    MAX_AUTO_SNAPSHOTS,
    AutoSnapshotter,
    SnapshotStore,
)


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path / "data")


@pytest.fixture
def graph():
    g = Graph(name="test")
    g.add_node(id="a", label="A", type="class")
    g.clear_dirty()
    return g


def run(coro):
    return asyncio.run(coro)


def kinds_of(store):
    return [row["kind"] for row in run(store.list_snapshots())]


# --- 주기 트리거: dirty 일 때만 ([23-C]) ---


def test_periodic_tick_saves_when_dirty(store, graph):
    snapshotter = AutoSnapshotter(graph, store)
    graph.add_node(id="b", label="B", type="class")

    result = run(snapshotter.snapshot_if_dirty())

    assert result is not None
    assert kinds_of(store) == ["auto"]


def test_periodic_tick_skips_when_not_dirty(store, graph):
    snapshotter = AutoSnapshotter(graph, store)

    result = run(snapshotter.snapshot_if_dirty())

    assert result is None
    assert run(store.list_snapshots()) == []


def test_periodic_save_clears_dirty_so_the_next_tick_skips(store, graph):
    """save_snapshot already clears the flag ([23-C]); no duplicate clear here."""
    snapshotter = AutoSnapshotter(graph, store)
    graph.add_node(id="b", label="B", type="class")

    run(snapshotter.snapshot_if_dirty())
    assert graph.dirty is False

    assert run(snapshotter.snapshot_if_dirty()) is None
    assert len(run(store.list_snapshots())) == 1


def test_a_further_change_makes_the_next_tick_save_again(store, graph):
    snapshotter = AutoSnapshotter(graph, store)

    graph.add_node(id="b", label="B", type="class")
    run(snapshotter.snapshot_if_dirty())
    graph.add_node(id="c", label="C", type="class")
    run(snapshotter.snapshot_if_dirty())

    assert kinds_of(store) == ["auto", "auto"]


def test_periodic_snapshot_captures_the_graph(store, graph):
    snapshotter = AutoSnapshotter(graph, store)
    graph.add_node(id="b", label="B", type="class")
    graph.add_finding(title="gold", node_ids=["a"])

    saved = run(snapshotter.snapshot_if_dirty())
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert set(loaded.nodes) == {"a", "b"}
    assert len(loaded.findings) == 1


# --- start() / stop() — the seam serve will use ---


def test_default_interval_is_300_seconds(store, graph):
    assert AutoSnapshotter(graph, store).interval_seconds == 300
    assert DEFAULT_AUTO_INTERVAL_SECONDS == 300


def test_started_task_snapshots_a_dirty_graph(store, graph):
    async def go():
        snapshotter = AutoSnapshotter(graph, store, interval_seconds=0.01)
        graph.add_node(id="b", label="B", type="class")
        snapshotter.start()
        await asyncio.sleep(0.15)
        await snapshotter.stop()

    run(go())

    assert kinds_of(store) == ["auto"], "one dirty graph yields one auto snapshot"


def test_started_task_leaves_a_clean_graph_alone(store, graph):
    async def go():
        snapshotter = AutoSnapshotter(graph, store, interval_seconds=0.01)
        snapshotter.start()
        await asyncio.sleep(0.1)
        await snapshotter.stop()

    run(go())

    assert run(store.list_snapshots()) == []


def test_stop_cancels_the_task(store, graph):
    async def go():
        snapshotter = AutoSnapshotter(graph, store, interval_seconds=0.01)
        snapshotter.start()
        assert snapshotter.running
        await snapshotter.stop()
        return snapshotter.running

    assert run(go()) is False


def test_stop_is_safe_without_start(store, graph):
    run(AutoSnapshotter(graph, store).stop())


def test_start_is_idempotent(store, graph):
    async def go():
        snapshotter = AutoSnapshotter(graph, store, interval_seconds=0.01)
        snapshotter.start()
        first = snapshotter._task
        snapshotter.start()
        assert snapshotter._task is first
        await snapshotter.stop()

    run(go())


def test_no_snapshot_after_stop(store, graph):
    async def go():
        snapshotter = AutoSnapshotter(graph, store, interval_seconds=0.02)
        snapshotter.start()
        await snapshotter.stop()
        graph.add_node(id="b", label="B", type="class")
        await asyncio.sleep(0.1)

    run(go())

    assert run(store.list_snapshots()) == []


# --- snapshot_before ([23-C] 파괴적 작업 직전 훅) ---


def test_snapshot_before_writes_one_auto_snapshot(store, graph):
    snapshotter = AutoSnapshotter(graph, store)

    result = run(snapshotter.snapshot_before("clear_all"))

    assert result["snapshot_id"]
    assert kinds_of(store) == ["auto"]


def test_snapshot_before_saves_even_when_not_dirty(store, graph):
    """Recovery is the point — a clean flag is no reason to skip."""
    snapshotter = AutoSnapshotter(graph, store)
    assert graph.dirty is False

    run(snapshotter.snapshot_before("clear_all"))

    assert len(run(store.list_snapshots())) == 1


def test_snapshot_before_names_after_the_reason(store, graph):
    """[5-A] "pre-clear-<ts>" convention."""
    snapshotter = AutoSnapshotter(graph, store)

    run(snapshotter.snapshot_before("clear_layer"))

    (row,) = run(store.list_snapshots())
    assert row["name"].startswith("pre-clear_layer-")


def test_snapshot_before_state_is_recoverable(store, graph):
    """clear_all 직전 auto 스냅샷 → clear 후 그 스냅샷 load 로 복구 ([23-F] TASK 5)."""
    snapshotter = AutoSnapshotter(graph, store)
    graph.add_node(id="b", label="B", type="class")
    graph.add_finding(title="gold", node_ids=["a", "b"])

    saved = run(snapshotter.snapshot_before("clear_all"))
    # clear_all itself is not implemented yet; simulate the loss.
    graph.nodes.clear()
    graph.findings.clear()

    recovered = run(store.load_snapshot(saved["snapshot_id"]))
    assert set(recovered.nodes) == {"a", "b"}
    assert len(recovered.findings) == 1


# --- rolling GC ([23-C]: auto 최근 20개, manual 은 건드리지 않음) ---


def test_default_keep_is_20():
    assert MAX_AUTO_SNAPSHOTS == 20


def test_twenty_first_auto_snapshot_evicts_the_oldest(store, graph):
    snapshotter = AutoSnapshotter(graph, store)

    names = []
    for i in range(MAX_AUTO_SNAPSHOTS + 1):
        graph.add_node(id=f"n{i}", label="N", type="class")
        run(snapshotter.snapshot_if_dirty())
        names.append(run(store.list_snapshots())[0]["name"])

    remaining = {row["name"] for row in run(store.list_snapshots())}
    assert len(remaining) == MAX_AUTO_SNAPSHOTS
    assert names[0] not in remaining, "the oldest auto snapshot is gone"
    assert names[-1] in remaining, "the newest survives"


def test_gc_keeps_exactly_the_limit(store, graph):
    snapshotter = AutoSnapshotter(graph, store)

    for i in range(MAX_AUTO_SNAPSHOTS + 5):
        graph.add_node(id=f"n{i}", label="N", type="class")
        run(snapshotter.snapshot_if_dirty())

    assert len(run(store.list_snapshots())) == MAX_AUTO_SNAPSHOTS


def test_snapshot_before_also_prunes(store, graph):
    snapshotter = AutoSnapshotter(graph, store)

    for i in range(MAX_AUTO_SNAPSHOTS + 3):
        run(snapshotter.snapshot_before(f"op{i}"))

    assert len(run(store.list_snapshots())) == MAX_AUTO_SNAPSHOTS


def test_gc_never_touches_manual_snapshots(store, graph):
    snapshotter = AutoSnapshotter(graph, store)
    run(store.save_snapshot(graph, name="사람이-저장한-v1"))

    for i in range(MAX_AUTO_SNAPSHOTS + 5):
        graph.add_node(id=f"n{i}", label="N", type="class")
        run(snapshotter.snapshot_if_dirty())

    rows = run(store.list_snapshots())
    manual = [r for r in rows if r["kind"] == "manual"]
    auto = [r for r in rows if r["kind"] == "auto"]
    assert [r["name"] for r in manual] == ["사람이-저장한-v1"]
    assert len(auto) == MAX_AUTO_SNAPSHOTS


def test_manual_snapshots_do_not_count_toward_the_auto_limit(store, graph):
    snapshotter = AutoSnapshotter(graph, store)
    for i in range(5):
        run(store.save_snapshot(graph, name=f"manual-{i}"))

    for i in range(MAX_AUTO_SNAPSHOTS):
        graph.add_node(id=f"n{i}", label="N", type="class")
        run(snapshotter.snapshot_if_dirty())

    rows = run(store.list_snapshots())
    assert len([r for r in rows if r["kind"] == "auto"]) == MAX_AUTO_SNAPSHOTS
    assert len([r for r in rows if r["kind"] == "manual"]) == 5


def test_keep_is_injectable(store, graph):
    snapshotter = AutoSnapshotter(graph, store, keep=3)

    for i in range(6):
        graph.add_node(id=f"n{i}", label="N", type="class")
        run(snapshotter.snapshot_if_dirty())

    assert len(run(store.list_snapshots())) == 3


def test_prune_reports_how_many_it_deleted(store, graph):
    for i in range(MAX_AUTO_SNAPSHOTS + 4):
        run(store.save_snapshot(graph, name=f"auto-{i}", kind="auto"))

    assert run(store.prune_auto_snapshots()) == 4
    assert run(store.prune_auto_snapshots()) == 0, "pruning again is a no-op"


def test_pruned_snapshots_leave_no_orphan_rows(store, graph):
    """ON DELETE CASCADE reaches node/edge/finding/finding_node."""
    import aiosqlite

    graph.add_edge(source="a", target="b", relation="field")
    graph.add_finding(title="gold", node_ids=["a"])
    snapshotter = AutoSnapshotter(graph, store, keep=1)

    for i in range(4):
        graph.add_node(id=f"n{i}", label="N", type="class")
        run(snapshotter.snapshot_if_dirty())

    async def counts():
        async with aiosqlite.connect(store.db_path) as db:
            out = {}
            for table in ("snapshot", "node", "edge", "finding", "finding_node"):
                async with db.execute(
                    f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed table names
                ) as cursor:
                    out[table] = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM node WHERE snapshot_id NOT IN"
                " (SELECT id FROM snapshot)"
            ) as cursor:
                out["orphan_nodes"] = (await cursor.fetchone())[0]
            return out

    result = run(counts())
    assert result["snapshot"] == 1
    assert result["orphan_nodes"] == 0
    assert result["finding"] >= 1
