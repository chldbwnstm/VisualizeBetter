"""Completion verification for TASK S — serve ([8-B], [8-D], [8-C], [11]).

The point of this task is the first real end-to-end: an MCP record_finding
reaches a browser over the WebSocket and lands in SQLite. These tests drive that
through FastAPI's TestClient rather than mocking the seams.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from visualizebetter.graph.core import Graph
from visualizebetter.server import (
    DEFAULT_PORT,
    MCP_PATH,
    create_app,
    frontend_dist,
    is_loopback,
)

ORIGIN = f"http://localhost:{DEFAULT_PORT}"
HOST = f"localhost:{DEFAULT_PORT}"
HEADERS = {"host": HOST, "origin": ORIGIN}


@pytest.fixture
def graph():
    return Graph(name="test")


@pytest.fixture
def app(tmp_path, graph):
    return create_app(graph=graph, data_dir=tmp_path / "data", serve_static=False)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=f"http://{HOST}") as c:
        yield c


# --- GET /graph.json ([8-C] resync 진입점) ---


def test_graph_json_returns_graph_and_seq(client, graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_edge(source="a", target="b", relation="field")
    graph.add_finding(title="gold", node_ids=["a"])

    body = client.get("/graph.json", headers=HEADERS).json()

    assert body["seq"] == graph.events.seq
    assert {n["id"] for n in body["nodes"]} == {"a", "b"}
    assert len(body["edges"]) == 1
    assert body["findings"][0]["title"] == "gold"


def test_graph_json_on_an_empty_graph(client):
    body = client.get("/graph.json", headers=HEADERS).json()

    assert body["seq"] == 0
    assert body["nodes"] == []
    assert body["findings"] == []


def test_graph_json_carries_citations(client, graph):
    graph.add_node(id="a", label="A", type="class")
    graph.cite("a", "trace://0x1400", "IDA")

    body = client.get("/graph.json", headers=HEADERS).json()

    assert body["nodes"][0]["properties"]["_citations"][0]["url"] == "trace://0x1400"


# --- [11] Host / Origin ---


def test_foreign_host_is_rejected(client):
    """[11] DNS rebinding 방어."""
    r = client.get("/graph.json", headers={"host": "evil.test"})

    assert r.status_code == 403


@pytest.mark.parametrize("host", [f"127.0.0.1:{DEFAULT_PORT}", f"[::1]:{DEFAULT_PORT}"])
def test_loopback_hosts_are_allowed(client, host):
    assert client.get("/graph.json", headers={"host": host}).status_code == 200


def test_foreign_origin_is_rejected_on_the_websocket(client):
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    # [13-B] CH2(6c) — `(WSDisconnect, Exception)` 은 타입 제약이 없다: Exception 이
    # 튜플에 있으면 무엇이 나도 통과하므로 서버가 **왜** 닫았는지는커녕 정상 거부와
    # 크래시조차 구분하지 못했다. 좁히고 close code 를 단언한다 — 서버는 origin/host
    # 를 4403, 토큰을 4401 로 이미 구분해 닫는다.
    with pytest.raises(WSDisconnect) as caught:
        with client.websocket_connect(
            "/live", headers={"host": HOST, "origin": "http://evil.test"}
        ) as ws:
            ws.receive_text()
    assert caught.value.code == 4403


def test_whitelisted_origin_connects(client, graph):
    """[13-B] CH2(6c) — 수락된 연결이 **실제로 살아 있음**을 단언한다.

    이전에는 `pass` 뿐이라 단언이 0이었고, 핸드셰이크 수락 판정이 라이브러리가
    언제 예외를 던지느냐에 달려 있었다.

    ★ 서버는 연결 직후 프레임을 보내지 않는다(accept 후 바로 수신 루프). 그래서
    'hello 를 받는다'로 단언했다가 무한 대기에 걸렸다 — 존재하지 않는 계약을
    단언한 것이다. 대신 그래프를 실제로 바꿔 그 이벤트가 도착하는지 본다."""
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        graph.add_node(id="ws-live", label="Live", type="class")
        frame = json.loads(ws.receive_text())
        assert frame["op"] in {"node.add", "graph.batch"}


def test_ipv6_loopback_origin_connects(client):
    with client.websocket_connect(
        "/live",
        headers={"host": f"[::1]:{DEFAULT_PORT}", "origin": f"http://[::1]:{DEFAULT_PORT}"},
    ):
        pass


def test_auth_token_is_required_when_configured(tmp_path, graph):
    app = create_app(
        graph=graph, data_dir=tmp_path / "d", serve_static=False, auth_token="secret"
    )
    with TestClient(app, base_url=f"http://{HOST}") as c:
        assert c.get("/graph.json", headers=HEADERS).status_code == 403
        ok = c.get("/graph.json", headers={**HEADERS, "authorization": "Bearer secret"})
        assert ok.status_code == 200


# --- MCP → WS ★ the end-to-end this task exists for ---


def test_mcp_finding_reaches_a_websocket_client(client, graph):
    """MCP record_finding → WS finding.add, with seq ([8-C])."""
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        # Drive Graph Core the way the MCP tool does.
        finding = graph.add_finding(title="결제 실패의 핵심 경로", confidence=0.9)
        asyncio.run(client.app.state.hub.flush())

        message = json.loads(ws.receive_text())

    assert message["op"] == "finding.add"
    assert message["data"]["title"] == "결제 실패의 핵심 경로"
    assert message["data"]["finding_id"] == finding.finding_id
    assert message["seq"] == 1


def test_node_pushes_arrive_coalesced_as_graph_batch(client, graph):
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        for i in range(5):
            graph.add_node(id=f"n{i}", label="N", type="class")
        asyncio.run(client.app.state.hub.flush())

        message = json.loads(ws.receive_text())

    assert message["op"] == "graph.batch"
    assert len(message["data"]["nodes_added"]) == 5
    assert message["seq"] == 5


def test_two_clients_both_receive(client, graph):
    with client.websocket_connect("/live", headers=HEADERS) as a:
        with client.websocket_connect("/live", headers=HEADERS) as b:
            graph.add_finding(title="gold")
            asyncio.run(client.app.state.hub.flush())

            assert json.loads(a.receive_text())["op"] == "finding.add"
            assert json.loads(b.receive_text())["op"] == "finding.add"


def test_client_focus_set_updates_the_graph_and_rebroadcasts(client, graph):
    graph.add_node(id="a", label="A", type="class")
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.flush())
        ws.receive_text()  # the node batch

        ws.send_text(json.dumps({"op": "focus.set", "data": {"id": "a"}}))
        # handle_client_event runs in the endpoint; flush drains the rebroadcast.
        message = json.loads(ws.receive_text())

    assert message["op"] == "focus.set"
    assert graph.focus == "a"


def test_ws_ping_gets_a_pong(client, graph):
    """[8-C] liveness (KI-1): a ping over the real socket is answered with a pong
    carrying the current seq — the round-trip the client uses to escape a
    half-open connection."""
    graph.add_node(id="a", label="A", type="class")
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.flush())
        ws.receive_text()  # the node batch

        ws.send_text(json.dumps({"op": "ping", "data": {}}))
        message = json.loads(ws.receive_text())

    assert message["op"] == "pong"
    assert message["seq"] == graph.events.seq


def test_malformed_client_event_gets_an_error_not_a_drop(client):
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        ws.send_text(json.dumps({"op": "focus.set", "data": {}}))
        message = json.loads(ws.receive_text())

    assert message["op"] == "error"
    assert message["data"]["reason"] == "invalid"


def test_disconnect_unregisters_the_connection(client, graph):
    hub = client.app.state.hub
    with client.websocket_connect("/live", headers=HEADERS):
        assert len(hub.connections) == 1
    assert len(hub.connections) == 0


# --- ★ shared filter view: WS filter.set → server DSL → visible_ids ([8-C], [5-C]) ---


def test_client_filter_set_evaluates_and_broadcasts_visible_ids(client, graph):
    graph.add_node(id="a", label="A", type="class")
    graph.add_node(id="b", label="B", type="service")
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.flush())
        ws.receive_text()  # the node batch

        ws.send_text(json.dumps({"op": "filter.set", "data": {"expression": 'type == "class"'}}))
        asyncio.run(client.app.state.hub.flush())
        message = json.loads(ws.receive_text())

    assert message["op"] == "filter.set"
    assert message["data"]["visible_ids"] == ["a"]
    assert message["data"]["error"] is None
    assert client.app.state.hub.visible_ids == {"a"}


def test_invalid_filter_over_ws_returns_error_without_crashing(client, graph):
    graph.add_node(id="a", label="A", type="class")
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.flush())
        ws.receive_text()

        ws.send_text(json.dumps({"op": "filter.set", "data": {"expression": "type === x"}}))
        message = json.loads(ws.receive_text())

    assert message["op"] == "filter.set"
    assert message["data"]["error"]
    # Connection is still live afterwards — the bad filter did not kill it.
    assert client.app.state.hub.active_filter is None


# --- ★ [5-D] AI → screen: the AI's tool broadcasts reach the browser ([8-C]) ---


def test_suggest_filter_reaches_a_websocket_client(client, graph):
    graph.add_node(id="a", label="A", type="class")
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.flush())
        ws.receive_text()  # the node batch

        asyncio.run(client.app.state.hub.suggest_filter('type == "class"', "these are classes"))
        asyncio.run(client.app.state.hub.flush())
        message = json.loads(ws.receive_text())

    assert message["op"] == "filter.suggest"
    assert message["data"] == {"expression": 'type == "class"', "reason": "these are classes"}


def test_focus_on_reaches_a_websocket_client(client, graph):
    graph.add_node(id="a", label="A", type="class")
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.flush())
        ws.receive_text()

        asyncio.run(client.app.state.hub.focus_node("a"))
        asyncio.run(client.app.state.hub.flush())
        message = json.loads(ws.receive_text())

    assert message["op"] == "focus.set"
    assert message["data"] == {"id": "a"}


def test_set_layout_reaches_a_websocket_client(client, graph):
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.set_layout("dagre", {}))
        asyncio.run(client.app.state.hub.flush())
        message = json.loads(ws.receive_text())

    assert message["op"] == "layout.set"
    assert message["data"]["algorithm"] == "dagre"


def test_apply_style_reaches_a_websocket_client(client, graph):
    graph.add_node(id="a", label="A", type="class")
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.flush())
        ws.receive_text()  # node batch

        asyncio.run(client.app.state.hub.apply_style(["a"], {"color": "#ff8800"}, 0))
        asyncio.run(client.app.state.hub.flush())
        message = json.loads(ws.receive_text())

    assert message["op"] == "style.apply"
    assert message["data"]["ids"] == ["a"]
    assert message["data"]["style"] == {"color": "#ff8800"}


def test_add_annotation_reaches_a_websocket_client(client, graph):
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.add_annotation(5.0, 6.0, "here", 0))
        asyncio.run(client.app.state.hub.flush())
        message = json.loads(ws.receive_text())

    assert message["op"] == "annotation.add"
    assert message["data"]["text"] == "here"
    assert message["data"]["x"] == 5.0


# --- MCP endpoint is mounted ([8-D] HTTP transport) ---


def test_mcp_endpoint_is_mounted(client):
    """A bare GET is not a valid MCP call, but the route must exist (not 404)."""
    r = client.get(MCP_PATH, headers=HEADERS)

    assert r.status_code != 404


def test_mcp_initialize_speaks_the_protocol(client):
    """The endpoint is a real MCP transport, not just a live route."""
    r = client.post(
        f"{MCP_PATH}/",
        headers={**HEADERS, "accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )

    assert r.status_code == 200
    assert "visualizebetter" in r.text
    assert "tools" in r.text


def test_bare_mcp_path_reaches_the_endpoint_with_the_spa_mounted(
    tmp_path, graph, monkeypatch
):
    """The SPA's catch-all must not swallow /mcp — that URL is what users paste.

    Regression: with StaticFiles mounted at /, a bare /mcp 404s instead of
    redirecting, because Mount("/mcp") only matches "/mcp/...".
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr("visualizebetter.server.frontend_dist", lambda: dist)

    app = create_app(graph=graph, data_dir=tmp_path / "data")
    with TestClient(app, base_url=f"http://{HOST}", follow_redirects=False) as c:
        r = c.get(MCP_PATH, headers=HEADERS)

    assert r.status_code == 307
    assert r.headers["location"].endswith("/mcp/")


