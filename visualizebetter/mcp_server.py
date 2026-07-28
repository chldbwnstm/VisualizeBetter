"""FastMCP tool 등록 ([5-F], [5-G]).

Tools delegate to Graph Core; no graph logic lives here. Docstrings are the
AI-facing tool descriptions, so they carry the plan's wording.

Scope note: the [5-A] WRITE tools (push_node/push_edge/update_node) and the
[23-B] reserved-key write guard land with their own task — none of the tools
here accept a properties map.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr as _xml_attr

from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourceCSP, UI_MIME_TYPE
from fastmcp.exceptions import ToolError
from fastmcp.resources import ResourceContent, ResourceResult
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from visualizebetter.filter import FilterError, compile_filter
from visualizebetter.graph.core import (
    MAX_FINDING_BODY_CHARS,
    MAX_FINDING_EVIDENCE,
    MAX_FINDING_NODE_IDS,
    MAX_FINDING_TAGS,
    MAX_FINDING_TITLE_CHARS,
    Finding,
    Graph,
    is_reserved_property,
)
from visualizebetter.graph.snapshots import AutoSnapshotter, SnapshotStore
from visualizebetter.render_in_chat import (
    RENDER_IN_CHAT_URI_TEMPLATE,
    build_subgraph,
    render_card_html,
)
from visualizebetter.ws.hub import MAX_POLL_LIMIT, SELECTION_HISTORY_MAX

MAX_RESPONSE_BYTES = 50 * 1024
"""[5] 공통 규칙: 직렬화 기준 50KB 초과 시 절단 + { truncated: true, total: N }.

Applies to list_findings — a real collection, where dropping rows is the right
answer. get_finding is never truncated: [23-B]'s size invariants bound a finding
at creation, so reading gold back returns it whole.
"""


def _resolve_layer(layer: str | None) -> str | None:
    """[23-E] layer auto-tagging hook — pass-through for now.

    Deriving a layer from the connection needs [10-A]'s
    ``f"{ai_client}-{session_start_iso}-{suffix}"``, which depends on the stdio
    proxy forwarding the MCP initialize clientInfo through to serve ([8-D]).
    That infrastructure does not exist yet, so an explicit layer passes through
    and None stays None. Auto-tagging attaches here once the connection→layer
    mapping lands.
    """
    return layer


def _serialized_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _finding_list_item(finding: Finding) -> dict[str, Any]:
    """[5-G] list row — body is excluded; get_finding carries the detail."""
    return {
        "finding_id": finding.finding_id,
        "title": finding.title,
        "confidence": finding.confidence,
        "node_ids": list(finding.node_ids),
        "created_by": finding.created_by,
        "created_at": finding.created_at,
        "tags": list(finding.tags),
    }


def _anchor_summary(graph: Graph, node_ids: list[str]) -> list[dict[str, Any]]:
    """Anchor node summary for get_finding.

    A finding is metadata and never creates graph nodes, so an anchor may point
    at a node that does not exist (yet) — those surface as {id, missing: true}.
    """
    summary: list[dict[str, Any]] = []
    for node_id in node_ids:
        node = graph.get_node(node_id)
        if node is None:
            summary.append({"id": node_id, "missing": True})
        else:
            summary.append({"id": node.id, "label": node.label, "type": node.type})
    return summary


def _capped_list(findings: list[dict[str, Any]], total: int) -> dict[str, Any]:
    """[5] 공통 규칙 — trim rows until the serialized response fits 50KB."""
    payload = {"findings": findings, "total": total}
    if _serialized_size(payload) <= MAX_RESPONSE_BYTES:
        return payload

    kept: list[dict[str, Any]] = []
    for item in findings:
        candidate = {"findings": [*kept, item], "total": total, "truncated": True}
        if _serialized_size(candidate) > MAX_RESPONSE_BYTES:
            break
        kept.append(item)
    return {"findings": kept, "total": total, "truncated": True}




# --- [5-B] READ limits (fail-fast at the tool boundary; untrusted args, [11]) ---
MAX_LIST_LIMIT = 1000
"""[5-B] list_* 페이지 상한 — a single response cannot carry the whole graph."""
MAX_NEIGHBOR_DEPTH = 3
"""[5-B] get_neighbors depth 서버측 상한 3 (hop 확장은 폭발적)."""
MAX_NEIGHBOR_NODES = 2000
"""[5-B] get_neighbors max_nodes 천장 — beyond this a summary is not a summary."""
MAX_FIND_PATHS = 100
"""[5-B] find_paths max_paths 천장."""
MAX_FIND_PATH_LENGTH = 10
"""[5-B] find_paths max_length 서버측 상한 10.

Distinct from [6]'s path_to bound of 8: path_to answers only "reachable?" and its
cap protects the filter evaluator, whereas find_paths enumerates real paths and
[5-B] sets its own cap at 10. Default stays 5.
"""
TOP_HUBS_COUNT = 10
"""[5-B] get_graph_summary top_hubs — the N highest-degree nodes."""

# --- [5-D] apply_style allowlist ([11]) ---
# The style dict is AI-supplied, so it is validated against a tiny allowlist, not
# passed through. Arbitrary CSS is the injection surface; only color / size /
# border are accepted, each range- or format-checked. Anchored, bounded patterns
# — no ReDoS risk (fixed patterns, unlike the [6] user-supplied regexes).
_STYLE_ALLOWED_KEYS = frozenset({"color", "size", "border"})
_BORDER_ALLOWED_KEYS = frozenset({"color", "width"})
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}")
_RGB_COLOR = re.compile(r"rgba?\(\s*[\d.\s,%]{1,64}\)")
_STYLE_MIN_SIZE = 1.0
_STYLE_MAX_SIZE = 100.0
_STYLE_MAX_BORDER_WIDTH = 20.0


def _validate_color(value: Any) -> str:
    """[11] a hex or rgb/rgba colour, nothing else (no url(), no names, no ;)."""
    if not isinstance(value, str) or not (
        _HEX_COLOR.fullmatch(value) or _RGB_COLOR.fullmatch(value)
    ):
        raise ToolError(f"style color must be hex or rgb/rgba, got {value!r} ([11])")
    return value


def _clamp_size(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ToolError("style size must be a number ([11])")
    return max(_STYLE_MIN_SIZE, min(_STYLE_MAX_SIZE, float(value)))


def _validate_border(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("style border must be an object ([11])")
    unknown = set(value) - _BORDER_ALLOWED_KEYS
    if unknown:
        raise ToolError(f"style border has unsupported keys: {sorted(unknown)} ([11])")
    out: dict[str, Any] = {}
    if "color" in value:
        out["color"] = _validate_color(value["color"])
    if "width" in value:
        width = value["width"]
        if not isinstance(width, (int, float)) or isinstance(width, bool):
            raise ToolError("style border width must be a number ([11])")
        out["width"] = max(0.0, min(_STYLE_MAX_BORDER_WIDTH, float(width)))
    return out


def _validate_style(style: dict[str, Any]) -> dict[str, Any]:
    """[5-D]/[11] reduce an AI style to the validated allowlist, or refuse it."""
    if not isinstance(style, dict):
        raise ToolError("style must be an object ([11])")
    unknown = set(style) - _STYLE_ALLOWED_KEYS
    if unknown:
        raise ToolError(f"style has unsupported keys: {sorted(unknown)} ([11])")
    if not style:
        raise ToolError("style must set at least one of color / size / border")
    out: dict[str, Any] = {}
    if "color" in style:
        out["color"] = _validate_color(style["color"])
    if "size" in style:
        out["size"] = _clamp_size(style["size"])
    if "border" in style:
        out["border"] = _validate_border(style["border"])
    return out

MAX_BATCH_ITEMS = 1000
"""[5-A] MCP 경유 상한: 호출당 nodes+edges 합계 1,000개."""

MAX_BATCH_PAYLOAD_BYTES = 1024 * 1024
"""[5-A] payload 1MB — tool 인자는 LLM 이 생성하는 토큰이라 대량 유입 경로가 아니다."""

_IMPORT_HINT = (
    "Use import_from_file for bulk loads — MCP tool arguments are LLM-generated "
    "tokens and cannot carry a graph this size ([5-A])."
)


UpdateReason = Annotated[
    Literal["correction", "supersede"] | None,
    Field(
        description=(
            "왜 바꾸는지 ([24]). 생략=일반 갱신. "
            "'correction'=기존 값이 틀렸다 → 덮어쓰고 틀린 값은 안 남긴다(변경 사실만 기록). "
            "'supersede'=기존 값이 유효했으나 낡았다 → 이전 값을 이력에 백업한 뒤 덮어쓴다."
        )
    ),
]
"""[24-D] "MCP tool 노출 시 Pydantic 이 reason enum 검증".

