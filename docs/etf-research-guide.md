# 독립 ETF 연구 실행 안내

SPY·QQQ 연구는 기존 국내 투자 모듈과 별도 진입점·저장소를 사용한다. 기존 KOSPI 전략, 국내 ETF 규칙, 모의 운용 DB와 한투 연결 설정을 바꾸지 않는다. 이번 구현의 사용 인터페이스는 **독립 명령행 도구**다. 기존 웹 화면의 전략 목록/자동 운용으로 등록되지는 않는다.

설계는 `domain/etf_strategies.py`(불변 규칙) → `domain/etf_research.py`(독립 원장 재생) → `application/etf_research.py`(실험 유스케이스)와 `ports/etf_research.py`(수집·저장 계약)로 나뉜다. 파일/HTTP 구현은 `adapters/etf_store.py`, `adapters/etf_sources.py`에 있고 `etf_cli.py`가 조립한다. 브로커·운용 DB 의존성은 없다.

## 준비와 최초 수집

저장소 루트의 PowerShell에서 실행한다. Python 3.12와 uv가 필요하다. `.env`, FRED/한투 키, Node, 실행 중인 앱은 필요 없다. 최초 수집만 인터넷을 사용한다.

```powershell
uv sync --project backend --frozen
uv run --project backend --frozen python -m invest_lab.etf_cli catalog
uv run --project backend --frozen python -m invest_lab.etf_cli collect --start 2002-01-01 --end 2026-09-28
```

날짜는 완료된 미국 거래일을 포함하는 과거 날짜로 명시한다. 수집 자료는 합성이 아니다. Yahoo의 SPY·QQQ 수정 가격, Fed H.10 원/달러, Cboe VIX/VXN, 미국 재무부 국채금리/실질금리, Chicago Fed NFCI, EIA WTI를 사용한다. Yahoo 공개 응답 구조는 향후 변경될 수 있으며 결측·소스 오류는 실패로 반환한다.

기본 폴더는 `.local/etf-research/`다. `raw/`에 원문·URL·취득 시각·SHA-256을, `datasets/`에 불변 데이터셋을, `runs/`에 개별 결과를 저장한다. `datasets-latest.json`은 최근 수집 데이터셋 ID의 편의용 목록이다. 같은 URL은 원문 캐시를 재사용하므로 새 시점의 원문을 받으려면 `--root .local/etf-research-새날짜`를 **명령 앞부분**에 지정한다. 기존 폴더를 지울 필요가 없다.

## 하나의 전략만 실행

```powershell
$etfData = Get-Content .local/etf-research/datasets-latest.json | ConvertFrom-Json
uv run --project backend --frozen python -m invest_lab.etf_cli run --dataset $etfData.SPY --policy trend_monthly --start 2004-01-01 --end 2026-04-24
uv run --project backend --frozen python -m invest_lab.etf_cli run --dataset $etfData.QQQ --policy trend_rates --start 2004-01-01 --end 2026-04-24 --cash USD --treasury-cash
```

`--policy all`로 전체를 실행한다. 원화 대기자금은 기본 `--cash KRW`, 달러 대기자금은 `--cash USD`다. 기본 대기자금 수익률은 명시적인 0% 가정이고 `--cash-rate 0.02`로 연 2%를 비교할 수 있다. `--treasury-cash`는 USD에만 적용하며 전 거래일에 보이던 미국 3개월물 수익률을 일할 적용하는 대용 모형이다. 실제 채권 ETF/예금의 투자수익과 다르다. `--cost-multiplier 2`는 모든 비용 가정을 두 배로 만든다.

`--parameters`에는 `{"defensive_weight":0.25}` 같은 JSON을 줄 수 있다. 조정은 해당 실행에만 적용하며 기본 레지스트리를 바꾸지 않는다. 전략 ID·유형은 재정의할 수 없다. PowerShell 버전별 따옴표 전달 차이가 있으면 Python에서 `EtfResearchService.run(..., parameters={...})`을 사용한다. 변경된 설정·코드 해시·데이터셋 ID는 결과에 남는다.

## 재현 가능한 전체 비교

```powershell
uv run --project backend --frozen python scripts/research-etf-suite.py --end 2026-04-24
uv run --project backend --frozen python -m invest_lab.etf_cli list
```

2026-09-29에 받은 NFCI 원문은 2026-04-24 관측까지 있어서 전체 전략 비교는 이 날짜에 맞췄다. 9월까지 강제로 실행하면 금융여건 전략에서 오래된 자료 오류가 발생한다. 값을 복제하거나 결측을 0으로 채우지 않는다. 현재 204개 비교는 11개 독립 전략/기준선 × 2개 ETF × 9개 조건(기본·달러 현금·비용 2배·원화 2%·미국채 현금 대용·4개 기간)과 2010년 이후 VIX/VXN 비교 6개다.