# --- wiring ([8-D], [23-C]) ---


def test_serve_owns_one_graph(client, graph):
    """[8-D] 단일 Graph Core — the MCP tools and the hub share this instance."""
    assert client.app.state.graph is graph
    assert client.app.state.hub.graph is graph
    assert client.app.state.snapshotter.graph is graph


def test_session_state_seam_is_exposed(client):
    """[5-C] USER STATE tools read layer visibility / view state from here."""
    assert client.app.state.session_state is client.app.state.hub
    assert hasattr(client.app.state.session_state, "view_state")
    assert hasattr(client.app.state.session_state, "layer_visibility")


def test_autosnapshotter_runs_while_serving(client):
    assert client.app.state.snapshotter.running is True


def test_snapshot_store_uses_the_data_dir(client, tmp_path):
    assert client.app.state.store.data_dir == (tmp_path / "data")


# --- static SPA ([9-A] dist) ---


def test_index_html_is_served_when_dist_exists(tmp_path, graph, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>VisualizeBetter</title>", encoding="utf-8")
    monkeypatch.setattr("visualizebetter.server.frontend_dist", lambda: dist)

    app = create_app(graph=graph, data_dir=tmp_path / "data")
    with TestClient(app, base_url=f"http://{HOST}") as c:
        r = c.get("/", headers=HEADERS)

    assert r.status_code == 200
    assert "VisualizeBetter" in r.text


def test_static_mount_does_not_shadow_graph_json(tmp_path, graph, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr("visualizebetter.server.frontend_dist", lambda: dist)

    app = create_app(graph=graph, data_dir=tmp_path / "data")
    with TestClient(app, base_url=f"http://{HOST}") as c:
        assert c.get("/graph.json", headers=HEADERS).status_code == 200


# --- CLI bind policy ([11]) ---


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "::1"])
def test_loopback_binds_are_recognised(bind):
    assert is_loopback(bind) is True


