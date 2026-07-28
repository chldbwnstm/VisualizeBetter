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
import copy
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import aiosqlite

from visualizebetter.graph.core import (
    Edge,
    EdgeKey,
    Finding,
    Graph,
    Node,
    _now,
    check_restorable,
    check_storable,
)

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

# [23-C] RN5 KK(2) — two limits worth stating plainly:
#  · A migrated snapshot cannot be deleted through any surface this product
#    exposes (there is no delete_snapshot tool, and deleting the target store
#    means "copy it again"). Closing that is a [5-E] delete_snapshot, not this.
#  · A legacy *manual* snapshot is copied under its own name, so a listing cannot
#    tell it apart from a native one. Provenance lives in the ledger.
MIGRATE_AUTO_BUDGET = 5
"""[23-C] RN5 GG — how many legacy *auto* snapshots migration will ever bring
over, in total, across all runs.

Deliberately its own number rather than ``MAX_AUTO_SNAPSHOTS``: the two answer
different questions. Rolling GC bounds a live pool whose members are cheap to
lose (the next tick makes another); this bounds a one-way import whose arrivals
land as ``manual`` and, today, **cannot be deleted by any surface the product
exposes** — there is no delete_snapshot tool, no store API, no UI, no CLI. Size
makes that concrete: a snapshot serializes at roughly 212 B per (node + edge), so
at the [15] KPI scale of 100K nodes / 200K edges one is ~64 MB — 20 would be
~1.3 GB, doubled at peak because the legacy store is kept. The purpose of
migrating autos at all is "recover something close to the moment before the
rename", and the newest 5 serve that. manual snapshots are copied in full: the
user asked for those, and [5-E] says they live until deleted.

Three timings that must stay ordered ([23-C] RN5 II) —
migration deadline 10s  <  proxy LAUNCH_TIMEOUT_S 25s  <  Tauri wait_for_serve 40s.
Raising one without the others reintroduces a start-up hang.
"""

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
-- [23-C] RN5 HH — one row per migration attempt, so the story survives even
-- where stderr does not. Additive and read-only to everything else.
CREATE TABLE IF NOT EXISTS migration_run (
    ran_at    TEXT NOT NULL,
    source_db TEXT NOT NULL,
    copied    INTEGER NOT NULL DEFAULT 0,
    declined  INTEGER NOT NULL DEFAULT 0,
    failed    INTEGER NOT NULL DEFAULT 0,
    aborted   TEXT NOT NULL DEFAULT '',
    deferred  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS copied_snapshot (
    -- [23-C] RN4 BB: keyed by snapshot_id alone. Snapshot ids are uuid4, so the
    -- id is globally unique; keying by (source_db, snapshot_id) made the same
    -- store seen through an 8.3 short name / junction / second candidate path
    -- count as a new source and resurrect snapshots the user had deleted ([5-E]).
    -- source_db stays as information, not identity.
    snapshot_id TEXT PRIMARY KEY,
    source_db   TEXT NOT NULL,
    copied_at   TEXT NOT NULL,
    -- [23-C] RN5 GG: the snapshot's kind *in the legacy store*, before it was
    -- landed as manual. The auto budget is spent against this, so it survives
    -- restarts. Counting `name LIKE 'migrated: %'` instead would tie a durable
    -- accounting decision to a display string.
    origin_kind TEXT NOT NULL DEFAULT ''
);
-- [23-C] RN5 KK(3): an existing copied_snapshot carrying the older composite PK
-- is left exactly as it is. Correctness does not depend on the key — the pending
-- query carries no source predicate — and SQLite cannot ALTER a primary key, so
-- rebuilding would risk the ledger for no gain. New databases get this DDL.
"""

# [23-C] RN4 X — migrated auto snapshots land as manual (prune must not reach
# them) and say so in their name rather than in a new `kind` value.
# [23-C] RN5 KK(1): the original reason given here — "auditing every consumer
# costs more than it gains" — was an overestimate; the measured consumers are one
# docstring, zero branches, zero frontend, zero CLI. The real reason is simpler:
# [5-E] advertises `kind: manual|auto` to the AI as a contract, and prune only
# ever deletes 'auto', so a third value would buy nothing.
_MIGRATED_NAME_PREFIX = "migrated: "

# [23-C] RN5 HH — durable migration reporting (log.warning is invisible in both
# shipped forms: the proxy DEVNULLs serve's stderr, the Tauri shell drops it).
_MIGRATION_LOG_NAME = "migration.log"

# [23-C] RN5 II / RN6 MM — the copy runs before the readiness signal, so it needs
# a ceiling. sqlite3's timeout is *per statement*, so the original 30s meant a
# locked target could stall start-up for 84s (two candidates: ~168s) — past the
# proxy's 25s and the Tauri shell's 40s, and the Tauri path has no retry.
#
# ★ What the deadline actually is (RN6 MM / RN7 UU — the earlier "10s wall clock"
# claim was not what the code did, and the fix after it was still stated
# per-candidate, which is not a wall clock either). ``_COPY_DEADLINE_S`` is
# checked at stage boundaries, never preemptively: a statement already waiting on
# a lock runs to its own timeout first. Two further facts the formula has to
# carry:
#
#   · SQLite's busy handler overshoots, and the factor is not constant —
#     measured 1.0s→1.80x, 2.0s→1.60x, 4.0s→1.49x. Count *blocking statements
#     between checks*, not stages.
#   · ``_record_migration`` runs outside the deadline by design (it is HH's
#     honesty surface, so it must run even when the copy gave up). Its cost is
#     real and belongs in the total.
#   · [23-C] RN7 VV adds a floor: every boot commits at least one snapshot before
#     the budget can bite, so one snapshot's cost is always included.
#
#     worst ≈ Σ(candidates started before the deadline)
#                 [blocking statements × _COPY_LOCK_TIMEOUT_S × ~1.6]
#             + candidates × _REPORT_LOCK_TIMEOUT_S × ~1.8
#
# ★ Measured **total** (not per candidate), fully locked target:
#     one candidate  … 8.15s      two candidates … 10.04s
# Lowering the per-candidate cost lets a second candidate start, so a per-
# candidate number cannot describe start-up; only the total can. Guarding the
# unconditional DETACH behind an "did we actually ATTACH" flag ([23-C] RN7 UU)
# is what brought the two-candidate total from 16.3s down to 10.04s.
#
#     migration  <  proxy LAUNCH_TIMEOUT_S 25s  <  Tauri wait_for_serve 40s
# Raising any one of these without the others reintroduces a start-up hang.
_COPY_LOCK_TIMEOUT_S = 2.0
_COPY_DEADLINE_S = 10.0
# Reporting must not extend start-up either. If the target is locked we still get
# the file log, which is the surface a user can actually find.
_REPORT_LOCK_TIMEOUT_S = 1.0

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


def _snapshot_column_alters(existing: set[str]) -> list[str]:
    """[13-B] CH1c3 — heal a ``snapshot`` table written before ``version`` existed.

    Such a store *reads* fine (``_header_value`` supplies the documented default)
    but could not be written to: ``CREATE TABLE IF NOT EXISTS`` leaves the old
    table alone and ``save_snapshot``'s INSERT then names a column that is not
    there. Being able to open a store but not save into it is the worst of the
    two — the next auto-snapshot fails on a store that looked healthy.

    Spelled out here for the same reason as the [24-C] ALTERs: both schema paths
    build from one list, so they cannot drift (RN4 DD).
    """
    if not existing or "version" in existing:
        return []
    return ["ALTER TABLE snapshot ADD COLUMN version INTEGER NOT NULL DEFAULT 1"]


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
    # [13-B] CH1c3 — the same treatment for `snapshot.version`. A store written
    # before that column existed loads fine (see `_header_value`) but could not be
    # *written* to: `CREATE TABLE IF NOT EXISTS` leaves the old table alone and the
    # INSERT then names a column that is not there. Reading an old store while
    # being unable to save into it is the worst of both — the user's next
    # auto-snapshot fails on a store that looked healthy.
    header_columns = {row[1] for row in connection.execute('PRAGMA table_info("snapshot")')}
    for statement in _snapshot_column_alters(header_columns):
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


@dataclass
class _MigrationOutcome:
    """[23-C] RN5 HH — what one legacy candidate's copy actually did."""

    source_db: str
    copied: int = 0
    declined: int = 0
    failed: int = 0
    aborted: str = ""
    deferred: int = 0
    """Snapshots left for the next start ([23-C] RN5 II). Distinct from
    ``declined``: deferred work resumes, declined work does not.

    ``-1`` means "deferred before we could count" — the time budget ran out at a
    stage boundary ([23-C] RN6 MM) before the pending set was enumerated. Still
    resumes; we just cannot say how much yet."""


def _record_migration(data_dir: Path, target_db: Path, outcome: _MigrationOutcome) -> None:
    """[23-C] RN5 HH — make the migration story survivable outside the log.

    Everything copy-forward has to say — how many arrived, how many the budget
    declined, which snapshots were skipped, why a store was refused outright,
    whether we ran out of time — used to exist only as ``log.warning``. With no
    logging configuration that lands on stderr via lastResort, and both shipped
    forms throw stderr away: the proxy spawns serve with
    ``stderr=subprocess.DEVNULL`` and the Tauri shell drops the sidecar's output
    receiver on spawn. Only someone running ``serve`` by hand in a terminal ever
    saw it — which excludes exactly the population this migration exists for.

    So it is written twice, both additive:

    * ``<data_dir>/migration.log``, **appended**. It lives in the directory we
      own, so [23-C] RN4 Y (never write into the legacy directory) is untouched.
    * a ``migration_run`` row in the target database, for a future diagnostic
      tool or recovery UX to read.

    Reporting must never be the reason a migration fails, so every failure here
    is swallowed.
    """
    line = (
        f"{_now()}\tsource={outcome.source_db}\tcopied={outcome.copied}"
        f"\tdeclined={outcome.declined}\tfailed={outcome.failed}"
        f"\taborted={outcome.aborted or '-'}\tdeferred={int(outcome.deferred)}\n"
    )
    try:
        with open(data_dir / _MIGRATION_LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
    try:
        connection = sqlite3.connect(target_db, timeout=_REPORT_LOCK_TIMEOUT_S)
        try:
            _apply_schema(connection)
            connection.execute(
                "INSERT INTO migration_run"
                " (ran_at, source_db, copied, declined, failed, aborted, deferred)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), outcome.source_db, outcome.copied, outcome.declined,
                    outcome.failed, outcome.aborted, outcome.deferred,
                ),
            )
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error:
        pass


def _copy_forward(
    legacy_db: Path,
    target_db: Path,
    deadline: float | None = None,
    progress: list | None = None,
) -> _MigrationOutcome:
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
      prefix for provenance. Autos are limited by ``MIGRATE_AUTO_BUDGET`` — a
      cumulative budget across all runs, not ``MAX_AUTO_SNAPSHOTS`` and not a
      per-run cap; what the budget declines is reported by count and stays in the
      legacy store, so the access path remains.
    * Granularity ([23-C] RN4 Z). One transaction per snapshot, one ledger row
      per snapshot. A single corrupt page or one NOT NULL violation used to roll
      back an entire store forever — 20 healthy snapshots held hostage by one
      bad row, re-attempted (and re-failed) on every boot. Now the bad snapshot
      is skipped and left out of the ledger so a later run can retry it, while
      its healthy neighbours arrive.

    Returns a ``_MigrationOutcome``; ``copied`` is the number actually committed
    ([23-C] RN4 CC).
    """
    source_key = os.path.normcase(os.path.abspath(legacy_db))
    outcome = _MigrationOutcome(source_db=source_key)
    connection = None
    attached = False
    if progress is None:
        # [23-C] RN7 VV — a direct caller gets the same floor as a boot does;
        # without one the deadline would never bite for them at all.
        progress = [False]

    def out_of_time() -> bool:
        """[23-C] RN6 MM + RN7 VV — the time budget, with a progress floor.

        MM added checks at every stage boundary so a locked target cannot burn
        the whole start-up. VV requires the opposite guarantee: every boot must
        commit at least one snapshot, or "it completes across restarts" has no
        termination argument — boots that enumerate and defer make no progress
        forever. Both hold if the budget only bites *after* this boot has
        actually moved: the floor costs one snapshot's worth of time, which the
        worst-case formula below accounts for.
        """
        if deadline is None or time.monotonic() <= deadline:
            return False
        return progress[0]

    try:
        connection = sqlite3.connect(target_db, timeout=_COPY_LOCK_TIMEOUT_S)
        if out_of_time():
            outcome.deferred = -1
            log.warning(
                "copy-forward: connect of %s deferred — will resume on next start",
                legacy_db,
            )
            return outcome
        _apply_schema(connection)
        connection.commit()
        if out_of_time():
            outcome.deferred = -1
            log.warning(
                "copy-forward: schema of %s deferred — will resume on next start",
                legacy_db,
            )
            return outcome

        try:
            connection.execute("ATTACH DATABASE ? AS legacy", (str(legacy_db),))
            attached = True
        except sqlite3.Error as exc:
            log.warning("copy-forward skipped: cannot open legacy store %s (%s)", legacy_db, exc)
            outcome.aborted = "cannot-open-legacy"
            return outcome
        if out_of_time():
            outcome.deferred = -1
            log.warning(
                "copy-forward: attach of %s deferred — will resume on next start",
                legacy_db,
            )
            return outcome

        # S(1): a store-wide structural mismatch is still all-or-nothing — if
        # legacy cannot supply a column target requires, no snapshot from it can
        # be copied correctly, so stop before touching anything.
        for table in _COPY_TABLES:
            source_columns = set(_table_columns(connection, "legacy", table))
            if not source_columns:
                log.warning(
                    "copy-forward aborted: legacy store %s has no '%s' table", legacy_db, table
                )
                outcome.aborted = f"missing-table:{table}"
                return outcome
            missing = _required_columns(connection, "main", table) - source_columns
            if missing:
                log.warning(
                    "copy-forward aborted: legacy store %s lacks required %s column(s) %s"
                    " — nothing was copied and nothing was changed",
                    legacy_db, table, sorted(missing),
                )
                outcome.aborted = f"missing-columns:{table}:{','.join(sorted(missing))}"
                return outcome

        if out_of_time():
            outcome.deferred = -1
            log.warning(
                "copy-forward: column checks of %s deferred — will resume on next start",
                legacy_db,
            )
            return outcome

        pending, outcome.declined = _pending_snapshots(connection)
        if not pending:
            return outcome

        shared_columns = {
            table: [
                c for c in _table_columns(connection, "legacy", table)
                if c in _table_columns(connection, "main", table)
            ]
            for table in _COPY_TABLES
        }

        processed = 0
        for snapshot_id, kind in pending:
            # [23-C] RN7 VV — checked *after* the first attempt, so every boot
            # commits at least one snapshot. Checking first allowed boots that
            # enumerated the pending set and then deferred without trying
            # anything (reproduced three boots running), which leaves II's "it
            # completes across restarts" with no termination guarantee at all.
            if out_of_time():
                # [23-C] RN5 II: out of *time* for this boot — never out of the
                # work. Checked only between snapshots, so a transaction in
                # flight always finishes (Z's atomicity pairs each commit with
                # its ledger row; cutting one apart would desynchronise them).
                # A large store exceeding this budget is normal and the design
                # intends it: per-snapshot commits make partial progress durable,
                # so the next start resumes exactly here and it eventually
                # completes.
                outcome.deferred = len(pending) - processed
                log.warning(
                    "copy-forward: deferred %d snapshot(s) of %s"
                    " — will resume on next start",
                    outcome.deferred, legacy_db,
                )
                break
            processed += 1
            if _copy_one_snapshot(connection, snapshot_id, kind, shared_columns, source_key):
                outcome.copied += 1
                if progress is not None:
                    progress[0] = True   # [23-C] RN7 VV — the floor is satisfied
            else:
                outcome.failed += 1
        copied, failed = outcome.copied, outcome.failed
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
        return outcome
    except Exception as exc:  # noqa: BLE001
        # [23-C] U — migration is a convenience; it must never be why the app
        # will not start. Nothing here deletes anything, so "failed" only ever
        # means "not yet".
        log.warning("copy-forward failed (%s); both stores left untouched", exc)
        outcome.aborted = f"error:{type(exc).__name__}"
        return outcome
    finally:
        if connection is not None:
            # [23-C] RN7 UU(1) — only detach what was attached. An abort path
            # never ran ATTACH, yet the unconditional DETACH still paid a full
            # busy timeout (x1.5) against a locked target: the single largest
            # slice of the measured worst case.
            if attached:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("DETACH DATABASE legacy")
            with contextlib.suppress(sqlite3.Error):
                connection.close()


def _pending_snapshots(connection: sqlite3.Connection) -> tuple[list[tuple[str, str]], int]:
    """Snapshots to copy this run, as ``(id, kind)`` — manual first, oldest-first.

    [23-C] RN4 X / RN5 GG — **budgeted**, not capped-per-run. RN4 subtracted the
    ledger first and then capped what was left, so the autos it declined were
    never recorded and came right back as pending on the next boot: boot1 took 21
    and reported "5 not copied", boot2 took those same 5 (26 total), and an old
    app still writing autos pushed that further every restart. Three things broke
    at once — the "newest N only" policy stopped holding after the first boot,
    the reassuring "not copied" count became a false statement, and the arrivals
    land as ``manual`` (outside rolling GC) in a product with **no delete path at
    all**, so the growth had no way back.

    So the cap became a **cumulative budget**: ``MIGRATE_AUTO_BUDGET`` minus the
    autos already migrated, counted from the ledger's ``origin_kind``. Selection
    ranks what is still *pending* newest-first and takes that much. (An earlier
    draft ranked the whole legacy population instead, on the theory that ledger
    independence made the cap stable — it does not: with 3 of 5 already taken and
    only older ones pending, "newest 2 overall" resolves to two rows already in
    the ledger, the budget buys nothing and the pending ones are refused forever.
    Stability comes from the budget being durable, not from where the ranking is
    drawn. The same shape bites harder with more than one candidate, because the
    ledger is global while selection is per candidate: once candidate 1 has taken
    the newest rows, candidate 2's newest are already in the ledger and its
    intersection is empty — two overlapping stores, and half the snapshots never
    arrive.)

    [23-C] RN4 BB: the ledger is keyed by ``snapshot_id`` alone. Snapshot ids are
    uuid4, so a store reached through an 8.3 short name, a junction or the second
    candidate path cannot masquerade as a new source and resurrect what the user
    deleted ([5-E]).
    """
    already = connection.execute(
        "SELECT count(*) FROM main.copied_snapshot WHERE origin_kind = ?",
        (SNAPSHOT_KIND_AUTO,),
    ).fetchone()[0]
    budget = max(0, MIGRATE_AUTO_BUDGET - already)

    rows = connection.execute(
        "SELECT id, kind FROM legacy.snapshot"
        " WHERE id IS NOT NULL"
        "   AND id NOT IN (SELECT snapshot_id FROM main.copied_snapshot)"
        " ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    manual = [(i, k) for i, k in rows if k != SNAPSHOT_KIND_AUTO]

    # Newest-first **among what is still pending**, limited by what is left of the
    # budget. Ranking the whole legacy population instead would spend the budget
    # on snapshots already copied: with 5 budgeted, 3 already taken (the newest
    # three) and A4..A6 pending, "newest 2 overall" resolves to the two already in
    # the ledger, the intersection is empty, and 2 units of budget buy nothing
    # while A4..A6 are refused forever. Stability comes from the budget being
    # durable, not from where the ranking is drawn.
    pending_autos = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM legacy.snapshot"
            " WHERE id IS NOT NULL AND kind = ?"
            "   AND id NOT IN (SELECT snapshot_id FROM main.copied_snapshot)"
            " ORDER BY created_at DESC, rowid DESC",
            (SNAPSHOT_KIND_AUTO,),
        )
    ]
    taken = set(pending_autos[:budget])
    declined = len(pending_autos) - len(taken)
    if declined > 0:
        # ``declined`` is a *policy* verdict and permanent: the budget does not
        # reset next boot, so these are not coming later. Distinct from
        # ``deferred`` below, which only means "not this time".
        log.warning(
            "copy-forward: declined %d auto snapshot(s) — auto budget exhausted"
            " (%d of %d migrated); they remain in the legacy store",
            declined, already + len(taken), MIGRATE_AUTO_BUDGET,
        )

    autos = [(i, k) for i, k in rows if k == SNAPSHOT_KIND_AUTO and i in taken]
    return manual + autos, declined


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
        # Known limit: this compares row *counts*, so a value rewritten in place
        # or a column dropped from the projection would pass. Same-column
        # INSERT..SELECT makes that unlikely, and a checksum is deferred rather
        # than pretended (also stated in KNOWN_ISSUES.md).
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
            "INSERT OR IGNORE INTO main.copied_snapshot"
            " (snapshot_id, source_db, copied_at, origin_kind) VALUES (?, ?, ?, ?)",
            (snapshot_id, source_key, _now(), kind),
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
    """The snapshot's logical content — what ``size`` measures ([5-E]).

    ``to_dict`` is ``asdict``, i.e. a deep copy, which is what makes the capture
    safe to hold across the ``await``s in ``save_snapshot``.
    """
    return {
        # [13-B] CH1b — copied, not referenced. nodes/edges/findings go through
        # asdict and were already deep, but these two were live objects: a
        # mid-save push added a phantom layer to a snapshot taken before it, and
        # a mid-save clear_all left an orphan one. The capture has to be true for
        # all six keys or it is not a capture.
        "metadata": copy.deepcopy(graph.metadata),
        "layers": copy.deepcopy(graph.layers),
        "version": graph.version,
        "nodes": [node.to_dict() for node in graph.nodes.values()],
        "edges": [edge.to_dict() for edge in graph.edges.values()],
        "findings": [finding.to_dict() for finding in graph.findings.values()],
    }


def _sizing_payload(graph: Graph) -> dict[str, Any]:
    """[13-B] CH1(6) — the same JSON as ``_snapshot_payload``, without the copy.

    ``size`` is one integer, and computing it used to cost a full ``asdict`` deep
    copy of the graph: at 100K nodes the capture blocked for ~2.25s (deepcopy
    1.13s + dumps 0.37s + row building 0.75s) and peaked +192MB. Serialising the
    records' ``__dict__`` directly produces byte-identical JSON — verified equal
    at both KPI sizes — for 409ms and 1.6MB, taking the capture to ~1.16s.

    ★ The result **must not cross an ``await``**. These are live references, and
    ``properties`` is mutated in place (``update()``, ``setdefault().append()``),
    so anything held past a suspension point would let a concurrent edit leak
    into a snapshot that had already been measured. Use it, take its length, drop
    it — the rows come from ``_snapshot_payload``'s deep capture.
    """
    return {
        "metadata": graph.metadata,
        "layers": graph.layers,
        "version": graph.version,
        "nodes": [node.__dict__ for node in graph.nodes.values()],
        "edges": [edge.__dict__ for edge in graph.edges.values()],
        "findings": [finding.__dict__ for finding in graph.findings.values()],
    }


def _header_value(row: Any, column: str, snapshot_id: str, fallback: Any) -> Any:
    """[13-B] CH1c3 — read a snapshot header column that an older store may lack.

    A store written before a column existed raised ``IndexError: No item with that
    key`` and nothing else: no snapshot id, no column name, no idea what to do
    about it. That is the least actionable error surface in the store, and it
    lands on exactly the population copy-forward exists for.

    The value is optional by construction — ``_copy_one_snapshot`` already
    computes a shared-column intersection, which is an admission that column
    drift is real. What was missing was any way to *see* it. Missing columns get
    a documented default and a warning naming the snapshot and the column;
    anything the graph genuinely cannot do without still fails loudly.

    (The deeper gap — this store carries no schema version marker at all: no
    ``PRAGMA user_version``, no version table — is registered as a [13-B] CH3
    contract item. This is the diagnostic, not the fix.)
    """
    try:
        return row[column]
    except (IndexError, KeyError):
        log.warning(
            "snapshot %s has no %r column (store predates it); using %r."
            " The snapshot still loads; re-saving it writes the current schema.",
            snapshot_id,
            column,
            fallback,
        )
        return fallback


def _report_quarantine(
    data_dir: Path, target_db: Path, snapshot_id: str, quarantine: list[str]
) -> None:
    """[13-B] CH1c C — a quarantined load must not be a quiet one.

    The row is handed back unchanged, so the only thing standing between the user
    and a silent surprise is this report. It goes where RN5 HH already established
    migration reporting has to go — a file we own, not a ``log.warning``, because
    both shipped forms throw stderr away (the proxy spawns serve with
    ``stderr=DEVNULL``, the Tauri shell drops the sidecar's output receiver).
    """
    log.warning(
        "snapshot %s loaded with %d quarantined record(s): %s",
        snapshot_id,
        len(quarantine),
        "; ".join(quarantine[:5]),
    )
    parts = "\t".join(
        (_now(), "quarantine", f"snapshot={snapshot_id}", f"count={len(quarantine)}")
    )
    line = parts + "\t" + " | ".join(quarantine) + "\n"
    try:
        with open(data_dir / _MIGRATION_LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def _check_restored(
    cls: type, record: Any, kind: str, identity: Any, snapshot_id: str
) -> list[str]:
    """[13-B] CH1c C — the restore gate. Returns quarantine notes, refuses rarely.

    CH1b applied the live gate here, and that was wrong in a way only measurement
    showed: a snapshot written by the shipped build with a 92KB snippet, depth-40
    properties or ``tags=[1]`` opens today and would have stopped opening — the
    whole snapshot, for one row. With no snapshot editor and both CLI export and
    CLI import going through ``load_snapshot``, that data would have had no exit
    from the product at all.

    So ``check_restorable`` refuses only what makes the loaded graph unusable
    (identity fields, non-finite float fields, unencodable records, depth past
    ``MAX_STRUCTURE_DEPTH``) and reports everything else. The row loads unchanged.
    """
    values = {f.name: getattr(record, f.name) for f in fields(cls)}
    try:
        notes = check_restorable(cls, values)
    except ValueError as exc:
        raise ValueError(
            f"snapshot {snapshot_id} cannot be loaded: {kind} {identity!r} is invalid"
            f" — {exc}. Only identity and serialisability refuse here; policy"
            " limits (size, depth, element types) load with a quarantine warning"
            " instead ([13-B] CH1c)."
        ) from None
    return [f"{kind} {identity!r}: {note}" for note in notes]


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
        deadline = time.monotonic() + _COPY_DEADLINE_S
        # [23-C] RN7 VV — one shared flag for the whole boot: the time budget
        # starts biting only once *something* has been committed, so a boot can
        # never end with zero progress while work remains.
        progress = [False]
        for legacy_db in _legacy_db_candidates(self.data_dir):
            # [23-C] RN4 EE: "start-up cannot fail here" is a structural
            # guarantee, not a promise that every callee remembers to keep. One
            # unreadable candidate must not stop the next one, and nothing in
            # migration is worth refusing to start the app for.
            try:
                if _same_path(legacy_db, self.db_path):
                    continue
                if progress[0] and time.monotonic() > deadline:
                    log.warning(
                        "copy-forward: %s deferred (time budget reached before it started)",
                        legacy_db,
                    )
                    _record_migration(
                        self.data_dir, self.db_path,
                        _MigrationOutcome(
                            source_db=os.path.normcase(os.path.abspath(legacy_db)),
                            deferred=-1,  # unknown: never opened
                        ),
                    )
                    continue
                outcome = _copy_forward(legacy_db, self.db_path, deadline, progress)
                _record_migration(self.data_dir, self.db_path, outcome)
                if outcome.copied:
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
        cursor = await db.execute('PRAGMA table_info("snapshot")')
        header_columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        for statement in _snapshot_column_alters(header_columns):
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
        """[5-E] save_snapshot -> { snapshot_id, size }.

        ``name`` is stored as a value only - it never reaches the filesystem.

        [13-B] CH1(3): every row is built from **one** ``_snapshot_payload``
        capture. Previously the graph was read six separate times - once for
        ``size``, once for the counts, then once per ``executemany`` - with
        ``await`` points in between, so any mutation landing mid-save produced a
        snapshot that never existed: counts from one instant, node rows from a
        second, finding rows from a third, and ``finding_node`` rows pointing at
        nodes the node pass had not seen. ``_snapshot_payload`` goes through
        ``asdict``, so the capture is a deep copy and stays true no matter what
        happens to the live graph afterwards.

        The dirty flag is likewise cleared only if the graph has not moved since
        the capture ([23-C]) - an unconditional clear told the autosnapshotter
        that changes made *during* the save were already on disk, and they were
        silently skipped until some later edit happened to set the flag again.
        """
        snapshot_id = str(uuid.uuid4())
        token = graph.dirty_token
        # [13-B] CH1(6) — measure off live references (cheap), then take the deep
        # capture the rows are built from. Both happen before the first await, so
        # they describe the same instant; the shallow one is dropped right here.
        size = len(_dumps(_sizing_payload(graph)).encode("utf-8"))
        payload = _snapshot_payload(graph)
        nodes, edges, findings = payload["nodes"], payload["edges"], payload["findings"]

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
                    len(nodes),
                    len(edges),
                    _dumps(payload["metadata"]),
                    _dumps(payload["layers"]),
                    payload["version"],
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
                        node["id"],
                        node["label"],
                        node["type"],
                        _dumps(node["properties"]),
                        node["parent_id"],
                        _dump_optional(node["style_hint"]),
                        _dump_optional(node["position_hint"]),
                        node["layer"],
                        node["ttl"],
                        _dumps(node["tags"]),
                        node["created_at"],
                        node["updated_at"],
                        node["created_by"],
                    )
                    for node in nodes
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
                        edge["source"],
                        edge["target"],
                        edge["relation"],
                        edge["key"],
                        int(edge["directed"]),
                        _dumps(edge["properties"]),
                        edge["weight"],
                        edge["layer"],
                        _dump_optional(edge["style_hint"]),
                        edge["ttl"],
                        _dumps(edge["tags"]),
                        edge["created_at"],
                        edge["created_by"],
                    )
                    for edge in edges
                ],
            )
            # ★ Finding is the one record whose dataclass field order differs
            # from its INSERT column order (``node_ids`` is the 4th field and has
            # no column here — it lives in ``finding_node``). Keys, never
            # positions: shortening this to ``tuple(d.values())`` would shift
            # every finding column by one and still insert cleanly.
            await db.executemany(
                "INSERT INTO finding (snapshot_id, finding_id, title, body,"
                " confidence, evidence, layer, tags, created_by, created_at,"
                " updated_at, superseded, provenance)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_id,
                        finding["finding_id"],
                        finding["title"],
                        finding["body"],
                        finding["confidence"],
                        _dumps(finding["evidence"]),
                        finding["layer"],
                        _dumps(finding["tags"]),
                        finding["created_by"],
                        finding["created_at"],
                        finding["updated_at"],
                        _dumps(finding["_superseded"]),
                        _dumps(finding["_provenance"]),
                    )
                    for finding in findings
                ],
            )
            await db.executemany(
                "INSERT INTO finding_node (snapshot_id, finding_id, node_id, ordinal)"
                " VALUES (?, ?, ?, ?)",
                [
                    (snapshot_id, finding["finding_id"], node_id, ordinal)
                    for finding in findings
                    for ordinal, node_id in enumerate(finding["node_ids"])
                ],
            )
            await db.commit()

        graph.clear_dirty(token)  # [23-C] only if nothing changed mid-save
        return {"snapshot_id": snapshot_id, "size": size}

    async def load_snapshot(self, snapshot_id: str) -> Graph:
        """[5-E] load_snapshot → a fresh Graph carrying the stored state.

        Rebuilt field by field rather than replayed through add_node/add_edge:
        a restore must preserve created_at/updated_at exactly, must not publish
        mutation events, and must not mark the graph dirty. The [5-E] "전체 그래프
        교체" semantics belong to the tool/serve layer.

        [13-B] CH1(3 개정) — this is the **fourth** creation path, and it was the
        one left unguarded: rebuilding by hand skipped the value contract as well
        as add_node's ordering. That is not hypothetical. Before CH1, push_batch
        and import accepted ``type=None`` silently, and ``None`` *is* hashable, so
        such a node reached SQLite as a clean NULL rather than erroring. Loading
        that row builds ``by_type[None]``, and the next ``get_graph_summary`` — the
        [23-D] handoff's first tool — dies with "'<' not supported between
        NoneType and str". Fixing the live paths does not help: a snapshot already
        on disk re-injects the bad row.

        Fail-closed: one bad row rejects the whole load, naming the row and the
        field. Nothing auto-loads a snapshot at startup, so this cannot brick the
        app, and refusing wholesale matches [5-E]'s all-or-nothing replace — a
        half-restored graph is the state no caller can reason about.
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
            quarantine: list[str] = []
            graph.metadata = json.loads(_header_value(header, "metadata", snapshot_id, "{}"))
            graph.layers = json.loads(_header_value(header, "layers", snapshot_id, "[]"))
            graph.version = _header_value(header, "version", snapshot_id, 1)

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
                    quarantine.extend(
                        _check_restored(Node, node, "node", node.id, snapshot_id)
                    )
                    graph.indices.add_node(node)  # index first, as add_node does
                    graph.nodes[node.id] = node

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
                    quarantine.extend(
                        _check_restored(Edge, edge, "edge", identity, snapshot_id)
                    )
                    graph.indices.add_edge(identity, edge)  # index first
                    graph.edges[identity] = edge

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
                    # [13-B] CH1c E — findings were the one restored record with
                    # no gate at all; a Finding is gold, so it is the last place
                    # to skip the check.
                    quarantine.extend(
                        _check_restored(
                            Finding, finding, "finding", finding.finding_id, snapshot_id
                        )
                    )
                    graph.findings[finding.finding_id] = finding

        if quarantine:
            _report_quarantine(self.data_dir, self.db_path, snapshot_id, quarantine)
        graph.restore_quarantine = quarantine
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


IDLE_DEBOUNCE_SECONDS = 5.0
"""[13-B] CH1(5 정정) — save this long after the last mutation, once it goes quiet.

The shutdown hook only fires on an orderly shutdown, and neither shipped form has
one: Tauri kills the sidecar with ``taskkill /F /T`` (TerminateProcess — no
lifespan shutdown at all), and the proxy launches ``serve`` detached, so nobody
asks it to stop. The hook therefore protects ``serve`` + Ctrl+C and nothing else,
leaving the periodic tick's 300s as the real worst-case exposure.

An idle debounce is the only trigger that also survives a force-kill, a crash or
a power cut, because it has already written before the process dies. It cannot
fight the [15] 1000 push/s KPI either: that workload has no 5s quiet gap, so the
debounce simply never fires during it — it fires in the pauses, which is when a
snapshot is cheap and when an AI session actually ends.
"""


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
        idle_debounce_seconds: float = IDLE_DEBOUNCE_SECONDS,
    ) -> None:
        self.graph = graph
        self.store = store
        self.interval_seconds = interval_seconds
        self.keep = keep
        self.idle_debounce_seconds = idle_debounce_seconds
        self._task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Begin the periodic and idle-debounce tasks. Called by serve at boot."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run())
        self._idle_task = asyncio.create_task(self._run_idle())

    async def stop(self) -> None:
        """Cancel both tasks and wait for them to unwind."""
        tasks = [self._task, self._idle_task]
        self._task = self._idle_task = None
        for task in tasks:
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        """[23-C] TT — one failing tick must not end the safety net.

        This loop had no guard, so any save failure — a corrupt value the store
        could not bind, a full disk, a permission error, a lock — killed the task
        and silently stopped both the periodic snapshots and the
        before-destructive-operation hook. The one mechanism protecting the
        user's gold disappeared on a single exception, with nothing to say so.

        ``CancelledError`` is deliberately not caught: ``stop()`` ends this task
        by cancelling it, and swallowing that would make the loop unstoppable.
        """
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.snapshot_if_dirty()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("auto snapshot tick failed: %s", exc)

    async def _run_idle(self) -> None:
        """[13-B] CH1(5 정정) — save once the graph has been quiet for a while.

        Polls the same ``dirty`` flag and the same mutation counter the periodic
        tick uses, so the two triggers never both write the same state: whichever
        fires first clears the flag and the other finds nothing to do.

        Follows TT's guard policy exactly — a failing tick logs and continues,
        ``CancelledError`` propagates so ``stop()`` still works.
        """
        last_seen = self.graph.dirty_token
        quiet_for = 0.0
        poll = min(1.0, self.idle_debounce_seconds)
        while True:
            await asyncio.sleep(poll)
            token = self.graph.dirty_token
            if token != last_seen:
                last_seen, quiet_for = token, 0.0
                continue
            if not self.graph.dirty:
                quiet_for = 0.0
                continue
            quiet_for += poll
            if quiet_for < self.idle_debounce_seconds:
                continue
            quiet_for = 0.0
            try:
                await self.snapshot_if_dirty()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("idle auto snapshot failed: %s", exc)

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