A Literal, so an unknown reason is refused by schema validation before the tool
body runs — and the two allowed values are advertised to the AI in the tool
schema, which is what makes the correction/supersession distinction usable.
"""


def _reject_reserved_properties(properties: dict[str, Any] | None) -> None:
    """[23-B] 예약키 쓰기보호 — `_` 접두 키는 시스템 소유다.

    `_citations` is written by cite() ([5-F]); letting a caller forge it would let
    AI-supplied text pose as server-recorded evidence, which is the one thing the
    inspector's evidence section is supposed to guarantee.
    """
    if not properties:
        return
    reserved = sorted(k for k in properties if is_reserved_property(k))
    if reserved:
        raise ToolError(
            f"properties keys starting with '_' are reserved: {reserved} ([23-B])"
        )


def _check_batch_limits(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """[5-A] 호출당 상한."""
    total = len(nodes) + len(edges)
    if total > MAX_BATCH_ITEMS:
        raise ToolError(f"batch of {total} exceeds {MAX_BATCH_ITEMS} items. {_IMPORT_HINT}")
    size = _serialized_size({"nodes": nodes, "edges": edges})
    if size > MAX_BATCH_PAYLOAD_BYTES:
        raise ToolError(
            f"batch payload {size} bytes exceeds {MAX_BATCH_PAYLOAD_BYTES}. {_IMPORT_HINT}"
        )


# push_batch takes raw dicts, so the per-field Pydantic bounds the push_node /
# push_edge tools enforce (ttl >= 0, weight >= 0) do not apply to its items — the
# same gap the ttl audit closed for the single-push tools ([audit #16]/M2g #3).
# These models re-apply exactly those bounds to each batch item; extra fields pass
# through untouched (add_node/add_edge still owns the rest of the spec).
class _NodeSpecBounds(BaseModel):
    model_config = ConfigDict(extra="allow")
    ttl: Annotated[int, Field(ge=0)] = 0


class _EdgeSpecBounds(BaseModel):
    model_config = ConfigDict(extra="allow")
    weight: Annotated[float, Field(ge=0)] = 1.0
    ttl: Annotated[int, Field(ge=0)] = 0


def _capped_snapshots(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """[5] 공통 규칙 — trim rows until the serialized response fits 50KB."""
    payload = {"snapshots": rows}
    if _serialized_size(payload) <= MAX_RESPONSE_BYTES:
        return payload

    kept: list[dict[str, Any]] = []
    for row in rows:
        candidate = {"snapshots": [*kept, row], "total": len(rows), "truncated": True}
        if _serialized_size(candidate) > MAX_RESPONSE_BYTES:
            break
        kept.append(row)
    return {"snapshots": kept, "total": len(rows), "truncated": True}


class FilterSession(Protocol):
    """The [5-C] user-state the read tools observe — what the human is doing.

    The WS hub implements this. The shared-filter half: ``active_filter`` is the
    expression the human last applied (None = no filter), ``visible_ids`` the set
    it evaluated to (None = everything visible). The rest is focus / view / event
    state, exposed as methods because they need the hub's clock and ring buffers.
    Kept a Protocol so the tools can be tested with a stub.
    """

    active_filter: str | None
    visible_ids: set[str] | None

    def get_focused_node(self) -> dict[str, Any] | None: ...
    def get_selection_history(self, last_n: int) -> list[dict[str, Any]]: ...
    def get_view_state(self) -> dict[str, Any] | None: ...
    def poll_events(
        self,
        since_cursor: int | None,
        limit: int,
        event_types: list[str] | None,
    ) -> dict[str, Any]: ...

    # [5-D] AI → screen. These broadcast a WS op; they do not touch the graph.
    async def suggest_filter(self, expression: str, reason: str) -> Any: ...
    async def focus_node(self, node_id: str) -> Any: ...
    async def set_layout(self, algorithm: str, options: dict[str, Any]) -> Any: ...
    async def apply_style(self, ids: list[str], style: dict[str, Any], ttl: int) -> str: ...
    async def clear_style(self, style_id: str | None) -> Any: ...
    async def add_annotation(self, x: float, y: float, text: str, ttl: int) -> str: ...


def create_server(
    graph: Graph,
    store: SnapshotStore | None = None,
    snapshotter: AutoSnapshotter | None = None,
    session: FilterSession | None = None,
) -> FastMCP:
    """Register the [5-G]/[5-F] tools against a Graph owned by the caller.

    The serve process owns the single Graph Core ([8-D]); this only wires tools
    to it.

    The [5-E] persistence tools need somewhere to persist to, so they register
    only when a store is supplied — serve always supplies one. Without them an AI
    cannot save or load, and the [23-D] handoff does not exist.
    """
    mcp = FastMCP("visualizebetter")
    _register_write(mcp, graph, snapshotter)

    @mcp.tool
    def record_finding(
        title: Annotated[str, Field(max_length=MAX_FINDING_TITLE_CHARS)],
        body: Annotated[str, Field(max_length=MAX_FINDING_BODY_CHARS)] = "",
        node_ids: Annotated[list[str], Field(max_length=MAX_FINDING_NODE_IDS)] | None = None,
        confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.8,
        evidence: Annotated[list[str], Field(max_length=MAX_FINDING_EVIDENCE)] | None = None,
        layer: str | None = None,
        tags: Annotated[list[str], Field(max_length=MAX_FINDING_TAGS)] | None = None,
    ) -> dict[str, Any]:
        """AI 가 분석 중 발견한 결정적 통찰(gold)을 못박는다.

        수천 개 구조 노드 사이에 묻히지 않도록 별도로 강조·조회된다.

        Args:
            title: 한 줄 요약 (예: "결제 실패의 핵심 경로")
            body: 상세 설명 (근거 서술)
            node_ids: 이 발견이 가리키는 노드들 (subgraph 앵커)
            confidence: 0.0~1.0 (AI 의 확신도 — 나중에 검증 우선순위)
            evidence: 근거 URL/주소 리스트 (cite 와 동일 성격)
            tags: 사용자 태그
        """
        try:
            finding = graph.add_finding(
                title=title,
                body=body,
                node_ids=node_ids or (),
                confidence=confidence,
                evidence=evidence or (),
                layer=_resolve_layer(layer),
                tags=tags or (),
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"ok": True, "finding_id": finding.finding_id}

    @mcp.tool
    def update_finding(
        finding_id: str, patch: dict[str, Any], reason: UpdateReason = None
    ) -> dict[str, Any]:
        """기존 finding 을 부분 갱신한다 (같은 발견의 갱신은 record 대신 이것).

        Args:
            finding_id: 대상 finding
            patch: { set: {...}, remove: [...] } — set 은 서버관리 필드
                (finding_id/created_at/updated_at/created_by/_superseded/
                _provenance) 를 제외한 모든 Finding 필드 허용. Finding 은
                properties 가 없어 remove 는 에러.
            reason: [24]. 'supersede' 는 이전 finding 버전을 _superseded 에
                보존한 뒤 갱신한다 — gold 의 이전 판본이 사라지지 않는다.
        """
        try:
            finding = graph.update_finding(finding_id, patch, reason=reason)
        except KeyError:
            raise ToolError(f"finding not found: {finding_id}") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"ok": True, "finding": finding.to_dict()}

    @mcp.tool
    def list_findings(
        layer: str | None = None,
        min_confidence: float | None = None,
        node_id: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """이전 세션의 gold 목록 — 인수인계 시 가장 먼저 호출한다.

        최신 발견이 먼저 온다 (created_at desc). body 는 무거워질 수 있어
        목록에서 빠지며, 상세는 get_finding 으로 가져온다.

        Args:
            layer: 특정 AI/세션의 발견만
            min_confidence: 이 확신도 이상만 (예: 0.5)
            node_id: 이 노드를 앵커로 가진 발견만
            limit: 페이지 크기
            offset: 페이지 시작 위치
        """
        # core 는 삽입순서 placeholder 이므로 전체 매치를 받아 도구 계층에서
        # created_at desc 로 정렬한 뒤 페이지를 자른다 ([5-G]).
        matches, total = graph.list_findings(
            layer=layer,
            min_confidence=min_confidence,
            node_id=node_id,
            limit=len(graph.findings),
            offset=0,
        )
        ordered = sorted(matches, key=lambda f: f.created_at, reverse=True)
        page = [_finding_list_item(f) for f in ordered[offset : offset + limit]]
        return _capped_list(page, total)

    @mcp.tool
    def get_finding(finding_id: str) -> dict[str, Any]:
        """단일 finding 의 전체 내용 (body·evidence 포함) 과 앵커 노드 요약.

        앵커가 아직 존재하지 않는 노드를 가리키면 { id, missing: true } 로 온다.
        """
        finding = graph.get_finding(finding_id)
        if finding is None:
            raise ToolError(f"finding not found: {finding_id}")
        # Never truncated: a finding is bounded at creation ([23-B] 크기 불변식),
        # and this tool exists to read gold back whole. Cutting evidence or body
        # here would damage the very thing the project preserves.
        return {
            "ok": True,
            "finding": finding.to_dict(),
            "anchors": _anchor_summary(graph, finding.node_ids),
        }

    @mcp.tool
    def delete_finding(finding_id: str) -> dict[str, Any]:
        """finding 을 삭제한다."""
        try:
            graph.delete_finding(finding_id)
        except KeyError:
            raise ToolError(f"finding not found: {finding_id}") from None
        return {"ok": True}

    @mcp.tool
    def cite(node_id: str, source_url: str, source_title: str) -> dict[str, Any]:
        """노드에 근거 링크를 붙인다 (예: IDA 어드레스, 문서 URL).

        한 노드에 여러 citation 이 누적된다. source_url 은 파일 경로/주소도
        허용 — 반드시 http 일 필요 없다.
        """
        try:
            node = graph.cite(node_id, source_url, source_title)
        except KeyError:
            raise ToolError(f"node not found: {node_id}") from None
        return {"ok": True, "node": node.to_dict()}

    _register_read(mcp, graph)
    _register_apps(mcp, graph)

    if session is not None:
        _register_view_read(mcp, graph, session)

    if store is not None:
        _register_persistence(mcp, graph, store, snapshotter)

    return mcp


def _register_apps(mcp: FastMCP, graph: Graph) -> None:
    """[9-D] MCP Apps — render_in_chat inline detail card (io.modelcontextprotocol/ui).

    A ``ui://`` resource returns the self-contained card HTML; the tool carries the
    MCP Apps ``_meta`` (AppConfig) so a host like Claude Desktop renders it in a
    sandboxed iframe. Native FastMCP low-level path — no prefab_ui dependency.
    """

    @mcp.resource(RENDER_IN_CHAT_URI_TEMPLATE, mime_type=UI_MIME_TYPE)
    def render_in_chat_card(node_id: str) -> ResourceResult:
        """[9-D] the neighbor-subgraph card HTML for ``node_id`` (self-contained).

        Returns a ResourceResult with an explicit UI mime — a bare ``str`` return
        from a resource template downgrades to text/plain, which the MCP Apps host
        would not render as a UI resource.
        """
        try:
            subgraph = build_subgraph(graph, node_id)
        except KeyError:
            raise ToolError(f"node not found: {node_id}") from None
        return ResourceResult(
            contents=[ResourceContent(render_card_html(subgraph), mime_type=UI_MIME_TYPE)]
        )

    @mcp.tool(app=AppConfig(resource_uri=RENDER_IN_CHAT_URI_TEMPLATE, csp=ResourceCSP()))
    def render_in_chat(node_id: str) -> dict[str, Any]:
        """이 노드의 이웃 서브그래프를 chat 안 인터랙티브 카드로 렌더한다 ([9-D]).

        Claude Desktop 등 MCP Apps 지원 클라이언트가 대화창 안 sandboxed iframe 으로
        컴팩트 detail 뷰(이웃 노드/엣지)를 보여준다 — 전체 라이브 뷰는 브라우저 별창.
        카드는 서버가 만든 자기완결 스냅샷이라 serve 에 연결하지 않고 비밀도 없다([11]).
        """
        try:
            subgraph = build_subgraph(graph, node_id)
        except KeyError:
            raise ToolError(f"node not found: {node_id}") from None
        return {
            "node_id": node_id,
            "neighbor_count": len(subgraph["neighbors"]),
            "edge_count": len(subgraph["edges"]),
        }


def _register_view_read(mcp: FastMCP, graph: Graph, session: FilterSession) -> None:
    """[5-C] the AI's view of the shared filter — what the human is looking at.

    Pure reads over the hub's session state ([5-C]); the filter is evaluated when
    the human sets it (WS filter.set), so these report the last shared result
    rather than re-evaluating. That is the point: the AI sees the same visible set
    the human's screen dims to.
    """

    @mcp.tool
    def get_active_filter() -> dict[str, Any]:
        """사람이 지금 적용한 필터 ([5-C]) — 식과 매칭 노드 수. 없으면 둘 다 null.

        AI 가 "사람이 무엇에 집중하고 있나"를 아는 통로다 (공유 뷰).
        """
        if session.active_filter is None or session.visible_ids is None:
            return {"expression": None, "matched_count": None}
        return {
            "expression": session.active_filter,
            "matched_count": len(session.visible_ids),
        }

    @mcp.tool
    def get_visible_nodes(
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_LIMIT)] = 1000,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """사람 화면에서 지금 보이는(필터 통과) 노드 id ([5-C]).

        필터가 없으면 전부 보이므로 전체 노드를 준다. 결정적 정렬 + 페이지네이션.
        """
        if session.visible_ids is None:
            ids = sorted(graph.nodes)  # no filter → everything is visible
        else:
            ids = sorted(session.visible_ids)
        total = len(ids)
        page = ids[offset : offset + limit]
        return {
            "total": total,
            "ids": page,
            "truncated": offset + len(page) < total,
        }

    @mcp.tool
    def get_focused_node() -> dict[str, Any] | None:
        """사람이 지금 선택(focus)한 노드 ([5-C]) — id 와 얼마나 됐는지(since_ms).

        선택이 없으면 null. AI 가 작업 사이에 "사람이 지금 뭘 보나"를 아는 통로다.
        """
        return session.get_focused_node()

    @mcp.tool
    def get_selection_history(
        last_n: Annotated[int, Field(ge=1, le=SELECTION_HISTORY_MAX)] = 10,
    ) -> dict[str, Any]:
        """최근 클릭 히스토리 ([5-C]) — 최신순 [{id, ts}, ...]."""
        return {"history": session.get_selection_history(last_n)}

    @mcp.tool
    def get_view_state() -> dict[str, Any] | None:
        """사람 화면의 뷰 상태 ([5-C], [9-C]) — mode/zoom/camera. 미설정이면 null.

        여러 브라우저가 붙어 있으면 가장 최근 활동 클라이언트의 상태다.
        """
        return session.get_view_state()

    @mcp.tool
    def poll_events(
        since_cursor: int | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_POLL_LIMIT)] = 100,
        event_types: list[Literal["focus_change", "filter_change"]] | None = None,
    ) -> dict[str, Any]:
        """사용자 상태 변화(focus/filter)를 커서 이후분만 폴링한다 ([5-C]).

        MCP 는 단발 요청/응답이라 서버가 AI 에게 push 하지 못한다 — AI 는 작업
        사이에 이걸 호출해 사람의 변화를 따라잡는다 ([5-C] 폴링 패턴).

        Args:
            since_cursor: 이 커서 이후 이벤트만. 생략하면 처음부터.
            limit: 한 번에 최대 (상한 100).
            event_types: 관심 타입 (기본 둘 다). 반환 cursor 로 다음 폴링을 잇는다.
                ring 이 넘쳐 유실된 건수는 dropped 로 알린다.
        ★ 이 cursor 는 [8-C] WS seq 와 별개다 (사용자상태 전용 커서).
        """
        return session.poll_events(
            since_cursor=since_cursor,
            limit=limit,
            event_types=event_types,
        )

    # --- [5-D] AI → screen: 제안·네비게이션 ---

    @mcp.tool
    async def suggest_filter(dsl_expr: str, reason: str) -> dict[str, Any]:
        """사람에게 필터를 제안한다 ([5-D]) — 적용이 아니라 배너로 뜬다.

        AI 가 "이걸 보면 좋겠다"를 제시하고, 적용 여부는 사람이 정한다.

        Args:
            dsl_expr: 제안할 필터 DSL 식 ([6]). 잘못된 식은 거부된다 — 나쁜 제안 방지.
            reason: 왜 이 필터인지 (사람이 읽을 한 줄).
        """
        # Validate before broadcasting: a suggestion the human cannot apply is
        # worse than none. compile_filter runs the full [6] parse + limits.
        try:
            compile_filter(dsl_expr)
        except FilterError as exc:
            raise ToolError(f"invalid filter suggestion: {exc}") from None
        await session.suggest_filter(dsl_expr, reason)
        return {"ok": True, "expression": dsl_expr}

    @mcp.tool
    async def focus_on(
        node_id: str,
        zoom_level: Annotated[float, Field(gt=0)] = 1.5,
    ) -> dict[str, Any]:
        """사람 화면을 이 노드로 이동한다 ([5-D]) — overview → detail 전환.

        Args:
            node_id: 초점을 맞출 노드.
            zoom_level: 상세 뷰 줌 힌트. M1 은 detail 뷰가 서브그래프를 자동 fit 하므로
                조언값이며, 프로토콜([8-C] focus.set{id})은 바뀌지 않는다.
        """
        await session.focus_node(node_id)
        return {"ok": True, "id": node_id}

    @mcp.tool
    async def set_layout(
        algorithm: Literal["dagre", "concentric", "fcose", "grid", "preset"],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """상세(cytoscape) 뷰의 레이아웃을 바꾼다 ([5-D], [7-B] 탭 enum 동일).

        Args:
            algorithm: 레이아웃 알고리즘 (탭 목록과 동일 enum).
            options: 알고리즘 옵션 (preset 은 노드 좌표를 여기로 전달).
        """
        await session.set_layout(algorithm, options or {})
        return {"ok": True, "algorithm": algorithm}

    @mcp.tool
    async def apply_style(
        selector: str,
        style: dict[str, Any],
        ttl: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """방금 push 한 것 등을 임시로 강조한다 ([5-D]) — 그래프는 안 바꾼다.

        Args:
            selector: 대상 노드를 고르는 필터 DSL 식 ([6]). 서버가 평가해 ids 를 낸다.
            style: {color?, size?, border?} 만 허용된다 — 임의 CSS 는 거부([11]).
                color 는 hex/rgba, size 는 1~100 로 clamp.
            ttl: 초. 0 이면 clear_style 로 지울 때까지 남는다.
        """
        try:
            ids = compile_filter(selector).evaluate_nodes(graph)
        except FilterError as exc:
            raise ToolError(f"invalid style selector: {exc}") from None
        validated = _validate_style(style)
        style_id = await session.apply_style(sorted(ids), validated, ttl)
        return {"ok": True, "style_id": style_id, "count": len(ids)}

    @mcp.tool
    async def clear_style(style_id: str | None = None) -> dict[str, Any]:
        """AI 스타일을 지운다 ([5-D]). style_id 생략하면 전부 지운다."""
        await session.clear_style(style_id)
        return {"ok": True}

    @mcp.tool
    async def add_annotation(
        x: float,
        y: float,
        text: Annotated[str, Field(max_length=500)],
        ttl: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """화면 (x, y) 에 텍스트 메모를 띄운다 ([5-D]).

        text 는 AI 가 쓴 것이라 프론트에서 이스케이프되어 렌더된다([11]).
        """
        annotation_id = await session.add_annotation(x, y, text, ttl)
        return {"ok": True, "annotation_id": annotation_id}


def _register_write(
    mcp: FastMCP, graph: Graph, snapshotter: AutoSnapshotter | None
) -> None:
    """[5-A] WRITE — the loop the project is built on: AI draws, human watches."""

    @mcp.tool
    def push_node(
        id: str,
        label: str,
        type: str,
        properties: dict[str, Any] | None = None,
        parent_id: str | None = None,
        style_hint: dict[str, Any] | None = None,
        layer: str | None = None,
        tags: list[str] | None = None,
        position_hint: dict[str, float] | None = None,
        ttl: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """발견한 노드를 그래프에 그린다 (분석 중 발견 즉시 호출).

        같은 id 로 다시 부르면 갱신된다 (properties 는 merge).

        Args:
            id: 고유 키 (예: "app.OrderService")
            label: 표시명
            type: 분류 (class/function/entity/file/...)
            properties: 임의 K/V. `_` 로 시작하는 키는 시스템 예약이라 거부된다.
            ttl: 초 단위 자동 만료 (0 = 영구)
        """
        _reject_reserved_properties(properties)
        node = graph.add_node(
            id=id,
            label=label,
            type=type,
            properties=properties or {},
            parent_id=parent_id,
            style_hint=style_hint,
            layer=_resolve_layer(layer),
            tags=tags or [],
            position_hint=position_hint,
            ttl=ttl,
        )
        return {"ok": True, "node": node.to_dict()}

    @mcp.tool
    def push_edge(
        source: str,
        target: str,
        relation: str,
        key: str = "",
        properties: dict[str, Any] | None = None,
        directed: bool = True,
        weight: Annotated[float, Field(ge=0)] = 1.0,
        layer: str | None = None,
        style_hint: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        ttl: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """두 노드의 관계를 그린다. 같은 (source,target,relation,key) 재호출 = 갱신.

        아직 push 하지 않은 노드를 가리켜도 된다 — 플레이스홀더 노드가 자동
        생성되고, 나중에 같은 id 로 push_node 하면 해소된다.

        Args:
            relation: 분류 (field / call / import / owns / ref)
            key: 같은 (source,target,relation) 병렬 엣지 구분자 (예: 필드명)
            weight: 렌더 강도 0.0~1.0
        """
        _reject_reserved_properties(properties)
        edge = graph.add_edge(
            source=source,
            target=target,
            relation=relation,
            key=key,
            properties=properties or {},
            directed=directed,
            weight=weight,
            layer=_resolve_layer(layer),
            style_hint=style_hint,
            tags=tags or [],
            ttl=ttl,
        )
        return {"ok": True, "edge": edge.to_dict()}

    @mcp.tool
    def push_batch(
        nodes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """여러 노드/엣지를 한 번에 그린다. 배치 안에서 서로 참조해도 된다.

        nodes 를 먼저 적용한 뒤 edges 를 적용한다. 호출당 nodes+edges 합계 1,000개
        상한 — 대량 유입은 import_from_file 을 쓴다.
        """
        node_dicts = nodes or []
        edge_dicts = edges or []
        _check_batch_limits(node_dicts, edge_dicts)

        errors: list[dict[str, Any]] = []
        added_nodes = 0
        added_edges = 0

        # [5-A] 트랜잭션 처리: nodes 먼저 → edges (배치 내 상호참조 허용).
        # [M2e] one batch = one undo step: the per-item add_* join this command.
        with graph.batch_command("push_batch"):
            for index, spec in enumerate(node_dicts):
                try:
                    _NodeSpecBounds.model_validate(spec)  # ttl >= 0, same as push_node
                    _reject_reserved_properties(spec.get("properties"))
                    graph.add_node(**{**spec, "layer": _resolve_layer(spec.get("layer"))})
                    added_nodes += 1
                except (ToolError, TypeError, ValueError, ValidationError) as exc:
                    errors.append({"kind": "node", "index": index, "error": str(exc)})

            for index, spec in enumerate(edge_dicts):
                try:
                    _EdgeSpecBounds.model_validate(spec)  # weight >= 0, ttl >= 0, same as push_edge
                    _reject_reserved_properties(spec.get("properties"))
                    graph.add_edge(**{**spec, "layer": _resolve_layer(spec.get("layer"))})
                    added_edges += 1
                except (ToolError, TypeError, ValueError, ValidationError) as exc:
                    errors.append({"kind": "edge", "index": index, "error": str(exc)})

        return {"added_nodes": added_nodes, "added_edges": added_edges, "errors": errors}

    @mcp.tool
    def update_node(
        id: str, patch: dict[str, Any], reason: UpdateReason = None
    ) -> dict[str, Any]:
        """노드를 부분 갱신한다.

        Args:
            patch: { set: {...}, remove: ["key", ...] } — set 은 merge,
                remove 는 properties 키 삭제. 서버관리 필드는 갱신 불가.
            reason: 틀린 값 정정과 낡은 값 갱신을 구분한다 ([24]).
                'supersede' 는 이전 값을 properties._superseded 에 보존하고,
                'correction' 은 보존하지 않는다 (틀린 값은 노이즈).
        """
        _reject_reserved_properties((patch or {}).get("set", {}).get("properties"))
        try:
            node = graph.update_node(id, patch, reason=reason)
        except KeyError:
            raise ToolError(f"node not found: {id}") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"ok": True, "node": node.to_dict()}

    @mcp.tool
    def update_edge(
        source: str,
        target: str,
        relation: str,
        key: str = "",
        patch: dict[str, Any] | None = None,
        reason: UpdateReason = None,
    ) -> dict[str, Any]:
        """엣지를 부분 갱신한다. patch/reason 규약은 update_node 와 같다."""
        _reject_reserved_properties((patch or {}).get("set", {}).get("properties"))
        try:
            edge = graph.update_edge(source, target, relation, key, patch or {}, reason=reason)
        except KeyError:
            raise ToolError(f"edge not found: {(source, target, relation, key)}") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        return {"ok": True, "edge": edge.to_dict()}

    @mcp.tool(annotations={"destructiveHint": True})
    def delete_node(id: str, cascade: bool = False) -> dict[str, Any]:
        """노드를 삭제한다.

        연결된 엣지가 있는데 cascade=False 면 삭제를 거부하고
        { ok: false, error: "has_edges", edge_count } 를 돌려준다 — 끊어진 엣지는
        허용되지 않는다.
        """
        try:
            return graph.delete_node(id, cascade=cascade)
        except KeyError:
            raise ToolError(f"node not found: {id}") from None

    @mcp.tool(annotations={"destructiveHint": True})
    def delete_edge(source: str, target: str, relation: str, key: str = "") -> dict[str, Any]:
        """엣지를 삭제한다."""
        try:
            return graph.delete_edge(source, target, relation, key)
        except KeyError:
            raise ToolError(f"edge not found: {(source, target, relation, key)}") from None

    @mcp.tool(annotations={"destructiveHint": True})
    async def clear_layer(layer: str) -> dict[str, Any]:
        """특정 AI/세션이 그린 노드/엣지를 삭제한다.

        삭제되는 노드에 걸린 엣지는 다른 layer 것이라도 함께 사라진다.
        findings 는 남는다. 실행 직전 자동 스냅샷이 저장되므로 응답의
        snapshot_id 로 되돌릴 수 있다.
        """
        snapshot_id = await _pre_clear_snapshot(snapshotter, f"clear_layer-{layer}")
        result = graph.clear_layer(layer)
        return {**result, "snapshot_id": snapshot_id}

    @mcp.tool(annotations={"destructiveHint": True})
    async def clear_all() -> dict[str, Any]:
        """그래프의 노드/엣지를 전부 삭제한다 (스냅샷과 findings 는 유지).

        실행 직전 자동 스냅샷이 저장되므로 응답의 snapshot_id 로 되돌릴 수 있다.
        """
        snapshot_id = await _pre_clear_snapshot(snapshotter, "clear_all")
        result = graph.clear_all()
        return {**result, "snapshot_id": snapshot_id}

    @mcp.tool
    def undo() -> dict[str, Any]:
        """[M2e] 마지막 그래프 변경(push/update/delete/clear/cite/import)을 되돌린다.

        되돌린 결과는 [8-C] 이벤트로 브로드캐스트되어 열린 뷰가 즉시 갱신된다.
        되돌릴 것이 없으면 { ok: false, error: "nothing_to_undo" }.
        (필터/스타일/포커스/스냅샷 같은 뷰 상태는 대상이 아니다.)
        """
        return graph.undo()

    @mcp.tool
    def redo() -> dict[str, Any]:
        """[M2e] 방금 undo 한 변경을 다시 적용한다.

        새로운 그래프 변경이 일어나면 redo 스택은 비워진다. 다시 적용할 것이
        없으면 { ok: false, error: "nothing_to_redo" }.
        """
        return graph.redo()


async def _pre_clear_snapshot(
    snapshotter: AutoSnapshotter | None, reason: str
) -> str | None:
    """[5-A] 파괴적 tool 안전장치 — confirm 파라미터 대신 되돌아갈 지점.

    [5-A] rejects a confirm flag outright: the caller is an AI, and an AI that
    wants to pass confirm=True simply does. A recovery point is protection that
    does not depend on the caller's judgement.
    """
    if snapshotter is None:
        return None
    pre = await snapshotter.snapshot_before(reason)
    return pre["snapshot_id"]


# --- [5-E] JSON import / export ---
#
# The interchange format is the /graph.json + snapshot schema built from to_dict —
# not a second serialization ([5-E] "재발명 말고 기존 to_dict 재사용"). Export is a
# plain dump; import is the security boundary: untrusted data goes through the same
# validation the WRITE tools use, so a '_'-prefixed reserved key ([23-B]) can never
# be forged, and server-managed fields (created_at, finding_id, history) are simply
# dropped rather than trusted.

_EXPORT_FORMAT_VERSION = "1"
_JSON_ONLY = ("json",)
# [5-E] export: json (native, round-trips) + graphml/dot/cytoscape (M2). Import
# stays JSON-only. Each format's file extension:
_EXPORT_FORMATS = ("json", "graphml", "dot", "cytoscape")
_EXPORT_EXT = {"json": "json", "graphml": "graphml", "dot": "dot", "cytoscape": "cyjs"}
MAX_IMPORT_INLINE_BYTES = MAX_BATCH_PAYLOAD_BYTES  # [5-E] inline 1MB (LLM tokens)

# The fields add_node/add_edge/add_finding accept. Everything else in to_dict
# (created_at/updated_at/finding_id, and Finding._superseded/_provenance) is
# server-managed and intentionally NOT importable — an import cannot forge it.
_IMPORT_NODE_FIELDS = (
    "id", "label", "type", "properties", "parent_id", "style_hint",
    "position_hint", "layer", "tags", "ttl", "created_by",
)
_IMPORT_EDGE_FIELDS = (
    "source", "target", "relation", "key", "properties", "directed", "weight",
    "layer", "style_hint", "tags", "ttl", "created_by",
)
_IMPORT_FINDING_FIELDS = (
    "title", "body", "node_ids", "confidence", "evidence", "layer", "tags", "created_by",
)


def _require_import_format(fmt: str) -> None:
    """[5-E] import is JSON-only (the native round-trip format)."""
    if fmt not in _JSON_ONLY:
        raise ToolError(f"import format {fmt!r} is not supported — use 'json' ([5-E]).")


def _require_export_format(fmt: str) -> None:
    """[5-E] export supports json + graphml / dot / cytoscape (M2)."""
    if fmt not in _EXPORT_FORMATS:
        raise ToolError(
            f"export format {fmt!r} is not supported — one of {list(_EXPORT_FORMATS)} ([5-E])."
        )


def _select_subgraph(graph: Graph, node_ids: set[str] | None):
    """The nodes/edges/findings an export covers. ``node_ids`` (a [6] filter match)
    restricts to those nodes, the edges among them, and the findings anchored to at
    least one of them ([5-E]) — identical selection across every format."""
    if node_ids is None:
        return (
            list(graph.nodes.values()),
            list(graph.edges.values()),
            list(graph.findings.values()),
        )
    nodes = [graph.nodes[i] for i in node_ids if i in graph.nodes]
    edges = [
        e for e in graph.edges.values() if e.source in node_ids and e.target in node_ids
    ]
    findings = [f for f in graph.findings.values() if any(n in node_ids for n in f.node_ids)]
    return nodes, edges, findings


def _graph_to_json_payload(graph: Graph, node_ids: set[str] | None) -> dict[str, Any]:
    """[5-E] the native JSON export payload, from to_dict (round-trips via import)."""
    nodes, edges, findings = _select_subgraph(graph, node_ids)
    return {
        "visualizebetter_export": _EXPORT_FORMAT_VERSION,
        "metadata": graph.metadata,
        "layers": list(graph.layers),
        "nodes": [n.to_dict() for n in nodes],
        "edges": [e.to_dict() for e in edges],
        "findings": [f.to_dict() for f in findings],
    }


def _serialize_json(graph: Graph, node_ids: set[str] | None) -> str:
    return json.dumps(_graph_to_json_payload(graph, node_ids), ensure_ascii=False)


def _serialize_cytoscape(graph: Graph, node_ids: set[str] | None) -> str:
    """cytoscape.js — ``{elements: {nodes:[{data}], edges:[{data}]}}``. JSON, so the
    serializer escapes every value; there is no markup/structure to break out of."""
    nodes, edges, _findings = _select_subgraph(graph, node_ids)
    return json.dumps(
        {
            "format": "cytoscape",
            "elements": {
                "nodes": [
                    {"data": {"id": n.id, "label": n.label, "type": n.type, "layer": n.layer}}
                    for n in nodes
                ],
                "edges": [
                    {
                        "data": {
                            "id": f"e{i}",
                            "source": e.source,
                            "target": e.target,
                            "relation": e.relation,
                            "weight": e.weight,
                            "directed": e.directed,
                        }
                    }
                    for i, e in enumerate(edges)
                ],
            },
        },
        ensure_ascii=False,
    )


def _serialize_graphml(graph: Graph, node_ids: set[str] | None) -> str:
    """GraphML (XML). ★[11] every id (attribute) and label/type/layer/relation
    (text) is XML-escaped, so a hostile value cannot close a tag or attribute and
    inject structure."""
    nodes, edges, _findings = _select_subgraph(graph, node_ids)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '  <key id="label" for="node" attr.name="label" attr.type="string"/>',
        '  <key id="type" for="node" attr.name="type" attr.type="string"/>',
        '  <key id="layer" for="node" attr.name="layer" attr.type="string"/>',
        '  <key id="relation" for="edge" attr.name="relation" attr.type="string"/>',
        '  <key id="weight" for="edge" attr.name="weight" attr.type="double"/>',
        '  <graph id="G" edgedefault="directed">',
    ]
    for n in nodes:
        out.append(f"    <node id={_xml_attr(n.id)}>")
        out.append(f'      <data key="label">{_xml_escape(n.label)}</data>')
        out.append(f'      <data key="type">{_xml_escape(n.type)}</data>')
        if n.layer:
            out.append(f'      <data key="layer">{_xml_escape(n.layer)}</data>')
        out.append("    </node>")
    for e in edges:
        out.append(f"    <edge source={_xml_attr(e.source)} target={_xml_attr(e.target)}>")
        out.append(f'      <data key="relation">{_xml_escape(e.relation)}</data>')
        out.append(f'      <data key="weight">{e.weight}</data>')
        out.append("    </edge>")
    out.append("  </graph>")
    out.append("</graphml>")
    return "\n".join(out)


def _dot_str(value: str) -> str:
    """A double-quoted dot string with ``\\`` and ``"`` escaped ([11] no breakout)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _serialize_dot(graph: Graph, node_ids: set[str] | None) -> str:
    """Graphviz dot. ★[11] every id and label is a quoted, escaped dot string so a
    hostile label cannot inject dot syntax (close the block, declare nodes, …)."""
    nodes, edges, _findings = _select_subgraph(graph, node_ids)
    out = ["digraph visualizebetter {"]
    for n in nodes:
        out.append(f"  {_dot_str(n.id)} [label={_dot_str(n.label)}, type={_dot_str(n.type)}];")
    for e in edges:
        attrs = f"label={_dot_str(e.relation)}"
        if not e.directed:  # an undirected edge in a digraph: draw it arrowless
            attrs += ", dir=none"
        out.append(f"  {_dot_str(e.source)} -> {_dot_str(e.target)} [{attrs}];")
    out.append("}")
    return "\n".join(out)


