"""WebSocket broadcast hub ([8-C], [11]).

Batching ([8-C] 성능절, Q1=A): node.*/edge.* never go on the wire individually —
they accumulate and leave as one ``graph.batch`` per flush. Their single-op
schemas survive as the batch's array element types. finding.* and the non-graph
ops (filter/focus/style/layer/annotation) are low-rate and ship as their own
messages.

Ordering: a non-graph event closes the open batch before queueing itself. Wire
seq therefore stays monotonically increasing ([8-C]), and a finding.add can never
overtake the node.add it anchors to.

serve wiring is deferred: ``flush()`` is the seam, and serve drives it on the
16~50ms window ([8-C]). Mounting the FastAPI endpoint is likewise serve's.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import ValidationError

from visualizebetter.filter import FilterError, compile_filter
from visualizebetter.graph.core import Graph
from visualizebetter.graph.events import (
    EDGE_ADD,
    EDGE_DELETE,
    EDGE_UPDATE,
    Event,
    NODE_ADD,
    NODE_DELETE,
    NODE_UPDATE,
)
from visualizebetter.ws.protocol import (
    CLIENT_EVENT_ADAPTER,
    EdgeIdentityData,
    EdgeUpdateData,
    GraphBatchData,
    NodeDeleteData,
    NodeUpdateData,
    encode_message,
)

logger = logging.getLogger("visualizebetter.ws")

GRAPH_BATCH_OP = "graph.batch"

# [5-C] user-state ring bounds. Bounded so a long session cannot grow unbounded;
# an AI that polls between tasks ([5-C] polling pattern) never needs deep history.
SELECTION_HISTORY_MAX = 100
USER_EVENT_RING_MAX = 1000
MAX_POLL_LIMIT = 100
USER_EVENT_TYPES = ("focus_change", "filter_change")

_BATCH_FIELDS: dict[str, tuple[str, Any]] = {
    # op -> (graph.batch field, element model). Node/Edge payloads stay dicts:
    # they are [4-A]/[4-B] objects already serialized by Graph Core ([8-C] Q2 —
    # each array reuses the matching single-op data shape).
    NODE_ADD: ("nodes_added", None),
    NODE_UPDATE: ("nodes_updated", NodeUpdateData),
    NODE_DELETE: ("nodes_deleted", NodeDeleteData),
    EDGE_ADD: ("edges_added", None),
    EDGE_UPDATE: ("edges_updated", EdgeUpdateData),
    EDGE_DELETE: ("edges_deleted", EdgeIdentityData),
}

_COALESCED_OPS = frozenset(_BATCH_FIELDS)
"""[8-C] graph.batch carries node/edge only — findings are not coalesced."""

DEFAULT_RATE_LIMIT = 20
"""[8-C] 연결당 rate limit (예: 초당 20 이벤트)."""


class Connection(Protocol):
    """A live client. serve adapts a FastAPI WebSocket to this."""

    async def send(self, text: str) -> None: ...


def allowed_origins(port: int) -> set[str]:
    """[11] WS 핸드셰이크 Origin 화이트리스트.

    [::1] rides along with localhost/127.0.0.1: [11] lists it among the allowed
    Hosts, and a browser on the IPv6 loopback sends it as its Origin too.
    """
    return {
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
        f"http://[::1]:{port}",
    }


def validate_origin(origin: str | None, port: int) -> bool:
    """[11] Origin check for the WS handshake.

    CORS does not apply to WebSockets, so without this any page the user visits
    could open ws://localhost:PORT/live and read the whole graph ([11]).

    A missing Origin is a non-browser client (script/CLI): [11] allows it on a
    127.0.0.1 binding and requires a token otherwise. Enforcing that split needs
    the bind address and token, which are serve's — this helper answers only the
    Origin question.
    """
    if origin is None:
        return True
    return origin in allowed_origins(port)


class RateLimiter:
    """[8-C] 연결당 rate limit. clock is injectable so tests stay deterministic."""

    def __init__(
        self,
        max_events: int = DEFAULT_RATE_LIMIT,
        window_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.clock = clock
        self._hits: dict[int, list[float]] = {}

    def allow(self, conn: Connection) -> bool:
        now = self.clock()
        hits = self._hits.setdefault(id(conn), [])
        cutoff = now - self.window_seconds
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= self.max_events:
            return False
        hits.append(now)
        return True

    def forget(self, conn: Connection) -> None:
        self._hits.pop(id(conn), None)


class RateLimitExceeded(Exception):
    """Raised when a connection exceeds its [8-C] budget."""


class WSHub:
    """Fans Graph Core events out to connected clients ([8-C])."""

    def __init__(
        self,
        graph: Graph,
        rate_limiter: RateLimiter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.graph = graph
        self.rate_limiter = rate_limiter or RateLimiter()
        # Wall clock, injectable so [5-C] time assertions (since_ms, event ts) are
        # deterministic in tests. Real time rather than monotonic because these
        # are user-facing timestamps, not durations to be diffed across restarts.
        self.clock = clock
        self._connections: set[Connection] = set()
        self._unsubscribe: Callable[[], None] | None = None
        self._outbox: list[tuple[int, str, Any]] = []
        self._batch: GraphBatchData | None = None
        self._batch_seq = 0

        # Session state, deliberately not on the Graph: layer visibility and
        # viewport are not graph knowledge and are not snapshotted ([4-C] has no
        # visibility field; [9-C] keeps LayerInfo client-side). get_view_state
        # ([5-C]) reads these through serve.
        self.layer_visibility: dict[str, bool] = {}
        self.view_state: dict[str, Any] | None = None
        self.active_layout: dict[str, Any] | None = None

        # [5-C] shared filter view: the expression the human last applied and the
        # set it evaluated to. None = no filter (everything visible). The set is
        # what the human's screen dims to, so get_visible_nodes returns exactly
        # what the human sees ([5-C] get_active_filter / get_visible_nodes).
        self.active_filter: str | None = None
        self.visible_ids: set[str] | None = None

        # [5-C] user state — what the human is looking at, for the AI to poll.
        # focus + a bounded click history, and a monotonic-cursor ring of
        # focus/filter changes.
        self.focused_id: str | None = None
        self.focused_since: float | None = None
        self.selection_history: deque[dict[str, Any]] = deque(maxlen=SELECTION_HISTORY_MAX)
        self._user_events: deque[dict[str, Any]] = deque(maxlen=USER_EVENT_RING_MAX)
        # ★ Separate from the [8-C] WS seq. That seq orders Server→Client graph
        # events for resync; this cursor orders user-state events for polling. They
        # count different things and must not be conflated.
        self._event_cursor = 0

        # [5-D] deterministic ids for AI overlays. Counters rather than random so
        # tests can assert the id and there is no Math/random dependency.
        self._style_seq = 0
        self._annotation_seq = 0

    def _record_user_event(self, event_type: str, data: dict[str, Any]) -> None:
        """[5-C] append a focus/filter change to the poll ring, monotonic cursor."""
        self._event_cursor += 1
        self._user_events.append(
            {
                "cursor": self._event_cursor,
                "type": event_type,
                "data": data,
                "ts": self.clock(),
            }
        )

    # --- connection registry ---

    @property
    def connections(self) -> frozenset[Connection]:
        return frozenset(self._connections)

    def register(self, conn: Connection) -> None:
        self._connections.add(conn)

    def unregister(self, conn: Connection) -> None:
        self._connections.discard(conn)
        self.rate_limiter.forget(conn)

    # --- Graph Core subscription ---

    def subscribe(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self.graph.events.subscribe(self._on_event)

    def unsubscribe(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _on_event(self, event: Event) -> None:
        """EventBus handler — synchronous, so it queues rather than sends.

        [13-B] CH1(2): wire encoding can fail here (model_validate on the
        batch models), and the two possible failures were both wrong. Before,
        the exception travelled back through publish into the mutation that
        raised it, so a committed graph change was reported to the caller as an
        error. With publish isolating its subscribers that stops — but then
        the event is merely *dropped*, and M1 clients have no way to notice a
        missing seq, so they render a graph the server no longer has.

        A message we cannot encode is exactly the situation resync exists for:
        drop the half-built batch and tell every client that what it holds is
        stale, the same signal a snapshot load sends ([8-C]). Costly and rare,
        but it converges — silence does not.
        """
        try:
            if event.op in _COALESCED_OPS:
                self._add_to_batch(event)
            else:
                self._close_batch()
                self._outbox.append((event.seq, event.op, event.data))
        except Exception:  # noqa: BLE001
            logger.exception("cannot encode %s (seq=%s); forcing a client resync", event.op, event.seq)
            self._batch = None
            self._outbox.append((event.seq, "snapshot.load", {"snapshot_id": ""}))

    def _add_to_batch(self, event: Event) -> None:
        if self._batch is None:
            self._batch = GraphBatchData()
        field, model = _BATCH_FIELDS[event.op]
        element = model.model_validate(event.data) if model is not None else event.data
        getattr(self._batch, field).append(element)
        self._batch_seq = event.seq  # [8-C] batch seq = 합쳐진 것 중 최댓값

    def _close_batch(self) -> None:
        if self._batch is not None and not self._batch.is_empty():
            self._outbox.append((self._batch_seq, GRAPH_BATCH_OP, self._batch))
        self._batch = None

    @property
    def pending(self) -> int:
        """Messages waiting for the next flush (an open batch counts as one)."""
        open_batch = 1 if self._batch is not None and not self._batch.is_empty() else 0
        return len(self._outbox) + open_batch

    async def flush(self) -> int:
        """Send everything queued. serve calls this on the [8-C] 16~50ms window."""
        self._close_batch()
        messages, self._outbox = self._outbox, []
        for seq, op, data in messages:
            await self._send_all(encode_message(op, data, seq))
        return len(messages)

    async def _send_all(self, text: str) -> None:
        """Fan out to every live connection, isolating failures.

        A send to a client that has gone away raises. Letting that escape would
        abort flush() mid-drain — and flush() has already emptied the outbox, so
        every message after the bad one is gone for *all* clients, not just the
        dead one. That is exactly the loss [8-C] promises never happens ("이벤트
        무손실", [15]). So each connection is sent to independently, and one that
        fails is dropped rather than allowed to take the broadcast down.
        """
        for conn in list(self._connections):
            try:
                await conn.send(text)
            except Exception:
                logger.debug("dropping a connection that failed to receive", exc_info=True)
                self.unregister(conn)

    async def broadcast(self, op: str, data: Any) -> Event:
        """Publish a server-originated op through Graph Core's seq sequence.

        The EventBus counter is the only monotonic seq source ([8-C] requires one
        sequence across every Server → Client event), so hub-originated ops go
        through it rather than a second counter. It does not touch the dirty flag.
        """
        return self.graph.events.publish(op, data)

    # --- Client → Server ([8-C]) ---

    async def handle_client_event(
        self, conn: Connection, message: str | dict[str, Any]
    ) -> Any:
        """Validate untrusted client input, apply it, re-broadcast ([8-C]).

        Raises ValidationError on a malformed message and RateLimitExceeded when
        the connection is over budget.
        """
        if not self.rate_limiter.allow(conn):
            raise RateLimitExceeded("rate limit exceeded")

        event = CLIENT_EVENT_ADAPTER.validate_python(
            message if isinstance(message, dict) else json.loads(message)
        )
        handler = {
            "focus.set": self._client_focus_set,
            "filter.set": self._client_filter_set,
            "layer.toggle": self._client_layer_toggle,
            "layout.set": self._client_layout_set,
            "view.update": self._client_view_update,
            "ping": self._client_ping,
            "undo": self._client_undo,
            "redo": self._client_redo,
        }[event.op]
        return await handler(conn, event)

    async def _client_undo(self, conn: Connection, event: Any) -> Any:
        """[M2e] Human hits undo — reverse the last graph mutation for everyone.

        graph.undo() re-publishes the ordinary node/edge/finding events on the
        bus this hub is subscribed to, so the reversal fans out to every client
        through the normal flush path — no dedicated Server → Client op ([M2e]
        D-5). A no-op when the stack is empty; nothing is broadcast.
        """
        self.graph.undo()
        return None

    async def _client_redo(self, conn: Connection, event: Any) -> Any:
        """[M2e] Human hits redo — re-apply the last undone mutation (see _client_undo)."""
        self.graph.redo()
        return None

    async def _client_ping(self, conn: Connection, event: Any) -> Any:
        """[8-C] liveness (KI-1) — reply pong to the requester only.

        Direct send, not a broadcast: it is a per-connection liveness reply. It
        carries the current seq so the client's monotonic seq is untouched, and it
        consumes no new seq and joins no batch — a control message, not a graph
        event. A client that gets no pong concludes the socket is half-open and
        forces a reconnect, whose resync backfills whatever it missed.
        """
        await conn.send(encode_message("pong", {}, self.graph.events.seq))
        return None

    async def _client_focus_set(self, conn: Connection, event: Any) -> Any:
        node_id = event.data.id
        self.graph.focus = node_id
        # [5-C] track what the human is on, when, and the click history.
        now = self.clock()
        self.focused_id = node_id
        self.focused_since = now
        self.selection_history.append({"id": node_id, "ts": now})
        self._record_user_event("focus_change", {"id": node_id})
        return await self.broadcast("focus.set", {"id": node_id})

    async def _client_filter_set(self, conn: Connection, event: Any) -> Any:
        """Evaluate the human's filter with the [6] DSL and broadcast the result.

        A valid filter becomes the shared view: it is stored and broadcast to every
        client with the visible set, so the human's screen and get_visible_nodes
        ([5-C]) agree. An empty expression clears the filter (all visible).

        ★ An invalid filter (syntax / a [6] safety cap) must not crash the hub and
        must not change the shared filter. It is replied to the requesting client
        only, so nobody else's view lurches on someone's typo; the reply carries
        the current seq, so it never rewinds that client's monotonic seq ([8-C]).
        This uses the [8-C]-approved ``error`` field on filter.set — no new op.
        """
        expression = event.data.expression
        if not expression or not expression.strip():
            self.active_filter = None
            self.visible_ids = None
            self.graph.active_filter = None
            return await self.broadcast(
                "filter.set", {"expression": "", "visible_ids": [], "error": None}
            )

        try:
            visible = compile_filter(expression).evaluate_nodes(self.graph)
        except FilterError as exc:
            await conn.send(
                encode_message(
                    "filter.set",
                    {"expression": expression, "visible_ids": [], "error": str(exc)},
                    self.graph.events.seq,
                )
            )
            return None

        self.active_filter = expression
        self.visible_ids = visible
        self.graph.active_filter = expression
        # [5-C] filter_change is a user-state event the AI can poll for.
        self._record_user_event(
            "filter_change", {"expression": expression, "matched_count": len(visible)}
        )
        return await self.broadcast(
            "filter.set",
            {"expression": expression, "visible_ids": sorted(visible), "error": None},
        )

    async def _client_layer_toggle(self, conn: Connection, event: Any) -> Any:
        """Client sends {layer}; the server derives {layer, visible} ([8-C])."""
        layer = event.data.layer
        visible = not self.layer_visibility.get(layer, True)
        self.layer_visibility[layer] = visible
        return await self.broadcast(
            "layer.toggle", {"layer": layer, "visible": visible}
        )

    async def _client_layout_set(self, conn: Connection, event: Any) -> Any:
        """Stores the choice; running the layout is the renderer's ([5-D], [7-B])."""
        self.active_layout = {"algorithm": event.data.algorithm, "options": {}}
        return await self.broadcast("layout.set", dict(self.active_layout))

    async def _client_view_update(self, conn: Connection, event: Any) -> Any:
        """Viewport is per-client, so it is stored and not re-broadcast.

        [8-C] has no Server → Client view.update op; the server keeps the last
        value for get_view_state ([5-C]). Multiple browsers: the most recent
        view.update wins, so this reflects the last active client ([5-C]).
        """
        self.view_state = event.data.model_dump()
        return None

    # --- [5-C] user-state reads (the AI's view of what the human is doing) ---

    def get_focused_node(self) -> dict[str, Any] | None:
        """[5-C] the node the human has focused, and how long ago. None if none."""
        if self.focused_id is None or self.focused_since is None:
            return None
        since_ms = max(0, int((self.clock() - self.focused_since) * 1000))
        return {"id": self.focused_id, "since_ms": since_ms}

    def get_selection_history(self, last_n: int = 10) -> list[dict[str, Any]]:
        """[5-C] recent clicks, newest first. Bounded by the ring's own maxlen."""
        if last_n <= 0:
            return []
        items = list(self.selection_history)[-last_n:]
        return list(reversed(items))

    def get_view_state(self) -> dict[str, Any] | None:
        """[5-C] the last client's viewport (mode/zoom/camera). None if unset."""
        return self.view_state

    def poll_events(
        self,
        since_cursor: int | None = None,
        limit: int = MAX_POLL_LIMIT,
        event_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """[5-C] user-state events after ``since_cursor``, cursor-paginated.

        The cursor is this ring's own monotonic counter — deliberately **not** the
        [8-C] WS seq. Returns up to ``limit`` events of the requested types with a
        cursor greater than ``since_cursor``; the returned ``cursor`` is where the
        next poll should resume. ``dropped`` counts events after ``since_cursor``
        that the bounded ring evicted before this poll could see them — across all
        types, since an evicted event's type is gone with it.
        """
        since = since_cursor or 0
        types = set(event_types) if event_types else set(USER_EVENT_TYPES)
        capped = max(1, min(limit, MAX_POLL_LIMIT))

        if self._user_events:
            oldest = self._user_events[0]["cursor"]
            dropped = max(0, (oldest - 1) - since)
        else:
            dropped = 0

        matching = [e for e in self._user_events if e["cursor"] > since and e["type"] in types]
        page = matching[:capped]
        # Advance to the last event returned; if none of the requested types
        # remain, advance past everything so the client does not re-scan.
        cursor = page[-1]["cursor"] if page else max(since, self._event_cursor)
        return {
            "events": [
                {"type": e["type"], "data": e["data"], "ts": e["ts"], "cursor": e["cursor"]}
                for e in page
            ],
            "cursor": cursor,
            "dropped": dropped,
        }

    # --- [5-D] AI → screen (suggestions / navigation) ---
    #
    # These broadcast a server-originated op through the normal [8-C] path (seq +
    # coalescing window). They do not touch the graph — the AI is proposing or
    # navigating the human's view, not changing the data.

    async def suggest_filter(self, expression: str, reason: str) -> Any:
        """[5-D] propose a filter to the human — a banner, not an applied filter."""
        return await self.broadcast(
            "filter.suggest", {"expression": expression, "reason": reason}
        )

    async def focus_node(self, node_id: str) -> Any:
        """[5-D] navigate the human's view to a node (cosmos → cytoscape detail).

        Sets the shared graph focus and broadcasts focus.set, the same op a human
        click produces — the frontend need not tell them apart. The [5-C] human
        focus tracking (get_focused_node / selection history) is deliberately left
        alone: it reports what the *human* chose, not where the AI moved the view.
        """
        self.graph.focus = node_id
        return await self.broadcast("focus.set", {"id": node_id})

    async def set_layout(self, algorithm: str, options: dict[str, Any]) -> Any:
        """[5-D] change the cytoscape detail layout ([7-B] enum)."""
        self.active_layout = {"algorithm": algorithm, "options": options}
        return await self.broadcast("layout.set", dict(self.active_layout))

    async def apply_style(self, ids: list[str], style: dict[str, Any], ttl: int) -> str:
        """[5-D] temporary visual highlight on the given ids. Returns the style id.

        The tool resolved the [6] selector to ``ids`` and validated ``style``; the
        hub only mints an id and broadcasts. Graph untouched.
        """
        self._style_seq += 1
        style_id = f"style-{self._style_seq}"
        await self.broadcast(
            "style.apply", {"style_id": style_id, "ids": ids, "style": style, "ttl": ttl}
        )
        return style_id

    async def clear_style(self, style_id: str | None = None) -> Any:
        """[5-D] remove one AI style, or all of them when style_id is None."""
        return await self.broadcast("style.clear", {"style_id": style_id})

    async def add_annotation(self, x: float, y: float, text: str, ttl: int) -> str:
        """[5-D] a screen-space text note. Returns the annotation id.

        text is AI-written; the frontend renders it as escaped text ([11]).
        """
        self._annotation_seq += 1
        annotation_id = f"annotation-{self._annotation_seq}"
        await self.broadcast(
            "annotation.add",
            {"annotation_id": annotation_id, "x": x, "y": y, "text": text, "ttl": ttl},
        )
        return annotation_id


__all__ = [
    "Connection",
    "RateLimitExceeded",
    "RateLimiter",
    "ValidationError",
    "WSHub",
    "allowed_origins",
    "validate_origin",
]
