"""Completion verification for TASK 6 — WebSocket 이벤트 층 ([8-C], [11]).

Covers: EventBus push → fake connection receives the wire message (seq included),
finding.* forwarding, client focus.set → graph.focus + re-broadcast, Origin
whitelist, protocol Pydantic round-trip, graph.batch coalescing + flush.
"""

import asyncio
import json

import pytest
from pydantic import ValidationError

from visualizebetter.graph.core import Graph
from visualizebetter.ws.hub import (
    DEFAULT_RATE_LIMIT,
    RateLimitExceeded,
    RateLimiter,
    WSHub,
    validate_origin,
)
from visualizebetter.ws.protocol import (
    decode_client_event,
    decode_server_event,
    encode_event,
)


class FakeConnection:
    """Test double for a live client — the hub only needs async send(text)."""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    @property
    def messages(self) -> list[dict]:
        return [json.loads(text) for text in self.sent]

    @property
    def ops(self) -> list[str]:
        return [m["op"] for m in self.messages]


@pytest.fixture
def graph():
    return Graph(name="test")


@pytest.fixture
def hub(graph):
    h = WSHub(graph)
    h.subscribe()
    return h


@pytest.fixture
def conn(hub):
    c = FakeConnection()
    hub.register(c)
    return c


def run(coro):
    return asyncio.run(coro)


# --- registry ---


def test_register_and_unregister(hub):
    conn = FakeConnection()

    hub.register(conn)
    assert conn in hub.connections

    hub.unregister(conn)
    assert conn not in hub.connections


def test_broadcast_reaches_every_connection(hub, graph):
    a, b = FakeConnection(), FakeConnection()
    hub.register(a)
    hub.register(b)

    graph.add_node(id="n1", label="N1", type="class")
    run(hub.flush())

    assert a.ops == ["graph.batch"]
    assert b.ops == ["graph.batch"]


class DeadConnection:
    """A client that went away — send raises, as a closed socket does."""

    async def send(self, text: str) -> None:
        raise ConnectionResetError("client is gone")


def test_a_dead_connection_does_not_cost_other_clients_their_events(hub, graph):
    """[8-C] 이벤트 무손실 ([15]): one broken client must not break the broadcast.

    flush() empties the outbox before sending, so an exception escaping mid-drain
    loses every remaining message for *everyone*.
    """
    alive = FakeConnection()
    hub.register(DeadConnection())
    hub.register(alive)

    graph.add_node(id="a", label="A", type="class")
    graph.add_finding(title="gold")
    graph.add_node(id="b", label="B", type="class")
    run(hub.flush())

    assert alive.ops == ["graph.batch", "finding.add", "graph.batch"]


def test_a_dead_connection_is_dropped(hub, graph):
    dead = DeadConnection()
    hub.register(dead)

    graph.add_node(id="a", label="A", type="class")
    run(hub.flush())

    assert dead not in hub.connections


def test_the_hub_keeps_working_after_a_connection_dies(hub, graph):
    """The failure must not poison later flushes — the outbox is not corrupted."""
    hub.register(DeadConnection())
    graph.add_node(id="a", label="A", type="class")
    run(hub.flush())

    alive = FakeConnection()
    hub.register(alive)
    graph.add_finding(title="later gold")
    run(hub.flush())

    assert alive.ops == ["finding.add"]


def test_unregistered_connection_stops_receiving(hub, graph):
    conn = FakeConnection()
    hub.register(conn)
    hub.unregister(conn)

    graph.add_node(id="n1", label="N1", type="class")
    run(hub.flush())

    assert conn.sent == []


# --- graph mutations coalesce into graph.batch ([8-C], Q1=A) ---


def test_node_add_arrives_as_graph_batch_not_individual(hub, conn, graph):
    graph.add_node(id="n1", label="N1", type="class")

    run(hub.flush())

    assert conn.ops == ["graph.batch"]
    (message,) = conn.messages
    assert message["data"]["nodes_added"][0]["id"] == "n1"
    assert message["seq"] == 1


