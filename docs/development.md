# 개발 실행·검증·인계

ETF 연구는 [종합 판단과 상세 결과 목록](research-conclusions-2026-09-29.md)을 먼저 읽는다. `etf_cli.py`, `auto_cli.py`, `selection_cli.py`는 국내 운용과 분리된 연구 진입점이다. 각 실행 안내에 원문 수집·오프라인 재현·자료 이전 절차가 있다. 원문과 전체 원장은 `.local/`, 공유용 보고서·JSON·그림은 `docs/`에 보관한다.

국내 연구·운용 자료의 집 PC 이전은 [작업 인계서](handoff-2026-09-29.md)를 따른다. [미국 ETF 전략 논의](us-etf-strategy-discussion-2026-09-29.md)는 구현 전 기록이며 현재 후보 판단은 종합 문서를 우선한다.

2026-09-29의 일봉 재생 엔진 변경과 재현 절차는 [투자 엔진 1차 개선](engine-improvement-2026-09-29.md)에 기록했다. 기존 탐색 결과와 새 실행 계약을 구분한다.

새 PC 설치와 데이터 이전은 [다른 PC에서 시작하고 이어가기](workstation-setup.md), 작업 원칙은 [AGENTS.md](../AGENTS.md)를 먼저 읽는다. 아래 PowerShell 명령은 **저장소 루트**에서 시작한다. Windows PowerShell에서 npm 스크립트 실행 제한을 피하도록 `npm.cmd`를 사용한다.

## 1. 개발 서버

완성된 화면을 확인하려면 [README](../README.md)의 통합 실행기를 사용한다. 화면/코드를 수정하며 즉시 확인하려면 PowerShell 창 세 개를 열어 각각 실행한다. 통합 실행기는 먼저 종료한다.

창 1 — API:

```powershell
$env:INVEST_MODE = 'local_simulation'
$env:INVEST_STATE_DIR = Join-Path $PWD '.local\dev'
$env:INVEST_DB_PATH = Join-Path $env:INVEST_STATE_DIR 'workspace.db'
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backend.ps1
```

창 2 — 작업자. API와 **같은 모드·같은 절대 DB 경로**를 사용한다.

```powershell
$env:INVEST_MODE = 'local_simulation'
$env:INVEST_STATE_DIR = Join-Path $PWD '.local\dev'
$env:INVEST_DB_PATH = Join-Path $env:INVEST_STATE_DIR 'workspace.db'
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\worker.ps1
```

창 3 — 프론트:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\frontend.ps1
```

화면은 <http://127.0.0.1:5173>, API는 <http://127.0.0.1:8000>이다. Vite가 `/api`를 API 서버로 전달한다. 종료할 때는 세 창 모두 `Ctrl+C`를 누른다. 모의 개발을 명시적으로 진행할 때만 API/작업자의 모드를 함께 `kis_paper`로 바꾸며, 기존 운용 데이터/동시 작업자를 먼저 확인한다.

`worker.ps1 -Once`는 실행 루프를 한 차례 진행하는 관리/진단 옵션이다. 한 번의 호출이 전체 매수→체결→매도 완료를 의미하지 않는다.

## 2. 의존성과 기본 검사

코드/잠금 파일을 받은 뒤 다음 명령으로 준비한다. 정상 설치된 지원 Node 버전을 사용한다. 모든 검사는 외부 계좌 연결 없이 실행할 수 있다.

```powershell
uv sync --project backend --frozen
uv run --project backend --frozen ruff check backend/src backend/tests scripts/export-openapi.py
uv run --project backend --frozen ruff format --check backend/src backend/tests scripts/export-openapi.py
uv run --project backend --frozen pytest backend/tests -q

Push-Location frontend
try {
    npm.cmd ci
    npm.cmd run format:check
    npm.cmd test
    npm.cmd run build
} finally { Pop-Location }
```

각 명령의 종료 결과를 확인하고, 실패한 채로 다음 검증까지 성공한 것으로 판단하지 않는다.

변경에 맞는 검증부터 실행한다. 계산/주문/저장/복원을 바꾸면 관련 백엔드 회귀, 화면을 바꾸면 단위/브라우저/빌드를 확인한다. 전체 CI 명령은 `.github/workflows/ci.yml`이 기준이다.

## 3. API 계약 확인·갱신

실행 중 API나 `.env` 없이 격리한 샘플에서 OpenAPI를 내보낼 수 있다.

```powershell
uv run --project backend --frozen python scripts/export-openapi.py .local/openapi.json
$env:INVEST_OPENAPI_PATH = Join-Path $PWD '.local\openapi.json'
Push-Location frontend
try {
    npm.cmd run api:check
} finally {
    Pop-Location
    Remove-Item Env:INVEST_OPENAPI_PATH -ErrorAction SilentlyContinue
}
```

의도한 API 변경으로 차이가 생긴 경우에만 같은 절차의 `api:check`를 `api:generate`로 바꿔 재생성하고, 생성된 차이와 호출 코드를 확인한다. `frontend/src/api.generated.ts`를 손으로 편집하지 않는다. 환경 변수가 없으면 기본적으로 `http://127.0.0.1:8000/openapi.json`을 조회하므로 서버 없이 검사할 때는 위 파일 지정이 필요하다.

## 4. 브라우저 검증

```powershell
Push-Location frontend
try {
    npm.cmd run test:e2e
} finally { Pop-Location }
```

Windows에서는 기본적으로 설치된 Edge를 사용한다. Edge가 없으면 별도로 설치하거나 아래처럼 Playwright Chromium을 선택한다.

