# 한투 API 호출 한도와 수집 설계

> 2026-09-27 후속 조사: 모의 인증·현재가·잔고의 [일회성 연결은 확인](2026-09-27-kis-connection-check.md)했다. 앱 연동은 아직 없으며, 모의 주문/WS 지원표와 미지원 조회 대체 설계는 [최신 데이터·API 계약](paper-investment-data-contracts.md)을 따른다. 아래 호출 예산은 실측 보장값이 아닌 기존 조사 기준이다.

확인일: 2026-09-25. 실제 계좌를 호출한 측정치가 아닌 공식 공지·샘플 코드에 근거한 설계다. 계좌별 적용 상태는 연동 때 확인한다.

## 확인한 한도

| 구분 | 공식 안내 | 적용 |
| --- | --- | --- |
| 모의 REST | 초당 1건 | 계좌 단위. 시세·잔고·주문 등을 같은 예산으로 관리 |
| 실전 REST 기본 | 초당 18건 | 계좌 단위 |
| 실전 신규 등록 | 신청 시점부터 3일간 초당 3건 | 이후 기본 유량. 모의 계좌에는 이 신규 제한 대신 모의 기본 정책 적용 |
| WebSocket | 계좌/앱키당 1세션, 실시간 합산 41개 구독 | 체결가·호가·예상체결·체결통보 등 모든 상품 합산 |

일반 유량 공지는 2026-04-20 기준으로 갱신됐다. 신규 정책은 2026-04-03 17:00부터 적용한다고 명시한다. 앱키를 여러 프로세스에서 사용해도 계좌 예산을 공유하므로 프로세스별 제한만으로는 부족하다. [일반 한도](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/d0d1a83f-6f8d-4437-9700-6d26702fd989), [신규 등록 정책](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/c1113824-17c7-47a7-b7b8-8880506a847c).

41은 종목 수가 아니다. 예를 들어 종목 20개의 체결가와 호가를 각각 구독하면 40개를 사용한다. 여기에 체결통보 구독 1개를 더하는 설계가 가능하지만 실제 지원 채널과 모의 환경 동작은 연결 검증이 필요하다. 구독 후 수신되는 체결 메시지마다 REST 조회를 다시 할 필요는 없다.

## 수집 시간 추산

아래의 1,000종목은 계산 예시이며 실제 코스피 종목 수를 의미하지 않는다. 모든 값은 다른 요청·응답 지연·재시도·점검 시간을 제외한 산술 추산이다.

| 작업 | 호출 수 가정 | 모의 1건/초 | 실전 신규 3건/초 | 실전 기본 18건/초 |
| --- | ---: | ---: | ---: | ---: |
| 20종목 현재가 1회씩 | 20 | 약 20초 | 약 7초 | 약 2초 |
| 100종목 현재가 1회씩 | 100 | 약 1분 40초 | 약 34초 | 약 6초 |
| 1,000종목 현재가 1회씩 | 1,000 | 약 16분 40초 | 약 5분 34초 | 약 56초 |
| 1,000종목 × 260일 일봉 최초 수집 | 최소 3,000 | 약 50분 | 약 16분 40초 | 약 2분 47초 |

공식 기간별 시세 샘플은 한 번에 최대 100건을 안내한다. 따라서 260개 일봉은 종목당 최소 3회로 계산했다. 달력 날짜 분할, 빈 구간, 기업행동 정정에 따라 더 필요하다. [공식 일봉 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py).

모의 환경에서 호출 시작 간격을 1.2초로 두는 초기 설계라면 위 작업은 각각 약 24초·2분·20분·60분 수준이다. 이는 권장 구현 가정이며 공식 보장 속도가 아니다. 주문을 위해 자리를 비우면 수집 시간은 더 길어진다. 월·일 요청 한도나 무제한 사용 보장은 이 초당 공지만으로 단정하지 않는다.

## 추천 운영 방식

사용자는 일봉 전체 분석 + 후보 10~20개 장중 감시를 선택했다. 다음 항목은 그 방향을 구현하기 위한 설계이며, 아직 계좌 연동이나 실시간 구독은 실행하지 않았다.

