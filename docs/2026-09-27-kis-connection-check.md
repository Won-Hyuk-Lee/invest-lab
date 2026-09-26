# 한투 모의투자 REST 연결 확인

확인 시각: 2026-09-27 01:40 KST. 사용자가 로컬 `.env`에 입력한 정보로 일회성 연결 검사를 수행했다.

## 결과

| 검사 | 결과 |
| --- | --- |
| 모의 서버 인증 | `/oauth2/tokenP` HTTP 200, 접근 토큰 수신 |
| 국내주식 현재가 조회 | 삼성전자 `005930`, HTTP 200 및 업무 성공 코드, 양수 가격 확인 |
| 모의계좌 잔고 조회 | HTTP 200 및 업무 성공 코드, 예수금·유가증권 평가액·총평가액 숫자 필드 수신 |
| 잔고 연속 조회 | 1페이지로 완료, 추가 페이지 없음 |
| 수량이 양수인 보유 항목 | 0개 |
| 주문 전송 | 0건 |

조회 API는 HTTP 상태뿐 아니라 `rt_cd == "0"`도 확인했다. 휴장 중의 조회 성공이므로 실시간 체결 발생이나 체결 시세 최신성을 검증한 것은 아니다.

## 설정 확인

- 모의 App Key·App Secret, 계좌 앞 8자리, HTS ID 항목이 존재했다.
- `.env`의 계좌 뒤 2자리 항목은 숫자가 아닌 문구였다. 원본 파일은 수정하지 않고 공식 예제의 국내주식 종합계좌 코드 `01`을 검사 프로세스에서만 사용했다.
- 해당 모의계좌의 잔고 조회가 `01`로 성공했다. 이후 정식 설정에는 이번에 조회 확인한 `01`을 사용하면 된다.
- 현재 `.env`는 설명식 항목과 다음 줄 값이 섞인 형식이다. 이번 검사는 해당 형식을 읽는 임시 파서를 사용했으며, 정식 연동에서는 표준 설정 형식과 유효성 검사를 추가해야 한다.
- HTS ID는 존재만 확인했다. 이번 REST 인증·현재가·잔고 요청에는 필요하지 않았으며 실시간 체결통보/WebSocket 연결은 검사하지 않았다.

## 검사 범위

대상 서버는 `https://openapivts.koreainvestment.com:29443`으로 고정했다. 허용한 요청은 토큰 발급 POST 1개와 현재가·잔고 GET뿐이며, 조회 시작 간격은 최소 1.25초로 뒀다. 실전 서버, 주문·취소 API, 다른 계좌 조회는 수행하지 않았다.

키·토큰·계좌번호·HTS ID·원본 잔고 응답을 보고서나 로그에 저장하지 않았다. 토큰은 프로세스 메모리에서만 사용했다. 비식별 검사 요약과 진단 스크립트는 Git 제외 대상인 `.local/`에 있다. `.env`도 Git 제외 상태다.

이번 결과는 **자격증명과 모의계좌의 읽기 연결이 가능함을 확인한 것**이다. 앱의 조립 코드는 여전히 DemoMarket + SQLite이며, 대시보드가 한투 데이터로 전환된 것은 아니다. 주문·체결·매도·잔고 대조 E2E와 실시간 감시는 후속 구현이다.

공식 명세 참고: [설정 예제](https://github.com/koreainvestment/open-trading-api/blob/main/kis_devlp.yaml), [인증](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/kis_auth.py), [현재가](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_price/inquire_price.py), [잔고](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_balance/inquire_balance.py).
