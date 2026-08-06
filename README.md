# 코드 수정 에이전트

검증된 코드 리뷰 이슈를 다시 확인한 뒤 대상 프로젝트 규칙에 맞춰 수정하고 설정된 remote와 target branch에 반영한다. finding 사실 여부, 원격 merge와 수정 결과의 판단 사유를 SQLite에 남기며 정책과 테스트를 통과하지 못한 작업은 push하지 않는다.

## 빠른 시작

```bash
cd /Users/brokenclaw/hermes-workspace/code-fix-agent
python3 -m venv .venv
.venv/bin/python -m pip install -e .

export CODE_FIX_TOKEN='replace-with-a-secret'
export MATRIX_MOBILE_FIX_GITHUB_TOKEN='repository-scoped-token'
export TESTSQUARE_FABLE_FIX_GITHUB_TOKEN='repository-scoped-token'
# fix-agent.toml에서 해당 저장소의 Discord enabled를 true로 바꾼 경우
export MATRIX_MOBILE_FIX_DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'

.venv/bin/fix-agent serve --config fix-agent.toml
```

서버는 기본값으로 `127.0.0.1:7081`에서 실행된다. `/reviews`는 Bearer token이 있는 `version = 1` 리뷰 이벤트를 받고 `/health`는 서버 상태를 반환한다. 같은 장비에서 연동한다면 서버와 수신 token 없이 `fix-agent submit`으로 이벤트를 전달할 수도 있다. push와 PR 생성은 TOML 직접 token, 저장소별 환경 변수, PC의 `gh auth` 인증 순으로 GitHub 인증을 선택한다.

HTTP 수신 token은 TOML 직접값과 환경 변수 중 하나를 선택한다. TOML에 `server.token`을 넣으면 `server.token_env`를 같이 적지 않는다. GitHub 인증은 `github_token`, `github_token_env`의 실제 값, `gh auth token --hostname github.com` 순으로 찾는다. PC에서 `gh auth login`을 완료했다면 GitHub token 설정을 생략할 수 있다.

```toml
[server]
token = "CODE_FIX_TOKEN으로 쓸 긴 난수"

[[repositories]]
# 다른 저장소 설정 생략
github_token = "저장소 쓰기 권한 GitHub token"
```

`CODE_FIX_TOKEN`은 외부에서 발급받는 값이 아니다. HTTP 송신자와 fix agent가 같이 아는 공유 비밀값으로, 기술적으로는 임의의 비어 있지 않은 문자열이면 된다. 추측 공격을 막으려면 `openssl rand -hex 32`로 생성한 64자 난수를 사용한다. `fix-agent submit`만 쓰고 `serve`를 실행하지 않으면 수신 token이 필요 없다.

직접 token이 든 설정은 Git에 commit하지 않는다. `.gitignore`에 포함된 `fix-agent.local.toml`을 쓰고 권한을 `0600`으로 제한하는 방식이 안전하다.

```bash
curl http://127.0.0.1:7081/health
.venv/bin/fix-agent jobs --config fix-agent.toml --json
.venv/bin/fix-agent events --config fix-agent.toml --after-id 0 --json
```

## 처리 범위

- `Matrix_Mobile_V2`, `TestSquare-Fable`의 `dev` branch 설정 포함
- Major·Minor finding 처리, Critical 기본 제외
- 대상 `AGENTS.md`, 추가 지침과 저장소 하네스 적용
- fingerprint 중복 방지, 독립 사실 검증과 사유 기록
- 저장소별 `remote`, `target_branch`, direct push 또는 PR 방식 선택
- finding별 최신 target 기반 detached worktree, 원격 이동 시 merge·재검증
- 이전 실패 원인과 하네스 출력을 반영한 순차 재시도, push·worktree 정리 뒤 완료 처리
- append-only 작업 event 기록과 Discord notifier용 순차 event ID
- `code-review-agent` 규격 호환 저장소별 Discord 알림, 검증·수정 단계 통지와 실패 재시도
- Crontrol에 현재 repository, job ID, 처리 단계와 대기 건수 자동 동기화

