# 모의투자 데이터·외부 API 계약

확인일: 2026-09-27. [전체 완료 계획](paper-investment-completion-plan.md)의 구현 부속 명세다. 공식 공개 문서에서 확인한 사실과 프로젝트 설계를 구분한다. 인증이 필요한 실제 호출은 이번 조사에서 수행하지 않았다. 실제 확인된 연결 범위는 [이전 연결 기록](2026-09-27-kis-connection-check.md)뿐이다.

## 1. 한투 모의 지원표

REST 공통 경로는 `/uapi/domestic-stock/v1/`, 모의 base URL은 `https://openapivts.koreainvestment.com:29443`이다. 모의/실전 URL과 TR을 별도 상수·capability로 선언하고 임의 TR 첫 글자 교체를 지원 판정에 쓰지 않는다.

| 기능 | 경로/채널 | 모의 지원·TR | 구현 시 확인 |
| --- | --- | --- | --- |
| 인증 | POST `/oauth2/tokenP` | 지원, 기존 진단 성공 | 만료 시각·공유 캐시·발급 잠금, 오류 로그 마스킹 |
| 현재가 | GET `quotations/inquire-price` | `FHKST01010100`, 기존 진단 성공 | 가격 기준 시각/세션·거래 가능 여부 |
| 일봉 | GET `quotations/inquire-daily-itemchartprice` | `FHKST03010100` | 최대 100건·기간 분할·확정/조정 기준 |
| 잔고 | GET `trading/inquire-balance` | `VTTC8434R`, 기존 진단 성공 | 모의 최대 20건/페이지·연속 조회 |
| 매수 가능 | GET `trading/inquire-psbl-order` | `VTTC8908R` | 미수 없는 `nrcvb_buy_amt/qty` |
| 현금 매수/매도 | POST `trading/order-cash` | `VTTC0012U` / `VTTC0011U` | KRX, 지정가/시장가 각각 실제 모의 검증 |
| 정정/취소 | POST `trading/order-rvsecncl` | `VTTC0013U` | 원주문·지점·잔량·정정 chain |
| 주문/체결 | GET `trading/inquire-daily-ccld` | 최근 3개월 `VTTC0081R`, 이전 `VTSC9215R` | 모의 최대 15건/페이지·누적 체결·취소·거부 |
| 정정취소 가능 | GET `trading/inquire-psbl-rvsecncl` | **모의 미지원** | 주문내역 `rmn_qty`+원주문으로 대체 후 검증 |
| 매도 가능 | GET `trading/inquire-psbl-sell` | **모의 미지원** | 잔고 `ord_psbl_qty` 의미·예약 반영 시점 검증 |
| 휴장일 | GET `quotations/chk-holiday` | **모의 미지원** | 별도 버전형 거래 달력 |
| WS 체결가 | `H0STCNT0` | 모의 지원 | 종목코드로 구독, 수신 시각·갭 감지 |
| WS 호가 | `H0STASP0` | 모의 지원 | 호가 스냅샷·수신 세션 |
| WS 계좌통보 | 실전 채널 `H0STCNI0`의 모의 TR | **`H0STCNI9`** | 구독 key는 **HTS ID**, 종목코드가 아님 |
| WS 장운영/VI | `H0STMKO0` | **모의 미지원** | 이 통보를 유일한 주문 가능성 근거로 쓰지 않음 |

지원 근거: [현재가](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_price/inquire_price.py), [일봉](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py), [잔고](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_balance/inquire_balance.py), [매수 가능](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_psbl_order/inquire_psbl_order.py), [현금 주문](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_cash/order_cash.py), [정정·취소](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_rvsecncl/order_rvsecncl.py), [주문체결](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py).

