/**
 * i18n — UI 표시 언어 (한국어/English).
 *
 * Deliberately dependency-free ([17] locked stack untouched): a typed dictionary
 * plus a tiny zustand store, persisted to localStorage. Only *chrome* strings
 * (buttons, headings, empty states, tooltips) are translated — graph content
 * (labels, types, properties, findings) is AI/user data and passes through
 * untouched. Technical vocabulary the MCP tools also use (undo/redo, live,
 * properties, min confidence, Findings) stays English in both languages so the
 * human and the AI keep naming the same things ([7-B] same-enum principle).
 *
 * Default is Korean (the project's current voice); the header select persists
 * the choice per browser. Adding a language = adding one object that must
 * satisfy `typeof KO` — the compiler enforces key parity, and i18n.test.ts
 * re-checks it at runtime for good measure.
 */

import { create } from 'zustand'

export type Lang = 'ko' | 'en'

const STORAGE_KEY = 'vb.lang'

const KO = {
  // App shell
  splitView: '2분할',
  undoTitle: '실행 취소 (마지막 그래프 변경)',
  redoTitle: '다시 실행',
  expandRequestsHeader: '확장 요청 (AI 전달 대기)',
  languageLabel: '언어',

  // FilterBar
  filterPlaceholder: '필터: type == "class" AND degree(node) > 3',
  apply: '적용',

  // SuggestionBanner
  aiSuggestion: 'AI 제안',
  dismiss: '무시',

  // TemporalScrubber
  temporalTitle:
    '시간축 스크러버 — 그래프가 만들어진 순서(created_at)를 재생합니다. 참고: 현재 그래프를 생성순으로 보여주므로 이후 삭제된 노드는 나타나지 않고, 수정된 노드는 현재 값으로 표시됩니다.',
  pastAt: (time: string) => `◑ 과거 ${time}`,
  backToLive: 'live 복귀',

  // OverviewCanvas
  overviewEmpty: 'AI 가 노드를 push 하면 여기에 나타납니다',

  // DetailPanel
  showMap: '지도 보기',
  neighborsTruncated: '이웃이 많아 일부만 표시합니다',
  hideNode: '숨기기',
  requestExpand: '확장 요청',
  addNote: '노트 추가',
  addNoteTitle: 'annotate_node ([5-F]) 노출 후 연결됩니다',

  // FindingsPanel
  evidence: '근거',
  anchorCount: (n: number) => `앵커 ${n}개`,
  noFindings: '아직 기록된 발견이 없습니다.',

  // NodeInspector
  citationsHeading: '근거 (citations)',
  selectNodeHint: '노드를 선택하면 상세가 표시됩니다.',
  nodeNotLoaded: (id: string) => `노드 ${id} 는 아직 로드되지 않았습니다.`,
  none: '없음',
  neighborsHeading: (n: number) => `이웃 (${n})`,

  // HistorySections
  supersededHeading: (n: number) => `이력 (superseded, ${n})`,
  provenanceHeading: (n: number) => `변경로그 (provenance, ${n})`,

  // GraphErrorBoundary
  viewLoadFailed: '그래프 뷰를 불러오지 못했습니다.',
}

/** `typeof KO` makes the compiler reject a missing/extra key here. */
const EN: typeof KO = {
  splitView: 'Split',
  undoTitle: 'Undo (last graph change)',
  redoTitle: 'Redo',
  expandRequestsHeader: 'Expand requests (awaiting AI)',
  languageLabel: 'Language',

  filterPlaceholder: 'filter: type == "class" AND degree(node) > 3',
  apply: 'Apply',

  aiSuggestion: 'AI suggestion',
  dismiss: 'Dismiss',

  temporalTitle:
    'Time-axis scrubber — replays the graph in creation order (created_at). Note: it reveals the current graph in creation order, so nodes deleted since will not appear and updated nodes show their current values.',
  pastAt: (time: string) => `◑ Past ${time}`,
  backToLive: 'Back to live',

  overviewEmpty: 'Nodes appear here as the AI pushes them',

  showMap: 'Show map',
  neighborsTruncated: 'Too many neighbours — showing a subset',
  hideNode: 'Hide',
  requestExpand: 'Request expand',
  addNote: 'Add note',
  addNoteTitle: 'Wired up once annotate_node ([5-F]) is exposed',

  evidence: 'Evidence',
  anchorCount: (n: number) => (n === 1 ? '1 anchor' : `${n} anchors`),
  noFindings: 'No findings recorded yet.',

  citationsHeading: 'Evidence (citations)',
  selectNodeHint: 'Select a node to see its details.',
  nodeNotLoaded: (id: string) => `Node ${id} is not loaded yet.`,
  none: 'None',
  neighborsHeading: (n: number) => `Neighbors (${n})`,

  supersededHeading: (n: number) => `History (superseded, ${n})`,
  provenanceHeading: (n: number) => `Changelog (provenance, ${n})`,

  viewLoadFailed: 'Failed to load the graph view.',
}

export const STRINGS: Record<Lang, typeof KO> = { ko: KO, en: EN }

function initialLang(): Lang {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'ko' || stored === 'en') return stored
  } catch {
    // no localStorage (SSR/tests without jsdom) → default
  }
  return 'ko'
}

interface I18nState {
  lang: Lang
  setLang: (lang: Lang) => void
}

export const useI18n = create<I18nState>((set) => ({
  lang: initialLang(),
  setLang: (lang) => {
    try {
      localStorage.setItem(STORAGE_KEY, lang)
    } catch {
      // persistence is best-effort; the in-memory switch still works
    }
    set({ lang })
  },
}))

/** Reactive dictionary for components — re-renders on language change. */
export function useStrings(): typeof KO {
  const lang = useI18n((s) => s.lang)
  return STRINGS[lang]
}

/** Non-reactive read for class components (GraphErrorBoundary). */
export function getStrings(): typeof KO {
  return STRINGS[useI18n.getState().lang]
}
