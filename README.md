# 코드 수정 에이전트

검증을 마친 코드 리뷰 이슈를 받아 작업 큐에 저장한다. 저장소별 정책으로 심각도와 경로를 제한하며 같은 fingerprint는 한 번만 등록한다.

## 설치

```bash
cd /Users/brokenclaw/hermes-workspace/code-fix-agent
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp fix-agent.example.toml fix-agent.local.toml
chmod 600 fix-agent.local.toml
```

`fix-agent.local.toml`에 저장소와 환경 변수 이름을 설정한다. 수신 token을 환경 변수에 넣고 서버를 실행한다.

```bash
export CODE_FIX_TOKEN='replace-with-a-secret'
.venv/bin/fix-agent serve --config fix-agent.local.toml
```

서버는 기본값으로 `127.0.0.1:7081`의 `/reviews`에서 `version = 1` 리뷰 이벤트를 받는다. `Authorization: Bearer <token>` 헤더가 필요하다. `/health`는 인증 없이 상태만 반환한다.

## 단독 등록

리뷰 이벤트 JSON 파일을 CLI에서 등록할 수 있다.

```bash
.venv/bin/fix-agent submit \
  --config fix-agent.local.toml \
  --file review-event.json
```

최근 작업은 다음 명령으로 확인한다.

```bash
.venv/bin/fix-agent jobs --config fix-agent.local.toml
```

## 저장소 규칙

수정 단계에서는 대상 파일에 적용되는 `AGENTS.md`를 기본 지침으로 사용한다. `additional_instructions`에는 해당 저장소에서 덧붙일 내용을 적는다. `test_commands`에는 대상 프로젝트가 요구하는 검증 하네스를 argument 배열로 등록한다.

`[repositories.policy]`에서 다음 항목을 설정한다.

- `allowed_severities`: 자동 수정 대상으로 받을 심각도
- `allowed_paths`, `denied_paths`: 수정 허용·금지 경로
- `skipped_paths`, `skipped_fingerprints`: 저장소별 예외
- `max_changed_files`, `max_changed_lines`: 변경량 상한
- `allow_new_files`, `allow_deletions`: 파일 추가·삭제 허용 여부

Critical은 기본 자동 수정 대상에서 제외한다. GitHub token 격리, 대상 branch HEAD 확인과 직접 merge 금지는 저장소 설정으로 해제할 수 없다.

## 테스트

```bash
.venv/bin/python -m unittest discover -s tests -q
git diff --check
```
