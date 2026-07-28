# VisualizeBetter — Known Issues (이월 이슈)

정직성 원칙: "flaky 테스트는 없느니만 못하다." retry/문서화로 가려진 것도 여기 남긴다.

---

## KI-1: E2E 잔여 간헐 실패 — cosmos.gl 로드-시점 크래시

- **상태:** ✅ CLOSED (TASK Z1, 2026-07-18)
- **발견:** TASK 7d.1 / 7d.2 (checkpoint/14, 2026-07-17)
- **증상:** Playwright 전체 스위트에서 드물게 한 테스트가 "push 한 데이터가
  제한시간 내 미도착"으로 실패. 실패 테스트가 이동(rotating). 단독 실행은 통과 —
  전체 스위트 문맥에서만 재현. `retries:1` 로 green.

- **근본 원인 (TASK Z1 계측으로 규명):** **cosmos.gl 3.3.0 로드-시점 크래시.**
  cosmos 는 WebGL 디바이스/points 모듈을 비동기 init 하는데, 첫 `graph.batch` 가
  init 완료 전에 도착하면 `OverviewCanvas` 의 `setPointPositions` 호출이 cosmos
  내부에서 throw 한다(`Cannot set properties of undefined (setting 'shouldSkipRescale')`
  — `this.points` 미할당, ensureDevice 게이트는 통과하는 좁은 레이스). **에러
  바운더리가 0개였으므로** 이 throw 가 React 루트 전체를 언마운트 → 페이지 blank
  → 그 테스트의 모든 assertion 이 30s 타임아웃. GPU/ReadPixels 스톨이 init 을 첫
  push 뒤로 밀 때 레이스 패배. retries:0 로 12회 중 7회 실패, 4/4 실패 동일
  시그니처로 실측 확인.
  - **핵심 단서 재해석:** "timeout 확대 무효" = 느려서가 아니라 **앱이 죽어서**
    데이터가 안 옴. "rotating" = 로드 레이스라 어느 테스트든 걸림.

- **오진 정정 이력:** Z1 초기엔 WS half-open 으로 진단했으나(WS CLOSE 가 실제
  관측 증상이었음 — 실은 앱 언마운트의 하위 결과), heartbeat 구현 후 retries:0
  검증에서 **안 고쳐졌고**(7/12 실패 유지), 계측(Playwright pageerror/WS 프레임
  훅)으로 진짜 원인이 cosmos 크래시임을 잡았다. WS 는 정상(데이터 134ms 정상
  수신), 그 7ms 뒤 cosmos 크래시. 근본검증이 오진을 잡은 사례.

- **수정 (Fable 판정 D = A+B):**
  - **A (근본):** `OverviewCanvas` 첫 데이터 적용 전 cosmos 공개 준비 신호
    (`graph.isReady`/`graph.ready`)로 게이트 → 미준비면 `ready` 후 이펙트 재실행.
    준비 전 push 는 setPointPositions 호출 안 함 → 준비 즉시 적용. 결정적(타이밍
    비의존). cosmos 내부 필드 결합 없음. cosmos 미준비 시뮬 유닛테스트로 검증.
  - **B (안전망):** `GraphErrorBoundary` 신규 — Overview·Detail 뷰를 감싸 크래시
    격리 + bounded rAF 자동 리마운트/복구. 렌더 throw 가 앱 전체를 언마운트하던
    갭(에러바운더리 0개)을 닫음. 시뮬 throw 격리+복구 유닛테스트로 검증.
  - **heartbeat([8-C] ping/pong):** KI-1 수정은 아니나 half-open 자동복구는
    별개의 실사용 견고성(sleep/wake·network blip·proxy timeout) — 유지.

- **검증:** 계측 제거 후 전체 스위트 **retries:0 로 18회 연속 clean**(수정 전
  7/14 실패 → 수정 후 0 크래시). `playwright.config.ts retries:1 → 0` 제거(더
  이상 가릴 플레이크 없음). 회귀: pytest 658 / vitest 252 / build exit0.
  (상세 계측·증거는 내부 진단 리포트에 보관)
