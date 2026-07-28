"""Completion verification for TASK 4 — 스냅샷 persistence ([5-E], [23-B], [11]).

Covers: save → load round-trip identity (nodes + edges + findings + citations),
property-based round-trip ([8-A] hypothesis), edge 4-tuple identity, finding size
invariants after load, list_snapshots kind, and the [5-E]/[11] rule that a
snapshot name never reaches the filesystem.
"""

import asyncio
import json
import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from visualizebetter.graph.core import (
    CITATIONS_PROPERTY,
    MAX_FINDING_BODY_CHARS,
    MAX_FINDING_EVIDENCE,
    MAX_FINDING_NODE_IDS,
    MAX_FINDING_TAGS,
    MAX_FINDING_TITLE_CHARS,
    SUPERSEDED_PROPERTY,
    Graph,
)
from visualizebetter.graph.snapshots import DB_FILENAME, SnapshotStore


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path / "data")


def run(coro):
    return asyncio.run(coro)


def populated_graph():
    """nodes + edges + 3 findings + citations — the [23-F] TASK 4 검증 shape."""
    graph = Graph(name="demo", description="services")
    graph.add_node(
        id="app.OrderService",
        label="OrderService",
        type="class",
        properties={"ns": "MOD"},
        tags=["core"],
        layer="claude-1",
    )
    graph.add_node(id="app.PaymentService", label="PaymentService", type="class")
    graph.add_node(id="app.Login", label="Login", type="function")

    graph.add_edge(
        source="app.OrderService",
        target="app.PaymentService",
        relation="field",
        key="m_paymentService",
        weight=0.7,
    )
    graph.add_edge(source="app.PaymentService", target="app.Login", relation="call")

    graph.cite("app.OrderService", "trace://0x1400", "Trace: sub_1400")
    graph.cite("app.OrderService", "https://example.test/doc", "Spec")
    graph.cite("app.Login", "trace://0x2800", "Trace: login")

    graph.add_finding(
        title="결제 실패의 핵심 경로",
        body="상세 근거",
        node_ids=["app.OrderService", "app.Login"],
        confidence=0.95,
        evidence=["trace://0x1400"],
        tags=["auth"],
        layer="claude-1",
    )
    graph.add_finding(title="순환 참조", node_ids=["app.PaymentService"], confidence=0.4)
    graph.add_finding(title="앵커 없는 발견", confidence=0.6)
    return graph


def assert_graphs_equal(loaded, original):
    assert {i: n.to_dict() for i, n in loaded.nodes.items()} == {
        i: n.to_dict() for i, n in original.nodes.items()
    }
    assert {k: e.to_dict() for k, e in loaded.edges.items()} == {
        k: e.to_dict() for k, e in original.edges.items()
    }
    assert {i: f.to_dict() for i, f in loaded.findings.items()} == {
        i: f.to_dict() for i, f in original.findings.items()
    }


# --- save → load round-trip ---


def test_save_returns_snapshot_id_and_size(store):
    graph = populated_graph()

    result = run(store.save_snapshot(graph, name="프로젝트-구조-v1"))

    assert result["snapshot_id"]
    assert result["size"] > 0


def test_round_trip_restores_the_whole_graph(store):
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert_graphs_equal(loaded, original)


def test_round_trip_restores_nodes_with_every_field(store):
    original = Graph()
    original.add_node(
        id="n1",
        label="N1",
        type="class",
        properties={"ns": "app.ui", "count": 3, "flag": True},
        parent_id="p1",
        style_hint={"color": "#fff"},
        position_hint={"x": 1.5, "y": -2.0},
        layer="claude-1",
        tags=["a", "b"],
        ttl=60,
        created_by="claude",
    )

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert loaded.get_node("n1").to_dict() == original.get_node("n1").to_dict()


def test_round_trip_preserves_timestamps(store):
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    for node_id, node in original.nodes.items():
        assert loaded.get_node(node_id).created_at == node.created_at
        assert loaded.get_node(node_id).updated_at == node.updated_at


def test_round_trip_restores_graph_container_fields(store):
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert loaded.metadata == original.metadata
    assert loaded.layers == original.layers
    assert loaded.version == original.version


# --- citations ([23-B]: node properties 안이라 node 직렬화로 커버) ---


def test_round_trip_restores_citations(store):
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    citations = loaded.get_node("app.OrderService").properties[CITATIONS_PROPERTY]
    assert len(citations) == 2
    assert citations[0]["url"] == "trace://0x1400"
    assert citations[0]["title"] == "Trace: sub_1400"
    assert citations[0]["ts"]


def test_citations_need_no_table_of_their_own(store):
    """They ride in the node's properties JSON."""
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    for node_id, node in original.nodes.items():
        assert loaded.get_node(node_id).properties == node.properties


# --- 생명주기 이력 ([24-C]) ---


