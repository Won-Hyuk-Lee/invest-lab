# 자동 투자 모델 연구 실행 안내

장기·단기·공포 매수·자산 교체를 독립적으로 계산하는 연구 도구다. **실제 과거 자료로 자동 매매 규칙을 재생**한다. 기존 코스피 운용, `etf_cli.py` 1차 연구, 웹 화면, 브로커 설정과 분리되어 있다. 이 명령은 `.env`를 읽거나 주문을 전송하지 않는다.

## 준비·수집

저장소 루트의 PowerShell에서 실행한다. Python 3.12/uv가 필요하고 Node나 실행 중인 서버는 필요 없다.

```powershell
uv sync --project backend --frozen
uv run --project backend --frozen python -m invest_lab.auto_cli catalog
uv run --project backend --frozen python -m invest_lab.auto_cli collect --end 2026-09-28
```

수집 기본 시작은 2000-01-04다. Fed 원/달러 자료 요청에서 1998/1999년 시작 URL은 빈 응답이었고 2000년 시작 응답만 확인했다. 2000-01-03 고시치를 다음 거래일 시가 환율 대용으로 쓰기 위해 1월 4일부터 시작했다. 부족한 과거 환율을 복제해 1999년으로 늘리지 않았다. 253세션 지표 준비 후 주식 모델의 확장 평가 시작은 **2001-02-01**이다. 닷컴 붕괴의 2000년 시작부터 전부 검증한 것은 아니다.

가격: Yahoo SPY·QQQ·IEF·GLD 수정 일봉, FX: Fed H.10 KRW/USD, 공포: Cboe VIX. IEF 원문 첫 관측은 2002-07-30, GLD는 2004-11-18이다. 상품 출시일과 원문의 첫 관측을 혼동하지 않는다. GLD까지 동일한 기간에 평가하기 위해 기본 시작은 2006년이다.

`.local/auto-models/raw/`에 URL·수집시각·원문 해시, `datasets/`에 불변 자료, `runs/`에 불변 결과를 저장한다. `.local/etf-research/`와 기존 KRX/운용 폴더는 수정하지 않는다. 기본 수집은 원문 캐시를 재사용한다. 새로운 원문 스냅샷은 `--root .local/auto-models-새날짜`를 **collect/run 앞**에 붙여 별도 폴더로 받는다.

## 개별 실행

```powershell
uv run --project backend --frozen python -m invest_lab.auto_cli run --model pullback_rsi --end 2026-09-28
uv run --project backend --frozen python -m invest_lab.auto_cli run --model panic_recovery --cash KRW --end 2026-09-28
uv run --project backend --frozen python -m invest_lab.auto_cli run --model asset_rotation --cost-multiplier 4 --end 2026-09-28
uv run --project backend --frozen python -m invest_lab.auto_cli run --model core_pullback --extra-delay 1 --end 2026-09-28
```

`--model all`은 16개 모델/조합/기준선을 실행한다. 기본 시작 2006-01-01, 원화 초기 1천만원, USD 대기자금·이자 0%, 편도 수수료 2.5bp/슬리피지 5bp/환전 10bp다. `--cash-rate 0.02`는 현금 연 2% 가정, `--cost-multiplier`는 모든 비용 배수다. `--extra-delay 1`은 종가 신호 직후 다음 시가보다 한 거래일 더 늦은 시가를 사용한다.

주식 모델만 오래된 기간으로 실행할 때:

```powershell
uv run --project backend --frozen python -m invest_lab.auto_cli run --model long_trend --symbols SPY QQQ --start 2001-02-01 --end 2026-09-28
```

`asset_rotation`은 IEF/GLD가 필요하므로 이 주식 전용 구간에 넣지 않는다. `--parameters` JSON으로 해당 모델의 수치 가정을 바꿀 수 있지만 ID/유형/자산 목록을 덮어쓸 수는 없다. PowerShell 인수 전달이 어려우면 `AutoResearchService.run(ids, model, AutoConfig(...), parameters={...})`을 사용한다. 조합의 구성 변경은 별도 실험으로 명시해야 한다.

## 전체 비교 재현

```powershell
uv run --project backend --frozen python scripts/research-auto-models.py --end 2026-09-28
```

2026-09-29의 연구 매트릭스는 207개다. 기본 USD/원화 현금/비용 2·4배/추가 지연 1일/USD 현금 2%/4개 기간에서 16개씩 160회, 주식 전용 장기·닷컴 구간 15개씩 30회, 임계값 민감도 12회, 평균 노출 대조 5회다. 마지막 대조는 결과를 본 뒤 평균 노출을 맞춘 **사후 진단 기준선**이며 실제 운용 후보/미사용 검증에 포함하지 않는다.

각 실행 ID와 결과는 `suite-<엔진해시 앞12자리>.json`에 저장한다. 오류는 `failures`에 기록하고 프로그램은 실패 종료한다. 결과가 없는 모델을 성공한 것으로 간주하거나 빠뜨리지 않는다. 같은 자료/코드/설정은 동일한 ID를 만든다. 데이터 ID 목록은 `datasets-latest.json`에 있다. 이 폴더를 다른 PC에 복사하면 수집 없이 오프라인 재실행할 수 있다.

결과 파일은 `FileEtfStore.load_run(id)`로 읽으면 해시를 검증한다. 일별 자산, 모든 거래/비용/신호, 연도별 수익, 종료 시 아직 체결되지 않은 판단(`pending`)을 볼 수 있다. `pending`은 이 연구 계좌의 신호이며 실제 모의 계좌에 보낼 주문이 아니다.

## 모델 목록과 실행 규칙

