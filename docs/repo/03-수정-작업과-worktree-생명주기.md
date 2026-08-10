# 수정 작업과 worktree 생명주기

이 문서는 리뷰 배치가 최신 원격 target에서 수정되고 finding 변경 그룹별로 push된 뒤 worktree가 제거될 때까지의 실행 계약을 설명한다. 외부 송신 형식은 [리뷰 이벤트 v1 계약](02-리뷰-이벤트-v1-계약.md)을 따른다.

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
processing_mode = "review_batch"
github_token_env = "MATRIX_MOBILE_FIX_GITHUB_TOKEN"
git_author_name = "broken-agent"
git_author_email = "g_uapm@inswave.com"
setup_commands = [
  ["flutter", "pub", "get", "-C", "client/matrix_flutter_template"],
  ["npm", "install", "--prefix", "client/matrix_rn_template", "--no-package-lock", "--prefer-offline", "--no-audit", "--no-fund"],
]
setup_watch_paths = [
  "client/matrix_flutter_template/pubspec.yaml",
  "client/matrix_flutter_template/pubspec.lock",
  "client/matrix_rn_template/package.json",
]

[repositories.execution]
command_timeout_seconds = 7200
codex_timeout_seconds = 3600
harness_timeout_seconds = 1800
job_timeout_seconds = 7200
setup_max_attempts = 3
setup_retry_delay_seconds = 15
max_attempts = 0
retry_delay_seconds = 30
max_remote_merge_attempts = 3
```

`max_attempts = 0`은 시도 횟수만 제한하지 않는다. `job_timeout_seconds`는 최초 claim 시각부터 재시작과 재시도를 합산하며 기본값은 7200초다. 시간 예산을 모두 쓰면 재시도를 예약하지 않고 `failed`로 끝낸다. 양수 `max_attempts`는 최초 시도를 포함한 최대 횟수다. 정책·하네스·결과 검증 실패는 같은 worktree에서 바로 보완한다. 프로세스를 재시작하면 진행 중 job을 재시도 횟수 차감 없이 다시 대기열에 넣되 누적 시간은 초기화하지 않는다. 기록된 worktree가 남아 있으면 기존 diff와 commit을 보존한 채 같은 경로에서 이어가고, 경로가 없으면 최신 target에서 새 worktree를 만든다. severity·경로·fingerprint 예외는 `skipped`, 독립 사실 검증의 오탐은 `rejected`로 끝낸다.

Codex 호출별 제한은 `codex_timeout_seconds`, 하네스 명령별 제한은 `harness_timeout_seconds`, 준비·Git 명령 제한은 `command_timeout_seconds`다. finding 보완에서 같은 오류가 두 번 이어지거나 Codex 호출 전후 worktree fingerprint가 같으면 진행 불가로 판정해 재시도를 멈춘다. 배치 수정이 diff를 바꾸지 못하면 batch 전체를 다시 호출하지 않고 관련 finding을 `fallback_pending`으로 옮긴다.

OS 전용 하네스는 `conditional_test_commands`의 `host_os`로 실행 가능 여부를 판정한다. 현재 OS가 목록 밖인 명령만 실행하지 않고 조건부 통과하며 사유를 기록한다. 현재 OS에서 실행된 명령이 실패하거나 timeout된 경우에는 같은 worktree에서 보완을 계속한다.

`github_token_env` 대신 `github_token = "..."`을 쓰면 GitHub token을 TOML에 직접 설정할 수 있다. 두 키를 모두 생략하면 `gh auth token --hostname github.com`으로 PC 로그인 token을 읽는다. 인증 값은 Codex와 테스트 명령에 전달하지 않고 Git network·PR 명령에만 사용한다.

운영 정책은 `allow_new_files`와 `allow_deletions`를 모두 `true`로 두고 `max_changed_files`, `max_changed_lines`를 `0`으로 둔다. 허용 경로 안에서는 파일 추가·수정·삭제와 변경량을 제한하지 않는다. `allowed_paths`, `denied_paths`와 finding 파일 변경 조건은 계속 적용한다.

```toml
[repositories.policy]
max_changed_files = 0
max_changed_lines = 0
allow_new_files = true
allow_deletions = true
```

`0`은 변경량 무제한을 뜻한다. 양수를 넣으면 파일 수와 추가·삭제 line 합계에 상한을 적용한다. 기본 `denied_paths`는 `.github/workflows/**`, `.env*`, `**/.env*`, `**/*.p12`, `**/*.mobileprovision`이다.

`remote`와 `target_branch`가 최신 코드 조회와 결과 push 위치를 정한다. `git_author_name`과 `git_author_email`은 자동 생성 commit과 원격 merge commit에만 사용하며 GitHub 인증에는 관여하지 않는다. 위 설정의 작업 경로는 다음과 같다.

```text
origin/dev 최신 commit
  → 리뷰 배치 전용 detached worktree
  → 배치 수정·하네스·결과 검증
  → finding 변경 그룹별 commit
  → 필요하면 최신 origin/dev merge와 배치 전체 재검증
  → commit별 origin/dev 순차 push
  → worktree 제거
