"""Completion verification for TASK 4 — 스냅샷 persistence ([5-E], [23-B], [11]).

Covers: save → load round-trip identity (nodes + edges + findings + citations),
property-based round-trip ([8-A] hypothesis), edge 4-tuple identity, finding size
invariants after load, list_snapshots kind, and the [5-E]/[11] rule that a
snapshot name never reaches the filesystem.
"""

import asyncio
import json
import sqlite3
import time

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
# ★ RN3: 예약키를 다시 생성기에 허용한다 (RN2 에서 제외했던 것을 원복).
# 그 제외는 core 생성 경로의 실제 갭을 가리고 있었다 — 이제 core 가 생성 시점에
# 예약키를 거부하므로(_reject_reserved_on_create), 생성기가 그것을 만들어내면
# add_node 가 ValueError 를 던지고 hypothesis 가 그 입력을 버린다. 적대성 복원.
_properties = st.dictionaries(_ids, _json_values, max_size=4)
_tags = st.lists(st.text(min_size=1, max_size=8), max_size=3)


@st.composite
def graphs(draw):
    graph = Graph()
    node_ids = draw(st.lists(_ids, min_size=1, max_size=6, unique=True))
    for node_id in node_ids:
        properties = draw(_properties)
        try:
            graph.add_node(
                id=node_id,
                label=draw(_labels),
                type=draw(_types),
                properties=properties,
                tags=draw(_tags),
                layer=draw(st.one_of(st.none(), st.sampled_from(["claude-1", "gpt-1"]))),
            )
        except ValueError:
            # ★ RN3: core 가 생성 시점에 예약키를 거부한다. 생성기는 계속 그걸
            # 만들어내되(적대성 유지), 거부가 실제로 일어났음을 여기서 단언하고
            # 예약키를 뺀 뒤 진행한다 — 라운드트립 불변식은 그대로 검증된다.
            assert any(k.startswith("_") for k in properties)
            graph.add_node(
                id=node_id,
                label=draw(_labels),
                type=draw(_types),
                properties={k: v for k, v in properties.items() if not k.startswith("_")},
                tags=draw(_tags),
                layer=draw(st.one_of(st.none(), st.sampled_from(["claude-1", "gpt-1"]))),
            )

    for _ in range(draw(st.integers(min_value=0, max_value=5))):
        edge_properties = draw(_properties)
        edge_kwargs = dict(
            source=draw(st.sampled_from(node_ids)),
            target=draw(st.sampled_from(node_ids)),
            relation=draw(st.sampled_from(["field", "call", "ref"])),
            key=draw(st.text(max_size=6)),
            weight=draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False)),
        )
        try:
            graph.add_edge(properties=edge_properties, **edge_kwargs)
        except ValueError:
            assert any(k.startswith("_") for k in edge_properties)
            graph.add_edge(
                properties={k: v for k, v in edge_properties.items() if not k.startswith("_")},
                **edge_kwargs,
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


# --- [23-C] ★★ RN3 이관 = additive copy-forward — 파괴 부재를 불변식으로 ---
#
# RN1(HTTP 프로브)·RN2(pid 검사)의 이관 테스트는 전부 삭제했다. 이유는 커버리지
# 축소가 아니라 **전제 소멸**이다: 두 세대 모두 "다른 프로세스가 이 스토어를
# 쓰는가"를 추론해 rename/unlink 여부를 정하는 구조였고, RN3 는 그 질문과 그
# 조작을 함께 없앴다. 오라클 테스트(live/dead pid, garbled serve.json, 가드 보류)와
# 파괴 순서 테스트(레이스 폴백, 빈 target 교체, 사이드카 rename 차단)는 지금
# 코드에 대응 분기가 존재하지 않는다. 그 테스트들이 지키던 **결과**(구 스토어의
# gold 가 사라지지 않는다)는 아래 감사 테스트 + 동시성 테스트가 더 강하게 지킨다.

import logging
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from visualizebetter.graph import snapshots as snap_mod


@pytest.fixture
def canonical(tmp_path, monkeypatch):
    """canonical (target, legacy) 기본경로 쌍을 tmp 로 고정 — 플랫폼 무관."""
    target = tmp_path / "visualizebetter"
    legacy = tmp_path / "mcpgraph"
    monkeypatch.setattr(snap_mod, "_default_base_pair", lambda: (target, legacy))
    return target, legacy


def _store_db(path: Path, ids) -> None:
    """주어진 id 들의 snapshot 행을 가진 실제 스토어를 만든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(snap_mod._SCHEMA)
        for i in ids:
            con.execute(
                "INSERT OR IGNORE INTO snapshot (id, name, description, created_at,"
                " kind, node_count, edge_count, metadata, layers, version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (i, f"name-{i}", "", "2026-01-01T00:00:00Z", "manual", 1, 0, "{}", "[]", ""),
            )
            con.execute(
                'INSERT OR IGNORE INTO node (snapshot_id, id, label, "type", properties,'
                " tags, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (i, f"n-{i}", "N", "class", "{}", "[]", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00Z"),
            )
        con.commit()
    finally:
        con.close()


def _snapshot_ids(path: Path) -> set:
    if not path.exists():
        return set()
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT id FROM snapshot")}
    except sqlite3.Error:
        return set()
    finally:
        con.close()


# --- L/P: 복사이지 이동이 아니다 ---


def test_legacy_store_is_copied_not_moved(canonical):
    """L — legacy 파일은 바이트 하나 안 바뀌고 그대로 남는다."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    _store_db(legacy_db, ["s1", "s2", "s3"])
    before = legacy_db.read_bytes()

    store = SnapshotStore(target)

    assert store.db_path == target / DB_FILENAME
    assert _snapshot_ids(store.db_path) == {"s1", "s2", "s3"}
    assert legacy_db.exists()
    assert legacy_db.read_bytes() == before   # ★ 원본 불변
    assert legacy.exists()                     # ★ 디렉토리도 그대로


def test_copy_forward_carries_child_rows(canonical):
    """L — snapshot 뿐 아니라 그에 딸린 node 행도 함께 온다."""
    target, legacy = canonical
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1"])

    store = SnapshotStore(target)

    con = sqlite3.connect(store.db_path)
    try:
        assert con.execute("SELECT count(*) FROM node WHERE snapshot_id='s1'").fetchone()[0] == 1
    finally:
        con.close()


def test_same_dir_legacy_filename_is_copied_in_place(tmp_path):
    """P — 같은 디렉토리의 구 파일명도 복사 대상. 임의 --data-dir 이어도 동작한다
    (같은 디렉토리 안이므로 '남의 경로에 마법' 이 아니다)."""
    d = tmp_path / "my-graphs"
    old = d / snap_mod._LEGACY_DB_FILENAME
    _store_db(old, ["a", "b"])

    store = SnapshotStore(d)

    assert store.db_path == d / DB_FILENAME
    assert _snapshot_ids(store.db_path) == {"a", "b"}
    assert old.exists()  # 그대로


def test_arbitrary_data_dir_does_not_reach_the_canonical_legacy(canonical, tmp_path):
    """P — 임의 경로는 canonical legacy 디렉토리를 끌어오지 않는다 (마법 금지)."""
    target, legacy = canonical
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1"])
    other = tmp_path / "elsewhere"

    store = SnapshotStore(other)

    assert _snapshot_ids(store.db_path) == set()
    assert (legacy / snap_mod._LEGACY_DB_FILENAME).exists()


def test_serve_json_is_never_touched(canonical):
    """P — 남의 프로세스 상태(serve.json)는 읽지도 쓰지도 지우지도 않는다.
    RN1/RN2 가드가 바로 이 파일을 근거로 삼았고, 그 파일을 지우기까지 했다."""
    target, legacy = canonical
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1"])
    port_file = legacy / "serve.json"
    port_file.write_text('{"pid": 424242, "url": "http://127.0.0.1:8765"}', encoding="utf-8")
    before = port_file.read_bytes()

    SnapshotStore(target)

    assert port_file.exists()
    assert port_file.read_bytes() == before


# --- M: liveness 오라클 부재 ---


def test_no_liveness_oracle_exists(canonical):
    """M — 오라클 심볼이 모듈에서 사라졌고, '살아있는' serve 광고가 있어도
    복사는 그냥 진행된다 (구 serve 가 계속 써도 우리는 읽기만 하므로 무해)."""
    for gone in ("_pid_alive", "_win_pid_alive", "_serve_alive_in",
                 "_is_empty_store", "_migrate_legacy_dir", "_recover_sqlite_sidecars"):
        assert not hasattr(snap_mod, gone), f"{gone} 이 아직 존재한다"

    target, legacy = canonical
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1"])
    (legacy / "serve.json").write_text(
        json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:8765"}), encoding="utf-8"
    )

    store = SnapshotStore(target)

    assert _snapshot_ids(store.db_path) == {"s1"}  # 살아있는 pid 여도 보류 없음


def test_old_serve_keeps_writing_and_later_run_copies_the_rest(canonical):
    """M — 구 serve 가 legacy 에 계속 쓰는 상황. 1차 복사 후 legacy 에 새 스냅샷이
    생기면, 이후 실행이 차분만큼 추가 복사한다 (보류·파괴 없음)."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    _store_db(legacy_db, ["s1"])

    first = SnapshotStore(target)
    assert _snapshot_ids(first.db_path) == {"s1"}

    _store_db(legacy_db, ["s1", "s2"])          # 구 serve 가 s2 를 추가
    second = SnapshotStore(target)

    assert _snapshot_ids(second.db_path) == {"s1", "s2"}
    assert _snapshot_ids(legacy_db) == {"s1", "s2"}   # legacy 도 온전


def test_repeated_runs_are_idempotent(canonical, caplog):
    """L/O — 재실행은 멱등. 차분이 없으면 아무 일도 하지 않는다."""
    target, legacy = canonical
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1", "s2"])

    SnapshotStore(target)
    caplog.clear()  # 1차 복사 로그가 2차 단언에 섞이지 않게
    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        again = SnapshotStore(target)

    assert _snapshot_ids(again.db_path) == {"s1", "s2"}
    assert "copied" not in caplog.text  # 2회차엔 복사 로그가 없다


def test_target_content_is_never_replaced(canonical):
    """L — target 이 이미 채워져 있어도 legacy 를 덮지 않고 합친다 (INSERT OR IGNORE)."""
    target, legacy = canonical
    _store_db(target / DB_FILENAME, ["t1"])
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1"])

    store = SnapshotStore(target)

    assert _snapshot_ids(store.db_path) == {"t1", "s1"}


# --- N: hot journal ---


def test_hot_journal_is_rolled_back_then_copied(canonical):
    """N — legacy 를 read-write 로 열어 SQLite 가 저널을 처리하게 한다.
    파일 이동이 없으므로 '복구 후 rename' 순서 의존이 아예 없다."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    _store_db(legacy_db, ["s1"])
    (legacy / (legacy_db.name + "-journal")).write_bytes(b"stale journal header")

    store = SnapshotStore(target)

    assert _snapshot_ids(store.db_path) == {"s1"}
    assert legacy_db.exists()


def test_corrupt_legacy_store_destroys_nothing(canonical, caplog):
    """N — 열 수 없는 legacy 는 이번 실행 보류. 아무것도 지우지 않고, target 은
    정상 사용 가능한 채로 남는다 (재시도 가능)."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    legacy.mkdir(parents=True, exist_ok=True)
    legacy_db.write_bytes(b"this is not a sqlite database")

    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        store = SnapshotStore(target)

    assert legacy_db.exists()
    assert legacy_db.read_bytes() == b"this is not a sqlite database"
    assert store.db_path == target / DB_FILENAME
    run(store.initialize())  # 여전히 쓸 수 있다


# --- O: 경로 안전 (RN2 의 URI 문자열 결합 결함) ---


@pytest.mark.parametrize("odd", ["has#hash", "has%20pct", "has'quote", "has space"])
def test_paths_with_uri_metacharacters_work(tmp_path, monkeypatch, odd):
    """O — RN2 는 f"file:{path}?mode=ro" 를 손으로 만들어 '#' 가 경로를 잘라먹고
    data dir 밖에 파일까지 만들었다([11] 위반). 경로는 이제 바인딩 파라미터다."""
    base = tmp_path / odd
    target = base / "visualizebetter"
    legacy = base / "mcpgraph"
    monkeypatch.setattr(snap_mod, "_default_base_pair", lambda: (target, legacy))
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1", "s2"])

    store = SnapshotStore(target)

    assert _snapshot_ids(store.db_path) == {"s1", "s2"}
    # data dir 밖에 아무것도 만들지 않았다
    assert sorted(p.name for p in base.iterdir()) == ["mcpgraph", "visualizebetter"]


# --- Q: 관측성 ---


def test_breadcrumb_is_left_and_is_additive(canonical):
    """Q — 어디로 복사됐는지 남긴다. 새 파일만 추가하고 기존 것은 손대지 않는다."""
    target, legacy = canonical
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1"])

    SnapshotStore(target)

    note = legacy / "MIGRATED-TO-VISUALIZEBETTER.txt"
    assert note.exists()
    body = note.read_text(encoding="utf-8")
    assert str(target / DB_FILENAME) in body
    assert "COPIED" in body
    # ★ RN4 Y: 코드가 삭제를 권하는 문구는 어떤 경우에도 금지 — RN1/RN2 가
    # 데이터를 파괴한 바로 그 조언을 글로 하는 것과 같다.
    lowered = body.lower()
    for advice in ("delete", "remove", "unused", "safe to"):
        assert advice not in lowered, f"breadcrumb 이 정리를 권한다: {advice!r}"


def test_copy_count_is_logged(canonical, caplog):
    """Q — 복사 건수를 남긴다 (기동 시 무슨 일이 있었는지 알 수 있게)."""
    target, legacy = canonical
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1", "s2", "s3"])

    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        SnapshotStore(target)

    assert "copied 3 snapshot(s)" in caplog.text


# --- R: 파괴 부재의 정적 감사 + 실프로세스 동시성 ---


# [23-C] RN4 EE(2): 파일을 지우거나·옮기거나·덮어쓰는 호출 이름. `str.replace` 와
# 이름이 겹치는 `Path.replace` 때문에 문자열 검색은 오탐이 난다 — AST 로 "호출된
# 이름"만 본다.
_DESTRUCTIVE_CALLS = frozenset({
    "unlink", "rename", "replace", "renames", "rmdir", "removedirs",
    "remove", "truncate", "move", "rmtree", "write_bytes", "write_text",
})


def test_source_contains_no_destructive_file_calls():
    """R6 ★ 감사 — snapshots.py 에 사용자 데이터 파일을 지우거나 옮기는 호출이
    없음을 소스에서 직접 단언한다. RN1·RN2 의 blocker 는 둘 다 '추론 + 파괴'
    구조에서 나왔으므로, 파괴 호출의 재도입 자체를 여기서 막는다.

    예외 하나: breadcrumb 은 legacy 디렉토리에 **새 파일**을 만든다(덮어쓰지
    않는다 — 존재하면 즉시 반환). 그 한 곳만 허용하고 나머지는 전부 금지."""
    import ast

    source = Path(snap_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed_line = next(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_leave_breadcrumb"
    )
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in _DESTRUCTIVE_CALLS:
            if name == "write_text" and node.lineno > allowed_line:
                continue  # breadcrumb 작성 (새 파일만)
            offenders.append(f"{name}() @ line {node.lineno}")
    assert offenders == [], f"파괴적 파일 호출이 재도입됐다: {offenders}"


def test_concurrent_processes_leave_both_stores_intact(canonical):
    """R1 ★ 실프로세스 N개 동시 실행 — RN2 의 unlink→rename 은 여기서 25회 중
    1회 'gold 전멸'(target·legacy 둘 다 없음)을 냈다. 복사는 원리적으로 불가능."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    ids = [f"s{i}" for i in range(5)]
    _store_db(legacy_db, ids)
    legacy_bytes = legacy_db.read_bytes()

    program = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path.cwd())!r})
        from pathlib import Path
        from visualizebetter.graph import snapshots as s
        target, legacy = Path({str(target)!r}), Path({str(legacy)!r})
        s._default_base_pair = lambda: (target, legacy)
        s.SnapshotStore(target)
        """
    )
    procs = [subprocess.Popen([sys.executable, "-c", program]) for _ in range(6)]
    for proc in procs:
        proc.wait(timeout=120)

    assert legacy_db.exists()
    assert legacy_db.read_bytes() == legacy_bytes          # ★ 원본 불변
    assert _snapshot_ids(target / DB_FILENAME) == set(ids)  # ★ 전부 도착, 중복 없음


# --- [23-C] ★★ RN3 설계 보정 S~W — 코디 선행검증이 실측한 결함 고정 ---


def _legacy_with_narrow_schema(path: Path) -> None:
    """구스키마 legacy: node 에 label 컬럼이 없다 (target 은 NOT NULL 로 요구)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE snapshot (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, kind TEXT NOT NULL, node_count INTEGER NOT NULL,
                edge_count INTEGER NOT NULL, metadata TEXT NOT NULL, layers TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT '');
            CREATE TABLE node (
                snapshot_id TEXT NOT NULL, id TEXT NOT NULL, "type" TEXT NOT NULL,
                properties TEXT NOT NULL, tags TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, id));
            CREATE TABLE edge (
                snapshot_id TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
                relation TEXT NOT NULL, "key" TEXT NOT NULL DEFAULT '',
                properties TEXT NOT NULL, tags TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, source, target, relation, "key"));
            CREATE TABLE finding (
                snapshot_id TEXT NOT NULL, finding_id TEXT NOT NULL, title TEXT NOT NULL,
                body TEXT NOT NULL, confidence REAL NOT NULL, evidence TEXT NOT NULL,
                tags TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, finding_id));
            CREATE TABLE finding_node (
                snapshot_id TEXT NOT NULL, finding_id TEXT NOT NULL, node_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, PRIMARY KEY (snapshot_id, finding_id, ordinal));
            """
        )
        con.execute(
            "INSERT INTO snapshot VALUES ('s1','n','', '2026-01-01T00:00:00Z','manual',3,0,'{}','[]','')"
        )
        for i in range(3):
            con.execute(
                'INSERT INTO node (snapshot_id, id, "type", properties, tags, created_at,'
                " updated_at) VALUES ('s1',?,'class','{}','[]','z','z')",
                (f"n{i}",),
            )
        con.commit()
    finally:
        con.close()


def test_narrow_legacy_schema_aborts_without_writing_anything(canonical, caplog):
    """(7) S(1) ★ 구스키마 legacy 는 전체 중단. RN3 1차 구현은 OR IGNORE 가
    NOT NULL 위반 행을 조용히 건너뛰어 snapshot 은 들어가고 node 3행이 전부
    소실됐는데도 '복사 성공' 을 반환하고 breadcrumb 까지 남겼다."""
    target, legacy = canonical
    _legacy_with_narrow_schema(legacy / snap_mod._LEGACY_DB_FILENAME)

    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        store = SnapshotStore(target)

    assert _snapshot_ids(store.db_path) == set()          # 부분 복사조차 없다
    assert not (legacy / "MIGRATED-TO-VISUALIZEBETTER.txt").exists()  # 거짓 안내 없음
    assert "aborted" in caplog.text
    assert "label" in caplog.text                          # 어느 컬럼이 없는지 말한다


def test_silently_dropped_rows_trigger_rollback(canonical, caplog):
    """(7b) S(3) ★ 컬럼은 다 있지만 값이 NULL 인 legacy 행 — OR IGNORE 가 target 의
    NOT NULL 위반으로 조용히 건너뛴다. 사전 컬럼 검증(S1)만으로는 못 잡는 축이고,
    잡지 못하면 '복사됐다'가 거짓이 된 채 원장에 굳어 영구 복구 불가가 된다."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    legacy.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(legacy_db)
    try:
        # target 과 같은 컬럼 구성이되 label 이 nullable 인 legacy
        con.executescript(snap_mod._SCHEMA.replace("label         TEXT NOT NULL", "label         TEXT"))
        con.execute(
            "INSERT INTO snapshot (id, name, description, created_at, kind, node_count,"
            " edge_count, metadata, layers, version)"
            " VALUES ('s1','n','','z','manual',1,0,'{}','[]','')"
        )
        con.execute(
            'INSERT INTO node (snapshot_id, id, label, "type", properties, tags,'
            " created_at, updated_at) VALUES ('s1','n1',NULL,'class','{}','[]','z','z')"
        )
        con.commit()
    finally:
        con.close()

    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        store = SnapshotStore(target)

    assert "skipped" in caplog.text
    assert _snapshot_ids(store.db_path) == set()      # 부분 상태가 남지 않았다
    assert _snapshot_ids(legacy_db) == {"s1"}         # legacy 는 온전
    assert not (legacy / "MIGRATED-TO-VISUALIZEBETTER.txt").exists()  # 거짓 안내 없음