def test_many_mutations_coalesce_into_one_message(hub, conn, graph):
    for i in range(10):
        graph.add_node(id=f"n{i}", label="N", type="class")

    sent = run(hub.flush())

    assert sent == 1, "10 pushes → 1 wire message ([8-C] 성능절)"
    assert conn.ops == ["graph.batch"]
    assert len(conn.messages[0]["data"]["nodes_added"]) == 10


def test_batch_seq_is_the_highest_coalesced_seq(hub, conn, graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_node(id="c", label="C", type="class")

    run(hub.flush())

    assert conn.messages[0]["seq"] == 3


def test_batch_buckets_every_graph_op(hub, conn, graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="class")
    graph.add_edge(source="a", target="b", relation="field")
    graph.update_node("a", {"set": {"label": "A2"}})
    graph.update_edge("a", "b", "field", "", {"set": {"weight": 0.5}})
    graph.delete_edge("a", "b", "field")
    graph.delete_node("a")

    run(hub.flush())

    data = conn.messages[0]["data"]
    assert [n["id"] for n in data["nodes_added"]] == ["a", "b"]
    assert [n["id"] for n in data["nodes_updated"]] == ["a"]
    assert [n["id"] for n in data["nodes_deleted"]] == ["a"]
    assert len(data["edges_added"]) == 1
    assert len(data["edges_updated"]) == 1
    assert data["edges_deleted"][0]["relation"] == "field"


def test_nothing_is_sent_before_flush(hub, conn, graph):
    graph.add_node(id="n1", label="N1", type="class")

    assert conn.sent == []
    assert hub.pending == 1


def test_flush_on_an_empty_buffer_sends_nothing(hub, conn):
    assert run(hub.flush()) == 0
    assert conn.sent == []


def test_flush_drains_the_buffer(hub, conn, graph):
    graph.add_node(id="n1", label="N1", type="class")
    run(hub.flush())
    run(hub.flush())

    assert conn.ops == ["graph.batch"], "a second flush re-sends nothing"


# --- finding.* is not coalesced ([8-C] Q1-b) ---


def test_finding_add_is_its_own_message(hub, conn, graph):
    graph.add_finding(title="gold")

    run(hub.flush())

    assert conn.ops == ["finding.add"]
    assert conn.messages[0]["data"]["title"] == "gold"
    assert conn.messages[0]["seq"] == 1


def test_finding_update_and_delete_forward(hub, conn, graph):
    finding = graph.add_finding(title="gold")
    graph.update_finding(finding.finding_id, {"set": {"confidence": 0.1}})
    graph.delete_finding(finding.finding_id)

    run(hub.flush())

    assert conn.ops == ["finding.add", "finding.update", "finding.delete"]


def test_graph_batch_carries_no_findings_field(hub, conn, graph):
    graph.add_node(id="n1", label="N1", type="class")

    run(hub.flush())

    assert set(conn.messages[0]["data"]) == {
        "nodes_added",
        "nodes_updated",
        "nodes_deleted",
        "edges_added",
        "edges_updated",
        "edges_deleted",
    }


def test_a_finding_never_overtakes_the_nodes_it_anchors(hub, conn, graph):
    """Wire seq must stay monotonic, so a finding closes the open batch."""
    graph.add_node(id="a", label="A", type="class")
    graph.add_finding(title="gold", node_ids=["a"])
    graph.add_node(id="b", label="B", type="class")

    run(hub.flush())

    assert conn.ops == ["graph.batch", "finding.add", "graph.batch"]
    seqs = [m["seq"] for m in conn.messages]
    assert seqs == sorted(seqs), "seq is monotonically increasing on the wire"
    assert seqs == [1, 2, 3]


def test_anchor_cleanup_forwards_as_finding_update(hub, conn, graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_finding(title="gold", node_ids=["a"])
    run(hub.flush())

    graph.delete_node("a")
    run(hub.flush())

    assert conn.ops[-2:] == ["graph.batch", "finding.update"]


# --- Client → Server ([8-C]) ---


def test_client_focus_set_updates_graph_and_rebroadcasts(hub, conn, graph):
    graph.add_node(id="a", label="A", type="class")
    run(hub.flush())

    run(hub.handle_client_event(conn, {"op": "focus.set", "data": {"id": "a"}}))
    run(hub.flush())

    assert graph.focus == "a"
    assert conn.ops[-1] == "focus.set"
    assert conn.messages[-1]["data"] == {"id": "a"}


def test_client_event_rebroadcast_reaches_other_clients(hub, conn, graph):
    """[8-C]: 다시 브로드캐스트 (다른 접속자 동기화)."""
    other = FakeConnection()
    hub.register(other)

    run(hub.handle_client_event(conn, {"op": "focus.set", "data": {"id": "a"}}))
    run(hub.flush())

    assert other.ops == ["focus.set"]


def test_client_filter_set_stores_expression_and_rebroadcasts(hub, conn, graph):
    # The [6] evaluator now backs filter.set (TASK V), so visible_ids is the real
    # matched set. Detailed coverage lives in test_filter_view.py.
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="service")
    run(
        hub.handle_client_event(
            conn, {"op": "filter.set", "data": {"expression": 'type == "class"'}}
        )
    )
    run(hub.flush())

    assert graph.active_filter == 'type == "class"'
    assert conn.messages[-1]["data"] == {
        "expression": 'type == "class"',
        "visible_ids": ["a"],
        "error": None,
    }


def test_client_layer_toggle_derives_visible(hub, conn):
    """Client sends {layer}; server derives {layer, visible} ([8-C])."""
    run(hub.handle_client_event(conn, {"op": "layer.toggle", "data": {"layer": "l1"}}))
    run(hub.flush())
    assert conn.messages[-1]["data"] == {"layer": "l1", "visible": False}

    run(hub.handle_client_event(conn, {"op": "layer.toggle", "data": {"layer": "l1"}}))
    run(hub.flush())
    assert conn.messages[-1]["data"] == {"layer": "l1", "visible": True}


def test_layer_visibility_lives_on_the_hub_not_the_graph(hub, conn, graph):
    run(hub.handle_client_event(conn, {"op": "layer.toggle", "data": {"layer": "l1"}}))

    assert hub.layer_visibility == {"l1": False}
    assert not hasattr(graph, "layer_visibility")


def test_client_layout_set_stores_choice(hub, conn):
    run(
        hub.handle_client_event(
            conn, {"op": "layout.set", "data": {"algorithm": "dagre"}}
        )
    )
    run(hub.flush())

    assert hub.active_layout == {"algorithm": "dagre", "options": {}}
    assert conn.messages[-1]["op"] == "layout.set"


def test_client_view_update_is_stored_not_rebroadcast(hub, conn):
    """[8-C] has no Server → Client view.update — the viewport is per client."""
    run(
        hub.handle_client_event(
            conn,
            {
                "op": "view.update",
                "data": {"mode": "overview", "zoom": 1.5, "camera_pos": {"x": 1, "y": 2}},
            },
        )
    )
    run(hub.flush())

    assert hub.view_state["mode"] == "overview"
    assert hub.view_state["zoom"] == 1.5
    assert conn.sent == [], "nothing is broadcast"


def test_client_events_share_the_graph_seq_sequence(hub, conn, graph):
    graph.add_node(id="a", label="A", type="class")
    run(hub.handle_client_event(conn, {"op": "focus.set", "data": {"id": "a"}}))
    run(hub.flush())

    assert [m["seq"] for m in conn.messages] == [1, 2]


def test_client_event_does_not_dirty_the_graph(hub, conn, graph):
    graph.clear_dirty()

    run(hub.handle_client_event(conn, {"op": "focus.set", "data": {"id": "a"}}))

    assert graph.dirty is False, "focus is not graph knowledge to persist"


# --- Client → Server validation ([8-C]: 비신뢰 입력) ---


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"op": "focus.set", "data": {}}, id="missing-id"),
        pytest.param({"op": "focus.set"}, id="missing-data"),
        pytest.param({"op": "nonsense", "data": {}}, id="unknown-op"),
        pytest.param({"op": "view.update", "data": {"mode": "bogus", "zoom": 1, "camera_pos": {"x": 0, "y": 0}}}, id="bad-enum"),
        pytest.param({"op": "filter.set", "data": {"expression": 5}}, id="wrong-type"),
        pytest.param({"data": {"id": "a"}}, id="no-op"),
    ],
)
def test_malformed_client_event_is_rejected(hub, conn, message):
    with pytest.raises(ValidationError):
        run(hub.handle_client_event(conn, message))


