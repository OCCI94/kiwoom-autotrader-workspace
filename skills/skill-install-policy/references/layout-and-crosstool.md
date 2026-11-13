# 스킬 레이아웃과 Codex·Claude Code 공용 규칙 (2026-11-03 기준)

## 사양 (agentskills.io/specification)

- `SKILL.md` frontmatter 필드는 여섯 개뿐이다. 필수 `name`(1~64자, 소문자·숫자·하이픈, 앞뒤·연속 하이픈 금지, 부모 디렉터리명과 일치), 필수 `description`(1~1,024자), 선택 `license`, `compatibility`(500자 이하, 환경 요구 선언), `metadata`(문자열 맵), `allowed-tools`(실험적).
- 디렉터리: `SKILL.md` + 선택 `scripts/`, `references/`, `assets/`. 참조는 SKILL.md에서 한 단계 깊이까지.
- 로드 단계: 메타데이터(약 100토큰)는 항상, 본문(권장 5,000토큰·500줄 이하)은 활성화 시, 리소스는 필요할 때.
- 공식 검증기 `skills-ref`(github.com/agentskills/agentskills/skills-ref)는 "demonstration purposes only". 이 프로젝트는 `validate_skill.py`로 근사 검증한다.

## 두 도구의 탐색 경로

| 도구 | 프로젝트 경로 | 사용자 경로 | 비고 |
|---|---|---|---|
| OpenAI Codex CLI | `$CWD/.agents/skills` (상위로 탐색) | `~/.agents/skills`, `/etc/codex/skills` | 심볼릭 링크 따라감. `name`·`description`만 문서화. `$skill` 멘션 또는 `/skills`. 목록은 컨텍스트 2% 또는 8,000자까지. 같은 이름은 병합하지 않고 둘 다 노출. 문서: https://learn.chatgpt.com/docs/build-skills |
| Claude Code | `.claude/skills` (시작 디렉터리부터 상위로) | `~/.claude/skills`, 플러그인 | 심볼릭 링크 지원. `claude plugin validate .claude/skills`. 문서: https://code.claude.com/docs/en/skills |

이 저장소의 선택: 정본 `/Users/tr-oh/Documents/Codex/skills/`, 링크 `.agents/skills → ../skills`, `.claude/skills → ../skills`.

## 쓰지 말아야 할 Claude 전용 확장

`$ARGUMENTS`, `$1`, `${CLAUDE_SKILL_DIR}`, `` !`명령` `` 동적 주입, frontmatter의 `context: fork`, `hooks`, `disable-model-invocation`, `argument-hint`. Claude Code 밖에서는 사양 밖 키가 하드 에러("Unexpected key(s) in SKILL.md frontmatter")이고 Codex는 무시한다. 스크립트는 스킬 루트 기준 상대경로(`python3 scripts/x.py`)로 부른다.

## 스크립트 규약

- Python 3.9+ 표준 라이브러리만(이 Mac의 시스템 python3 3.9.6, Codex 런타임 3.12). `uv`가 없으므로 PEP 723 의존성 선언에 기대지 않는다.
- 비대화식, `--help`, JSON stdout, 진단은 stderr, 멱등, 변경 가능성이 있으면 `--dry-run`, 종료 코드 0/1/2/3.
- 비밀은 환경변수 이름으로만. 없으면 종료 코드 3과 안내 문구.
- Codex 권고: "Prefer instructions over scripts unless you need deterministic behavior or external tooling". 결정론이 필요한 곳(대조·통계·검증)에만 스크립트를 둔다.

## 완화 장치

- Claude Code: `disableSkillShellExecution`, 권한 규칙 `Skill(name)` deny, `skillOverrides`.
- Codex: `[[skills.config]] enabled=false`, `[skills.bundled] enabled=false`, 스킬별 `agents/openai.yaml`의 `policy.allow_implicit_invocation: false`(명시 호출만). 이 파일은 Claude Code에 무해하다.