def test_ledger_prevents_recopy_after_prune_and_user_delete(canonical):
    """(8) T ★ 원장 — target snapshot 테이블을 '이미 복사됨'의 근거로 쓰면
    auto prune 과 사용자 삭제가 다음 부팅에 되살아난다([5-E] 위반)."""
    target, legacy = canonical
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1", "s2", "s3"])

    first = SnapshotStore(target)
    assert _snapshot_ids(first.db_path) == {"s1", "s2", "s3"}

    # 사용자가 s2 를 지우고(또는 prune 이 정리하고) 재부팅한다
    con = sqlite3.connect(first.db_path)
    try:
        con.execute("DELETE FROM snapshot WHERE id='s2'")
        con.commit()
    finally:
        con.close()

    second = SnapshotStore(target)

    assert _snapshot_ids(second.db_path) == {"s1", "s3"}   # ★ 부활하지 않는다
    assert _snapshot_ids(legacy / snap_mod._LEGACY_DB_FILENAME) == {"s1", "s2", "s3"}


@pytest.mark.parametrize(
    "wreck",
    ["null_id", "not_a_db", "empty_file", "no_tables"],
)
def test_no_legacy_state_can_prevent_startup(canonical, wreck):
    """(9) U ★ 이관 실패가 부팅을 막지 못한다. NULL snapshot id 는 RN3 1차
    구현에서 sorted() TypeError 로 __init__ 를 탈출해 serve·전 CLI 를 기동
    불가로 만들었다 (raw traceback, 탈출 방법 안내 없음)."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    legacy.mkdir(parents=True, exist_ok=True)

    if wreck == "null_id":
        _store_db(legacy_db, ["ok"])
        con = sqlite3.connect(legacy_db)
        try:
            con.execute(
                "INSERT INTO snapshot (id, name, description, created_at, kind,"
                " node_count, edge_count, metadata, layers, version)"
                " VALUES (NULL,'x','', 'z','manual',0,0,'{}','[]','')"
            )
            con.commit()
        finally:
            con.close()
    elif wreck == "not_a_db":
        legacy_db.write_bytes(b"definitely not sqlite")
    elif wreck == "empty_file":
        legacy_db.write_bytes(b"")
    else:
        con = sqlite3.connect(legacy_db)
        con.execute("CREATE TABLE unrelated (x)")
        con.commit()
        con.close()

    store = SnapshotStore(target)          # ★ 예외가 밖으로 나오면 실패
    run(store.initialize())                # 그리고 실제로 쓸 수 있어야 한다
    assert store.db_path == target / DB_FILENAME


def test_finding_history_columns_survive_the_copy(canonical):
    """(V) 스키마 통합 — 복사 경로가 ALTER 를 빼먹으면 공유 컬럼 교집합이 좁아져
    superseded/provenance 가 조용히 사라진다 (이후 ALTER 가 '[]' 로 채워 흔적도 없음)."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    _store_db(legacy_db, ["s1"])
    con = sqlite3.connect(legacy_db)
    try:
        con.execute(
            "INSERT INTO finding (snapshot_id, finding_id, title, body, confidence,"
            " evidence, tags, created_at, updated_at, superseded, provenance)"
            " VALUES ('s1','f1','t','b',0.9,'[]','[]','z','z',?,'[]')",
            (json.dumps([{"prev": {"body": "old"}, "at": "z", "by": None}]),),
        )
        con.commit()
    finally:
        con.close()

    store = SnapshotStore(target)

    con = sqlite3.connect(store.db_path)
    try:
        (superseded,) = con.execute(
            "SELECT superseded FROM finding WHERE finding_id='f1'"
        ).fetchone()
    finally:
        con.close()
    assert json.loads(superseded)[0]["prev"] == {"body": "old"}   # 이력이 살아서 왔다


