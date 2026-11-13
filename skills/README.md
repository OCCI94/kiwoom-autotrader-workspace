# ai-stock 자체 스킬 모음

이 폴더가 스킬의 정본이다. `/Users/tr-oh/Documents/Codex/.agents/skills`와 `/Users/tr-oh/Documents/Codex/.claude/skills`는 이 폴더를 가리키는 심볼릭 링크이며, OpenAI Codex CLI는 `.agents/skills`, Claude Code는 `.claude/skills`를 읽는다. 두 도구 모두 시작 디렉터리에서 상위로 올라가며 찾으므로 `/Users/tr-oh/Documents/Codex` 아래에서 시작한 세션은 같은 파일을 본다.

봇 소스 루트(`2026-10-26/new-chat/outputs/ai-stock`) 안에는 아직 두지 않는다. Codex의 배포 도구가 소스 manifest 해시로 검증하므로 파일을 추가하면 진행 중인 배포 검증이 깨질 수 있다. 소스 루트에서도 읽게 하려면 배포가 없는 시간에 `ln -s ../../../../../skills outputs/ai-stock/.agents/skills` 같은 링크를 거는 것으로 충분하다.

## 공통 규약

- `SKILL.md` frontmatter는 agentskills.io 사양의 6필드(`name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools`)만 쓴다. `name`은 디렉터리명과 같아야 한다.
- 본문은 한국어, 500줄 이하. `description`은 영어 3인칭으로 쓰되 사용자가 실제로 말할 한국어 트리거어를 섞는다.
- Claude 전용 확장(`$ARGUMENTS`, `${CLAUDE_SKILL_DIR}`, `` !`명령` `` 주입, `context:`, `hooks:`)은 쓰지 않는다. 두 도구에서 같은 파일이 돌아야 한다.
- 스크립트는 Python 3.9 이상 표준 라이브러리만 쓴다(이 Mac의 시스템 python3는 3.9.6, Codex 런타임은 3.12). `--help`, JSON stdout, 진단은 stderr, 종료 코드 0 정상 / 1 경고·발견 / 2 실패 / 3 사용법·실행 오류.
- 스크립트는 주문을 내지 않고, 실행 중인 봇·설정·원장·LaunchAgent를 바꾸지 않으며, 비밀값(`config/secrets/`, 토큰 문자열)을 읽거나 출력하지 않는다. 원장은 복사본 또는 `mode=ro`로만 연다.
- 비밀정보는 환경변수 이름으로만 언급한다.
- 검증: `python3 skills/skill-install-policy/scripts/validate_skill.py skills/<이름>`.

## 목록 (로드맵 §0 묶음, 2026-11-03)

| # | 스킬 | 역할 |
|---|---|---|
| 0 | `skill-install-policy` | 제3자 스킬·MCP 도입 심사와 frontmatter 검증 |
| 1 | `kiwoom-rest-ops-runbook` | 키움 REST 모의투자 봇 읽기 전용 헬스체크와 장애 분류 |
| 2 | `ledger-reconciliation` | 원장 복사본과 증권사 관측 대조, 손익 재계산 |
| 3 | `arm-change-review` | arm 변경 전 검토 기록, 배포 창 검사, 기능 게이트 레지스트리 |
| 4 | `launchd-scheduler` | LaunchAgent plist 작성·검증·상태 확인 |
