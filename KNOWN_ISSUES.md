# Known issues

Open limitations in the current build: what each one costs you, and what to do
instead. Fixed issues are not kept here — the git history has them.

The principle this file exists for: a flaky or partial behaviour that is papered
over is worse than one that is written down.

---

## KI-2 — A graph carrying evidence or history does not survive a JSON round trip

**Severity:** high for backup and transfer workflows · **Status:** open

`export_graph(format="json")` writes the server-owned arrays that hold the parts
you most want to keep, and `import_graph` then refuses or drops them. Both
directions are broken, differently:

| Record | What export writes | What import does |
|---|---|---|
| Node | `_citations` — evidence attached by `cite` | **Rejects the whole payload**: `properties keys starting with '_' are reserved` |
| Finding | `_superseded`, `_provenance` — the correction log | Silently ignores them; the finding imports with an empty history |

So the file you exported is either un-importable, or it imports as something
smaller than what you exported — and the second case says nothing at the time.

Reproduce: `cite` any node, export to JSON, import that file into an empty graph.

**Why it is this way.** `_`-prefixed keys are server-owned so that AI-supplied
text cannot pose as evidence the server recorded — that guarantee is the whole
point of the evidence panel. Import applies the rule to our own export because
the exporter and the importer disagree about whether a reserved key arriving on
the wire is *forged* or *restored*.

**Workaround.** Use snapshots (`save_snapshot` / `load_snapshot`) for backup and
transfer; they carry citations, findings and history intact. JSON export is for
handing a graph to other tooling, not for round-tripping this one.

**Fix.** The importer has to be able to tell restoring from forging. Planned, not
implemented.

---

## KI-3 — The time scrubber shows what exists now, not what existed then

**Severity:** medium · **Status:** open, documented behaviour

The temporal scrubber filters the **current** graph by each record's
`created_at`. Two things are therefore untrue about the past it appears to show:

- **Deleted records never appear.** A node that existed on day 3 and was deleted
  on day 5 is absent from the day-3 view, because it is absent from the graph.
- **Edited records show today's values.** A node created on day 3 and relabelled
  on day 6 shows the day-6 label at every scrubber position.

It answers "when did this enter the map", which is what most questions about a
growing map actually are — it is not a reconstruction of past state.

**Workaround.** For real point-in-time state, load the snapshot from that time.
Snapshots are full copies; the scrubber is a filter.

---

## KI-4 — Legacy `NaN` values export as non-standard JSON

**Severity:** low · **Status:** open, pre-existing

`properties` written before the value gate existed may contain `NaN` or
`Infinity`. Those records still load — deliberately, with a quarantine warning,
because refusing them would mean a snapshot that opens today stops opening. But
`export_graph` then writes a bare `NaN` token, which is not standard JSON, and a
strict parser rejects the file.

New values cannot introduce this: the write gate refuses non-finite numbers, so
the affected set only shrinks.

**Why not replace them with `null`.** That silently changes data nobody asked us
to change, which is the one thing this project promises not to do. The quarantine
warning at load time names the record instead, so it can be corrected knowingly.

---

## KI-5 — Snapshot copy-forward verifies row counts, not row contents

**Severity:** low · **Status:** open, documented limitation

When a snapshot store is carried forward from an older data directory, the check
that the copy succeeded compares **row counts per table**, not the values in
them.

- **It detects** every case where rows go missing — rows an `INSERT OR IGNORE`
  skipped over a constraint violation, partial copies, whole-table failures.
- **It does not detect** a row whose count is right but whose values changed, or
  a column left out of the `SELECT` list.

The risk is small by construction: the copy is `INSERT … SELECT` over identical
column lists inside one transaction, so there is no transform to go wrong. A
checksum comparison was deferred rather than ruled out — and because the original
store is never deleted, the worst case is still recoverable from it.
