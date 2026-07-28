"""Graph Core — Node / Edge / Graph / Finding ([4-A], [4-B], [4-C], [23-B]).

Mutation semantics are [5-A] verbatim; every mutation sets the [23-C] dirty flag
and publishes the matching [8-C] event.
"""

from __future__ import annotations

import copy
import functools
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

from visualizebetter.graph.events import (
    CLEAR,
    EDGE_ADD,
    EDGE_DELETE,
    EDGE_UPDATE,
    FINDING_ADD,
    FINDING_DELETE,
    FINDING_UPDATE,
    NODE_ADD,
    NODE_DELETE,
    NODE_UPDATE,
    EventBus,
)
from visualizebetter.graph.history import GraphHistory
from visualizebetter.graph.indices import Indices

EdgeKey = tuple[str, str, str, str]
"""Edge identity ([4-B]): (source, target, relation, key)."""

PLACEHOLDER_TYPE = "unresolved"
PLACEHOLDER_PROPERTY = "placeholder"

RESERVED_PROPERTY_PREFIX = "_"
CITATIONS_PROPERTY = "_citations"
SUPERSEDED_PROPERTY = "_superseded"
PROVENANCE_PROPERTY = "_provenance"

# [24] 데이터 생명주기 — 정정 vs supersession.
REASON_CORRECTION = "correction"
REASON_SUPERSEDE = "supersede"
VALID_REASONS = frozenset({REASON_CORRECTION, REASON_SUPERSEDE})

# [24-C] node/edge 이력 상한. These live in `properties`, which carries no size
# invariant in M1 ([23-B] bounds findings only), so the cap is by count alone.
MAX_SUPERSEDED_ENTRIES = 10

MAX_CITATIONS_ENTRIES = 100
"""[13-B] CH1(4) — cap on a node's ``_citations`` array.

The third reserved array was the only uncapped one (``_superseded`` 10,
``_provenance`` 50), and it grows through the same quadratic path XX was fixed
for: ``cite()`` appends, then publishes the *whole* list in the patch, while
``history.touch_node`` deep-copies the entire node per call. Measured: 200 cites
= 21KB properties / 2.1MB of batch payload; 2000 cites = 215KB / **214MB**.

100 rather than 50: an entry is small (url + title + ts) and evidence lists are
legitimately longer than change logs — a node can reasonably carry dozens of
sources. What must stay bounded is the wire payload, which grows linearly with
this number, so it is capped rather than left open. Oldest evicted first.
"""

MAX_PROVENANCE_ENTRIES = 50
"""[23-C] RN7 XX — cap on a node/edge ``_provenance`` log.

``_superseded`` was capped from the start but this was not, so
``reason="correction"`` repeated 2000 times grew properties to 160KB, made the
final node.update WS payload 160KB, and took 7.81s in total — the whole log is
deep-copied on every write, so the cost is quadratic. A provenance entry is small
(action + timestamp + author, ~100B) unlike a superseded ``prev`` value, so the
cap is looser than 10 while still bounded: 50 x ~100B ~ 5KB.
"""

# [24-C] finding 이력 상한 — count alone does not bound a finding's size: a
# superseded body may be MAX_FINDING_BODY_CHARS, so 10 of them would build a
# ~160KB finding through a path that never passes _check_finding_size. That would
# break the [23-B] invariant get_finding relies on ("bounded at creation, so gold
# reads back whole"). The archive is therefore bounded by serialized size, oldest
# evicted first (최근 이력 우선), with a count cap as a secondary guard.
MAX_FINDING_SUPERSEDED_ENTRIES = 20
MAX_FINDING_SUPERSEDED_BYTES = 16384

# [23-B] Finding 크기 불변식 — a finding is bounded at creation so that reading
# gold back never has to truncate it. Enforced here rather than at the MCP layer
# because adapters / import / snapshot load call Graph Core directly ([5-E], [12]).
MAX_FINDING_TITLE_CHARS = 1024
MAX_FINDING_BODY_CHARS = 16384
MAX_FINDING_NODE_IDS = 256
MAX_FINDING_EVIDENCE = 64
MAX_FINDING_TAGS = 64


def is_reserved_property(key: str) -> bool:
    """[23-B]: properties keys starting with ``_`` are system-reserved.

    Reserved keys hold system-owned data (citations / provenance / supersede
    history) and surface separately in the UI inspector's evidence section. The
    filter DSL evaluator imports this function (filter/evaluate.py) and resolves
    any reserved key — ``properties._x`` or a bare ``_x`` — to MISSING, so no
    filter, including untrusted WS ``filter.set``, can read them ([6]).
    """
    return key.startswith(RESERVED_PROPERTY_PREFIX)


_PATCH_KEYS = frozenset({"set", "remove"})
"""[5-A] the only keys a patch may carry ([23-C] RN6 NN(2))."""


def check_patch_shape(patch: Any) -> None:
    """[5-A] a patch is ``{set: {...}, remove: [...]}`` — settle that shape first.

    [23-C] RN5 JJ. Several entry points read ``patch["set"]`` *before* the patch
    reaches ``_apply_patch`` — ``update_finding`` sizes the prospective result,
    ``_previous_values`` snapshots the before-image — so validating only inside
    ``_apply_patch`` left those paths raising AttributeError out of ``.get`` on a
    list or a string. ``{"remove": "label"}`` was worse: a bare string is
    iterable, so it was walked character by character and silently changed
    nothing, telling the caller its edit had landed.
    """
    # [23-C] RN6 NN(1): None is refused, not waved through. It used to pass here
    # and then hit ``patch.get`` inside _apply_patch as an AttributeError, except
    # in update_edge/update_finding where a stray ``patch or {}`` happened to
    # absorb it — three tools, three behaviours for one input.
    if not isinstance(patch, dict):
        raise ValueError(f"patch must be an object, got {type(patch).__name__}")

    # [23-C] RN6 NN(2): only the [5-A] keys. A typo — {"sett": {...}}, {"add": …} —
    # used to return ok:True having changed nothing, while still bumping
    # updated_at and publishing a node.update. An LLM that mistypes the key would
    # be told its edit succeeded and lose it forever; this is the same class of
    # silent no-op that {"remove": "label"} was refused for ([23-C] RN5 JJ).
    unknown_keys = sorted(k for k in patch if k not in _PATCH_KEYS)
    if unknown_keys:
        raise ValueError(
            f"unknown patch keys: {unknown_keys}. A patch takes {sorted(_PATCH_KEYS)} ([5-A])"
        )

    updates = patch.get("set")
    if updates is not None:
        if not isinstance(updates, dict):
            raise ValueError(f"patch 'set' must be an object, got {type(updates).__name__}")
        bad = sorted((repr(k) for k in updates if not isinstance(k, str)), key=str)
        if bad:
            raise ValueError(f"patch 'set' keys must be strings: {bad}")
    removals = patch.get("remove")
    if removals is not None:
        if isinstance(removals, str) or not isinstance(removals, (list, tuple)):
            raise ValueError(
                f"patch 'remove' must be a list of property names, got "
                f"{type(removals).__name__}"
            )
        bad = sorted((repr(k) for k in removals if not isinstance(k, str)), key=str)
        if bad:
            raise ValueError(f"patch 'remove' entries must be strings: {bad}")