def test_hot_journal_legacy_preserves_committed_content(canonical):
    """(10) W ★ 불변식은 바이트가 아니라 **내용**이다.

    legacy 를 read-write 로 여는 것은 의도다 — 그래야 SQLite 가 hot journal 을
    롤백한다. 롤백은 legacy 바이트를 재작성하므로 바이트 동일성은 단정하지
    않는다. read-only 로 열어 바이트를 지키려 하면 크래시한 스토어가 전부
    조용히 영구 미복사가 되는데, 그쪽이 훨씬 나쁘다."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    _store_db(legacy_db, ["s1", "s2"])
    committed = _snapshot_ids(legacy_db)
    (legacy / (legacy_db.name + "-journal")).write_bytes(b"stale journal header")

    store = SnapshotStore(target)

    assert _snapshot_ids(store.db_path) == committed   # 커밋된 내용이 그대로 왔고
    assert _snapshot_ids(legacy_db) == committed       # legacy 에도 그대로 남아있다


def test_copy_upgrades_an_old_target_schema_before_computing_columns(canonical):
    """(V) ★ 스키마 통합 — target 이 [24-C] 이전 스키마면 복사 경로가 ALTER 를
    먼저 적용해야 한다. 안 그러면 공유 컬럼 교집합에서 superseded/provenance 가
    빠져 이력이 조용히 사라지고, 이후 ALTER 가 '[]' 로 채워 흔적조차 남지 않는다."""
    target, legacy = canonical
    target.mkdir(parents=True, exist_ok=True)
    old_target = target / DB_FILENAME
    con = sqlite3.connect(old_target)
    try:  # 이력 컬럼이 없는 구 target
        con.executescript(
            snap_mod._SCHEMA.replace("    superseded  TEXT NOT NULL DEFAULT '[]',\n", "")
            .replace("    provenance  TEXT NOT NULL DEFAULT '[]',\n", "")
        )
        con.commit()
    finally:
        con.close()
    assert "superseded" not in {
        r[1] for r in sqlite3.connect(old_target).execute('PRAGMA table_info("finding")')
    }

    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    _store_db(legacy_db, ["s1"])
    con = sqlite3.connect(legacy_db)
    try:
        con.execute(
            "INSERT INTO finding (snapshot_id, finding_id, title, body, confidence,"
            " evidence, tags, created_at, updated_at, superseded, provenance)"
            " VALUES ('s1','f1','t','b',0.9,'[]','[]','z','z',?,'[]')",
            (json.dumps([{"prev": {"body": "old"}, "at": "z", "by": None}]),),
        )
        con.commit()
    finally:
        con.close()

    store = SnapshotStore(target)

    con = sqlite3.connect(store.db_path)
    try:
        (superseded,) = con.execute(
            "SELECT superseded FROM finding WHERE finding_id='f1'"
        ).fetchone()
    finally:
        con.close()
    assert json.loads(superseded)[0]["prev"] == {"body": "old"}


def test_a_non_sqlite_failure_still_cannot_prevent_startup(canonical, monkeypatch, caplog):
    """(U) ★ 이관은 부가 기능이다 — 어떤 종류의 예외든 부팅을 막으면 오답.

    sqlite3.Error 만 잡으면 그 밖의 실패(스키마 적용 중 OS 오류 등)가 그대로
    __init__ 를 탈출해 serve·전 CLI 가 기동 불가가 된다."""
    target, legacy = canonical
    _store_db(legacy / snap_mod._LEGACY_DB_FILENAME, ["s1"])

    def boom(_connection):
        raise RuntimeError("schema application blew up")

    monkeypatch.setattr(snap_mod, "_apply_schema", boom)
    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        store = SnapshotStore(target)   # ★ 예외가 밖으로 나오면 실패

    assert "copy-forward failed" in caplog.text
    assert store.db_path == target / DB_FILENAME
    assert (legacy / snap_mod._LEGACY_DB_FILENAME).exists()   # 파괴 0


# --- [23-C] ★★★ RN4 X~FF — 기존 서브시스템과의 상호작용 결함 고정 ---


def _store_db_kinds(path: Path, rows) -> None:
    """(id, kind) 쌍으로 스토어를 만든다 — created_at 은 순서대로."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(snap_mod._SCHEMA)
        for n, (sid, kind) in enumerate(rows):
            con.execute(
                "INSERT OR IGNORE INTO snapshot (id, name, description, created_at, kind,"
                " node_count, edge_count, metadata, layers, version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, f"name-{sid}", "", f"2026-01-01T00:00:{n:02d}Z", kind, 0, 0, "{}", "[]", ""),
            )
        con.commit()
    finally:
        con.close()