```

`publish_mode`는 두 값을 지원한다.

| 값 | push 위치 | 완료 조건 |
|---|---|---|
| `direct` | `remote/target_branch` | target branch push 완료 |
| `pull_request` | `remote/autofix/<repository-id>/<fingerprint-short>` | PR 생성 완료 |

현재 저장소의 대상 설정은 `direct`다. branch protection이 직접 push를 금지하면 작업은 `failed`가 된다. 설정을 우회하거나 force push하지 않는다.

`processing_mode = "review_batch"`는 `publish_mode = "direct"`에서만 쓸 수 있다. 기존 finding별 worktree가 필요한 저장소는 `processing_mode = "finding"`으로 설정한다.

## 작업 분리 단위

finding별 SQLite job과 fingerprint 판단 기록은 유지한다. `review_batch` worker는 한 리뷰 요청에서 새로 만든 job을 `batch_id`로 묶고 worktree, 환경 준비, Codex 배치 호출, 저장소 하네스와 target 이동 재검증을 공유한다. 이슈 10개를 한 이벤트의 `findings[]`로 보내면 worktree는 하나다. 같은 이슈를 요청 10건으로 나눠 보내면 배치도 10개가 되므로 송신기는 같은 repository·branch·review target의 finding을 한 요청에 모아야 한다.

commit과 push는 변경 그룹별로 나눈다. 서버는 Codex 호출 전에 finding 파일별 최소 그룹 ID를 만든다. 같은 파일 finding은 반드시 한 그룹이며 fingerprint별 판정 사유는 각각 남긴다. Codex가 최소 그룹을 합치거나 같은 지원 파일을 여러 그룹에 배정하면 해당 그룹을 연결 요소 하나로 합친다. 이 방식은 여러 finding 파일을 한 그룹에 담은 응답도 허용하면서 같은 파일이 서로 다른 commit에 들어가는 충돌을 막는다. 연결되지 않은 그룹은 각각 commit하고 앞 commit의 push가 끝난 뒤 다음 commit을 같은 target branch로 push한다.

반복 실패 원인이 특정 그룹으로 좁혀지면 해당 그룹의 변경을 worktree에서 되돌리고 그 fingerprint만 `fallback_pending`으로 바꾼다. 이 상태는 일반 worker가 claim하지 않는다. 나머지 배치는 같은 worktree에서 계속 보완한다. 배치 worktree 제거와 `git worktree prune`이 끝나면 `queued`로 바꿔 기존 finding 처리 대기열에 공개한다. 전환 과정은 `batch_finding_fallback_pending`, `batch_fallback_started`, `worktree_removed`, `batch_finding_fallback` 순서로 기록한다. 프로세스가 pending 상태에서 중단되면 재기동 복구가 개별 finding worktree 생성을 예약한다.

`worktree_created`와 `worktree_resumed`에는 `scope = batch|finding`을 기록한다. fallback finding은 `batch` 범위 worktree를 재사용하지 않는다. 범위 정보가 없는 이전 event는 fallback event보다 앞선 경로를 배치 worktree로 보고 제외한다. `processing_mode = "finding"`은 finding마다 `fix-<random>` 디렉터리, detached worktree, fix commit과 push 이력을 만든다.

`serve`는 `[server].max_concurrent_jobs`에 지정한 수만큼 worker를 실행하며 운영값은 `3`이다. 각 worker는 배치나 finding마다 worktree를 나눈다. 같은 저장소의 다른 worker가 원격 target을 먼저 갱신하면 뒤 작업은 자기 worktree에서 merge와 전체 재검증을 수행한다.

```toml
[server]
max_concurrent_jobs = 3
```

허용 범위는 `1..32`다. `run-once`와 `submit --run-now`는 이 값과 관계없이 호출당 한 건만 처리한다.

같은 `local_path`를 쓰는 worker의 clone, fetch, worktree 등록·제거와 prune은 Git 공용 메타데이터 충돌을 막기 위해 짧게 직렬화한다. worktree 생성 뒤 Codex 수정과 하네스 실행은 서로 겹쳐서 진행한다. push는 작업별로 시도하며 원격 선행 변경은 target 이동 절차에서 처리한다.

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

생성 직후에는 Git이 추적하는 파일과 Git이 무시하지 않는 새 파일만 확인한다. 일반 파일에는 소유자 읽기·쓰기 권한을, 상위 디렉터리에는 소유자 읽기·쓰기·실행 권한을 보장한다. 삭제된 tracked 파일의 부모 디렉터리까지 이미 사라졌다면 검사 대상에서 제외하며, 검사 도중 경로가 사라지는 경우도 삭제 작업으로 처리한다. 기존 실행 bit는 유지하고 symlink 대상의 mode는 바꾸지 않는다. checkout root에 임시 파일을 만들었다가 지우는 쓰기 probe까지 통과해야 다음 단계로 간다. 검사 수, 보정 수와 cache 경로는 `worktree_permissions_ready` event에 남긴다.

권한 변경 범위는 에이전트가 만든 `state_dir/worktrees/fix-*/checkout`과 `state_dir/runtime-cache`뿐이다. 원본 저장소 `local_path`, 다른 checkout, Git 공용 metadata와 사용자 홈의 권한은 바꾸지 않는다.

## 4. 환경 준비

Git 검증을 통과한 worktree에서 저장소별 `setup_commands`를 순서대로 실행한다. 각 명령은 shell을 통하지 않는 argument 배열이며 Codex·하네스와 같은 비밀값 제거 환경을 사용한다. 한 명령이 실패하면 처음부터 다시 실행하며 `setup_max_attempts`와 `setup_retry_delay_seconds`로 횟수와 간격을 정한다. 준비·Git 명령은 `command_timeout_seconds`, Codex는 `codex_timeout_seconds`, 하네스는 `harness_timeout_seconds`를 적용한다. 명령이 제한 시간을 넘으면 해당 프로세스 그룹에 `TERM`을 보내고 2초 뒤에도 남은 프로세스는 `KILL`로 끝낸다. 자식 빌드가 worktree를 계속 수정하는 상태로 남지 않아야 다음 보완을 시작한다.

도구 cache는 저장소별 `state_dir/runtime-cache/<repository-key>` 아래에 둔다. 사용자 `HOME`은 그대로 유지한다.

| 환경 변수 | 전용 경로 용도 |
|---|---|
| `NPM_CONFIG_CACHE` | npm package cache |
| `GRADLE_USER_HOME` | Gradle cache와 wrapper |
| `PUB_CACHE` | Dart·Flutter package cache |
| `PLAYWRIGHT_BROWSERS_PATH` | Playwright browser binary |
| `CP_HOME_DIR` | CocoaPods cache |
| `TMPDIR` | finding worktree별 임시 파일 |

각 경로에는 현재 실행 사용자만 접근할 수 있는 mode를 기본 적용한다. 기존 `~/.gradle/init.d`의 일반 `.gradle`·`.kts` script는 전용 Gradle home으로 복사해 로컬 container 같은 필수 초기화를 유지한다. symlink와 그 밖의 사용자 Gradle 설정은 복사하지 않는다.

준비 전후의 tracked diff와 Git이 추적할 새 파일 내용을 비교한다. 설치 명령이 lockfile이나 프로젝트 파일을 바꾸면 실행 전 내용을 복원한다. Codex가 작업 중 바꾼 파일은 그 시점의 내용을 보관했다가 되돌리므로 의도한 수정은 유지한다. 복원 뒤 diff가 준비 전과 다르면 환경 준비 실패로 처리하며 push하지 않는다. `node_modules`, Gradle cache와 Playwright 사용자 cache처럼 `.gitignore` 또는 전역 cache에 있는 파일은 비교 대상에서 빠진다.

`setup_watch_paths`는 준비 결과가 의존하는 파일을 지정한다. 최초 준비 뒤 Codex 수정이나 원격 merge로 이 파일들의 내용이 바뀌면 하네스 전에 준비 명령을 다시 실행한다. 내용이 같으면 같은 worktree에서 설치를 반복하지 않는다.

다음 event가 중간 경과와 실패 출력을 남긴다.

- `environment_setup_started`: 실행할 명령 수와 감시 경로
- `environment_setup_failed`: 실패 명령, 종료 코드, 제한한 stdout·stderr와 다음 재시도 여부
- `environment_setup_completed`: 성공한 시도, 명령 수, 복원한 Git 경로와 권한 보정 수

## 5. 배치 finding 검증과 수정

`review_batch` worktree 안에서 다음 단계를 실행한다.

1. finding 도입 commit이 리뷰 target의 조상인지 확인
2. 도입 commit이 finding 파일을 변경했는지 확인
3. finding line이 `baseline..target` diff에 속하는지 확인
4. 저장소 환경 준비 완료
5. read-only Codex 한 번으로 모든 finding의 독립 사실 검증과 fingerprint별 사유 기록
6. 오탐 finding만 개별 `rejected` 처리
7. 같은 파일 finding을 한 변경 그룹으로 합친 workspace-write Codex 수정
8. 의존성 선언 변경 시 환경 준비 갱신
9. 경로·파일 수·line 수·symlink·binary 정책 검사
10. 대상 저장소 하네스 한 번 실행
11. read-only Codex의 배치 결과 검증과 fingerprint별 사유 기록

정책·하네스·결과 검증에 실패하면 기존 diff와 실패 출력을 같은 worktree의 다음 배치 수정에 전달한다. 두 번째 실패부터 원인 그룹을 진단하며, 특정 그룹을 찾으면 해당 그룹만 finding 모드로 분리한다.

Codex 수정 직전, 환경 준비 명령 전후와 하네스 실행 직전에 worktree 권한을 다시 확인한다. 도구가 파일을 read-only로 바꿨으면 같은 worktree에서 소유자 권한을 복구하고 `worktree_permissions_repaired` event에 단계와 파일·디렉터리 수를 남긴다. 권한 확인이나 쓰기 probe가 실패하면 수정·commit·push를 진행하지 않는다.

Codex와 테스트 환경에서는 GitHub token, 일반적인 token·secret·password와 webhook 환경 변수를 제거한다. Git network 명령에만 별도 인증 환경을 사용한다.

## 6. fix commit 생성

초기 정책·테스트·수정 결과 검증을 통과하면 변경 그룹별로 수정분을 나눠 commit한다. 같은 파일 finding은 commit 하나를 공유하고 모든 fingerprint가 같은 결과 commit을 가리킨다. read-only Codex는 그룹별 실제 diff와 대상 저장소의 `AGENTS.md`에 맞는 commit 제목을 만든다. 제목의 type은 변경 종류, scope는 실제 하위 시스템, 설명은 달라진 동작을 나타내야 한다. fingerprint나 `autofix`, `review finding`, `review issue`, `리뷰 이슈` 같은 포괄 표기는 거부한다.

`commit_message_template`의 첫 줄은 Codex가 만든 제목으로 교체하고 설정된 본문은 유지한다. 새 template은 첫 줄에 `{title}`을 사용한다. `{title}`이 없는 기존 template도 같은 방식으로 첫 줄을 교체하므로 설정을 바로 마이그레이션하지 않아도 된다.

`direct`에서는 detached HEAD에 그룹별 commit chain을 만들므로 별도 local branch를 남기지 않는다. 각 commit을 만들기 전에 해당 그룹 파일만 stage됐는지 확인하고, 마지막 commit의 tree가 배치 검증을 통과한 tree와 같은지 검사한다.

```bash
git add --all
git -c user.name="broken-agent" \
    -c user.email="g_uapm@inswave.com" \
    -c commit.gpgsign=false \
    commit -m "<생성된 제목과 commit_message_template 본문>"
