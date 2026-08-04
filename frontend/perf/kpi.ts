/**
 * ★ [15] 인수 기준을 **로그가 아니라 단언**으로 남긴다 ([13-B] CH2(5)).
 *
 * 이전 판까지 KPI 판정은 전부 `record('… 판정', cond ? 'PASS' : 'FAIL')` 이었고
 * 스펙이 실제로 단언하는 것은 "프로브가 돌았다"(`expect(stats.frames)
 * .toBeGreaterThan(30)`) 뿐이었다. 30 FPS 가 12 로 떨어져도, push 지연이 100ms
 * 에서 900ms 가 되어도 스펙은 초록이었다 — 미달은 콘솔에만 남고, 콘솔은 아무도
 * 빨갛게 만들지 않는다. CLAUDE.md 는 "[15] 성능 인수 기준은 협상 대상이 아니다"
 * 라고 못박는데 그것을 강제하는 장치가 하나도 없었다.
 *
 * **왜 soft 인가.** KPI 하나가 미달했을 때 남은 측정이 계속 수집돼야 회귀의
 * 전경(어디까지 무너졌나)이 보인다. hard 단언은 첫 미달에서 스펙을 끊어 그 뒤의
 * 수치를 통째로 없애는데, 그러면 "고치고 다시 돌려야 다음 수치를 본다" 가 되어
 * 진단 한 번에 왕복 N 회가 든다. soft 는 **봐주는 게 아니다** — 실패한 soft 단언은
 * 테스트를 그대로 실패시키고, 보고 시점만 테스트 끝으로 미룬다.
 *
 * **기준값은 [15] 그대로다.** 이 파일은 판정을 옮겨 적을 뿐 새 숫자를 만들지
 * 않는다 (인수 기준의 값 변경은 CLAUDE.md 의 STOP&ASK 항목이다).
 *
 * 기기 의존이라 CI 게이트가 아니라는 성격은 그대로다 — 이 harness 는 실 GPU 를
 * 요구하고(SwiftShader 감지 시 실패) 별도 config 로 분리돼 있다. 자세한 것은
 * `docs/benchmarks.md`.
 */

import { expect } from '@playwright/test'

function show(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

/** [15] "X 이상" 판정 — 예: 100K 렌더 >= 30 FPS. */
export function kpiAtLeast(actual: number, floor: number, criterion: string): void {
  expect.soft(actual, `[15] ${criterion} — 실측 ${show(actual)}, 기준 >= ${floor}`)
    .toBeGreaterThanOrEqual(floor)
}

/** [15] "X 미만" 판정 — 예: push → 화면 반영 < 100ms. */
export function kpiUnder(actual: number, ceiling: number, criterion: string): void {
  expect.soft(actual, `[15] ${criterion} — 실측 ${show(actual)}, 기준 < ${ceiling}`)
    .toBeLessThan(ceiling)
}

/** [15] "정확히 X" 판정 — 예: 라이브 push 부하 중 프레임 드랍 0. */
export function kpiExactly(actual: number, want: number, criterion: string): void {
  expect.soft(actual, `[15] ${criterion} — 실측 ${show(actual)}, 기준 = ${want}`).toBe(want)
}