def _kinds(path: Path) -> dict:
    con = sqlite3.connect(path)
    try:
        return {r[0]: r[1] for r in con.execute("SELECT id, kind FROM snapshot")}
    finally:
        con.close()


# --- X: rolling GC 상호작용 ---


def test_migrated_autos_survive_prune_and_do_not_evict_user_autos(canonical, caplog):
    """(X) ★ blocker — legacy auto 를 target 의 auto 풀에 부으면 prune 이 지우고
    원장이 재복사를 막아 영구 손실. 더 나쁜 것은 병합 풀이 **사용자 자신의 auto**
    를 evict 한다 (이관을 안 했으면 살아있었을 데이터)."""
    target, legacy = canonical
    _store_db_kinds(
        legacy / snap_mod._LEGACY_DB_FILENAME,
        [(f"L{i}", "auto") for i in range(25)] + [("Lm", "manual")],
    )

    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        store = SnapshotStore(target)

    kinds = _kinds(store.db_path)
    migrated = {i for i in kinds if i.startswith("L")}
    assert "Lm" in migrated                                     # manual 은 전부
    assert len(migrated) == 1 + snap_mod.MIGRATE_AUTO_BUDGET    # auto 는 예산만큼
    assert all(kinds[i] == "manual" for i in migrated)          # ★ prune 대상 밖
    assert "auto budget exhausted" in caplog.text               # 무언의 절단 금지

    # 사용자가 이후 자기 auto 를 20건 만든다 (실 prune API 사용)
    graph = Graph(name="user")
    for n in range(20):
        graph.add_node(id=f"u{n}", label="U", type="class")
        run(store.save_snapshot(graph, name=f"user-auto-{n}", kind=snap_mod.SNAPSHOT_KIND_AUTO))
        run(store.prune_auto_snapshots())

    after = _kinds(store.db_path)
    user_autos = [i for i, k in after.items() if k == "auto"]
    assert len(user_autos) == 20                       # ★ 사용자 auto 무손실
    assert migrated <= set(after)                      # ★ 이관분도 전부 생존


