# KRX 백테스트 검토 보조자료

> 보관 자료: 초기 백테스트에 대한 외부 검토와 보조 계산을 원문 그대로 보존한다. 이후 엔진·기업행동 보정 결과는 [최종 비교 보고서](../docs/sell-engine-backtest-2026-09-29.md), 집 PC 재개 절차는 [작업 인계서](../docs/handoff-2026-09-29.md)를 따른다. 이 폴더의 수익률을 현재 엔진의 결과로 사용하지 않는다.

검토일: 2026-09-29. 사용자가 제공한 7개 파일만을 대상으로 한 읽기 전용 진단입니다.

## 포함 파일

- `krx_review_2026-09-29.md`: 주요 발견, 구현 검토, 후속 실험 설계.
- `computed_summary.json`: 저장된 거래·일별 평가액에서 재계산한 지표.
- `round_trips.csv`: 청산된 296개 거래의 진입·청산 연결 원장. 수익률은 진입 수수료를 포함한 매입 원가 대비 순손익입니다. 수익률 필드는 소수 단위입니다.
- `daily_diagnostics.csv`: 421거래일의 보유 종목 수·노출도. `active_positions`는 보고서상 020760의 정지 기간에 그 1개 종목만 제외한 참고 집계이며, 개별 일봉으로 모든 종목의 거래 가능 여부를 재검증한 값이 아닙니다.
- `analyze_results.py`: 위 재계산을 수행하는 표준 라이브러리 전용 스크립트.
- `synthetic_engine_checks.py`, `synthetic_checks.json`: 첨부된 공용 replay 엔진의 선택된 동작을 합성 데이터로 재현한 검사와 결과.
- `recalculation_log.txt`, `synthetic_log.txt`: 실행 결과.

## 실행

Python 3.10 이상에서 실행합니다. `--input-dir`는 원본 첨부 파일이 있는 폴더로 지정합니다.

```bash
python analyze_results.py --input-dir /path/to/originals --output-dir ./diagnostics
python synthetic_engine_checks.py --input-dir /path/to/originals --output-dir ./diagnostics
```

첫 스크립트는 `result_full_2025_to_latest.json.gz`, `backtest_audit.json.gz`를 읽습니다. 두 번째 스크립트는 `strategies.py`, `research.py`, `indicators.py`를 읽습니다. 두 스크립트는 네트워크·브로커·SQLite·서비스 설정을 사용하지 않습니다. 입력 파일은 수정하지 않습니다.

합성 검사는 전체 애플리케이션 테스트가 아닙니다. `models.py`가 첨부되지 않아 `indicators.py`에서 표준 라이브러리 import와 `rsi_wilder` 함수만 AST로 추출하고, `research.py`와 `strategies.py`는 원본 코드 그대로 별도 Python 프로세스에서 로드합니다. 특정 이벤트를 쉽게 발생시키기 위해 합성 ETF 데이터를 공용 replay 엔진에 넣었습니다. 이는 ETF 실데이터를 백테스트했다는 뜻이 아닙니다.

## 한계와 계산 계약

원본 `dataset.json.gz`, 종목별 일봉, KRX 원문, 지수 일봉, 마스터 및 원문 manifest가 첨부되지 않았습니다. 따라서 신호 재생, 기업행동 영향, 지수 수익률, 변경된 전략 성과는 독립 재현하지 않았습니다. 결과 JSON의 canonical SHA-256과 감사 JSON의 result_hash는 일치합니다. 입력 데이터셋의 해시는 원문 데이터셋이 없어 재계산하지 않았습니다.

비용 제거·세율 변경 계산은 **거래 목록, 거래일, 체결 수량, 기말 종가 평가를 고정**한 회계 민감도입니다. 비용을 바꿔 엔진을 재실행한 결과가 아닙니다. 거래량·자금 제한으로 수량과 후속 진입이 달라질 가능성을 포함하지 않습니다.

`holding_intervals`는 제공된 421개 세션 배열에서 매도일 인덱스-매수일 인덱스입니다. 따라서 매수 시가부터 매도 시가까지의 거래일 간격이며, 양 끝 날짜를 포함한 달력상의 날짜 수가 아닙니다.

`reason_exact`는 서로 겹치지 않는 청산 사유 조합입니다. `reason_overlap`는 복수 사유를 각각 집계하므로 행별 손익·거래 수를 더하면 중복됩니다.

Profit factor는 청산된 거래의 순이익 합계 / 순손실 절댓값 합계입니다. 거래별 산술평균 순수익률은 각 거래를 동일 가중한 통계로 포트폴리오 수익률과 다릅니다. 2025·2026년 청산 손익은 청산일 기준이며, 연도별 포트폴리오 수익률은 이전 연도 말 보유 포지션을 승계합니다.