미지원 및 WS 지원 여부는 공개 포털의 `virtualDomain/virtualTrId`를 직접 확인했다. [정정취소 가능](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl), [매도 가능](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/trading/inquire-psbl-sell), [휴장일](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/quotations/chk-holiday), [WS 체결가](https://apiportal.koreainvestment.com/apiservice-apiservice?/tryitout/H0STCNT0), [WS 호가](https://apiportal.koreainvestment.com/apiservice-apiservice?/tryitout/H0STASP0), [WS 계좌통보](https://apiportal.koreainvestment.com/apiservice-apiservice?/tryitout/H0STCNI0), [WS 장운영](https://apiportal.koreainvestment.com/apiservice-apiservice?/tryitout/H0STMKO0).

포털 본문이 동적으로 렌더링될 때 공개 문서의 metadata 조회 패턴은 `https://apiportal.koreainvestment.com/api/apis/public/detail?accessUrl=<URL-encoded endpoint>`다. 계좌 데이터 API가 아니며 위 지원표 확인에 사용했다. 구현 착수 시 변경 공지와 다시 대조한다.

### 주문 정규화 계약

- `EXCG_ID_DVSN_CD=KRX`, 초기 허용 유형은 현금 지정가/시장가. 모의에서 NXT/SOR와 모든 고급 주문이 지원된다고 추정하지 않는다.
- 수량/단가를 문자열로 보내고 본문 키의 대문자를 지킨다. 주문 시장가 `ORD_UNPR="0"`와 매수가능조회 시장가 `ORD_UNPR=""`를 엔드포인트별로 구분한다. 미수를 포함할 수 있는 `max_buy_*`를 현금 한도로 쓰지 않는다. [포털 주문](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/trading/order-cash), [포털 매수 가능](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/trading/inquire-psbl-order).
- 지정가/가격정정은 상품별 호가단위와 당일 가격제한을 검증한다. 현재가의 `aspr_unit/stck_mxpr/stck_llam`과 적용 규칙의 유효성을 확인하고, 가격 구간을 넘는 정정도 검사한다. 방향별 반올림 정책 후 수량·예약·위험을 재계산한다. [현재가 포털](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/quotations/inquire-price).
- 주문번호·원주문·지점코드의 선행 0을 보존한다. 현금 주문 응답 `KRX_FWDG_ORD_ORGNO/ODNO/ORD_TMD`와 정정취소 응답의 필드 대소문자는 각 어댑터에서 정규화한다.
- 주문번호의 전역 유일성을 가정하지 않는다. 기본 식별 범위는 `mode/account/broker/trading_date/order_number`이며 지점·거래소도 보존한다. 원주문 참조와 WS 매칭에도 같은 범위를 사용한다.
- 취소 설명은 사전 정정취소 가능 조회를 요구하지만 해당 조회가 모의 미지원이다. 주문내역의 잔량·원주문으로 대체하는 경로는 **실제 모의 취소/정정 증거가 있어야 지원 완료**다.
- `hldg_qty`는 보유수량, `ord_psbl_qty`는 주문가능수량이다. 미체결 매도 전후의 예약 반영 시점은 문서만으로 확정하지 않는다. 브로커에 반영되지 않은 로컬 예약만 추가 차감한다.
- HTTP 200과 업무 성공 `rt_cd`를 모두 검사한다. 원장 혼잡 `EGW00215` 등 조회 실패에는 제한적 backoff를 둔다. 실패를 빈 보유로 변환하지 않는다. [잔고 포털](https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/trading/inquire-balance).
- 취소/정정 명령에도 결과 불명 상태를 둔다. 누적 `tot_ccld_qty/tot_ccld_amt/rmn_qty/cncl_cfrm_qty/rjct_qty/orgn_odno`와 원주문 chain을 보존해 투영을 복구한다.

### WebSocket 계약

모의 접속은 `ws://ops.koreainvestment.com:31000/tryitout`, 접속키는 REST 토큰과 별도 `/oauth2/Approval`에서 받는다. 구독 `tr_type=1`, 해제 `2`를 사용한다. 체결통보는 응답의 AES key/IV로 해독하고 접수/정정/취소/거부와 실제 체결을 구분한다. 샘플 일부 주석보다 포털과 공통 코드의 계약을 우선한다. [공통 코드](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/kis_auth.py), [사용자 WS 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_stock/domestic_stock_examples_ws.py).

JSON 구독 확인·PINGPONG과 시세 본문을 분리한다. 패킷의 데이터 건수만큼 파싱하고 목표/승인 구독 목록을 따로 저장한다. 재연결 시 key/IV·구독을 복구한 뒤 REST 누락 대조를 실행한다. 구독 ACK만 받았다고 장중 가격·계좌 체결 수신 성공으로 판정하지 않는다.

## 2. 원천별 수집과 과거 시점 계약

### 시장 데이터·종목·기업행동

한투 일봉은 `FID_ORG_ADJ_PRC=0` 수정주가, `1` 원주가이며 시장 `J/NX/UN`을 구분한다. 이번 전략의 KRX 정규장과 섞이지 않게 원천 시장/세션을 저장한다. 날짜별 원주가·수정 계열·조정 버전을 분리하고 최신 수정값을 과거에 알고 있었다고 가정하지 않는다.

공식 코스피 마스터 예제는 현재 파일을 읽는다. 따라서 이 예제만으로 과거 상장폐지 종목과 당시 구성종목이 완전하다고 판단할 수 없다. 현재 목록 수집과 과거 유니버스 복원은 별도 작업이다. [공식 마스터 코드](https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/kis_kospi_code_mst.py).

KRX OpenAPI 서비스 목록에는 유가증권 일별매매/종목기본정보와 ETF 일별매매가 있으며 2010년부터 제공한다고 안내한다. 실제 필드·조정 기준·폐지 종목 포함 여부·허용 사용 범위는 승인 후 검증한다. [서비스 목록](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd). 공식 자료/공시의 역사 정보가 부족하면 출처와 라이선스가 확인된 가져오기 어댑터를 제공한다.

수집 완료에는 날짜 창/page/checkpoint, 중복·역순·반복 페이지·빈 응답·휴장·상장 전/폐지 후 구간의 사유가 있어야 한다. 실제 거래일 누락을 전일 가격으로 채워 정상 봉으로 만들지 않는다. 거래량 0과 미수집은 다르다. 원본 정정은 덮어쓰기만 하지 않고 새 버전과 checksum을 남긴다.

기업행동은 공시/권리락/기준/효력/지급일과 비율·금액·통화·정정본을 구분한다. 분할·병합은 수량/가격을 조정하고 배당·분배금은 권리와 실제 지급 현금을 분리한다. 총수익 계열을 체결/평가에 쓰면서 현금 배당까지 더하지 않는다. KIS 배당일정/계좌 권리 조회가 전체 시장의 완전한 역사적 action 원장을 제공한다고 단정하지 않는다. [배당일정](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/ksdinfo_dividend/ksdinfo_dividend.py), [계좌 권리](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/period_rights/period_rights.py).

### ETF — E01

| 필드/계산 | 공급·검증 계획 |
| --- | --- |
| 상품 분류/지수/통화/헤지/레버리지·인버스 | KRX·발행사 상품 정보와 공시. 이름 문자열만으로 추정하지 않고 효력 기간 저장 |
| 총보수·기타 비용 | 발행사 공개 자료의 정의/기준일/단위 저장. 보수 하나를 모든 실제 비용으로 표시하지 않음 |
| 가격·거래대금·NAV/iNAV | KRX/KIS 지원 필드 표본 확인. 각각 시각·확정/추정 상태 구분 |
| 괴리율 | 같은 시점의 가격/NAV 계약이 성립할 때 `(가격/NAV-1)×100`. 해외 기초자산 휴장·시차로 비교 불가하면 사유 표시 |
| 추적차이·추적오차 | 같은 통화/기간/가격·총수익 정의의 ETF와 실제 benchmark를 사용. 차이는 수익률 차, 오차는 일별 차이 표준편차의 명시한 연율화 |
| 분배금 | KRX 분배금 목록 + KIND/발행사 원문으로 권리 기준일·락일·지급일 보완 |
| 겹치는 노출 | 지수/지역/통화/헤지/업종 우선. 개별 편입비중 이력이 없으면 정확한 look-through 분석은 불가 상태 |

KRX 분배금 화면은 최근 5년을 제공하고, ETF 가격 수익률에는 분배금이 반영되지 않는다고 안내한다. 조회 기간 밖 이력과 실제 지급일을 보완해야 한다. [분배금](https://m.krx.co.kr/contents/02/0207/JHPETP0207M01.jsp), [수익률 설명](https://m.krx.co.kr/contents/02/0204/020401/JHPETP020401M01.jsp), [기준일·지급예정일 공시 예](https://kind.krx.co.kr/external/2026/09/11/000441/20260911001119/68659.htm).

ETF 완료에는 데이터 없는 지표를 0으로 만드는 대신 누락·시차·benchmark 불일치를 보여주는 UI와 검증된 대표 ETF들의 전체 계산을 포함한다. 전체 대상의 필드 coverage도 함께 제시한다. 필수 전략 입력이 빠진 상품은 적격 대상에서 제외하고 사유를 남긴다.

### OpenDART 재무·공시 — F01

1. `corpCode`로 회사와 종목을 연결한다. 내부 instrument ID와 DART corp code를 분리하고 변경 유효기간을 보존한다. [고유번호](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018).
2. 공시검색 `list.json`의 `last_reprt_at=N`으로 최초/정정 보고를 함께 수집한다. `rcept_no`, 보고서명, `rcept_dt`, 법인/결산기를 보존한다. 날짜만 제공되는 `rcept_dt`를 정확한 시·분으로 꾸미지 않는다. [공시검색](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001).
3. 최신 재무표 조회와 과거 원본 보존을 구분한다. `fnlttSinglAcntAll`은 연도·보고서·연결/별도로 조회하며 접수번호를 입력받지 않는다. 이 결과만 과거 전체에 적용하면 당시 공시 값이 아닐 수 있다. [전체 재무제표](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020).
4. 접수번호를 받는 원문 `document.xml`, 원본 `fnlttXbrl.xml`을 활용해 개별 제출본을 보존한다. 실제 XBRL/원문에서 대상 계정·기간·정정 연결을 추출할 수 있는지 표본 검증한다. [공시 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019003), [XBRL 원본](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019019).
5. 정확한 공개시각을 확보하지 못하면 해당 공시 날짜가 끝난 **다음 KRX 거래 세션부터 사용**하는 보수적 규칙을 기록한다. 이후 정정 보고는 그 정정이 알려진 시점부터만 유효하다. 보고서 대상 분기 말에 이미 알고 있었다고 처리하지 않는다.

재무 정규화는 다음 계약을 사용한다. 이는 DART가 계산해 보장해 주는 값이 아니라 프로젝트 구현 규칙이다.

- 원 계정 ID/이름·원문 위치·단위·통화·연결(CFS)/별도(OFS)·기간형/시점형·지배주주 귀속 여부를 보존한다. 금액 문자열·괄호 음수·결측·계정명 변경을 검증한다.
- 연결이 있으면 연결을 기본 연구 집합으로 쓰되 별도 자료와 혼합하지 않는다. 금융업과 일반 제조업의 부채/현금흐름을 같은 순위식에 넣지 않고 업종 정책을 둔다.
- 분기값과 누적값을 구분한다. 누적 반기/9개월/연간에서 단독 분기를 만들 때 동일 회계 범위와 사용 시점의 이전 누적본을 사용한다. 부족 기간을 0으로 채우지 않는다.
- TTM은 호환되는 최근 4개 단독 분기 합이다. 매출/영업이익/순이익/CFO, 영업이익률, 평균 자본 기준 ROE, 부채비율, 현금흐름 대비 이익을 우선 구현한다. 평균 잔액과 귀속 범위를 식에 명시한다.
- 가치 지표 PER/PBR·earnings yield는 당시 가격·주식수/시가총액과 같은 귀속 범위의 이익/자본을 결합한다. 우선주/자사주·증자·분할 때문에 범위를 맞출 수 없으면 누락 사유를 반환한다. 음수 이익의 PER를 단순 저평가 순위로 쓰지 않는다.
- 업종별 비교, 이상치 처리, 결측 indicator와 피처 버전을 남긴다. 원문 추출/계정 매핑 실패를 근거 없는 숫자로 대체하지 않는다.

대표 기업의 최초/정정 보고, 연결/별도, 누적/분기, 결산기 변경, 음수 자본·적자, 금융업을 fixture와 실제 원문으로 검증한다. 연구 기간 전체의 당시에 이용 가능했던 값 확보가 G3의 선행 조건이다.

### 매크로·시장 국면 — M01

첫 원천은 FRED/ALFRED와 이미 수집한 국내 시장 데이터다. 다음 시리즈를 기본 집합으로 제안한다.

| 시리즈 | 의미 | 사용 방식 |
| --- | --- | --- |
| [VIXCLS](https://fred.stlouisfed.org/series/VIXCLS) | CBOE VIX | 주식시장 변동성 국면 입력 |
| [VXNCLS](https://fred.stlouisfed.org/series/VXNCLS) | CBOE Nasdaq-100 VXN | Nasdaq-100 관련 변동성 입력, VIX와 구분 |
| [DEXKOUS](https://fred.stlouisfed.org/series/DEXKOUS) | 달러당 원화 환율 | 환율 수준/변화, 제공 지연을 감안 |
| [DGS2](https://fred.stlouisfed.org/series/DGS2) / [DGS10](https://fred.stlouisfed.org/series/DGS10) | 미 국채 2/10년 | 금리 변화와 10년-2년 스프레드 |
| [BAMLH0A0HYM2](https://fred.stlouisfed.org/series/BAMLH0A0HYM2) | 미국 하이일드 OAS | 신용 스트레스 입력 |
| 국내 시장 폭 | 당시 분석 가능한 KOSPI 중 SMA120 상회 비율 등 | 당시 유니버스·유효 분모·누락률과 함께 산출 |
| 국내 투자자 수급 | KIS 투자자 자료의 개인/외국인/기관 | 당일 자료는 장 종료 후 제공, 장중 선행 입력으로 사용하지 않음 |

KIS 투자자 예제는 실전/모의 구분과 `FHKST01010900`, 장 종료 후 당일 제공을 안내한다. 실제 지원·보존 기간·확정 시각은 포털/소량 호출로 검증하고 역사 부족분은 승인된 공식 자료 가져오기로 보완한다. [공식 투자자 코드](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_investor/inquire_investor.py).

FRED 기본 조회는 현재 알려진 과거 값이다. `realtime_start/end`와 vintage로 당시 알려진 값을 구분해야 한다. [실시간 기간의 정의](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html), [관측값 API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html), [vintage 날짜](https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html).

`observation_date`는 실제 발표시각이 아니다. 일별 시리즈도 며칠 늦게 갱신될 수 있다. `vintage date`도 정확한 시각은 아니므로 원 발표시각이 없으면 원천 시간대의 해당 날짜 종료 이후 다음 운용 세션부터 쓰는 보수적 정책을 둔다. 미국 서머타임, 한국 거래일, 공급 지연, 수집 실패를 각각 처리한다. 과거 시점 검증은 source available 시각을, 실제 운용 재현은 당시 ingest 시각까지 포함한다.

허용 나이는 원천/빈도/정상 발표 일정에 맞춰 고정한다. 발표가 없는 휴일의 carry-forward와 수집 실패를 구분하고 미래 값을 역방향 채움하지 않는다. 레벨/변화/z-score의 rolling window와 최소 관측치를 피처 버전에 기록한다.

공통 거시값은 같은 날 모든 종목에 동일하므로 선형 순위 모델에 단순 추가하면 횡단면 순위를 거의 바꾸지 않을 수 있다. 시장 진입/위험 예산 또는 사전 정의한 기술지표와의 상호작용으로 역할을 분명히 한다. 모델 효과는 ablation으로 확인한다. 각 시리즈의 원 제공자 사용 조건은 FRED 접근 가능 여부와 별도로 기록한다.

## 3. 공통 데이터 스키마의 최소 계약

다음은 도메인/저장소/API에서 공유할 의미다. 모든 표를 즉시 하나의 DB 모델로 합치라는 뜻은 아니다.

| 객체 | 필수 의미 |
| --- | --- |
| `InstrumentIdentity` | 불투명 내부 ID·ISIN·문자열 단축코드·거래소·상품종류·코드/이름 유효기간. 영숫자 허용, 마스터로 검증 |
| `UniverseSnapshot` | 세션·그때 포함된 종목·제외 사유·출처·이력 완전성·버전. 오늘 목록으로 과거를 대체하지 않음 |
| `MarketBar` | instrument/세션/venue/session_type, OHLCV·실제 거래대금, raw/adjusted·조정 버전, 확정 여부·품질 |
| `CorporateAction` | 공시/수집/효력/락/기준/지급일, 비율·금액·통화, 원문·정정 관계·가격/수량/현금 처리 |
| `FinancialFact` | 회사/접수번호/계정/단위/기간/연결범위/귀속, 원값·정규값·공개 가능 시점·정정 버전 |
| `MacroObservation` | source/series/관측일/값/단위/빈도/vintage/available_at/수집시각/품질 |
| `FeatureSnapshot` | decision_at·기준 일봉·각 입력 available_at·dataset/feature 버전·결측 사유·coverage |
| `Signal` | 전략/모델/코드/데이터 버전·후보/제외 이유·순위·진입/청산 조건·유효 세션 |
| `AccountSnapshot/CashFlow` | 모드·계좌·운용 구간·시작 기준선·예수금·D+1/D+2 정산·미수 없는 주문가능액·평가액·입출금/초기화·대조 근거 |
| `ValuationSnapshot` | 계좌·모드·보유/현금/예약·종목별 시세 시각·허용 나이/시각 차이·잔고 버전 |
| `OrderIntent` | 불변 요청 ID/내용·payload hash·모드/계좌·거래일·전략/신호/위험/평가 snapshot·예약 |
| `PositionAllocation` | 운용 구성·전략·외부/수동 출처별 관리 수량·원가/손익·현금 배분. 전체 브로커 보유와 대조 |
| `BrokerOrder/Fill` | 모드/계좌/broker/거래일/주문번호 식별 범위·지점/시장·원주문 chain·수량/누적 금액·접수/체결/취소/거부·외부 주문 여부·대조 상태 |
| `DatasetManifest` | 원천/사용 범위/대상/기간/coverage/변환 버전/checksum, raw 자료 참조·생성 job |
| `Experiment/ModelArtifact` | target/피처/분할/purge/calibration/파라미터/seed/코드/환경/비용/체결 모델·평가·활성 상태 |
| `JobRun` | 고유 작업키·대상 세션·선행 작업·lease/heartbeat·실행권 세대·checkpoint·시작/종료/오류·재실행 관계 |

시간은 `period_end`(대상 기간), `source_published_at`(공개), `available_at`(정책상 사용 가능), `ingested_at`(수집)을 분리한다. source 시각의 정밀도·추정 규칙을 따로 저장한다. UTC 시각과 거래소 세션 날짜를 동시에 관리한다. 정정은 system time과 유효 기간을 보존해 과거 스냅샷을 덮어쓰지 않는다.

금액은 Decimal 또는 단위가 정의된 정수로 저장하고 JSON에서는 decimal 문자열을 쓴다. 지표/모델은 유한 실수만 허용하고 결측은 `null + missing_reason`이다. 도메인 내부 수량/거래량의 bool·NaN·inf·비정수 입력을 거부한다. API에서 수신한 문자열의 공백/빈값/부호와 거래량 단위를 정규화한 뒤 도메인에 넘긴다.

개인 계좌 원본은 공개 시장 원본과 보관 정책을 분리한다. 운영에 필요한 주문·체결 근거는 로컬 보호 저장소에 보존하되 비밀 헤더/키/토큰은 제거한다. Git에는 schema·코드·합성 fixture·비식별 보고만 들어간다. `.local/`의 실제 자료/모델/계좌/백업은 제외하고 필요 용량·보존기간은 측정 후 정책으로 설정한다.

## 4. 데이터 확보 단계의 종료 증거

각 원천은 **계약 fixture 통과 → 실제 소량 조회/원문 비교 → 전체 수집 coverage → PIT 재생 검증**의 네 단계를 별도 상태로 기록한다. API 키가 있다는 사실은 네 단계 중 실제 데이터 검증을 대신하지 않는다.

최소 표본은 일반주식·금융업·상장폐지/정지 사례·국내 지수 ETF·국내 상장 미국 지수 ETF·영숫자 코드 ETF, 최초/정정 재무, 배당/분할/분배금, 미국 휴일/DST/매크로 수정 사례를 포함한다. 필요한 과거 데이터가 없는 사례는 제공 범위 밖인지 수집 실패인지 구분해 보고한다.

재현 fixture: 상장/폐지·코드 변경·거래 정지·누락 봉·분할·분배금·정정 공시·지연 발표·시점 누출·갭 하락·동일일 손절/익절 충돌. 미래 데이터 행을 주입했을 때 계산 입력에서 차단되는 음성 테스트도 포함한다.

백테스트의 날짜 범위·종목 집합·기업행동/benchmark/재무/vintage 가용성·제외 비율을 실행 전 manifest로 고정한다. 손실 종목/불편한 기간을 결과를 본 뒤 제거한 실행을 같은 최종 검증으로 표시하지 않는다. 해당 manifest와 입력 hash로 같은 결과를 재생할 수 있어야 한다.