def test_migrated_snapshots_are_named_for_provenance(canonical):
    """(X) kind 를 새로 만들지 않고 name 접두로 출처를 남긴다."""
    target, legacy = canonical
    _store_db_kinds(legacy / snap_mod._LEGACY_DB_FILENAME, [("a1", "auto"), ("m1", "manual")])

    store = SnapshotStore(target)

    con = sqlite3.connect(store.db_path)
    try:
        names = {r[0]: r[1] for r in con.execute("SELECT id, name FROM snapshot")}
    finally:
        con.close()
    assert names["a1"].startswith(snap_mod._MIGRATED_NAME_PREFIX)
    assert names["m1"] == "name-m1"   # manual 은 이름을 건드리지 않는다


# --- Y: breadcrumb 이 살아있는 디렉토리에 삭제를 권하지 않는다 ---


def test_no_breadcrumb_when_legacy_lives_in_the_data_dir(tmp_path):
    """(Y) ★ 후보 #1 은 data_dir/mcpgraph.sqlite3 — RN2 를 거친 사용자의 실제
    상태다. 그 디렉토리에 "unused; delete it" 노트를 쓰면, 방금 이관한 gold·원장·
    serve.json 이 같이 들어있는 디렉토리를 지우라고 코드가 글로 지시하게 된다."""
    d = tmp_path / "data"
    _store_db(d / snap_mod._LEGACY_DB_FILENAME, ["s1"])

    store = SnapshotStore(d)

    assert _snapshot_ids(store.db_path) == {"s1"}      # 복사는 정상
    assert not (d / "MIGRATED-TO-VISUALIZEBETTER.txt").exists()   # ★ 노트 없음


# --- Z: 스냅샷 단위 트랜잭션 ---


