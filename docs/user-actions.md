# 사용자가 할 일 — 시작부터 실제 한 달 관찰까지

이 문서는 계정·키·승인 자료·실제 장중 확인처럼 사용자의 준비가 필요한 항목을 한곳에 모은다. 기준일은 **2026-09-27**이다. 코드와 오프라인 검증을 완료한 것, 실제 외부 자료/계좌로 확인한 것, 한 달이 경과한 것은 서로 다른 상태다.

사무실 등 새 PC의 설치·코드 동기화·기존 데이터 이전은 [다른 PC에서 시작하고 이어가기](workstation-setup.md)를 먼저 따른다. 아래 키/연결 준비 상태는 기존 PC의 당시 기록이며, 새 클론에는 `.env`와 `.local`이 포함되지 않는다.

## 1. 지금 바로 실행하기

PowerShell에서 현재 PC에 클론한 `invest-lab` 저장소 루트로 이동한 후 실행한다. 특정 PC 경로를 사용할 필요는 없다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

<http://127.0.0.1:8000>을 연다. 기본은 `local_simulation`이며 샘플 계좌의 매수/매도·전략·연구 기능을 외부 키 없이 확인할 수 있다. 연구실에서 합성 데이터셋을 만든 뒤 백테스트/Ridge/로지스틱/비교 실험을 실행할 수 있다. 합성 결과는 실제 투자 성과로 표시하지 않는다.

`uv`와 Node.js/npm이 필요하다. Node 24.x의 24.15.0 이상을 권장한다. 최초 실행은 잠금 파일에 맞춰 설치/빌드한다. 이후 코드가 그대로일 때 설치를 생략하려면 `-SkipSetup`을 붙인다. pull한 뒤에는 설치/빌드를 다시 한다. 창에서 `Ctrl+C`를 누르면 API와 함께 시작된 작업자를 종료한다. 브라우저만 닫으면 작업자는 계속 실행하며, PC가 꺼지거나 절전 상태이면 실행되지 않는다.

이하 명령은 **저장소 루트**에서 실행한다. 관리 스크립트가 내부에서 작업 디렉터리를 바꾸므로 JSON 경로는 `"$PWD\.local\inputs\파일.json"`처럼 절대 경로로 넘긴다.

## 2. 한투 모의 연결 — 각 PC에서 설정 확인

2026-09-27 기존 PC의 `.env`를 표준 필드로 정리했다. 새 PC에는 `.env`를 별도로 준비하고 아래 진단을 실행한다. 값 전체를 채팅이나 Git에 게시하지 않는다. 정리 전 원본은 당시 PC의 `.local/config-backups/.env.legacy-20260927`에 보관했으며 Git에 없다.

| 필드 | 용도/현재 준비 |
| --- | --- |
| `KIS_PAPER_APP_KEY` / `KIS_PAPER_APP_SECRET` | 모의 API 키, 기존 설정 유지 |
| `KIS_PAPER_ACCOUNT_NUMBER` | 모의 계좌 앞 8자리 |
| `KIS_PAPER_ACCOUNT_PRODUCT_CODE` | 계좌 뒤 상품 코드 2자리, 현재 조회 확인값 `01` |
| `KIS_PAPER_HTS_ID` | 모의 실시간 체결통보 식별자, 설정 존재 |
| `KIS_PAPER_ORDERS_ENABLED` | 현재 `false`. 모의 주문 전송을 준비한 시점에만 `true`로 변경 |
| `KIS_REQUEST_INTERVAL` | 현재 기본 1.25초. 여러 작업이 공통 호출 예산을 공유 |