def test_rejected_client_event_changes_nothing(hub, conn, graph):
    with pytest.raises(ValidationError):
        run(hub.handle_client_event(conn, {"op": "focus.set", "data": {}}))

    assert graph.focus is None
    assert conn.sent == []


def test_server_ops_are_not_accepted_from_clients(hub, conn):
    """node.add is Server → Client only; a browser must not inject one."""
    with pytest.raises(ValidationError):
        run(
            hub.handle_client_event(
                conn, {"op": "node.add", "data": {"id": "x", "label": "X", "type": "t"}}
            )
        )


# --- [8-C] heartbeat / liveness (KI-1) ---


def test_client_ping_replies_pong(hub, conn):
    """[8-C] liveness: a ping is answered immediately with a pong ([8-C], KI-1)."""
    run(hub.handle_client_event(conn, {"op": "ping", "data": {}}))

    assert conn.ops == ["pong"]
    assert conn.messages[-1]["data"] == {}


def test_ping_accepts_an_omitted_data(hub, conn):
    """The client may send a bare {op: ping}; data defaults to empty."""
    run(hub.handle_client_event(conn, {"op": "ping"}))

    assert conn.ops == ["pong"]


def test_pong_carries_current_seq_and_consumes_none(hub, conn, graph):
    """A pong is a control reply: it reports the current seq but never advances it,
    so it cannot desync the client's monotonic seq ([8-C])."""
    graph.add_node(id="a", label="A", type="class")
    run(hub.flush())
    seq_before = graph.events.seq

    run(hub.handle_client_event(conn, {"op": "ping", "data": {}}))

    assert conn.messages[-1]["op"] == "pong"
    assert conn.messages[-1]["seq"] == seq_before

    # The next real mutation is seq_before + 1 — the ping did not burn a seq.
    graph.add_node(id="b", label="B", type="class")
    run(hub.flush())
    assert graph.events.seq == seq_before + 1