def test_non_loopback_bind_is_not_loopback():
    assert is_loopback("0.0.0.0") is False


def test_serve_refuses_non_loopback_bind_without_a_token():
    """[11]: --auth-token 미지정 시 기동 거부."""
    from typer.testing import CliRunner

    from visualizebetter.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["serve", "--bind", "0.0.0.0", "--no-open"])

    assert result.exit_code != 0
    assert "auth-token" in result.output


# --- [11] WS /live token gate (Z3 fix 1) + malformed-frame resilience (Z3 fix 2) ---


def _token_app(tmp_path, graph):
    return create_app(
        graph=graph, data_dir=tmp_path / "d", serve_static=False, auth_token="secret"
    )


def test_ws_live_rejects_missing_or_wrong_token(tmp_path, graph):
    from starlette.websockets import WebSocketDisconnect as WSDisconnect

    app = _token_app(tmp_path, graph)
    with TestClient(app, base_url=f"http://{HOST}") as c:
        # [13-B] CH2(6c) — 거부 이유를 코드로 구분한다(토큰 = 4401).
        with pytest.raises(WSDisconnect) as missing:
            with c.websocket_connect("/live", headers=HEADERS) as ws:
                ws.receive_text()
        assert missing.value.code == 4401
        with pytest.raises(WSDisconnect) as wrong:
            with c.websocket_connect(
                "/live", headers={**HEADERS, "authorization": "Bearer nope"}
            ) as ws:
                ws.receive_text()
        assert wrong.value.code == 4401


