"""Completion verification for TASK 2 — citation 저장 ([5-F] cite, [23-B]).

Covers: cite twice -> _citations length 2 with {url, title, ts} entries, missing
node -> KeyError, reserved-key predicate, dirty flag, node.update event ([8-C]).
"""

import pytest

from visualizebetter.graph.core import (
    CITATIONS_PROPERTY,
    Graph,
    is_reserved_property,
)


@pytest.fixture
def graph():
    g = Graph(name="test")
    g.add_node(id="app.OrderService", label="OrderService", type="class")
    return g


@pytest.fixture
def events(graph):
    captured = []
    graph.events.subscribe(captured.append)
    return captured


# --- citations accumulate ([5-F]) ---


def test_cite_twice_accumulates_two_citations(graph):
    graph.cite("app.OrderService", "trace://0x1400", "Trace: sub_1400")
    graph.cite("app.OrderService", "https://example.test/doc", "Spec doc")

    citations = graph.get_node("app.OrderService").properties[CITATIONS_PROPERTY]
    assert len(citations) == 2


def test_each_citation_has_url_title_ts(graph):
    graph.cite("app.OrderService", "trace://0x1400", "Trace: sub_1400")

    (citation,) = graph.get_node("app.OrderService").properties[CITATIONS_PROPERTY]
    assert set(citation) == {"url", "title", "ts"}
    assert citation["url"] == "trace://0x1400"
    assert citation["title"] == "Trace: sub_1400"
    assert citation["ts"]


def test_citations_keep_insertion_order(graph):
    graph.cite("app.OrderService", "first://1", "First")
    graph.cite("app.OrderService", "second://2", "Second")
    graph.cite("app.OrderService", "third://3", "Third")

    citations = graph.get_node("app.OrderService").properties[CITATIONS_PROPERTY]
    assert [c["title"] for c in citations] == ["First", "Second", "Third"]


def test_citations_array_is_created_on_first_cite(graph):
    assert CITATIONS_PROPERTY not in graph.get_node("app.OrderService").properties

    graph.cite("app.OrderService", "trace://0x1400", "IDA")

    assert CITATIONS_PROPERTY in graph.get_node("app.OrderService").properties


def test_cite_preserves_existing_properties(graph):
    graph.add_node(id="n1", label="N1", type="class", properties={"ns": "app.ui"})

    graph.cite("n1", "trace://0x1400", "IDA")

    properties = graph.get_node("n1").properties
    assert properties["ns"] == "app.ui"
    assert len(properties[CITATIONS_PROPERTY]) == 1


def test_cite_accepts_non_http_sources(graph):
    """[5-F]: source_url 은 파일 경로/주소도 허용 — 반드시 http 일 필요 없음."""
    graph.cite("app.OrderService", "trace://0x1400", "IDA address")
    graph.cite("app.OrderService", "C:/reports/trace.txt", "Local dump")

    urls = [
        c["url"] for c in graph.get_node("app.OrderService").properties[CITATIONS_PROPERTY]
    ]
    assert urls == ["trace://0x1400", "C:/reports/trace.txt"]


def test_citations_are_per_node(graph):
    graph.add_node(id="other", label="Other", type="class")

    graph.cite("app.OrderService", "trace://0x1400", "A")
    graph.cite("other", "trace://0x2800", "B")

    assert len(graph.get_node("app.OrderService").properties[CITATIONS_PROPERTY]) == 1
    assert len(graph.get_node("other").properties[CITATIONS_PROPERTY]) == 1


def test_cite_returns_the_node(graph):
    node = graph.cite("app.OrderService", "trace://0x1400", "IDA")

    assert node is graph.get_node("app.OrderService")


def test_cite_bumps_updated_at(graph):
    node = graph.get_node("app.OrderService")
    before = node.updated_at

    graph.cite("app.OrderService", "trace://0x1400", "IDA")

    assert node.updated_at >= before


# --- missing node ([TASK 0 #3] 승인 원칙: 창작 대신 raise) ---


def test_cite_missing_node_raises_keyerror(graph):
    with pytest.raises(KeyError):
        graph.cite("does-not-exist", "trace://0x1400", "IDA")


def test_cite_missing_node_does_not_create_it(graph):
    with pytest.raises(KeyError):
        graph.cite("ghost", "trace://0x1400", "IDA")

    assert graph.get_node("ghost") is None


def test_cite_missing_node_leaves_graph_clean(graph):
    graph.clear_dirty()

    with pytest.raises(KeyError):
        graph.cite("ghost", "trace://0x1400", "IDA")

    assert graph.dirty is False


# --- reserved property keys ([23-B]) ---


def test_underscore_prefixed_keys_are_reserved():
    assert is_reserved_property("_citations") is True
    assert is_reserved_property("_internal") is True
    assert is_reserved_property("_") is True


def test_ordinary_keys_are_not_reserved():
    assert is_reserved_property("ns") is False
    assert is_reserved_property("placeholder") is False
    assert is_reserved_property("citations") is False


def test_underscore_only_counts_as_a_prefix_not_anywhere():
    assert is_reserved_property("my_key") is False
    assert is_reserved_property("trailing_") is False


def test_citations_property_is_itself_reserved():
    """The array cite() writes must be hidden from `properties.` by default."""
    assert is_reserved_property(CITATIONS_PROPERTY) is True


def test_placeholder_marker_is_not_reserved():
    """[5-A] uses a bare `placeholder` key, so it stays filter-visible."""
    from visualizebetter.graph.core import PLACEHOLDER_PROPERTY

    assert is_reserved_property(PLACEHOLDER_PROPERTY) is False


# --- dirty flag ([23-C]) ---


def test_cite_sets_dirty_flag(graph):
    graph.clear_dirty()

    graph.cite("app.OrderService", "trace://0x1400", "IDA")

    assert graph.dirty is True


# --- node.update event ([8-C]) ---


def test_cite_publishes_node_update_with_full_citations_array(graph, events):
    graph.cite("app.OrderService", "trace://0x1400", "IDA")

    assert events[-1].op == "node.update"
    assert events[-1].data["id"] == "app.OrderService"
    citations = events[-1].data["patch"]["set"]["properties"][CITATIONS_PROPERTY]
    assert len(citations) == 1
    assert citations[0]["url"] == "trace://0x1400"


def test_second_cite_event_carries_both_citations(graph, events):
    """patch properties merge is key-level, so the array must be sent whole."""
    graph.cite("app.OrderService", "first://1", "First")
    graph.cite("app.OrderService", "second://2", "Second")

    citations = events[-1].data["patch"]["set"]["properties"][CITATIONS_PROPERTY]
    assert [c["title"] for c in citations] == ["First", "Second"]


def test_published_payload_is_not_mutated_by_later_cites(graph, events):
    graph.cite("app.OrderService", "first://1", "First")
    first_payload = events[-1].data["patch"]["set"]["properties"][CITATIONS_PROPERTY]

    graph.cite("app.OrderService", "second://2", "Second")

    assert len(first_payload) == 1, "an earlier event must not grow retroactively"


def test_cite_event_shares_the_graph_seq_sequence(graph, events):
    graph.cite("app.OrderService", "trace://0x1400", "IDA")
    graph.cite("app.OrderService", "trace://0x2800", "IDA2")

    assert [e.seq for e in events] == [2, 3], "node.add took seq 1"