def test_round_trip_restores_finding_history(store):
    """A finding's history is the record of what the gold used to say. Losing it
    at the snapshot boundary would lose it exactly where [23-D] hands work over."""
    original = populated_graph()
    finding = original.add_finding(title="t", body="old body")
    original.update_finding(finding.finding_id, {"set": {"body": "new"}}, reason="supersede")
    original.update_finding(finding.finding_id, {"set": {"title": "t2"}}, reason="correction")

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    restored = loaded.get_finding(finding.finding_id)
    assert restored._superseded[0]["prev"] == {"body": "old body"}
    assert restored._provenance[0]["action"] == "correction"


def test_round_trip_restores_node_history(store):
    """Node history rides in properties, so it needs no column of its own."""
    original = populated_graph()
    original.update_node("app.OrderService", {"set": {"label": "v2"}}, reason="supersede")

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    archived = loaded.get_node("app.OrderService").properties[SUPERSEDED_PROPERTY]
    assert archived[0]["prev"]["label"] == "OrderService"


def test_a_database_written_before_task_l_is_migrated(store):
    """CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a data
    directory from before [24] has no history columns — and the snapshots in it
    are the gold this project exists to keep, so it must upgrade, not fail."""
    legacy_sql = """
    CREATE TABLE finding (
        snapshot_id TEXT NOT NULL,
        finding_id  TEXT NOT NULL,
        title       TEXT NOT NULL,
        body        TEXT NOT NULL,
        confidence  REAL NOT NULL,
        evidence    TEXT NOT NULL,
        layer       TEXT,
        tags        TEXT NOT NULL,
        created_by  TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, finding_id)
    );
    """
    with sqlite3.connect(store.db_path) as db:
        db.executescript(legacy_sql)

    original = populated_graph()
    finding = original.add_finding(title="t", body="old body")
    original.update_finding(finding.finding_id, {"set": {"body": "new"}}, reason="supersede")

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert loaded.get_finding(finding.finding_id)._superseded[0]["prev"] == {
        "body": "old body"
    }


# --- findings ([23-B] finding + finding_node 조인 테이블) ---


def test_round_trip_restores_all_three_findings(store):
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert len(loaded.findings) == 3
    assert {f.title for f in loaded.findings.values()} == {
        "결제 실패의 핵심 경로",
        "순환 참조",
        "앵커 없는 발견",
    }


def test_round_trip_preserves_anchor_order(store):
    """node_ids is an ordered list, so the join table carries an ordinal."""
    original = Graph()
    anchors = [f"n{i}" for i in range(10)]
    finding = original.add_finding(title="t", node_ids=anchors)

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert loaded.get_finding(finding.finding_id).node_ids == anchors


def test_round_trip_restores_finding_without_anchors(store):
    original = Graph()
    finding = original.add_finding(title="anchorless")

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert loaded.get_finding(finding.finding_id).node_ids == []


def test_loaded_findings_are_queryable_by_anchor(store):
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    page, total = loaded.list_findings(node_id="app.Login")
    assert total == 1
    assert page[0].title == "결제 실패의 핵심 경로"


def test_loaded_findings_respect_size_invariants(store):
    """[23-B] 크기 불변식 survives the round-trip."""
    original = Graph()
    original.add_finding(
        title="제" * MAX_FINDING_TITLE_CHARS,
        body="본" * MAX_FINDING_BODY_CHARS,
        node_ids=[f"n{i}" for i in range(MAX_FINDING_NODE_IDS)],
        evidence=[f"e{i}" for i in range(MAX_FINDING_EVIDENCE)],
        tags=[f"t{i}" for i in range(MAX_FINDING_TAGS)],
    )

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    for finding in loaded.findings.values():
        assert len(finding.title) <= MAX_FINDING_TITLE_CHARS
        assert len(finding.body) <= MAX_FINDING_BODY_CHARS
        assert len(finding.node_ids) <= MAX_FINDING_NODE_IDS
        assert len(finding.evidence) <= MAX_FINDING_EVIDENCE
        assert len(finding.tags) <= MAX_FINDING_TAGS


# --- edge 4-tuple identity ([4-B]) ---


def test_round_trip_preserves_parallel_edges(store):
    original = Graph()
    original.add_edge(source="a", target="b", relation="field", key="m_health")
    original.add_edge(source="a", target="b", relation="field", key="m_mana")
    original.add_edge(source="a", target="b", relation="call")

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert len(loaded.edges) == 3
    assert loaded.get_edge("a", "b", "field", "m_health") is not None
    assert loaded.get_edge("a", "b", "field", "m_mana") is not None
    assert loaded.get_edge("a", "b", "call") is not None


def test_round_trip_rebuilds_indices(store):
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert loaded.indices.nodes_of_type("class") == {"app.OrderService", "app.PaymentService"}
    assert loaded.indices.edges_of("app.PaymentService") == {
        ("app.OrderService", "app.PaymentService", "field", "m_paymentService"),
        ("app.PaymentService", "app.Login", "call", ""),
    }


def test_loaded_graph_supports_cascade_delete(store):
    """Indices are rebuilt well enough for the [5-A] cascade rule to work."""
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    refused = loaded.delete_node("app.PaymentService")
    assert refused == {"ok": False, "error": "has_edges", "edge_count": 2}


