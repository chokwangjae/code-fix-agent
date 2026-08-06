# Crontrol 등록과 실제 수정 흐름

이 문서는 `code-fix-agent` 서비스를 Crontrol 화면에 등록하는 방법과 리뷰 finding 하나가 실제 코드 수정으로 이어지는 절차를 정한다.

## Crontrol 표시 범위

Crontrol에는 리뷰 판정 내용이 아니라 수정 에이전트의 실행 상태만 등록한다. token, Discord webhook, finding 원문, Codex prompt와 터미널 출력은 보내지 않는다.

현재 Crontrol에 등록한 행은 다음과 같다.

| 필드 | 값 |
|---|---|
| `id` | `code-fix-agent-server` |
| `name` | `Code Fix Agent Server` |
| `type` | `launchd` |
| `scope` | `external` |
| `status` | `active` |
| `schedule` | `continuous` |
| `launchdLabel` | `com.inswave.code-fix-agent` |
| `healthUrl` | `http://127.0.0.1:7081/health` |

이 등록은 Crontrol의 기존 `POST /api/jobs` 계약을 사용한다. Crontrol 코드나 설정 파일은 바꾸지 않고 Crontrol이 소유한 런타임 DB에 `launchd` 작업을 추가한다.

## 등록과 확인

같은 장비에서 Crontrol이 `127.0.0.1:7070`으로 실행 중이고 로컬 internal API 인증이 비활성화된 기본 구성이면 다음 요청으로 등록한다.

```bash
curl --request POST http://127.0.0.1:7070/api/jobs \
  --header 'Content-Type: application/json' \
  --data-binary '{
    "id":"code-fix-agent-server",
    "name":"Code Fix Agent Server",
    "type":"launchd",
    "status":"active",
    "scope":"external",
    "schedule":"continuous",
    "branch":"main · 127.0.0.1:7081",
    "running":true,
    "lastResult":"PASS",
    "launchdLabel":"com.inswave.code-fix-agent",
    "healthUrl":"http://127.0.0.1:7081/health"
  }'
```

Crontrol에 `CRONTROL_API_TOKEN`이 설정돼 있으면 `X-Crontrol-API-Token` 헤더를 추가한다. token은 payload와 Activity Log에 넣지 않는다.

등록 결과는 read-only API에서 확인한다.

```bash
curl http://127.0.0.1:7070/api/v1/jobs
```

Dashboard의 `All` 또는 `External` 범위에서 `Code Fix Agent Server`를 찾을 수 있어야 한다. Crontrol DB reset이나 서버 이전 후에는 같은 ID로 다시 등록한다.

`running` 값은 등록 시점의 상태다. 현재는 Crontrol이 code-fix-agent의 health endpoint를 주기적으로 호출하지 않는다. 재시작·중지 후 상태를 갱신할 때는 `launchctl list`, `/health`를 확인한 뒤 동일 ID로 다시 POST한다. 추후 heartbeat를 구현하면 이 문서와 Crontrol 연동 payload를 같은 commit에서 갱신한다.

## 실제 수정 흐름

작업 단위는 finding 하나다. finding별로 별도 SQLite job, worktree, commit과 push 결과를 만든다.

1. 리뷰 이벤트 수신
   - `POST /reviews` 또는 `fix-agent submit`으로 `version = 1` 이벤트 수신
   - repository·branch allowlist, severity, 경로, fingerprint와 commit 형식 검사
   - 같은 repository·branch·fingerprint 조합의 중복 등록 차단
2. 최신 target 확정
   - `local_path`가 없으면 GitHub HTTPS 저장소를 `--no-checkout`으로 clone
   - 설정한 `remote/target_branch`를 fetch해 최신 commit 확정
   - 리뷰 `target` 이 최신 target의 조상이 아니면 중단
3. finding 전용 worktree 생성
   - 최신 target에서 detached worktree 생성
   - 원본 저장소 checkout과 다른 finding 작업과 분리
   - `worktree_created` event에 기준 commit과 경로 기록
