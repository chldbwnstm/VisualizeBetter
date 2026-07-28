# VisualizeBetter — 세션 핸드오프 프로토콜

> 목적: **재리서치 0.** 한 AI 세션이 그래프에 기록한 지식(구조 + gold)을
> 다음 세션이 처음부터 다시 파악하지 않고 즉시 이어받는다. 사람은 그 그래프를
> 눈으로 보고, AI 와 같은 화면을 공유한다. ([23-D])

이 문서는 두 독자를 위한 것이다:
- **AI 에이전트** — 작업을 시작할 때의 권장 시퀀스와 도구 목록.
- **사람** — 아무 MCP 클라이언트(Claude Desktop 등)에 붙일 시스템 프롬프트 스니펫.

---

## 1. 새 세션 권장 시작 시퀀스 ([23-D])

이전 세션의 작업을 이어받을 때:

```
1. list_snapshots()               # 가장 최근(또는 지정) 스냅샷 확인
2. load_snapshot(snapshot_id)     # 그 시점의 그래프 + finding 복원
3. get_graph_summary()            # 규모 · 타입 분포 · layer · top hub 개요
4. list_findings(min_confidence=0.5)   # ★ 이전 세션의 gold 를 먼저 읽는다
5. get_finding(finding_id)        # 관심 finding 의 상세 + 앵커 노드 확인
6. get_neighbors(id) / list_nodes(filter="…")   # 관심 영역만 드릴다운
7. (작업 이어가며) push_* + cite + record_finding 로 새 발견을 즉시 기록
```

핵심 원칙: **gold(finding) 를 먼저 읽어라.** finding 은 이전 세션이 "이건 중요하다"
고 명시적으로 남긴 결론이다. 노드/엣지 전체를 훑기 전에 finding 이 어디를 보라고
가리키는지부터 확인하면 재리서치가 0 에 수렴한다.

---

## 2. 시스템 프롬프트 스니펫 (아무 MCP 클라이언트에 붙여넣기)

```
너는 visualizebetter 그래프 워크스페이스에 연결돼 있다.
- 작업 시작 전: load_snapshot 으로 이전 상태를 복원하고, list_findings() 로
  기존 발견(gold)을 먼저 확인한 뒤 get_graph_summary() 로 전체를 파악하라.
- 작업 중: 새로 알아낸 노드/관계는 push_node / push_edge 로 즉시 그래프에
  기록하고, 근거 URL 은 cite() 로 붙여라.
- 결정적 통찰(gold)은 record_finding() 으로 남겨라 — 다음 세션과 사람이 그걸
  먼저 본다. 확신도(confidence)와 근거(evidence)를 함께 적어라.
- 사람이 무엇을 보는지 궁금하면 get_active_filter() / get_focused_node() 를,
  사람의 주의를 끌고 싶으면 suggest_filter() / apply_style() 를 써라.
```

---

## 3. 도구 레퍼런스 (구현된 전체)

### 3.1 핸드오프 / 영속성
| 도구 | 용도 |
|---|---|
| `list_snapshots()` | 저장된 스냅샷 목록 |
| `save_snapshot(name, description="")` | 현재 그래프+finding 을 이름으로 저장 |
| `load_snapshot(snapshot_id)` | 스냅샷 복원(로드 전 자동 안전 스냅샷) |
| `export_graph(format, filter=…)` | 그래프를 파일로 내보냄(json/graphml/dot/cytoscape, filter=[6] DSL 서브그래프) |
| `import_graph(data, format, merge=True)` | 인라인 데이터 임포트(≤1MB, merge=병합/False=교체) |
| `import_from_file(path, format, merge=True)` | 대량 파일 임포트(서버 데이터 디렉토리 내 경로만) |

### 3.2 지식 기록 (WRITE)
| 도구 | 용도 |
|---|---|
| `push_node(...)` / `push_edge(...)` | 노드 · 관계 추가/갱신(멱등) |
| `push_batch(nodes, edges)` | 대량 추가(호출당 합계 ≤1000) |
| `update_node(...)` / `update_edge(...)` | 부분 수정. `reason`=correction(덮어씀)/supersede(백업 후 덮어씀) |
| `delete_node(id, cascade=False)` / `delete_edge(...)` | 삭제(cascade 시 엣지 연쇄) |
| `clear_layer(layer)` / `clear_all()` | layer/전체 정리 — ★finding(gold)·스냅샷은 보존 |
| `cite(node_id, source_url, source_title)` | 노드에 근거 출처 첨부(`_citations`) |

### 3.3 gold 기록 (FINDING)
| 도구 | 용도 |
|---|---|
| `record_finding(...)` | 결정적 통찰을 앵커 노드와 함께 기록(confidence·evidence) |
| `update_finding(...)` / `delete_finding(id)` | finding 수정/삭제 |
| `list_findings(min_confidence=…, limit, offset)` | gold 목록(created_at desc) |
| `get_finding(finding_id)` | finding 전체 + 앵커 |