Discord 알림은 `fix-agent.toml`의 저장소별 `[repositories.discord]`에서 켠다. 알림을 받을 저장소는 `enabled = true`로 설정한다. `webhook_url`을 직접 넣거나 `webhook_url_env`에 적힌 환경 변수로 주소를 전달하면 `serve`와 `run-once`가 작업 이벤트를 전송한다. 발송을 멈추려면 해당 저장소의 `enabled`를 `false`로 바꾼다. 수동 전송·재시도는 다음 명령으로 확인한다.

웹훅 주소를 TOML에 직접 넣으면 `webhook_url_env`를 제거하고 다음과 같이 설정한다.

```toml
[repositories.discord]
enabled = true
webhook_url = "https://discord.com/api/webhooks/..."
timeout_seconds = 30
```

`fix-agent.toml`은 프로세스 시작 시 한 번만 읽는다. `enabled`, `webhook_url`, `webhook_url_env`, `timeout_seconds` 중 하나라도 바꾸면 직접 실행 중인 `serve`를 재시작하거나 LaunchAgent를 다시 설치해야 한다.

```bash
.venv/bin/fix-agent notify-once --config fix-agent.toml
.venv/bin/fix-agent notify-once --config fix-agent.toml --force
```

Crontrol 연동은 전역 `[crontrol]`에서 설정한다. 활성화하면 같은 ID의 `Code Fix Agent` 행에 현재 단계를 자동 반영한다. 로컬 Crontrol에 API token이 없으면 `token`과 `token_env`를 모두 생략한다.

```toml
[crontrol]
enabled = true
base_url = "http://127.0.0.1:7070"
job_id = "code-fix-agent-server"
name = "Code Fix Agent"
branch = "main"
timeout_seconds = 5
```

수동 동기화와 현재 등록값 확인은 다음 명령을 사용한다.

```bash
.venv/bin/fix-agent crontrol-once --config fix-agent.toml
curl http://127.0.0.1:7070/api/v1/jobs
```

`[crontrol]` 설정을 바꾼 뒤에는 `serve` 또는 LaunchAgent를 재시작해야 자동 단계 동기화에 반영된다.

자동 트리거는 인증된 `POST /reviews` 요청으로 연결한다. 같은 장비에서 수동으로 전달할 때는 `fix-agent submit`을 사용할 수 있다.

severity·경로·fingerprint 예외와 독립 사실 검증에서 오탐으로 판정한 항목은 수정하지 않는다. 사실 검증을 통과해 수정 대상으로 확정된 job은 `max_attempts = 0`일 때 완료될 때까지 처리한다. `retry_delay_seconds`가 지난 뒤 같은 job을 다시 처리하며, 이 job이 끝나기 전에는 뒤 job을 시작하지 않는다. 다음 시도는 기존 사실 판정과 사유를 유지하고 최신 `remote/target_branch`로 새 worktree를 만든다. 직전 `last_error`와 실패한 하네스 출력도 Codex에 전달한다. `completed`는 push와 worktree 제거가 모두 끝난 상태다.

```toml
[repositories.execution]
command_timeout_seconds = 3600
max_attempts = 0
retry_delay_seconds = 30
max_remote_merge_attempts = 3
```

## 문서

- [리뷰 에이전트 연동 가이드](docs/repo/01-리뷰-에이전트-연동-가이드.md): 설치, 로컬·HTTP 연결, 작업 상태와 운영 조건
- [리뷰 이벤트 v1 계약](docs/repo/02-리뷰-이벤트-v1-계약.md): 필드, fingerprint, 응답, 오류, 재시도와 호환성 규칙
- [수정 작업과 worktree 생명주기](docs/repo/03-수정-작업과-worktree-생명주기.md): 최신 target 동기화, merge 충돌 해결, 작업별 push, 정리와 event log
- [Crontrol 등록과 실제 수정 흐름](docs/repo/04-Crontrol-등록과-실제-수정-흐름.md): `Code Fix Agent` 표시 규칙과 finding 재검증부터 worktree 정리까지의 전체 절차

## 검증

```bash
.venv/bin/python -m unittest discover -s tests -q
git diff --check
git status --short
```
