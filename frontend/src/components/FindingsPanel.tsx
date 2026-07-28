/**
 * FindingsPanel — [23-F] TASK 7, [9-B] RightSidebar.
 *
 * The gold list: title, confidence, created_by, layer colour. Clicking a finding
 * focuses its anchor and opens the detail (body, evidence).
 *
 * Every string here is untrusted ([11]) and rendered as JSX text, which React
 * escapes. Evidence URLs go through the [11] scheme allowlist.
 */

import { useMemo, useState } from 'react'
import { useStrings } from '../i18n'
import { useGraphStore } from '../stores/graphStore'
import type { Finding } from '../types'
import { ProvenanceSection, SupersededSection } from './HistorySections'
import { isClickableUrl, layerColor } from './safety'

export interface FindingsPanelProps {
  /**
   * [23-F] TASK 7: 클릭 시 앵커 노드로 focus_on + 앵커 강조.
   *
   * Hands over every anchor, not just the first: the finding points at a
   * subgraph, and the overview highlights all of it.
   */
  onFocusFinding?: (anchorIds: readonly string[]) => void
}

const CONFIDENCE_OPTIONS = [0, 0.5, 0.8]

function EvidenceLink({ url }: { url: string }) {
  if (!isClickableUrl(url)) {
    // [11]: non-http schemes stay plain text — no href, nothing to navigate.
    return <span className="break-all text-xs text-slate-400">{url}</span>
  }
  return (
    <a
      className="break-all text-xs text-sky-400 underline"
      href={url}
      target="_blank"
      rel="noreferrer noopener"
    >
      {url}
    </a>
  )
}

function FindingDetail({ finding }: { finding: Finding }) {
  const t = useStrings()
  return (
    <div className="mt-2 border-l-2 border-slate-700 pl-3" data-testid="finding-detail">
      {finding.body && (
        <p className="whitespace-pre-wrap text-xs text-slate-300" data-testid="finding-body">
          {finding.body}
        </p>
      )}
      {finding.evidence.length > 0 && (
        <div className="mt-2">
          <h4 className="text-[10px] uppercase tracking-wide text-slate-500">{t.evidence}</h4>
          <ul data-testid="finding-evidence">
            {finding.evidence.map((url, i) => (
              <li key={`${url}-${i}`}>
                <EvidenceLink url={url} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {finding.node_ids.length > 0 && (
        <p className="mt-2 text-[10px] text-slate-500" data-testid="finding-anchors">
          {t.anchorCount(finding.node_ids.length)}
        </p>
      )}
      {/* [24-C] 이전 finding 버전 — gold 의 이전 판본이 사라지지 않았음을 보여준다. */}
      {(finding._superseded?.length ?? 0) > 0 && (
        <div className="mt-2">
          <SupersededSection entries={finding._superseded ?? []} />
        </div>
      )}
      {(finding._provenance?.length ?? 0) > 0 && (
        <div className="mt-2">
          <ProvenanceSection entries={finding._provenance ?? []} />
        </div>
      )}
    </div>
  )
}

export function FindingsPanel({ onFocusFinding }: FindingsPanelProps) {
  const t = useStrings()
  const findings = useGraphStore((s) => s.findings)
  const layers = useGraphStore((s) => s.layers)
  const setFocus = useGraphStore((s) => s.setFocus)
  const [selected, setSelected] = useState<string | null>(null)
  const [minConfidence, setMinConfidence] = useState(0)

  const visible = useMemo(() => {
    return [...findings.values()]
      .filter((f) => f.confidence >= minConfidence)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
  }, [findings, minConfidence])

  function open(finding: Finding) {
    setSelected(finding.finding_id === selected ? null : finding.finding_id)
    if (finding.node_ids.length > 0) {
      setFocus(finding.node_ids[0])
      onFocusFinding?.(finding.node_ids)
    }
  }

  return (
    <section className="flex flex-col gap-2 p-3" data-testid="findings-panel">
      <header className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-amber-300">Findings</h2>
        <label className="flex items-center gap-1 text-[10px] text-slate-400">
          min confidence
          <select
            aria-label="min confidence"
            className="rounded bg-slate-800 px-1 py-0.5 text-slate-200"
            value={minConfidence}
            onChange={(e) => setMinConfidence(Number(e.target.value))}
          >
            {CONFIDENCE_OPTIONS.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
      </header>

      {visible.length === 0 ? (
        <p className="text-xs text-slate-500" data-testid="findings-empty">
          {t.noFindings}
        </p>
      ) : (
        <ul className="flex flex-col gap-1" data-testid="findings-list">
          {visible.map((finding) => {
            const color = layerColor(finding.layer, layers.get(finding.layer ?? '')?.color)
            const isOpen = selected === finding.finding_id
            return (
              <li key={finding.finding_id} data-testid="finding-item">
                <button
                  type="button"
                  onClick={() => open(finding)}
                  aria-expanded={isOpen}
                  className="w-full rounded px-2 py-1 text-left hover:bg-slate-800"
                >
                  <span className="flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      data-testid="layer-dot"
                      data-color={color ?? ''}
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={color ? { backgroundColor: color } : undefined}
                    />
                    <span className="flex-1 text-xs text-slate-100">{finding.title}</span>
                    <span className="text-[10px] text-amber-300" data-testid="finding-confidence">
                      {finding.confidence.toFixed(2)}
                    </span>
                  </span>
                  {finding.created_by && (
                    <span className="ml-4 text-[10px] text-slate-500" data-testid="finding-author">
                      {finding.created_by}
                    </span>
                  )}
                </button>
                {isOpen && <FindingDetail finding={finding} />}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