```powershell
Push-Location frontend
try {
    npx.cmd playwright install chromium
    $env:PW_CHANNEL = 'chromium'
    npm.cmd run test:e2e
} finally {
    Remove-Item Env:PW_CHANNEL -ErrorAction SilentlyContinue
    Pop-Location
}
```

테스트용 Vite는 `127.0.0.1:15174`를 사용하므로 포트를 비워 둔다. 브라우저 테스트는 합성 API 응답의 회귀이며 실제 KIS 주문 성공의 증거가 아니다. 실제 API + worker의 수동 확인은 위의 격리된 샘플 환경에서 한다.

## 5. Linux/macOS에서 개발할 때

`.ps1` 실행기는 Windows 전용 프로세스/경로 처리를 포함한다. Python/React는 Linux CI로 검증했지만 macOS 실기기 확인은 하지 않았다. 아래는 같은 구성 요소를 실행하는 방법이며, 클라우드 운영 배포는 별도로 구성해야 한다.

Git/uv/지원 Node.js를 준비하고 저장소 루트에서 의존성을 설치한다.

```bash
uv sync --project backend --frozen
cd frontend
npm ci
cd ..
```

API용과 worker용 각 터미널에서 저장소 루트 기준으로 같은 환경 변수를 설정한다.

```bash
export INVEST_MODE=local_simulation
export INVEST_STATE_DIR="$PWD/.local/dev"
export INVEST_DB_PATH="$INVEST_STATE_DIR/workspace.db"
```

API용 터미널:

```bash
uv run --project backend --frozen uvicorn invest_lab.adapters.http:create_app --factory --host 127.0.0.1 --port 8000
```

worker용 터미널:

```bash
uv run --project backend --frozen python -m invest_lab.worker
```

다른 터미널에서 프론트를 실행한다.

```bash
cd frontend
npm run dev
```

화면은 `http://127.0.0.1:5173`이다. 종료할 때 각 터미널에서 `Ctrl+C`를 누른다. Linux 브라우저 테스트는 `npx playwright install --with-deps chromium`, macOS는 `npx playwright install chromium`으로 준비한다.

## 6. 현재 인계 상태

2026-09-29 매도·포트폴리오 정책 및 비교 재생 변경은 [구현 기록](sell-engine-implementation-2026-09-29.md), 18개 조건의 결과와 자료 한계는 [비교 보고서](sell-engine-backtest-2026-09-29.md)를 참고한다. 백엔드 299개 테스트와 독립 원장 대조는 통과했지만, 배당·권리/PIT 자료와 실제 장중 모의 운영 게이트는 미완료다.

2026-09-28 문서 정리 시점의 기준 구현은 `7644734`다. 검증의 상세 내용은 [2026-09-27 구현 검증 기록](2026-09-27-implementation-verification.md)에 있다.

- 로컬 분석/연구/주문/복구 UI, KIS 모의 어댑터, 시점 정보를 보존하는 연구, 작업자, 백업/복원을 구현했다.
- 기록 당시 오프라인 검증은 백엔드 226개, 프론트 단위 10개, 브라우저 7개가 통과했다. 과거 기준이며 새 변경의 테스트 결과로 재사용하지 않는다.
- KIS 실제 읽기/잔고 대조와 주말 WS 연결·구독 ACK를 확인했다. 실제 모의 주문은 전송하지 않았다.
- G1 전체 시장/역사 자료, G2 장중 모의 주문/정정/취소/체결, G3 실제 자료 연구, G4 연속 운영, G5 실제 한 달 관찰은 미완료다. 키와 기존 원본도 Git에 없다.
- 다음 작업은 최신 [작업 인계서](handoff-2026-09-29.md)의 연구 방향을 확인하고 정한다. 외부 연결/모의 운영을 재개할 때는 [사용자가 할 일](user-actions.md)의 준비 상태도 새 PC에서 진단한다. 합성 자료로 외부 검증을 완료 처리하지 않으며 실제 매매나 원격 배포는 해당 작업의 사용자 지시에 따른다.

구현이나 운영 상태가 바뀌면 근거와 날짜를 남겨 이 인계 부분과 해당 문서를 갱신한다. 로컬 전용 메모는 `.local/`, 다른 PC에서도 필요한 비밀이 아닌 절차/설계는 `docs/`로 공유한다.

## 7. 이번 문서의 실행 확인

2026-09-28, Windows의 한글·공백을 포함한 별도 폴더에 추적 파일만 내보내 `.env`, 기존 DB, 가상환경, 프론트 의존성이 없는 상태에서 확인했다. Python 3.12.12, Node 24.19.0을 사용했다.

- 통합 실행기의 의존성 설치/프론트 빌드, 샘플 화면·API 응답, 작업자의 `running` 상태를 확인했다. `Ctrl+C` 후 해당 API/작업자 프로세스 종료도 확인했다.
- 샘플 데이터 백업, 새 폴더 복원, 두 저장 경로를 지정한 복원 후 진단을 확인했다. PowerShell 간 결과 전달에서 한글 경로가 깨지는 경우는 이전 안내의 UTF-8 설정으로 해결했다.
- 외부 API 없이 OpenAPI를 내보내 프론트 계약 검사를 통과했다. 문서의 상대 링크와 PowerShell 예시 구문도 검사했다.

이 확인은 같은 PC의 별도 폴더에서 수행했다. 사무실 PC의 설치 권한·네트워크·실제 한투 연결은 해당 PC에서 확인해야 한다. 문서 변경에 대해 기존 전체 테스트를 다시 실행한 것으로 표시하지 않는다.
