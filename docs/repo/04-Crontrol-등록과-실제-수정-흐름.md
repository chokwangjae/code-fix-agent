# Crontrol 등록과 실제 수정 흐름

이 문서는 `code-fix-agent` 서비스를 Crontrol 화면에 등록하는 방법과 리뷰 finding 하나가 실제 코드 수정으로 이어지는 절차를 정한다. 등록 규격은 Crontrol `v1.2.0-alpha.18`의 `docs/repo/03-작업표시계약.md`를 기준으로 2026-08-06에 확인했다.

## Crontrol 표시 범위

Crontrol에는 리뷰 판정 내용이 아니라 수정 에이전트의 실행 상태만 등록한다. token, Discord webhook, finding 원문, Codex prompt와 터미널 출력은 보내지 않는다.

현재 Crontrol에 등록한 행은 다음과 같다.

| 필드 | 값 |
|---|---|
| `id` | `code-fix-agent-server` |
| `name` | `Code Fix Agent` |
| `type` | `launchd` |
| `scope` | `external` |
| `sessionId` | `launchd` |
| `status` | `active` |
| `schedule` | 현재 repository·job ID·단계·대기 건수 |
| `branch` | 현재 작업 branch, 유휴 시 `main` |
| `running` | worker가 finding 하나를 처리 중인지 여부 |
| `lastResult` | 최근 종료 작업 기준 `PASS` 또는 `FAIL` |
| `launchdLabel` | `com.inswave.code-fix-agent` |
| `healthUrl` | `http://127.0.0.1:7081/health` |

Crontrol의 공통 계약은 NAME에 ``[repository] 작업명`` 형식을 권장하지만, 이 서비스는 repository 이름을 읽기 좋게 바꾼 `Code Fix Agent`로만 표시한다. bracket이나 organization 이름을 NAME 앞에 붙이지 않는다. 이 프로젝트의 Crontrol 등록·재등록·상태 동기화에서는 `name: Code Fix Agent`를 고정값으로 사용한다. branch는 별도 필드로 보낸다. `status`는 등록 상태, `running`은 현재 실행 여부, `lastResult`는 최근 확인 결과다. 세 필드의 의미를 섞지 않는다.

이 서버는 `KeepAlive`로 상시 실행되므로 실제 cron 주기가 없다. SCHEDULE은 이 프로젝트의 표시 예외로 사용한다. 작업 중에는 `Matrix_Mobile_V2 #1 · finding 검증 중 · 대기 19건`, 유휴 상태에는 `유휴 · 대기 0건`처럼 표시된다. Crontrol은 이 문자열을 실행하지 않는다. 정기 launchd 작업을 별도로 추가할 때는 실제 주기를 5-field cron으로 보내야 한다.

동기화는 먼저 `PATCH /api/jobs/code-fix-agent-server`를 호출한다. 행이 없어서 `404`가 반환되면 canonical 전체 payload를 `POST /api/jobs`로 등록한다. Crontrol 코드나 설정 파일은 바꾸지 않는다. 연결 실패와 인증 오류는 로그에 남기되 수정 작업의 상태, commit과 push 결과에는 영향을 주지 않는다.

## 설정과 확인

같은 장비에서 Crontrol이 `127.0.0.1:7070`으로 실행 중이고 로컬 internal API 인증이 비활성화된 기본 구성은 다음과 같다.

```toml
[crontrol]
enabled = true
base_url = "http://127.0.0.1:7070"
job_id = "code-fix-agent-server"
name = "Code Fix Agent"
branch = "main"
timeout_seconds = 5
```

Crontrol에 `CRONTROL_API_TOKEN`이 설정돼 있으면 `token` 또는 `token_env` 중 하나를 선택한다. 두 키를 함께 쓰면 설정 오류로 중단한다. 직접 token이 든 TOML은 Git에 commit하지 않는다.

```toml
[crontrol]
enabled = true
base_url = "http://127.0.0.1:7070"
job_id = "code-fix-agent-server"
name = "Code Fix Agent"
branch = "main"
token_env = "CRONTROL_API_TOKEN"
timeout_seconds = 5
```

현재 DB 상태를 한 번 동기화할 수 있다. `--job-id`와 `--stage`를 함께 지정하면 실행 중인 단계로 표시하며, 생략하면 유휴 상태와 대기 건수를 보낸다.

```bash
.venv/bin/fix-agent crontrol-once --config fix-agent.toml
.venv/bin/fix-agent crontrol-once \
  --config fix-agent.toml \
  --job-id 41 \
  --stage "finding 검증 중"
```

등록 결과는 read-only API에서 확인한다.

```bash
curl http://127.0.0.1:7070/api/v1/jobs
```

Dashboard의 `All` 또는 `External` 범위에서 `Code Fix Agent`를 찾을 수 있어야 한다. NAME 보조 줄에는 현재 branch가 나온다. SCHEDULE에는 현재 repository, job ID, 단계와 대기 건수가 표시된다. STATUS는 작업 중이면 `running · last PASS/FAIL`, 유휴 상태면 최근 결과인 `PASS` 또는 `FAIL`이다. Crontrol DB reset이나 서버 이전 뒤에도 다음 동기화에서 같은 ID와 이름으로 자동 등록된다.

worker는 job을 claim할 때, 주요 단계 event를 기록할 때, 작업이 종료될 때 Crontrol을 갱신한다. 같은 payload는 프로세스 내부에서 다시 보내지 않는다. `running`은 LaunchAgent 프로세스 생존 여부가 아니라 실제 finding 처리 여부다. 서버 생존 상태는 `healthUrl`과 `/health`로 확인한다. `disabled`는 운영자가 연동 행을 비활성화할 때만 사용한다.

