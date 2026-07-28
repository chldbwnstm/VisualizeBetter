/**
 * SuggestionBanner — the AI's filter proposal, awaiting the human ([5-D], [11]).
 *
 * suggest_filter broadcasts filter.suggest; the store holds it as a pending
 * suggestion. This shows it and lets the human decide: [적용] runs it through the
 * shared-filter path (client.sendFilterSet → server evaluates → dim, TASK V), and
 * [무시] just closes it. The AI never applies a filter directly — it only asks.
 *
 * ★ The expression and reason are AI-written, not human, so they are rendered as
 * JSX text and React escapes them ([11] XSS). No dangerouslySetInnerHTML.
 */

import { useStrings } from '../i18n'
import { useGraphStore } from '../stores/graphStore'

export interface SuggestionBannerProps {
  /** Apply the suggested expression via the shared-filter path ([5-D] → TASK V). */
  onApply: (expression: string) => void
}

export function SuggestionBanner({ onApply }: SuggestionBannerProps) {
  const t = useStrings()
  const suggestion = useGraphStore((s) => s.suggestion)
  const dismiss = useGraphStore((s) => s.dismissSuggestion)

  if (!suggestion) return null

  return (
    <div
      className="flex items-center gap-3 border-b border-sky-900 bg-sky-950/60 px-3 py-1.5 text-xs"
      data-testid="suggestion-banner"
    >
      <span className="text-sky-300">{t.aiSuggestion}</span>
      <code className="rounded bg-slate-900 px-1.5 py-0.5 text-slate-200" data-testid="suggestion-expression">
        {suggestion.expression}
      </code>
      <span className="text-slate-400" data-testid="suggestion-reason">
        {suggestion.reason}
      </span>
      <div className="ml-auto flex items-center gap-2">
        <button
          type="button"
          data-testid="suggestion-apply"
          className="rounded border border-sky-700 px-2 py-0.5 text-sky-200 hover:bg-sky-900"
          onClick={() => {
            onApply(suggestion.expression)
            dismiss()
          }}
        >
          {t.apply}
        </button>
        <button
          type="button"
          data-testid="suggestion-dismiss"
          className="rounded border border-slate-700 px-2 py-0.5 text-slate-400 hover:bg-slate-800"
          onClick={dismiss}
        >
          {t.dismiss}
        </button>
      </div>
    </div>
  )
}