def test_one_bad_snapshot_does_not_hold_the_healthy_ones_hostage(canonical, caplog):
    """(Z) ★ all-or-nothing 이면 오염 1건 때문에 정상 20건이 매 부팅 rollback 되어
    영구히 도착하지 않는다 (340MB 스토어에선 부팅마다 2s 를 태우면서)."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    legacy.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(legacy_db)
    try:
        con.executescript(snap_mod._SCHEMA.replace("label         TEXT NOT NULL", "label         TEXT"))
        for n in range(20):  # 정상 20건
            con.execute(
                "INSERT INTO snapshot (id, name, description, created_at, kind, node_count,"
                " edge_count, metadata, layers, version)"
                f" VALUES ('ok{n}','n','','2026-01-01T00:00:{n:02d}Z','manual',1,0,'{{}}','[]','')"
            )
            con.execute(
                'INSERT INTO node (snapshot_id, id, label, "type", properties, tags,'
                f" created_at, updated_at) VALUES ('ok{n}','n1','L','class','{{}}','[]','z','z')"
            )
        con.execute(  # 오염 1건 — ★ 가장 먼저 처리되도록 created_at 을 앞에 둔다.
            # 뒤에 두면 "첫 실패에서 전체 중단" 회귀를 테스트가 놓친다 (정상분이
            # 이미 커밋된 뒤라 차이가 안 보인다).
            "INSERT INTO snapshot (id, name, description, created_at, kind, node_count,"
            " edge_count, metadata, layers, version)"
            " VALUES ('bad','n','','2020-01-01T00:00:00Z','manual',1,0,'{}','[]','')"
        )
        con.execute(
            'INSERT INTO node (snapshot_id, id, label, "type", properties, tags,'
            " created_at, updated_at) VALUES ('bad','n1',NULL,'class','{}','[]','z','z')"
        )
        con.commit()
    finally:
        con.close()

    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        store = SnapshotStore(target)

    arrived = _snapshot_ids(store.db_path)
    assert arrived == {f"ok{n}" for n in range(20)}   # ★ 정상 20건 도착
    assert "bad" not in arrived                        # 오염 1건만 skip
    assert "snapshot bad skipped" in caplog.text

    # 원장에 안 남아 다음 실행이 재시도한다 (그리고 다시 skip 하지만 정상분은 재복사 안 함)
    con = sqlite3.connect(store.db_path)
    try:
        ledger = {r[0] for r in con.execute("SELECT snapshot_id FROM copied_snapshot")}
    finally:
        con.close()
    assert "bad" not in ledger
    assert ledger == {f"ok{n}" for n in range(20)}

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
        again = SnapshotStore(target)
    assert _snapshot_ids(again.db_path) == arrived
    assert "copied 20" not in caplog.text   # 전량 재복사(성능 회귀) 없음


# --- BB: 원장 키 = snapshot_id 단독 ---


def test_a_second_source_path_does_not_resurrect_a_deleted_snapshot(tmp_path, monkeypatch):
    """(BB) ★ 원장을 (source_db, snapshot_id) 로 키잉하면, 같은 내용이 다른 경로로
    보일 때(8.3 단축명·junction·두 번째 후보 경로) 새 source 로 취급돼 사용자가
    지운 스냅샷이 부활한다 — T 가 막으려던 [5-E] 위반이 형태만 옮겨간 것.
    snapshot_id 는 uuid4 라 전역 유일하므로 id 단독 키면 이 축이 원천 소멸한다."""
    base = tmp_path / "base"
    target = base / "visualizebetter"
    legacy_a = base / "old-a"
    legacy_b = base / "old-b"
    monkeypatch.setattr(snap_mod, "_default_base_pair", lambda: (target, legacy_a))
    _store_db(legacy_a / snap_mod._LEGACY_DB_FILENAME, ["s1", "s2"])

    monkeypatch.setattr(
        snap_mod, "_legacy_db_candidates",
        lambda _d: [legacy_a / snap_mod._LEGACY_DB_FILENAME],
    )
    first = SnapshotStore(target)
    assert _snapshot_ids(first.db_path) == {"s1", "s2"}

    con = sqlite3.connect(first.db_path)      # 사용자가 s2 를 지운다
    try:
        con.execute("DELETE FROM snapshot WHERE id='s2'")
        con.commit()
    finally:
        con.close()

    # 같은 스토어가 다른 경로로 다시 후보에 오른다 (별칭/두 번째 후보 경로)
    legacy_b.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        legacy_a / snap_mod._LEGACY_DB_FILENAME, legacy_b / snap_mod._LEGACY_DB_FILENAME
    )
    monkeypatch.setattr(
        snap_mod, "_legacy_db_candidates",
        lambda _d: [legacy_b / snap_mod._LEGACY_DB_FILENAME],
    )
    second = SnapshotStore(target)

    assert _snapshot_ids(second.db_path) == {"s1"}   # ★ 부활하지 않는다


def test_ledger_primary_key_is_snapshot_id_alone(tmp_path):
    """(BB) 스키마 계약 고정 — 별칭 축이 원천 소멸하려면 PK 가 id 단독이어야 한다."""
    store = SnapshotStore(tmp_path / "d")
    run(store.initialize())
    con = sqlite3.connect(store.db_path)
    try:
        pk = [r[1] for r in con.execute('PRAGMA table_info("copied_snapshot")') if r[5]]
    finally:
        con.close()
    assert pk == ["snapshot_id"]


# --- FF: 무인자 배선 ---


def test_default_wiring_uses_default_data_dir(tmp_path, monkeypatch):
    """(FF) SnapshotStore() 무인자 경로 — stdio_proxy·cli 가 쓰는 배선인데 직접
    검증이 0건이었다."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    expected = snap_mod.default_data_dir()
    store = SnapshotStore()

    assert store.data_dir == expected
    assert store.db_path == expected / DB_FILENAME
    assert store.data_dir.is_dir()
    run(store.initialize())
    assert store.db_path.exists()


# --- [23-C] ★★★★ RN5 GG~KK — RN4 설계 결함 보정 ---


def _migration_rows(db: Path) -> list:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT copied, declined, failed, aborted, deferred FROM migration_run"
            " ORDER BY rowid"
        ).fetchall()
    finally:
        con.close()


# --- GG: 누적 예산 (부팅 간 안정) ---


def test_auto_budget_is_cumulative_across_boots(canonical, caplog):
    """(GG) ★ blocker — RN4 는 원장 차분 **뒤** 집합에 상한을 걸어서, 거절된 auto 가
    원장에 안 남고 다음 부팅에 전부 들어왔다 (boot1 21 → boot2 26 → 계속 누적).
    예산이 durable 해야 '최신 N 만' 정책과 거절 건수 진술이 부팅 간 참이 된다."""
    target, legacy = canonical
    _store_db_kinds(
        legacy / snap_mod._LEGACY_DB_FILENAME,
        [(f"L{i:02d}", "auto") for i in range(25)] + [("Lm", "manual")],
    )
    budget = snap_mod.MIGRATE_AUTO_BUDGET

    totals, declines = [], []
    for _ in range(3):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=snap_mod.log.name):
            store = SnapshotStore(target)
        ids = _snapshot_ids(store.db_path)
        totals.append(len({i for i in ids if i.startswith("L") and i != "Lm"}))
        declines.append(
            [ln for ln in caplog.text.splitlines() if "auto budget exhausted" in ln]
        )

    assert totals == [budget] * 3          # (a) 예산 초과 없음 (b) 추가 유입 없음
    assert all(d for d in declines)        # (c) 거절 진술이 매 부팅 존재
    assert declines[0][0].split("declined")[1] == declines[2][0].split("declined")[1]
    assert "Lm" in _snapshot_ids(store.db_path)   # (d) manual 은 전량 도착

    # 최신순으로 골랐는지 — L24 가 최신이다
    assert "L24" in _snapshot_ids(store.db_path)
    assert "L00" not in _snapshot_ids(store.db_path)


