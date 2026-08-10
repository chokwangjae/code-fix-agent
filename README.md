# 코드 수정 에이전트

검증된 코드 리뷰 이슈를 다시 확인한 뒤 대상 프로젝트 규칙에 맞춰 수정하고 설정된 remote와 target branch에 반영한다. 기본 `review_batch` 모드는 한 리뷰의 finding을 저장소별로 함께 검증·수정하고 commit과 push는 finding 변경 그룹별로 나눈다. finding 사실 여부, 원격 merge와 수정 결과의 판단 사유를 SQLite에 남기며 정책과 테스트를 통과하지 못한 작업은 push하지 않는다.

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

자동 생성 commit과 원격 merge commit의 작성자는 저장소별 `git_author_name`, `git_author_email`로 설정한다. 이 값은 GitHub 인증에 쓰지 않는다.

```toml
[server]
token = "CODE_FIX_TOKEN으로 쓸 긴 난수"
max_concurrent_jobs = 3

[[repositories]]
# 다른 저장소 설정 생략
publish_mode = "direct"
processing_mode = "review_batch"
github_token = "저장소 쓰기 권한 GitHub token"
git_author_name = "broken-agent"
git_author_email = "g_uapm@inswave.com"
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
- 정책 허용 경로 안의 신규 파일 추가와 기존 파일 삭제 허용
- 파일 수와 변경 line 수 무제한
- fingerprint 중복 방지, 독립 사실 검증과 사유 기록
- 검증된 diff와 대상 저장소 규칙에 맞는 commit type·scope·제목 생성
- 저장소별 `remote`, `target_branch`, direct push 또는 PR 방식 선택
- 리뷰 배치별 최신 target 기반 detached worktree, 원격 이동 시 배치 전체 merge·재검증
- fingerprint별 판정 근거와 결과 유지, 같은 파일 finding의 변경 그룹 병합
- finding 변경 그룹별 commit·순차 push, 반복 실패 그룹의 finding 모드 전환
- 배치별 Codex 호출 수·token·실행 시간 기록
- 관리 worktree의 소유자 쓰기 권한 자동 보정과 단계별 재확인
- npm·Gradle·Pub·Playwright·CocoaPods·임시 파일용 전용 runtime cache
- 새 worktree의 저장소별 의존성·브라우저·컨테이너 사전 준비와 실패 재시도
- 최대 3개 작업 동시 실행, 같은 작업의 실패 원인과 하네스 출력을 동일 worktree에서 반영
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

Crontrol 연동은 전역 `[crontrol]`에서 설정한다. 활성화하면 같은 ID의 `Code Fix Agent` 행에 실행 중인 작업 수와 각 작업의 현재 단계를 자동 반영한다. 로컬 Crontrol에 API token이 없으면 `token`과 `token_env`를 모두 생략한다.

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

`serve`는 `[server].max_concurrent_jobs`만큼 worker를 띄운다. 현재 운영값은 `3`이며 각 worker는 서로 다른 리뷰 배치 또는 finding 작업과 worktree를 사용한다. 이 값을 바꾸면 프로세스를 재시작해야 한다.

`processing_mode = "review_batch"`는 이벤트의 finding을 한 worktree에서 함께 사실 검증하고 수정한다. 서버가 finding 파일별 최소 변경 그룹을 먼저 정하고, 같은 파일이나 공용 지원 파일로 연결된 그룹을 합친다. Codex가 여러 finding 파일을 한 그룹에 담아도 계약 오류로 중단하지 않는다. 다른 그룹은 각각 commit한 뒤 target branch에 순서대로 push한다. 오탐만 fingerprint별 `rejected`로 끝낸다. 정책·하네스·결과 검증이 실패하면 같은 worktree에서 배치 전체를 보완한다. 반복 실패 원인이 특정 그룹으로 좁혀지면 그 그룹만 기존 finding 처리 방식으로 돌린다. fallback finding은 배치 worktree 정리가 끝날 때까지 `fallback_pending`에 두고, 정리 후 개별 처리 대기열에 공개한다. 이전 방식이 필요하면 저장소별 `processing_mode = "finding"`으로 되돌린다. 배치 모드는 finding별 순차 push가 필요한 `publish_mode = "direct"`에서만 사용할 수 있다.

프로세스를 재시작하면 `validating`, `fixing`, `testing`, `ready`, `pushed`, `fallback_pending` 상태였던 job을 자동 복구한다. 재시작은 시도 횟수를 늘리지 않는다. 기록된 worktree가 남아 있으면 미커밋 diff와 생성된 commit을 포함한 현재 상태에서 수정을 이어가고, worktree가 없으면 최신 target에서 다시 시작한다. 일반 작업 복구는 `restart_recovery_scheduled`, `worktree_resumed` event로 남고 pending fallback 복구는 `fallback_recovery_scheduled`로 남는다.

수정 결과를 검증하는 Codex는 같은 diff와 대상 저장소의 `AGENTS.md`를 근거로 commit 제목도 만든다. 변경 종류에 맞는 type과 실제 하위 시스템 scope를 고르고 변경된 동작을 제목에 적는다. fingerprint, `autofix`, `review finding`, `review issue`, `리뷰 이슈`처럼 수정 내용을 대신하는 표기는 거부한다. `commit_message_template`의 첫 줄에는 생성 제목을 넣고 나머지 본문은 설정값을 유지한다. 새 설정은 첫 줄에 `{title}`을 사용하며, `{title}`이 없는 기존 설정도 commit할 때 첫 줄을 생성 제목으로 교체한다.

`setup_commands`는 새 worktree를 만든 뒤 Codex 검증보다 먼저 실행한다. 준비 명령이 실패하면 `setup_max_attempts`만큼 같은 worktree에서 재시도한다. `setup_watch_paths`에 지정한 lockfile이나 빌드 설정이 수정 또는 원격 merge로 바뀌면 하네스 전에 준비 명령을 다시 실행한다. 명령은 shell 문자열이 아닌 argument 배열로 지정한다. 설치 도구가 lockfile이나 프로젝트 파일을 다시 쓰면 실행 전 내용을 복원해 수정 diff와 분리한다.

worktree 생성 직후 Git이 추적하는 파일과 디렉터리에 현재 실행 사용자의 읽기·쓰기 권한을 보장하고 쓰기 probe를 실행한다. 삭제된 tracked 파일의 부모 경로가 이미 없어졌다면 권한 검사에서 제외한다. Codex 수정 전, 환경 준비 명령 전후, 하네스 실행 전과 worktree 제거 전에도 같은 검사를 반복한다. 설치 cache는 `.fix-agent/runtime-cache/<repository-key>` 아래에 분리하며 `HOME`과 원본 저장소 `local_path`의 권한은 바꾸지 않는다.

`command_timeout_seconds`를 넘긴 Codex·하네스·Git 명령은 자식까지 포함한 프로세스 그룹을 종료해 worktree를 계속 바꾸는 백그라운드 프로세스를 남기지 않는다.

```toml
[[repositories]]
setup_commands = [
  ["npm", "ci", "--prefix", "web", "--prefer-offline", "--no-audit", "--no-fund"],
  ["web/node_modules/.bin/playwright", "install", "chromium"],
]
setup_watch_paths = ["web/package.json", "web/package-lock.json"]
```

```toml
[repositories.execution]
command_timeout_seconds = 7200
setup_max_attempts = 3
setup_retry_delay_seconds = 15
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
