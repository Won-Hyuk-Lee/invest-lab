# 로컬 매매 실행 안내

2026-09-30부터 기본 용도는 외부 판단을 실행하는 로컬 매매 도구다. 판단 생성은 AI/외부 스크립트가 담당하고 앱은 주문 계약과 실행을 담당한다. 내장 AI 호출이나 전략 추천은 제공하지 않는다.

## 실행 모드와 판단 수명

`INVEST_WORKSPACE`는 셸 환경 변수이며 기본 `execution`이다. 기본 작업자는 매 분 계좌 대조와 요청한 주문 작업을 처리하며 기존 `auto_trade`·연구/일별 분석 작업은 거부한다. 기존 배포가 켜진 DB도 자동 전략을 재개하지 않는다. 외부 판단은 전략 버전·백테스트·연구 데이터셋 없이 주문할 수 있다.

`INVEST_MODE`는 `local_simulation`, `kis_paper`, `kis_live`다. CLI의 `--mode`와 실행기의 `-Mode`로도 지정한다. API와 CLI/작업자는 같은 모드·같은 `INVEST_STATE_DIR`·같은 `INVEST_DB_PATH`를 사용한다. `.env`는 키 저장용이며 셸 실행 설정은 자동 적용하지 않는다.

- 새 외부 연결: 기본 중지, 허용 종목 없음, 주문당 10만원, 종목별 20%, 총 투자 50%.
- 설정 저장은 `expected_version`을 비교하고 버전을 증가시킨다. 변경 전 대기 판단은 실행하지 않는다.
- `expires_at`은 시간대가 포함된 현재부터 최대 300초의 시각이다. 큐 대기 중 만료되면 전송하지 않는다.
- 매수/매도 수량은 1~100000의 정수다. 시장가는 `limit_price:null`, 지정가는 양의 가격을 넣는다.
- 주문 금액 한도는 지정가의 경우 제출 가격과 최신 시세 중 큰 값 × 수량으로 확인한다. 모의/실전 시장가는 KRX 상한가 × 수량을 사용해 금액을 보수적으로 제한하며 상한가가 없으면 차단한다. 수수료/세금은 이 주문대금 한도에 포함하지 않는다. 실제 매수 가능액·매도 가능 수량·보유 비중은 기존 실행 엔진에서 추가 검사한다.
- `source`와 `reason`을 기록한다. JSON에서 서버 한도·전략 버전·설정을 덮어쓸 수 없다.
- 신규 매수 중지는 매수만 막고, 외부 자동 주문 중지는 외부 매수/매도를 모두 막는다. 이미 접수된 주문 취소는 별도다.

## 1. 로컬 샘플로 연결 확인

다음은 저장소 루트의 PowerShell에서 실행한다. 샘플 서버와 작업자를 먼저 시작한다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

화면에서 허용 종목을 입력하고 설정을 저장한다. 코드 목록은 화면 또는 `GET /api/trading/instruments`로 확인한다. 이 모드의 가격과 계좌는 합성 값이다.

HTTP로도 설정할 수 있다. API의 `version`을 조회한 뒤 변경 JSON에 `expected_version`으로 넣는다.

```powershell
$base = 'http://127.0.0.1:8000'
$state = Invoke-RestMethod "$base/api/trading"
$policy = @{
    expected_version = $state.control.version
    enabled = $true
    allowed_symbols = @('005930')
    max_order_value = '100000'
    max_position_pct = 20
    max_invested_pct = 50
}
Invoke-RestMethod "$base/api/trading/control" -Method Post -ContentType 'application/json' -Body ($policy | ConvertTo-Json)
```

AI 연결을 대신할 샘플 판단이다. 자동 재시도 루프에서 UUID를 매번 새로 만들지 않는다.

```powershell
$decision = @{
    client_request_id = [guid]::NewGuid().ToString()
    symbol = '005930'
    side = 'buy'
    quantity = 1
    order_type = 'market'
    limit_price = $null
    source = 'local-flow-check'
    reason = '로컬 연결 흐름 확인'
    expires_at = [DateTimeOffset]::UtcNow.AddMinutes(2).ToString('o')
}
$body = $decision | ConvertTo-Json
Invoke-RestMethod "$base/api/trading/signals/preview" -Method Post -ContentType 'application/json' -Body $body
Invoke-RestMethod "$base/api/trading/signals" -Method Post -ContentType 'application/json' -Body $body
Invoke-RestMethod "$base/api/trading/signals/$($decision.client_request_id)"
```

작업자가 처리한 뒤 `filled`를 확인한다. 체결 전에는 `registered`/`acknowledged`일 수 있다. 같은 본문을 다시 보내도 동일 주문을 반환한다. 로컬 매도 확인은 매수 체결을 확인한 후 새 판단 ID·새 만료 시각·`side:'sell'`로 제출한다. 주문 상세에서 체결 수량과 잔고 대조를 확인한다.

## 2. AI나 외부 프로그램에서 사용

