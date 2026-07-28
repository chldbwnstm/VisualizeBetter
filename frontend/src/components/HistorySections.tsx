/**
 * [24-D] 인스펙터의 "이력(_superseded)" · "변경로그(_provenance)" 섹션.
 *
 * Shared by the node inspector and the finding detail because the same two
 * reserved records mean the same thing in both places — only where they are
 * stored differs ([24-C]: a node keeps them in `properties`, a finding in fields
 * of its own, since [23-B] gave findings no properties map).
 *
 * Everything rendered here is AI-supplied ([11]): archived values are whatever
 * the AI once pushed. It is all rendered as JSX text, so React escapes it —
 * dangerouslySetInnerHTML is banned. That matters more here than elsewhere: the
 * archive is the one place the UI shows values the AI has already replaced, and
 * markup smuggled in before a correction would otherwise still be live.
 */

import { useStrings } from '../i18n'
import type { ProvenanceEntry, SupersededEntry } from '../types'

/** A `prev` snapshot holds only the fields the patch changed; shapes vary. */
function renderArchivedValue(value: unknown): string {
  if (typeof value === 'string') return value
  return JSON.stringify(value) ?? String(value)
}

function Timestamp({ at, by }: { at: string; by: string | null }) {
  return (
    <span className="text-[10px] text-slate-500">
      {at}
      {/* null until [10-A] carries the client identity through to serve. */}
      {by && <span data-testid="history-by"> · {by}</span>}
    </span>
  )
}

export function SupersededSection({ entries }: { entries: SupersededEntry[] }) {
  const t = useStrings()
  return (
    <section data-testid="superseded-section">
      <h3 className="text-[10px] uppercase tracking-wide text-slate-500">
        {t.supersededHeading(entries.length)}
      </h3>
      <ul className="flex flex-col gap-1.5">
        {/* Newest last, as stored — the archive reads as a timeline. */}
        {entries.map((entry, index) => (
          <li key={`${entry.at}-${index}`} data-testid="superseded-item">
            <Timestamp at={entry.at} by={entry.by} />
            <table className="w-full text-left text-xs">
              <tbody>
                {Object.entries(entry.prev ?? {}).map(([field, value]) => (
                  <tr key={field} data-testid="superseded-field">
                    <th className="pr-2 align-top font-normal text-slate-500">{field}</th>
                    <td className="break-all text-slate-300" data-testid="superseded-value">
                      {renderArchivedValue(value)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function ProvenanceSection({ entries }: { entries: ProvenanceEntry[] }) {
  const t = useStrings()
  return (
    <section data-testid="provenance-section">
      <h3 className="text-[10px] uppercase tracking-wide text-slate-500">
        {t.provenanceHeading(entries.length)}
      </h3>
      <ul className="flex flex-col gap-0.5">
        {entries.map((entry, index) => (
          <li key={`${entry.at}-${index}`} data-testid="provenance-item">
            <span className="text-xs text-slate-300" data-testid="provenance-action">
              {entry.action}
            </span>{' '}
            <Timestamp at={entry.at} by={entry.by} />
          </li>
        ))}
      </ul>
    </section>
  )
}
