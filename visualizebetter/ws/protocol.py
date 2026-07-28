"""WebSocket wire schema — [8-C] verbatim.

Every Server → Client event carries a monotonically increasing ``seq``.

Node / Edge / Finding payloads stay ``dict`` rather than getting mirrored Pydantic
models: they are already defined by [4-A] / [4-B] / [23-B] and serialized by Graph
Core's ``to_dict()``. A second declaration here would be a duplicate data model —
free to drift, and no safer, since Server → Client data is server-generated.
Pydantic's job per [8-C] is validating Client → Server input, which is untrusted
browser traffic; those payloads are fully modelled below.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from visualizebetter.graph.events import Event

# --- Server → Client payloads ([8-C]) ---


class NodeUpdateData(BaseModel):
    id: str
    patch: dict[str, Any]  # patch 규약 = [5-A]


class NodeDeleteData(BaseModel):
    id: str


class EdgeIdentityData(BaseModel):
    source: str
    target: str
    relation: str
    key: str = ""


class EdgeUpdateData(EdgeIdentityData):
    patch: dict[str, Any]


class GraphBatchData(BaseModel):
    """[8-C] — node/edge only. findings are not coalesced (they are low-rate)."""

    nodes_added: list[dict[str, Any]] = Field(default_factory=list)
    nodes_updated: list[NodeUpdateData] = Field(default_factory=list)
    nodes_deleted: list[NodeDeleteData] = Field(default_factory=list)
    edges_added: list[dict[str, Any]] = Field(default_factory=list)
    edges_updated: list[EdgeUpdateData] = Field(default_factory=list)
    edges_deleted: list[EdgeIdentityData] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (
                self.nodes_added,
                self.nodes_updated,
                self.nodes_deleted,
                self.edges_added,
                self.edges_updated,
                self.edges_deleted,
            )
        )


class FindingUpdateData(BaseModel):
    finding_id: str
    patch: dict[str, Any]


class FindingDeleteData(BaseModel):
    finding_id: str


class FilterSetData(BaseModel):
    expression: str
    visible_ids: list[str] = Field(default_factory=list)
    # [8-C] present (a per-client notice) when the [6] filter was rejected — the
    # shared filter is then unchanged. null/absent on a normally applied filter.
    # The hub already puts this on the wire; the frontend type carries it too.
    error: str | None = None


class FilterSuggestData(BaseModel):
    expression: str
    reason: str


class FocusSetData(BaseModel):
    id: str


class LayerToggleData(BaseModel):
    layer: str
    visible: bool


class LayoutSetData(BaseModel):
    algorithm: str
    options: dict[str, Any] = Field(default_factory=dict)


class StyleApplyData(BaseModel):
    """[5-D] style.apply — the server evaluates the selector and sends target ids.

    The frontend has no filter DSL, so the wire carries resolved ``ids``, not the
    selector. ``style`` is the allowlisted subset the tool validated ([11]).
    """

    style_id: str
    ids: list[str]
    style: dict[str, Any]
    ttl: int = 0


class StyleClearData(BaseModel):
    style_id: str | None = None


class AnnotationAddData(BaseModel):
    annotation_id: str
    x: float
    y: float
    text: str
    ttl: int = 0


class SnapshotLoadData(BaseModel):
    snapshot_id: str


class ClearData(BaseModel):
    layer: str | None = None


# --- Server → Client envelopes ([8-C]) ---


class _ServerEvent(BaseModel):
    seq: int


class NodeAddEvent(_ServerEvent):
    op: Literal["node.add"] = "node.add"
    data: dict[str, Any]  # Node ([4-A])


class NodeUpdateEvent(_ServerEvent):
    op: Literal["node.update"] = "node.update"
    data: NodeUpdateData


class NodeDeleteEvent(_ServerEvent):
    op: Literal["node.delete"] = "node.delete"
    data: NodeDeleteData


class EdgeAddEvent(_ServerEvent):
    op: Literal["edge.add"] = "edge.add"
    data: dict[str, Any]  # Edge ([4-B])


class EdgeUpdateEvent(_ServerEvent):
    op: Literal["edge.update"] = "edge.update"
    data: EdgeUpdateData


class EdgeDeleteEvent(_ServerEvent):
    op: Literal["edge.delete"] = "edge.delete"
    data: EdgeIdentityData


class GraphBatchEvent(_ServerEvent):
    op: Literal["graph.batch"] = "graph.batch"
    data: GraphBatchData


class FindingAddEvent(_ServerEvent):
    op: Literal["finding.add"] = "finding.add"
    data: dict[str, Any]  # Finding ([23-B])


class FindingUpdateEvent(_ServerEvent):
    op: Literal["finding.update"] = "finding.update"
    data: FindingUpdateData


class FindingDeleteEvent(_ServerEvent):
    op: Literal["finding.delete"] = "finding.delete"
    data: FindingDeleteData


class FilterSetEvent(_ServerEvent):
    op: Literal["filter.set"] = "filter.set"
    data: FilterSetData


class FilterSuggestEvent(_ServerEvent):
    op: Literal["filter.suggest"] = "filter.suggest"
    data: FilterSuggestData


class FocusSetEvent(_ServerEvent):
    op: Literal["focus.set"] = "focus.set"
    data: FocusSetData


class LayerToggleEvent(_ServerEvent):
    op: Literal["layer.toggle"] = "layer.toggle"
    data: LayerToggleData


class LayoutSetEvent(_ServerEvent):
    op: Literal["layout.set"] = "layout.set"
    data: LayoutSetData


class StyleApplyEvent(_ServerEvent):
    op: Literal["style.apply"] = "style.apply"
    data: StyleApplyData


class StyleClearEvent(_ServerEvent):
    op: Literal["style.clear"] = "style.clear"
    data: StyleClearData


class AnnotationAddEvent(_ServerEvent):
    op: Literal["annotation.add"] = "annotation.add"
    data: AnnotationAddData


class SnapshotLoadEvent(_ServerEvent):
    op: Literal["snapshot.load"] = "snapshot.load"
    data: SnapshotLoadData


class ClearEvent(_ServerEvent):
    op: Literal["clear"] = "clear"
    data: ClearData


class PongData(BaseModel):
    """[8-C] heartbeat reply — no payload; the reply itself proves the server is live."""


class PongEvent(_ServerEvent):
    """[8-C] liveness (KI-1). Sent in reply to a client ping; carries the current
    seq like any server message but consumes no new seq and joins no batch."""

    op: Literal["pong"] = "pong"
    data: PongData = PongData()


ServerEvent = Annotated[
    Union[
        NodeAddEvent,
        NodeUpdateEvent,
        NodeDeleteEvent,
        EdgeAddEvent,
        EdgeUpdateEvent,
        EdgeDeleteEvent,
        GraphBatchEvent,
        FindingAddEvent,
        FindingUpdateEvent,
        FindingDeleteEvent,
        FilterSetEvent,
        FilterSuggestEvent,
        FocusSetEvent,
        LayerToggleEvent,
        LayoutSetEvent,
        StyleApplyEvent,
        StyleClearEvent,
        AnnotationAddEvent,
        SnapshotLoadEvent,
        ClearEvent,
        PongEvent,
    ],
    Field(discriminator="op"),
]

SERVER_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(ServerEvent)


# --- Client → Server ([8-C]) — untrusted browser input, fully modelled ---


class ClientFocusSetData(BaseModel):
    id: str


class ClientFilterSetData(BaseModel):
    expression: str


class ClientLayerToggleData(BaseModel):
    layer: str


class ClientLayoutSetData(BaseModel):
    algorithm: str


class CameraPos(BaseModel):
    x: float
    y: float


class ClientViewUpdateData(BaseModel):
    mode: Literal["overview", "detail", "split"]  # [9-C] viewMode 와 동일 enum
    zoom: float
    camera_pos: CameraPos


class ClientFocusSet(BaseModel):
    op: Literal["focus.set"]
    data: ClientFocusSetData


class ClientFilterSet(BaseModel):
    op: Literal["filter.set"]
    data: ClientFilterSetData


class ClientLayerToggle(BaseModel):
    op: Literal["layer.toggle"]
    data: ClientLayerToggleData


class ClientLayoutSet(BaseModel):
    op: Literal["layout.set"]
    data: ClientLayoutSetData


class ClientViewUpdate(BaseModel):
    op: Literal["view.update"]
    data: ClientViewUpdateData


class ClientPingData(BaseModel):
    """[8-C] heartbeat — no payload. The client sends this to prove *the server*
    is still live: an unanswered ping is how the client escapes a half-open
    socket its own onclose never fired for (KI-1)."""


class ClientPing(BaseModel):
    op: Literal["ping"]
    data: ClientPingData = ClientPingData()


class ClientHistoryData(BaseModel):
    """[M2e] undo/redo carry no payload — the target is the shared history stack."""


class ClientUndo(BaseModel):
    op: Literal["undo"]
    data: ClientHistoryData = ClientHistoryData()


class ClientRedo(BaseModel):
    op: Literal["redo"]
    data: ClientHistoryData = ClientHistoryData()


ClientEvent = Annotated[
    Union[
        ClientFocusSet,
        ClientFilterSet,
        ClientLayerToggle,
        ClientLayoutSet,
        ClientViewUpdate,
        ClientPing,
        ClientUndo,
        ClientRedo,
    ],
    Field(discriminator="op"),
]

CLIENT_EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(ClientEvent)


# --- wire encoding ---


def encode_event(event: Event) -> str:
    """Graph Core's Event(op, data, seq) → the [8-C] wire message."""
    return json.dumps(
        {"op": event.op, "data": event.data, "seq": event.seq}, ensure_ascii=False
    )


def encode_message(op: str, data: Any, seq: int) -> str:
    if isinstance(data, BaseModel):
        data = data.model_dump()
    return json.dumps({"op": op, "data": data, "seq": seq}, ensure_ascii=False)


def decode_server_event(raw: str | dict[str, Any]) -> Any:
    """Parse a Server → Client message back into its typed envelope."""
    payload = json.loads(raw) if isinstance(raw, str) else raw
    return SERVER_EVENT_ADAPTER.validate_python(payload)


def decode_client_event(raw: str | dict[str, Any]) -> Any:
    """Validate untrusted Client → Server input ([8-C]). Raises ValidationError."""
    payload = json.loads(raw) if isinstance(raw, str) else raw
    return CLIENT_EVENT_ADAPTER.validate_python(payload)
