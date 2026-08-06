# 수정 작업과 worktree 생명주기

이 문서는 리뷰 finding 한 건이 최신 원격 target에서 수정되고 push된 뒤 worktree가 제거될 때까지의 실행 계약을 설명한다. 외부 송신 형식은 [리뷰 이벤트 v1 계약](02-리뷰-이벤트-v1-계약.md)을 따른다.

리뷰 수신부터 Crontrol 표시와 최종 통지까지의 전체 순서는 [Crontrol 등록과 실제 수정 흐름](04-Crontrol-등록과-실제-수정-흐름.md)에 정리했다.

## 저장소 설정

```toml
[[repositories]]
id = "matrix-mobile-v2"
github = "inswave/Matrix_Mobile_V2"
target_branch = "dev"
local_path = "../Matrix_Mobile_V2"
remote = "origin"
publish_mode = "direct"
github_token_env = "MATRIX_MOBILE_FIX_GITHUB_TOKEN"

[repositories.execution]
command_timeout_seconds = 3600
max_attempts = 1
max_remote_merge_attempts = 3
```

`github_token_env` 대신 `github_token = "..."`을 쓰면 GitHub token을 TOML에 직접 설정할 수 있다. 두 키를 모두 생략하면 `gh auth token --hostname github.com`으로 PC 로그인 token을 읽는다. 인증 값은 Codex와 테스트 명령에 전달하지 않고 Git network·PR 명령에만 사용한다.

`remote`와 `target_branch`가 최신 코드 조회와 결과 push 위치를 정한다. 위 설정의 작업 경로는 다음과 같다.

```text
origin/dev 최신 commit
  → finding 전용 detached worktree
  → 수정·검증·commit
  → 필요하면 최신 origin/dev merge
  → origin/dev push
  → worktree 제거
```

`publish_mode`는 두 값을 지원한다.

| 값 | push 위치 | 완료 조건 |
|---|---|---|
| `direct` | `remote/target_branch` | target branch push 완료 |
| `pull_request` | `remote/autofix/<repository-id>/<fingerprint-short>` | PR 생성 완료 |

현재 저장소의 대상 설정은 `direct`다. branch protection이 직접 push를 금지하면 작업은 `failed`가 된다. 설정을 우회하거나 force push하지 않는다.

## 작업 분리 단위

리뷰 이벤트에 finding이 여러 개 있어도 SQLite 작업은 finding마다 하나씩 만든다. worker는 작업 하나마다 다음 자원을 새로 만든다.

- SQLite job ID
- `fix-<random>` 임시 디렉터리
- detached worktree
- fix commit
- push 시도와 event log

한 finding의 파일 변경을 다른 finding worktree와 합치지 않는다. 상시 worker는 작업을 순차 처리한다. 여러 `run-once` 프로세스를 동시에 실행해 원격 target이 바뀌면 뒤 작업이 merge 절차를 수행한다.

## 1. 로컬 저장소 준비

설정한 `local_path`가 없으면 다음과 같은 `--no-checkout` clone을 만든다.

```bash
git clone --no-checkout \
  --origin origin \
  -- \
  https://github.com/inswave/Matrix_Mobile_V2.git \
  /configured/local_path
```

경로가 있으면 Git 저장소인지 `git rev-parse --git-dir`로 확인한다. 원래 checkout에서 `git pull`, branch switch나 reset은 실행하지 않는다. 이 저장소는 object와 remote ref를 갱신하고 worktree를 연결하는 기준 저장소로만 쓴다.

## 2. 최신 target fetch

기준 저장소에서 target branch 하나를 fetch한다.

```bash
git check-ref-format --branch dev
git fetch --prune --no-tags -- \
  origin \
  +refs/heads/dev:refs/remotes/origin/dev
git rev-parse --verify refs/remotes/origin/dev^{commit}
```

결과 commit을 `workspace base`로 기록한다. 리뷰 이벤트의 `target`은 workspace base와 같거나 그 조상이어야 한다.

```bash
git merge-base --is-ancestor <review target> <workspace base>
```

조상 관계가 아니면 다른 이력이나 잘못된 리뷰 결과로 보고 중단한다. 리뷰 뒤 정상 commit이 추가된 경우에는 최신 target을 기준으로 계속한다.

## 3. detached worktree 생성

worktree root는 `state_dir/worktrees` 아래에 만든다.

```text
.fix-agent/
└── worktrees/
    └── fix-<random>/
        └── checkout/
```

checkout은 최신 workspace base에서 detached HEAD로 생성한다.

```bash
git worktree add --detach \
  .fix-agent/worktrees/fix-<random>/checkout \
  <workspace base>
```

이 시점에는 수정 branch를 만들지 않는다. 원래 checkout의 현재 branch와 작업 파일도 바꾸지 않는다. 생성 경로, remote, target branch와 base commit은 `worktree_created` event에 남긴다.

## 4. finding 검증과 수정

worktree 안에서 다음 단계를 실행한다.

