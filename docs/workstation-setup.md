# 다른 PC에서 시작하고 이어가기

집과 사무실의 **Windows PC**에서 같은 저장소로 작업하는 절차다. 폴더 경로와 Windows 사용자명은 같을 필요가 없다. 아래 명령은 별도 설명이 없으면 `invest-lab` 저장소 루트의 PowerShell에서 실행한다.

| 목적 | 준비물 |
| --- | --- |
| 화면/샘플 연구를 새로 실행 | 저장소 코드 + Git/uv/Node.js |
| 한투 모의계좌 조회 | 위 항목 + 저장소 루트의 `.env` |
| 기존 전략·수집 자료·연구·주문 이력까지 계속 사용 | 위 항목 + 기존 PC의 데이터 백업 bundle |
| 실제 모의 자동 운용 | 키 외에 거래 달력·시장 자료·대조·운용 설정 준비. [사용자가 할 일](user-actions.md) 참고 |

Git에는 코드와 문서가 들어간다. `.env`와 실행 데이터는 Git에 없으므로 **pull만으로 기존 계좌 설정이나 연구 기록이 따라오지 않는다.**

## 1. 새 PC에서 한 번 설치

| 도구 | 설치와 기준 |
| --- | --- |
| Git | [Git 공식 Windows 설치 안내](https://git-scm.com/install/windows). 비공개 저장소에 접근할 GitHub 계정으로 인증한다. |
| uv | [uv 공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/). Windows에서는 `winget install --id=astral-sh.uv -e`도 가능하다. |
| Node.js + npm | [Node.js 공식 다운로드](https://nodejs.org/en/download)에서 **24.x의 24.15.0 이상**을 선택한다. npm을 포함해서 설치한다. |

설치 후 PowerShell을 새로 열고 확인한다.

```powershell
git --version
uv --version
node --version
npm.cmd --version
```

정확한 Node 허용 범위는 `frontend/package.json`의 `engines`가 기준이다. Codex나 특정 IDE는 실행에 필요하지 않다. 실행 스크립트에 Codex의 Node를 찾는 보조 경로가 있지만, 사무실 PC에는 요구 버전을 정상 설치하면 된다. Python은 uv가 `backend/.python-version`의 **3.12**를 준비하므로 별도 가상환경을 복사할 필요가 없다.

처음에는 Python/npm 패키지를 다운로드하므로 인터넷이 필요하다. 회사 프록시나 설치 정책으로 막히면 해당 환경의 승인된 설치·인증서 절차를 사용한다. 저장소 코드에서 TLS 검증을 끄는 방식으로 해결하지 않는다.

## 2. 저장소 받기

예시는 사용자 폴더 아래 `source`를 사용한다. 다른 작업 폴더로 바꿔도 된다.

```powershell
$workRoot = Join-Path $env:USERPROFILE 'source'
New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
Set-Location $workRoot
git clone https://github.com/Won-Hyuk-Lee/invest-lab.git
Set-Location .\invest-lab
```

비공개 저장소 접근 오류가 나면 GitHub 로그인 계정과 저장소 접근 권한을 확인한다. 토큰을 clone 주소에 넣지 않는다. 이미 클론했다면 다시 clone하지 말고 아래의 코드 동기화 절차를 따른다.

## 3. 우선 키 없이 실행 확인

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

첫 실행에서 백엔드 설치 → 프론트 설치/빌드 → API와 작업자 실행이 진행된다. 완료되면 <http://127.0.0.1:8000>을 연다. 화면에 로컬 샘플 모드가 표시되는지 확인한다. `/api/health`와 `/docs`도 같은 주소에서 확인할 수 있다.

- `Ctrl+C`: 이 실행기가 시작한 API와 작업자 종료. 브라우저만 닫으면 계속 실행한다.
- `-Port 8001`: 포트가 이미 사용 중일 때 다른 포트로 실행한다.
- `-SkipSetup`: 설치/빌드가 끝난 뒤 코드가 그대로일 때만 사용한다. 새 clone 또는 pull 직후에는 생략한다.
- 절전/PC 종료 중에는 수집과 주문 감시가 실행되지 않는다.

## 4. `.env` 준비와 모의 연결

집 PC의 `.env`를 별도 안전한 전달 방식으로 가져와 **`invest-lab/.env`**에 둔다. 위치는 `README.md`, `.env.example` 옆이다. `backend/.env`나 `frontend/.env`에 넣지 않는다. 탐색기에서 확장자를 표시해 `.env.txt`가 되지 않았는지 확인한다.

기존 파일이 없고 새로 입력할 때만 아래 예시를 복사한다. 이미 있는 `.env`는 덮어쓰지 않는다.

```powershell
if (-not (Test-Path -LiteralPath .\.env)) {
    Copy-Item -LiteralPath .\.env.example -Destination .\.env
}
```

편집기로 모의 키·시크릿·계좌번호·상품 코드·HTS ID를 입력하고 `KIS_PAPER_ORDERS_ENABLED=false`로 시작한다. DART/FRED/KRX 키는 해당 자료 수집에만 추가로 필요하며 샘플 실행 조건은 아니다. 값 전체를 채팅·스크린샷·로그에 올리지 않는다.

```powershell
# 값 출력 없이 설정 상태 확인
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 --mode kis_paper doctor

# 실제 모의계좌 조회/대조. 주문 전송 없음
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 --mode kis_paper paper-read-check

# 앞서 실행한 샘플 서버를 종료한 뒤 실행
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Mode kis_paper
```

`doctor`에서 키 준비 여부를 확인하고 `paper-read-check`의 조회·대조 결과를 확인한다. 기존 계좌 기록이 있는 환경은 대조 상태가 `matched`인지 확인하고, 차이가 있으면 [복구 절차](user-actions.md)를 따른다. 조회 진단도 로컬 계좌/대조 기록을 만들지만 주문을 전송하지는 않는다.

모드가 다르면 현금/종목이 다르게 보이는 것이 정상이다. 샘플은 합성 계좌, 모의는 실제 한투 모의계좌다. `.env`만 가져온 새 환경에는 수집 데이터·이전 전략·주문 의도가 없다. 기존 운용을 이어갈 목적이면 다음 백업/복원을 먼저 진행한다.

## 5. 집 ↔ 사무실에서 코드 이어가기

작업 시작 시 저장소 루트에서:

```powershell
git status --short --branch
git pull --ff-only origin main
```

이 저장소의 기본 작업 브랜치는 `main`이다. 다른 작업 브랜치에 있으면 해당 작업의 브랜치 절차를 따른다. 변경 사항이 남았으면 먼저 확인하고 보존한다. fast-forward가 불가능하면 중단하고 분기된 커밋을 비교하며 `reset --hard`나 강제 push로 맞추지 않는다.

pull 이후에는 다시 설치/빌드하는 실행 명령을 쓴다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1
```

모의 실행이 목적이면 `-Mode kis_paper`를 붙인다. 이미 실행 중인 서버/작업자는 먼저 종료한다. 작업을 마치면 변경 파일을 검토해 관련 파일만 커밋·푸시하고 다른 PC에서 받는다. AI 도구에는 저장소의 [AGENTS.md](../AGENTS.md)를 먼저 읽고 진행하도록 요청하면 된다.

`.env`, `.local`, `.venv`, `node_modules`는 동기화되지 않는다. **집과 사무실에서 같은 모의계좌 작업자를 동시에 돌리지 않는다.** 서로의 실행권·호출 예산·주문 의도를 공유하지 않기 때문이다. 사무실에서 코드 수정이나 샘플 실행만 할 때는 기존 모의 운영 데이터를 옮길 필요가 없다.

## 6. 기존 데이터까지 이전하기

기본 저장 위치는 `.local/`이다. 단순 개발/샘플 확인이면 이 절차를 건너뛸 수 있다. 계속 운영할 PC 하나를 정하고 그 PC의 데이터가 최신이라는 기준을 유지한다. DB 파일을 Git으로 병합하거나 실행 중인 `.local`을 동기화 프로그램으로 맞추지 않는다.

한글 경로가 포함된 백업/복원 결과를 정확히 읽도록 **각 PC에서 명령을 실행할 PowerShell 창**에 먼저 UTF-8 출력을 설정한다. 이 설정은 현재 실행 환경에만 적용한다.

```powershell
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
```

### 기존 PC

1. 통합 실행 창에서 `Ctrl+C`로 종료한다. 별도로 켠 API/작업자 창도 종료한다.
2. 사용 중인 코드 커밋을 `git rev-parse HEAD`로 기록한다. 사용자 정의 저장 경로를 썼다면 해당 환경 변수를 같은 셸에 설정한다.
3. 다음 명령으로 백업한다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 --mode kis_paper backup
```

반환된 `path`의 **bundle 폴더 전체**를 가져간다. `manifest.json`, `manifest.sha256`, `databases/`, 존재하는 연구 artifact를 함께 보존한다. 이 명령은 해당 모드의 운영 DB, workspace, 연구 DB/artifact, 시장 DB를 묶는다. 다른 모드의 운영 기록이 필요하면 `--mode local_simulation`으로 별도 백업한다.

`.env`와 토큰 캐시는 bundle에 포함되지 않는다. `.env`는 별도로 전달한다. 임의 입력 원본·로그·로컬 메모 전체를 백업하는 기능도 아니므로 `.local/inputs/` 등 별도 보관이 필요한 파일은 따로 챙긴다. 백업에는 계좌/주문 이력이 있으므로 Git에 올리지 않는다.

### 새 PC

1. 기존 PC와 같은 코드 버전으로 설치한 뒤 `.env`를 준비한다. 백업 이후 코드의 DB 형식이 바뀌었다면 해당 버전의 이전 절차부터 확인한다.
2. bundle 전체를 `.local/imports/from-home/`에 둔다. 그 폴더 바로 아래에 `manifest.json`이 있어야 한다.
3. 앱/작업자를 실행하지 않은 상태에서 **아직 없는 새 폴더**로 복원한다.

```powershell
$bundlePath = Join-Path $PWD '.local\imports\from-home'
$restoredState = Join-Path $PWD '.local\restored-office'
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 --mode kis_paper restore $bundlePath $restoredState
```

복원 결과를 확인한다. `.env`의 계좌/상품 코드와 백업의 계좌 구분이 같아야 한다. 기존 복원 폴더가 있으면 새 이름을 사용한다. 무결성 검사 실패를 무시하거나 기존 DB를 덮어쓰지 않는다.

4. **같은 PowerShell 창**에서 복원 경로를 지정하고 진단/실행한다.

```powershell
$env:INVEST_STATE_DIR = $restoredState
$env:INVEST_DB_PATH = Join-Path $restoredState 'workspace.db'
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 --mode kis_paper doctor
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\manage.ps1 --mode kis_paper paper-read-check
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run.ps1 -Mode kis_paper
```

새 PowerShell 창을 열 때마다 두 환경 변수를 다시 설정한다. 설정하지 않으면 기본 `.local`의 다른 DB가 열려 데이터가 사라진 것처럼 보일 수 있다. 위 설정을 저장소 루트에서 실행하는 개인용 `.local/start-office.ps1`에 보관해도 되지만 Git에는 넣지 않는다. `.env`에 경로를 적는 것만으로 런처에 적용되지는 않는다.

복원하면 신규 진입과 자동 운용은 꺼지고, 미종료 주문은 `unknown`으로 격리되며 대기 작업은 중단된다. 이는 자동 재전송을 막는 의도된 동작이다. 전체 계좌/주문 대조와 격리 주문 해결 후 [사용자가 할 일](user-actions.md)의 재개 절차를 따른다. 나중에 집으로 다시 옮길 때도 **최신 운영 PC에서 새 백업**을 만들어 같은 절차를 진행한다.

## 7. 환경 변수와 자주 막히는 부분

| 설정 | 지정 위치와 동작 |
| --- | --- |
| `KIS_PAPER_*`, DART/FRED/KRX 키 | 저장소 루트 `.env`. 프로세스에 같은 키가 있으면 환경 변수가 우선한다. 수정 후 재시작한다. |
| `INVEST_MODE` | 프로세스 환경 변수. 통합 실행은 `-Mode` 인자를 우선 사용한다. 기본 `local_simulation`. |
| `INVEST_STATE_DIR` | 프로세스 환경 변수, 절대 경로 권장. 운영/시장/연구 DB와 artifact/log 경로. 기본 저장소 `.local`. |
| `INVEST_DB_PATH` | 프로세스 환경 변수, 절대 경로 권장. workspace DB. 상태 이전 시 위 경로와 함께 지정한다. |

| 증상 | 확인/해결 |
| --- | --- |
| `uv`/`npm`을 찾을 수 없음 | 설치 후 새 터미널에서 버전을 확인한다. Node 설치에 npm이 포함됐는지 확인한다. |
| Node 버전 오류 | `node --version`과 `frontend/package.json`을 비교한다. 예전 Node 20이나 24.15 미만의 24.x는 사용하지 않는다. |
| PowerShell에서 `npm.ps1` 실행 차단 | `npm.cmd`를 사용한다. 저장소 스크립트는 문서처럼 `powershell -NoProfile -ExecutionPolicy Bypass -File ...`로 호출한다. |
| 포트 사용 중 | 이전 실행을 종료하거나 `run.ps1 -Port 8001`을 사용한다. 해당 주소의 포트도 바꾼다. |
| pull했는데 예전 화면 | 실행을 종료하고 `-SkipSetup` 없이 다시 실행한다. |
| 관리 명령 결과의 한글 경로가 깨짐 | 위 UTF-8 설정을 적용한다. 백업은 현재 상태 폴더의 `backups/` 아래 실제 디렉터리를 확인하고, 깨진 출력 경로를 그대로 복사하지 않는다. |
| KIS 필드 누락/접속 실패 | `.env`의 위치/확장자, 모의 키와 계좌 필드, `doctor` 결과를 확인한다. 키 값은 출력하지 않는다. |
| 연구/주문 작업이 대기 중 | 작업자 실행과 `.local/logs`를 확인한다. 개발 모드는 API와 작업자를 각각 켜야 한다. |
| 복원 뒤 데이터가 비거나 운영이 중지됨 | 모드와 두 DB 경로를 확인한다. 복원 직후 진입/자동 운용 중지는 정상이며 대조 후 재개한다. |

개발 서버 분리 실행, 테스트와 Linux/macOS 실행은 [개발 안내](development.md)에 있다. 이 프로젝트는 현재 localhost 사용을 전제로 하며 `.env`만 복사해서 공개 클라우드 서비스로 배포하는 구성은 아니다.