def test_budget_holds_when_the_old_app_keeps_writing_autos(canonical):
    """(GG) 구 앱이 계속 auto 를 만들어도 총수는 예산에 고정된다."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    _store_db_kinds(legacy_db, [(f"A{i:02d}", "auto") for i in range(10)])
    first = SnapshotStore(target)
    migrated = {i for i in _snapshot_ids(first.db_path) if i.startswith("A")}
    assert len(migrated) == snap_mod.MIGRATE_AUTO_BUDGET

    _store_db_kinds(legacy_db, [(f"B{i:02d}", "auto") for i in range(10)])  # 구 앱이 추가
    second = SnapshotStore(target)

    after = {i for i in _snapshot_ids(second.db_path) if i[0] in "AB"}
    assert len(after) == snap_mod.MIGRATE_AUTO_BUDGET     # ★ 총수 불변


def test_ledger_remembers_origin_kind(canonical):
    """(GG) 예산은 원장의 origin_kind 로 계산된다 — 이름 문자열 의존 금지."""
    target, legacy = canonical
    _store_db_kinds(legacy / snap_mod._LEGACY_DB_FILENAME, [("a1", "auto"), ("m1", "manual")])
    store = SnapshotStore(target)
    con = sqlite3.connect(store.db_path)
    try:
        kinds = dict(con.execute("SELECT snapshot_id, origin_kind FROM copied_snapshot"))
    finally:
        con.close()
    assert kinds == {"a1": "auto", "m1": "manual"}


# --- HH: 로그 밖으로 내구화 ---


def test_migration_is_recorded_durably_and_appended(canonical):
    """(HH) ★ 두 배포 형태 모두 stderr 를 버린다 — 이관 서사가 log.warning 에만
    있으면 대상 사용자에게는 표면이 0개다."""
    target, legacy = canonical
    _store_db_kinds(legacy / snap_mod._LEGACY_DB_FILENAME, [("s1", "manual")])

    first = SnapshotStore(target)
    log_path = target / snap_mod._MIGRATION_LOG_NAME
    assert log_path.exists()
    first_body = log_path.read_text(encoding="utf-8")
    assert "copied=1" in first_body
    rows = _migration_rows(first.db_path)
    assert rows and rows[-1][0] == 1

    SnapshotStore(target)   # 2회차
    body = log_path.read_text(encoding="utf-8")
    assert body.startswith(first_body)          # ★ append (기존 줄 보존)
    assert len(body.splitlines()) >= 2
    assert len(_migration_rows(first.db_path)) >= 2


def test_abort_reason_is_recorded(canonical):
    """(HH) S(1) 중단 사유가 내구 표면에 남는다."""
    target, legacy = canonical
    _legacy_with_narrow_schema(legacy / snap_mod._LEGACY_DB_FILENAME)

    store = SnapshotStore(target)

    body = (target / snap_mod._MIGRATION_LOG_NAME).read_text(encoding="utf-8")
    assert "missing-columns:node" in body
    assert "label" in body
    assert any("missing-columns" in (r[3] or "") for r in _migration_rows(store.db_path))


def test_skipped_snapshot_is_recorded(canonical):
    """(HH) Z 의 skip 건수도 내구 표면에 남는다."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    legacy.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(legacy_db)
    try:
        con.executescript(
            snap_mod._SCHEMA.replace("label         TEXT NOT NULL", "label         TEXT")
        )
        con.execute(
            "INSERT INTO snapshot (id, name, description, created_at, kind, node_count,"
            " edge_count, metadata, layers, version)"
            " VALUES ('bad','n','','z','manual',1,0,'{}','[]','')"
        )
        con.execute(
            'INSERT INTO node (snapshot_id, id, label, "type", properties, tags,'
            " created_at, updated_at) VALUES ('bad','n1',NULL,'class','{}','[]','z','z')"
        )
        con.commit()
    finally:
        con.close()

    store = SnapshotStore(target)

    assert "failed=1" in (target / snap_mod._MIGRATION_LOG_NAME).read_text(encoding="utf-8")
    assert any(r[2] == 1 for r in _migration_rows(store.db_path))


def test_audit_allows_append_but_not_truncating_writes():
    """(HH/EE) 정적 감사 — append 는 허용, 'w'/truncate 는 계속 금지."""
    import ast

    source = Path(snap_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [
                kw.value.value for kw in node.keywords
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant)
            ]
            assert modes, f"open() without an explicit mode @ line {node.lineno}"
            for mode in modes:
                assert "w" not in mode and "+" not in mode, (
                    f"truncating open({mode!r}) @ line {node.lineno}"
                )


# --- II: readiness 앞 비용에 상한 ---


