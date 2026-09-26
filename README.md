# invest-lab

구현 범위와 실제/합성 검증의 구분은 [2026-09-27 구현 검증 기록](docs/2026-09-27-implementation-verification.md)에 정리했습니다.

코스피 보통주와 국내 상장 ETF를 분석하고, 규칙 전략·재무·매크로·통계모델을 검증하는 개인용 투자 연구실입니다. 프론트는 React + TypeScript, 백엔드는 Python입니다.

**로컬 샘플 실행과 한국투자증권 모의계좌 연결을 지원합니다.** 두 모드의 주문·잔고 저장 공간을 분리합니다. 실제 돈을 거래하는 실전 서버는 지원하지 않습니다. 처음 사용할 때 필요한 준비는 **[사용자가 할 일](docs/user-actions.md)** 한 문서에서 확인하세요.

## 실행

저장소 루트 `C:\Users\Lee\Desktop\갠플\invest-lab`에서 실행합니다.

```powershell
# 화면 + API + 작업자를 함께 실행합니다. 기본값은 로컬 샘플입니다.
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1

# 한투 모의계좌 모드
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Mode kis_paper
```

- 화면: <http://127.0.0.1:8000> · API 문서: <http://127.0.0.1:8000/docs>
- `Ctrl+C`로 서버와 함께 시작한 작업자를 종료합니다. 브라우저만 닫으면 작업자는 계속 실행합니다. PC 종료·절전 중에는 실행되지 않습니다.
- 첫 실행에는 `uv`, npm이 포함된 Node.js가 필요합니다. Node **24.15 이상**을 권장하며, 정확한 허용 범위는 `frontend/package.json`에 있습니다. Python 3.12와 의존성은 잠금 파일을 사용해 설치합니다.
- 처음 설치/빌드한 뒤에는 `-SkipSetup`으로 설치를 건너뛸 수 있습니다. 포트 충돌 시 기존 실행을 종료하거나 `-Port 8001`을 사용하세요.
- 개발 중에는 `scripts/backend.ps1`, `scripts/frontend.ps1`, `scripts/worker.ps1`을 각각 사용할 수도 있습니다. 개발 화면은 5173번 포트입니다.

서버는 `127.0.0.1`에만 열립니다. 개인 PC에서 쓰는 구성이며 공개 서버 인증·원격 인프라는 이번 범위에 포함하지 않습니다.

## 현재 동작

| 영역 | 구현 내용 |
| --- | --- |
| 계좌·주문 | 현금/주문 가능액/보유 평가액, 매수·매도, 정정·취소, 미체결·부분 체결·결과 불명, 중복 의도 차단, 계좌 대조 |
| 시장 데이터 | 검토된 마스터·개장/휴장 달력 가져오기, 일봉 분할 수집/재시작, 원주가·수정주가 구분, 원본·수정 시각·coverage 보존 |
| 분석·전략 | 7·30·90 **거래일** 수익률, 이동평균·RSI·ATR·상대 강도·유동성, 코스피 상승 추세 눌림목과 ETF 추세를 별도 규칙으로 평가 |
| 위험·운영 | 진입/청산·수량/노출 제한, 선택한 운용 버전, 신규 진입 중지, 미확정 주문 차단, 작업자 실행권·재시작·오래된 신호 차단 |
| 재무·매크로·ETF | DART 접수번호 원문/정정, 호환 분기/TTM·가치/품질, FRED 빈티지/공개 지연, ETF 분배금·메타데이터·가격수익/총수익 구분 |
| 백테스트 | 당시 유니버스/사용 가능 정보, 다음 세션 시가 체결, 비용·분할·배당/분배금·상폐 정산, 결측 제외/실패 사유, 비용 민감도 |
| 통계모델 | Ridge·LogisticRegression, 날짜 단위 분할·레이블 중첩 제거·별도 확률 보정·최종 평가, 기술/재무/매크로/전체 4가지 입력 비교 |
| 연구 화면 | 불변 데이터셋/manifest, 피처와 결손, 실험·모델 등록, 동일 규칙/비용/위험의 포트폴리오 비교, 후보·관찰/선택/거절 상태 |
| 기록·복구 | 일/월 보고·관찰 구간, 외부 입출금/초기화 후 기준선 관리, DB+연구 artifact 백업, 새 폴더 복원 후 진입·배포 중지와 재대조 |

연구 점수는 설명 가능한 순위이고 수익 확률이 아닙니다. 로지스틱 출력은 명시한 목표 사건의 추정 확률입니다. 모델을 학습했다고 자동 채택하지 않으며 합성 자료나 불완전한 검증 결과는 모의 자동 운용 모델로 승격하지 않습니다.