```

`pull_request`에서는 commit 전에 다음 branch를 만든다.

```text
autofix/<repository-id>/<fingerprint 앞 12자리>
```

생성 branch와 commit은 `fix_committed` event에 남긴다.

## 7. 원격 이동 감지와 merge

각 commit을 push하기 전에 `origin/dev`를 다시 fetch한다. 현재 원격 commit이 해당 commit의 parent와 같으면 push 단계로 간다. 다르면 `target_moved` event를 남기고 worktree의 남은 배치와 최신 target을 merge한다.

```bash
git -c user.name="broken-agent" \
    -c user.email="g_uapm@inswave.com" \
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
git -c user.name="broken-agent" \
    -c user.email="g_uapm@inswave.com" \
    -c commit.gpgsign=false \
    commit --no-edit
```

남은 unmerged 파일이나 conflict marker가 있으면 실패한다. 성공하면 해결 사유와 merge commit을 `merge_conflict_resolved` event에 기록한다.

## 8. merge 후 전체 재검증

원격 target을 merge하면 최신 target commit을 새 workspace base로 바꾼다. 이미 push한 finding을 포함한 배치 전체 결과를 다시 검사한다.

1. diff 경로와 변경량 정책 재검사
2. 저장소 하네스 전체 재실행
3. 하네스가 tracked 파일을 바꾸지 않았는지 확인
4. read-only Codex의 fingerprint별 원인 해소·회귀 여부 재검증

모두 통과하면 최신 target을 parent로 삼도록 남은 변경을 다시 그룹별 commit하고 `merged_fix_revalidated` event를 남긴다. 검증 중 원격 target이 또 이동하면 merge와 재검증을 반복한다. 반복 횟수는 `max_remote_merge_attempts`로 제한하며 기본값은 3이다.

## 9. 작업별 push

`direct` 설정은 다음 형태로 push한다.

```bash
git push origin <finding-group-commit>:refs/heads/dev
```

force push와 `--set-upstream`은 사용하지 않는다. 그룹 commit을 앞에서부터 하나씩 push하며 push 직전과 직후에는 각각 `push_started`, `push_completed` event를 그룹의 모든 finding에 기록한다.

fetch와 push 사이에 원격이 이동해 non-fast-forward가 발생하면 최신 target을 다시 fetch한다. 변경된 commit을 merge하고 8단계 검증을 다시 통과한 뒤 push를 재시도한다. 원격 HEAD가 이미 worktree 결과 commit이면 앞선 push가 성공하고 응답만 유실된 것으로 간주한다.

`pull_request` 설정은 `autofix/...` branch를 push한 뒤 `gh pr create`를 실행한다. 어느 방식도 force push나 자동 merge를 사용하지 않는다.

## 10. worktree 제거

정상 완료, 오탐 거부, 정책 실패, 테스트 실패, merge 실패와 Python 예외 모두 context 종료 경로에서 worktree 정리를 시도한다.

제거 직전에도 관리 worktree의 소유자 권한을 복구한다. 복구가 실패해도 `git worktree remove --force`와 prune은 시도하며, 오류는 정리 event의 `permission_error`에 남긴다.

```bash
git worktree remove --force \
  .fix-agent/worktrees/fix-<random>/checkout