def test_migration_gives_up_within_its_deadline_when_the_target_is_locked(canonical):
    """(II) ★ sqlite3 timeout 은 **문장당**이라 30s 설정은 잠긴 target 에서 84s 를
    태웠다 (후보 2개면 ~168s). 소비자 예산은 proxy 25s / Tauri 40s 이고 Tauri 는
    실패 시 재시도가 없어 웹뷰가 영구 로딩에 머문다."""
    target, legacy = canonical
    # 여러 건이어야 문장당 락 대기가 누적된다 — 1건이면 deadline 없이도 짧게 끝나
    # 회귀를 못 잡는다 (락 timeout 4s × 건수 vs 전체 예산 10s).
    _store_db_kinds(
        legacy / snap_mod._LEGACY_DB_FILENAME, [(f"s{i}", "manual") for i in range(6)]
    )
    target.mkdir(parents=True, exist_ok=True)
    target_db = target / DB_FILENAME
    _store_db(target_db, [])

    holder = sqlite3.connect(target_db, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")     # target 을 잠근 프로세스가 있다
        started = time.monotonic()
        store = SnapshotStore(target)
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    budget = snap_mod._COPY_DEADLINE_S
    assert elapsed < budget + 6, f"이관이 {elapsed:.1f}s 를 태웠다 (예산 {budget}s)"
    assert store.db_path == target_db          # 기동은 정상적으로 끝난다
    # 지켜야 할 불변식은 "시간 안에 끝나고, 왜 못 했는지가 내구 표면에 남는다" 이다.
    # 어느 단계에서 포기했는지(스키마 적용 실패 vs deadline 도달)는 락의 타이밍에
    # 달려 있으므로 둘 다 허용한다.
    body = (target / snap_mod._MIGRATION_LOG_NAME).read_text(encoding="utf-8")
    assert "deferred=1" in body or "aborted=error:" in body
    assert "copied=0" in body


def test_normal_path_has_no_deadline_regression(canonical):
    """(II) 정상 경로는 빨라야 한다 — deadline 도입이 성능 회귀를 만들지 않았는지."""
    target, legacy = canonical
    _store_db_kinds(legacy / snap_mod._LEGACY_DB_FILENAME, [(f"s{i}", "manual") for i in range(5)])
    started = time.monotonic()
    store = SnapshotStore(target)
    assert time.monotonic() - started < 5
    assert len(_snapshot_ids(store.db_path)) == 5


def test_timing_constants_stay_ordered():
    """(II) 이관 ≤10s < proxy 25s < Tauri 40s — 한쪽만 늘리면 기동 행이 되살아난다."""
    from visualizebetter import stdio_proxy

    assert snap_mod._COPY_DEADLINE_S < stdio_proxy.LAUNCH_TIMEOUT_S
    assert stdio_proxy.LAUNCH_TIMEOUT_S < 40   # Tauri wait_for_serve (main.rs)
    assert snap_mod._COPY_LOCK_TIMEOUT_S < snap_mod._COPY_DEADLINE_S


def test_expired_deadline_defers_the_remaining_snapshots(canonical):
    """(II) deadline 분기 직격 — 락 시나리오는 스키마 단계에서 먼저 중단돼 이
    분기에 도달하지 못한다. 예산이 이미 소진된 상태로 호출해 보류 경로를 고정한다."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    _store_db_kinds(legacy_db, [(f"s{i}", "manual") for i in range(4)])
    target.mkdir(parents=True, exist_ok=True)

    outcome = snap_mod._copy_forward(
        legacy_db, target / DB_FILENAME, deadline=time.monotonic() - 1
    )

    # RN6 MM 이후로는 단계 경계에서 더 일찍 보류될 수 있다 — 그때는 pending 을
    # 세기 전이라 -1(미집계)이다. 어느 쪽이든 '이번엔 못 함, 다음에 재개' 다.
    assert outcome.deferred != 0
    assert outcome.copied == 0
    assert _snapshot_ids(target / DB_FILENAME) == set()   # 아무것도 안 들어갔고
    assert _snapshot_ids(legacy_db) == {f"s{i}" for i in range(4)}   # legacy 온전

    # 예산이 있는 다음 실행이 정확히 이어받는다 (additive + 원장)
    resumed = snap_mod._copy_forward(legacy_db, target / DB_FILENAME)
    assert resumed.deferred == 0
    assert resumed.copied == 4
    assert _snapshot_ids(target / DB_FILENAME) == {f"s{i}" for i in range(4)}


def test_budget_is_not_wasted_on_already_copied_snapshots(canonical):
    """(GG 정정 1) ★ 선택을 legacy **전체 인구**에서 최신순으로 하면, 이미 이관된
    최신 건들이 남은 예산을 먹고 pending 교집합이 비어 아무것도 복사되지 않는다 —
    예산이 남아있는데 오래된 것들이 영구 거절된다. 선택은 pending 안에서 한다."""
    target, legacy = canonical
    # A1 이 가장 최신, A6 이 가장 오래됨
    _store_db_kinds(
        legacy / snap_mod._LEGACY_DB_FILENAME,
        [(f"A{i}", "auto") for i in (6, 5, 4, 3, 2, 1)],
    )
    store = SnapshotStore(target)
    assert len(_snapshot_ids(store.db_path)) == snap_mod.MIGRATE_AUTO_BUDGET

    # '최신 3건만 이관된' 상태로 되돌린다 (정정 1 의 전제)
    con = sqlite3.connect(store.db_path)
    try:
        con.execute("DELETE FROM copied_snapshot WHERE snapshot_id NOT IN ('A1','A2','A3')")
        con.execute("DELETE FROM snapshot WHERE id NOT IN ('A1','A2','A3')")
        con.commit()
    finally:
        con.close()

    resumed = SnapshotStore(target)

    ids = _snapshot_ids(resumed.db_path)
    assert len(ids) == snap_mod.MIGRATE_AUTO_BUDGET   # 남은 예산 2 를 실제로 쓴다
    assert {"A4", "A5"} <= ids                        # pending 중 최신부터


def test_a_store_larger_than_the_deadline_completes_across_boots(canonical):
    """(II 정정 2) ★ deadline 을 넘는 스토어는 여러 부팅에 걸쳐 진행되는 것이
    **설계된 동작**이다 — 스냅샷 단위 커밋·원장 덕에 부분 진행이 영속되므로
    다음 부팅이 정확히 이어받아 결국 완료된다 (절단이 아니다)."""
    target, legacy = canonical
    legacy_db = legacy / snap_mod._LEGACY_DB_FILENAME
    wanted = {f"m{i}" for i in range(6)}
    _store_db_kinds(legacy_db, [(f"m{i}", "manual") for i in range(6)])
    target.mkdir(parents=True, exist_ok=True)
    target_db = target / DB_FILENAME

    # 매번 2건만 처리되도록 deadline 을 짧게 끊어 여러 부팅을 시뮬레이션
    for _ in range(10):
        outcome = snap_mod._copy_forward(legacy_db, target_db, deadline=time.monotonic() + 0.0)
        if outcome.deferred == 0 and outcome.copied == 0:
            break
        snap_mod._copy_forward(legacy_db, target_db)   # 예산 있는 실행이 이어받는다
        if _snapshot_ids(target_db) == wanted:
            break

    assert _snapshot_ids(target_db) == wanted          # ★ 결국 전부 도착
    assert _snapshot_ids(legacy_db) == wanted          # legacy 온전


def test_timing_guard_reads_the_real_tauri_timeout():
    """(QQ) 순서 가드가 main.rs 를 실제로 읽는다. 리터럴 40 을 박아두면 main.rs 의
    값을 낮춰도 테스트가 초록이라 가드가 아무것도 지키지 못한다."""
    import re

    main_rs = Path(snap_mod.__file__).parents[2] / "src-tauri" / "src" / "main.rs"
    source = main_rs.read_text(encoding="utf-8")
    match = re.search(r"wait_for_serve\([^)]*Duration::from_secs\((\d+)\)", source)
    assert match, "main.rs 의 wait_for_serve 타임아웃을 못 찾았다 — 가드가 무력하다"
    tauri_timeout = int(match.group(1))

    from visualizebetter import stdio_proxy

    assert snap_mod._COPY_LOCK_TIMEOUT_S < snap_mod._COPY_DEADLINE_S
    assert snap_mod._COPY_DEADLINE_S < stdio_proxy.LAUNCH_TIMEOUT_S
    assert stdio_proxy.LAUNCH_TIMEOUT_S < tauri_timeout


def test_measured_worst_case_stays_under_the_proxy_budget(canonical):
    """(MM) 락 홀더가 붙은 상태의 **실측** 최악 시간이 소비자 예산 아래인지.
    docstring 이 주장하는 바와 코드가 하는 바를 일치시키기 위한 숫자다."""
    from visualizebetter import stdio_proxy

    target, legacy = canonical
    _store_db_kinds(
        legacy / snap_mod._LEGACY_DB_FILENAME, [(f"m{i}", "manual") for i in range(8)]
    )
    target.mkdir(parents=True, exist_ok=True)
    target_db = target / DB_FILENAME
    _store_db(target_db, [])

    holder = sqlite3.connect(target_db, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        SnapshotStore(target)
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    assert elapsed < stdio_proxy.LAUNCH_TIMEOUT_S, (
        f"실측 {elapsed:.1f}s 가 proxy 예산 {stdio_proxy.LAUNCH_TIMEOUT_S}s 를 넘는다"
    )
    print(f"\n[MM] 락 홀더 상태 실측 최악: {elapsed:.2f}s")
