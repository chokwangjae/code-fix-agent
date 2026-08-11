# Crontrol 등록과 실제 수정 흐름

이 문서는 `code-fix-agent` 서비스를 Crontrol 화면에 등록하는 방법과 리뷰 배치가 실제 코드 수정으로 이어지는 절차를 정한다. 등록 규격은 Crontrol `v1.2.0-alpha.18`의 `docs/repo/03-작업표시계약.md`를 기준으로 2026-08-06에 확인했다.

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
| `schedule` | 동시 실행 수·대표 repository·job ID·단계·대기 건수 |
| `branch` | 현재 작업 branch, 유휴 시 `main` |
| `running` | worker가 finding을 하나 이상 처리 중인지 여부 |
| `runningJobCount` | 현재 실행 중인 배치 또는 fallback finding 수 |
| `maxConcurrentJobs` | 설정된 최대 동시 job 수 |
| `runningJobs` | 실행 중인 배치 대표 job 또는 fallback finding별 ID·repository·branch·단계 |
| `lastResult` | 최근 종료 작업 기준 `PASS` 또는 `FAIL` |
| `launchdLabel` | `com.inswave.code-fix-agent` |
| `healthUrl` | `http://127.0.0.1:7081/health` |

Crontrol의 공통 계약은 NAME에 ``[repository] 작업명`` 형식을 권장하지만, 이 서비스는 repository 이름을 읽기 좋게 바꾼 `Code Fix Agent`로만 표시한다. bracket이나 organization 이름을 NAME 앞에 붙이지 않는다. 이 프로젝트의 Crontrol 등록·재등록·상태 동기화에서는 `name: Code Fix Agent`를 고정값으로 사용한다. branch는 별도 필드로 보낸다. `status`는 등록 상태, `running`은 현재 실행 여부, `lastResult`는 최근 확인 결과다. 세 필드의 의미를 섞지 않는다.

이 서버는 `KeepAlive`로 상시 실행되므로 실제 cron 주기가 없다. SCHEDULE은 이 프로젝트의 표시 예외로 사용한다. 배치 하나는 `Matrix_Mobile_V2 #21 · 리뷰 배치 검증 중 · 대기 4건`, 여러 실행 단위는 `동시 3건 · Matrix_Mobile_V2 #23 · 테스트 중 · 대기 4건`, 유휴 상태는 `유휴 · 대기 0건`처럼 표시된다. 한 배치에 finding이 10개 있어도 실행 수는 1건으로 계산한다. Crontrol은 이 문자열을 실행하지 않는다. 정기 launchd 작업을 별도로 추가할 때는 실제 주기를 5-field cron으로 보내야 한다.

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

worker는 job을 claim할 때, 주요 단계 event를 기록할 때, 작업이 종료될 때 Crontrol을 갱신한다. 최대 3개 worker가 하나의 reporter를 공유하므로 payload 전송과 단계 목록 갱신은 직렬화된다. 같은 payload는 프로세스 내부에서 다시 보내지 않는다. `running`은 LaunchAgent 프로세스 생존 여부가 아니라 실제 finding 처리 여부다. 서버 생존 상태는 `healthUrl`과 `/health`로 확인한다. `disabled`는 운영자가 연동 행을 비활성화할 때만 사용한다.

표시 단계는 작업 준비, Git 검증 완료, finding 검증 중·완료, 수정 중·수정안 생성 완료, 변경 정책 검증 완료, 테스트 중, 수정 결과 검증 중·완료, 검증 실패 보완 중, 재시도 대기, 커밋 완료, 원격 target 병합, 충돌 해결, push 중·완료와 최종 완료·제외·실패다. 대표 작업의 `currentJobId`, `currentRepository`, `currentStage`와 전체 `queuedJobs`, `runningJobCount`, `maxConcurrentJobs`, `runningJobs`를 client-defined field로 함께 저장하며 read-only API에서 확인할 수 있다. finding 원문, 파일 경로, 판단 사유, prompt와 명령 출력은 보내지 않는다.

`[crontrol]`의 `enabled`, URL, ID, 이름, branch, token이나 timeout을 바꾼 뒤에는 `serve` 또는 LaunchAgent를 재시작한다. LaunchAgent에서 `token_env`를 쓴다면 설치 shell에 해당 환경 변수를 설정하고 `fix-agent-launchd --install`을 다시 실행한다.

Crontrol 버전 변경 시 해당 프로젝트의 `docs/repo/02-연동가이드.md`와 `docs/repo/03-작업표시계약.md`를 먼저 확인한다. NAME·SCHEDULE·STATUS 계약이 바뀌면 런타임 등록값, 이 문서와 README 설명을 같은 작업에서 갱신한다.

