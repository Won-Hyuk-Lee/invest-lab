# 이전 연구 보관 자료

2026-09-30 프로젝트를 AI·외부 판단용 로컬 매매 실행 도구로 전환했다. 아래 연구는 과거 탐색 기록이며 현재 기본 운용 목적이나 주문 활성화 조건이 아니다. 원문/결과를 삭제하거나 최고 전략을 자동 채택하지 않는다.

| 보관 내용 | 문서 |
| --- | --- |
| ETF·자동 모델·선정 연구 종합 | [2026-09-29 종합 판단](research-conclusions-2026-09-29.md) |
| 국내 백테스트와 자료 이전 | [2026-09-29 인계](handoff-2026-09-29.md) |
| ETF 독립 연구 | [실행 안내](etf-research-guide.md) |
| 자동 모델 비교 | [실행 안내](auto-models-guide.md) |
| 선정·위험 배분 | [실행 안내](selection-research-guide.md) |
| 초기 모의 투자 계획 | [완료 계획](paper-investment-completion-plan.md) |

기본 화면의 ‘이전 연구 기록’에서 이전 화면을 열 수 있다. 기존 연구 CLI와 `.local/etf-research`, `.local/auto-models`, `.local/selection-research`도 보존한다. 기본 작업자는 연구 작업과 내장 전략 자동 주문을 거부한다.

기존 통합 연구 작업을 명시적으로 다시 실행할 때만 별도 셸에서 `INVEST_WORKSPACE=research`를 지정한다. 이 프로필은 과거 내장 전략 운용 기능도 포함하므로 주 실행기/계좌와 분리한 상태 폴더를 사용한다. `INVEST_WORKSPACE`를 지정하지 않은 새 실행은 항상 `execution`이다.

현재 실행 절차와 기능 범위는 [README](../README.md)와 [로컬 매매 안내](local-trading.md)를 우선한다. 과거 문서의 ‘다음 운용 후보’·연구 완료 게이트·성과 수치는 새 도구의 자동 운용 결정을 뜻하지 않는다.