## 확인한 것과 남은 것

2026-09-27에는 앱의 한투 모의 어댑터로 현재가·계좌 조회와 잔고 대조를 확인했습니다. 02:55 KST 검사에서 대조 결과는 `matched`, 조회된 주문과 전송한 주문은 모두 0건이었습니다. 가입 키/계좌/HTS ID 설정은 준비됐고, `KIS_PAPER_ORDERS_ENABLED=false`를 유지합니다. 이전 설명식 `.env`는 표준 필드로 정리됐으며 원본은 Git 제외 경로에 보관했습니다.

현재 마스터의 공개 원본은 수집했지만 **현재 목록은 역사 유니버스의 증거가 아닙니다.** ETF 지리/상품 분류, 검증된 거래 달력, 역사 원주가/기업행동, 재무 원문과 매크로 빈티지 확보는 별도입니다. DART/FRED/KRX 키는 아직 설정되지 않았습니다.

오프라인 화면 흐름에서 매수·매도·백테스트·Ridge 실험·보고서 연결을 확인했고, 합성 500세션 비교 실험에서 8개 모델을 생성했습니다. 합성 실험의 실제 자료 검증 상태는 계속 `false`입니다. 실제 장중 모의 매매·정정/취소·WebSocket 복구, 실제 자료의 전체 PIT 검증, 연속 운영과 한 달 관찰은 완료되지 않았습니다. **G1~G5를 전체 통과했다고 표시하지 않습니다.** 구체적인 준비·검증 순서는 [사용자가 할 일](docs/user-actions.md)에 모았습니다.

한투의 정정 주문 수량 의미는 실제 브로커 근거를 확인하기 전까지 미확정으로 남기며, 조회에서 빠진 수수료는 0으로 만들지 않습니다. 근거 연결과 비용 기록 절차도 [사용자가 할 일](docs/user-actions.md)에 있습니다.

기본 수수료·세금·슬리피지는 연구 가정입니다. 한투 실제 요율이나 미래 성과를 뜻하지 않으므로 실험 설정과 결과의 비용 가정을 함께 확인하세요.

## 구조와 저장 위치

```text
frontend/                      React / TypeScript / Vite / React Query
backend/src/invest_lab/
  domain/                      순수 지표·전략·위험·PIT 계산
  application/                 주문·연구 유스케이스
  ports/                       브로커·저장소·학습기 인터페이스
  adapters/                    FastAPI / SQLite / KIS 모의 / DART·FRED·KRX
  worker.py                    일정·수집·분석·대조·감시 작업자
  cli.py                       진단·가져오기·백업·복구 도구
scripts/                       통합 실행·개발 실행·관리 명령
docs/user-actions.md           사용자 준비/외부 검증의 단일 진입 문서
.local/                        실제 로컬 상태와 원본 (Git 제외)
```

계산·유스케이스는 FastAPI/SQLite/한투 어댑터를 직접 import하지 않습니다. 저장소와 외부 서비스를 포트로 주입하며, 아키텍처 테스트로 의존성 방향을 검사합니다. 다른 호스트로 옮길 때에도 실행 환경·영구 저장소·외부 인증 준비는 필요합니다.

`.local/workspace.db`, `runtime-local_simulation.db`, `runtime-kis_paper.db`, `market.db`, `research.db`, `research_artifacts/`, `logs/`를 사용합니다. `INVEST_STATE_DIR`는 운영/시장/연구 경로, `INVEST_DB_PATH`는 workspace DB 경로를 변경합니다. `.env*`, `.local/`, `.venv/`, `node_modules/`는 Git 제외 대상이며 값이 비어 있는 `.env.example`만 추적합니다. 키·계좌번호·원문 `.env`를 화면 코드나 커밋에 넣지 않습니다.

## 개발 검증

```powershell
# backend에서
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .

# frontend에서
npm ci
npm run build
npm test
npm run format:check
npm run api:check
npm run test:e2e
```

Playwright 브라우저 설치가 필요한 환경은 최초 한 번 `npx playwright install chromium`을 실행합니다. 실제 한투 주문 검증은 이 오프라인 테스트와 분리합니다.

설계 근거는 [완료 계획](docs/paper-investment-completion-plan.md), [데이터 계약](docs/paper-investment-data-contracts.md), [전체 작업 목록](docs/research-roadmap.md), [지표 정의](docs/indicator-catalog.md)에 있습니다. 9월 25일 MVP 기록과 9월 27일 최초 감사/연결 기록은 **그 시점의 기록**이며, 현재 실행 방법과 준비 상태는 이 README와 [사용자가 할 일](docs/user-actions.md)을 우선합니다.