1. 과거 일봉은 최초에 저장하고 이후 미수집 날짜만 보충한다. 데이터 갱신과 지표 계산을 분리한다.
2. 전체 종목의 일봉 분석은 정규장 일봉이 확정된 뒤 수행한다. 계산은 로컬 DB를 읽으므로 지표를 여러 번 계산해도 API를 추가 호출하지 않는다.
3. 장중에는 보유 종목과 상위 후보 10~20개를 우선 감시한다. WebSocket 지원이 확인되면 체결가·호가를 구독한다.
4. 잔고·체결 조회는 일정 간격과 주문 이벤트 후 수행한다. 화면 새로고침마다 브로커를 직접 호출하지 않고 서버 캐시를 제공한다.
5. 계좌별 단일 호출 큐에서 취소·주문 상태 확인·주문·잔고·배경 수집의 우선순위를 관리한다. 미확인 주문이 있으면 새 주문을 멈추고 대조한다.
6. GET 실패는 제한된 횟수로 지연 재시도한다. 주문 POST 타임아웃은 성공 여부가 불명확하므로 주문 내역을 확인하기 전에 재전송하지 않는다.
7. 토큰은 유효기간을 저장해 재사용한다. 토큰 발급 루프나 프로세스별 독립 발급을 피한다.

실전 시세 조회와 모의 주문을 별도 어댑터로 조합하는 구성도 검토할 수 있다. 다만 실전 자격증명을 쓰는 조회 어댑터에는 주문 메서드를 제공하지 않고, 서비스 약관·조회 권한·시세 시각 일치 여부를 확인한 뒤 연결한다. 현재는 둘 다 미연동이다.

## API 구현 확인 목록

| 기능 | 공식 예시 | 구현에서 확인할 내용 |
| --- | --- | --- |
| 현재가·호가 | [현재가](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/inquire_price) | 데이터 시각, 정규장/다른 세션, 시장 구분 |
| 일봉 | [기간별 시세](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/inquire_daily_itemchartprice) | 수정/원주가 분리, 최대 100개, 날짜 분할 |
| 보유 종목·계좌 | [잔고](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/inquire_balance) | 계좌번호+상품코드, 연속 조회, 0주 잔고, 평가 기준 |
| 매수·매도 | [현금 주문](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/order_cash) | 모의/실전 TR ID, 수량/단가 문자열, 현금 한도, 시장·주문유형 |
| 주문·미체결·체결 | [일별 주문 체결](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock/inquire_daily_ccld) | 부분 체결, 페이지 누락, 주문번호, 누적 체결량 |
| 정정·취소·매수 가능액 | [국내주식 예시 전체](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm/domestic_stock) | 기능별 모의 지원 여부와 최신 명세 확인 |

모의 잔고는 공식 샘플상 1회 최대 20건, 모의 일별 주문체결은 최대 15건이다. 첫 페이지만 읽으면 실제 보유 종목·체결을 누락할 수 있다. [잔고 명세](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_balance/inquire_balance.py), [체결 명세](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py).

## 최신 공지에서 발견한 설계 영향

- 한투는 2026-09-14 KRX 애프터마켓 도입에 따른 주문유형·잔고 입력값·실시간 응답 필드 변경을 공지했다. 초기 전략은 **KRX 정규장** 범위로 제안하며, 시장과 세션을 DB에 명시한다. 구형 샘플의 주석이나 ‘15:30 이후면 모든 거래 종료’ 같은 가정을 그대로 사용하지 않는다. [공식 세션 변경 공지](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/26dfe350-eb72-48e5-8175-34eb27970f3e).
- 모의계좌 만료와 API 유효기간은 별개일 수 있다. 계좌·HTS ID 매칭과 모의계좌 유효성 확인을 초기 연결 진단에 넣는다. [공식 모의투자 안내](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/747384fe-f047-495c-8548-769c8e6f4c15).
- 공식 개인용 시세 사용 범위는 자기 자산 투자 목적이다. 현재 내부 서비스 방향을 유지하고, 포트폴리오 사이트에는 실제 계좌/시세 대신 서비스 소개를 게시하는 구성이 적합하다. [공식 서비스 안내](https://apiportal.koreainvestment.com/about-open-api).

이 문서는 호출 한도를 실측했다는 의미가 아니다. 실제 연동 때 읽기 전용 소량 호출로 한도 오류·지연·누락·모의 지원 범위를 확인한다.