표시 단계는 작업 준비, Git 검증 완료, finding 검증 중·완료, 수정 중·수정안 생성 완료, 변경 정책 검증 완료, 테스트 중, 수정 결과 검증 중·완료, 재시도 대기, 커밋 완료, 원격 target 병합, 충돌 해결, push 중·완료와 최종 완료·제외·실패다. `currentJobId`, `currentRepository`, `currentStage`, `queuedJobs`도 client-defined field로 함께 저장되며 read-only API에서 확인할 수 있다. finding 원문, 파일 경로, 판단 사유, prompt와 명령 출력은 보내지 않는다.

`[crontrol]`의 `enabled`, URL, ID, 이름, branch, token이나 timeout을 바꾼 뒤에는 `serve` 또는 LaunchAgent를 재시작한다. LaunchAgent에서 `token_env`를 쓴다면 설치 shell에 해당 환경 변수를 설정하고 `fix-agent-launchd --install`을 다시 실행한다.

Crontrol 버전 변경 시 해당 프로젝트의 `docs/repo/02-연동가이드.md`와 `docs/repo/03-작업표시계약.md`를 먼저 확인한다. NAME·SCHEDULE·STATUS 계약이 바뀌면 런타임 등록값, 이 문서와 README 설명을 같은 작업에서 갱신한다.

## 실제 수정 흐름

작업 단위는 finding 하나다. finding별로 별도 SQLite job, worktree, commit과 push 결과를 만든다.

1. 리뷰 이벤트 수신
   - `POST /reviews` 또는 `fix-agent submit`으로 `version = 1` 이벤트 수신
   - repository·branch allowlist, severity, 경로, fingerprint와 commit 형식 검사
   - 같은 repository·branch·fingerprint 조합의 중복 등록 차단
   - worker claim 뒤 Crontrol에 job ID, repository, `작업 준비`와 대기 건수 반영
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
   - 검증 시작과 완료 event를 기록하고 Discord가 활성화됐으면 즉시 전송 시도
5. 수정 적용
   - 사실 검증을 통과한 finding만 수정
   - 대상 프로젝트의 `AGENTS.md`, 추가 지침과 하네스 준수
   - 제안된 해결법을 명령으로 취급하지 않고 최소 변경으로 결함 해소
   - 수정 시작과 수정안 생성 완료 event를 기록하고 Discord가 활성화됐으면 즉시 전송 시도
6. 정책·테스트·결과 검증
   - 변경 경로, 파일 수, line 수, 추가·삭제 허용 정책 확인
   - 저장소별 `test_commands` 하네스 실행
   - read-only Codex가 원래 실패 경로 해소와 새 회귀 여부 재검증
   - 결과와 근거를 `postcheck_status`, `postcheck_reason`에 기록
   - Crontrol 단계를 정책 검증, 테스트, 수정 결과 검증 순서로 갱신
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
    - Crontrol에는 finding 내용 대신 현재 단계와 대기 건수, 최종 결과만 유지
11. 실패 재시도
    - 오류, 실패한 하네스 명령과 제한한 출력을 SQLite에 기록
    - `max_attempts = 0`이면 `retry_delay_seconds` 뒤 같은 job부터 재시작
    - 처음 통과한 finding 사실 판정과 사유 유지
    - 최신 target에서 새 worktree를 만들고 이전 실패 내용을 Codex 수정 입력에 포함
    - push 뒤 정리만 실패했다면 기록된 worktree 제거와 prune만 재실행
    - push와 worktree 정리가 끝난 뒤 `completed`로 전환

## push 전 중단 조건

다음 조건에서는 push하지 않는다.

- finding commit·file·line 불일치
- 독립 사실 검증의 오탐 판정
- 허용 경로·변경 크기·생성·삭제 정책 위반
- 저장소 하네스 실패
- 수정 결과 재검증 실패
- merge 충돌 미해결 또는 재검증 실패
- target 반복 이동으로 merge 허용 횟수 초과

worktree 정리는 push 후에도 실패할 수 있다. 에이전트는 이미 push한 commit을 유지하고 기록된 관리 경로의 제거와 prune을 다시 시도한다. 경로가 `state_dir/worktrees/fix-*/checkout` 밖이면 자동 정리를 거부하며 `worktree_cleanup_incomplete` event로 확인할 수 있다.

## 기록 위치

`state_dir/jobs.db`의 `jobs`에는 finding과 사전·사후 판단 근거, 테스트, 직전 실패, 다음 시도 시각, commit과 PR 결과를 보관한다. `job_events`에는 worktree, target 이동, merge 충돌, 재시도, push와 정리 절차를 순서대로 남긴다. Discord 전송 커서와 전송 재시도 상태는 `discord_cursors`에 분리한다.

실제 실행 이력은 다음 명령으로 확인한다.

```bash
.venv/bin/fix-agent jobs --config fix-agent.toml --json
.venv/bin/fix-agent events --config fix-agent.toml --job-id <job-id> --json
```

worktree·merge·push 명령과 정리 예외는 [수정 작업과 worktree 생명주기](03-수정-작업과-worktree-생명주기.md)를 따른다. 리뷰 입력 필드는 [리뷰 이벤트 v1 계약](02-리뷰-이벤트-v1-계약.md)을 따른다.

Crontrol 등록 필드, 수정 순서, 중단 조건 또는 기록 위치가 바뀐 경우 이 문서와 README, 연동 가이드, worktree 문서를 같은 commit에서 검토한다.