### 3.4 조회 / 탐색 (READ)
| 도구 | 용도 |
|---|---|
| `get_graph_summary()` | 규모·타입 분포·layer·top hub |
| `get_node(id, include_neighbors=False)` | 노드 전체(+1홉 이웃) |
| `list_nodes(filter=…, limit, offset, sort_by, order)` | 필터([6] DSL) + 페이지네이션 |
| `list_edges(filter=…, limit, offset)` | 엣지 필터 |
| `get_neighbors(id, depth=1, max_nodes=200, direction="both", edge_filter=…)` | 이웃 서브그래프(방향 in/out/both) |
| `find_paths(source, target, max_length=5, max_paths=10, edge_filter=…)` | 두 노드 간 경로 열거 |
| `search(query, in_fields=["label","id"], limit=50)` | 부분 일치 검색(대소문자 무시, 필드 화이트리스트) |

필터 표현식 문법은 **[docs/filter-dsl.md](filter-dsl.md)** 참조
(예: `type == "class" AND connected_to("Svc.Auth", within=2)`).

### 3.5 사람의 화면 인지 (USER STATE)
| 도구 | 용도 |
|---|---|
| `get_active_filter()` | 사람이 건 필터 + 매칭 수 |
| `get_visible_nodes(limit=1000, offset=0)` | 지금 사람 화면에 보이는(필터 통과) 노드 |
| `get_focused_node()` | 사람이 선택한 노드 + 얼마나 됐는지(since_ms) |
| `get_selection_history(last_n=10)` | 최근 클릭 이력 |
| `get_view_state()` | 뷰 모드/줌/카메라 |
| `poll_events(since_cursor, limit=100, event_types=[…])` | 사람의 focus/filter 변화 폴링(커서 기반) |

> ※ MCP 는 서버발 push 가 안 되므로, 사람의 변화를 반영하려면 작업 사이사이에
> `poll_events` / `get_focused_node` 를 호출하는 폴링 패턴을 쓴다.

### 3.6 사람에게 제안 / 화면 조작 (VIEW CONTROL)
| 도구 | 용도 |
|---|---|
| `suggest_filter(dsl_expr, reason)` | 필터 제안(배너로 뜸, 적용은 사람이 결정) |
| `focus_on(node_id, zoom_level=1.5)` | 사람 화면을 이 노드의 상세 뷰로 이동 |
| `set_layout(algorithm, options={})` | 상세 뷰 레이아웃 변경(dagre/concentric/fcose/grid/preset) |
| `apply_style(selector, style, ttl=0)` | selector([6] DSL) 매칭 노드 임시 강조(color/size/border) |
| `clear_style(style_id=None)` | 임시 스타일 해제(None=전체) |
| `add_annotation(x, y, text, ttl=0)` | 화면 좌표에 텍스트 주석 |
| `render_in_chat(node_id)` | 그 노드의 이웃 서브그래프를 대화창 안 인라인 카드로 렌더(MCP Apps) |

### 3.7 되돌리기 (UNDO/REDO — M2)
| 도구 | 용도 |
|---|---|
| `undo()` | 마지막 그래프 mutation 을 되돌림(전역 스택, 서버 before-image 복원) |
| `redo()` | undo 를 다시 적용 |

> ※ undo/redo 는 그래프 상태 변경(push/update/delete/clear/cite/finding/import)만
> 대상이며, 새 mutation 시 redo 스택은 초기화된다. 결과는 라이브 뷰에 자동 반영.

---

## 4. 데이터 수명주기 (중복·정정·최신화) — [24]

AI 가 이미 잘 기록한 데이터를 다룰 때:
- **중복 금지** — push 는 멱등이다. 같은 노드/엣지를 다시 push 해도 중복되지 않고
  병합된다(identity: 노드=`id`, 엣지=`(source, target, relation, key)`).
- **정정(100% 틀림 확인)** — `update_node(..., reason="correction")`: 기존 값을
  백업 없이 덮어쓰고 `_provenance` 에 정정 사실만 남긴다.
- **최신화(outdated)** — `update_node(..., reason="supersede")`: 기존 값을
  `_superseded` 에 백업한 뒤 최신값으로 덮어쓴다. 옛 정보가 사라지지 않는다.

`_` 로 시작하는 예약 속성(`_citations` / `_superseded` / `_provenance`)은
시스템이 관리하며 직접 set/remove 할 수 없다(위조 방지, [11]).

---

## 5. 연결 방법 (요약)

- **웹앱 (M1)**: `visualizebetter serve` → 브라우저에서 그래프 뷰, MCP 는 HTTP 로 노출.
- **stdio 프록시 (예정, [8-D])**: Claude Desktop 등 stdio MCP 클라이언트를
  serve 에 연결. 연결의 clientInfo 가 layer 태그로 전달돼 "누가 기록했는지"가
  provenance 로 남는다([23-E]).

---

*이 문서는 구현된 도구 집합을 반영한다. 필터 문법 상세는
[filter-dsl.md](filter-dsl.md).*