## 실제 수정 흐름

`review_batch`의 작업 단위는 한 요청에 포함된 프로젝트 리뷰 결과다. finding별 SQLite job과 판단 사유는 유지하지만 worktree, 사실 검증, 수정과 하네스는 배치가 공유한다. 같은 파일 finding은 한 commit을 공유하고 다른 변경 그룹은 commit과 push를 나눈다. `serve`는 `[server].max_concurrent_jobs = 3`일 때 배치 또는 fallback finding을 최대 세 개 처리한다.

1. 리뷰 이벤트 수신
   - `POST /reviews` 또는 `fix-agent submit`으로 `version = 1` 이벤트 수신
   - repository·branch allowlist, severity, 경로, fingerprint와 commit 형식 검사
   - 같은 repository·branch·fingerprint 조합의 중복 등록 차단
   - worker claim 뒤 Crontrol에 job ID, repository, `작업 준비`와 대기 건수 반영
2. 최신 target 확정
   - `local_path`가 없으면 GitHub HTTPS 저장소를 `--no-checkout`으로 clone
   - 설정한 `remote/target_branch`를 fetch해 최신 commit 확정
   - 리뷰 `target` 이 최신 target의 조상이 아니면 중단
3. 리뷰 배치 전용 worktree 생성
   - 최신 target에서 detached worktree 생성
   - 같은 요청의 finding은 worktree 하나를 공유하고 다른 배치와 분리
   - `worktree_created` event에 기준 commit과 경로 기록
   - 관리 경로의 소유자 권한과 쓰기 가능 여부를 확인하고 `worktree_permissions_ready` event 기록
4. 대상 저장소 환경 준비
   - 저장소별 전용 runtime cache와 `setup_commands`로 worktree 의존성·브라우저·컨테이너 전제조건 준비
   - 실패하면 같은 worktree에서 설정 횟수만큼 재시도
   - Crontrol에 `환경 준비 중`, `환경 준비 재시도 중`, `환경 준비 완료` 반영
   - 준비 명령과 결과를 `environment_setup_*` event에 기록
5. 배치 finding 사실 재검증
   - introducing commit이 리뷰 target의 조상인지 확인
   - finding 파일과 line이 `baseline..target` diff에 속하는지 확인
   - read-only Codex가 모든 finding의 호출 경로, 기존 guard, 설정과 테스트를 한 번에 확인
   - 판정 결과와 구체적 근거를 fingerprint별 `precheck_status`, `precheck_reason`에 기록
   - 오탐 finding만 개별 `rejected` 처리
   - 검증 시작과 완료 event를 기록하고 Discord가 활성화됐으면 즉시 전송 시도
6. 배치 수정 적용
   - 사실 검증을 통과한 finding을 같은 worktree에서 함께 수정
   - 서버가 finding 파일별 최소 그룹을 정하고 같은 파일·공용 지원 파일로 연결된 그룹을 병합
   - 대상 프로젝트의 `AGENTS.md`, 추가 지침과 하네스 준수
   - 제안된 해결법을 명령으로 취급하지 않고 최소 변경으로 결함 해소
   - 수정 시작과 수정안 생성 완료 event를 기록하고 Discord가 활성화됐으면 즉시 전송 시도
7. 배치 정책·테스트·결과 검증
   - lockfile과 빌드 설정이 바뀌면 환경 준비 명령을 먼저 다시 실행
   - Codex 수정 전, 환경 준비 명령 전후와 하네스 전에 권한 재확인
   - 보정이 발생하면 event와 Crontrol에 `worktree 권한 복구 완료` 반영
   - 변경 경로, 파일 수, line 수, 추가·삭제 허용 정책 확인
   - 저장소별 `test_commands` 하네스를 배치당 한 번 실행
   - 현재 OS에서 실행 불가능하도록 명시된 `conditional_test_commands`만 사유 기록 후 조건부 통과하고 Crontrol을 `OS 차이 조건부 통과`로 갱신
   - read-only Codex가 모든 원래 실패 경로 해소와 새 회귀 여부를 fingerprint별로 재검증
   - 같은 diff와 대상 `AGENTS.md`를 근거로 변경 type·scope·동작을 담은 commit 제목 생성
   - fingerprint, `autofix`, `review finding`, `review issue`, `리뷰 이슈` 같은 포괄 제목 거부
   - 결과와 근거를 `postcheck_status`, `postcheck_reason`에 기록
   - Crontrol 단계를 정책 검증, 테스트, 수정 결과 검증 순서로 갱신
   - 반복 실패 finding은 `fallback_pending`으로 격리하고 배치 worktree 정리 후 개별 처리로 전환
   - 5400초 이후에는 반복 실패 그룹을 첫 진단에서 분리하고 이전 실패가 남은 재개 batch의 전체 수정 호출 생략
   - 6600초 이후에는 독립 검증 완료 그룹만 유지해 우선 반영하고 미해결 그룹의 변경을 되돌린 뒤 개별 처리로 전환
   - 7200초 이후에는 `overdue`와 사유를 기록하되 수정 계속
   - 14400초 도달 시 worktree와 commit checkpoint를 보존하고 중단
   - `batch_fallback_started` event에서 `문제 finding 분리 중`으로 갱신