# --- restore is not a mutation stream ---


def test_loaded_graph_is_not_dirty(store):
    original = populated_graph()

    saved = run(store.save_snapshot(original, name="v1"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert loaded.dirty is False


def test_save_clears_the_dirty_flag(store):
    """[23-C] dirty flag는 스냅샷 저장 시 clear."""
    graph = populated_graph()
    assert graph.dirty is True

    run(store.save_snapshot(graph, name="v1"))

    assert graph.dirty is False


def test_load_of_unknown_snapshot_raises(store):
    with pytest.raises(KeyError):
        run(store.load_snapshot("does-not-exist"))


# --- list_snapshots ([5-E]) ---


def test_list_snapshots_reports_spec_fields(store):
    graph = populated_graph()
    run(store.save_snapshot(graph, name="프로젝트-구조-v1", description="첫 스냅샷"))

    (row,) = run(store.list_snapshots())

    assert set(row) == {
        "id",
        "name",
        "description",
        "created_at",
        "node_count",
        "edge_count",
        "kind",
    }
    assert row["name"] == "프로젝트-구조-v1"
    assert row["description"] == "첫 스냅샷"
    assert row["node_count"] == 3
    assert row["edge_count"] == 2


def test_manual_is_the_default_kind(store):
    run(store.save_snapshot(Graph(), name="v1"))

    (row,) = run(store.list_snapshots())

    assert row["kind"] == "manual"


def test_kind_distinguishes_auto_snapshots(store):
    """auto is written by TASK 5; the column carries it today."""
    run(store.save_snapshot(Graph(), name="manual-one"))
    run(store.save_snapshot(Graph(), name="auto-one", kind="auto"))

    kinds = {row["name"]: row["kind"] for row in run(store.list_snapshots())}

    assert kinds == {"manual-one": "manual", "auto-one": "auto"}


def test_list_snapshots_is_empty_on_a_fresh_store(store):
    assert run(store.list_snapshots()) == []


def test_snapshots_are_independent(store):
    first = populated_graph()
    second = Graph()
    second.add_node(id="only", label="Only", type="other")

    saved_first = run(store.save_snapshot(first, name="v1"))
    saved_second = run(store.save_snapshot(second, name="v2"))

    assert len(run(store.load_snapshot(saved_first["snapshot_id"])).nodes) == 3
    assert len(run(store.load_snapshot(saved_second["snapshot_id"])).nodes) == 1


# --- [5-E] / [11]: a name is a DB value, never a path ---


TRAVERSAL_NAMES = [
    "../../etc/passwd",
    "..\\..\\Windows\\System32\\config\\SAM",
    "/etc/shadow",
    "C:\\Windows\\win.ini",
    "....//....//secret",
    "snap\x00.db",
]


@pytest.mark.parametrize("name", TRAVERSAL_NAMES)
def test_hostile_name_creates_no_file_outside_the_data_dir(store, tmp_path, name):
    run(store.save_snapshot(Graph(), name=name))

    written = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert written <= {store.db_path}, "the only file written is the fixed DB"


@pytest.mark.parametrize("name", TRAVERSAL_NAMES)
def test_hostile_name_survives_as_plain_data(store, name):
    """The defense is structural, so the name needs no mangling to be safe."""
    run(store.save_snapshot(Graph(), name=name))

    (row,) = run(store.list_snapshots())
    assert row["name"] == name


def test_db_filename_is_fixed_regardless_of_name(store):
    run(store.save_snapshot(Graph(), name="../../pwned"))

    assert store.db_path.name == DB_FILENAME
    assert store.db_path.parent == store.data_dir


def test_data_dir_is_created(tmp_path):
    target = tmp_path / "nested" / "visualizebetter"

    created = SnapshotStore(target)

    assert created.data_dir.is_dir()


# --- property-based round-trip ([8-A]: hypothesis for graph invariants) ---

_ids = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=12,
)
_labels = st.text(max_size=12)
_types = st.sampled_from(["class", "function", "field", "file", "unresolved"])
_json_values = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.text(max_size=12),
)
# 예약키(`_` 접두)는 제외한다: MCP 경계가 생성 시 이를 거부하므로([23-B]) 스냅샷
# 라운드트립 불변식이 다뤄야 할 입력이 아니다. 제외하지 않으면 생성기가
# {"_citations": None} 같은 상태를 만들어 cite() 가 그 위에서 터진다 — 그건 이
# 테스트의 관심사가 아니라 core 생성 경로의 별도 이슈다.
_properties = st.dictionaries(
    _ids.filter(lambda k: not k.startswith("_")), _json_values, max_size=4
)
_tags = st.lists(st.text(min_size=1, max_size=8), max_size=3)


