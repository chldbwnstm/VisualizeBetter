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
    -- [23-C] RN4 BB: keyed by snapshot_id alone. Snapshot ids are uuid4, so the
    -- id is globally unique; keying by (source_db, snapshot_id) made the same
    -- store seen through an 8.3 short name / junction / second candidate path
    -- count as a new source and resurrect snapshots the user had deleted ([5-E]).
    -- source_db stays as information, not identity.
    snapshot_id TEXT PRIMARY KEY,
    source_db   TEXT NOT NULL,
    copied_at   TEXT NOT NULL
);
"""

# [23-C] RN4 X — migrated auto snapshots land as manual (prune must not reach
# them) and say so in their name rather than in a new `kind` value: kind is a
# contract several tools/UI paths branch on, and widening it costs an audit of
# all of them for no gain here.
_MIGRATED_NAME_PREFIX = "migrated: "

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


def _history_column_alters(existing: set[str]) -> list[str]:
    """[23-C] RN4 DD — the one place the [24-C] ALTERs are spelled out.

    Both schema paths (sync copy-forward, async store) build their statements
    from this, so neither can drift from the other. They diverged once already
    and the copy path lost finding history in silence as a result.
    """
    return [
        f"ALTER TABLE finding ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'"
        for column in _FINDING_HISTORY_COLUMNS
        if column not in existing
    ]


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
    for statement in _history_column_alters(columns):
        connection.execute(statement)


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
    """[23-C] RN3 L~R + S~W, RN4 X/Z/BB/CC — copy snapshots legacy → target.

    Nothing is ever moved or deleted, so concurrency is SQLite's problem and it
    already solves it. The invariant is **content** ([23-C] W): committed legacy
    content is never removed by any path here. Legacy is attached read-write on
    purpose — that is what lets SQLite roll back a hot journal.

    Two policies keep the copy from being destructive by a longer route:

    * ``kind`` ([23-C] RN4 X). Copying an auto snapshot as ``auto`` drops it into
      the target's rolling-GC pool, where ``prune_auto_snapshots`` deletes it and
      the ledger then refuses to bring it back — and worse, the merged pool
      evicts the *user's own* auto snapshots, destroying data that would have
      survived had we never migrated. Migrated snapshots therefore land as
      ``manual`` (prune never touches manual, [5-E]) with a ``migrated:`` name
      prefix for provenance. Only the newest ``MAX_AUTO_SNAPSHOTS`` autos are
      taken; the rest are reported by count, never silently dropped — legacy
      keeps them, so the access path remains.
    * Granularity ([23-C] RN4 Z). One transaction per snapshot, one ledger row
      per snapshot. A single corrupt page or one NOT NULL violation used to roll
      back an entire store forever — 20 healthy snapshots held hostage by one
      bad row, re-attempted (and re-failed) on every boot. Now the bad snapshot
      is skipped and left out of the ledger so a later run can retry it, while
      its healthy neighbours arrive.

    Returns the number of snapshots actually committed ([23-C] RN4 CC).
    """
    source_key = os.path.normcase(os.path.abspath(legacy_db))
    connection = None
    try:
        connection = sqlite3.connect(target_db, timeout=30.0)
        _apply_schema(connection)
        connection.commit()

        try:
            connection.execute("ATTACH DATABASE ? AS legacy", (str(legacy_db),))
        except sqlite3.Error as exc:
            log.warning("copy-forward skipped: cannot open legacy store %s (%s)", legacy_db, exc)
            return 0

        # S(1): a store-wide structural mismatch is still all-or-nothing — if
        # legacy cannot supply a column target requires, no snapshot from it can
        # be copied correctly, so stop before touching anything.
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

        pending = _pending_snapshots(connection)
        if not pending:
            return 0

        shared_columns = {
            table: [
                c for c in _table_columns(connection, "legacy", table)
                if c in _table_columns(connection, "main", table)
            ]
            for table in _COPY_TABLES
        }

        copied = 0
        failed = 0
        for snapshot_id, kind in pending:
            if _copy_one_snapshot(connection, snapshot_id, kind, shared_columns, source_key):
                copied += 1
            else:
                failed += 1
        if copied:
            log.warning(
                "copy-forward: copied %d snapshot(s) from %s -> %s", copied, legacy_db, target_db
            )
        if failed:
            log.warning(
                "copy-forward: skipped %d unreadable snapshot(s) in %s"
                " — they stay in the legacy store and will be retried next run",
                failed, legacy_db,
            )
        return copied
    except Exception as exc:  # noqa: BLE001
        # [23-C] U — migration is a convenience; it must never be why the app
        # will not start. Nothing here deletes anything, so "failed" only ever
        # means "not yet".
        log.warning("copy-forward failed (%s); both stores left untouched", exc)
        return 0
    finally:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("DETACH DATABASE legacy")
            with contextlib.suppress(sqlite3.Error):
                connection.close()


def _pending_snapshots(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """(id, kind) of legacy snapshots not yet in the ledger, oldest-first.

    [23-C] RN4 X: manual snapshots all qualify; only the newest
    ``MAX_AUTO_SNAPSHOTS`` autos do, because that is the most the target would
    have kept anyway. Whatever is left behind is counted and logged, never
    dropped in silence — and legacy still holds it.

    [23-C] RN4 BB: the ledger is keyed by ``snapshot_id`` alone. Keying it by
    (source_db, snapshot_id) meant the same store reached through an 8.3 short
    name, a junction, or the second candidate path counted as a *new* source and
    resurrected snapshots the user had deleted — exactly the [5-E] violation the
    ledger exists to prevent, wearing a different hat. Snapshot ids are uuid4, so
    the id alone is globally unique and the alias axis simply disappears.
    """
    rows = connection.execute(
        "SELECT id, kind FROM legacy.snapshot"
        " WHERE id IS NOT NULL"
        "   AND id NOT IN (SELECT snapshot_id FROM main.copied_snapshot)"
        " ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    manual = [(i, k) for i, k in rows if k != SNAPSHOT_KIND_AUTO]
    autos = [(i, k) for i, k in rows if k == SNAPSHOT_KIND_AUTO]
    skipped = max(0, len(autos) - MAX_AUTO_SNAPSHOTS)
    if skipped:
        log.warning(
            "copy-forward: %d older auto snapshot(s) not copied (newest %d kept);"
            " they remain in the legacy store",
            skipped, MAX_AUTO_SNAPSHOTS,
        )
    return manual + autos[skipped:]


def _copy_one_snapshot(
    connection: sqlite3.Connection,
    snapshot_id: str,
    kind: str,
    shared_columns: dict[str, list[str]],
    source_key: str,
) -> bool:
    """Copy exactly one snapshot in its own transaction ([23-C] RN4 Z).

    Returns True when the snapshot is committed and recorded. On any problem the
    transaction is rolled back and no ledger row is written, so the snapshot is
    retried on a later run instead of being lost — and its neighbours are
    unaffected.
    """
    try:
        connection.execute("BEGIN")
        for table in _COPY_TABLES:
            columns = shared_columns[table]
            names = ",".join(f'"{c}"' for c in columns)
            key = "id" if table == "snapshot" else "snapshot_id"
            connection.execute(
                f'INSERT OR IGNORE INTO main."{table}" ({names})'
                f' SELECT {names} FROM legacy."{table}" WHERE "{key}" = ?',
                (snapshot_id,),
            )

        # S(3), now per snapshot: "we copied it" has to mean the rows are there.
        for table in _COPY_TABLES:
            key = "id" if table == "snapshot" else "snapshot_id"
            (want,) = connection.execute(
                f'SELECT count(*) FROM legacy."{table}" WHERE "{key}" = ?', (snapshot_id,)
            ).fetchone()
            (got,) = connection.execute(
                f'SELECT count(*) FROM main."{table}" WHERE "{key}" = ?', (snapshot_id,)
            ).fetchone()
            if want != got:
                connection.rollback()
                log.warning(
                    "copy-forward: snapshot %s skipped (%s expected %d row(s), found %d)",
                    snapshot_id, table, want, got,
                )
                return False

        if kind == SNAPSHOT_KIND_AUTO:
            # X: land as manual so rolling GC cannot delete it, and say where it
            # came from in the name.
            connection.execute(
                "UPDATE main.snapshot SET kind = ?, name = ? || name WHERE id = ?",
                (SNAPSHOT_KIND_MANUAL, _MIGRATED_NAME_PREFIX, snapshot_id),
            )
        connection.execute(
            "INSERT OR IGNORE INTO main.copied_snapshot (snapshot_id, source_db, copied_at)"
            " VALUES (?, ?, ?)",
            (snapshot_id, source_key, _now()),
        )
        connection.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — one bad snapshot must not stop the rest
        with contextlib.suppress(sqlite3.Error):
            connection.rollback()
        log.warning("copy-forward: snapshot %s skipped (%s)", snapshot_id, exc)
        return False


def _leave_breadcrumb(legacy_db: Path, target_db: Path) -> None:
    """[23-C] Q + RN4 Y — leave a signpost next to a legacy store, when useful.

    Not written when the legacy database sits in the data directory we are using
    (the normal state for anyone who came through the rename): a note in *that*
    directory would be describing the live store — the freshly copied gold, the
    ledger and serve.json are all right there.

    The wording states facts and never suggests deleting anything. Advice to
    remove a directory is exactly how RN1/RN2 destroyed data; a note that says it
    in prose is the same failure with extra steps, so no code path here proposes
    cleanup.
    """
    if _same_path(legacy_db.parent, target_db.parent):
        return
    note = legacy_db.parent / "MIGRATED-TO-VISUALIZEBETTER.txt"
    if note.exists():
        return
    try:
        note.write_text(
            "This directory holds a pre-2026-07-28 store from when the project\n"
            "was named 'mcpgraph'.\n\n"
            "Its snapshots were COPIED to:\n\n"
            f"    {target_db}\n\n"
            "The original store here is intact and unchanged. Both copies now\n"
            "exist. What happens to this directory from here is entirely your\n"
            "call. This note is only a signpost.\n",
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
            # [23-C] RN4 EE: "start-up cannot fail here" is a structural
            # guarantee, not a promise that every callee remembers to keep. One
            # unreadable candidate must not stop the next one, and nothing in
            # migration is worth refusing to start the app for.
            try:
                if _same_path(legacy_db, self.db_path):
                    continue
                if _copy_forward(legacy_db, self.db_path):
                    _leave_breadcrumb(legacy_db, self.db_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("copy-forward skipped for %s (%s)", legacy_db, exc)

    @staticmethod
    async def _ensure_schema(db: aiosqlite.Connection) -> None:
        """Idempotent DDL — every entry point runs it, so no call order matters.

        [23-C] RN4 DD: the schema is whatever ``_apply_schema`` says it is. This
        used to re-implement the DDL+ALTER sequence alongside the copy path, so
        the two could drift — which is how the copy path silently lost finding
        history once already.
        """
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(_SCHEMA)
        cursor = await db.execute('PRAGMA table_info("finding")')
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        for statement in _history_column_alters(columns):
            await db.execute(statement)
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
