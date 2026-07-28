/**
 * FilterBar — the human's entry point to the shared filter view ([5-C], [6]).
 *
 * The human types a [6] expression; on submit it goes to the server
 * (client.sendFilterSet), which evaluates the DSL and broadcasts the visible set
 * so every client — and the AI, via get_visible_nodes — sees the same thing. The
 * server is authoritative: this bar shows the last confirmed expression and any
 * error the server returned, it does not compute matches itself.
 *
 * A plain input for now; the [17]/D12 CodeMirror editor is a follow-up polish.
 * The value is AI/human text rendered as a controlled input value, so React
 * escapes it ([11]).
 */

import { useEffect, useState } from 'react'
import { useStrings } from '../i18n'
import { useGraphStore } from '../stores/graphStore'

export interface FilterBarProps {
  /** Send the expression to the server ([8-C] filter.set). */
  onApply: (expression: string) => void
}

export function FilterBar({ onApply }: FilterBarProps) {
  const t = useStrings()
  const filter = useGraphStore((s) => s.filter)
  const setFilter = useGraphStore((s) => s.setFilter)
  const [draft, setDraft] = useState(filter.expression)

  // Follow the server-confirmed expression when it changes underneath us (another
  // client applied a filter, or a resync restored one) — unless the human is
  // mid-edit, which we cannot see here, so we only sync on confirmed changes.
  useEffect(() => {
    setDraft(filter.expression)
  }, [filter.expression])

  const submit = () => {
    const expression = draft.trim()
    setFilter(expression) // local echo; the broadcast is authoritative
    onApply(expression)
  }

  return (
    <div className="flex items-center gap-2" data-testid="filter-bar">
      <input
        type="text"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') submit()
        }}
        placeholder={t.filterPlaceholder}
        aria-label="filter"
        data-testid="filter-input"
        className="w-72 rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-xs text-slate-200 placeholder:text-slate-600"
      />
      <button
        type="button"
        onClick={submit}
        data-testid="filter-apply"
        className="rounded border border-slate-700 px-2 py-0.5 text-xs text-slate-300 hover:bg-slate-800"
      >
        {t.apply}
      </button>
      {filter.error ? (
        <span className="text-[10px] text-rose-400" data-testid="filter-error">
          {filter.error}
        </span>
      ) : (
        filter.expression && (
          <span className="text-[10px] text-slate-500" data-testid="filter-active">
            {filter.visibleIds.size} matched
          </span>
        )
      )}
    </div>
  )
}