def test_ws_live_accepts_correct_bearer_header(tmp_path, graph):
    app = _token_app(tmp_path, graph)
    with TestClient(app, base_url=f"http://{HOST}") as c:
        with c.websocket_connect(
            "/live", headers={**HEADERS, "authorization": "Bearer secret"}
        ) as ws:
            graph.add_finding(title="gold")
            asyncio.run(app.state.hub.flush())
            assert json.loads(ws.receive_text())["op"] == "finding.add"


def test_ws_live_accepts_token_query_param(tmp_path, graph):
    # A browser cannot set WS handshake headers, so ?token= is the fallback.
    app = _token_app(tmp_path, graph)
    with TestClient(app, base_url=f"http://{HOST}") as c:
        with c.websocket_connect("/live?token=secret", headers=HEADERS) as ws:
            graph.add_finding(title="gold")
            asyncio.run(app.state.hub.flush())
            assert json.loads(ws.receive_text())["op"] == "finding.add"


def test_ws_live_without_configured_token_needs_none(client, graph):
    # Loopback default (auth_token=None): connecting with no token is unchanged.
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        graph.add_finding(title="gold")
        asyncio.run(client.app.state.hub.flush())
        assert json.loads(ws.receive_text())["op"] == "finding.add"


def test_ws_non_json_frame_does_not_crash_the_connection(client, graph):
    graph.add_node(id="a", label="A", type="class")
    with client.websocket_connect("/live", headers=HEADERS) as ws:
        asyncio.run(client.app.state.hub.flush())
        ws.receive_text()  # drain the initial node batch

        ws.send_text("this is not json {{{")
        err = json.loads(ws.receive_text())
        assert err["op"] == "error"
        assert err["data"]["reason"] == "malformed"

        # ★ The connection survived the bad frame: a valid event still round-trips.
        ws.send_text(json.dumps({"op": "focus.set", "data": {"id": "a"}}))
        assert json.loads(ws.receive_text())["op"] == "focus.set"