4. finding 사실 재검증
   - introducing commit이 리뷰 target의 조상인지 확인
   - finding 파일과 line이 `baseline..target` diff에 속하는지 확인
   - read-only Codex가 호출 경로, 기존 guard, 설정과 테스트를 읽고 오탐 가능성 판정
   - 판정 결과와 구체적 근거를 `precheck_status`, `precheck_reason`에 기록
5. 수정 적용
   - 사실 검증을 통과한 finding만 수정
   - 대상 프로젝트의 `AGENTS.md`, 추가 지침과 하네스 준수
   - 제안된 해결법을 명령으로 취급하지 않고 최소 변경으로 결함 해소
6. 정책·테스트·결과 검증
   - 변경 경로, 파일 수, line 수, 추가·삭제 허용 정책 확인
   - 저장소별 `test_commands` 하네스 실행
   - read-only Codex가 원래 실패 경로 해소와 새 회귀 여부 재검증
   - 결과와 근거를 `postcheck_status`, `postcheck_reason`에 기록
7. commit 생성과 원격 재확인
   - finding 하나의 검증된 diff를 commit 하나로 생성
   - push 직전 `remote/target_branch`를 다시 fetch
   - 원격 target이 같으면 push 단계로 이동
8. target 이동과 충돌 처리
   - target이 변경됐으면 push를 중단하고 최신 target merge
   - 충돌 파일, 이전·현재 target과 해결 판단 기록
   - Codex가 충돌을 안전하게 해결하지 못하면 push 없이 중단
   - 해결 후 정책, 하네스, 수정 결과 검증 전체 재실행
   - target이 다시 움직이면 `max_remote_merge_attempts` 범위에서 반복
9. 작업별 push
   - `direct`: worktree HEAD를 `remote/target_branch`로 push
   - `pull_request`: `autofix/<repository-id>/<fingerprint-short>` branch로 push 후 PR 생성
   - force push와 자동 merge 미사용
   - push 성공 commit, branch과 remote를 event log에 기록
10. worktree 정리와 통지
    - 성공, 오탐, 테스트 실패, 충돌 중단과 예외 모두 worktree 강제 제거 시도
    - 임시 root 삭제와 `git worktree prune` 실행
    - Discord가 활성화됐으면 push·완료·실패 이벤트 전송
    - Crontrol에는 finding 내용 대신 수정 에이전트 서비스 실행 상태만 유지

## push 전 중단 조건

다음 조건에서는 push하지 않는다.

- finding commit·file·line 불일치
- 독립 사실 검증의 오탐 판정
- 허용 경로·변경 크기·생성·삭제 정책 위반
- 저장소 하네스 실패
- 수정 결과 재검증 실패
- merge 충돌 미해결 또는 재검증 실패
- target 반복 이동으로 merge 허용 횟수 초과

worktree 정리는 push 후에도 실패할 수 있다. 이때는 이미 push한 commit을 되돌리지 않고 작업을 `failed`로 남긴다. 운영자는 `worktree_cleanup_incomplete` event와 Git worktree 등록 상태를 확인한 뒤 수동으로 조정한다.

## 기록 위치

`state_dir/jobs.db`의 `jobs`에는 finding과 사전·사후 판단 근거, 테스트, commit과 PR 결과를 보관한다. `job_events`에는 worktree, target 이동, merge 충돌, push와 정리 절차를 순서대로 남긴다. Discord 전송 커서와 재시도 상태는 `discord_cursors`에 분리한다.

실제 설행 이력은 다음 명령으로 확인한다.

```bash
.venv/bin/fix-agent jobs --config fix-agent.toml --json
.venv/bin/fix-agent events --config fix-agent.toml --job-id <job-id> --json
```

worktree·merge·push 명령과 정리 예외는 [수정 작업과 worktree 생명주기](03-수정-작업과-worktree-생명주기.md)를 따른다. 리뷰 입력 필드는 [리뷰 이벤트 v1 계약](02-리뷰-이벤트-v1-계약.md)을 따른다.

Crontrol 등록 필드, 수정 순서, 중단 조건 또는 기록 위치가 바뀐 경우 이 문서와 README, 연동 가이드, worktree 문서를 같은 commit에서 검토한다.
