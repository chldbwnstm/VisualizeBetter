"""Snapshot persistence — SQLite ([5-E], [23-B], [23-C], [11]).

Schema (approved for TASK 4): one DB file holding many snapshots. Typed fields
get columns; the schema-less ones ([4-A] "임의 K/V") are JSON TEXT, since this is
a snapshot store and not a query engine ([2-C]). Only ``node_ids`` is relational
— the finding_node join table ([23-B]) is what backs list_findings(node_id=X).

Citations need no table of their own: they live in node.properties["_citations"]
([23-B]) and ride along in the node's properties JSON.

Security ([5-E], [11]): a snapshot name is a DB value and never touches a path.
The DB filename is fixed, so a hostile name has no path to traverse — the defense
is structural rather than a blocklist.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from visualizebetter.graph.core import Edge, EdgeKey, Finding, Graph, Node, _now

DB_FILENAME = "visualizebetter.sqlite3"
# Pre-rename identities (the project was "mcpgraph" until 2026-07-28). Existing
# users carry a populated store under the old names; a fresh default dir would
# silently orphan every snapshot, so both are migrated once, best-effort.
# Hardened per [23-C] ★ (Fable 확정): canonical-pair trigger, adoption,
# recover-then-migrate, live-serve guard, race-safe fallbacks, warning logs.
_LEGACY_DIR_NAME = "mcpgraph"
_LEGACY_DB_FILENAME = "mcpgraph.sqlite3"
# Rollback-journal mode sidecars ([23-C] c). -wal/-shm included for safety even
# though this project does not switch to WAL: a foreign tool may have.
_DB_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
# [8-D] discovery file name. Duplicated from server.PORT_FILE_NAME on purpose:
# server.py imports this module, so importing it back would be a cycle.
_PORT_FILE_NAME = "serve.json"
_HEALTH_TIMEOUT_S = 1.0

log = logging.getLogger(__name__)

SNAPSHOT_KIND_MANUAL = "manual"
SNAPSHOT_KIND_AUTO = "auto"

MAX_AUTO_SNAPSHOTS = 20
"""[23-C] rolling GC — auto 스냅샷은 최근 20개만 유지. manual 은 영구 ([5-E])."""

DEFAULT_AUTO_INTERVAL_SECONDS = 300
"""[23-C] 주기 트리거 기본 300초 ([5-E]: 기본 5분)."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    node_count   INTEGER NOT NULL,
    edge_count   INTEGER NOT NULL,
    metadata     TEXT NOT NULL,
    layers       TEXT NOT NULL,
    version      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS node (
    snapshot_id   TEXT NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    id            TEXT NOT NULL,
    label         TEXT NOT NULL,
    "type"        TEXT NOT NULL,
    properties    TEXT NOT NULL,
    parent_id     TEXT,
    style_hint    TEXT,
    position_hint TEXT,
    layer         TEXT,
    ttl           INTEGER NOT NULL DEFAULT 0,
    tags          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    created_by    TEXT,
    PRIMARY KEY (snapshot_id, id)
);

CREATE TABLE IF NOT EXISTS edge (
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    relation    TEXT NOT NULL,
    "key"       TEXT NOT NULL DEFAULT '',
    directed    INTEGER NOT NULL DEFAULT 1,
    properties  TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 1.0,
    layer       TEXT,
    style_hint  TEXT,
    ttl         INTEGER NOT NULL DEFAULT 0,
    tags        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT,
    PRIMARY KEY (snapshot_id, source, target, relation, "key")
);

CREATE TABLE IF NOT EXISTS finding (
    snapshot_id TEXT NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
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
    -- [24-C] 이력/변경로그. Columns of their own because a finding has no
    -- properties blob to ride along in, unlike a node or an edge ([23-B]).
    superseded  TEXT NOT NULL DEFAULT '[]',
    provenance  TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (snapshot_id, finding_id)
);