1. `GET /api/trading`으로 연결 허용, 한도, 작업자 상태를 확인한다.
2. 종목 목록·시세·계좌를 읽는다. 자료가 없거나 오래됐으면 판단을 실행하지 않는다.
3. AI 결과를 지정한 JSON 계약으로 변환한다. 자유 텍스트를 주문 코드로 실행하지 않는다. 관망 결정이면 주문 요청을 만들지 않는다.
4. 하나의 결정 ID와 본문을 저장하고 검사 API를 호출한다. 검사는 전송하지 않으며 실행 성공을 보장하지 않는다.
5. 허용 범위에서 `POST /api/trading/signals`를 호출한다. 제출 자체가 자동 주문 요청이며 UI에서 건별 승인을 기다리지 않는다.
6. 같은 판단 ID로 결과를 조회한다. HTTP 202/접수는 체결을 뜻하지 않는다. 실제 체결은 주문 상태와 계좌 대조로 확인한다.
7. 통신 오류/결과 불명이면 새 ID로 중복 요청하지 않는다. 동일 ID 조회 또는 동일 본문 재요청으로 기존 기록을 확인한다. 기존 판단은 재요청해도 다시 큐에 넣지 않는다.

AI 서비스 키는 AI를 호출하는 외부 프로그램에서 관리한다. 이 도구에 KRX·DART·FRED 키나 AI 키를 설정할 필요는 없다. 한투 모의/실전 연결을 쓰는 경우에만 선택한 모드의 키/계좌 설정이 필요하다. 로컬 API 접근 권한은 로컬 사용자/프로그램의 권한으로 취급하며 원격 공개 서버로 운영하지 않는다.

### CLI 설정 파일

`.local/agent-control.json` 예시다. 현재 버전은 `trading-status`로 먼저 확인한다.

```json
{
  "expected_version": 1,
  "enabled": true,
  "allowed_symbols": ["005930"],
  "max_order_value": "100000",
  "max_position_pct": 20,
  "max_invested_pct": 50
}
```

```powershell
.\scripts\manage.ps1 trading-status
.\scripts\manage.ps1 trading-control "$PWD\.local\agent-control.json"
.\scripts\manage.ps1 signal-preview "$PWD\.local\signal.json"
.\scripts\manage.ps1 signal-submit "$PWD\.local\signal.json"
.\scripts\manage.ps1 signal-status '판단 UUID'
```

CLI 제출도 큐 등록이다. 통합 실행기/작업자가 켜져 있어야 브로커로 전송한다. 같은 시뮬레이션 모드에서 큐를 한 차례 비우려면 `scripts/worker.ps1 -Once`를 사용한다. 모의계좌 CLI는 모든 명령에 `--mode kis_paper`를 지정한다.

## 3. 한투 국내 모의 연결

이 절은 국내 모의계좌 준비다. 국내 실전계좌는 아래 별도 절차를 따른다. 미국 상장 ETF 주문은 현재 지원하지 않는다. 국내 모의 주문 전에는 다음을 준비한다.