def test_pong_is_a_direct_reply_not_a_broadcast(hub, conn, graph):
    """The pong goes only to the pinging connection; other clients see nothing."""
    other = FakeConnection()
    hub.register(other)

    run(hub.handle_client_event(conn, {"op": "ping", "data": {}}))
    run(hub.flush())

    assert conn.ops == ["pong"]
    assert other.sent == []


def test_ping_does_not_dirty_the_graph(hub, conn, graph):
    """Liveness is not graph knowledge; a ping must not mark a snapshot needed."""
    graph.clear_dirty()

    run(hub.handle_client_event(conn, {"op": "ping", "data": {}}))

    assert graph.dirty is False


def test_client_ping_round_trip():
    decoded = decode_client_event('{"op": "ping", "data": {}}')

    assert decoded.op == "ping"


def test_pong_server_event_round_trip():
    decoded = decode_server_event({"op": "pong", "data": {}, "seq": 7})

    assert decoded.op == "pong"
    assert decoded.seq == 7


# --- rate limit ([8-C] 연결당 rate limit) ---


def test_default_rate_limit_is_20_per_second():
    assert DEFAULT_RATE_LIMIT == 20


def test_rate_limit_rejects_over_budget(graph):
    clock = [0.0]
    hub = WSHub(graph, rate_limiter=RateLimiter(max_events=3, clock=lambda: clock[0]))
    conn = FakeConnection()
    hub.register(conn)

    for _ in range(3):
        run(hub.handle_client_event(conn, {"op": "focus.set", "data": {"id": "a"}}))

    with pytest.raises(RateLimitExceeded):
        run(hub.handle_client_event(conn, {"op": "focus.set", "data": {"id": "a"}}))