1. finding 도입 commit이 리뷰 target의 조상인지 확인
2. 도입 commit이 finding 파일을 변경했는지 확인
3. finding line이 `baseline..target` diff에 속하는지 확인
4. read-only Codex의 독립 사실 검증과 사유 기록
5. workspace-write Codex의 최소 수정
6. 경로·파일 수·line 수·symlink·binary 정책 검사
7. 대상 저장소 하네스 실행
8. read-only Codex의 수정 결과 검증과 사유 기록

Codex와 테스트 환경에서는 GitHub token, 일반적인 token·secret·password와 webhook 환경 변수를 제거한다. Git network 명령에만 별도 인증 환경을 사용한다.

## 5. fix commit 생성

초기 정책·테스트·수정 결과 검증을 통과하면 수정분을 commit한다.

`direct`에서는 detached HEAD 상태로 commit하므로 별도 local branch를 남기지 않는다.

```bash
git add --all
git -c user.name="Code Fix Agent" \
    -c user.email="code-fix-agent@users.noreply.github.com" \
    -c commit.gpgsign=false \
    commit -m "<commit_message_template>"
```

`pull_request`에서는 commit 전에 다음 branch를 만든다.

```text
autofix/<repository-id>/<fingerprint 앞 12자리>
```

생성 branch와 commit은 `fix_committed` event에 남긴다.

## 6. 원격 이동 감지와 merge

commit 뒤 `origin/dev`를 다시 fetch한다. 현재 원격 commit이 workspace base와 같으면 push 단계로 간다. 다르면 `target_moved` event를 남기고 worktree에서 최신 target을 merge한다.

```bash
git -c user.name="Code Fix Agent" \
    -c user.email="code-fix-agent@users.noreply.github.com" \
    -c commit.gpgsign=false \
    merge --no-edit <latest origin/dev commit>
```

clean merge가 끝나면 `target_merged` event에 이전 base, 최신 target과 merge commit을 기록한다.

### merge 충돌

merge 종료 코드가 0이 아니면 다음 명령으로 unmerged 파일을 구한다.

```bash
git diff --name-only --diff-filter=U -z
```

unmerged 파일이 있으면 `merge_conflict_detected` event에 파일 목록과 양쪽 commit을 기록한다. 별도 workspace-write Codex는 다음 자료를 확인한다.

- 적용 범위의 `AGENTS.md`
- index의 base·ours·theirs stage
- 기존에 검증된 fix 의도
- 최신 target 변경 의도
- 원래 finding의 실패 조건

Codex는 conflict marker를 제거하고 파일만 수정한다. `git add`, commit과 push는 실행하지 않는다. 결과는 다음 형태로 받아 `merge_conflict_decided` event에 그대로 기록한다.

```json
{
  "resolved": true,
  "reason": "양쪽 변경 의도를 보존한 파일별 해결 근거"
}
```

`resolved`가 `false`면 push하지 않는다. `true`면 worker가 다음을 실행한다.

```bash
git add --all
git diff --cached --check
git -c user.name="Code Fix Agent" \
    -c user.email="code-fix-agent@users.noreply.github.com" \
    -c commit.gpgsign=false \
    commit --no-edit
```

남은 unmerged 파일이나 conflict marker가 있으면 실패한다. 성공하면 해결 사유와 merge commit을 `merge_conflict_resolved` event에 기록한다.

## 7. merge 후 전체 재검증

원격 target을 merge하면 최신 target commit을 새 workspace base로 바꾼다. 새 base 대비 agent 변경분만 다시 검사한다.

1. diff 경로와 변경량 정책 재검사
2. 저장소 하네스 전체 재실행
3. 하네스가 tracked 파일을 바꾸지 않았는지 확인
4. read-only Codex의 원인 해소·회귀 여부 재검증

모두 통과하면 `merged_fix_revalidated` event를 남긴다. 검증 중 원격 target이 또 이동하면 merge와 재검증을 반복한다. 반복 횟수는 `max_remote_merge_attempts`로 제한하며 기본값은 3이다.

## 8. 작업별 push

`direct` 설정은 다음 형태로 push한다.

```bash
git push origin HEAD:refs/heads/dev
```

force push와 `--set-upstream`은 사용하지 않는다. push 직전과 직후에는 각각 `push_started`, `push_completed` event를 기록한다.

fetch와 push 사이에 원격이 이동해 non-fast-forward가 발생하면 최신 target을 다시 fetch한다. 변경된 commit을 merge하고 7단계 검증을 다시 통과한 뒤 push를 재시도한다. 원격 HEAD가 이미 worktree 결과 commit이면 앞선 push가 성공하고 응답만 유실된 것으로 간주한다.

`pull_request` 설정은 `autofix/...` branch를 push한 뒤 `gh pr create`를 실행한다. 어느 방식도 force push나 자동 merge를 사용하지 않는다.

## 9. worktree 제거

정상 완료, 오탐 거부, 정책 실패, 테스트 실패, merge 실패와 Python 예외 모두 context 종료 경로에서 worktree 정리를 시도한다.

```bash
git worktree remove --force \
  .fix-agent/worktrees/fix-<random>/checkout
git worktree prune
```

두 명령 사이에 `fix-<random>` 디렉터리도 재귀 삭제한다. 결과는 다음 event 중 하나로 남긴다.

