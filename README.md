# 코드 수정 에이전트

검증을 마친 코드 리뷰 이슈를 받아 사실 여부를 다시 확인하고 수정한다. 테스트와 수정 결과 검증을 통과하면 전용 branch를 push하고 PR을 만든다. 저장소별 정책으로 심각도와 경로를 제한하며 같은 fingerprint는 한 번만 등록한다.

## 설치

```bash
cd /Users/brokenclaw/hermes-workspace/code-fix-agent
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp fix-agent.example.toml fix-agent.local.toml
chmod 600 fix-agent.local.toml
```

저장소에 포함된 `fix-agent.toml`에는 현재 `Matrix_Mobile_V2`와 `TestSquare-Fable` 설정이 들어 있다. 다른 저장소를 연결할 때는 `fix-agent.example.toml`을 복사해 로컬 설정을 만든다. 설정 파일에 저장소와 환경 변수 이름을 적은 뒤 수신 token을 환경 변수에 넣고 서버를 실행한다.

```bash
export CODE_FIX_TOKEN='replace-with-a-secret'
export MATRIX_MOBILE_FIX_GITHUB_TOKEN='repository-scoped-token'
export TESTSQUARE_FABLE_FIX_GITHUB_TOKEN='repository-scoped-token'
.venv/bin/fix-agent serve --config fix-agent.toml
```

서버는 기본값으로 `127.0.0.1:7081`의 `/reviews`에서 `version = 1` 리뷰 이벤트를 받는다. `Authorization: Bearer <token>` 헤더가 필요하다. `/health`는 인증 없이 상태만 반환한다. 서버 내부 작업 루프가 SQLite의 `queued` 작업을 한 건씩 처리한다.

macOS에서는 LaunchAgent를 설치할 수 있다. 설치 명령을 실행하는 shell에 수신 token과 저장소별 GitHub token을 먼저 설정한다.

```bash
.venv/bin/fix-agent-launchd \
  --config /Users/brokenclaw/hermes-workspace/code-fix-agent/fix-agent.toml \
  --install
```

## 단독 등록

리뷰 이벤트 JSON 파일을 CLI에서 등록할 수 있다.

```bash
.venv/bin/fix-agent submit \
  --config fix-agent.local.toml \
  --file review-event.json \
  --run-now
```

최근 작업은 다음 명령으로 확인한다.

```bash
.venv/bin/fix-agent jobs --config fix-agent.local.toml --json
```

`--json` 결과에는 수정 전 사실 검증의 `precheck_status`·`precheck_reason`, 수정 후 검증의 `postcheck_status`·`postcheck_reason`, 테스트 결과와 실패 사유가 포함된다.

## 작업 순서

1. 원격 branch HEAD와 리뷰의 `target`이 같은지 확인한다.
2. 도입 commit, 변경 파일과 line이 리뷰 범위에 속하는지 Git으로 검사한다.
3. read-only Codex가 실제 실패 경로를 다시 확인하고 판단 사유를 기록한다.
4. 유효한 finding만 별도 worktree에서 수정한다.
5. 변경 경로와 변경량을 정책에 맞춰 검사하고 설정된 테스트를 실행한다.
6. read-only Codex가 원인 해소와 회귀 여부를 검증한다.
7. 원격 HEAD를 다시 확인한 뒤 `autofix/<repository-id>/<fingerprint>` branch와 PR을 만든다.

사실 검증이나 수정 결과 검증에서 `invalid` 판정이 나오면 파일을 push하지 않는다. 테스트 실패와 실행 예외는 `last_error`에 기록한다. `max_attempts` 범위 안에서 서버 작업 루프가 실패 작업을 다시 시도한다.

## 저장소 규칙

수정 단계에서는 대상 파일에 적용되는 `AGENTS.md`를 기본 지침으로 사용한다. `additional_instructions`에는 해당 저장소에서 덧붙일 내용을 적고 `commit_message_template`에는 대상 저장소의 commit 규칙에 맞는 형식을 지정한다. 사용할 수 있는 placeholder는 `{fingerprint}`, `{fingerprint_short}`, `{file}`이다. `test_commands`에는 대상 프로젝트가 요구하는 검증 하네스를 argument 배열로 등록한다.

`[repositories.policy]`에서 다음 항목을 설정한다.

- `allowed_severities`: 자동 수정 대상으로 받을 심각도
- `allowed_paths`, `denied_paths`: 수정 허용·금지 경로
- `skipped_paths`, `skipped_fingerprints`: 저장소별 예외
- `max_changed_files`, `max_changed_lines`: 변경량 상한
- `allow_new_files`, `allow_deletions`: 파일 추가·삭제 허용 여부
- `require_finding_file_changed`: finding 대상 파일의 직접 변경 요구 여부

Critical은 기본 자동 수정 대상에서 제외한다. `command_timeout_seconds`와 `max_attempts`로 명령 제한 시간과 실패 재시도 횟수를 조정할 수 있다. GitHub token 격리, 대상 branch HEAD 확인과 직접 merge 금지는 저장소 설정으로 해제할 수 없다.

Codex와 테스트 명령에서는 `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, webhook 환경 변수를 제거한다. GitHub token은 push와 PR 명령에만 전달한다. 설정값이나 리뷰 결과에 들어 있는 명령은 shell에서 실행하지 않는다.

## 테스트

```bash
.venv/bin/python -m unittest discover -s tests -q
git diff --check
```