git worktree prune
```

두 명령 사이에 `fix-<random>` 디렉터리도 재귀 삭제한다. 결과는 다음 event 중 하나로 남긴다.

- `worktree_removed`: worktree 제거 명령 성공, 임시 root 삭제 확인
- `worktree_cleanup_incomplete`: 제거 명령 실패 또는 임시 root 잔존, 발생한 권한 오류 포함

PR 방식의 PR 생성은 worktree 제거 뒤에 실행한다. direct 방식은 worktree 제거 뒤 작업을 `completed`로 바꾼다. push 뒤 정리에 실패하면 기록된 관리 경로를 확인해 제거와 prune을 다음 시도에서 다시 실행한다. 이미 push한 commit은 다시 만들지 않는다. push된 원격 branch, SQLite 기록과 PR은 worktree 정리 대상이 아니다.

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

`batch_runs`는 `batch_id`, 상태, 시도 횟수, Codex 호출 수, 입력·cache·출력·reasoning·전체 token, 누적 실행 시간과 마지막 오류를 보관한다. token 값은 Codex JSONL의 `turn.completed.usage`를 합산하며 지원되지 않는 항목은 `0`으로 남긴다.

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

같은 event 흐름은 `src/fix_agent/crontrol.py`가 Crontrol의 `Code Fix Agent` 행에 동시 실행 수, 작업별 repository·job ID·단계와 대기 건수로 요약한다. Crontrol에는 finding 원문, 파일 경로, 판단 사유나 명령 출력을 보내지 않는다. 연결 실패는 로컬 로그에 남기고 작업 상태를 바꾸지 않는다.

- embed payload와 `Content-Type: application/json` 전제
- `username = "Code Fix Agent"`
- `allowed_mentions = {"parse": []}`
- 성공 초록색, 실패 빨간색, 정보 파란색, 충돌 경고 주황색
- `@everyone`, `@here` 문자열 무력화
- event ID, job ID, repository, branch, finding과 구조화 세부 정보 포함
- embed 전체 약 5,500자 이내 제한

기본 알림 후보는 finding·배치 검증 시작과 완료, 수정 시작, 결과 검증 시작과 완료, 같은 worktree의 보완, 문제 finding 분리, 프로세스 재시도 예정, 정책 제외, target 이동, merge 충돌 감지·해결, push 완료, worktree 정리 실패와 `completed`·`rejected`·최종 `failed` 상태다. 재시도 시각이 있는 실패는 최종 실패 알림을 보내지 않는다. 검증과 수정 단계 알림은 해당 event 기록 직후 전송을 시도한다. 나머지 내부 진행 event는 formatter가 빈 payload를 반환하고 커서만 전진한다.

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
batch_claimed
batch_processing_started
worktree_created
finding_git_validated
batch_validation_started
batch_validation_completed
batch_fix_started
status_changed: testing
result_validation_started
result_validation_completed
status_changed: ready
[target_moved → target_merged 또는 merge_conflict_* → 재검증]
finding 그룹별 push_started
finding 그룹별 push_completed
status_changed: pushed
worktree_removed
status_changed: completed
batch_metrics_recorded
```