- `worktree_removed`: worktree 제거 명령 성공, 임시 root 삭제 확인
- `worktree_cleanup_incomplete`: 제거 명령 실패 또는 임시 root 잔존

PR 방식의 PR 생성은 worktree 제거 뒤에 실행한다. direct 방식은 worktree 제거 뒤 작업을 `completed`로 바꾼다. push된 원격 branch, SQLite 기록과 PR은 worktree 정리 대상이 아니다.

`SIGKILL`이나 장비 전원 종료는 Python 정리 경로를 실행하지 못한다. 현재 시작 시 잔존 worktree를 자동 회수하는 기능은 없다. 운영자는 event가 없는 오래된 경로를 바로 삭제하지 말고 다음 명령으로 Git 등록 상태와 실행 중 작업을 먼저 확인한다.

```bash
git -C /configured/local_path worktree list --porcelain
.venv/bin/fix-agent jobs --config fix-agent.toml --json
```

## event log와 Discord 알림

`job_events`는 삭제·수정하지 않는 append-only 로그다. 각 row는 다음 필드를 가진다.

| 필드 | 의미 |
|---|---|
| `id` | 전체 작업에서 증가하는 event cursor |
| `job_id` | finding 작업 ID |
| `event_type` | 상태, worktree, merge, 검증, push 단계 |
| `status` | event 기록 시점의 작업 상태 |
| `message` | 짧은 설명 |
| `details_json` | commit, 경로, 충돌 파일과 판단 사유 |
| `created_at` | UTC ISO 8601 시각 |

작업 하나의 이력을 조회한다.

```bash
.venv/bin/fix-agent events \
  --config fix-agent.toml \
  --job-id 41 \
  --json
```

외부 소비자는 마지막 처리 ID 다음부터 조회할 수 있다.

```bash
.venv/bin/fix-agent events \
  --config fix-agent.toml \
  --after-id 120 \
  --limit 100 \
  --json
```

내장 notifier는 저장소별 `discord_cursors`에서 마지막 처리 ID를 관리한다. Discord가 HTTP 2xx를 반환한 뒤에만 커서를 전진한다. 실패하면 같은 event를 재시도하며 Discord 메시지에는 `event.id`를 넣어 중복을 판별한다.

`src/fix_agent/discord.py`는 `code-review-agent`의 Discord 형식에 맞춰 payload를 만들고 `src/fix_agent/notify.py`가 저장소별 웹훅으로 전송한다.

- embed payload와 `Content-Type: application/json` 전제
- `username = "Code Fix Agent"`
- `allowed_mentions = {"parse": []}`
- 성공 초록색, 실패 빨간색, 정보 파란색, 충돌 경고 주황색
- `@everyone`, `@here` 문자열 무력화
- event ID, job ID, repository, branch, finding과 구조화 세부 정보 포함
- embed 전체 약 5,500자 이내 제한

기본 알림 후보는 정책 제외, target 이동, merge 충돌 감지·해결, push 완료, worktree 정리 실패와 `completed`·`rejected`·`failed` 상태다. 내부 진행 event는 formatter가 빈 payload를 반환하고 커서만 전진한다.

sender는 `code-review-agent`와 같은 운영 규칙을 따른다.

- 저장소별 webhook 분리
- 설정 파일의 URL 또는 URL 환경 변수 중 하나만 허용
- 일반 webhook에서만 선택적인 Bearer token 환경 변수 사용
- 외부 전송 직전 설정과 secret 재조회
- UTF-8 JSON POST, 30초 timeout과 HTTP 2xx 확인
- 전송 성공 뒤에만 event ID checkpoint 전진
- 여러 payload 중 하나라도 실패하면 checkpoint 유지 후 같은 event부터 재시도

재시도 간격은 5초, 30초, 2분, 10분이며 이후에는 10분으로 유지한다. 전송 실패와 누락된 웹훅 환경 변수는 `discord_cursors`에 기록하고 수정 작업의 최종 상태는 바꾸지 않는다. worker가 없을 때는 `fix-agent notify-once`로 처리하고, `--force`를 붙이면 저장된 다음 재시도 시각을 무시한다. 처음 활성화한 저장소는 기존 마지막 event에서 시작해 과거 메시지를 일괄 발송하지 않는다.

설정과 환경 변수 예시는 [리뷰 에이전트 연동 가이드](01-리뷰-에이전트-연동-가이드.md)의 Discord 작업 알림 절을 따른다.

## 주요 event 순서

정상 direct 작업은 대체로 다음 순서로 기록된다.

```text
job_created
job_claimed
processing_started
worktree_created
finding_git_validated
status_changed: fixing
fix_applied
diff_validated
status_changed: testing
status_changed: ready
fix_committed
[target_moved → target_merged 또는 merge_conflict_* → 재검증]
push_started
push_completed
status_changed: pushed
worktree_removed
status_changed: completed
```

모든 예외는 `last_error`와 `status_changed: failed`로 끝난다. 오탐과 수정 결과 검증 실패는 `rejected`로 남긴다.