@st.composite
def graphs(draw):
    graph = Graph()
    node_ids = draw(st.lists(_ids, min_size=1, max_size=6, unique=True))
    for node_id in node_ids:
        graph.add_node(
            id=node_id,
            label=draw(_labels),
            type=draw(_types),
            properties=draw(_properties),
            tags=draw(_tags),
            layer=draw(st.one_of(st.none(), st.sampled_from(["claude-1", "gpt-1"]))),
        )

    for _ in range(draw(st.integers(min_value=0, max_value=5))):
        graph.add_edge(
            source=draw(st.sampled_from(node_ids)),
            target=draw(st.sampled_from(node_ids)),
            relation=draw(st.sampled_from(["field", "call", "ref"])),
            key=draw(st.text(max_size=6)),
            weight=draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
            properties=draw(_properties),
        )

    for _ in range(draw(st.integers(min_value=0, max_value=3))):
        graph.add_finding(
            title=draw(st.text(min_size=1, max_size=20)),
            body=draw(st.text(max_size=30)),
            node_ids=draw(st.lists(st.sampled_from(node_ids), max_size=4)),
            confidence=draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
            evidence=draw(st.lists(st.text(max_size=10), max_size=3)),
            tags=draw(_tags),
        )

    for node_id in draw(st.lists(st.sampled_from(node_ids), max_size=3)):
        graph.cite(node_id, draw(st.text(max_size=15)), draw(st.text(max_size=10)))

    return graph


@given(graph=graphs())
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_round_trip_is_identity(store, graph):
    saved = run(store.save_snapshot(graph, name="property"))
    loaded = run(store.load_snapshot(saved["snapshot_id"]))

    assert_graphs_equal(loaded, graph)


@given(graph=graphs())
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_reported_size_matches_the_serialized_payload(store, graph):
    """[5-E] size = 직렬화 바이트 수."""
    saved = run(store.save_snapshot(graph, name="property"))

    loaded = run(store.load_snapshot(saved["snapshot_id"]))
    payload = {
        "metadata": loaded.metadata,
        "layers": loaded.layers,
        "version": loaded.version,
        "nodes": [n.to_dict() for n in loaded.nodes.values()],
        "edges": [e.to_dict() for e in loaded.edges.values()],
        "findings": [f.to_dict() for f in loaded.findings.values()],
    }
    assert saved["size"] == len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


# --- 2026-07-28 rename migration (mcpgraph → visualizebetter) ---


def test_migrate_legacy_dir_renames_once(tmp_path):
    from visualizebetter.graph.snapshots import _migrate_legacy_dir

    legacy = tmp_path / "mcpgraph"
    legacy.mkdir()
    (legacy / "mcpgraph.sqlite3").write_bytes(b"store")
    target = tmp_path / "visualizebetter"

    assert _migrate_legacy_dir(target, legacy) == target
    assert not legacy.exists()
    assert (target / "mcpgraph.sqlite3").read_bytes() == b"store"
    # Idempotent: a second call (legacy gone) still answers target.
    assert _migrate_legacy_dir(target, legacy) == target


def test_migrate_legacy_dir_never_clobbers_an_existing_target(tmp_path):
    from visualizebetter.graph.snapshots import _migrate_legacy_dir

    legacy = tmp_path / "mcpgraph"
    legacy.mkdir()
    target = tmp_path / "visualizebetter"
    target.mkdir()
    (target / "keep").write_text("new")

    assert _migrate_legacy_dir(target, legacy) == target
    assert legacy.exists()  # left alone — target wins
    assert (target / "keep").read_text() == "new"


def test_store_adopts_legacy_db_file_in_a_user_supplied_dir(tmp_path):
    from visualizebetter.graph.snapshots import _LEGACY_DB_FILENAME

    (tmp_path / _LEGACY_DB_FILENAME).write_bytes(b"old-store")

    store = SnapshotStore(tmp_path)

    assert store.db_path == tmp_path / DB_FILENAME
    assert store.db_path.read_bytes() == b"old-store"
    assert not (tmp_path / _LEGACY_DB_FILENAME).exists()


def test_store_prefers_existing_new_db_over_legacy(tmp_path):
    from visualizebetter.graph.snapshots import _LEGACY_DB_FILENAME

    (tmp_path / DB_FILENAME).write_bytes(b"new-store")
    (tmp_path / _LEGACY_DB_FILENAME).write_bytes(b"old-store")

    store = SnapshotStore(tmp_path)

    assert store.db_path.read_bytes() == b"new-store"
    assert (tmp_path / _LEGACY_DB_FILENAME).exists()  # untouched


# --- [23-C] ★ 이관 하드닝 (Fable 확정 설계 a~f) — 적대 시나리오 고정 ---


import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from visualizebetter.graph import snapshots as snap_mod


@pytest.fixture
def canonical(tmp_path, monkeypatch):
    """canonical (target, legacy) 기본경로 쌍을 tmp 로 고정 — 플랫폼 무관."""
    target = tmp_path / "visualizebetter"
    legacy = tmp_path / "mcpgraph"
    monkeypatch.setattr(snap_mod, "_default_base_pair", lambda: (target, legacy))
    return target, legacy


def _real_sqlite(path: Path, marker: str = "gold") -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (marker,))
    con.commit()
    con.close()


