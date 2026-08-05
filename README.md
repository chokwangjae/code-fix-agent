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

서버는 기본값으로 `127.0.0.1:7081`에서 실행된다. `/reviews`는 Bearer token이 있는 `version = 1` 리뷰 이벤트를 받고 `/health`는 서버 상태를 반환한다. 같은 장비에서 연동한다면 서버와 수신 token 없이 `fix-agent submit`으로 이벤트를 전달할 수도 있다. 다만 push와 PR 생성에는 저장소별 GitHub token이 필요하다.

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
- append-only 작업 event 기록과 Discord notifier용 순차 event ID
- `code-review-agent` 규격 호환 저장소별 Discord 알림과 실패 재시도

Discord 알림은 `fix-agent.toml`의 저장소별 `[repositories.discord]`에서 켠다. 현재 기본 설정은 실제 발송을 막기 위해 `enabled = false`다. `true`로 바꾼 뒤 `webhook_url_env`에 적힌 환경 변수에 웹훅 URL을 넣으면 `serve`와 `run-once`가 작업 이벤트를 전송한다. 수동 전송·재시도는 다음 명령으로 확인한다.

```bash
.venv/bin/fix-agent notify-once --config fix-agent.toml
.venv/bin/fix-agent notify-once --config fix-agent.toml --force
```

현재 `code-review-agent`와 자동 트리거는 연결하지 않았다. 승인 없이 사용할 수 있는 입력 방식은 HTTP API와 `fix-agent submit`이다.

## 문서

- [리뷰 에이전트 연동 가이드](docs/repo/01-리뷰-에이전트-연동-가이드.md): 설치, 로컬·HTTP 연결, 작업 상태와 운영 조건
- [리뷰 이벤트 v1 계약](docs/repo/02-리뷰-이벤트-v1-계약.md): 필드, fingerprint, 응답, 오류, 재시도와 호환성 규칙
- [수정 작업과 worktree 생명주기](docs/repo/03-수정-작업과-worktree-생명주기.md): 최신 target 동기화, merge 충돌 해결, 작업별 push, 정리와 event log

## 검증

```bash
.venv/bin/python -m unittest discover -s tests -q
git diff --check
git status --short
```
