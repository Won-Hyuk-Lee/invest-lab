# 투자 대상 선정·위험 배분 연구 실행 안내

[사전 설계](selection-research-results-2026-09-29.md#사전-설계와-실행-중-변경) · [결과 보고서](selection-research-results-2026-09-29.md)

미국 업종/세계 자산/투자 스타일 ETF의 선택과 비중 조절, 공개 재무 포트폴리오 진단을 수행한다. 기존 국내 운용 및 이전 ETF 연구를 유지하는 독립 명령행 연구다. 한투 계좌·`.env`·프론트·실행 중인 서버는 필요하지 않다.

## 준비와 실행

저장소 루트에서 Python 3.12/uv를 사용한다.

```powershell
uv sync --project backend --frozen
uv run --project backend --frozen python -m invest_lab.selection_cli catalog
uv run --project backend --frozen python -m invest_lab.selection_cli collect --end 2026-09-28
uv run --project backend --frozen python scripts/research-selection-suite.py --end 2026-09-28
uv run --project backend --frozen python scripts/audit-selection-suite.py
```

개별 실행:

```powershell
uv run --project backend --frozen python -m invest_lab.selection_cli run --cohort style --model style_rank --start 2015-01-01 --end 2026-09-28
uv run --project backend --frozen python -m invest_lab.selection_cli run --cohort global --model global_trend --start 2006-01-01 --end 2026-09-28 --cash KRW --cost-multiplier 2 --extra-delay 1
```

`--root`는 명령 앞부분에 지정한다. 예: `python -m invest_lab.selection_cli --root .local/selection-new collect --end ...`. 최초 수집만 인터넷이 필요하다. 같은 URL은 원문 캐시를 재사용한다. 새로운 수정본을 받고 싶으면 새 루트를 사용한다. 기존 원문을 삭제/덮어쓰지 않는다. 수집 실패 시 재실행하면 완성된 원문 캐시를 재사용한다.

그림은 [PNG](images/selection-research-2026-09-29.png)로 제공한다. 중복 그림을 자동 생성하지 않으며 벡터 파일이 필요할 때만 `--output`에 `.svg` 경로를 지정한다. 재생성할 때만 임시 plotting 의존성을 사용한다. 앱 의존성/잠금 파일은 바뀌지 않는다.

```powershell
uv run --project backend --frozen --with matplotlib python scripts/plot-selection-results.py
```

## 구조와 보존

- `domain/selection_models.py`: 고정 후보 목록, 월별 순위/추세, 과거 63일 공분산, 역변동성·목표 변동성·개별/그룹 한도, 순위 완충.
- `domain/factor_research.py`: 공개 재무 특성 포트폴리오의 월별 복리·추세·비용 민감도. 실제 주식 주문 엔진과 구분한다.
- `application/selection_research.py`: 포트로 주입한 데이터/저장소를 사용하며 기존 `auto_replay`에 독립 의사결정 함수를 전달한다. 기존 함수의 기본 동작은 유지한다.
- `adapters/selection_sources.py`: 공개 ETF 수집기를 확장하고 French 월별 CSV의 첫 시가총액 가중 표를 읽는다. 결측 표시는 `null`로 보존하고 사용하려는 수익률이 결측이면 실패한다.
- `selection_cli.py`: 조립 및 수집/개별 실행. `scripts/research-selection-suite.py`: 고정 전체 비교. `scripts/audit-selection-suite.py`: 저장 결과·원문·원장 재검증과 추가 통계.

기본 `.local/selection-research/` 아래 `raw/`, `datasets/`, `runs/`에 원문 및 내용 해시 기반 불변 자료를 보관한다. `datasets-latest.json`, `factors-latest.json`은 편의용 포인터다. 전체 결과는 `suite-<엔진해시 앞12자>.json`, 대조 결과는 `audit-<동일해시>.json`이다. 기본 검증 스크립트는 이번 고정 실험 행렬(ETF 315, 재무 108)의 완전성을 확인한다. 실험 범위를 바꾸면 사전 설계·행렬 검사도 함께 새 연구로 작성한다.

다른 PC에서는 코드 외에 `.local/selection-research/` 전체를 별도로 옮긴다. 수집을 건너뛰고 실행/대조할 수 있다. `.venv`는 복사하지 않는다. Git에는 요약 JSON·보고서·그림을 남기고 원문/대용량 원장은 포함하지 않는다. 같은 자료·설정·코드는 같은 실행 ID를 생성한다.

## 계약과 한계

- 업종 9개, 세계 자산 9개, 스타일 5개를 선정 후보로 사용한다. 전체 수집은 중복 제거·SHY·기준선을 포함한 24개 ETF다. 현재 살아남은 대표 상품을 골랐으므로 모든 과거 상품에 대한 무편향 연구는 아니다.
- 상대 강도는 63/126/252일 수정 종가 수익률 평균이다. 월말에 상위 3개(스타일 2개)를 먼저 고른 뒤 양의 상대 강도/200일 평균 조건을 적용한다. 탈락한 자리를 하위 순위로 채우지 않는다. 동일 비중 기준선도 3%p 매매 완충을 적용한다.
- 위험 배분은 선택 자산의 63일 표본 공분산을 사용한다. 역변동성 배분, 개별 40%(스타일 60%) 상한, 연율 12% 목표의 순서다. 대상 선정은 월별이고 목표 비중 위험 갱신은 주별이다. 변동성은 **USD 가격 기준**이다. 원화 환위험을 헤지하거나 원화 변동성 12%를 보장하지 않는다.
- SHY 방어도 실제 ETF 가격·비용을 사용하며 현금으로 취급하지 않는다. SHY를 채운 뒤 예상 변동성이 목표를 초과하면 전체 목표 비중을 다시 축소해 현금을 남긴다. 개별/그룹 한도와 변동성은 의사결정 시점의 목표다. 시장 변화·시가 갭·3%p 매매 완충으로 실제 비중은 달라진다.
- 종가 신호는 다음 세션 시가에 체결한다. 1세션 추가 지연 대조가 있다. 모든 자산 매수는 하나의 현금 예산을 공유하며, 70:30 조합은 초기 자금만 분할하고 이후 구성 비중은 변한다.
- 수정 가격 소수점 연구 단위다. 실주수·세금·배당 지급일·실제 체결 호가·최종 전량 청산 비용은 미반영이다. USD 현금은 환위험을 갖는다. KRW 현금 버전은 매수/매도 시마다 환전 가정을 적용한다.
- 원화 평가는 Fed 고시 환율을 사용한다. 실제 시가 환전 호가가 아니며, 가격/금융 자료는 수집 시점의 수정본이다. 엄격한 PIT 설정은 이 자료를 거절한다.
- 재무 포트폴리오는 원천 제공자가 전년도 재무로 매년 6월 구성한 시가총액 가중 수익률이다. 자체 개별 주식 선정, 원문 공시 시점, 구성종목 내부 회전 비용을 검증한 자료가 아니다. 원천은 2026-08까지이고 월말 낙폭은 일중/일별 낙폭을 과소 표현할 수 있다.
- 재무 추세는 관측 월 `t`에 `t-2`까지의 10개월 지수를 사용해 집계 자료 지연을 한 달 추가 가정한다. 정확한 당시 배포 시각을 검증한 것은 아니다. 현금은 원천의 해당 월 T-bill 실현 수익을 받고, 이 미래 수익률로 매수 여부를 결정하지 않는다. 기본 7.5bp 외부 교체 비용 및 연 0/1/2% 추가 비용 시나리오는 추정 실현 비용이 아닌 민감도다. KRW 재무 비교는 월말 고시 환율 환산이고 별도 환전 호가 비용은 없다.
- 모든 역사 구간은 공개된 과거 자료를 재실험한 것이다. 마지막 구간을 미사용 테스트라 부르지 않는다. 재표본 통계도 여러 전략 비교를 보정한 유의성 검정/미래 성공 확률이 아니다.

## 검증

```powershell
uv run --project backend --frozen pytest backend/tests/test_selection_research.py -q
uv run --project backend --frozen pytest backend/tests -q
uv run --project backend --frozen ruff check backend/src backend/tests scripts --no-cache
uv run --project backend --frozen ruff format --check backend/src backend/tests scripts --no-cache
```

미래 가격 교란, 공분산/상관관계, 현금 예산, 비중 한도, 방어 ETF 위험, 순위 완충, 월별 선정/주별 비중, 다음 시가/지연, 재무 자료 결측·월별 시점·복리·환율·비용, 불변 저장/재현, 기존 규칙 보존을 확인한다. API/프론트 계약과 브로커 설정은 변경하지 않는다.
