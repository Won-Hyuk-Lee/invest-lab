# invest-lab — 로컬 매매 도구

AI·외부 스크립트가 만든 매매 판단을 받아 **주문 검사 → 전송 → 체결/잔고 대조**를 처리하는 개인용 로컬 도구입니다. Python/FastAPI 백엔드, SQLite 장부, React 화면으로 구성합니다.

이 프로젝트가 종목을 선정하거나 투자 전략을 최적화하는 일은 더 이상 기본 목적이 아닙니다. 기존 코스피·미국 ETF 백테스트와 연구 문서는 [보관 자료](docs/research-archive.md)로 유지합니다. 기본 실행에서는 기존 전략의 자동 매매와 연구 작업을 실행하지 않습니다.

## 할 수 있는 것

- 로컬 화면에서 계좌·보유·외부 판단·주문/체결 상태 확인
- AI나 외부 프로그램의 JSON 판단을 HTTP API 또는 CLI로 전달해 자동 주문 요청
- 판단 ID 중복 방지, 5분 이내 만료, 허용 종목, 주문 금액·보유 비중 검사
- 지정가/시장가 매수·매도, 정정·취소, 결과 불명 주문과 계좌 대조
- 외부 자동 주문 중지, 신규 매수 중지, 작업자 재시작, 로컬 백업·복원

**기본은 합성 시세와 계좌를 사용하는 `local_simulation`입니다.** 외부 브로커 연결은 한국투자증권 **국내 주식/국내 상장 ETF 모의계좌**(`kis_paper`)와 **실전계좌**(`kis_live`)를 지원합니다. 미국 상장 QQQ/SPY 직접 주문은 구현하지 않았습니다. AI 모델 호출/구독을 내장하지 않으므로 원하는 AI나 스크립트에서 판단 JSON을 생성합니다.

## 내 PC에서 실행

Windows PowerShell, 저장소 루트 기준입니다. Git·uv·지원 Node.js가 필요합니다. Python 3.12와 의존성은 실행기가 준비합니다. Node는 24.x의 24.15.0 이상을 권장합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

화면은 <http://127.0.0.1:8000>, API 문서는 <http://127.0.0.1:8000/docs>입니다. 기본 화면에서 허용 종목과 한도를 저장하고, 판단 예시를 만들어 **전송 없이 검사 → 외부 자동 주문 허용 → 주문 요청** 순서로 로컬 흐름을 확인할 수 있습니다. 작업자는 실행기가 함께 시작합니다.

- 새 외부 연결은 기본 중지입니다. 기존 연구 전략의 활성화 상태를 가져와도 기본 작업자는 그 전략을 실행하지 않습니다.
- 화면의 자동 주문 중지는 아직 전송하지 않은 외부 판단을 막습니다. 이미 접수한 주문은 주문·체결 화면에서 취소합니다.
- `Ctrl+C`로 API와 작업자를 종료합니다. 브라우저만 닫으면 프로세스는 계속 실행합니다. PC 종료/절전 중에는 실행되지 않습니다.
- 첫 설치 이후 코드가 바뀌지 않았다면 `-SkipSetup`, 포트 변경은 `-Port 8001`을 사용합니다.
- 서버는 루프백 주소에서 사용합니다. 같은 계좌는 한 PC의 작업자만 실행합니다.

## AI·스크립트 연결

AI의 매매 결정을 다음 계약으로 변환합니다. `client_request_id`는 **하나의 판단에 하나의 UUID**를 발급해 파일/프로그램에 보관합니다. `expires_at`은 현재부터 5분 이내의 시간대 포함 시각으로 설정합니다. 아래 예시 시각은 실행 시 새 값으로 교체해야 합니다.

```json
{
  "client_request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "symbol": "005930",
  "side": "buy",
  "quantity": 1,
  "order_type": "limit",
  "limit_price": "70000",
  "source": "my-ai-agent",
  "reason": "외부 AI가 생성한 이번 판단의 근거",
  "expires_at": "2026-09-30T15:00:00+09:00"
}
```

| 목적 | HTTP API |
| --- | --- |
| 외부 연결/한도/작업자/최근 판단 | `GET /api/trading` |
| 사용할 수 있는 종목 | `GET /api/trading/instruments` |
| 종목 시세 | `GET /api/trading/quotes/{symbol}` |
| 계좌·보유 | `GET /api/portfolio` |
| 한도·허용 종목·외부 자동 주문 설정 | `POST /api/trading/control` |
| 전송 없이 판단 검사 | `POST /api/trading/signals/preview` |
| 판단 등록 + 작업자 주문 요청 | `POST /api/trading/signals` |
| 같은 판단 ID로 결과 조회 | `GET /api/trading/signals/{client_request_id}` |
| 주문·체결·정정·취소 | 기존 `/api/orders`와 주문 상세 API |

