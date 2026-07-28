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
# [23-C] RN3 L — tables copied forward, all keyed by a snapshot id.
_COPY_TABLES = ("snapshot", "node", "edge", "finding", "finding_node")

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

-- [23-C] RN3 T — which snapshots we have already copied out of which legacy
-- store. Durable on its own: pruning auto-snapshots or deleting one by hand
-- removes rows from `snapshot`, and inferring "already copied" from that table
-- would re-copy pruned snapshots every boot and resurrect deleted ones ([5-E]
-- makes a delete durable). This ledger records the copy, not the survival.
CREATE TABLE IF NOT EXISTS copied_snapshot (
    source_db   TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    copied_at   TEXT NOT NULL,
    PRIMARY KEY (source_db, snapshot_id)
);
"""

# [24-C] history columns added by ALTER — see _apply_schema.
_FINDING_HISTORY_COLUMNS = ("superseded", "provenance")


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


def _legacy_db_candidates(data_dir: Path) -> list[Path]:
    """[23-C] RN3 P — legacy stores whose contents belong in ``data_dir``.

    The old filename sitting in this very directory always counts. The old
    default *directory* counts only when this directory is the canonical new
    default ([23-C] a: the Tauri shell passes it as an explicit ``--data-dir``,
    main.rs:100-101) — an arbitrary user path still gets no magic.
    """
    target_default, legacy_default = _default_base_pair()
    candidates = [data_dir / _LEGACY_DB_FILENAME]
    if _same_path(data_dir, target_default):
        candidates.append(legacy_default / _LEGACY_DB_FILENAME)
    return [p for p in candidates if p.exists()]


def _apply_schema(connection: sqlite3.Connection) -> None:
    """[23-C] RN3 V — the whole schema, in one place both paths call.

    ``_SCHEMA`` alone is not the schema: ``CREATE TABLE IF NOT EXISTS`` leaves a
    pre-[24-C] ``finding`` table exactly as it was, so the history columns only
    exist after the ALTERs. When the copy path applied just ``_SCHEMA``, the
    shared-column intersection it computes came out narrower than the real
    schema and finding history was dropped in silence — and a later ALTER then
    filled the columns with ``'[]'``, erasing the evidence that anything was
    lost.
    """
    connection.executescript(_SCHEMA)
    columns = {row[1] for row in connection.execute('PRAGMA table_info("finding")')}
    for column in _FINDING_HISTORY_COLUMNS:
        if column not in columns:
            connection.execute(
                f"ALTER TABLE finding ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'"
            )


def _table_columns(connection: sqlite3.Connection, schema: str, table: str) -> list[str]:
    """Column names of ``schema.table`` — [] when the table is absent."""
    # PRAGMA takes no bind parameters, but both arguments are our own literals
    # ("main"/"legacy" and a name from _COPY_TABLES), never user input.
    return [row[1] for row in connection.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _required_columns(connection: sqlite3.Connection, schema: str, table: str) -> set[str]:
    """Columns a row cannot omit: NOT NULL without a default, plus primary keys."""
    required = set()
    for _cid, name, _type, notnull, default, pk in connection.execute(
        f'PRAGMA {schema}.table_info("{table}")'
    ):
        if pk or (notnull and default is None):
            required.add(name)
    return required


def _copy_forward(legacy_db: Path, target_db: Path) -> int:
    """[23-C] RN3 L/M/N/O + S/T/U/V/W — copy snapshots legacy → target.

    RN1 and RN2 both tried to *move* the store, which forced them to answer "is
    another process using this file?" first. That oracle was wrong every time it
    was examined, and each wrong answer destroyed data because the action was
    rename/unlink. RN3 removes the question: nothing is ever moved or deleted,
    so concurrency is SQLite's problem and it already solves it.

    The invariant is **content**, not bytes ([23-C] W): *committed legacy content
    is never removed by any path here*. Legacy is attached read-write on purpose
    — that is what lets SQLite roll back a hot journal, and rolling back does
    rewrite legacy's bytes. Opening read-only instead would leave every
    crash-interrupted store permanently uncopied, which is the worse failure.

    Correctness of "copied" is enforced three ways ([23-C] S), because
    ``INSERT OR IGNORE`` silently skips NOT NULL / CHECK violations too — not
    just duplicate keys. Trusting it alone let a narrower legacy schema drop
    every child row while the copy still reported success:
      1. refuse the whole copy unless legacy carries every column target
         requires;
      2. exclude already-copied snapshots with an anti-join, so OR IGNORE is
         left doing only what it is safe for (absorbing PK collisions between
         concurrent copiers);
      3. re-count every copied table against legacy before committing, and roll
         back on any mismatch.

    "Already copied" is tracked in a ledger ([23-C] T) rather than inferred from
    target's snapshot table: auto-snapshot pruning and user deletes remove rows
    from that table, so inferring would re-copy pruned snapshots on every boot
    and resurrect snapshots the user deleted ([5-E] says a delete is durable).

    Returns the number of snapshots copied; 0 on any problem, having changed
    nothing.
    """
    source_key = os.path.normcase(os.path.abspath(legacy_db))
    connection = None
    try:
        connection = sqlite3.connect(target_db, timeout=30.0)
        # Foreign keys stay OFF for the copy: rows arrive parent-first
        # (_COPY_TABLES order), and OR IGNORE does not absorb FK violations.
        _apply_schema(connection)
        connection.commit()

        try:
            connection.execute("ATTACH DATABASE ? AS legacy", (str(legacy_db),))
        except sqlite3.Error as exc:
            log.warning("copy-forward skipped: cannot open legacy store %s (%s)", legacy_db, exc)
            return 0

        # Everything below runs on SQL sets — no snapshot id ever crosses into
        # Python ([23-C] U). That removes both the 32766-placeholder ceiling and
        # the crash a NULL id used to cause, and a NULL id simply never matches
        # the anti-join, so a corrupt row is skipped instead of bricking start-up.
        pending = (
            'SELECT id FROM legacy.snapshot WHERE id NOT IN'
            ' (SELECT snapshot_id FROM main.copied_snapshot WHERE source_db = ?)'
        )
        (count,) = connection.execute(
            f"SELECT count(*) FROM ({pending})", (source_key,)
        ).fetchone()
        if not count:
            return 0

        # S(1): a legacy store missing a column target requires cannot be copied
        # correctly at all — stop before writing anything.
        for table in _COPY_TABLES:
            source_columns = set(_table_columns(connection, "legacy", table))
            if not source_columns:
                log.warning(
                    "copy-forward aborted: legacy store %s has no '%s' table", legacy_db, table
                )
                return 0
            missing = _required_columns(connection, "main", table) - source_columns
            if missing:
                log.warning(
                    "copy-forward aborted: legacy store %s lacks required %s column(s) %s"
                    " — nothing was copied and nothing was changed",
                    legacy_db, table, sorted(missing),
                )
                return 0

        log.warning(
            "copy-forward: %d snapshot(s) from %s -> %s", count, legacy_db, target_db
        )
        connection.execute("BEGIN")
        for table in _COPY_TABLES:
            shared = [
                c for c in _table_columns(connection, "legacy", table)
                if c in _table_columns(connection, "main", table)
            ]
            names = ",".join(f'"{c}"' for c in shared)
            key = "id" if table == "snapshot" else "snapshot_id"
            connection.execute(
                f'INSERT OR IGNORE INTO main."{table}" ({names})'
                f' SELECT {names} FROM legacy."{table}"'
                f' WHERE "{key}" IN ({pending})',
                (source_key,),
            )

        # S(3): "we copied it" must mean the rows are actually there.
        for table in _COPY_TABLES:
            key = "id" if table == "snapshot" else "snapshot_id"
            (want,) = connection.execute(
                f'SELECT count(*) FROM legacy."{table}" WHERE "{key}" IN ({pending})',
                (source_key,),
            ).fetchone()
            (got,) = connection.execute(
                f'SELECT count(*) FROM main."{table}" WHERE "{key}" IN ({pending})',
                (source_key,),
            ).fetchone()
            if want != got:
                connection.rollback()
                log.warning(
                    "copy-forward rolled back: %s expected %d row(s), found %d"
                    " — legacy %s is untouched and will be retried",
                    table, want, got, legacy_db,
                )
                return 0

        connection.execute(
            "INSERT OR IGNORE INTO main.copied_snapshot (source_db, snapshot_id, copied_at)"
            f" SELECT ?, id, ? FROM ({pending})",
            (source_key, _now(), source_key),
        )
        connection.commit()
        return count
    except Exception as exc:  # noqa: BLE001
        # [23-C] U — migration is a convenience; it must never be the reason the
        # app will not start. Nothing here deletes anything, so "failed" only
        # ever means "not yet".
        log.warning("copy-forward failed (%s); both stores left untouched", exc)
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.rollback()
        return 0
    finally:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("DETACH DATABASE legacy")
            with contextlib.suppress(sqlite3.Error):
                connection.close()


def _leave_breadcrumb(legacy_db: Path, target_db: Path) -> None:
    """[23-C] RN3 Q — tell a human where their old store was copied to.

    Additive only: a new file next to the legacy database. Nothing another
    process owns is touched (notably never ``serve.json``), and the note says
    outright that deleting it is harmless. Written only after a copy that passed
    verification, so it never claims more than happened.
    """
    note = legacy_db.parent / "MIGRATED-TO-VISUALIZEBETTER.txt"
    if note.exists():
        return
    try:
        note.write_text(
            "This directory holds a pre-2026-07-28 store from when the project\n"
            "was named 'mcpgraph'. Its snapshots were COPIED (not moved) to:\n\n"
            f"    {target_db}\n\n"
            "Nothing here was deleted. This directory is now unused; keep it as a\n"
            "backup or delete it once you have confirmed the new store looks right.\n"
            "Deleting this note is harmless — it is only a signpost.\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # best effort; a read-only legacy dir must not break start-up


def default_data_dir() -> Path:
    """[11] 전용 데이터 디렉토리.

    [23-C] RN3 P: this only names the directory. It no longer renames anything —
    the old directory keeps existing, and its contents are copied forward by
    ``SnapshotStore``.
    """
    target, _legacy = _default_base_pair()
    return target


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
        self.data_dir = _prepare_data_dir(
            Path(data_dir) if data_dir is not None else default_data_dir()
        )
        self.db_path = self.data_dir / DB_FILENAME
        self._copy_legacy_forward()

    def _copy_legacy_forward(self) -> None:
        """[23-C] RN3 — bring any pre-rename store's snapshots into this one.

        Additive by construction: every legacy database is read and left in
        place, so this cannot lose data no matter who else is running. There is
        no liveness check because there is nothing to protect against — the old
        serve may keep writing to its own file, and the next run copies what it
        added ([23-C] M).

        Deciding *whether* to copy is a snapshot-id set difference ([23-C] O),
        not an "is it empty?" judgement: a wrong answer here costs one skipped
        copy, never a deleted store, so no fail-closed handling is needed.
        """
        for legacy_db in _legacy_db_candidates(self.data_dir):
            if _same_path(legacy_db, self.db_path):
                continue
            if _copy_forward(legacy_db, self.db_path):
                _leave_breadcrumb(legacy_db, self.db_path)

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
        for column in _FINDING_HISTORY_COLUMNS:
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