CREATE TABLE IF NOT EXISTS finding_node (
    snapshot_id TEXT NOT NULL,
    finding_id  TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, finding_id, ordinal),
    FOREIGN KEY (snapshot_id, finding_id)
        REFERENCES finding(snapshot_id, finding_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_finding_node_node
    ON finding_node(snapshot_id, node_id);
"""


def _default_base_pair() -> tuple[Path, Path]:
    """[23-C] a — the canonical ``(target, legacy)`` default pair.

    The single source the migration decision keys on: an explicit ``--data-dir``
    that equals the canonical target (the Tauri shell does exactly that,
    main.rs:100-101) gets the same migration as a defaulted run, while an
    arbitrary user path never does (no magic on paths we did not name).
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "visualizebetter", Path(base) / _LEGACY_DIR_NAME
    return Path.home() / ".visualizebetter", Path.home() / f".{_LEGACY_DIR_NAME}"


def _same_path(a: Path | str, b: Path | str) -> bool:
    """Windows-safe path identity (case-insensitive, absolute)."""
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except OSError:
        return False


def _legacy_serve_alive(legacy: Path) -> bool:
    """[23-C] d — does ``legacy/serve.json`` point at a serve that answers now?

    Same verdict as stdio_proxy.is_serve_healthy (GET /graph.json → 200), but
    reimplemented on urllib because server/stdio_proxy import this module — an
    import back would be a cycle. A dead or garbled advertisement is "not alive":
    only an actually-answering old serve defers migration.
    """
    try:
        info = json.loads((legacy / _PORT_FILE_NAME).read_text(encoding="utf-8"))
        url = info["url"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    headers = {}
    if isinstance(info, dict) and info.get("token"):
        headers["Authorization"] = f"Bearer {info['token']}"
    request = urllib.request.Request(f"{url}/graph.json", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_HEALTH_TIMEOUT_S) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def default_data_dir() -> Path:
    """[11] 전용 데이터 디렉토리 (구 mcpgraph 디렉토리는 최초 1회 자동 이관)."""
    target, legacy = _default_base_pair()
    return _migrate_legacy_dir(target, legacy)


def _migrate_legacy_dir(target: Path, legacy: Path) -> Path:
    """Rename the pre-2026-07-28 data dir to the new name, once ([23-C] ★).

    - Live-serve guard (d): while an old serve still answers for ``legacy``,
      nothing moves this run — renaming the dir under it would kill its
      auto-snapshotter mid-flight and hand its serve.json to the new proxy.
      The deferred run starts on ``target``; a later run adopts the store
      (b, in SnapshotStore._locate_db) once the old serve is gone.
    - ``target`` existing is not proof of migration (the Tauri shell
      pre-creates it): the dir step simply yields, and DB-level adoption
      self-heals the split.
    - Race fallback (e): a lost rename re-checks which side still exists
      instead of assuming the legacy path survived.
    """
    if not legacy.exists():
        return target
    if _legacy_serve_alive(legacy):
        log.warning(
            "data-dir migration deferred: a live serve still answers for %s", legacy
        )
        return target
    if target.exists():
        return target
    try:
        legacy.rename(target)
    except OSError:
        fallback = legacy if legacy.exists() else target
        log.warning(
            "data-dir migration rename failed (%s -> %s); using %s",
            legacy, target, fallback,
        )
        return fallback
    log.warning("data directory migrated: %s -> %s", legacy, target)
    return target


def _recover_sqlite_sidecars(db_file: Path) -> bool:
    """[23-C] c — make ``db_file`` clean; True iff no sidecars remain.

    A hot rollback journal must be replayed under the database's original
    filename — renaming the pair and hoping is the corruption vector SQLite's
    docs warn about. Opening the database and reading once makes SQLite itself
    perform the rollback. A sidecar that survives a *successful* open is by
    definition cold (invalid header — SQLite ignored it), so it is deleted.
    Any failure leaves everything in place and reports not-clean.
    """
    sidecars = [db_file.parent / (db_file.name + s) for s in _DB_SIDECAR_SUFFIXES]
    if not any(path.exists() for path in sidecars):
        return True
    try:
        connection = sqlite3.connect(db_file, timeout=1.0)
        try:
            connection.execute("PRAGMA schema_version").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        log.warning("sqlite recovery open failed for %s; not migrating it", db_file)
        return False
    for path in sidecars:
        if path.exists():
            try:
                path.unlink()
                log.warning("removed cold sqlite sidecar %s", path)
            except OSError:
                return False
    return True


def _prepare_data_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)  # [11]
    return path


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _dump_optional(value: Any) -> str | None:
    return None if value is None else _dumps(value)


def _load_optional(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


def _snapshot_payload(graph: Graph) -> dict[str, Any]:
    """The snapshot's logical content — what ``size`` measures ([5-E])."""
    return {
        "metadata": graph.metadata,
        "layers": graph.layers,
        "version": graph.version,
        "nodes": [node.to_dict() for node in graph.nodes.values()],
        "edges": [edge.to_dict() for edge in graph.edges.values()],
        "findings": [finding.to_dict() for finding in graph.findings.values()],
    }


class SnapshotStore:
    """Snapshot persistence over one SQLite file in the data directory ([11])."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        if data_dir is None:
            base = default_data_dir()
        else:
            base = Path(data_dir)
            target, legacy = _default_base_pair()
            if _same_path(base, target):
                # [23-C] a: the Tauri shell passes the canonical new default as
                # an explicit --data-dir (and pre-creates it, main.rs:72-73) —
                # that must not bypass migration. Arbitrary paths are untouched.
                base = _migrate_legacy_dir(target, legacy)
        self.data_dir = _prepare_data_dir(base)
        self.db_path = self._locate_db()

    def _locate_db(self) -> Path:
        """[23-C] b/c/d/e/f — find, and if needed adopt, the store's DB file.

        Adoption sources, in order: the old filename in this directory (a dir
        that was itself migrated), then — only when this directory *is* the
        canonical new default — the old default directory (a store stranded by
        a pre-created target). Every rename is preceded by sidecar recovery
        (c) and guarded by the live-serve check (d).
        """
        db = self.data_dir / DB_FILENAME
        target_default, legacy_default = _default_base_pair()
        canonical = _same_path(self.data_dir, target_default)
        same_dir_old = self.data_dir / _LEGACY_DB_FILENAME

        if db.exists():
            # f: split-store detection — a new store already exists but an old
            # one is still around. We keep using the new one; say so loudly.
            if same_dir_old.exists():
                log.warning(
                    "split store detected: %s and %s both exist; using %s",
                    db, same_dir_old, db.name,
                )
            elif canonical and (legacy_default / _LEGACY_DB_FILENAME).exists():
                log.warning(
                    "split store detected: %s and %s both exist; using %s",
                    db, legacy_default / _LEGACY_DB_FILENAME, db.name,
                )
            return db

        source = same_dir_old
        if not source.exists() and canonical:
            # b: adopt across directories — target was pre-created (empty), the
            # legacy default still holds the store.
            if _legacy_serve_alive(legacy_default):
                log.warning(
                    "db adoption deferred: a live serve still answers for %s",
                    legacy_default,
                )
                return db
            cross = legacy_default / _LEGACY_DB_FILENAME
            if cross.exists():
                source = cross
        if not source.exists():
            return db

        # d: an old serve using this very directory (race fallback landed us in
        # the legacy dir) must not have its DB renamed out from under it.
        if _same_path(self.data_dir, legacy_default) and _legacy_serve_alive(
            legacy_default
        ):
            log.warning("using legacy store in place (live serve): %s", source)
            return source

        # c: recover under the original name before any rename.
        if not _recover_sqlite_sidecars(source):
            if _same_path(source.parent, self.data_dir):
                log.warning("db adoption blocked by sidecars; using %s in place", source)
                return source
            log.warning("cross-dir db adoption skipped (sidecars remain): %s", source)
            return db
        try:
            source.rename(db)
        except OSError:
            # e: re-check which side exists instead of assuming.
            if source.exists() and _same_path(source.parent, self.data_dir):
                log.warning("db adoption rename failed; using %s in place", source)
                return source
            log.warning("db adoption rename failed and source is gone; using %s", db)
            return db
        log.warning("snapshot store adopted: %s -> %s", source, db)
        return db

    @staticmethod
    async def _ensure_schema(db: aiosqlite.Connection) -> None:
        """Idempotent DDL — every entry point runs it, so no call order matters."""
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(_SCHEMA)
        await SnapshotStore._migrate_finding_history(db)

    @staticmethod
    async def _migrate_finding_history(db: aiosqlite.Connection) -> None:
        """Add [24-C]'s history columns to a `finding` table that predates them.

        CREATE TABLE IF NOT EXISTS leaves an existing table exactly as it was, so
        a database written before TASK L has no superseded/provenance columns and
        every save would fail against it. The data directory outlives any one
        version ([11]), so the upgrade has to happen here rather than by asking
        the user to delete their snapshots — which are the gold this project
        exists to keep.
        """
        cursor = await db.execute("PRAGMA table_info(finding)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        for column in ("superseded", "provenance"):
            if column not in columns:
                await db.execute(
                    f"ALTER TABLE finding ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'"
                )
        await db.commit()

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_schema(db)
            await db.commit()

    async def save_snapshot(
        self,
        graph: Graph,
        name: str,
        description: str = "",
        kind: str = SNAPSHOT_KIND_MANUAL,
    ) -> dict[str, Any]:
        """[5-E] save_snapshot → { snapshot_id, size }.

        ``name`` is stored as a value only — it never reaches the filesystem.
        Clears the dirty flag on success ([23-C]).
        """
        snapshot_id = str(uuid.uuid4())
        payload = _snapshot_payload(graph)
        size = len(_dumps(payload).encode("utf-8"))

        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_schema(db)
            await db.execute(
                "INSERT INTO snapshot (id, name, description, created_at, kind,"
                " node_count, edge_count, metadata, layers, version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    name,
                    description,
                    _now(),
                    kind,
                    len(graph.nodes),
                    len(graph.edges),
                    _dumps(graph.metadata),
                    _dumps(graph.layers),
                    graph.version,
                ),
            )
            await db.executemany(
                'INSERT INTO node (snapshot_id, id, label, "type", properties,'
                " parent_id, style_hint, position_hint, layer, ttl, tags,"
                " created_at, updated_at, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_id,
                        node.id,
                        node.label,
                        node.type,
                        _dumps(node.properties),
                        node.parent_id,
                        _dump_optional(node.style_hint),
                        _dump_optional(node.position_hint),
                        node.layer,
                        node.ttl,
                        _dumps(node.tags),
                        node.created_at,
                        node.updated_at,
                        node.created_by,
                    )
                    for node in graph.nodes.values()
                ],
            )
            await db.executemany(
                'INSERT INTO edge (snapshot_id, source, target, relation, "key",'
                " directed, properties, weight, layer, style_hint, ttl, tags,"
                " created_at, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_id,
                        edge.source,
                        edge.target,
                        edge.relation,
                        edge.key,
                        int(edge.directed),
                        _dumps(edge.properties),
                        edge.weight,
                        edge.layer,
                        _dump_optional(edge.style_hint),
                        edge.ttl,
                        _dumps(edge.tags),
                        edge.created_at,
                        edge.created_by,
                    )
                    for edge in graph.edges.values()
                ],
            )
            await db.executemany(
                "INSERT INTO finding (snapshot_id, finding_id, title, body,"
                " confidence, evidence, layer, tags, created_by, created_at,"
                " updated_at, superseded, provenance)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_id,
                        finding.finding_id,
                        finding.title,
                        finding.body,
                        finding.confidence,
                        _dumps(finding.evidence),
                        finding.layer,
                        _dumps(finding.tags),
                        finding.created_by,
                        finding.created_at,
                        finding.updated_at,
                        _dumps(finding._superseded),
                        _dumps(finding._provenance),
                    )
                    for finding in graph.findings.values()
                ],
            )
            await db.executemany(
                "INSERT INTO finding_node (snapshot_id, finding_id, node_id, ordinal)"
                " VALUES (?, ?, ?, ?)",
                [
                    (snapshot_id, finding.finding_id, node_id, ordinal)
                    for finding in graph.findings.values()
                    for ordinal, node_id in enumerate(finding.node_ids)
                ],
            )
            await db.commit()

        graph.clear_dirty()  # [23-C]
        return {"snapshot_id": snapshot_id, "size": size}

    async def load_snapshot(self, snapshot_id: str) -> Graph:
        """[5-E] load_snapshot → a fresh Graph carrying the stored state.

        Rebuilt field by field rather than replayed through add_node/add_edge:
        a restore must preserve created_at/updated_at exactly, must not publish
        mutation events, and must not mark the graph dirty. The [5-E] "전체 그래프
        교체" semantics belong to the tool/serve layer.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._ensure_schema(db)

            async with db.execute(
                "SELECT * FROM snapshot WHERE id = ?", (snapshot_id,)
            ) as cursor:
                header = await cursor.fetchone()
            if header is None:
                raise KeyError(snapshot_id)

            graph = Graph()
            graph.metadata = json.loads(header["metadata"])
            graph.layers = json.loads(header["layers"])
            graph.version = header["version"]

            async with db.execute(
                "SELECT * FROM node WHERE snapshot_id = ?", (snapshot_id,)
            ) as cursor:
                async for row in cursor:
                    node = Node(
                        id=row["id"],
                        label=row["label"],
                        type=row["type"],
                        properties=json.loads(row["properties"]),
                        parent_id=row["parent_id"],
                        style_hint=_load_optional(row["style_hint"]),
                        position_hint=_load_optional(row["position_hint"]),
                        layer=row["layer"],
                        ttl=row["ttl"],
                        tags=json.loads(row["tags"]),
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        created_by=row["created_by"],
                    )
                    graph.nodes[node.id] = node
                    graph.indices.add_node(node)

            async with db.execute(
                "SELECT * FROM edge WHERE snapshot_id = ?", (snapshot_id,)
            ) as cursor:
                async for row in cursor:
                    edge = Edge(
                        source=row["source"],
                        target=row["target"],
                        relation=row["relation"],
                        key=row["key"],
                        directed=bool(row["directed"]),
                        properties=json.loads(row["properties"]),
                        weight=row["weight"],
                        layer=row["layer"],
                        style_hint=_load_optional(row["style_hint"]),
                        ttl=row["ttl"],
                        tags=json.loads(row["tags"]),
                        created_at=row["created_at"],
                        created_by=row["created_by"],
                    )
                    identity: EdgeKey = edge.identity
                    graph.edges[identity] = edge
                    graph.indices.add_edge(identity, edge)

            anchors = await self._load_anchors(db, snapshot_id)

            async with db.execute(
                "SELECT * FROM finding WHERE snapshot_id = ?", (snapshot_id,)
            ) as cursor:
                async for row in cursor:
                    finding = Finding(
                        finding_id=row["finding_id"],
                        title=row["title"],
                        body=row["body"],
                        node_ids=anchors.get(row["finding_id"], []),
                        confidence=row["confidence"],
                        evidence=json.loads(row["evidence"]),
                        layer=row["layer"],
                        tags=json.loads(row["tags"]),
                        created_by=row["created_by"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        _superseded=json.loads(row["superseded"]),
                        _provenance=json.loads(row["provenance"]),
                    )
                    graph.findings[finding.finding_id] = finding

        return graph

    @staticmethod
    async def _load_anchors(
        db: aiosqlite.Connection, snapshot_id: str
    ) -> dict[str, list[str]]:
        """node_ids per finding, in their stored order ([23-B] node_ids is a list)."""
        anchors: dict[str, list[str]] = {}
        async with db.execute(
            "SELECT finding_id, node_id FROM finding_node WHERE snapshot_id = ?"
            " ORDER BY finding_id, ordinal",
            (snapshot_id,),
        ) as cursor:
            async for row in cursor:
                anchors.setdefault(row["finding_id"], []).append(row["node_id"])
        return anchors

    async def prune_auto_snapshots(self, keep: int = MAX_AUTO_SNAPSHOTS) -> int:
        """[23-C] rolling GC — drop all but the newest ``keep`` auto snapshots.

        manual snapshots are never touched: they live until the user deletes
        them ([5-E]). Returns how many were deleted. The node/edge/finding rows
        of a pruned snapshot go with it via ON DELETE CASCADE.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                "DELETE FROM snapshot WHERE id IN ("
                "  SELECT id FROM snapshot WHERE kind = ?"
                "  ORDER BY created_at DESC, rowid DESC"
                "  LIMIT -1 OFFSET ?"
                ")",
                (SNAPSHOT_KIND_AUTO, keep),
            )
            deleted = cursor.rowcount
            await db.commit()
        return max(deleted, 0)

    async def list_snapshots(self) -> list[dict[str, Any]]:
        """[5-E] list_snapshots — newest first."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await self._ensure_schema(db)
            async with db.execute(
                "SELECT id, name, description, created_at, node_count, edge_count,"
                " kind FROM snapshot ORDER BY created_at DESC, rowid DESC"
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]


class AutoSnapshotter:
    """[23-C] 자동 스냅샷 — gold 유실 방지.

    Two triggers, both writing kind="auto" and both pruning afterwards so the
    rolling limit holds:

    * **주기** — every ``interval_seconds`` (기본 300, [5-E] 5분), but only when
      the graph is dirty. An unchanged graph is already on disk.
    * **파괴적 작업 직전** — ``snapshot_before(reason)``, saved unconditionally:
      the point is having something to come back to.

    serve integration is not wired yet (the serve process does not exist).
    ``start()`` / ``stop()`` are the seam: serve calls start() at boot and
    awaits stop() on shutdown for a graceful cancel.

    The destructive call sites of ``snapshot_before`` ([23-C]) are likewise not
    wired, because none of them exist yet. Each belongs to the task that exposes
    it, and all four act at the tool/serve layer:

    * ``clear_layer`` ([5-A]) — pre-clear snapshot, snapshot_id in the response
    * ``clear_all`` ([5-A]) — same
    * ``load_snapshot`` ([5-E]) — the destructive part is the tool replacing the
      live graph. ``SnapshotStore.load_snapshot`` itself is a pure read that
      returns a new Graph and is *not* a call site.
    * ``import_from_file`` ([5-E]) — before a bulk 100K+ load
    """

    def __init__(
        self,
        graph: Graph,
        store: SnapshotStore,
        interval_seconds: float = DEFAULT_AUTO_INTERVAL_SECONDS,
        keep: int = MAX_AUTO_SNAPSHOTS,
    ) -> None:
        self.graph = graph
        self.store = store
        self.interval_seconds = interval_seconds
        self.keep = keep
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Begin the periodic task. Called by serve at boot."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the periodic task and wait for it to unwind."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.snapshot_if_dirty()

    async def snapshot_if_dirty(self) -> dict[str, Any] | None:
        """One periodic tick: save only when the graph changed ([23-C])."""
        if not self.graph.dirty:
            return None
        return await self._save_auto(f"auto-{_now()}")

    async def snapshot_before(self, reason: str) -> dict[str, Any]:
        """Save an auto snapshot ahead of a destructive operation ([23-C]).

        Unconditional — a clean dirty flag only means the newest snapshot is
        current, and that snapshot may itself have been pruned. Named after the
        [5-A] "pre-clear-<ts>" convention.
        """
        return await self._save_auto(f"pre-{reason}-{_now()}")

    async def _save_auto(self, name: str) -> dict[str, Any]:
        result = await self.store.save_snapshot(
            self.graph, name=name, kind=SNAPSHOT_KIND_AUTO
        )
        await self.store.prune_auto_snapshots(self.keep)
        return result