등록 응답은 **체결 완료가 아니라 큐 등록**입니다. 작업자가 최신 시세·잔고·장 시간·설정을 다시 검사해 전송하고, 브로커 조회로 체결을 확인합니다. 응답이 유실되면 같은 ID를 조회하고 새 ID로 같은 주문을 만들지 않습니다. 동일 판단의 재요청은 기존 주문을 반환하며 다시 큐에 넣지 않습니다.

CLI도 같은 서비스와 장부를 사용합니다. `signal.json`은 `.local`에 둡니다.

```powershell
.\scripts\manage.ps1 trading-status
.\scripts\manage.ps1 trading-control .\.local\agent-control.json
.\scripts\manage.ps1 signal-preview .\.local\signal.json
.\scripts\manage.ps1 signal-submit .\.local\signal.json
.\scripts\manage.ps1 signal-status '판단 UUID'
```

전체 설정 JSON, 로컬 매수→매도 확인, AI 연결 시 책임 구분과 오류 처리: **[로컬 매매 실행 안내](docs/local-trading.md)**.

## 한투 모의계좌

저장소 루트의 `.env`에 모의계좌 설정을 준비합니다. 기존 `.env`를 덮어쓰지 않습니다.

```powershell
.\scripts\manage.ps1 --mode kis_paper doctor
.\scripts\manage.ps1 --mode kis_paper paper-read-check
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Mode kis_paper
```

읽기 확인은 주문을 보내지 않습니다. 모의 주문에는 `.env`의 `KIS_PAPER_ORDERS_ENABLED=true`, 검증된 종목 마스터·개장 달력, 화면의 외부 자동 주문 허용/신규 매수 허용이 별도로 필요합니다. 연구 데이터셋·모델 학습·전략 선택은 외부 판단 주문의 전제 조건이 아닙니다.

설치와 PC 전환은 [작업 환경 안내](docs/workstation-setup.md), 모의 준비와 복구는 [로컬 매매 안내](docs/local-trading.md)를 따릅니다. 이 구현 변경의 검증에는 외부 모의 주문을 사용하지 않습니다.

## 실전계좌 연결

모의 키를 재사용하지 않고 `.env.example`의 **`KIS_LIVE_*`** 항목에 별도 실전 키·계좌를 설정합니다. 기본 `KIS_LIVE_ORDERS_ENABLED=false`는 조회만 허용합니다. 실제 주문을 사용할 때만 이 값을 `true`로 바꾸고, 화면에서 외부 자동 주문/신규 매수를 별도로 허용합니다. 구현 검증에서는 실제 주문을 전송하지 않았습니다.

```powershell
.\scripts\manage.ps1 --mode kis_live doctor
.\scripts\manage.ps1 --mode kis_live broker-read-check
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Mode kis_live
```

`broker-read-check`는 현재가·계좌·주문 이력을 읽고 대조하며 매수/매도/정정/취소를 전송하지 않습니다. 실전 화면에는 **한투 실전계좌**를 표시하고 운영 장부/토큰 범위는 모의와 분리합니다. 대상 종목 마스터와 검증된 개장 달력도 준비해야 합니다. 실전의 체결 확인은 REST 대조를 사용합니다.

구체적인 준비와 설정은 [실전 연결 절차](docs/local-trading.md#실전계좌-연결)를 따릅니다.

## 저장·개발

- `backend/src/invest_lab/application/agent_trading.py`: 외부 판단 계약/등록/한도 설정
- `application/execution.py`: 주문 검사·중복·전송·정정·취소·체결 대조
- `adapters/http.py`, `cli.py`: HTTP API와 로컬 명령
- `worker.py`: 주문 큐·계좌 대조 작업자
- `frontend/src/features/trading.tsx`: 기본 매매 콘솔
- `.local/`: 실행 장부·로그·로컬 자료, `.env`: 비밀값. 둘 다 Git 제외

다른 PC로 코드만 옮길 때는 Git을 사용합니다. 계좌 설정은 `.env`, 기존 주문 이력은 백업 bundle을 별도로 옮깁니다. `.venv`와 `node_modules`는 복사하지 않습니다. 복원하면 외부 자동 주문과 신규 매수가 중지되고 미종료 주문은 확인 대상으로 격리됩니다.

개발·테스트·OpenAPI 생성·브라우저 확인은 [개발 안내](docs/development.md), 이번 변경의 결과는 [전환·검증 기록](docs/local-trading-transition-2026-09-30.md), 저장소 작업 원칙은 [AGENTS.md](AGENTS.md)에 있습니다. 연구 자료는 [연구 보관 목록](docs/research-archive.md)에서만 참고하며 수익을 보장하거나 자동 채택하지 않습니다.