def check_properties(properties: Any) -> None:
    """[23-B] properties must be a plain mapping carrying no reserved (``_``) key.

    The type check comes first, and it is load-bearing rather than cosmetic:
    ``dict.update`` also accepts an iterable of key/value pairs, so a caller who
    sent ``[["_citations", "forged"]]`` sailed past a keys-only check and still
    overwrote the evidence ``cite()`` had accumulated. Asking "is it a mapping"
    before "which keys" closes that door on creation and update alike.

    A non-string key is rejected rather than left to raise AttributeError out of
    ``startswith``: a surprise exception escapes per-item error handling and
    drops the rest of a batch in silence.

    Enforced in **core** on purpose ([23-B]) — import, snapshot load and adapters
    call core directly and would bypass an MCP-only guard.
    """
    if properties is None:
        return
    if not isinstance(properties, dict):
        raise ValueError(
            f"properties must be an object, got {type(properties).__name__} ([23-B])"
        )
    non_string = sorted((repr(k) for k in properties if not isinstance(k, str)), key=str)
    if non_string:
        raise ValueError(f"properties keys must be strings: {non_string} ([23-B])")
    reserved = sorted(k for k in properties if is_reserved_property(k))
    if reserved:
        raise ValueError(
            f"properties keys starting with '{RESERVED_PROPERTY_PREFIX}' are reserved: "
            f"{reserved} ([23-B])"
        )