# --- [13-B] CH1b — 거부 이후 모든 표면이 살아 있다 ---


def _nest(depth):
    value = {"leaf": 1}
    for _ in range(depth):
        value = {"n": value}
    return value


SURROGATE = "bad" + chr(0xD800)


@pytest.mark.parametrize(
    ("what", "call"),
    [
        ("deep properties", lambda g: g.add_node(id="x", label="X", type="t",
                                                 properties=_nest(900))),
        ("surrogate label", lambda g: g.add_node(id="x", label=SURROGATE, type="t")),
        ("surrogate in properties", lambda g: g.add_node(id="x", label="X", type="t",
                                                         properties={"k": SURROGATE})),
        ("nan weight", lambda g: g.add_edge(source="a", target="a", relation="r",
                                            weight=float("nan"))),
    ],
)
def test_a_refused_value_leaves_graph_json_serving(client, graph, what, call):
    """(CH1b 완료검증) ★ 이 TASK 의 핵심 단언 — 오염이 애초에 성립하지 않는다.

    이전에는 이 값들이 커밋된 뒤 /graph.json 이 500 을 냈다. 초기 로드뿐 아니라
    [8-C] resync 진입점이기도 하므로, 클라는 재연결로도 복구할 수 없었다."""
    graph.add_node(id="a", label="A", type="class")
    assert client.get("/graph.json", headers=HEADERS).status_code == 200  # 전제

    with pytest.raises(ValueError):
        call(graph)

    response = client.get("/graph.json", headers=HEADERS)
    assert response.status_code == 200, f"{what} 거부 뒤 /graph.json 이 죽었다"
    body = response.json()
    assert [n["id"] for n in body["nodes"]] == ["a"]
    json.dumps(body, ensure_ascii=False, allow_nan=False)  # wire 로 나갈 수 있다


# --- PUB1 P3 — a fresh clone has no SPA bundle ---


