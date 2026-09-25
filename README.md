# invest-lab

국내 주식과 국내 상장 ETF를 분석하고 전략을 검증하는 개인용 로컬 연구실.

현재 버전은 **생성한 샘플 시세 + 로컬 모의계좌**로 작동합니다. 한투 모의투자 서버와 연결되지 않으며 증권사 주문을 전송하지 않습니다. 기존 `.env`는 읽거나 변경하지 않습니다.

## 실행

Windows PowerShell 두 창에서 저장소 루트를 기준으로 각각 실행합니다.

```powershell
# 첫 번째 터미널: Python API
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backend.ps1
```

```powershell
# 두 번째 터미널: React 화면
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\frontend.ps1
```

- 화면: <http://127.0.0.1:5173>
- API 문서: <http://127.0.0.1:8000/docs>
- 중지: 각 터미널에서 `Ctrl+C`
- 필수 도구: `uv`, Node.js 22.12 이상, npm. Python 3.12는 uv가 관리합니다.
- 이 PC의 기존 Node.js가 오래된 경우 프론트 스크립트는 설치된 Codex Node 런타임을 우선 대체 사용합니다. 전역 Node나 Python을 변경하지 않습니다.
- 스크립트는 잠금 파일에 맞춰 의존성을 설치합니다. 처음 실행할 때 인터넷이 필요하고, 설치 후 화면과 계산에는 외부 통신이 필요하지 않습니다.
- 두 서버는 `127.0.0.1`에만 바인딩합니다. 현재는 개인 PC 전용이며 외부 공개 배포용 인증은 아직 없습니다.

Windows 외 환경에서도 `backend`에서 `uv sync --frozen` 후 `uv run uvicorn invest_lab.adapters.http:create_app --factory --host 127.0.0.1 --port 8000`, `frontend`에서 `npm ci` 후 `npm run dev`로 실행할 수 있습니다.

## 구현 범위

| 화면 | 동작 |
| --- | --- |
| 대시보드 | 현금, 보유 종목 평가금액, 총 평가자산, 평가손익, 상태별 종목 수 |
| 종목 탐색 | 코스피 / 국내 지수 ETF / 국내 상장 미국 지수 ETF 분류, 검색, 상태 필터, 점수 정렬, 관심 표시 |
| 종목 상세 | 7·30·90거래일 등락률, RSI(14), 90·120일선, 이격도, 연속 하락, 거래량, 점수 기여도, 가격 차트 |
| 전략 관리 | 4개 지표 가중치, RSI 기준, 검토 기준 점수, 종목별 보유 비중 제한, 버전 보관 |
| 모의투자 기록 | 샘플 가격으로 수동 모의매수 기록, 현금·잔고 반영, 중복 주문 차단 |

초기 관심 종목은 10개입니다. 전체 코스피 종목 수집, 실제 시세, 실제 계좌 잔고, 매도, 자동 실행, 백테스트는 후속 구현 대상입니다. 초기 현금 1,000만 원과 매수 수수료 0.015%는 로컬 실험 가정이며 실제 증권사 요율이 아닙니다. 데이터는 고정된 예제이므로 시간에 따른 실제 손익을 관찰하는 용도로 사용할 수 없습니다.

## 구조

```text
frontend/                     React + TypeScript + Vite
backend/src/invest_lab/
  domain/                     순수 계산, 전략 모델, 로컬 매수 정책
  application/                종목 분석, 포트폴리오 조회, 전략 저장 유스케이스
  ports/                      데이터·저장소·시계 인터페이스
  adapters/                   FastAPI, SQLite, 샘플 시세
  bootstrap.py                구현체 조립
backend/tests/                계산·거래·API·의존성 방향 검증
docs/                         요구사항, 계산 정의, 후속 작업
.local/workspace.db            로컬 계좌와 전략 기록 (Git 제외)
```

계산 계층과 유스케이스는 FastAPI, SQLite, 한투 SDK에 의존하지 않습니다. 현재 저장소는 SQLite이며 이후 실제 시세 어댑터나 다른 DB를 연결할 때 포트 구현과 조립 코드를 교체합니다. 운영 환경 이동에는 별도의 실행 환경·인증·스케줄링 구성이 필요합니다.

## 데이터와 비밀정보

- `.env*`, `.local/`, `.venv/`, `node_modules/`는 Git에서 제외합니다. 빈 예시용 `.env.example`만 예외입니다.
- 현재 앱은 `.env`를 로드하지 않습니다. 증권사 키·계좌번호는 프론트엔드에 넣지 않습니다.
- `INVEST_DB_PATH` 환경변수로 별도 SQLite 파일을 지정할 수 있습니다. 검증용 DB와 개인 기록을 분리할 때 사용합니다.
- 같은 주문 식별자를 재전송해도 한 번만 반영합니다. 현금·보유 비중 검증과 장부 갱신은 한 트랜잭션으로 처리합니다.
- 전략 저장은 버전 충돌을 감지하며 이전 버전을 덮어쓰지 않습니다.

## 검증

```powershell
# backend 디렉터리
uv run pytest -q
uv run ruff check .
uv run ruff format --check .

# frontend 디렉터리, Node.js 22.12 이상
npm run build
npm run format:check
```

계산 정의와 확장 순서는 [서비스 설계](docs/service-design.md), 초기 구현과 확인 결과는 [작업 기록](docs/2026-09-25-local-mvp.md)을 참고하세요.

후속 개발 방향은 **일봉 전체 분석 + 후보 10~20개 장중 감시**, **코스피 상승 추세의 눌림목 중심 + ETF 추세 비교**로 정했습니다. 상세 계산값은 실험 후보이며 현재 앱에 반영된 기능은 아닙니다. [전체 작업 목록과 결정 기록](docs/research-roadmap.md), [계산식·전략 후보](docs/indicator-catalog.md), [한투 API 호출 한도](docs/kis-api-budget.md)를 참고하세요.
