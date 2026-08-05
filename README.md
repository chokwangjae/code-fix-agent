# 코드 수정 에이전트

검증된 코드 리뷰 이슈를 다시 확인한 뒤 대상 프로젝트 규칙에 맞춰 수정하고 PR을 만든다. finding 사실 여부와 수정 결과의 판단 사유를 SQLite에 남기며, 정책·테스트·HEAD 검사를 통과하지 못한 작업은 push하지 않는다.

## 빠른 시작

```bash
cd /Users/brokenclaw/hermes-workspace/code-fix-agent
python3 -m venv .venv
.venv/bin/python -m pip install -e .

export CODE_FIX_TOKEN='replace-with-a-secret'
export MATRIX_MOBILE_FIX_GITHUB_TOKEN='repository-scoped-token'
export TESTSQUARE_FABLE_FIX_GITHUB_TOKEN='repository-scoped-token'

.venv/bin/fix-agent serve --config fix-agent.toml
```

서버는 기본값으로 `127.0.0.1:7081`에서 실행된다. `/reviews`는 Bearer token이 있는 `version = 1` 리뷰 이벤트를 받고 `/health`는 서버 상태를 반환한다. 같은 장비에서 연동한다면 서버와 수신 token 없이 `fix-agent submit`으로 이벤트를 전달할 수도 있다. 다만 push와 PR 생성에는 저장소별 GitHub token이 필요하다.

```bash
curl http://127.0.0.1:7081/health
.venv/bin/fix-agent jobs --config fix-agent.toml --json
```

## 처리 범위

- `Matrix_Mobile_V2`, `TestSquare-Fable`의 `dev` branch 설정 포함
- Major·Minor finding 처리, Critical 기본 제외
- 대상 `AGENTS.md`, 추가 지침과 저장소 하네스 적용
- fingerprint 중복 방지, 독립 사실 검증과 사유 기록
- 전용 `autofix/` branch와 PR 생성, 직접 merge 금지

현재 `code-review-agent`와 자동 트리거는 연결하지 않았다. 승인 없이 사용할 수 있는 입력 방식은 HTTP API와 `fix-agent submit`이다.

## 문서

- [리뷰 에이전트 연동 가이드](docs/repo/01-리뷰-에이전트-연동-가이드.md): 설치, 로컬·HTTP 연결, 작업 상태와 운영 조건
- [리뷰 이벤트 v1 계약](docs/repo/02-리뷰-이벤트-v1-계약.md): 필드, fingerprint, 응답, 오류, 재시도와 호환성 규칙

## 검증

```bash
.venv/bin/python -m unittest discover -s tests -q
git diff --check
git status --short
```