정책·하네스·결과 검증 오류는 `fix_iteration_failed`에 기록한다. 다음 Codex 수정은 같은 worktree의 기존 diff와 직전 오류, 실패한 하네스 명령·출력을 받아 보완을 이어간다. 반복 실패 finding은 배치 정리 중 `fallback_pending`, 정리 후 `queued`로 전환한다. 프로세스 재시작으로 중단된 job은 `restart_recovery_scheduled` 뒤 다시 claim하며, pending finding은 `fallback_recovery_scheduled` 뒤 개별 worktree에서 시작한다. 기존 worktree를 찾으면 `worktree_resumed`를 기록하고 중단 직전 diff에서 보완을 계속한다. 프로세스 밖으로 빠져나온 실행 오류는 `last_error`, `status_changed: failed`, `retry_scheduled`로 기록한다. 독립 사실 검증에서 오탐으로 판정한 job만 `rejected`로 남긴다.

## Worker pause와 오류 복구

`fix-agent pause --config fix-agent.toml --reason "사유"`는 SQLite의 `worker_control`에 pause 상태를 저장한다. worker는 실행 중인 작업을 강제로 끊지 않고 다음 job 또는 batch claim부터 멈춘다. `worker-status`로 상태를 확인하고 `resume`으로 claim을 재개한다. 세 명의 worker가 같은 상태를 읽으므로 프로세스 재시작은 필요 없다.

이전 운영 방식에서 사용한 `manual_pause_claims_20260810` SQLite trigger는 DB schema 초기화 시 pause 상태로 이전한 뒤 제거한다. trigger가 claim의 `UPDATE`를 예외로 끊어 worker thread까지 종료하던 경로를 없애기 위한 이전 절차다. worker loop는 `run_once()` 밖의 예외를 기록하고 poll을 계속하므로 일시적인 SQLite 오류 하나가 해당 worker를 영구 중단시키지 않는다.