- 루트 `.env`의 `KIS_PAPER_APP_KEY/APP_SECRET/ACCOUNT_NUMBER/ACCOUNT_PRODUCT_CODE`.
- `doctor`와 `paper-read-check`로 모의 계좌 조회·대조.
- `master-download`와 검토된 종목 분류, 개장/휴장 시각을 가진 달력 가져오기. 자세한 가져오기 계약은 [기존 데이터 준비 안내](user-actions.md#4-시장역사-자료-준비)에서 종목/달력 부분을 참고한다.
- 모의 주문을 직접 실행할 때만 `KIS_PAPER_ORDERS_ENABLED=true`로 변경하고 프로세스를 재시작.
- 화면에서 허용 종목·한도·외부 자동 주문을 설정하고 신규 매수 허용 여부 확인.

외부 주문 경로는 최신 REST 시세와 계좌 조회를 사용하며 기존 연구의 후보 감시/실시간 시세 구독을 요구하지 않는다. 장 시간, 계좌 대조 불일치, 미확정 주문, 매수/매도 가능 수량과 보유 한도는 그대로 적용한다. 연구 전략의 승인 게이트와 외부 주문의 실행 검사를 혼동하지 않는다.

## 4. 중지·오류·복원

- 외부 자동 주문 중지: 이후 실행 전 검사에서 외부 판단 차단. 이미 접수된 주문은 유지.
- 신규 매수 중지: 신규 매수만 차단. 보유 매도/취소는 별도 요청.
- HTTP 409: 설정 버전 또는 판단 ID 내용 충돌. 기존 자료를 조회하고 충돌 이유를 확인.
- HTTP 422/차단: JSON, 허용 종목, 금액, 만료, 장 시간/잔고 등의 이유를 확인. 수정한 새 결정은 새로운 ID를 사용.
- `unknown`/`sending`: 기존 주문 API와 브로커 대조로 확인. 같은 주문을 새 ID로 보내지 않음.
- 큐가 실패하고 주문이 미전송 등록 상태이면 해당 판단은 자동 재큐잉하지 않음. 오류를 해결하고 아직 주문되지 않았음을 확인한 후 새 판단을 생성.
- 화면에서 통신 결과가 불명확하면 ID를 브라우저에 보존하고 조회부터 진행. 새로고침 후에도 같은 계좌의 판단 ID로 복구.

백업/복원은 기존 관리 명령을 사용한다.

```powershell
.\scripts\manage.ps1 backup
.\scripts\manage.ps1 restore '백업 bundle 경로' '존재하지 않는 새 복원 폴더'
```

복원은 외부 자동 주문·신규 매수·기존 전략 배포를 중지하고 버전을 변경한다. 미종료 주문은 결과 확인 대상으로 격리하며 오래된 대기 작업을 재전송하지 않는다. 계좌 대조와 미확정 주문 해결 후 새 판단으로 재개한다. `.env`는 백업에 넣지 않고 직접 옮긴다.


## 실전계좌 연결

`kis_live`는 사용자의 2026-09-30 구현 지시에 따라 추가한 **국내 현금 주문** 모드다. 실전 연결 구현과 실제 계좌에서의 체결 검증은 별개이며 개발 검증은 전부 로컬/MockTransport에서 진행한다.

### 설정

루트 `.env`에 다음 항목을 직접 설정한다. 모의 필드 또는 범용 `KIS_APP_KEY`로 자동 대체하지 않는다.

```dotenv
KIS_LIVE_APP_KEY=
KIS_LIVE_APP_SECRET=
KIS_LIVE_ACCOUNT_NUMBER=
KIS_LIVE_ACCOUNT_PRODUCT_CODE=01
KIS_LIVE_ORDERS_ENABLED=false
```

`INVEST_WORKSPACE=execution`에서만 실전 모드를 지원한다. 실전 계좌에 과거 연구 전략을 배포하는 프로필은 허용하지 않는다. 기존 `.env`나 사용자의 키를 소스/공유 문서에 복사하지 않는다.

```powershell
.\scripts\manage.ps1 --mode kis_live doctor
.\scripts\manage.ps1 --mode kis_live broker-read-check
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Mode kis_live
```

`doctor`는 설정 상태만 검사한다. `broker-read-check`는 조회/대조만 수행한다. `run.ps1 -Mode kis_live`로 실행해도 `KIS_LIVE_ORDERS_ENABLED=false`이면 주문은 전송하지 않는다.

주문을 사용할 때는 실전 종목 마스터·개장 달력·계좌 대조를 준비하고 `KIS_LIVE_ORDERS_ENABLED=true`로 프로세스를 재시작한다. 화면의 외부 자동 주문 허용과 신규 매수 허용도 별도 설정이다. API/CLI 계약은 로컬/모의와 같다. `signal-submit` 및 `/api/trading/signals`는 허용 상태에서 건별 추가 승인 없이 실전 주문을 작업자에 요청한다.

### 모드 분리와 확인

- 실전 REST 주소는 `https://openapi.koreainvestment.com:9443`, 모의는 `https://openapivts.koreainvestment.com:29443`으로 고정한다. 모드와 주소가 다르면 설정을 거부한다.
- 실전 운영 DB는 `.local/runtime-kis_live.db`, 계좌 namespace는 `kis_live:계좌해시`다. 모의 기록과 같은 계좌 번호/DB 파일을 사용해도 namespace가 섞이지 않는다.
- 실전 기본 토큰/호출 조정 DB는 `.local/kis-live-coordination.db`다. 공유 파일 경로를 지정해도 토큰/호출 scope는 모의와 다르다.
- 시장 마스터/달력은 공통 KRX 자료다. 모의 계좌의 주문/현금/보유 상태를 실전 계좌로 복사하지 않는다.
- 화면과 `/api/context`, `/api/health`는 실전 모드와 실전 주문 활성화 설정을 구분해 표시한다.
- 시장가/지정가 현금 매수·매도, 정정·취소, 최근/과거 체결 조회와 계좌 대조를 지원한다. 신용·미수·공매도·NXT/SOR·해외 주문은 제공하지 않는다.
- 실전 체결 감시는 분 단위 REST 대조와 제출 직후 대조다. 실전 WebSocket 체결통보는 이번 구현에 포함하지 않는다. 응답 유실 주문은 재전송하지 않는다.

### 공식 계약 근거

2026-09-30 한국투자증권의 공식 공개 예제를 확인했다. 실전 매수/매도는 `TTTC0012U/TTTC0011U`, 정정·취소는 `TTTC0013U`, 매수 가능액은 `TTTC8908R`, 잔고는 `TTTC8434R`, 최근/과거 체결은 `TTTC0081R/CTSC9215R`을 사용한다. KRX 거래소를 명시한다.

- [현금 주문 공식 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_cash/order_cash.py)
- [정정·취소 공식 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_rvsecncl/order_rvsecncl.py)
- [잔고 조회 공식 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_balance/inquire_balance.py)
- [체결 조회 공식 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py)

실전 키의 등록·API 권한·장중 실제 접수/체결과 브로커 정정 회계/비용은 실제 계좌 환경에서 별도로 확인해야 한다. 이 구현 작업에서 실전 주문을 전송하거나 `.env`의 주문 활성화를 변경하지 않는다.
