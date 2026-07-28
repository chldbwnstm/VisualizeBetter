/**
 * NodeInspector — [9-B] RightSidebar: PropertiesTable, CitationsSection, NeighborsList.
 *
 * [7-D] shapes how this reads data: node/edge bodies live outside React, so the
 * component subscribes to aggregates (focus, seq) and reads `graphData` at render
 * time. `seq` bumps once per flush, so an inspector for a single node re-renders
 * on graph change without putting 10K node bodies into reactive state.
 *
 * Values shown here are AI/import-supplied ([11]) and rendered as JSX text.
 */

import { useMemo } from 'react'
import { useStrings } from '../i18n'
import { graphData, useGraphStore } from '../stores/graphStore'
import type { Citation, Edge, ProvenanceEntry, SupersededEntry } from '../types'
import { ProvenanceSection, SupersededSection } from './HistorySections'
import {
  CITATIONS_PROPERTY,
  PROVENANCE_PROPERTY,
  SUPERSEDED_PROPERTY,
  isClickableUrl,
  isReservedProperty,
} from './safety'

export interface NodeInspectorProps {
  onSelectNeighbor?: (nodeId: string) => void
}

function renderValue(value: unknown): string {
  if (typeof value === 'string') return value
  return JSON.stringify(value) ?? String(value)
}

function CitationsSection({ citations }: { citations: Citation[] }) {
  const t = useStrings()
  return (
    <section data-testid="citations-section">
      <h3 className="text-[10px] uppercase tracking-wide text-slate-500">{t.citationsHeading}</h3>
      <ul className="flex flex-col gap-1">
        {citations.map((citation, i) => (
          <li key={`${citation.url}-${i}`} data-testid="citation-item">
            <div className="text-xs text-slate-200" data-testid="citation-title">
              {citation.title}
            </div>
            {isClickableUrl(citation.url) ? (
              <a
                className="break-all text-[10px] text-sky-400 underline"
                href={citation.url}
                target="_blank"
                rel="noreferrer noopener"
                data-testid="citation-url"
              >
                {citation.url}
              </a>
            ) : (
              // [11]: only http/https are clickable; the rest stay plain text.
              <span className="break-all text-[10px] text-slate-400" data-testid="citation-url">
                {citation.url}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

export function NodeInspector({ onSelectNeighbor }: NodeInspectorProps) {
  const t = useStrings()
  const focus = useGraphStore((s) => s.focus)
  // Aggregate subscription ([7-D]): re-render when a flush changed the graph.
  const seq = useGraphStore((s) => s.seq)
  const setFocus = useGraphStore((s) => s.setFocus)

  const node = useMemo(() => (focus ? graphData.getNode(focus) : undefined), [focus, seq])

  const neighbors = useMemo(() => {
    if (!focus) return [] as Array<{ id: string; edge: Edge }>
    const out: Array<{ id: string; edge: Edge }> = []
    for (const edge of graphData.edges.values()) {
      if (edge.source === focus) out.push({ id: edge.target, edge })
      else if (edge.target === focus) out.push({ id: edge.source, edge })
    }
    return out
  }, [focus, seq])

  if (!focus) {
    return (
      <section className="p-3 text-xs text-slate-500" data-testid="inspector-empty">
        {t.selectNodeHint}
      </section>
    )
  }

  if (!node) {
    return (
      <section className="p-3 text-xs text-slate-500" data-testid="inspector-missing">
        {t.nodeNotLoaded(focus)}
      </section>
    )
  }

  const citations = (node.properties[CITATIONS_PROPERTY] as Citation[] | undefined) ?? []
  // [24-C] A node's history rides in its reserved properties, unlike a finding's.
  const superseded = (node.properties[SUPERSEDED_PROPERTY] as SupersededEntry[] | undefined) ?? []
  const provenance = (node.properties[PROVENANCE_PROPERTY] as ProvenanceEntry[] | undefined) ?? []
  const visibleProperties = Object.entries(node.properties).filter(
    ([key]) => !isReservedProperty(key),
  )

  return (
    <section className="flex flex-col gap-3 p-3" data-testid="node-inspector">
      <header>
        <h2 className="text-sm font-semibold text-slate-100" data-testid="inspector-label">
          {node.label}
        </h2>
        <p className="text-[10px] text-slate-500">
          <span data-testid="inspector-type">{node.type}</span>
          {node.layer && <span data-testid="inspector-layer"> · {node.layer}</span>}
        </p>
      </header>

      <section data-testid="properties-table">
        <h3 className="text-[10px] uppercase tracking-wide text-slate-500">properties</h3>
        {visibleProperties.length === 0 ? (
          <p className="text-xs text-slate-500" data-testid="properties-empty">
            {t.none}
          </p>
        ) : (
          <table className="w-full text-left text-xs">
            <tbody>
              {visibleProperties.map(([key, value]) => (
                <tr key={key} data-testid="property-row">
                  <th className="pr-2 align-top font-normal text-slate-500">{key}</th>
                  <td className="break-all text-slate-200">{renderValue(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {citations.length > 0 && <CitationsSection citations={citations} />}

      {superseded.length > 0 && <SupersededSection entries={superseded} />}
      {provenance.length > 0 && <ProvenanceSection entries={provenance} />}

      <section data-testid="neighbors-list">
        <h3 className="text-[10px] uppercase tracking-wide text-slate-500">
          {t.neighborsHeading(neighbors.length)}
        </h3>
        <ul className="flex flex-col gap-0.5">
          {neighbors.map(({ id, edge }, i) => (
            <li key={`${id}-${edge.relation}-${edge.key}-${i}`}>
              <button
                type="button"
                data-testid="neighbor-item"
                className="text-xs text-sky-400 hover:underline"
                onClick={() => {
                  setFocus(id)
                  onSelectNeighbor?.(id)
                }}
              >
                {id}
                <span className="ml-1 text-slate-500">({edge.relation})</span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </section>
  )
}