def _store_db(path: Path, snapshots: int = 1) -> None:
    """실제 스토어 모양 — [23-C] g 의 "빈 스토어" 판정 대상인 snapshot 테이블."""
    con = sqlite3.connect(path)
    con.executescript(snap_mod._SCHEMA)
    for i in range(snapshots):
        con.execute(
            "INSERT INTO snapshot (id, name, description, created_at, kind,"
            " node_count, edge_count, metadata, layers, version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"s{i}", f"snap{i}", "", "2026-01-01T00:00:00Z", "manual", 0, 0, "{}", "[]", ""),
        )
    con.commit()
    con.close()


def _advertise(data_dir: Path, pid: int, **extra) -> Path:
    """[8-D] serve.json 을 쓴다 — RN2 의 liveness 는 이 pid 만 본다."""
    data_dir.mkdir(parents=True, exist_ok=True)
    port_file = data_dir / snap_mod._PORT_FILE_NAME
    payload = {"pid": pid, "port": 8765, "url": "http://127.0.0.1:8765", "token": None}
    payload.update(extra)
    port_file.write_text(json.dumps(payload), encoding="utf-8")
    return port_file


@pytest.fixture
def live_pid():
    """진짜 살아있는 프로세스의 pid (테스트 종료 시 정리)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(0.3)
    yield child.pid
    child.kill()
    child.wait(timeout=10)


@pytest.fixture
def dead_pid():
    """확실히 종료된 프로세스의 pid — RN1 이 Windows 에서 살아있다고 오판하던 값."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    time.sleep(0.2)
    return child.pid


def test_precreated_empty_target_adopts_legacy_db(canonical):
    """(1) 결함 #1 재현: Tauri 셸이 target 을 선생성하고 명시 --data-dir 로 실행 —
    빈 target 이 이관을 영구 차단하던 경로가 입양(b)으로 자가 치유되어야 한다."""
    target, legacy = canonical
    target.mkdir(parents=True)  # main.rs:72-73 의 create_dir_all 재현
    legacy.mkdir(parents=True)
    _real_sqlite(legacy / snap_mod._LEGACY_DB_FILENAME)

    store = SnapshotStore(target)  # main.rs:100-101 의 명시 --data-dir 재현

    assert store.db_path == target / DB_FILENAME
    assert store.db_path.exists()
    assert not (legacy / snap_mod._LEGACY_DB_FILENAME).exists()
    con = sqlite3.connect(store.db_path)
    assert con.execute("SELECT v FROM t").fetchone() == ("gold",)
    con.close()


def test_explicit_canonical_data_dir_migrates_like_default(canonical):
    """(2a) 명시 --data-dir 라도 canonical 신 기본경로면 이관이 돈다 (a)."""
    target, legacy = canonical
    legacy.mkdir(parents=True)
    _real_sqlite(legacy / snap_mod._LEGACY_DB_FILENAME)

    store = SnapshotStore(target)  # target 미존재 → dir rename → 파일명 입양

    assert not legacy.exists()
    assert store.db_path == target / DB_FILENAME
    assert store.db_path.exists()


def test_explicit_arbitrary_data_dir_stays_magic_free(canonical, tmp_path):
    """(2b) 임의 사용자 지정 경로에서는 어떤 이관도 일어나지 않는다 (a: 마법 금지)."""
    target, legacy = canonical
    legacy.mkdir(parents=True)
    _real_sqlite(legacy / snap_mod._LEGACY_DB_FILENAME)
    other = tmp_path / "elsewhere"

    store = SnapshotStore(other)

    assert legacy.exists()  # 손대지 않음
    assert (legacy / snap_mod._LEGACY_DB_FILENAME).exists()
    assert store.db_path == other / DB_FILENAME
    assert not store.db_path.exists()


def test_cold_journal_is_recovered_then_migrated(tmp_path):
    """(3a) 유효 DB + cold journal: 1회 open 으로 무해가 입증된 사이드카만 제거 후
    rename — journal 을 매달고 rename 하지 않는다 (c)."""
    src = tmp_path / snap_mod._LEGACY_DB_FILENAME
    _real_sqlite(src)
    (tmp_path / (src.name + "-journal")).write_bytes(b"stale garbage header")

    store = SnapshotStore(tmp_path)

    assert store.db_path == tmp_path / DB_FILENAME
    assert store.db_path.exists()
    assert not (tmp_path / (src.name + "-journal")).exists()
    con = sqlite3.connect(store.db_path)
    assert con.execute("SELECT v FROM t").fetchone() == ("gold",)
    con.close()


def test_unrecoverable_journal_blocks_rename(tmp_path):
    """(3b) 복구 open 이 실패하면 rename 금지 — 구 이름 그대로 제자리 사용 (c).

    저널 파일 자체는 단언하지 않는다: SQLite 가 실패한 open 중에도 cold(무효
    헤더) 저널을 스스로 제거할 수 있고, 그것은 SQLite 의 정상 판정이다. 여기서
    지키는 불변식은 "검증되지 않은 스토어는 절대 새 이름으로 옮기지 않는다"다."""
    src = tmp_path / snap_mod._LEGACY_DB_FILENAME
    src.write_bytes(b"this is not a sqlite database")
    (tmp_path / (src.name + "-journal")).write_bytes(b"maybe hot")

    store = SnapshotStore(tmp_path)

    assert store.db_path == src  # rename 하지 않았다
    assert src.exists()  # 원본은 그대로 남는다
    assert not (tmp_path / DB_FILENAME).exists()