_EXPORT_SERIALIZERS = {
    "json": _serialize_json,
    "graphml": _serialize_graphml,
    "dot": _serialize_dot,
    "cytoscape": _serialize_cytoscape,
}


def export_graph_to_dir(
    graph: Graph, data_dir: Path, fmt: str, node_ids: set[str] | None
) -> dict[str, Any]:
    """[5-E] export_graph core — serialize to a file the server names inside its own
    data directory (the caller never supplies a path, so there is nothing to
    traverse [11]). Path safety is identical across formats."""
    _require_export_format(fmt)
    text = _EXPORT_SERIALIZERS[fmt](graph, node_ids)
    # Server-generated name only ([5-E]/[11]): no caller input reaches the path.
    target = Path(data_dir) / f"export-{uuid.uuid4().hex[:12]}.{_EXPORT_EXT[fmt]}"
    target.write_text(text, encoding="utf-8")
    return {"format": fmt, "path": str(target), "size": len(text.encode("utf-8"))}


def _import_specs(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """A payload's node/edge/finding list, validated to be a list of objects."""
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ToolError(f"import {key!r} must be a list of objects ([5-E]).")
    return value


def _parse_import_data(data: str | dict[str, Any], fmt: str) -> dict[str, Any]:
    """[5-E] import_graph input — inline JSON, 1MB cap (arguments are LLM tokens)."""
    _require_import_format(fmt)
    if isinstance(data, str):
        if len(data.encode("utf-8")) > MAX_IMPORT_INLINE_BYTES:
            raise ToolError(
                f"import payload exceeds {MAX_IMPORT_INLINE_BYTES} bytes — use "
                f"import_from_file for bulk loads ([5-E])."
            )
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ToolError(f"import data is not valid JSON: {exc}") from None
    elif isinstance(data, dict):
        if _serialized_size(data) > MAX_IMPORT_INLINE_BYTES:
            raise ToolError(
                f"import payload exceeds {MAX_IMPORT_INLINE_BYTES} bytes — use "
                f"import_from_file for bulk loads ([5-E])."
            )
        payload = data
    else:
        raise ToolError("import data must be a JSON string or object ([5-E]).")
    if not isinstance(payload, dict):
        raise ToolError("import data must be a JSON object with nodes/edges/findings.")
    return payload


def _resolve_import_path(root: Path, path: str) -> Path:
    """[11] import_from_file path safety — the resolved real path must stay inside
    the server's data directory. Absolute paths outside it, ``..`` traversal, and
    symlinks escaping it all resolve outside ``root`` and are refused; no arbitrary
    file read."""
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ToolError(
            f"import path must stay within the server data directory ([11]): {path!r}"
        )
    if not target.is_file():
        raise ToolError(f"import file not found in the data directory: {path!r}")
    return target


def _apply_import(target: Graph, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply nodes → edges → findings onto ``target`` through the WRITE validation
    ([5-E]/[11]). Reserved keys are rejected up front (fail-closed, atomic) so a
    forged '_' key applies nothing. Idempotent by identity; counts newly created."""
    node_specs = _import_specs(payload, "nodes")
    edge_specs = _import_specs(payload, "edges")
    finding_specs = _import_specs(payload, "findings")

    # Pre-pass: refuse a reserved-key forgery before touching the graph ([23-B]).
    for spec in node_specs:
        _reject_reserved_properties(spec.get("properties"))
    for spec in edge_specs:
        _reject_reserved_properties(spec.get("properties"))

    added_nodes = 0
    for spec in node_specs:
        kwargs = {k: spec[k] for k in _IMPORT_NODE_FIELDS if k in spec}
        if "id" not in kwargs:
            raise ToolError("imported node is missing 'id' ([4-A]).")
        is_new = kwargs["id"] not in target.nodes
        target.add_node(**{**kwargs, "layer": _resolve_layer(kwargs.get("layer"))})
        if is_new:
            added_nodes += 1

    added_edges = 0
    for spec in edge_specs:
        kwargs = {k: spec[k] for k in _IMPORT_EDGE_FIELDS if k in spec}
        for required in ("source", "target", "relation"):
            if required not in kwargs:
                raise ToolError(f"imported edge is missing {required!r} ([4-B]).")
        identity = (kwargs["source"], kwargs["target"], kwargs["relation"], kwargs.get("key", ""))
        is_new = identity not in target.edges
        target.add_edge(**{**kwargs, "layer": _resolve_layer(kwargs.get("layer"))})
        if is_new:
            added_edges += 1

    for spec in finding_specs:
        kwargs = {k: spec[k] for k in _IMPORT_FINDING_FIELDS if k in spec}
        if "title" not in kwargs:
            raise ToolError("imported finding is missing 'title' ([23-B]).")
        target.add_finding(**kwargs)

    return {"added_nodes": added_nodes, "added_edges": added_edges}


def import_payload(graph: Graph, payload: dict[str, Any], merge: bool) -> dict[str, Any]:
    """[5-E] import semantics. merge=True: idempotent merge onto the live graph
    (add_* publish events). merge=False: a full replace — import into a fresh Graph
    (same validation), then reload_from it ([5-E] "전체 그래프 교체" primitive)."""
    if merge:
        # [M2e] a merge import is one undo step (its add_* join this command).
        # merge=False needs none: reload_from clears the history anyway (D-6).
        with graph.batch_command("import"):
            return _apply_import(graph, payload)
    fresh = Graph(name=graph.metadata.get("name", "visualizebetter"))
    result = _apply_import(fresh, payload)
    graph.reload_from(fresh)
    # A full replace, like load_snapshot: tell live clients everything they hold is
    # stale so they full-resync from /graph.json ([8-C], audit #10). Published on the
    # graph's own bus so the hub forwards it and seq stays monotonic. snapshot_id is
    # empty — a replace-import is not a saved snapshot; the client resyncs regardless.
    graph.events.publish("snapshot.load", {"snapshot_id": ""})
    return result


def _register_persistence(
    mcp: FastMCP,
    graph: Graph,
    store: SnapshotStore,
    snapshotter: AutoSnapshotter | None,
) -> None:
    """[5-E] SESSION / PERSISTENCE — what makes the [23-D] handoff possible."""

    @mcp.tool
    async def save_snapshot(name: str, description: str = "") -> dict[str, Any]:
        """이 세션의 그래프를 스냅샷으로 저장한다 (다음 세션이 이어받을 지점).

        Args:
            name: 스냅샷 이름 (예: "프로젝트-구조-v1")
            description: 설명
        """
        # name is a DB value only — it never reaches the filesystem ([5-E], [11]).
        result = await store.save_snapshot(graph, name=name, description=description)
        return {"ok": True, **result}

    @mcp.tool
    async def list_snapshots() -> dict[str, Any]:
        """저장된 스냅샷 목록 (최신 먼저). 인수인계 시 가장 먼저 호출한다.

        kind 는 "manual"(사용자/AI 가 저장) 또는 "auto"(자동 스냅샷)다.
        """
        rows = await store.list_snapshots()
        return _capped_snapshots(rows)

    @mcp.tool
    async def load_snapshot(snapshot_id: str) -> dict[str, Any]:
        """스냅샷을 불러와 현재 그래프를 통째로 교체한다.

        교체 직전 현재 상태가 auto 스냅샷으로 저장되므로, 실수로 불러왔더라도
        되돌아갈 지점이 남는다.
        """
        pre_snapshot_id: str | None = None
        if snapshotter is not None:
            # [23-C] 파괴적 작업 직전 훅 — replacing the graph discards whatever is
            # live, so the current state gets a recovery point first.
            pre = await snapshotter.snapshot_before("load_snapshot")
            pre_snapshot_id = pre["snapshot_id"]

        try:
            loaded = await store.load_snapshot(snapshot_id)
        except KeyError:
            raise ToolError(f"snapshot not found: {snapshot_id}") from None

        graph.reload_from(loaded)
        # Published through the graph's own bus, so the hub forwards it on the
        # normal path and seq stays monotonic ([8-C]). Clients treat this as
        # "everything you hold is stale" and full-resync from /graph.json.
        graph.events.publish("snapshot.load", {"snapshot_id": snapshot_id})
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "findings": len(graph.findings),
            "pre_load_snapshot_id": pre_snapshot_id,
        }

    @mcp.tool
    def export_graph(
        format: str = "json",
        filter: str | None = None,
    ) -> dict[str, Any]:
        """그래프를 데이터 포맷 파일로 내보낸다 ([5-E]).

        파일은 서버 데이터 디렉토리 안에 생성되고(호출자 경로 지정 불가 — path
        traversal 차단 [11]) 그 경로를 반환한다. JSON 은 import 로 라운드트립된다.

        Args:
            format: 'json'(네이티브, import 로 라운드트립), 'graphml'(XML), 'dot'(Graphviz),
                'cytoscape'(cytoscape.js JSON) 중 하나. 그 외 값은 에러.
            filter: [6] DSL — 매치 노드 + 그 사이 엣지 + 앵커된 finding 만 부분 export.
        """
        node_ids: set[str] | None = None
        if filter is not None:
            compiled = _compile_or_tool_error(filter)
            try:
                node_ids = compiled.evaluate_nodes(graph)
            except FilterError as exc:
                raise ToolError(f"invalid filter: {exc}") from None
        return export_graph_to_dir(graph, store.data_dir, format, node_ids)

    @mcp.tool
    async def import_graph(
        data: str | dict[str, Any],
        format: str = "json",
        merge: bool = True,
    ) -> dict[str, Any]:
        """인라인 JSON 데이터를 임포트한다 ([5-E]). payload 1MB 상한.

        merge=True 는 identity((id) / (source,target,relation,key))로 멱등 병합,
        merge=False 는 전체 그래프 교체. 임포트 데이터는 WRITE 검증 경로를 통과하므로
        예약('_') 키를 위조할 수 없다([11]/[23-B]) — 서버관리 필드는 무시된다.

        Args:
            data: {nodes, edges, findings} JSON (문자열 또는 객체).
        """
        payload = _parse_import_data(data, format)
        if not merge:
            await _pre_clear_snapshot(snapshotter, "import_graph(replace)")
        return import_payload(graph, payload, merge)

    @mcp.tool
    async def import_from_file(
        path: str,
        format: str = "json",
        merge: bool = True,
    ) -> dict[str, Any]:
        """서버 데이터 디렉토리 안의 파일에서 대량 임포트한다 ([5-E], 100K+).

        서버가 파일을 직접 읽어 in-process 처리한다(push_batch 의 MCP 상한 미적용).
        path 는 데이터 디렉토리 내로 제한된다 — 절대경로/.. traversal/루트 밖 거부([11]).
        merge 의미론은 import_graph 와 같다.
        """
        _require_import_format(format)
        target = _resolve_import_path(store.data_dir, path)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ToolError(f"import file is not valid JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise ToolError("import file must be a JSON object with nodes/edges/findings.")
        if not merge:
            await _pre_clear_snapshot(snapshotter, "import_from_file(replace)")
        return import_payload(graph, payload, merge)


# --- [5-B] READ (AI → 그래프 상태 조회) ---


def _degree(graph: Graph, node_id: str) -> int:
    """무방향 incident-edge 수 (필터 DSL degree() 와 동일 정의, TASK R)."""
    return len(graph.indices.edges_of(node_id))


def _neighbor_summary(graph: Graph, node_id: str) -> list[dict[str, Any]]:
    """1홉 무방향 이웃 요약 ([5-B] get_node include_neighbors, Q1 무방향)."""
    seen: dict[str, dict[str, Any]] = {}
    for source, target, relation, _key in graph.indices.edges_of(node_id):
        other = target if source == node_id else source
        node = graph.get_node(other)
        if node is None or other in seen:
            continue
        seen[other] = {"id": node.id, "label": node.label, "type": node.type, "relation": relation}
    return list(seen.values())


def _capped_read(kind: str, rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    """[5] 공통 규칙 — trim the page until the serialized response fits 50KB.

    total is the full match count before the size trim, so the AI knows more rows
    exist even when this response could not carry them.
    """
    payload = {kind: rows, "total": total}
    if _serialized_size(payload) <= MAX_RESPONSE_BYTES:
        return payload
    kept: list[dict[str, Any]] = []
    for row in rows:
        candidate = {kind: [*kept, row], "total": total, "truncated": True}
        if _serialized_size(candidate) > MAX_RESPONSE_BYTES:
            break
        kept.append(row)
    return {kind: kept, "total": total, "truncated": True}


def _compile_or_tool_error(expression: str) -> Any:
    """[6] filter → CompiledFilter, DSL 에러를 AI 용 ToolError 로 변환 (fail-fast).

    Length/depth caps are checked here at parse; within/path/visited caps surface
    when the compiled filter is applied. Both families are FilterError, so a tool
    refuses an abusive filter with a clear message instead of a stack trace.
    """
    try:
        return compile_filter(expression)
    except FilterError as exc:
        raise ToolError(f"invalid filter: {exc}") from None


def _matched_edge_keys(graph: Graph, edge_filter: str | None) -> set | None:
    """Edge identities allowed by an edge_filter, or None for "all edges"."""
    if edge_filter is None:
        return None
    compiled = _compile_or_tool_error(edge_filter)
    try:
        return compiled.evaluate_edges(graph)
    except FilterError as exc:
        raise ToolError(f"invalid edge filter: {exc}") from None


def _sorted_page(
    items: list[Any], key: Any, order: str, limit: int, offset: int
) -> tuple[list[Any], int]:
    """Deterministic sort + [5-B] limit/offset window. Returns (page, total)."""
    ordered = sorted(items, key=key, reverse=(order == "desc"))
    return ordered[offset : offset + limit], len(ordered)


def _step_follows_direction(
    graph: Graph, key: tuple[str, str, str, str], at: str, direction: str
) -> bool:
    """Whether get_neighbors may step across ``key`` from ``at`` ([5-B] direction).

    ``both`` follows any incident edge (undirected). ``out`` follows an edge that
    leaves ``at`` (``at`` is the source); ``in`` follows one that enters it (``at``
    is the target) — the edge's direction is [4]'s source/target.

    ★ An edge with ``directed=False`` has no direction, so it is followed in every
    mode (treated as ``both``): a directional query must not hide an edge the graph
    explicitly marked undirected. A self-loop (source == target) is likewise both.
    """
    if direction == "both":
        return True
    edge = graph.edges.get(key)
    if edge is None or not edge.directed:
        return True
    source, target, _relation, _k = key
    if direction == "out":
        return source == at
    return target == at


# [5-B] search: the node fields an AI may text-search. A fixed allowlist, NOT the
# filter DSL — a bare field-name list ([11]). Reserved properties ('_'-prefixed,
# [23-B]) are never searchable, so search cannot be turned into a probe for
# system-managed provenance / citation / history values.
SEARCHABLE_NODE_FIELDS = frozenset(
    {"id", "label", "type", "layer", "created_by", "parent_id", "tags"}
)
_SEARCH_PROPERTIES_PREFIX = "properties."


def _validate_search_fields(in_fields: list[str]) -> None:
    """Reject any in_field outside the [5-B]/[11] allowlist (fail-closed).

    Allowed: a SEARCHABLE_NODE_FIELDS name, or 'properties.<name>' for a
    non-reserved property. An unknown field, or a reserved '_'-prefixed property,
    is refused with a clear message rather than silently searched — a bare field
    name must never become a hole for reading system-owned keys.
    """
    for field in in_fields:
        if field in SEARCHABLE_NODE_FIELDS:
            continue
        if field.startswith(_SEARCH_PROPERTIES_PREFIX):
            name = field[len(_SEARCH_PROPERTIES_PREFIX) :]
            if not name:
                raise ToolError(
                    f"malformed search field {field!r}: expected 'properties.<name>'"
                )
            if is_reserved_property(name):
                raise ToolError(
                    f"search field {field!r} targets a reserved property "
                    f"('_'-prefixed keys are system-owned, [23-B]) — not searchable ([11])"
                )
            continue
        raise ToolError(
            f"unknown search field {field!r}. allowed: "
            f"{sorted(SEARCHABLE_NODE_FIELDS)} or 'properties.<name>' (non-reserved)"
        )


def _node_matches_query(node: Any, in_fields: list[str], needle: str) -> bool:
    """True if ``needle`` (already lower-cased) is a substring of any in_field.

    Substring, case-insensitive, never a regex ([11]: no ReDoS surface). A ``None``
    field never matches (a missing created_by is not found by searching 'none').
    ``tags`` matches if any tag contains the needle; 'properties.<name>' matches
    the stringified property value.
    """
    for field in in_fields:
        if field == "tags":
            if any(needle in str(tag).lower() for tag in node.tags):
                return True
            continue
        if field.startswith(_SEARCH_PROPERTIES_PREFIX):
            name = field[len(_SEARCH_PROPERTIES_PREFIX) :]
            if name in node.properties and needle in str(node.properties[name]).lower():
                return True
            continue
        value = getattr(node, field, None)
        if value is not None and needle in str(value).lower():
            return True
    return False


def _register_read(mcp: FastMCP, graph: Graph) -> None:
    """[5-B] READ — how an AI (or the next session) inspects the graph.

    Every tool here is pure: it never mutates the graph, never touches the dirty
    flag, and never publishes an event. Reading gold back is the other half of the
    [23-D] handoff the WRITE tools set up.
    """

    @mcp.tool
    def get_graph_summary() -> dict[str, Any]:
        """그래프 전경 — 규모·type 분포·layer·최고 허브. 인수인계 시 먼저 부른다.

        수천 노드를 훑기 전에 "무엇이 얼마나 있나"를 한눈에 준다 (MVP 3대 도구).
        """
        type_counts: dict[str, int] = {}
        for node in graph.nodes.values():
            type_counts[node.type] = type_counts.get(node.type, 0) + 1
        top_hubs = sorted(
            ({"id": nid, "degree": _degree(graph, nid)} for nid in graph.nodes),
            key=lambda h: (-h["degree"], h["id"]),
        )[:TOP_HUBS_COUNT]
        return {
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "types": [
                {"type": t, "count": c}
                for t, c in sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
            "layers": list(graph.layers),
            "top_hubs": top_hubs,
        }

    @mcp.tool
    def get_node(id: str, include_neighbors: bool = False) -> dict[str, Any]:
        """노드 하나를 통째로 조회한다 (properties·태그 포함).

        properties 의 예약키(_citations/_superseded/_provenance)는 그대로 노출된다
        — 읽기 전용이라 위조 위험이 없고, 근거·이력을 보여주는 게 목적이다.

        Args:
            include_neighbors: True 면 1홉 무방향 이웃 요약을 함께 준다.
        """
        node = graph.get_node(id)
        if node is None:
            raise ToolError(f"node not found: {id}")
        result: dict[str, Any] = {"node": node.to_dict()}
        if include_neighbors:
            result["neighbors"] = _neighbor_summary(graph, id)
        return result

    @mcp.tool
    def list_nodes(
        filter: str | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_LIMIT)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
        sort_by: Literal["created_at", "id", "label", "type"] = "created_at",
        order: Literal["asc", "desc"] = "desc",
    ) -> dict[str, Any]:
        """노드 목록 — filter 로 좁히고, 결정적 정렬 + 페이지네이션.

        Args:
            filter: 필터 DSL 식 ([6]) — 예: 'type == "class" AND degree(node) > 5'.
                생략하면 전체. 잘못된 식/상한 초과는 명확한 에러로 거부된다.
            limit: 한 페이지 최대 (<=1000). offset: 건너뛸 개수.
            sort_by/order: 결정적 순서 (기본 created_at desc, 동률은 id 오름차순).
        """
        if filter is not None:
            compiled = _compile_or_tool_error(filter)
            try:
                ids = compiled.evaluate_nodes(graph)
            except FilterError as exc:
                raise ToolError(f"invalid filter: {exc}") from None
            matched = [graph.nodes[i] for i in ids]
        else:
            matched = list(graph.nodes.values())
        page, total = _sorted_page(
            matched,
            key=lambda n: (getattr(n, sort_by), n.id),
            order=order,
            limit=limit,
            offset=offset,
        )
        return _capped_read("nodes", [n.to_dict() for n in page], total)

    @mcp.tool
    def list_edges(
        filter: str | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_LIMIT)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """엣지 목록 — filter 로 좁히고 결정적 정렬 + 페이지네이션.

        Args:
            filter: 엣지 필터 DSL ([6]) — 예: 'relation == "calls" AND weight > 1'.
                엣지 필터에 노드 group function(degree 등)을 쓰면 명확한 에러로 거부된다.
        """
        edges = list(graph.edges.values())
        if filter is not None:
            compiled = _compile_or_tool_error(filter)
            try:
                keys = compiled.evaluate_edges(graph)
            except FilterError as exc:
                raise ToolError(f"invalid edge filter: {exc}") from None
            edges = [e for e in edges if e.identity in keys]
        page, total = _sorted_page(
            edges,
            key=lambda e: (e.created_at, e.source, e.target, e.relation, e.key),
            order="desc",
            limit=limit,
            offset=offset,
        )
        return _capped_read("edges", [e.to_dict() for e in page], total)

    @mcp.tool
    def get_neighbors(
        id: str,
        depth: Annotated[int, Field(ge=1, le=MAX_NEIGHBOR_DEPTH)] = 1,
        max_nodes: Annotated[int, Field(ge=1, le=MAX_NEIGHBOR_NODES)] = 200,
        direction: Literal["in", "out", "both"] = "both",
        edge_filter: str | None = None,
    ) -> dict[str, Any]:
        """id 로부터 depth 홉 이웃 서브그래프 ([5-B]).

        Args:
            depth: 홉 수 (서버 상한 3 — hop 확장은 폭발적).
            max_nodes: 이웃 노드 상한. 초과하면 잘라내고 truncated=true.
            direction: 홉마다 엣지 방향을 어떻게 따를지 ([4] source/target).
                'both'=무방향(기본), 'out'=나가는 엣지(id→…)만, 'in'=들어오는 엣지(…→id)만.
                directed=False 엣지는 방향이 없어 in/out 양쪽에서 따라간다.
            edge_filter: 이 필터를 통과하는 엣지만 따라간다 ([6]).
        """
        center = graph.get_node(id)
        if center is None:
            raise ToolError(f"node not found: {id}")
        allowed = _matched_edge_keys(graph, edge_filter)

        # BFS. Truncation is explicit (truncated=true), never silent: a summary
        # that quietly dropped nodes would mislead the reader ([5-B]). The
        # direction rule is applied per hop, relative to the node being expanded.
        reached: dict[str, int] = {id: 0}
        frontier: list[str] = [id]
        edge_keys: set = set()
        truncated = False
        hop = 0
        while frontier and hop < depth:
            hop += 1
            nxt: list[str] = []
            for node_id in frontier:
                for src, tgt, rel, key in graph.indices.edges_of(node_id):
                    if allowed is not None and (src, tgt, rel, key) not in allowed:
                        continue
                    if not _step_follows_direction(graph, (src, tgt, rel, key), node_id, direction):
                        continue
                    other = tgt if src == node_id else src
                    edge_keys.add((src, tgt, rel, key))
                    if other in reached:
                        continue
                    if len(reached) >= max_nodes:
                        truncated = True
                        continue
                    reached[other] = hop
                    nxt.append(other)
            frontier = nxt

        neighbor_ids = [n for n in reached if n != id]
        edges = [graph.edges[k].to_dict() for k in edge_keys if k in graph.edges]
        return {
            "center": center.to_dict(),
            "neighbors": [graph.nodes[n].to_dict() for n in neighbor_ids if n in graph.nodes],
            "edges": edges,
            "truncated": truncated,
        }

    @mcp.tool
    def find_paths(
        source: str,
        target: str,
        max_paths: Annotated[int, Field(ge=1, le=MAX_FIND_PATHS)] = 10,
        max_length: Annotated[int, Field(ge=1, le=MAX_FIND_PATH_LENGTH)] = 5,
        edge_filter: str | None = None,
    ) -> dict[str, Any]:
        """source→target 실제 경로 열거 (무방향, 최단 우선). path_to 의 후속.

        필터 DSL 의 path_to 는 "도달 가능?"(bool)만 답한다 — 실제 경로는 여기서 준다.

        Args:
            max_paths: 반환할 경로 수 (상한 100). 도달 시 조기 종료 + truncated.
            max_length: 경로 최대 홉 (기본 5, 서버 상한 10 — [5-B]).
            edge_filter: 이 필터를 통과하는 엣지만 따라간다 ([6]).
        """
        if graph.get_node(source) is None:
            raise ToolError(f"node not found: {source}")
        if graph.get_node(target) is None:
            raise ToolError(f"node not found: {target}")
        allowed = _matched_edge_keys(graph, edge_filter)

        # BFS over simple paths → shorter paths surface first ([5-B] k-shortest).
        # Bounded twice: max_length hops, and a hard expansion budget so a dense
        # graph cannot explode the search ([6] 안전상한 정신 계승).
        paths: list[list[str]] = []
        queue: deque[list[str]] = deque([[source]])
        truncated = False
        expansions = 0
        budget = MAX_NEIGHBOR_NODES * MAX_NEIGHBOR_DEPTH
        while queue:
            path = queue.popleft()
            last = path[-1]
            if last == target:
                paths.append(path)
                if len(paths) >= max_paths:
                    truncated = truncated or bool(queue)
                    break
                continue
            if len(path) - 1 >= max_length:
                continue
            for src, tgt, rel, key in sorted(graph.indices.edges_of(last)):
                if allowed is not None and (src, tgt, rel, key) not in allowed:
                    continue
                other = tgt if src == last else src
                if other in path:  # simple path — no revisits
                    continue
                expansions += 1
                if expansions > budget:
                    truncated = True
                    queue.clear()
                    break
                queue.append([*path, other])
        return {"paths": paths, "truncated": truncated}

    @mcp.tool
    def search(
        query: str,
        in_fields: list[str] | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_LIMIT)] = 50,
    ) -> dict[str, Any]:
        """노드 부분 일치 검색 — query 를 in_fields 각 필드에서 substring 매치 ([5-B]).

        대소문자 무시 substring 매치(정규식 아님 — ReDoS 없음, [11]). in_fields 는
        filter DSL 이 아니라 단순 필드명 목록이고, 검색 대상은 화이트리스트로 제한된다:
        {id, label, type, layer, created_by, parent_id, tags} 와 'properties.<name>'
        (비예약 키만). 예약('_' 접두) 속성이나 미지 필드는 명확한 에러로 거부한다 —
        bare 필드명이 시스템 소유 키([23-B])를 읽는 구멍이 되지 않게 한다.

        결과는 id 오름차순(결정적)으로 정렬해 limit(상한 1000)까지 준다. total 은
        limit 이전 전체 매치 수라, 더 있으면 AI 가 안다. READ 무변형(dirty·이벤트 없음).

        Args:
            query: 찾을 문자열. 대소문자 무시 부분 일치(빈 문자열은 모든 필드값의
                부분열이므로 값이 있는 노드를 매치한다).
            in_fields: 검색할 노드 필드 (기본 ["label", "id"]).
            limit: 최대 반환 노드 수 (기본 50, 상한 1000).
        """
        fields = ["label", "id"] if in_fields is None else in_fields
        _validate_search_fields(fields)
        needle = query.lower()
        matched = [
            node
            for node in graph.nodes.values()
            if _node_matches_query(node, fields, needle)
        ]
        # id-ascending is fully deterministic and independent of insertion order.
        page, total = _sorted_page(
            matched, key=lambda n: n.id, order="asc", limit=limit, offset=0
        )
        return _capped_read("nodes", [n.to_dict() for n in page], total)