| ID | 역할과 규칙 |
|---|---|
| `hold_spy`, `hold_qqq` | 최초 시가 매수 후 보유 |
| `equal_equity` | 월말 SPY/QQQ 50:50, 3%p 미만 편차는 매매하지 않음 |
| `cash`, `fixed_half` | 현금 100%, QQQ 50% 고정의 기준선 |
| `long_trend` | 완료된 월말 10개 평균 위는 QQQ 100%, 아래는 50% |
| `dual_momentum` | SPY/QQQ의 126·252일 평균 수익 상위 1개. 양수이고 200일 평균 위일 때 100%, 아니면 현금 |
| `breakout` | QQQ가 직전 55일 최고 종가를 돌파하고 200일 평균 위에서 진입. 직전 20일 최저 종가 이탈 또는 보유 중 최고 종가/진입가 기준 10% 추적 손절 |
| `pullback_rsi` | 상승하는 200일 평균 위에서 Wilder RSI(2)<=10. RSI>=70/5일 평균 회복/10거래일/8% 손절 중 먼저 충족한 조건으로 청산 |
| `pullback_z` | 같은 장기 추세에서 20일 종가 z-score<=-2. 평균 회복/15거래일/8% 손절 |
| `panic_immediate` | QQQ 63일 고점 대비 -10% 이하, RSI(14)<=35, 사용 가능한 VIX>=30에서 진입 |
| `panic_recovery` | 같은 공포 발생 후 20거래일 안에 직전 3일 최고 종가 회복 및 VIX 20관측 최고 대비 20% 하락 후 진입 |
| 공포 모델 공통 청산 | 진입가 대비 종가 +10%/-12% 또는 최대 20거래일 |
| `asset_rotation` | SPY/QQQ/IEF/GLD의 63·126·252일 평균 수익 상위 2개에 각각 최대 50%. 각 자산이 양수·200일 평균 위일 때만 배정, 나머지는 현금 |
| `core_pullback` | 초기 자금 QQQ 장기 보유 70% + RSI 단기 30% |
| `core_panic` | 초기 자금 QQQ 장기 보유 70% + 공포 회복 30% |
| `momentum_pullback` | 초기 자금 상대·절대 모멘텀 70% + RSI 단기 30% |

조합은 **초기 자금 구획**이다. 각각의 이익·손실을 그 구획 안에서 복리 운용하고 구획 간 정기 이체는 하지 않는다. 시간이 지나면 70:30에서 비중이 달라진다. 전액으로 각각 실행한 수익률을 더하는 계산이 아니며, 총 현금/수량/비용과 각 구획의 매수 여력을 별도로 대조한다.

모든 진입·청산은 종가 판단 후 이후 시가다. 매도 대금으로 현금을 확보한 뒤 여러 자산의 매수 수량을 비용 포함 현금 안에서 비례 축소한다. 차입·공매도·레버리지는 없다. 손절 비율은 종가 신호 임계값이며 손실 상한 보장이 아니다. 청산을 기다리는 동안의 갭도 체결 가격에 반영된다. 추가 지연 실험에서는 미체결 판단이 있는 동안 새 판단을 쌓지 않는다. 실제 장중 주문 취소·체결 정책을 구현한 것은 아니다.

## 지표를 읽을 때

- `cagr_krw`는 세전 원화 복리 연환산, MDD는 일별 평가 기준 고점 대비 최대 하락이다. USD 표시 손익/낙폭도 별도로 제공한다.
- 252/756거래일 이동 수익의 최악/최고/양수 비율은 **겹치는 역사 구간** 통계다. 서로 독립인 표본이나 미래 성공 확률이 아니다.
- `realized_sales` 승률/손익비는 평균 매입원가 기준 **매도 실현 구간**이며 부분 매도도 1건이다. 남은 미실현 손실은 승률 분모에 없으므로 계좌 수익률과 함께 본다. 연구 단위 매입원가이며 실제 세금 계산에 사용하지 않는다.
- 거래 비용 총액은 실제 시점별 가정 비용의 합계다. 복리 기회비용까지 합친 차감액과 다르다. 대기 USD도 원화 기준 환위험에 노출된다.

## 검증·한계

```powershell
uv run --project backend --frozen pytest backend/tests/test_auto_models.py -q
uv run --project backend --frozen pytest backend/tests -q
uv run --project backend --frozen ruff check --no-cache backend scripts/research-auto-models.py
uv run --project backend --frozen ruff format --check --no-cache backend/src backend/tests scripts/research-auto-models.py
```

모델/재생은 `domain/auto_models.py`, `domain/auto_replay.py`, 유스케이스는 `application/auto_research.py`, 수집은 `adapters/auto_sources.py`, 조립은 `auto_cli.py`다. 기존 수집/저장 포트를 이용하며 도메인에서 HTTP/DB를 참조하지 않는다. 설정/모델 상태는 실행마다 분리된다.

**`performance_validated=false`를 유지한다.** 수정 가격 연구 단위, 현재 수정본·추정 공개 지연, 고시 환율 대용, 세금/실제 배당 지급일/정수 주수 생략, 현재 선택한 살아남은 ETF라는 한계가 있다. ETF 가격에 내재한 운용보수를 별도로 또 차감하지 않았다. 원문 당시 공개본 검증을 요청하는 `--strict-pit`는 이 자료를 거절한다.

이번 역사 구간은 이미 관찰된 구간이다. 민감도 결과의 최고값으로 기본 규칙을 교체하지 않았다. 수익률/낙폭의 미래 재현, 국내 상장 대응 ETF의 동일 성과, 모의/실전 체결 성공을 주장하지 않는다. 상세 결과와 다음 후보는 [비교 보고서](auto-models-results-2026-09-29.md)에 있다.