def test_race_loser_fallback_returns_target_when_legacy_vanished(tmp_path, monkeypatch):
    """(4) 레이스 패자: rename 이 OSError 로 지고 legacy 도 사라졌다면 폴백은
    (사라진) legacy 가 아니라 target 이어야 한다 (e)."""
    target = tmp_path / "visualizebetter"
    legacy = tmp_path / "mcpgraph"
    legacy.mkdir(parents=True)

    real_rename = Path.rename

    def raced(self, destination):
        if _same := (str(self) == str(legacy)):  # noqa: F841 — clarity
            shutil.rmtree(legacy, ignore_errors=True)  # 상대가 먼저 가져감
            raise OSError("lost the race")
        return real_rename(self, destination)

    monkeypatch.setattr(Path, "rename", raced)

    assert snap_mod._migrate_legacy_dir(target, legacy) == target


def test_live_legacy_serve_defers_all_migration(canonical, live_pid, caplog):
    """(5) 살아있는 구 serve: dir·file 이관 전부 보류 + 경고 (d/h) — 결함 #2 재현.
    RN2: HTTP 프로브가 아니라 serve.json 의 pid 존재로 판정한다."""
    target, legacy = canonical
    legacy.mkdir(parents=True)
    _real_sqlite(legacy / snap_mod._LEGACY_DB_FILENAME)
    _advertise(legacy, live_pid)

    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        store = SnapshotStore(target)

    assert legacy.exists()  # dir 이관 보류
    assert (legacy / snap_mod._LEGACY_DB_FILENAME).exists()  # DB 입양도 보류
    assert (legacy / snap_mod._PORT_FILE_NAME).exists()  # 살아있으면 삭제하지 않는다
    assert store.db_path == target / DB_FILENAME
    assert not store.db_path.exists()
    assert "deferred" in caplog.text


def test_dead_legacy_serve_json_does_not_block_migration(canonical, dead_pid):
    """(5b) 죽은 serve 의 잔존 serve.json 은 이관을 막지 않고, stale 광고는 삭제된다.

    ★ RN1 회귀 지점: Windows 에서 os.kill(pid,0) 은 종료된 pid 에도 예외를 던지지
    않아 이 케이스를 영원히 alive 로 오판했다 (사망 증명 불가 → 보류 영구화)."""
    target, legacy = canonical
    legacy.mkdir(parents=True)
    _real_sqlite(legacy / snap_mod._LEGACY_DB_FILENAME)
    _advertise(legacy, dead_pid)

    store = SnapshotStore(target)

    assert not legacy.exists()
    assert store.db_path == target / DB_FILENAME
    assert store.db_path.exists()


# --- [23-C] ★ RN2 2차 하드닝 (g~k) — 4렌즈 적대 검증이 확정한 결함 고정 ---


def test_blocker_chain_deferred_run_leaves_empty_db_then_next_run_adopts(canonical, live_pid):
    """(k1) ★ blocker end-to-end — RN1 이 놓친 체인 전체.

    run1: 구 serve 가 살아있어 이관 보류 → 그럼에도 serve lifespan 의
    initialize() 가 target 에 빈 DB 를 만든다 (uvicorn 은 bind 전에 lifespan 을
    돌리므로 포트 충돌로 기동 실패해도 생성됨).
    run2: 구 serve 사망 → RN1 이라면 `if db.exists()` 가 입양을 영구 차단했다.
    RN2 (g) 는 "빈 스토어"도 입양 자격으로 보므로 legacy 데이터가 복원된다."""
    target, legacy = canonical
    legacy.mkdir(parents=True)
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, snapshots=3)  # 사용자의 gold
    port_file = _advertise(legacy, live_pid)

    # --- run 1: 보류 + lifespan initialize() 의 빈 DB 생성 ---
    run1 = SnapshotStore(target)
    assert run1.db_path == target / DB_FILENAME
    run(run1.initialize())
    assert run1.db_path.exists()  # 빈 DB 잔해가 생겼다
    assert snap_mod._is_empty_store(run1.db_path)
    assert (legacy / snap_mod._LEGACY_DB_FILENAME).exists()  # gold 는 아직 legacy 에

    # --- run 2: 구 serve 사망 후 ---
    port_file.unlink()  # serve 종료 시 광고 제거
    run2 = SnapshotStore(target)

    assert run2.db_path == target / DB_FILENAME
    con = sqlite3.connect(run2.db_path)
    try:
        assert con.execute("SELECT count(*) FROM snapshot").fetchone()[0] == 3
    finally:
        con.close()
    assert not (legacy / snap_mod._LEGACY_DB_FILENAME).exists()