def test_rate_limit_window_slides(graph):
    clock = [0.0]
    hub = WSHub(graph, rate_limiter=RateLimiter(max_events=2, clock=lambda: clock[0]))
    conn = FakeConnection()
    hub.register(conn)

    for _ in range(2):
        run(hub.handle_client_event(conn, {"op": "focus.set", "data": {"id": "a"}}))

    clock[0] = 1.5
    run(hub.handle_client_event(conn, {"op": "focus.set", "data": {"id": "a"}}))


def test_rate_limit_is_per_connection(graph):
    clock = [0.0]
    hub = WSHub(graph, rate_limiter=RateLimiter(max_events=1, clock=lambda: clock[0]))
    a, b = FakeConnection(), FakeConnection()
    hub.register(a)
    hub.register(b)

    run(hub.handle_client_event(a, {"op": "focus.set", "data": {"id": "x"}}))
    run(hub.handle_client_event(b, {"op": "focus.set", "data": {"id": "y"}}))

    with pytest.raises(RateLimitExceeded):
        run(hub.handle_client_event(a, {"op": "focus.set", "data": {"id": "z"}}))


# --- Origin whitelist ([11]) ---


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:8765", "http://127.0.0.1:8765"],
)
def test_whitelisted_origins_are_allowed(origin):
    assert validate_origin(origin, 8765) is True


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.test",
        "https://evil.test",
        "http://localhost:9999",
        "http://127.0.0.1:9999",
        "http://localhost.evil.test:8765",
        "http://192.168.0.5:8765",
        "null",
        "file://",
    ],
)
def test_foreign_origins_are_rejected(origin):
    assert validate_origin(origin, 8765) is False


def test_missing_origin_is_allowed_for_non_browser_clients():
    """[11]: Origin 없는 연결은 127.0.0.1 바인딩에서 허용 (토큰 요구는 serve)."""
    assert validate_origin(None, 8765) is True


# --- protocol round-trip ---


def test_encode_event_produces_the_wire_shape(graph):
    captured = []
    graph.events.subscribe(captured.append)
    graph.add_node(id="n1", label="N1", type="class")

    wire = json.loads(encode_event(captured[0]))

    assert wire["op"] == "node.add"
    assert wire["seq"] == 1
    assert wire["data"]["id"] == "n1"


def test_server_event_round_trip(graph):
    captured = []
    graph.events.subscribe(captured.append)
    graph.add_node(id="n1", label="N1", type="class")

    decoded = decode_server_event(encode_event(captured[0]))

    assert decoded.op == "node.add"
    assert decoded.seq == 1
    assert decoded.data["id"] == "n1"


def test_graph_batch_round_trip(hub, conn, graph):
    graph.add_node(id="a", label="A", type="class")
    graph.update_node("a", {"set": {"label": "A2"}})
    run(hub.flush())

    decoded = decode_server_event(conn.sent[0])

    assert decoded.op == "graph.batch"
    assert decoded.data.nodes_added[0]["id"] == "a"
    assert decoded.data.nodes_updated[0].id == "a"
    assert decoded.data.nodes_updated[0].patch == {"set": {"label": "A2"}}