_NODE_SERVER_MANAGED = frozenset({"id", "created_at", "updated_at", "created_by"})
_EDGE_SERVER_MANAGED = frozenset(
    {"source", "target", "relation", "key", "created_at", "created_by"}
)
# _superseded/_provenance are listed even though the reserved-prefix rule in
# _apply_patch already refuses them. Node/Edge keep their history inside
# `properties`, where [23-B]'s write protection covers it; Finding has no
# properties map ([23-B] on purpose), so its history is a field — and a field is
# reachable by a plain `set` patch. Without this, update_finding(patch={"set":
# {"_superseded": [...]}}) would let AI-supplied text pose as server-recorded
# history: the _citations forgery [23-B] blocks on nodes, arriving at findings
# through a different door.
_FINDING_SERVER_MANAGED = frozenset(
    {
        "finding_id",
        "created_at",
        "updated_at",
        "created_by",
        SUPERSEDED_PROPERTY,
        PROVENANCE_PROPERTY,
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Node:
    """[4-A]. created_at / updated_at are server-managed."""

    id: str
    label: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    style_hint: dict[str, Any] | None = None
    position_hint: dict[str, float] | None = None
    layer: str | None = None
    ttl: int = 0
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    created_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Edge:
    """[4-B]. Identity is the (source, target, relation, key) 4-tuple."""

    source: str
    target: str
    relation: str
    key: str = ""
    directed: bool = True
    properties: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    layer: str | None = None
    style_hint: dict[str, Any] | None = None
    ttl: int = 0
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    created_by: str | None = None

    @property
    def identity(self) -> EdgeKey:
        return (self.source, self.target, self.relation, self.key)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    """[23-B] gold nugget.

    A first-class collection alongside nodes/edges — never pushed into the graph
    as a type="finding" node, so filter/layout/degree stay uncontaminated. The
    node_ids links are weak: they anchor the finding for rendering without
    touching graph topology.
    """

    finding_id: str
    title: str
    body: str = ""
    node_ids: list[str] = field(default_factory=list)
    confidence: float = 0.8
    evidence: list[str] = field(default_factory=list)
    layer: str | None = None
    tags: list[str] = field(default_factory=list)
    created_by: str | None = None
    created_at: str = ""
    updated_at: str = ""
    # [24-C] 이력/변경로그. Fields rather than reserved `properties` keys because
    # a Finding has no properties map — [23-B] withheld one deliberately, and
    # adding one would open an arbitrary-value entry point no size invariant covers.
    # The `_` prefix keeps the [23-B] meaning: system-owned, not caller-writable
    # (enforced by _FINDING_SERVER_MANAGED and _apply_patch's reserved check).
    _superseded: list[dict[str, Any]] = field(default_factory=list)
    _provenance: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check_finding_size(
    title: str,
    body: str,
    node_ids: Any,
    evidence: Any,
    tags: Any,
) -> None:
    """[23-B] 크기 불변식 — reject a pathological finding at the door.

    Bounding on the way in is what lets get_finding hand gold back whole.
    """
    for name, value, cap, expected in (
        ("title", title, MAX_FINDING_TITLE_CHARS, (str,)),
        ("body", body, MAX_FINDING_BODY_CHARS, (str,)),
        ("node_ids", node_ids, MAX_FINDING_NODE_IDS, (list, tuple)),
        ("evidence", evidence, MAX_FINDING_EVIDENCE, (list, tuple)),
        ("tags", tags, MAX_FINDING_TAGS, (list, tuple)),
    ):
        # [13-B] CH1(2): type before len(). A dict title has len 1 and sailed
        # under the cap; an int node_ids raised TypeError, which never became a
        # ToolError because only ValueError is translated.
        if not isinstance(value, expected):
            raise ValueError(
                f"finding {name} must be {' or '.join(e.__name__ for e in expected)},"
                f" got {type(value).__name__} ([23-B])"
            )
        if len(value) > cap:
            raise ValueError(
                f"finding {name} exceeds the limit: {len(value)} > {cap}"
            )


_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "str | None": (str, type(None)),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list[str]": (list, tuple),
    "list[dict[str, Any]]": (list, tuple),
    "dict[str, Any]": (dict,),
    "dict[str, Any] | None": (dict, type(None)),
    "dict[str, float] | None": (dict, type(None)),
}
"""[5-A]/[23-B] RN7 SS — declared field type → what a patch may set it to.

Keyed by the annotation string because ``from __future__ import annotations``
makes every field's ``.type`` a string. Deliberately explicit rather than
inferred: an annotation this table does not know about raises instead of being
waved through, so a field added later cannot become a silent hole (that is what
``test_every_declared_field_type_is_covered`` pins down).
"""


def _accepts(annotation: str, value: Any) -> bool:
    """True if ``value`` is assignable to a field declared as ``annotation``."""
    allowed = _FIELD_TYPES[annotation]
    # bool is a subclass of int, so an int field would silently accept True.
    # A flag arriving where a count belongs is a caller mistake, not a coercion.
    if bool not in allowed and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


def check_new_record(cls: type, values: dict[str, Any]) -> None:
    """[13-B] CH1(1) — the *creation* path gets the same value contract as update.

    RN7 closed the update door with ``check_field_types`` but left this one open,
    on the assumption that the MCP signature (``type: str``) already типed it.
    That holds for ``push_node``/``push_edge`` only: ``push_batch`` takes raw
    dicts (its bounds model allows extras and checks ttl alone) and
    ``import_graph``/``import_from_file`` hand arbitrary JSON straight to
    ``add_*``. So the same ``{"type": {}}`` that update refuses walked in through
    import — and the asymmetry was visible to a caller as "the first push is
    accepted, the second (a merge) is refused".

    [23-B] put the reserved-key and size rules in core for exactly this reason:
    guarding MCP alone leaves import, snapshot load and adapters outside. The
    value contract had not followed them down yet.
    """
    declared = {f.name: f.type for f in fields(cls)}
    for name, value in values.items():
        annotation = declared.get(name)
        if annotation is None:
            continue
        if annotation not in _FIELD_TYPES:
            raise ValueError(
                f"field {name!r} has an unmapped declared type {annotation!r};"
                " extend _FIELD_TYPES ([23-C] RN7 SS)"
            )
        if not _accepts(annotation, value):
            raise ValueError(
                f"field {name!r} expects {annotation}, got {type(value).__name__}"
                " ([5-A])"
            )


def check_field_types(target: Node | Edge | Finding, updates: dict[str, Any]) -> None:
    """[23-C] RN7 SS — a patch may not put the wrong *type* into a known field.

    ``validate_patch`` checked which fields could be written, never what could go
    into them, so the rejection happened later — inside or after ``_apply_patch``
    — and by then the damage was done. ``{"set": {"type": {}}}`` passed every
    check, ``setattr`` applied it, and the index then raised
    ``TypeError: unhashable type: 'dict'``: the caller saw a failure while the
    node kept ``type == {}``, vanished from every ``by_type`` bucket, and could
    no longer be updated, deleted, undone or snapshotted — the store's own
    ``save_snapshot`` began failing with a binding error, which (with the
    unguarded auto-snapshot loop) stopped the safety net entirely.

    Fields that never reach an index were worse, not better: ``label``/``layer``
    took a dict *successfully* and published a node.update, and only the next
    snapshot failed. The user saw "edit succeeded" and lost every later
    auto-snapshot.

    The creation path was already type-checked by the MCP signature
    (``type: str``); the update path took ``patch: dict[str, Any]`` and checked
    nothing — exactly the "guard one door, leave the other open" shape [23-B]
    warns about.
    """
    declared = {f.name: f.type for f in fields(target)}
    for name, value in updates.items():
        annotation = declared.get(name)
        if annotation is None:
            continue  # unknown fields are rejected by validate_patch itself
        if annotation not in _FIELD_TYPES:
            raise ValueError(
                f"field {name!r} has an unmapped declared type {annotation!r};"
                " extend _FIELD_TYPES ([23-C] RN7 SS)"
            )
        if not _accepts(annotation, value):
            raise ValueError(
                f"field {name!r} expects {annotation}, got {type(value).__name__}"
                " ([5-A])"
            )


def validate_patch(
    target: Node | Edge | Finding, patch: dict[str, Any], server_managed: frozenset[str]
) -> None:
    """[5-A] Decide whether a patch is acceptable — changing nothing either way.

    [23-C] RN6 LL. This used to live inside ``_apply_patch``, which runs *after*
    ``_record_lifecycle``. A patch that was going to be rejected therefore still
    got a ``_superseded`` entry written first: the caller was told the edit
    failed, the record kept an un-erasable reserved-key artefact ({'prev': {}}),
    no [8-C] event was published, and the undo stack gained a node.update. Twelve
    such failed calls pushed a *real* supersession out through the
    MAX_SUPERSEDED_ENTRIES FIFO — a rejected call destroying exactly the history
    [24-C] exists to keep. ``update_finding`` promised the opposite in its own
    docstring ("a rejected patch leaves the finding untouched").

    So validation is pure and happens first. The invariant the entry points
    uphold: **a rejected patch leaves no trace — not on the record, not in the
    history, not on the event bus, not in the undo stack.**
    """
    check_patch_shape(patch)
    updates = patch.get("set") or {}
    removals = patch.get("remove") or []

    # [23-B] 예약키 쓰기보호 applies to *field* names too, not only properties
    # keys. Findings carry their history in `_superseded`/`_provenance` fields
    # ([24-C]), and a field passes hasattr — so without this a caller could set
    # them directly and forge the record of what the graph used to say. Enforced
    # in core rather than at the MCP layer because import / snapshot load /
    # adapters call core directly ([5-E], [12]), same as the size invariants.
    reserved = sorted(k for k in updates if is_reserved_property(k))
    if reserved:
        raise ValueError(
            f"fields starting with '_' are system-owned and not patchable: {reserved} ([23-B])"
        )
    # ...and to keys *nested inside* a `properties` update. Checking only the
    # top level left the forgery [23-B] exists to prevent wide open through a
    # second door: {"set": {"properties": {"_citations": "forged"}}} merged
    # straight in and replaced the evidence cite() had accumulated. The server's
    # own history writes are unaffected — _record_lifecycle and cite() mutate
    # target.properties directly, and only the *published* patch carries the
    # reserved key, never this apply path.
    if "properties" in updates:
        # ★ RN4 AA: type first. isinstance(..., dict) used to be the *guard*, so a
        # pair-list slipped past it and dict.update applied it anyway.
        check_properties(updates["properties"])
    rejected = server_managed.intersection(updates)
    if rejected:
        raise ValueError(f"server-managed fields are not patchable: {sorted(rejected)}")
    unknown = {k for k in updates if not hasattr(target, k)}
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    check_field_types(target, updates)  # [23-C] RN7 SS

    if removals:
        if not hasattr(target, "properties"):
            raise ValueError(
                f"{type(target).__name__} has no properties; 'remove' is not applicable"
            )
        # Deleting is writing. [23-B]'s protection would be hollow if a caller
        # could not forge `_citations`/`_superseded` but could erase them —
        # supersession ([24-C]) exists to preserve the value that used to be
        # there, and a removable archive preserves nothing.
        reserved_removals = sorted(k for k in removals if is_reserved_property(k))
        if reserved_removals:
            raise ValueError(
                "properties keys starting with '_' are system-owned and cannot be "
                f"removed: {reserved_removals} ([23-B])"
            )


def _apply_patch(
    target: Node | Edge | Finding, patch: dict[str, Any], server_managed: frozenset[str]
) -> None:
    """[5-A] Apply an already-validated patch — ``{set: {...}, remove: [...]}``.

    ``set`` merges onto the record (a ``properties`` entry merges into the
    existing properties rather than replacing them); ``remove`` deletes property
    keys, which merge alone cannot do.

    Validation stays a separate call ([23-C] RN6 LL) so entry points can decide
    *before* they write history. It is repeated here rather than assumed, because
    adapters and import call ``_apply_patch`` directly — the cost is one more
    pass over a small dict, and the alternative is an unvalidated write path.
    """
    validate_patch(target, patch, server_managed)
    updates = patch.get("set") or {}
    removals = patch.get("remove") or []

    for name, value in updates.items():
        if name == "properties":
            target.properties.update(value)
        else:
            setattr(target, name, value)

    for key in removals:
        target.properties.pop(key, None)


def _validate_reason(reason: str | None) -> None:
    """[24] reason 검증. None = 일반 갱신 (현행 동작).

    An unknown reason raises rather than degrading to a plain update: a caller
    that meant "supersede" and typo'd would otherwise silently lose the backup,
    which is the one thing supersession promises to keep.
    """
    if reason is not None and reason not in VALID_REASONS:
        raise ValueError(
            f"unknown reason: {reason!r}. Use one of {sorted(VALID_REASONS)} or omit it ([24])"
        )


def _previous_values(target: Node | Edge | Finding, patch: dict[str, Any]) -> dict[str, Any]:
    """[24-C] prev — the current value of every field this patch will change.

    Only what the patch touches: superseding a label should archive the label,
    not a copy of the whole record. Keys that do not exist yet are skipped —
    there is no prior value to preserve. Deep-copied so that a later mutation of
    the live record cannot reach back and rewrite history.
    """
    updates = patch.get("set") or {}
    removals = patch.get("remove") or []
    prev: dict[str, Any] = {}

    for name, value in updates.items():
        if name == "properties" and hasattr(target, "properties"):
            touched = {
                key: copy.deepcopy(target.properties[key])
                for key in value
                if key in target.properties
            }
            if touched:
                prev["properties"] = touched
        elif hasattr(target, name):
            prev[name] = copy.deepcopy(getattr(target, name))

    # A removed property is also a value that used to be there.
    if removals and hasattr(target, "properties"):
        removed = {
            key: copy.deepcopy(target.properties[key])
            for key in removals
            if key in target.properties
        }
        if removed:
            prev.setdefault("properties", {}).update(removed)

    return prev


def _serialized_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _trim_by_count(archive: list[dict[str, Any]], cap: int) -> None:
    """Keep the most recent ``cap`` entries (FIFO — oldest go first)."""
    excess = len(archive) - cap
    if excess > 0:
        del archive[:excess]


def _trim_finding_archive(archive: list[dict[str, Any]]) -> None:
    """[24-C] finding 이력 — bound by serialized size, not just count.

    Count alone cannot bound a finding: one superseded body may be 16KB, so a
    count cap of 10 still permits a ~160KB finding built entirely through
    supported calls — past the response budget and straight through the [23-B]
    invariant that get_finding depends on. Oldest entries are evicted first so
    the recent history survives.

    The most recent entry is always kept, even if it alone exceeds the budget:
    superseding a max-size finding must not silently archive nothing, and one
    entry still leaves get_finding well inside its response budget.
    """
    _trim_by_count(archive, MAX_FINDING_SUPERSEDED_ENTRIES)
    while len(archive) > 1 and _serialized_bytes(archive) > MAX_FINDING_SUPERSEDED_BYTES:
        archive.pop(0)


def _history_entry(prev: dict[str, Any], by: str | None) -> dict[str, Any]:
    """[24-C] { prev: 이전 값 스냅샷, at, by }."""
    return {"prev": prev, "at": _now(), "by": by}


def _provenance_entry(action: str, by: str | None) -> dict[str, Any]:
    """[24-B] { action, at, by } — 틀린 값 자체는 안 남긴다."""
    return {"action": action, "at": _now(), "by": by}


def _undoable(label: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """[M2e] Record one undo command around a Graph mutation.

    Re-entrant ([M2e] D-3): a mutation invoked inside another — ``delete_edge``
    within a cascade delete, the placeholder ``add_node`` within ``add_edge`` —
    joins the outer command instead of opening its own, so the whole compound
    change undoes as a unit. The method body still marks *which* records it
    touches via ``self.history.touch_*`` so the before/after image is captured.
    """

    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def wrapper(self: Graph, *args: Any, **kwargs: Any) -> Any:
            with self.history.command(label):
                return method(self, *args, **kwargs)

        return wrapper

    return decorate


class Graph:
    """[4-C] container."""

    def __init__(self, name: str = "", description: str = "") -> None:
        self.metadata: dict[str, Any] = {
            "name": name,
            "description": description,
            "created_at": _now(),
        }
        self.nodes: dict[str, Node] = {}
        self.edges: dict[EdgeKey, Edge] = {}
        self.findings: dict[str, Finding] = {}
        self.layers: list[str] = []
        self.active_filter: str | None = None
        self.focus: str | None = None
        # [4-C] snapshot version — populated by the snapshot layer ([5-E], TASK 4).
        self.version: str = ""
        self.dirty: bool = False
        self._mutations: int = 0  # [13-B] CH1(3) - dirty-flag epoch, see clear_dirty
        self.events = EventBus()
        self.indices = Indices()
        # [M2e] undo/redo command history — owned by Graph Core, the single owner
        # of graph state ([8-D]). Records before/after images of each mutation.
        self.history = GraphHistory(self)

    def reload_from(self, other: Graph) -> None:
        """Replace this graph's contents with another's, in place ([5-E] 전체 그래프 교체).

        In place, not by swapping the object: serve owns exactly one Graph Core
        ([8-D]) and the WS hub subscribed to *this* instance's event bus. Rebinding
        serve.graph to a freshly loaded Graph would leave the hub — and every other
        subscriber — fanning out events from an object nobody reads any more.

        So the event bus is deliberately **not** replaced. Its seq keeps counting
        up, which [8-C] requires: a client that has seen seq N must never be sent
        a lower one, and a reload is not a reason to rewind the wire.

        Not marked dirty: the incoming state came from a snapshot and is already
        on disk, so re-saving it would only duplicate it ([23-C]).
        """
        self.metadata = dict(other.metadata)
        self.nodes = other.nodes
        self.edges = other.edges
        self.findings = other.findings
        self.layers = list(other.layers)
        self.active_filter = other.active_filter
        self.focus = other.focus
        self.version = other.version
        self.indices = other.indices
        self.dirty = False
        self._mutations = other._mutations
        # [M2e D-6] a full replace invalidates the undo/redo history — its images
        # point at records this graph no longer holds (snapshot load / replace import).
        self.history.clear()

    # --- [23-C] dirty flag ---

    def _touch(self) -> None:
        self.dirty = True
        self._mutations += 1

    @property
    def dirty_token(self) -> int:
        """[13-B] CH1(3) - opaque epoch a writer captures before a long save."""
        return self._mutations

    def clear_dirty(self, token: int | None = None) -> None:
        """Cleared when a snapshot is written ([23-C]).

        [13-B] CH1(3): ``save_snapshot`` awaits, so the graph can be mutated
        between the payload capture and the commit. Clearing unconditionally
        declared those mutations persisted when the written snapshot predates
        them, and the autosnapshotter - which only fires ``if graph.dirty`` -
        then skipped them until an unrelated later edit re-raised the flag.
        Passing the token captured before the save makes the clear conditional;
        ``None`` keeps the unconditional behaviour for callers that replace the
        whole graph.
        """
        if token is not None and token != self._mutations:
            return
        self.dirty = False

    # --- [M2e] undo / redo ---

    def undo(self) -> dict[str, Any]:
        """[M2e] Reverse the last graph mutation ([5-A] writes, clear, cite, import).

        Returns ``{ok: True, label, changed}`` or ``{ok: False,
        error: "nothing_to_undo"}``. The reversal re-publishes ordinary [8-C]
        events, so every connected client follows (D-5).
        """
        return self.history.undo()

    def redo(self) -> dict[str, Any]:
        """[M2e] Re-apply the last undone mutation. Cleared by any new mutation (D-5)."""
        return self.history.redo()

    @contextmanager
    def batch_command(self, label: str) -> Iterator[None]:
        """[M2e] Group many mutations into a single undo step (push_batch / import merge).

        The mutations inside keep publishing their own events; only the undo
        boundary is coalesced, so one undo reverses the whole batch (D-3/D-6).
        """
        with self.history.command(label):
            yield

    def _track_layer(self, layer: str | None) -> None:
        if layer and layer not in self.layers:
            self.layers.append(layer)

    # --- nodes ---

    @_undoable("node.add")
    def add_node(
        self,
        id: str,
        label: str,
        type: str,
        properties: dict[str, Any] | None = None,
        parent_id: str | None = None,
        style_hint: dict[str, Any] | None = None,
        layer: str | None = None,
        tags: list[str] | None = None,
        position_hint: dict[str, float] | None = None,
        ttl: int = 0,
        created_by: str | None = None,
    ) -> Node:
        """[5-A] push_node. Idempotent: same id re-push = update, properties merge.

        Re-pushing an auto-created placeholder resolves it ([5-A]: "merge 로 해소").
        """
        check_properties(properties)  # [23-B] core 강제 (RN3 부수건)
        check_new_record(Node, {  # [13-B] CH1(1) — same contract as update
            "id": id, "label": label, "type": type, "parent_id": parent_id,
            "style_hint": style_hint, "position_hint": position_hint,
            "layer": layer, "ttl": ttl, "tags": tags or [], "created_by": created_by,
        })
        self.history.touch_node(id)  # [M2e] before-image (absent → create, present → merge)
        existing = self.nodes.get(id)
        if existing is not None:
            return self._merge_node(
                existing,
                label=label,
                type=type,
                properties=properties,
                parent_id=parent_id,
                style_hint=style_hint,
                layer=layer,
                tags=tags,
                position_hint=position_hint,
                ttl=ttl,
            )

        now = _now()
        node = Node(
            id=id,
            label=label,
            type=type,
            properties=dict(properties or {}),
            parent_id=parent_id,
            style_hint=style_hint,
            position_hint=position_hint,
            layer=layer,
            ttl=ttl,
            tags=list(tags or []),
            created_at=now,
            updated_at=now,
            created_by=created_by,
        )
        # [13-B] CH1(1) — index first, then the dict. The dict write used to land
        # first, so an index failure left a record that was present but invisible:
        # by_type had no bucket for it, and from then on get_graph_summary,
        # list_nodes(sort_by), delete_node, save_snapshot, clear_all and undo all
        # raised — the store could not even be snapshotted again.
        #
        # Ordering rather than a try/except rollback, because the two are only
        # equivalent while this stays the create path: `del self.nodes[id]` on an
        # upsert would delete the record that was already there. Indexing first
        # makes "committed but unindexed" unreachable without depending on a
        # handler being correct, and the dict assignment that follows cannot fail.
        # The value contract above should keep this unreachable anyway; this is
        # what holds when the next type hole gets through it.
        self.indices.add_node(node)
        self.nodes[id] = node
        self._track_layer(layer)
        self._touch()
        self.events.publish(NODE_ADD, node.to_dict())
        return node

    def _merge_node(
        self,
        node: Node,
        *,
        label: str,
        type: str,
        properties: dict[str, Any] | None,
        parent_id: str | None,
        style_hint: dict[str, Any] | None,
        layer: str | None,
        tags: list[str] | None,
        position_hint: dict[str, float] | None,
        ttl: int,
    ) -> Node:
        old_type = node.type
        patch_set: dict[str, Any] = {"label": label, "type": type}
        if properties:
            patch_set["properties"] = dict(properties)
        for name, value in (
            ("parent_id", parent_id),
            ("style_hint", style_hint),
            ("position_hint", position_hint),
            ("layer", layer),
        ):
            if value is not None:
                patch_set[name] = value
        if tags:
            patch_set["tags"] = list(tags)
        if ttl:
            patch_set["ttl"] = ttl

        # An explicit push carries real data, so the placeholder marker is resolved.
        removals = (
            [PLACEHOLDER_PROPERTY]
            if node.properties.get(PLACEHOLDER_PROPERTY) is True
            else []
        )
        patch = {"set": patch_set, "remove": removals}
        # [13-B] CH1(1) — same ordering as the create path, for the same reason.
        # ``retype_node`` ran *after* the record was already patched, so a type
        # the index cannot hold left the node carrying it while ``by_type`` lost
        # the node entirely: the graph became unsummarisable, unsortable,
        # unsnapshottable and unrecoverable, and the caller was told the re-push
        # failed. The value contract cannot see this one — a str subclass with
        # ``__hash__ = None`` satisfies every declared type — so the index has to
        # accept the new type *before* the record commits to it.
        validate_patch(node, patch, _NODE_SERVER_MANAGED)  # RN6 LL, pure/pre-state
        self.indices.retype_node(node.id, old_type, type)
        _apply_patch(node, patch, _NODE_SERVER_MANAGED)
        node.updated_at = _now()
        self._track_layer(node.layer)
        self._touch()
        self.events.publish(NODE_UPDATE, {"id": node.id, "patch": patch})
        return node

    def get_node(self, id: str) -> Node | None:
        return self.nodes.get(id)

    @_undoable("node.update")
    def update_node(
        self, id: str, patch: dict[str, Any], reason: str | None = None
    ) -> Node:
        """[5-A] update_node. Raises KeyError if the node does not exist.

        ``reason`` ([24]) selects what happens to the value being replaced:

        - ``None`` — plain update, as before.
        - ``"correction"`` ([24-B]) — the old value was wrong. It is overwritten
          and *not* kept; only a ``_provenance`` note records that a correction
          happened. Keeping a value known to be false would be noise.
        - ``"supersede"`` ([24-C]) — the old value was valid but is now stale. It
          is archived to ``_superseded`` before the patch applies, because
          preserving what was once true is the point of this project.
        """
        _validate_reason(reason)
        node = self.nodes.get(id)
        if node is None:
            raise KeyError(id)
        # [23-C] RN6 LL — decide before touching anything. Everything below this
        # line writes (history before-image, lifecycle archive, the record
        # itself, the event), so a patch that is going to be refused must be
        # refused here or its refusal leaves debris.
        validate_patch(node, patch, _NODE_SERVER_MANAGED)
        # [13-B] CH1(1) — the index accepts the new type before the record commits
        # to it, exactly as in the create/merge paths. RN6 LL made validation come
        # first so a refusal leaves no debris; the index is the one refusal
        # validate_patch cannot foresee (a str subclass with __hash__ = None), so
        # it has to happen before touch_node/_record_lifecycle too.
        old_type = node.type
        new_type = patch.get("set", {}).get("type", old_type)
        self.indices.retype_node(id, old_type, new_type)
        self.history.touch_node(id)  # [M2e] before any lifecycle/patch mutates it
        published = self._record_lifecycle(node, patch, reason)
        try:
            _apply_patch(node, patch, _NODE_SERVER_MANAGED)
        except Exception:
            self.indices.retype_node(id, new_type, old_type)  # 인덱스만 앞서가지 않게
            raise
        node.updated_at = _now()
        self._track_layer(node.layer)
        self._touch()
        self.events.publish(NODE_UPDATE, {"id": id, "patch": published})
        return node

    def _record_lifecycle(
        self, target: Node | Edge, patch: dict[str, Any], reason: str | None
    ) -> dict[str, Any]:
        """[24-B]/[24-C] history for a node/edge, whose archive lives in properties.

        Returns the patch to publish. The archive is a server-side write that the
        caller's patch knows nothing about, so — exactly as cite() does for
        ``_citations`` ([5-F]) — the published patch carries the resulting array.
        Otherwise a subscriber applying the patch would never see the history and
        the inspector would show an empty 이력 until the next full resync. This
        adds no [8-C] op: it is the existing node.update/edge.update carrying one
        more reserved property.
        """
        if reason is None:
            return patch

        by = target.created_by
        extra: dict[str, Any] = {}

        if reason == REASON_SUPERSEDE:
            # Snapshot before _apply_patch runs — afterwards the old value is gone.
            previous = _previous_values(target, patch)
            if not previous:
                # [23-C] RN7 WW — nothing was going to change, so there is no
                # prior value to preserve. Appending {'prev': {}} anyway let a
                # no-op call ({} or a remove of an absent key, both ordinary LLM
                # slips) push real supersessions out through the FIFO cap —
                # [24-C]'s promise destroyed by calls that changed nothing.
                return patch
            archive = target.properties.setdefault(SUPERSEDED_PROPERTY, [])
            archive.append(_history_entry(previous, by))
            _trim_by_count(archive, MAX_SUPERSEDED_ENTRIES)
            extra[SUPERSEDED_PROPERTY] = copy.deepcopy(archive)
        else:  # REASON_CORRECTION — [24-B] 틀린 값은 버린다.
            log = target.properties.setdefault(PROVENANCE_PROPERTY, [])
            log.append(_provenance_entry(REASON_CORRECTION, by))
            _trim_by_count(log, MAX_PROVENANCE_ENTRIES)  # [23-C] RN7 XX
            extra[PROVENANCE_PROPERTY] = copy.deepcopy(log)

        merged_properties = {**((patch.get("set") or {}).get("properties") or {}), **extra}
        return {**patch, "set": {**(patch.get("set") or {}), "properties": merged_properties}}

    @_undoable("node.delete")
    def delete_node(self, id: str, cascade: bool = False) -> dict[str, Any]:
        """[5-A] delete_node.

        cascade=False with connected edges is refused with
        { ok: false, error: "has_edges", edge_count: N } — dangling edges are
        forbidden by the [4] invariant. Raises KeyError if the node is absent.
        """
        node = self.nodes.get(id)
        if node is None:
            raise KeyError(id)

        edge_keys = self.indices.edges_of(id)
        if edge_keys and not cascade:
            return {"ok": False, "error": "has_edges", "edge_count": len(edge_keys)}

        # [M2e] before the node, its cascaded edges (each delete_edge touches its
        # own), and finding detaches vanish — the whole cascade is one undo unit.
        self.history.touch_node(id)
        for edge_key in sorted(edge_keys):
            self.delete_edge(*edge_key)

        del self.nodes[id]
        self.indices.remove_node(node)
        self._touch()
        self.events.publish(NODE_DELETE, {"id": id})
        self._detach_node_from_findings(id)
        return {"ok": True}

    @_undoable("node.cite")
    def cite(self, node_id: str, source_url: str, source_title: str) -> Node:
        """[5-F] cite — attach evidence to a node.

        Appends to the node's reserved ``_citations`` array ([23-B]), creating it
        on first use. Citations accumulate: several per node is the point.
        source_url may be a file path or an address (an IDA address, say) — it
        need not be http ([5-F]). Raises KeyError if the node does not exist.
        """
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(node_id)

        self.history.touch_node(node_id)  # [M2e] before the _citations append
        citations = node.properties.setdefault(CITATIONS_PROPERTY, [])
        citations.append({"url": source_url, "title": source_title, "ts": _now()})
        _trim_by_count(citations, MAX_CITATIONS_ENTRIES)  # [13-B] CH1(4)
        node.updated_at = _now()
        self._touch()
        # Copied, so a later cite() cannot mutate an already-published payload.
        self.events.publish(
            NODE_UPDATE,
            {
                "id": node_id,
                "patch": {"set": {"properties": {CITATIONS_PROPERTY: list(citations)}}},
            },
        )
        return node

    # --- clear ([5-A]) ---

    @_undoable("clear.layer")
    def clear_layer(self, layer: str) -> dict[str, Any]:
        """[5-A] clear_layer — drop one AI/session's pushed graph.

        Cascades: an edge touching a removed node goes too, whatever layer owns
        it. Leaving it would be a dangling edge, which [4] forbids as an
        invariant — the same rule delete_node(cascade=True) follows.

        findings survive ([23-B] gold). A finding anchored to a removed node just
        loses that anchor, exactly as delete_node does.
        """
        node_ids = {i for i, node in self.nodes.items() if node.layer == layer}
        edge_keys = {k for k, edge in self.edges.items() if edge.layer == layer}
        for node_id in node_ids:
            edge_keys |= self.indices.edges_of(node_id)
        removed = self._remove(node_ids, edge_keys)
        if layer in self.layers:
            self.layers.remove(layer)
        # clear first, then its consequences: the client reads cause before effect.
        self.events.publish(CLEAR, {"layer": layer})
        self._detach_nodes_from_findings(node_ids)
        return {"ok": True, **removed}

    @_undoable("clear.all")
    def clear_all(self) -> dict[str, Any]:
        """[5-A] clear_all — 전체 삭제 (스냅샷은 유지).

        Snapshots and findings are the session's memory, not its graph: this
        wipes nodes and edges and leaves the gold standing ([23-B]).
        """
        node_ids = set(self.nodes)
        removed = self._remove(node_ids, set(self.edges))
        self.layers.clear()
        self.active_filter = None
        self.focus = None
        self.events.publish(CLEAR, {"layer": None})
        self._detach_nodes_from_findings(node_ids)
        return {"ok": True, **removed}

    def _remove(self, node_ids: set[str], edge_keys: set[EdgeKey]) -> dict[str, int]:
        """Drop nodes/edges without per-item events — `clear` covers them ([8-C]).

        One `clear` op instead of thousands of node.delete messages: [8-C] forbids
        exactly that shape of broadcast.
        """
        for edge_key in edge_keys:
            self.history.touch_edge(edge_key)  # [M2e] before-image for undo of clear
            edge = self.edges.pop(edge_key, None)
            if edge is not None:
                self.indices.remove_edge(edge_key, edge)
        for node_id in node_ids:
            self.history.touch_node(node_id)  # [M2e]
            node = self.nodes.pop(node_id, None)
            if node is not None:
                self.indices.remove_node(node)
        self._touch()
        return {"removed_nodes": len(node_ids), "removed_edges": len(edge_keys)}

    def _detach_nodes_from_findings(self, node_ids: set[str]) -> None:
        """Drop removed nodes from every finding anchoring them, in one pass."""
        if not node_ids:
            return
        for finding in self.findings.values():
            if not any(n in node_ids for n in finding.node_ids):
                continue
            self.history.touch_finding(finding.finding_id)  # [M2e] before node_ids change
            finding.node_ids = [n for n in finding.node_ids if n not in node_ids]
            finding.updated_at = _now()
            self.events.publish(
                FINDING_UPDATE,
                {
                    "finding_id": finding.finding_id,
                    "patch": {"set": {"node_ids": list(finding.node_ids)}},
                },
            )

    def _detach_node_from_findings(self, node_id: str) -> None:
        """Drop a deleted node from the findings anchoring it.

        The finding itself survives — other anchors may remain, and a finding
        with no anchors left is still the session's gold ([23-B]).
        """
        for finding in self.findings.values():
            if node_id not in finding.node_ids:
                continue
            self.history.touch_finding(finding.finding_id)  # [M2e] before node_ids change
            finding.node_ids = [n for n in finding.node_ids if n != node_id]
            finding.updated_at = _now()
            self.events.publish(
                FINDING_UPDATE,
                {
                    "finding_id": finding.finding_id,
                    "patch": {"set": {"node_ids": list(finding.node_ids)}},
                },
            )

    # --- edges ---

    @_undoable("edge.add")
    def add_edge(
        self,
        source: str,
        target: str,
        relation: str,
        key: str = "",
        properties: dict[str, Any] | None = None,
        directed: bool = True,
        weight: float = 1.0,
        layer: str | None = None,
        style_hint: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        ttl: int = 0,
        created_by: str | None = None,
    ) -> Edge:
        """[5-A] push_edge. Idempotent on the (source, target, relation, key) tuple.

        Missing endpoints get a placeholder node ([5-A]) — AI pushes edges before
        nodes as a matter of course ([3-A]).
        """
        check_properties(properties)  # [23-B] core 강제 (RN3 부수건)
        check_new_record(Edge, {  # [13-B] CH1(1) — same contract as update
            "source": source, "target": target, "relation": relation, "key": key,
            "directed": directed, "weight": weight, "layer": layer,
            "style_hint": style_hint, "ttl": ttl, "tags": tags or [],
            "created_by": created_by,
        })
        for endpoint in (source, target):
            if endpoint not in self.nodes:
                self._add_placeholder(endpoint, layer=layer, created_by=created_by)

        identity: EdgeKey = (source, target, relation, key)
        self.history.touch_edge(identity)  # [M2e] before-image (absent → create, present → merge)
        existing = self.edges.get(identity)
        if existing is not None:
            patch_set: dict[str, Any] = {"directed": directed, "weight": weight}
            if properties:
                patch_set["properties"] = dict(properties)
            for name, value in (("style_hint", style_hint), ("layer", layer)):
                if value is not None:
                    patch_set[name] = value
            if tags:
                patch_set["tags"] = list(tags)
            if ttl:
                patch_set["ttl"] = ttl
            _apply_patch(existing, {"set": patch_set}, _EDGE_SERVER_MANAGED)
            self._track_layer(existing.layer)
            self._touch()
            self.events.publish(EDGE_ADD, existing.to_dict())
            return existing

        edge = Edge(
            source=source,
            target=target,
            relation=relation,
            key=key,
            directed=directed,
            properties=dict(properties or {}),
            weight=weight,
            layer=layer,
            style_hint=style_hint,
            ttl=ttl,
            tags=list(tags or []),
            created_at=_now(),
            created_by=created_by,
        )
        # [13-B] CH1(1) — index first, same as add_node. Here the two orders are
        # provably equivalent, not merely untested: every key indices.add_edge
        # hashes (the identity tuple, source, target) is a component of the tuple
        # the dict write hashes first, so neither can fail while the other
        # succeeds. Kept in this order anyway so the two creation paths read the
        # same and a future index that keys on something outside the tuple does
        # not quietly reintroduce the create-path bug.
        self.indices.add_edge(identity, edge)
        self.edges[identity] = edge
        self._track_layer(layer)
        self._touch()
        self.events.publish(EDGE_ADD, edge.to_dict())
        return edge

    def _add_placeholder(
        self, id: str, layer: str | None, created_by: str | None
    ) -> Node:
        """[5-A]: label=id, type="unresolved", properties={"placeholder": true}."""
        return self.add_node(
            id=id,
            label=id,
            type=PLACEHOLDER_TYPE,
            properties={PLACEHOLDER_PROPERTY: True},
            layer=layer,
            created_by=created_by,
        )

    def get_edge(
        self, source: str, target: str, relation: str, key: str = ""
    ) -> Edge | None:
        return self.edges.get((source, target, relation, key))

    @_undoable("edge.update")
    def update_edge(
        self,
        source: str,
        target: str,
        relation: str,
        key: str = "",
        patch: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> Edge:
        """[5-A] update_edge; patch 규약 = update_node. reason 의미론 = [24]."""
        _validate_reason(reason)
        identity: EdgeKey = (source, target, relation, key)
        edge = self.edges.get(identity)
        if edge is None:
            raise KeyError(identity)
        # [23-C] RN6 NN(3): no `patch or {}` degradation here. update_node and
        # update_finding raise on a falsy patch; this tool advertises "patch 규약
        # = update_node", so quietly accepting [] / '' / 0 / None made that
        # advertisement false.
        validate_patch(edge, patch, _EDGE_SERVER_MANAGED)  # [23-C] RN6 LL
        self.history.touch_edge(identity)  # [M2e] before any lifecycle/patch mutates it
        published = self._record_lifecycle(edge, patch, reason)
        _apply_patch(edge, patch, _EDGE_SERVER_MANAGED)
        self._track_layer(edge.layer)
        self._touch()
        self.events.publish(
            EDGE_UPDATE,
            {
                "source": source,
                "target": target,
                "relation": relation,
                "key": key,
                "patch": published,
            },
        )
        return edge

    @_undoable("edge.delete")
    def delete_edge(
        self, source: str, target: str, relation: str, key: str = ""
    ) -> dict[str, Any]:
        """[5-A] delete_edge. Raises KeyError if the edge is absent."""
        identity: EdgeKey = (source, target, relation, key)
        edge = self.edges.get(identity)
        if edge is None:
            raise KeyError(identity)
        self.history.touch_edge(identity)  # [M2e] before removal
        del self.edges[identity]
        self.indices.remove_edge(identity, edge)
        self._touch()
        self.events.publish(
            EDGE_DELETE,
            {"source": source, "target": target, "relation": relation, "key": key},
        )
        return {"ok": True}

    # --- findings ([23-B], [5-G]) ---

    @_undoable("finding.add")
    def add_finding(
        self,
        title: str,
        body: str = "",
        node_ids: tuple[str, ...] | list[str] = (),
        confidence: float = 0.8,
        evidence: tuple[str, ...] | list[str] = (),
        layer: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
        created_by: str | None = None,
    ) -> Finding:
        """[5-G] record_finding. Not idempotent: every call is a new finding.

        Raises ValueError if a [23-B] size invariant is exceeded.
        """
        _check_finding_size(title, body, node_ids, evidence, tags)
        check_new_record(Finding, {  # [13-B] CH1(1)
            "title": title, "body": body, "confidence": confidence,
            "layer": layer, "created_by": created_by,
        })
        now = _now()
        finding = Finding(
            finding_id=str(uuid.uuid4()),
            title=title,
            body=body,
            node_ids=list(node_ids),
            confidence=confidence,
            evidence=list(evidence),
            layer=layer,
            tags=list(tags),
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self.history.touch_finding(finding.finding_id)  # [M2e] before-image is absent
        self.findings[finding.finding_id] = finding
        self._track_layer(layer)
        self._touch()
        self.events.publish(FINDING_ADD, finding.to_dict())
        return finding

    def get_finding(self, finding_id: str) -> Finding | None:
        return self.findings.get(finding_id)

    @_undoable("finding.update")
    def update_finding(
        self, finding_id: str, patch: dict[str, Any], reason: str | None = None
    ) -> Finding:
        """[5-G] update_finding; patch 규약 = [5-A]. reason 의미론 = [24].

        Raises ValueError if the patched result would breach a [23-B] size
        invariant — checked before applying, so a rejected patch leaves the
        finding untouched rather than half-written.

        A finding's history lives in the ``_superseded``/``_provenance`` fields
        rather than in properties, which it does not have ([23-B]). The archive
        is bounded by size, not only count — see _trim_finding_archive.
        """
        _validate_reason(reason)
        finding = self.findings.get(finding_id)
        if finding is None:
            raise KeyError(finding_id)
        # [23-C] RN6 NN(3): falsy patches raise here as they do in update_node.
        # [23-C] RN6 LL — the whole decision happens before any write. The size
        # invariant was already checked up-front; the rest of the validation now
        # joins it, so "a rejected patch leaves the finding untouched" (this
        # docstring's own promise) is finally true for every rejection reason.
        validate_patch(finding, patch, _FINDING_SERVER_MANAGED)
        updates = patch.get("set") or {}
        _check_finding_size(
            title=updates.get("title", finding.title),
            body=updates.get("body", finding.body),
            node_ids=updates.get("node_ids", finding.node_ids),
            evidence=updates.get("evidence", finding.evidence),
            tags=updates.get("tags", finding.tags),
        )

        self.history.touch_finding(finding_id)  # [M2e] before any lifecycle/patch mutates it
        published = patch
        if reason is not None:
            by = finding.created_by
            extra: dict[str, Any] = {}
            if reason == REASON_SUPERSEDE:
                # Before _apply_patch: afterwards the superseded value is gone.
                finding._superseded.append(
                    _history_entry(_previous_values(finding, patch), by)
                )
                _trim_finding_archive(finding._superseded)
                extra[SUPERSEDED_PROPERTY] = copy.deepcopy(finding._superseded)
            else:  # REASON_CORRECTION — [24-B] 틀린 값은 안 남긴다.
                finding._provenance.append(_provenance_entry(REASON_CORRECTION, by))
                extra[PROVENANCE_PROPERTY] = copy.deepcopy(finding._provenance)
            # Published, not applied: _apply_patch would refuse these (they are
            # server-owned, which is the point) — the wire copy exists so a
            # subscriber's finding carries the same history as ours ([8-C]).
            published = {**patch, "set": {**updates, **extra}}

        _apply_patch(finding, patch, _FINDING_SERVER_MANAGED)
        finding.updated_at = _now()
        self._track_layer(finding.layer)
        self._touch()
        self.events.publish(
            FINDING_UPDATE, {"finding_id": finding_id, "patch": published}
        )
        return finding

    @_undoable("finding.delete")
    def delete_finding(self, finding_id: str) -> dict[str, Any]:
        """[5-G] delete_finding. Raises KeyError if absent."""
        if finding_id not in self.findings:
            raise KeyError(finding_id)
        self.history.touch_finding(finding_id)  # [M2e] before removal
        del self.findings[finding_id]
        self._touch()
        self.events.publish(FINDING_DELETE, {"finding_id": finding_id})
        return {"ok": True}

    def list_findings(
        self,
        layer: str | None = None,
        min_confidence: float | None = None,
        node_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Finding], int]:
        """[5-G] list_findings → (page, total).

        ``total`` counts every match before limit/offset. Order is insertion
        order. The body-vs-title trimming of the response is the MCP layer's
        ([5-G], TASK 3); this returns whole Finding records.
        """
        matches = [
            finding
            for finding in self.findings.values()
            if (layer is None or finding.layer == layer)
            and (min_confidence is None or finding.confidence >= min_confidence)
            and (node_id is None or node_id in finding.node_ids)
        ]
        return matches[offset : offset + limit], len(matches)