def test_empty_store_is_adoptable_but_populated_store_is_not(canonical):
    """(k2) g 의 판정 경계 — 빈 스토어는 입양 대상, 내용이 있으면 보존된다."""
    target, legacy = canonical
    legacy.mkdir(parents=True)
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, snapshots=2)
    target.mkdir(parents=True)
    _store_db(target / DB_FILENAME, snapshots=0)  # 빈 잔해

    store = SnapshotStore(target)
    con = sqlite3.connect(store.db_path)
    try:
        assert con.execute("SELECT count(*) FROM snapshot").fetchone()[0] == 2
    finally:
        con.close()


def test_populated_target_never_swallows_a_legacy_store(tmp_path, monkeypatch):
    """(k2b) 반대 방향 — target 이 이미 채워져 있으면 legacy 를 삼키지 않는다."""
    target = tmp_path / "visualizebetter"
    legacy = tmp_path / "mcpgraph"
    monkeypatch.setattr(snap_mod, "_default_base_pair", lambda: (target, legacy))
    legacy.mkdir()
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, snapshots=1)
    target.mkdir()
    _store_db(target / DB_FILENAME, snapshots=5)

    store = SnapshotStore(target)

    con = sqlite3.connect(store.db_path)
    try:
        assert con.execute("SELECT count(*) FROM snapshot").fetchone()[0] == 5
    finally:
        con.close()
    assert (legacy / snap_mod._LEGACY_DB_FILENAME).exists()  # 남의 것은 안 건드린다


def test_unreadable_store_counts_as_populated(tmp_path):
    """(k2c) g 는 fail-closed — 판정 실패(손상/스키마 없음)는 '비어있지 않음'.
    반대로 판정하면 진짜 스토어를 입양이 지워버릴 수 있다."""
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a database at all")
    assert snap_mod._is_empty_store(corrupt) is False

    no_schema = tmp_path / "noschema.sqlite3"
    _real_sqlite(no_schema)  # 유효 DB 지만 snapshot 테이블 없음
    assert snap_mod._is_empty_store(no_schema) is False

    assert snap_mod._is_empty_store(tmp_path / "missing.sqlite3") is False


def test_pid_liveness_alive_dead_and_stale_cleanup(tmp_path, live_pid, dead_pid):
    """(k3) h — pid 존재로만 판정하고, 사망 확인 시 stale 광고를 제거한다."""
    assert snap_mod._pid_alive(live_pid) is True
    assert snap_mod._pid_alive(dead_pid) is False   # ★ os.kill 이 못 잡던 케이스
    assert snap_mod._pid_alive(0) is False
    assert snap_mod._pid_alive(-1) is False
    assert snap_mod._pid_alive(os.getpid()) is True

    alive_dir = tmp_path / "alive"
    _advertise(alive_dir, live_pid)
    assert snap_mod._serve_alive_in(alive_dir) is True
    assert (alive_dir / snap_mod._PORT_FILE_NAME).exists()  # 살아있으면 보존

    dead_dir = tmp_path / "dead"
    port_file = _advertise(dead_dir, dead_pid)
    assert snap_mod._serve_alive_in(dead_dir) is False
    assert not port_file.exists()  # 사망 증명 → stale 광고 삭제


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 liveness 분기")
def test_win32_liveness_branch(live_pid, dead_pid):
    """(k3b) Windows 전용 — OpenProcess/WaitForSingleObject 매핑 실측 고정.
    권한 거부(System, pid 4)는 'alive' 로 판정해야 한다 (존재하지만 못 엶)."""
    assert snap_mod._win_pid_alive(live_pid) is True
    assert snap_mod._win_pid_alive(dead_pid) is False
    assert snap_mod._win_pid_alive(999_999) is False
    assert snap_mod._win_pid_alive(4) is True


@pytest.mark.parametrize(
    "content",
    [
        '{"url": null, "pid": null}',
        "not json at all {{{",
        '{"url": "no-scheme-host", "pid": 1}',
        '{"port": 8765}',
        "[]",
        "",
    ],
)
def test_garbled_serve_json_never_crashes_startup(canonical, content):
    """(k4) 결함 6 (RN1 회귀) — 어떤 serve.json 내용으로도 기동이 죽지 않는다.
    RN1 은 url 을 try 밖에서 Request() 에 넘겨 ValueError 로 serve/CLI/proxy 를
    전부 크래시시켰다. 파싱 불가 = dead 취급 + 이관은 정상 진행."""
    target, legacy = canonical
    legacy.mkdir(parents=True)
    _real_sqlite(legacy / snap_mod._LEGACY_DB_FILENAME)
    (legacy / snap_mod._PORT_FILE_NAME).write_text(content, encoding="utf-8")

    store = SnapshotStore(target)  # 예외 없이 끝나야 한다

    assert store.db_path == target / DB_FILENAME
    assert store.db_path.exists()  # dead 취급이므로 이관 진행