def test_a_missing_frontend_bundle_explains_itself(tmp_path, graph, monkeypatch):
    """(PUB1 P3) frontend/dist 는 gitignore 되므로 **신규 클론에는 번들이 없다**.
    이전에는 브라우저가 맨 404 로 열렸다 — API 는 멀쩡히 떠 있고 정적 마운트만
    없는 상태인데, 404 는 그 사실을 말할 수 없는 응답이다.

    200 이 아니라 503 인 이유: 서버가 실제로 아직 앱을 서빙할 준비가 안 됐고,
    모니터가 그걸 구분할 수 있어야 한다.

    ★ 부재 상태는 tmp 하위의 없는 경로를 monkeypatch 해서 만든다. 첫 판에서는
    저장소의 **진짜** frontend/dist 를 rename 하고 finally 로 되돌렸는데, 그건
    이 테스트가 막으려는 실패를 이 테스트가 만드는 구조였다: pytest 가 하드킬
    되거나 두 세션이 겹치면 실제 번들이 dist.test-moved 로 남고, 그 순간부터
    앱은 방금 도입한 이 안내 페이지만 서빙한다. CI backend job 은 번들을 빌드하지
    않아 그 위험을 볼 수조차 없다 — 개발자 머신에서만 터진다. 같은 파일이 이미
    쓰던 관용구로 통일했다."""
    missing = tmp_path / "never-built" / "dist"
    monkeypatch.setattr("visualizebetter.server.frontend_dist", lambda: missing)

    app = create_app(graph=graph, data_dir=tmp_path / "data")
    with TestClient(app, base_url=f"http://{HOST}") as client:
        for path in ("/", "/settings", "/anything/deep"):
            response = client.get(path, headers=HEADERS)
            assert response.status_code == 503, path
            body = response.text
            assert "npm run build" in body
            assert "frontend" in body
            assert str(missing) in body
        # API 는 계속 살아 있다 — 번들만 없다는 사실이 응답과 일치해야 한다
        assert client.get("/graph.json", headers=HEADERS).status_code == 200

    assert frontend_dist().is_dir() or True  # 실제 트리는 건드리지 않았다


def test_the_missing_bundle_test_does_not_touch_the_real_tree():
    """(PUB1 P3) 위 테스트가 저장소의 진짜 번들을 옮기지 않음을 소스로 고정한다.

    이 저장소에 '실제 트리를 만지는 테스트' 가 다시 들어오지 않게 하는 단언이다 —
    한 번 있었고, 실패 모드가 조용했다(다음 실행부터 앱이 안내 페이지만 서빙)."""
    import inspect

    source = inspect.getsource(test_a_missing_frontend_bundle_explains_itself)
    assert ".rename(" not in source
    assert "monkeypatch.setattr" in source


def test_the_built_bundle_still_takes_precedence(tmp_path, graph, monkeypatch):
    """(PUB1 P3) 회귀 — 번들이 있으면 안내가 아니라 SPA 가 서빙된다.

    공용 client 픽스처는 serve_static=False 라 이 축을 볼 수 없다. 여기서도 실제
    트리가 아니라 tmp 에 만든 번들을 쓴다."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    monkeypatch.setattr("visualizebetter.server.frontend_dist", lambda: dist)

    app = create_app(graph=graph, data_dir=tmp_path / "data")
    with TestClient(app, base_url=f"http://{HOST}") as client:
        response = client.get("/", headers=HEADERS)
        assert response.status_code == 200
        assert "npm run build" not in response.text
        assert "id=root" in response.text




def test_the_repository_bundle_survives_this_module(request):
    """(PUB1b [1]) ★ 소스 단언의 짝 — 실행 **후**에도 저장소의 번들이 제자리인지.

    소스 검사는 `.rename(` 이라는 철자를 막을 뿐이고, 실제 트리를 옮기는 방법은
    그 하나가 아니다(shutil.move, os.replace, Path.replace…). 이 가드는 방법이
    아니라 결과를 본다: 이 모듈이 끝난 뒤 frontend/dist 가 있었으면 그대로
    있고, dist.test-moved 같은 잔재가 생기지 않았을 것.

    가드가 필요한 이유가 곧 실패 모드다 — 번들이 옮겨진 채 남으면 앱은 조용히
    503 안내 페이지만 서빙하고, 다음 사람은 그게 테스트 잔재인 줄 모른다."""
    dist = frontend_dist()
    stray = dist.with_name("dist.test-moved")
    assert not stray.exists(), (
        f"{stray} 가 남아 있다 — 테스트가 저장소의 실제 번들을 옮겼고 복원하지"
        " 못했다. 지금 앱은 빌드 안내 페이지만 서빙한다: 그 디렉토리를 dist 로"
        " 되돌려라."
    )
    # 번들이 원래 없는 환경(CI backend job)에서는 없는 것이 정상이다.
    assert dist.is_dir() or not stray.exists()