def test_finding_event_round_trip(hub, conn, graph):
    graph.add_finding(title="gold", node_ids=["a"])
    run(hub.flush())

    decoded = decode_server_event(conn.sent[0])

    assert decoded.op == "finding.add"
    assert decoded.data["title"] == "gold"


def test_edge_delete_round_trip(hub, conn, graph):
    graph.add_edge(source="a", target="b", relation="field", key="k")
    run(hub.flush())
    graph.delete_edge("a", "b", "field", "k")
    run(hub.flush())

    decoded = decode_server_event(conn.sent[-1])

    assert decoded.data.edges_deleted[0].key == "k"


def test_client_event_round_trip():
    decoded = decode_client_event('{"op": "focus.set", "data": {"id": "a"}}')

    assert decoded.op == "focus.set"
    assert decoded.data.id == "a"


def test_client_view_update_round_trip():
    decoded = decode_client_event(
        {
            "op": "view.update",
            "data": {"mode": "split", "zoom": 2.0, "camera_pos": {"x": 1.0, "y": -1.0}},
        }
    )

    assert decoded.data.mode == "split"
    assert decoded.data.camera_pos.x == 1.0


@pytest.mark.parametrize(
    "op",
    [
        "node.add",
        "node.update",
        "node.delete",
        "edge.add",
        "edge.update",
        "edge.delete",
        "graph.batch",
        "finding.add",
        "finding.update",
        "finding.delete",
        "filter.set",
        "filter.suggest",
        "focus.set",
        "layer.toggle",
        "layout.set",
        "style.apply",
        "style.clear",
        "annotation.add",
        "snapshot.load",
        "clear",
        "pong",
    ],
)
def test_every_8c_server_op_has_a_schema(op):
    """[8-C] Server → Client 목록 전부."""
    samples = {
        "node.add": {"id": "n", "label": "N", "type": "t"},
        "node.update": {"id": "n", "patch": {}},
        "node.delete": {"id": "n"},
        "edge.add": {"source": "a", "target": "b", "relation": "r"},
        "edge.update": {"source": "a", "target": "b", "relation": "r", "key": "", "patch": {}},
        "edge.delete": {"source": "a", "target": "b", "relation": "r", "key": ""},
        "graph.batch": {},
        "finding.add": {"finding_id": "f", "title": "t"},
        "finding.update": {"finding_id": "f", "patch": {}},
        "finding.delete": {"finding_id": "f"},
        "filter.set": {"expression": "x", "visible_ids": []},
        "filter.suggest": {"expression": "x", "reason": "why"},
        "focus.set": {"id": "n"},
        "layer.toggle": {"layer": "l", "visible": True},
        "layout.set": {"algorithm": "dagre", "options": {}},
        "style.apply": {"style_id": "s", "ids": ["a"], "style": {}, "ttl": 0},
        "style.clear": {"style_id": "s"},
        "annotation.add": {"annotation_id": "n", "x": 1.0, "y": 2.0, "text": "hi", "ttl": 0},
        "snapshot.load": {"snapshot_id": "s"},
        "clear": {"layer": None},
        "pong": {},
    }

    decoded = decode_server_event({"op": op, "data": samples[op], "seq": 1})

    assert decoded.op == op
    assert decoded.seq == 1


# --- [8-C] filter.set optional error field (Z3 fix 4 — schema matches the wire) ---


def test_filter_set_error_field_round_trips():
    decoded = decode_server_event(
        {
            "op": "filter.set",
            "data": {"expression": "bad(", "visible_ids": [], "error": "syntax error"},
            "seq": 3,
        }
    )
    assert decoded.op == "filter.set"
    assert decoded.data.error == "syntax error"


def test_filter_set_error_defaults_to_none_when_absent():
    decoded = decode_server_event(
        {
            "op": "filter.set",
            "data": {"expression": 'type == "class"', "visible_ids": ["a"]},
            "seq": 4,
        }
    )
    assert decoded.data.error is None