def test_custom_data_dir_same_dir_adoption_is_guarded(tmp_path, live_pid):
    """(k5) 결함 5 — 임의 --data-dir 의 same-dir 입양도 그 디렉토리 serve.json 을
    본다. RN1 은 canonical legacy 만 봐서 커스텀 dir 의 살아있는 serve 밑에서
    DB 를 rename 해버렸다 (i 로 일반화)."""
    custom = tmp_path / "my-graphs"
    custom.mkdir()
    old = custom / snap_mod._LEGACY_DB_FILENAME
    _real_sqlite(old)
    _advertise(custom, live_pid)

    store = SnapshotStore(custom)

    assert old.exists()          # 살아있는 serve 밑에서 rename 하지 않았다
    assert store.db_path == old  # 구 이름 그대로 제자리 사용
    assert not (custom / DB_FILENAME).exists()


def test_cross_dir_adoption_is_guarded_by_the_destination_serve(canonical, live_pid, caplog):
    """(k5b) i — 가드는 source 쪽만이 아니라 *쓰기가 일어나는* 디렉토리 전부.

    cross-dir 입양은 legacy 에서 읽어 target 에 쓴다. target 에서 serve 가 돌고
    있으면(그 serve 의 빈 DB 를 교체하게 되므로) 보류해야 한다 — source.parent
    만 검사하면 이 경우가 통과해버린다."""
    target, legacy = canonical
    legacy.mkdir(parents=True)
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, snapshots=2)
    target.mkdir(parents=True)
    _store_db(target / DB_FILENAME, snapshots=0)  # 살아있는 serve 가 만든 빈 스토어
    _advertise(target, live_pid)                  # ...그리고 그 serve 는 아직 살아있다

    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        store = SnapshotStore(target)

    assert (legacy / snap_mod._LEGACY_DB_FILENAME).exists()  # 가져오지 않았다
    assert store.db_path == target / DB_FILENAME
    assert snap_mod._is_empty_store(store.db_path)           # 빈 채로 둔다
    assert "deferred" in caplog.text


def test_file_side_race_fallback_prefers_surviving_side(tmp_path, monkeypatch):
    """(k6) file-side (e) 폴백 — RN1 에서 테스트가 0이라 뮤테이션이 생존했다.
    rename 이 지고 source 도 사라졌으면 target 을, source 가 남아있으면(same-dir)
    source 를 반환해야 한다."""
    d = tmp_path / "data"
    d.mkdir()
    old = d / snap_mod._LEGACY_DB_FILENAME
    _real_sqlite(old)

    real_rename = Path.rename

    def lose_and_vanish(self, destination):
        if str(self) == str(old):
            old.unlink()  # 경쟁자가 먼저 가져갔다
            raise OSError("lost the race")
        return real_rename(self, destination)

    monkeypatch.setattr(Path, "rename", lose_and_vanish)
    assert SnapshotStore(d).db_path == d / DB_FILENAME  # 사라진 source 말고 target

    # source 가 살아남은 경우: 제자리 사용
    d2 = tmp_path / "data2"
    d2.mkdir()
    old2 = d2 / snap_mod._LEGACY_DB_FILENAME
    _real_sqlite(old2)

    def lose_only(self, destination):
        if str(self) == str(old2):
            raise OSError("lost the race")
        return real_rename(self, destination)

    monkeypatch.setattr(Path, "rename", lose_only)
    assert SnapshotStore(d2).db_path == old2


def test_wal_sidecar_survival_is_fail_closed(tmp_path):
    """(k7) j — -wal/-shm 이 복구 open 후에도 남으면 not-clean (rename 포기).

    살아있는 WAL 연결을 실제로 붙잡아 재현한다 — 고아 -wal 파일을 그냥 놓아두면
    SQLite 가 복구 open 중에 스스로 치워버려 이 위험을 재현하지 못한다. 지속되는
    -wal 은 "다른 프로세스가 이 DB 를 쓰는 중"이라는 뜻이고, 그 밑에서 rename 하면
    그 프로세스의 쓰기가 갈 곳을 잃는다."""
    db = tmp_path / snap_mod._LEGACY_DB_FILENAME
    holder = sqlite3.connect(db)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("CREATE TABLE t (v TEXT)")
        holder.execute("INSERT INTO t VALUES ('gold')")
        holder.commit()
        assert (tmp_path / (db.name + "-wal")).exists()  # 보유자가 잡고 있다

        assert snap_mod._recover_sqlite_sidecars(db) is False

        store = SnapshotStore(tmp_path)
        assert store.db_path == db                     # rename 하지 않았다
        assert not (tmp_path / DB_FILENAME).exists()
    finally:
        holder.close()


def test_recover_never_creates_the_database(tmp_path):
    """(k8) j — 소스가 없으면 즉시 False. RN1 은 sqlite3.connect 가 파일을 만들어
    0-byte DB 를 남기고 clean 이라고 보고했다."""
    missing = tmp_path / "gone.sqlite3"
    (tmp_path / "gone.sqlite3-journal").write_bytes(b"orphan journal")

    assert snap_mod._recover_sqlite_sidecars(missing) is False
    assert not missing.exists()  # ★ 만들지 않았다