앱은 모의 REST/WS 주소만 허용한다. 실전 키로 주소를 바꾸는 운용은 지원하지 않는다. 신청/재발급이 필요하면 [한국투자 Open API 포털](https://apiportal.koreainvestment.com/)의 모의투자 이용 절차를 확인한다.

```powershell
.\scripts\manage.ps1 --mode kis_paper doctor
.\scripts\manage.ps1 --mode kis_paper paper-read-check
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Mode kis_paper
```

`doctor`는 설정의 유무를 표시하며 키 값을 출력하지 않는다. `paper-read-check`는 현재가·계좌·주문 이력을 조회하고 대조하며 **주문을 전송하지 않는다**. 2026-09-27 02:55 KST에 앱 어댑터에서 읽기 성공, 대조 `matched`, 브로커 주문 0건/전송 0건을 확인했다. 일요일 검사여서 장중 주문·실시간 체결 수신을 확인한 것은 아니다.

03:10 KST에는 모의 WS를 20초 연결해 구독 ACK와 계좌 대조도 확인했다. 오류/재접속은 없었고 실제 시세·체결통보는 0건이었다. 장중 데이터 수신·체결 확인은 아래 실제 모의 검증에서 진행해야 한다.

## 3. 재무·매크로·KRX 키 신청

2026-09-27 검증 당시 다음 세 키는 미설정이었다. 이후 2026-09-29 사무실 PC에서는 KRX 자료 수집과 DART 회사코드/공시 원문 연결을 확인했다. 새 PC의 키/승인/접속 상태까지 보장하지 않으며 `.env`는 사용자가 별도로 이전한다. 현재 PC의 설정은 `doctor`로 확인한다. 없는 키는 각 서비스에서 직접 신청한 뒤 `.env`에 해당 **필드만 추가**한다. 기존 한투 값이나 파일 전체를 교체하지 않는다. FRED 연결 성공은 이번 인계에서 확인하지 않았다.

```dotenv
DART_API_KEY=발급받은_키
FRED_API_KEY=발급받은_키
KRX_API_KEY=발급받은_키
```

| 서비스 | 신청 위치 | 이번 서비스에서 사용하는 범위 |
| --- | --- | --- |
| OpenDART | [공식 사이트](https://opendart.fss.or.kr/)의 인증키 신청/관리 | 회사 코드 매핑, 공시 목록, 접수번호별 원문 XBRL, 최초 공시/정정 공개 시각 |
| FRED | [API key 안내](https://fred.stlouisfed.org/docs/api/api_key.html)에서 로그인 후 신청 | `VIXCLS`, `VXNCLS`, `DEXKOUS`, `DGS2`, `DGS10`, `BAMLH0A0HYM2` 및 빈티지 |
| KRX OPEN API | [공식 사이트](https://openapi.krx.co.kr/)의 인증키 발급 및 서비스 이용 신청 | 유가증권 일별매매정보/종목기본정보, ETF 일별매매정보, KOSPI 시리즈 일별시세 |

KRX는 키 발급과 개별 API 이용 승인을 구분한다. 승인되지 않은 서비스나 원천에서 제공하지 않는 필드는 자동으로 채우지 않는다. 키를 추가한 뒤 `doctor`로 유무를 확인하고 실행 중인 프로세스를 다시 시작한다. 키가 생겨도 역사 유니버스·배당 원문·ETF 분류가 자동으로 완비되는 것은 아니다.

## 4. 시장/역사 자료 준비

### 필요한 자료

| 자료 | 필요한 이유와 완료 조건 |
| --- | --- |
| 개장/휴장 달력 | 연구/운영 범위의 **모든 날짜**가 필요하다. 토·일/공휴일도 `is_open:false`로 입력한다. 개장일은 실제 개장·폐장 시각을 포함한다. 평일을 자동 개장일로 추측하지 않는다. |
| 현재 마스터 | 코스피 보통주와 운용할 국내 상장 ETF를 구분한다. 현재 ST 목록에는 우선주/비거래 항목이 섞일 수 있으므로 실제 사용 목록을 확인한다. |
| ETF 메타데이터 | 국내/미국지수 등 상품 분류, 기초지수, 통화/벤치마크 통화, 환헤지, 레버리지/인버스, 보수, NAV 시점, 분배금 이력이 필요하다. 이름만으로 분류하지 않는다. |
| 원주가·수정주가 | 확정 KRX 정규장 OHLCV와 거래대금, 원천·공개 시각·수집 시각을 보존한다. 수정 일봉만으로 과거 원주가/기업행동을 복원하지 않는다. |
| 역사 유니버스 | 각 과거 세션의 구성종목/상장·상폐·거래 가능 상태. 오늘 살아 있는 종목 목록을 과거에 복사하지 않는다. |
| 기업행동 | 분할 비율, 배당/분배금 권리락일·금액·지급일, 상폐 정산가. 자료가 없다는 것과 사건이 없었다는 것을 구분한다. |
| 벤치마크 | 상품에 맞는 지수의 동일 날짜·통화·가격수익/총수익 기준. 미국 지수의 한국 의사결정 시점 이전 공개 여부도 확인한다. |
| 재무 원문/매크로 빈티지 | 공시 접수번호별 원본·정정, 실제 공개 시각, 계정·기간·연결/별도 범위. 현재 수정본을 과거에 미리 사용하지 않는다. |

현재 공개 마스터 다운로드에서 ST 915개, 현재 보통주 804개/거래 가능 보통주 757개와 미분류 ETF 1,175개를 확인했다. 이는 당시 현재 마스터 결과이며 **5년 역사 coverage를 확보했다는 뜻이 아니다**. 목록은 이후 변경될 수 있다.

공식 마스터를 새로 받으려면:

```powershell
.\scripts\manage.ps1 --mode kis_paper master-download
```

ETF를 검토했다면 `.local/inputs/etf-classifications.json`에 `{"종목코드":"etf_kr"}` 또는 `"etf_us"` 매핑을 넣고 다음처럼 실행한다. 예: `{"069500":"etf_kr","379800":"etf_us"}`는 형식 예시이며 실제 상품 확인 후 사용한다. `0048K0` 같은 영숫자 코드를 정수로 변환하지 않는다.

```powershell
.\scripts\manage.ps1 --mode kis_paper master-download --classifications "$PWD\.local\inputs\etf-classifications.json"
```

### 가져오기 파일 형식

아래 날짜·금액은 **형식 예시**다. 실제 운영에는 승인된 원천 자료로 교체해야 한다. 예시 자료를 검증 완료로 표시하지 않는다. 입력/원본 파일은 Git 제외 경로 `.local/inputs/`에 둔다.

`calendar.json`은 `import_calendar` 인자를 그대로 담는다. 실제 전체 범위의 개장/휴장 행을 모두 넣는다.

```json
{
  "source": "synthetic_example_calendar",
  "as_of": "2026-09-20T00:00:00+00:00",
  "rows": [
    {"session":"2026-09-27","is_open":false},
    {"session":"2026-09-28","is_open":true,
     "open_at":"2026-09-28T00:00:00+00:00","close_at":"2026-09-28T06:30:00+00:00"}
  ]
}
```

```powershell
.\scripts\manage.ps1 --mode kis_paper import-market calendar "$PWD\.local\inputs\calendar.json"
```

수동 마스터는 `{"source":"검토한 원천","as_of":"공개시각 ISO","rows":[...]}` 형식이다. 행에는 `symbol` 문자열, `name`, `kind`(`kospi/etf_kr/etf_us`), `sector`를 넣고 확인한 `listed_on`, `delisted_on`, `valid_from`, `valid_to`를 선택적으로 보존한다. 명령은 `import-market instruments 파일경로`다.

원주가 파일은 다음 형식이다. `available_at`은 원천 근거가 있는 실제 사용 가능 시각, `recorded_at`은 수집 시각이다. 공개 시각을 모르면 수집 시각으로 기록하고 역사 PIT 검증에 사용하지 않는다.

```json
{
  "symbol":"005930","adjusted":false,"source":"synthetic_example_bar",
  "available_at":"2026-09-28T07:00:00+00:00",
  "recorded_at":"2026-09-28T07:05:00+00:00",
  "rows":[{"session":"2026-09-28","open":"100","high":"105","low":"99",
           "close":"103","volume":1000,"turnover":"102000"}]
}
```

명령은 `import-market bars 파일경로`다. 다른 공개 시각을 가진 수정본은 별도 import하며 기존 행을 덮어쓰지 않는다.

한투 일봉 수집은 마스터와 달력이 먼저 준비돼야 한다. 수집 작업 JSON의 실제 형태는 다음과 같다. 원하는 종목 목록/기간으로 바꾼다.

```json
{"symbols":["005930"],"start":"2026-01-01","end":"2026-09-25","adjusted":false}
```

```powershell
.\scripts\manage.ps1 --mode kis_paper enqueue collect_market --payload "$PWD\.local\inputs\collect-raw.json"
```

수정주가 수집은 같은 형식에서 `adjusted:true`로 별도 작업을 만든다. 작업자가 실행 중이어야 큐를 처리한다. API 호출 제한 때문에 전체 종목의 장기 이력은 시간이 걸린다. 휴장/일봉 결손을 감지하면 멈추며 같은 범위로 다시 요청하면 저장 체크포인트를 사용한다. 현재 API로 오늘 받은 과거 자료가 그 과거 시점에 알려졌다는 증거가 되지는 않는다.

### 연구 데이터셋 만들기

검토한 연구 JSON을 그대로 가져오려면:

```powershell
.\scripts\manage.ps1 --mode kis_paper import-dataset "$PWD\.local\inputs\research-dataset.json"
```

기본 객체에는 `name`, `source_kind`(`imported/provider/synthetic`), `provenance`, `sessions`, `instruments`, `bars`, `universe`, `actions`, `financial_facts`, `macro`, `benchmarks`, `etf_metadata`가 들어간다. 종목 식별자는 문자열이며, 시계열/원문 행은 `available_at`과 `ingested_at`을 포함한다. 일봉에는 `price_basis:raw|adjusted|total_return`, `final:true`, `venue:KRX`, `session_type:regular`가 필요하다.

주요 추가 행의 실제 필드:

| 테이블 | 필드 예시/조건 |
| --- | --- |
| `universe` | `session`, `symbols:["005930",...]`, `available_at`, `ingested_at` |
| `actions` 분배금 | `event_id`, `symbol`, `type:"distribution"`, `effective_date`, `payment_date`, `amount`, 두 시각 |
| `actions` 분할/상폐 | `type:"split"` + `ratio`, 또는 `type:"delisting"` + `settlement_price`. 원문/유효일/공개 시각 필요 |
| `etf_metadata` | `symbol`, `benchmark_id`, `currency`, `benchmark_currency`, `benchmark_return_basis`, `leverage:1`, `inverse:false`, `hedged`, `annual_fee_pct`, 두 시각. 총수익 지수가 별도면 `total_return_benchmark_id` |
| `financial_facts` | `symbol`, `receipt_id`, `account`, `scope:CFS|OFS`, `attribution:total`, `currency:KRW`, `period_type:quarter|ytd|instant`, `period_start`, `period_end`, `value`, 두 시각 |
| `macro` | `series`, `observation_date`, `vintage_date`, `value`, 두 시각 |

저장된 market DB를 연구 자료로 묶는 `build-dataset` 파일은 아래 인자를 사용한다. `historical_universe` 등에는 검토한 행을 직접 넣는다. 아래 빈 배열은 준비되지 않은 상태를 설명하는 것으로 검증을 통과시키지 않는다.

```json
{
  "start":"2026-01-01","end":"2026-09-25","name":"첫 연구 자료",
  "source_kind":"imported",
  "historical_universe":[],"benchmarks":[],"actions":[],
  "financial_facts":[],"macro":[],"etf_metadata":[],"extra_instruments":[],
  "provenance":{"source":"원천/승인자료 식별자",
    "historical_universe_verified":false,"calendar_verified":false,
    "corporate_actions_verified":false}
}
```

```powershell
.\scripts\manage.ps1 --mode kis_paper build-dataset "$PWD\.local\inputs\build-dataset.json"
```

`extra_instruments`에는 일봉 마스터에 없는 벤치마크 식별자를 `{"symbol":"KOSPI","name":"KOSPI","kind":"benchmark"}`처럼 등록한다. 날짜 목록의 모든 세션, 원주가, 당시 유니버스, 기업행동 및 `verification_report` 근거가 충족돼야 `coverage.pit_ready`가 가능하다. `*_verified`를 단지 통과시키려고 `true`로 바꾸지 않는다. 해당 값은 제출 자료의 확인 기록이며 앱이 독립적으로 원문을 인증했다는 뜻이 아니다.

무료 원천으로 채울 수 없는 구간은 정확한 결손을 확인한 뒤 승인된 자료를 추가한다. 유료 자료 구매를 자동으로 진행하지 않는다. 상세 정의는 [데이터 계약](paper-investment-data-contracts.md)을 참고한다.

## 5. 원천 수집과 연구 실행

원천 수집도 작업자 큐로 실행한다. 키는 `.env`에서 읽고 작업 JSON에는 넣지 않는다. 각 예시의 접수번호/회사 코드/날짜/데이터셋 ID는 실제 값으로 교체한다.

FRED 작업 예시:

```json
{
  "provider":"fred","operation":"observations","base_dataset_id":"만든_데이터셋_ID",
  "parameters":{"series":"VIXCLS","start":"2026-09-01","end":"2026-09-25",
                "sessions":["2026-09-28","2026-09-29"]}
}
```

`sessions`는 위 두 날짜만 복사하는 값이 아니라 요청 범위와 발표 지연을 포함한 **검증된 KRX 세션 전체 목록**이다. 빈티지/공개 지연을 보존하며 없는 값을 전일/미래 값으로 채우지 않는다.

DART는 `operation:"companies"`로 회사코드 매핑, `operation:"filings"`로 `corp_code/start/end/sessions`의 최초 공시·정정 목록을 수집한 뒤 접수번호 원문을 가져온다. 원문 재무의 작업 형태는 다음과 같다.

```json
{
  "provider":"dart","operation":"financial_facts","base_dataset_id":"만든_데이터셋_ID",
  "parameters":{"receipt":"실제14자리접수번호","symbol":"005930",
                "filing_date":"2026-08-14","report_code":"11012","scope":"CFS",
                "sessions":["2026-08-18","2026-08-19"]}
}
```

보고서 코드 `11011/11012/11013/11014`는 각각 사업/반기/1분기/3분기다. 실제 공시의 보고서 코드와 공개일을 사용한다. 회사 고유 확장 계정이나 중복/분할 문맥은 임의로 합하지 않고 제외 이유를 반환하므로 매핑 검토가 필요할 수 있다. 최신 재무 조회 응답은 `revised_only`이며 원문 PIT 자료로 자동 승격하지 않는다.

KRX 작업은 `provider:"krx"`, `operation:"daily"`, `parameters:{"service":"kospi_daily","session":"YYYY-MM-DD"}` 형태다. 지원 service는 `kospi_daily`, `kospi_master`, `etf_daily`, `kospi_index`다. 지수 이름과 내부 종목 식별자가 다르면 `symbol_map`을 명시한다. 현재 마스터 분류에는 검토한 `instrument_overrides`가 필요하다. 공개 시각 근거를 명시하지 않은 과거 KRX 응답은 수집 시각만 가진 탐색 자료로 남는다.

```powershell
.\scripts\manage.ps1 --mode kis_paper enqueue collect_source --payload "$PWD\.local\inputs\source-job.json"
```

`base_dataset_id`를 넣으면 원천 행·원문/hash를 병합한 **새 데이터셋**을 만든다. 빼면 수집 결과만 저장한다. 연구실에서 새 ID와 결손 사유를 확인한 뒤 실험을 실행한다.

```json
{
  "kind":"compare","dataset_id":"검토한_데이터셋_ID",
  "parameters":{"asset_kind":"kospi","horizon":10,"return_basis":"price","seed":42,
                "commission_bps":1.5,"sell_tax_bps":0,"slippage_bps":5}
}
```

```powershell
.\scripts\manage.ps1 --mode kis_paper enqueue research_experiment --payload "$PWD\.local\inputs\experiment.json"
```

위 비용은 형식용 연구 가정이며 실제 요율로 바꿔야 한다. `asset_kind:"etf"`로 ETF 학습군을 분리할 수 있다. 기본 `return_basis:"price"`는 배당을 제외한 가격수익을 동일 기준 지수와 비교한다. `"total_return"`은 현금 분배를 포함하고 총수익 벤치마크를 요구한다. ETF 분배금 coverage가 검증되지 않으면 총수익/추적오차를 완성된 값처럼 보여주지 않는다.

모델은 후보로 등록한다. 시간 분할·자료/피처/레이블 coverage와 비용 후 baseline 비교를 통과하지 않으면 선택할 수 없고 shadow(관찰)로 비교한다. 최종 평가를 보고 다시 매개변수를 고르면 같은 최종 구간을 새 미사용 평가처럼 해석하지 않는다. 새 평가 기간을 확보해야 한다.

## 6. 실제 장중 모의 한 사이클과 한 달 관찰

현재 실제 주문/정정/취소와 WebSocket 장중 검증은 남아 있다. 다음 순서로 확인한다.

1. 실제 개장/폐장 달력, 대상 종목/메타데이터와 최신 가격을 준비한다. `paper-read-check`로 계좌 대조가 일치하는지 확인한다.
2. 실제 **모의계좌**로 한 사이클을 진행할 준비가 되면 `.env`의 `KIS_PAPER_ORDERS_ENABLED=true`로 바꾸고 재실행한다. 화면의 신규 진입과 자동 운용 활성화는 각각 별도다. 처음에는 자동 운용을 끈 채 수동 모의 한 사이클을 확인한다.
3. 정규장에 유효한 가격/수량으로 매수 → 접수/체결 → 보유 → 매도 → 체결 → 현금/보유 대조를 확인한다. 미체결 취소와 가격 정정도 지원 유형별로 확인한다.
4. 체결통보 수신, 연결 끊김/재연결 뒤 대조, 다음 세션 재시작을 확인한다. 결과 불명은 성공으로 추정하거나 같은 주문을 다시 보내지 않는다. 운영 화면의 미확정 내역과 실제 브로커 이력을 확인한다.
5. 검증된 자료·규칙/모델 버전을 선택하고 자동 운용을 활성화한다. 당일 전체 분석 후 후보 10~20개와 보유 종목을 감시한다. 외부 HTS의 수동 보유를 자동 전략 소유로 임의 배정하지 않는다.

실제 장에서 부분 체결/취소 경합/응답 유실을 억지로 만들 필요는 없다. 장애 fixture 검증과 실제 관찰된 결과를 구분해 기록한다. 현재 실제 G2와 G4 완료 증거는 아직 없다.

한 달 운용은 시작일·종료일과 최소 거래일을 먼저 고정한다. 아래 기간은 예시다.

```powershell
.\scripts\manage.ps1 --mode kis_paper observe 2026-10-05 2026-11-04 --minimum-sessions 20
```

실제 시간 경과, 해당 거래 세션의 일별 보고, 시작 자산·입출금/초기화·무거래 사유·오류·전략/모델 변경·미확정 주문을 확인한다. 합성 시계를 한 달 진행한 테스트는 실제 한 달 관찰 G5의 대체물이 아니다. 일/월 보고서가 있어도 빠진 세션이나 설명되지 않은 잔고 차이가 있으면 완료로 보지 않는다.

모의계좌 재신청/초기화, 입출금, 외부 거래로 기준선이 달라졌다면 먼저 원인을 확인한다. 미확정·접수/등록 주문이 남아 있는 동안 기준선을 바꾸지 않는다. 확인한 뒤에만:

```powershell
.\scripts\manage.ps1 --mode kis_paper rebaseline --reason '확인한 계좌 초기화 또는 외부 변동 사유' --confirm
```

정정/취소 명령의 결과 불명은 브로커 확인 근거 JSON으로 `resolve-command 명령ID 근거파일`에 기록할 수 있다. 파일은 `outcome:"acknowledged"|"rejected"`, `receipt:{"broker_order_id":"확인한 접수번호"}`, `reason`을 포함한다. 접수 인정은 해당 거래일/종목/방향의 실제 이력으로 다시 확인하며, 이 명령은 주문을 재전송하지 않는다.

### 미확정 주문·정정 의미·실제 비용 확인

접수번호가 없는 `unknown` 주문은 `resolve-order`로 브로커 확인 근거를 연결한다. 실제 이력을 확인한 뒤 `order-evidence.json`에 다음 형태를 작성한다.

```json
{"outcome":"matched","operator_checked":true,
 "receipt":{"broker_order_id":"실제접수번호"},"reason":"확인한 거래일/종목/방향/수량 근거"}
```

```powershell
.\scripts\manage.ps1 --mode kis_paper resolve-order '화면의로컬주문ID' "$PWD\.local\inputs\order-evidence.json"
```

실제로 전송되지 않았음을 확인했고 해당 날짜에 유사 주문도 없을 때만 `outcome:"not_submitted"`로 기록한다. 앱이 브로커 이력을 다시 확인하며, 이 경우 과거 의도를 만료시킨다. 어느 경우에도 이 절차에서 주문을 재전송하지 않는다. 단지 주문을 찾지 못했다는 이유만으로 미전송이라고 입력하지 않는다.

한투 정정 후 원주문/자식 주문의 이동·체결·취소 수량 의미는 실제 계좌에서 확인할 때까지 `UNKNOWN`을 유지한다. 실제 정정 접수 근거와 연결 주문들을 확인한 뒤 다음 명령으로 계좌의 의미 검증 기록을 남길 수 있다.

```powershell
.\scripts\manage.ps1 --mode kis_paper verify-amendment '원주문의로컬ID' --reason '브로커 원주문/정정 주문과 수량 대조 근거' --confirm-semantics
```

이 명령도 실제 주문 그래프와 확인한 정정 접수번호를 검사한다. 문서의 필드 이름만 보고 정정 회계가 검증됐다고 가정하지 않는다.

한투 조회에서 수수료가 없으면 0원으로 꾸미지 않는다. 실제 거래 명세에서 확정한 비용은 `costs.json` 배열로 기록한다. `order_id`는 앱의 로컬 주문 ID이며, 수량과 금액은 해당 주문의 누적 체결 합계와 정확히 일치해야 한다. 아래 값은 형식 예시다.

```json
[{"order_id":"화면의로컬주문ID","filled_quantity":1,"filled_amount":"10000",
  "fee":"2","source":"확인한 모의 거래명세 식별자","verified_by_user":true}]
```

```powershell
.\scripts\manage.ps1 --mode kis_paper record-costs "$PWD\.local\inputs\costs.json"
```

근거 기록 후 계좌 대조 작업이 다시 실행된다. 확인한 실제 비용과 연구의 가정 수수료는 서로 구분한다.

## 7. 백업·복구와 완료 판정

```powershell
.\scripts\manage.ps1 --mode kis_paper backup
.\scripts\manage.ps1 --mode kis_paper restore '반환된_bundle_절대경로' "$PWD\.local\restored-20261030"
```

백업은 현재 모드의 운영 DB, workspace, 연구 DB/artifact, 해당 시장 DB를 함께 저장한다. 모든 DB 쓰기 잠금 후 SQLite 백업과 hash/manifest 대조를 수행한다. 별도로 선택하지 않은 `.env`/토큰 파일을 bundle에 넣지 않는다. 다른 모드의 운영 DB도 필요하면 그 모드에서 별도로 백업한다.

복원은 **존재하지 않는 새 폴더**에만 한다. 실행 중인 원본 파일을 덮어쓰지 않는다. 계좌 namespace, DB 무결성/migration, 파일 hash를 검사한다. 복원한 진입과 자동 운용은 꺼지고, 미종료 주문은 `unknown`으로 격리되고, 대기/실행 중 작업은 중단된다. 복원 시점 이후에 브로커가 접수했을 가능성이 있으므로 과거 `registered` 의도도 자동 재전송하지 않는다.

원본 앱을 종료한 후 복원한 경로로 실행하려면 두 경로를 함께 지정한다. 복원 명령이 반환한 파일명을 확인한다.

```powershell
$env:INVEST_STATE_DIR = "$PWD\.local\restored-20261030"
$env:INVEST_DB_PATH = "$PWD\.local\restored-20261030\workspace.db"
.\scripts\manage.ps1 --mode kis_paper paper-read-check
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Mode kis_paper -SkipSetup
```

전체 브로커 이력/미체결/잔고 대조와 격리 주문 해결 후 새 자료로 분석하고, 운용 버전을 확인한 뒤 수동으로 재개한다. 단순히 진입 스위치만 켜서 복원 상태를 우회하지 않는다.

| 단계 | 현재 판단 |
| --- | --- |
| 앱/오프라인 계산·화면·장애 fixture | 구현·검증 결과는 릴리스 검증 기록에 남김 |
| G1 실제 읽기/시장 자료 | 한투 현재가·계좌 대조 성공. 전체 시장/달력/원천 coverage는 준비 필요 |
| G2 실제 모의 매수/매도·취소/정정·실시간 | 장중 확인 필요 |
| G3 실제 PIT 연구/재무·매크로·모델 | 키·원문/역사 자료 및 실제 데이터 실험 필요. 합성 실험은 별도 |
| G4 실제 연속 자동 운영/복구 | 오프라인 장애 검증과 실제 연속 세션 증거를 구분해 추가 필요 |
| G5 실제 한 달 | 기간을 정하고 실제 경과/세션 기록 필요 |

실전 거래 L01과 원격 배포 H01은 후속 범위다. 이번 모의 실험의 결과만으로 실전 전환을 자동 결정하지 않는다.
