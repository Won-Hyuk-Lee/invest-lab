# main 통합·검증·비밀정보 확인 — 2026-10-22

사용자 요청에 따라 개발 작업 전체를 [PR #1](https://github.com/Won-Hyuk-Lee/invest-lab/pull/1)로 `main`에 병합했다. 병합 커밋은 `c268ee1`이며 개발 브랜치의 14개 커밋을 보존했다. 검증한 개발 코드 `b9a9282`와 병합 직후 `main`의 파일 트리가 같음을 확인한 뒤 로컬·원격의 개발 브랜치를 삭제했다. 이후 README와 PC 동기화·인계 안내를 `main`에서 갱신한다.

## 현재 구현과 검증

현재 기본 제품은 외부 AI/스크립트의 판단을 실행하는 로컬 매매 도구다. `local_simulation`이 기본이고 `kis_paper`/`kis_live`는 별도 키·계좌·장부·주문 허용 설정을 사용한다. 내장 연구 전략은 기본 실행에서 자동 주문하지 않는다. 실제 사용 절차는 [README](../README.md)와 [로컬 매매 안내](local-trading.md)를 따른다.

| 검사 | 이번 결과 |
| --- | --- |
| 백엔드 회귀 | 426개 통과, 기존 Starlette/httpx 사용 중단 예정 경고 1건 |
| Python lint·format | 통과, 형식 검사 90개 파일 |
| OpenAPI 프론트 계약·Prettier | 통과 |
| 프론트 단위 | 5개 파일, 11개 테스트 통과 |
| 프로덕션 빌드 | 통과 |
| Playwright/Edge | 7개 테스트 통과 |
| 컴파일 화면 | 직접 진입·새로고침 4개 경로 통과, JS 오류·쓰기 요청 없음 |

검증은 별도의 상태/DB를 사용하는 `local_simulation`에서 수행했다. 외부 브로커 연결·모의/실전 주문은 전송하지 않았고 기존 계좌 DB를 초기화하지 않았다. 검증 서버 종료와 포트 해제도 확인했다. 문서 갱신에서는 링크·PowerShell 예시 구문·현재 CLI 경로를 확인하며 코드 테스트를 불필요하게 반복하지 않는다.

개발 head의 기존 GitHub Verify는 성공 상태였다. 새 PR Verify는 GitHub 실행 대기 상태여서 위 로컬 전체 검증과 동일 head의 기존 성공을 확인하고 병합했다. 이후 새 원격 검사까지 통과했다고 간주하지 않는다. 현재 상태는 [GitHub Actions](https://github.com/Won-Hyuk-Lee/invest-lab/actions)에서 확인한다.

## API 키·토큰 노출 검사

**확인한 범위에서 실제 API 키·토큰 노출 증거를 찾지 못했다.** 현재 `.env`가 Git 제외라는 사실만으로 결론 내리지 않고 다음을 함께 검사했다.

- 병합 전 전체 Git 이력: 27개 커밋 메시지, 276개 과거 파일 객체와 모든 도달 가능한 브랜치/태그. 삭제된 파일도 객체 기준으로 포함했다.
- 로컬 `.env`, Git 제외된 과거 설정 백업, 현재 GitHub 인증에서 얻은 민감값 6개를 메모리 안에서 정확 비교했다. 원문·Base64·URL 인코딩·UTF-16 변형을 확인했고 실제 값이나 매치 본문은 출력/공유 문서에 남기지 않았다.
- 공식 배포 체크섬을 검증한 **Gitleaks v8.30.1**로 전체 Git 변경 이력과 파일 객체/커밋 메시지 전체 내용도 검사했다.
- 접근 가능한 PR #1 본문·119개 파일 패치·14개 커밋 메시지, 게시물/댓글 목록, 완료된 Actions 6회의 로그 12개를 추가 검사했다. 당시 릴리스·Actions 아티팩트·댓글은 없었다.
- `.env`와 `.local/` 파일이 과거에 추적된 이력은 없었다. 기존 GitHub 인증은 메모리에서만 사용하고 외부 저장소의 서명된 로그 URL에는 인증 헤더를 전달하지 않았다.

Gitleaks가 표시한 1건은 `backend/tests/test_kis.py`의 WebSocket 테스트에 사용하는 반복 16진수 암호화 키/IV다. 코드 문맥과 실제 설정값 대조로 합성 fixture임을 확인했으며 실제 유출로 세지 않았다. 실제 키가 아니라는 판정 때문에 전역 탐지 규칙을 끄거나 소스에 광범위한 예외를 추가하지 않았다.

저장소는 비공개이고 GitHub 자체 secret scanning은 비활성 상태였다. 이 기록은 별도로 수행한 감사 결과다. 삭제·만료된 로그/게시물, 원격에서 접근할 수 없는 객체, 외부 복제본과 알 수 없는 난독화 비밀값까지 완전한 부재를 증명하지는 않는다. 검사 중 대기 중인 새 CI 실행의 로그는 완료 로그 검사에 포함하지 않았다. 병합·문서 변경분도 최종 푸시 전 추가 검사한다.

## 별도로 남은 항목

- npm production 의존성 감사는 0건이었다. 개발 도구 의존성에는 high 2개, moderate 2개가 보고됐다(`vite`, `source-map-js`, `vitest`, `@vitest/mocker`). 비밀키 유출과는 다른 항목이며 이번 브랜치 통합에서 잠금 파일을 임의 갱신하지 않았다.
- 실제 모의/실전 계좌의 장중 접수·체결·정정·취소와 연속 운용은 이 오프라인 검증으로 완료 처리하지 않는다.
- API 키나 `.local`의 운영/연구 자료는 계속 Git에 올리지 않는다. 다른 PC에서는 코드만 `main`에서 받고 데이터는 [PC 이전 안내](workstation-setup.md)를 따른다.

원문 없는 감사 요약과 로컬 검증 결과는 `.local/repository-maintenance-20261022/`에 보관한다. `validation-summary.json`, `secret-audit-summary.json`, `exact-secret-audit.json`, `all-objects-pattern-audit.json`, `github-surfaces-secret-audit.json`을 사용하며 이 디렉터리는 Git 제외다.