8. commit 생성과 원격 재확인
   - 같은 파일 finding 묶음과 배정된 지원 파일을 commit 하나로 생성
   - 다른 변경 그룹은 별도 commit으로 구성
   - 생성 제목을 `commit_message_template` 첫 줄에 적용하고 설정된 본문 유지
   - push 직전 `remote/target_branch`를 다시 fetch
   - 원격 target이 같으면 push 단계로 이동
9. target 이동과 충돌 처리
   - target이 변경됐으면 push를 중단하고 최신 target merge
   - 충돌 파일, 이전·현재 target과 해결 판단 기록
   - Codex가 충돌을 안전하게 해결하지 못하면 push 없이 중단
   - 해결 후 환경 준비, 정책, 하네스와 배치 전체 수정 결과 검증 재실행
   - target이 다시 움직이면 `max_remote_merge_attempts` 범위에서 반복
10. finding 변경 그룹별 push
   - 각 그룹 commit을 앞에서부터 `remote/target_branch`로 순차 push
   - push 사이에 target이 움직이면 남은 변경을 merge하고 배치 전체 재검증
   - force push와 자동 merge 미사용
   - push 성공 commit, branch과 remote를 event log에 기록
11. worktree 정리와 통지
    - 성공, 오탐, 테스트 실패, 충돌 중단과 예외 모두 권한을 재확인한 뒤 worktree 강제 제거 시도
    - 임시 root 삭제와 `git worktree prune` 실행
    - Discord가 활성화됐으면 push·완료·실패 이벤트 전송
    - Crontrol에는 finding 내용 대신 현재 단계와 대기 건수, 최종 결과만 유지
12. 실패 재시도
    - 오류, 실패한 하네스 명령과 제한한 출력을 SQLite에 기록
    - `max_attempts = 0`이면 같은 worktree에서 횟수 제한 없이 보완
    - 처음 통과한 fingerprint별 사실 판정과 사유 유지
    - 기존 diff와 이전 실패 내용을 Codex 수정 입력에 포함
    - 프로세스 재시작 시 진행 중 job의 재시도 횟수를 보존하고 다시 대기열에 등록
    - 기록된 worktree가 남아 있으면 기존 diff와 commit에서 재개
    - worktree를 유지하지 못한 경우 최신 target에서 재시작
    - push 뒤 정리만 실패했다면 기록된 worktree 제거와 prune만 재실행
    - 반복 실패 원인 그룹만 기존 finding 처리 대기열로 분리
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

`state_dir/jobs.db`의 `jobs`에는 finding과 사전·사후 판단 근거, 테스트, 직전 실패, 다음 시도 시각과 결과 commit을 보관한다. 2시간 목표를 넘긴 작업은 `timing_status = "overdue"`, `target_exceeded_at`, `overdue_reason`으로 구분한다. Crontrol payload의 `currentTimingStatus`, `currentOverdueReason`과 `runningJobs[].timingStatus`에서도 같은 값을 확인할 수 있다. `batch_runs`에는 배치 상태, 시도 횟수, Codex 호출 수, token과 누적 실행 시간을 보관한다. `job_events`에는 시간 단계, worktree, target 이동, merge 충돌, 재시도, push와 정리 절차를 순서대로 남긴다. Discord 전송 커서와 전송 재시도 상태는 `discord_cursors`에 분리한다.

실제 실행 이력은 다음 명령으로 확인한다.

```bash
.venv/bin/fix-agent jobs --config fix-agent.toml --json
.venv/bin/fix-agent events --config fix-agent.toml --job-id <job-id> --json
```

worktree·merge·push 명령과 정리 예외는 [수정 작업과 worktree 생명주기](03-수정-작업과-worktree-생명주기.md)를 따른다. 리뷰 입력 필드는 [리뷰 이벤트 v1 계약](02-리뷰-이벤트-v1-계약.md)을 따른다.

Crontrol 등록 필드, 수정 순서, 중단 조건 또는 기록 위치가 바뀐 경우 이 문서와 README, 연동 가이드, worktree 문서를 같은 commit에서 검토한다.