결과는 `suite-<코드해시 앞12자리>.json`과 `runs/<결과해시>.json.gz`다. 같은 코드·자료·설정은 같은 실행 ID를 만든다. 실행마다 현금·보유량·의사결정 상태를 새로 만들기 때문에 다른 전략 실행 순서가 영향을 주지 않는다. 모든 실패는 suite의 `failures`에 남고 프로그램도 실패 종료한다. 실패한 비교를 성공한 결과에 섞지 않는다.

다른 PC에는 코드와 별도로 `.local/etf-research/`를 복사한다. 이를 복사하면 수집 없이 오프라인 실행 가능하다. Git에는 원문/대용량 결과가 포함되지 않는다. 데이터셋과 결과는 gzip JSON이며 `FileEtfStore.load_dataset/load_run`이 내용 해시를 검사한다. `dataset_id`, `engine_revision`, `policy`, `config`, `provenance`를 함께 확인한다.

## 실행 계약과 한계

- 시작 전 2002년부터의 자료는 준비 기간이며 평가 수익에 포함하지 않는다. 기본 연구는 2004년부터 시작해 닷컴 붕괴 전 구간을 포함하지 않는다.
- 전 거래일 종가 신호로 최초 배분하고, 이후 월말 종가 신호를 다음 거래 시가에 집행한다. VIX 회복 전략만 매일 판정한다. 목표 비중과의 차이가 3%p 미만이면 매매하지 않는다. 리밸런싱 비용 때문에 체결 직후 비중은 목표와 약간 다를 수 있다.
- 추세는 완료된 월말 10개 가격의 평균이며 ETF 비중을 기본 100%/50%로 조절한다. 변동성 목표는 최근 60거래일 일수익률 표준편차 × √252로 구한 연율 12%다. 매크로 조건은 각각 독립 필터이며 자동 가중치 학습을 하지 않는다.
- 금리는 최근 64관측 실질 10년물 +0.5%p 또는 2년물 +0.75%p, NFCI는 0 초과이면서 4관측 전보다 상승, WTI는 63관측 전 대비 20% 상승, 금리차는 10년–3개월 음수일 때 50%로 제한한다. 음수 WTI는 방어 상태로 처리한다. 환율은 252관측 평균보다 5% 비쌀 때 비중 확대를 월 25%p로 나눈다.
- VIX 회복은 최근 20관측 최고가 30 이상이고 그 최고보다 20% 내려오며 ETF가 이틀 연속 20일 평균 위일 때 확대한다. 장기 추세가 유지되는 동안 확대 상태를 유지하고 추세가 무너지면 50%로 줄인다. VIX는 S&P 500, VXN은 Nasdaq 100 기반으로 동일 지수가 아니다.
- 기본 편도 수수료 2.5bp, 슬리피지 5bp, 환전 스프레드 10bp(1bp=0.01%)는 **가정**이다. KRW 대기자금은 거래 때 환전, USD 대기자금은 처음 전체 자금을 환전한다. USD 잔액도 환위험에 노출된다. 세금은 포함하지 않았고 마지막 보유분은 매도하지 않고 평가한다.
- 배당·분할 조정 가격으로 만든 소수점 **연구 단위**다. 분배금을 다시 더하지 않는다. 실제 주수·배당 지급 시점·원천징수·현실 주문 체결의 재현이 아니다.
- 시가 환율은 직전 Fed 고시치, 종가 평가는 당일 고시치(미발표일은 최대 7일 전 값) 대용이다. 실제 시각별 환율 호가가 아니다. 전략용 환율에는 8일 지연을 적용하며 평가용 환율과 구분한다.
- 금리는 익일 0시, VIX/VXN은 당일 16:15, NFCI/WTI는 7일 뒤 23:59, 환율 신호는 8일 뒤 23:59부터 보이도록 했다(미 동부 시각). 이는 보수적인 추정 지연이지 각 관측의 실제 공개 시각 검증이 아니다.
- 원문은 현재 수정본이다. 특히 NFCI는 과거 수치가 재추정된다. `available_at` 이후에만 입력하고 미래 빈티지 테스트가 통과하더라도 이 원문으로 **당시 공개본(PIT) 검증 완료를 주장할 수 없다**. `--strict-pit`는 이 데이터셋을 거절한다.
- 원장 대조 `ledger_verified=true`는 현금·수량·비용·평가 계산의 일치다. `performance_validated=false`는 그대로 유지한다. 기간별 비교는 역사 구간 재실험이며 미사용 검증/미래 수익 보장이 아니다.

## 검사

```powershell
uv run --project backend --frozen pytest backend/tests/test_etf_research.py -q
uv run --project backend --frozen pytest backend/tests -q
uv run --project backend --frozen ruff check backend scripts/research-etf-suite.py
uv run --project backend --frozen ruff format --check backend/src backend/tests scripts/research-etf-suite.py
```

프론트/API 계약은 이번 독립 연구 모듈에서 변경하지 않았다. 웹 화면 연결 및 국내 상장 ETF 실주수/세금/호가 모형은 이후 별도 작업이다. 미국 ETF 주문 기능으로 자동 연결하지 않는다.
