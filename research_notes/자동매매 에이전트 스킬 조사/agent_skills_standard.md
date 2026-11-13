# Agent Skills 오픈 표준과 생태계 (2026-11 기준)

조사일: 2026-11-03. 모든 항목에 출처 URL을 붙였고, 날짜가 특정되는 자료(블로그·보안 보고서·버전 노트)는 날짜를 병기했다. 최종 용도: Kiwoom REST API 모의투자 봇(Python, Telegram 제어)을 OpenAI Codex CLI(주)와 Claude Code(부)에서 함께 쓸 수 있는 스킬로 만드는 것.

---

## Q1. agentskills/agentskills 저장소·사양(spec)의 정확한 내용

### Takeaway
저장소에는 사양 본문이 아니라 문서 사이트(`docs/`)와 Python 레퍼런스 검증기(`skills-ref/`)만 있고, 사양 본문은 agentskills.io/specification 에 게시된다. 사양은 `SKILL.md`의 YAML frontmatter 6개 필드(`name`, `description` 필수 / `license`, `compatibility`, `metadata`, `allowed-tools` 선택)와 `scripts/`·`references/`·`assets/` 디렉터리 관례, 3단계 progressive disclosure(메타 ~100 tokens → 본문 <5,000 tokens·500줄 이하 → 리소스 온디맨드)를 규정한다.

### Cited Findings

**저장소 구성·거버넌스**
- 저장소 폴더는 `.claude/`, `docs/`, `skills-ref/`, 파일은 `.gitignore`, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `LICENSE`, `README.md`, `package.json`. `spec/`나 `examples/` 폴더는 없고, 사양은 agentskills.io/specification, 예제 스킬은 별도 저장소(anthropics/skills)로 링크된다 — [GitHub agentskills/agentskills](https://github.com/agentskills/agentskills)
- README 표현: "A standardized way to give AI agents new capabilities and expertise." 메타데이터는 "metadata (`name` and `description`, at minimum)" — [GitHub agentskills/agentskills](https://github.com/agentskills/agentskills)
- 라이선스: 코드 Apache 2.0, 문서 CC-BY-4.0("See individual directories for details."). 형식은 Anthropic이 개발해 오픈 표준으로 공개했고 "a growing number of agent products"가 채택. 조사 시점 스탯: 26.9k stars, 2.0k forks, 145 commits, 릴리스 없음. Discord: discord.gg/MKPE9g8aUy — [GitHub agentskills/agentskills](https://github.com/agentskills/agentskills)
- 문서 사이트 페이지 전체 목록(9개): home, specification, clients, client-implementation/adding-skills-support, skill-creation/{quickstart, best-practices, evaluating-skills, optimizing-descriptions, using-scripts}. 보안 전용 페이지와 커뮤니티 스킬 목록 페이지는 없음 — [agentskills.io/llms.txt](https://agentskills.io/llms.txt)
- 표준의 기원: Anthropic 엔지니어링 블로그(2025-10-16)에서 스킬을 "organized folders of instructions, scripts, and resources that agents can discover and load dynamically"로 정의하고 "Building a skill for an agent is like putting together an onboarding guide for a new hire."라고 설명 — [Anthropic Engineering, 2025-10-16](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)

**디렉터리 레이아웃(사양 원문)**
```
skill-name/
├── SKILL.md          # Required: metadata + instructions
├── scripts/          # Optional: executable code
├── references/       # Optional: documentation
├── assets/           # Optional: templates, resources
└── ...               # Any additional files or directories
```
— [agentskills.io/specification](https://agentskills.io/specification)

**Frontmatter 필드(사양 표 원문)**

| Field | Required | Constraints |
|---|---|---|
| `name` | Yes | Max 64 characters. Lowercase letters, numbers, and hyphens only. Must not start or end with a hyphen. |
| `description` | Yes | Max 1024 characters. Non-empty. Describes what the skill does and when to use it. |
| `license` | No | License name or reference to a bundled license file. |
| `compatibility` | No | Max 500 characters. Indicates environment requirements (intended product, system packages, network access, etc.). |
| `metadata` | No | Arbitrary key-value mapping for additional metadata (a map from string keys to string values). |
| `allowed-tools` | No | Space-separated string of pre-approved tools the skill may use. (Experimental) |

— [agentskills.io/specification](https://agentskills.io/specification)

- `name` 세부 규칙 원문: "Must be 1-64 characters / May only contain unicode lowercase alphanumeric characters (`a-z`, `0-9`) and hyphens (`-`) / Must not start or end with a hyphen (`-`) / Must not contain consecutive hyphens (`--`) / Must match the parent directory name" — [agentskills.io/specification](https://agentskills.io/specification)
- `description` 규칙 원문: "Must be 1-1024 characters / Should describe both what the skill does and when to use it / Should include specific keywords that help agents identify relevant tasks". 좋은 예: "Extracts text and tables from PDF files, fills PDF forms, and merges multiple PDFs. Use when working with PDF documents or when the user mentions PDFs, forms, or document extraction." 나쁜 예: "Helps with PDFs." — [agentskills.io/specification](https://agentskills.io/specification)
- `compatibility` 예시: `compatibility: Requires Python 3.14+ and uv`, `compatibility: Designed for Claude Code (or similar products)`; "Most skills do not need the `compatibility` field." — [agentskills.io/specification](https://agentskills.io/specification)
- `metadata` 예시: `metadata: {author: example-org, version: "1.0"}`; "We recommend making your key names reasonably unique to avoid accidental conflicts" — [agentskills.io/specification](https://agentskills.io/specification)
- `allowed-tools` 예시: `allowed-tools: Bash(git:*) Bash(jq:*) Read`; "Experimental. Support for this field may vary between agent implementations" — [agentskills.io/specification](https://agentskills.io/specification)
- Anthropic 플랫폼 문서는 추가 제약을 둔다: `name`은 "Cannot contain XML tags / Cannot contain reserved words: `anthropic`, `claude`", `description`은 "Cannot contain XML tags" — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)

**본문·선택 디렉터리·progressive disclosure(원문)**
- 본문: "The Markdown body after the frontmatter contains the skill instructions. There are no format restrictions." 권장 섹션: "Step-by-step instructions / Examples of inputs and outputs / Common edge cases". "Note that the agent will load this entire file once it's decided to activate a skill. Consider splitting longer `SKILL.md` content into referenced files." — [agentskills.io/specification](https://agentskills.io/specification)
- `scripts/`: "Scripts should: Be self-contained or clearly document dependencies / Include helpful error messages / Handle edge cases gracefully. Supported languages depend on the agent implementation. Common options include Python, Bash, and JavaScript." — [agentskills.io/specification](https://agentskills.io/specification)
- `references/`: `REFERENCE.md`, `FORMS.md`, 도메인 파일(`finance.md`, `legal.md` 등); "Keep individual reference files focused. Agents load these on demand, so smaller files mean less use of context." `assets/`: 템플릿·이미지·데이터 파일(lookup tables, schemas) — [agentskills.io/specification](https://agentskills.io/specification)
- Progressive disclosure 원문: "1. **Metadata** (~100 tokens): The `name` and `description` fields are loaded at startup for all skills / 2. **Instructions** (< 5000 tokens recommended): The full `SKILL.md` body is loaded when the skill is activated / 3. **Resources** (as needed)". "Keep your main `SKILL.md` under 500 lines. Move detailed reference material to separate files." — [agentskills.io/specification](https://agentskills.io/specification)
- 파일 참조: "use relative paths from the skill root", "Keep file references one level deep from `SKILL.md`. Avoid deeply nested reference chains." — [agentskills.io/specification](https://agentskills.io/specification)
- 검증: "`skills-ref validate ./my-skill` … This checks that your `SKILL.md` frontmatter is valid and follows all naming conventions." — [agentskills.io/specification](https://agentskills.io/specification)

**skills-ref 레퍼런스 검증기**
- Python 패키지(`pyproject.toml`, `src/skills_ref`). 설치: venv 후 `pip install -e .` 또는 `uv sync`. CLI: `skills-ref validate <skill-path>`, `skills-ref read-properties <skill-path>`(JSON), `skills-ref to-prompt <skill-path>...`(`<available_skills>` XML 생성). Python API: `validate(path)`→문제 목록, `read_properties(path)`, `to_prompt(paths)`. 라이선스 Apache 2.0 — [GitHub skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref)
- 주의 문구: "This library is intended for demonstration purposes only. It is not meant to be used in production." `to-prompt`가 만드는 XML은 각 스킬의 name/description/location을 담으며 "recommended for Anthropic's models, and that other skill clients may format it differently" — [GitHub skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref)
- 클라이언트 구현 가이드 페이지가 별도로 존재: https://agentskills.io/client-implementation/adding-skills-support — [agentskills.io/llms.txt](https://agentskills.io/llms.txt)

### Inferences
- "spec"이 저장소 안에 파일로 없으므로, 로컬에서 사양을 고정 참조하려면 agentskills.io/specification.md(마크다운 버전)를 받아 두는 편이 안전하다.
- `name`이 "Must match the parent directory name"이므로 Claude Code처럼 디렉터리명을 기본값으로 쓰는 툴과 Codex처럼 `name`을 읽는 툴 모두에서 일치시켜야 충돌이 없다.
- `metadata.version`이 사양의 유일한 버전 표기 관례이므로 프로젝트 스킬 버저닝은 `metadata: {version: "x.y"}`로 통일하는 것이 합리적이다.

### Gaps
- `skills-ref validate`가 수행하는 개별 검사 항목(디렉터리명 일치, 길이, 문자셋 외의 세부)은 README에 명시되지 않아 소스코드 확인이 필요하다.
- 사양 자체에는 보안·비밀정보 처리 조항이 없다(llms.txt 상 보안 페이지 없음).

---

## Q2. 표준을 지원하는 제품과 스킬 발견 경로, Claude Code vs Codex 차이

### Takeaway
agentskills.io 클라이언트 쇼케이스 데이터에는 46개 제품이 등재돼 있다(Claude Code, Claude, ChatGPT & Codex, Cursor, Gemini CLI, GitHub Copilot, VS Code, OpenCode, Goose, Kiro, Amp, Factory, Roo Code, Junie 등). Codex는 2026-11 현재 프로젝트 `.agents/skills/`·사용자 `~/.agents/skills/`·관리자 `/etc/codex/skills`에서 읽고 `$skill`/`/skills`로 호출하며 frontmatter는 `name`·`description`만 문서화한다. Claude Code는 `.claude/skills/`·`~/.claude/skills/`·플러그인에서 읽고 `/skill-name`으로 호출하며, `allowed-tools`, `$ARGUMENTS`, `${CLAUDE_SKILL_DIR}`, `` !`cmd` `` 주입, `context: fork` 등 표준 밖 확장을 다수 가진다. 양쪽에서 돌리려면 이 확장을 쓰지 않아야 한다.

### Cited Findings

**클라이언트 쇼케이스(공식 데이터 파일)**
- `docs/snippets/clients.jsx`에 46개 엔트리. 주요 항목과 각 공식 문서 링크: Claude Code → https://code.claude.com/docs/en/skills ; Claude(claude.ai/API) → https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview ; ChatGPT & Codex → https://developers.openai.com/codex/skills/ (소스 https://github.com/openai/codex) ; Cursor → https://cursor.com/docs/context/skills ; Gemini CLI → https://geminicli.com/docs/cli/skills/ ; GitHub Copilot → https://docs.github.com/en/copilot/concepts/agents/about-agent-skills ; VS Code → https://code.visualstudio.com/docs/copilot/customization/agent-skills ; OpenCode → https://opencode.ai/docs/skills/ ; Goose → https://block.github.io/goose/docs/guides/context-engineering/using-skills/ ; Kiro → https://kiro.dev/docs/skills/ ; Amp → https://ampcode.com/manual#agent-skills ; Factory → https://docs.factory.ai/cli/configuration/skills ; Roo Code → https://docs.roocode.com/features/skills ; Junie → https://junie.jetbrains.com/docs/agent-skills.html ; OpenHands → https://docs.openhands.dev/overview/skills ; Letta, Mistral AI Vibe, TRAE, Spring AI(2026-01-13 블로그), Databricks Genie Code, Snowflake Cortex Code, Tabnine, Pulumi Neo, OpenClaw, Hermes Agent 등 — [agentskills clients.jsx (raw)](https://raw.githubusercontent.com/agentskills/agentskills/main/docs/snippets/clients.jsx)
- agentskills.io/clients 페이지 자체는 JS 렌더링이라 정적 fetch로는 목록이 비어 보인다("Shuffle / Alphabetical"만 출력) — [agentskills.io/clients](https://agentskills.io/clients)

**툴별 스킬 경로(Vercel `skills` CLI 설치 테이블, 3rd-party 문서이나 가장 포괄적)**

| Agent | 프로젝트 경로 | 글로벌 경로 |
|---|---|---|
| Claude Code | `.claude/skills/` | `~/.claude/skills/` |
| Codex | `.agents/skills/` | `~/.agents/skills/` |
| Cursor | `.agents/skills/` | `~/.cursor/skills/` |
| Gemini CLI | `.agents/skills/` | `~/.gemini/skills/` |
| GitHub Copilot | `.agents/skills/` | `~/.copilot/skills/` |
| OpenCode | `.agents/skills/` | `~/.config/opencode/skills/` |
| Amp | `.agents/skills/` | `~/.config/agents/skills/` |
| Goose | `.goose/skills/` | `~/.config/goose/skills/` |
| Kiro CLI | `.kiro/skills/` | `~/.kiro/skills/` |
| Windsurf | `.windsurf/skills/` | `~/.codeium/windsurf/skills/` |
| Antigravity | `.agents/skills/` | `~/.gemini/antigravity/skills/` |
| Zed | `.agents/skills/` | `~/.agents/skills/` |
| Factory/Droid | `.agents/skills/` | `~/.factory/skills/` |
| Roo Code | `.roo/skills/` | `~/.roo/skills/` |
| Kilo Code | `.agents/skills/` | `~/.kilo/skills/` |

— [vercel-labs/skills docs/agents.md](https://github.com/vercel-labs/skills/blob/main/docs/agents.md)
- 같은 문서: "Many agents, including Amp, Cursor, Codex, and Zed, use the shared `.agents/skills/` project directory." — [vercel-labs/skills docs/agents.md](https://github.com/vercel-labs/skills/blob/main/docs/agents.md)
- 검색 결과 요약(3rd-party README 기반): Cursor `.cursor/skills/`(또한 `.agents/skills/` 읽음), Gemini CLI `.gemini/skills/`, Copilot `.github/skills/`, OpenCode `.opencode/skills/` 등 툴 고유 경로도 병존하며, `.agents/skills/`가 Codex·Gemini CLI·Cursor·VS Code Copilot·Zed·Amp·Goose·OpenCode 등 다수 툴이 공통으로 읽는 경로로 수렴하고 있다. 단 OpenCode 경로는 소스 간 상충(`.opencode/skills/` vs `.agent/skills/`) — [WebSearch: julianosirtori/skills](https://github.com/julianosirtori/skills); [MicrosoftDocs/Agent-Skills](https://github.com/MicrosoftDocs/Agent-Skills); [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)

**OpenAI Codex (공식 문서; 2026-11-03 현재 developers.openai.com/codex/skills 는 learn.chatgpt.com/docs/build-skills 로 308 리다이렉트됨)**
- 표준 관계: "Skills build on the open agent skills standard." — [OpenAI Build skills](https://learn.chatgpt.com/docs/build-skills)
- 발견 위치: "Codex reads skills from repository, user, admin, and system locations." REPO: `$CWD/.agents/skills`, `$CWD/../.agents/skills`, … `$REPO_ROOT/.agents/skills`(현재 디렉터리에서 저장소 루트까지 상향 탐색); USER: `$HOME/.agents/skills`; ADMIN: `/etc/codex/skills`; SYSTEM: Codex 번들(`skill-creator`, `skill-installer` 포함). 심볼릭 링크 폴더 따라감. "If two skills share the same `name`, Codex doesn't merge them; both can appear in skill selectors." — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- 공식 문서는 `.codex/skills`, `~/.codex/skills`, `$CODEX_HOME/skills`를 언급하지 않음(설정 파일만 `~/.codex/config.toml`) — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- Frontmatter: "The `SKILL.md` file must include `name` and `description`." 다른 필드(`allowed-tools` 등)에 대한 언급 없음 — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- 컨텍스트 예산: 시작 시 name/description/경로만 로드, "this list uses at most 2% of the model's context window, or 8,000 characters when the context window is unknown." `skills.max_context_tokens`로 "up to 10,000 tokens"까지 조정 — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- 호출: "In Codex CLI or the IDE extension, run `/skills` or type `$` to mention a skill." ChatGPT에서는 `@`. 암묵 호출: "Codex can choose a skill when your task matches the skill description." — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- `agents/openai.yaml`(선택): `interface`(`display_name`, `short_description`, `icon_small`, `icon_large`, `brand_color`, `default_prompt`), `policy.allow_implicit_invocation`(기본 `true`; "When `false`, Codex won't implicitly invoke the skill based on user prompt;" 명시적 `$skill`은 계속 동작), `dependencies.tools`(`type`, `value`, `description`, `transport`, `url` — MCP 서버 선언 예) — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- 설정: `~/.codex/config.toml`에 `[[skills.config]]` + `path = "/path/to/skill/SKILL.md"` 또는 `name`, `enabled = false`("Use exactly one selector per entry.", "the last matching entry wins."); 프로젝트의 trusted `.codex/config.toml`에 `[skills.bundled] enabled = false`로 시스템 스킬 비활성 — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- 스크립트 지침: "Prefer instructions over scripts unless you need deterministic behavior or external tooling." 샌드박스·승인·스크립트 실행 방식은 이 페이지에 서술 없음 — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- 적용 범위: "Standalone skills are available in the ChatGPT desktop app, Codex CLI, and IDE extension." 플러그인 번들 스킬은 "universal plugin directory shared by ChatGPT and Codex"로 배포 — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- 생성·설치 도우미: `$skill-creator`(Codex)/`@skill-creator`(ChatGPT Work)는 설명으로부터 스킬 초안 작성(instruction-only 기본), Record & Replay로 시연 녹화 기반 초안 가능; `$skill-installer linear`처럼 큐레이션 스킬 설치, 다른 저장소에서도 설치 가능. 변경은 자동 감지, 안 보이면 Codex 재시작 — [OpenAI Build skills](https://learn.chatgpt.com/docs/build-skills)
- openai/codex 저장소 `docs/skills.md`는 한 줄("For information about skills, refer to this documentation")로 developers.openai.com/codex/skills 로 위임 — [openai/codex docs/skills.md (raw)](https://raw.githubusercontent.com/openai/codex/main/docs/skills.md)
- 레거시 경로 관련(3rd-party): 커뮤니티 PR 설명에 "Current Codex builds discover both `$HOME/.agents/skills` and `$CODEX_HOME/skills`, but the public documentation names `$HOME/.agents/skills` as the user-scoped location." — [EtanHey/golems PR #782](https://github.com/EtanHey/golems/pull/782); OpenAI의 공식 마이그레이션 스킬은 Claude 유래 스킬을 `.agents/skills/`에, 커스텀 에이전트를 `.codex/agents/`에 기록 — [openai/skills migrate-to-codex SKILL.md](https://github.com/openai/skills/blob/main/skills/.curated/migrate-to-codex/SKILL.md)
- Codex `/import`(Claude Code·Cursor 선택) 매핑 표: Instruction files→`AGENTS.md`, `settings.json`→`config.toml`, Skills→Skills, Slash commands→Skills, Subagents→Codex subagents, Hooks→Codex hooks, MCP→Codex MCP 설정. 가져온 뒤 검토 항목: "Tool restrictions or permissions in imported skills and agents", "Hooks whose behavior may differ after import", "Prompt templates or command-style prompts that depend on arguments, shell interpolation, or file-path placeholders" — [OpenAI Import (.md)](https://learn.chatgpt.com/docs/import.md)
- Codex 스킬 출시 시기: Medium 기사가 2025-12 출시라고 주장하나 OpenAI 1차 자료로 확인 못 함 — [WebSearch 결과](https://medium.com/@juno-skilltrove/codex-skills-what-they-are-how-they-work-and-where-to-find-them-5ca0a013d20e)

**Claude Code (공식 문서)**
- 표준 관계 원문: "Claude Code skills follow the Agent Skills open standard, which works across multiple AI tools. Claude Code extends the standard with additional features like invocation control, subagent execution, and dynamic context injection." — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 위치 표: Enterprise(managed settings dir의 `.claude/skills/`), Personal `~/.claude/skills/<skill-name>/SKILL.md`, Project `.claude/skills/<skill-name>/SKILL.md`, Nested `<subdir>/.claude/skills/...`, `--add-dir` 디렉터리, Plugin `<plugin>/skills/<skill-name>/SKILL.md`(`/plugin-name:skill-name`), claude.ai 계정 동기화 스킬. "Project skills load from the start directory and every parent up to the repo root." "Symlinked skill folders are supported." `synced`·`anthropic-skills`는 예약 폴더명. 이름 충돌 우선순위: "Enterprise over personal, and personal over project." — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- Frontmatter(전부 선택, `description` 권장): `name`(기본값 디렉터리명), `description`, `when_to_use`, `argument-hint`, `arguments`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `disallowed-tools`, `model`, `effort`, `context`(`fork`), `agent`, `background`, `hooks`, `paths`, `shell`, 그리고 "`metadata`, `license`, `compatibility`: Spec fields that Claude Code accepts but does not act on". "Claude Code ignores a field it doesn't recognize without reporting an error"; YAML 파싱 실패 시 "the skill still loads with no fields set." — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- **표준 밖 사용 시 하드 에러**: "Outside Claude Code (claude.ai uploads, the Skills API, `package_skill.py`), only `name`, `description`, `license`, `compatibility`, `metadata`, and `allowed-tools` are allowed." 에러 메시지 원문: `Unexpected key(s) in SKILL.md frontmatter: argument-hint. Allowed properties are: allowed-tools, compatibility, description, license, metadata, name` — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 호출: 자동("Claude uses skills when relevant") 또는 `/skill-name`. `disable-model-invocation: true` → 사용자만 호출, description은 컨텍스트에 미포함; `user-invocable: false` → Claude만 호출 — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- `allowed-tools` 의미: 호출 턴 동안만 사전 승인("The grant clears when you send your next message."), 도구를 제한하는 것은 아님. 예: `allowed-tools: Bash(git add *) Bash(git commit *) Bash(git status *)`. 스크립트 무프롬프트 실행 패턴: `allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/render.sh *)` — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 치환 변수(Claude 전용): `$ARGUMENTS`, `$ARGUMENTS[N]`/`$N`, `$name`, `${CLAUDE_SESSION_ID}`, `${CLAUDE_EFFORT}`, `${CLAUDE_SKILL_DIR}`, `${CLAUDE_PROJECT_DIR}`, `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_PLUGIN_DATA}`. 인자가 플레이스홀더에 안 들어가면 "appends them as `ARGUMENTS: <value>`" — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 동적 컨텍스트 주입(Claude 전용): `` !`git diff HEAD` `` 또는 ```` ```! ```` 블록; 실패 시 "aborts the entire skill invocation"; `disableSkillShellExecution` 설정으로 차단 가능 — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 서브에이전트 실행(Claude 전용): `context: fork` + `agent: Explore`; "The subagent doesn't see your conversation history, so the skill's instructions have to stand on their own." — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 컨텍스트 수명: 호출된 스킬 내용은 턴을 넘어 유지되며, 자동 compaction 시 "the first 5,000 tokens of each" / "a combined budget of 25,000 tokens"로 재첨부. 스킬 목록 예산은 "1% of the model's context window", 항목당 description+when_to_use 합산 1,536자에서 절단(`skillListingMaxDescChars`, `skillListingBudgetFraction`, `SLASH_COMMAND_TOOL_CHAR_BUDGET`로 조정). `/context`의 Skills 행 정확도 개선은 v2.1.196 — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 검증·진단: `claude plugin validate .claude/skills` 또는 `claude plugin validate ~/.claude/skills`(v2.1.233+), `/doctor`, `/skill-doctor`, `--debug`; 플러그인 eval에 `tool_used: Skill` grader로 트리거율 측정(`claude plugin eval`) — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 슬래시 명령 통합: "Custom commands have been merged into skills." `.claude/commands/deploy.md`와 `.claude/skills/deploy/SKILL.md` 모두 `/deploy` — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 배포 범위: "Project skills: Commit `.claude/skills/` to version control / Plugins: Create a `skills/` directory in your plugin / Managed: Deploy organization-wide through managed settings" — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 권한 규칙: `Skill(commit)`, `Skill(review-pr *)`, deny `Skill(deploy *)`; `skillOverrides`로 `"on"`, `"name-only"`, `"user-invocable-only"`, `"off"` — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 개인 스킬 사라짐 이슈: v2.1.280 이전 `~/.claude/skills/manifest.json`이 있으면 해당 폴더를 `.trash/`로 이동시키는 버그; 30일 후 삭제 — [Claude Code Skills](https://code.claude.com/docs/en/skills)

**Anthropic 플랫폼(claude.ai / Skills API) 환경 차이**
- "claude.ai: Can install packages from npm and PyPI and pull from GitHub repositories / Claude API: Has no network access and no runtime package installation" — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)

### Inferences
- 프로젝트 저장소에서 **정본을 `.agents/skills/`에 두고 `.claude/skills` → `.agents/skills` 심볼릭 링크**를 거는 방식이 Codex(네이티브 경로)·Claude Code(심링크 지원 명시)·Cursor/Gemini/Copilot(공유 경로) 전부를 한 번에 만족시킨다. 검색에서 실제 이 패턴을 쓰는 프로젝트가 확인됐다(출처 요약 참조: "`.claude/skills` is a symlink to that directory so Claude Code still loads the same skills").
- 양쪽 호환 스킬의 frontmatter는 사양 6개 필드로 한정해야 한다. Claude Code 확장 필드는 Claude Code에서만 동작하고 claude.ai/API에서는 하드 에러이며, Codex는 `name`/`description` 외 필드를 어떻게 다루는지 문서화돼 있지 않다.
- `allowed-tools`는 사양상 Experimental이고 Codex 문서에 전혀 없으므로, 크로스툴 스킬에서는 "있어도 무해하지만 Claude Code에서만 효과" 정도로 가정해야 한다. Codex의 승인·샌드박스는 스킬이 아닌 Codex 전역 설정(`config.toml`)이 좌우한다.
- `${CLAUDE_SKILL_DIR}`·`$ARGUMENTS`·`` !`cmd` ``·`context: fork`는 Codex에서 치환되지 않으므로(Codex import 문서가 "arguments, shell interpolation, or file-path placeholders" 의존 프롬프트를 검토 대상으로 명시) 크로스툴 스킬 본문에는 쓰지 말고, 스크립트는 사양대로 스킬 루트 기준 상대경로(`scripts/x.py`)로 안내해야 한다.
- Codex는 동일 `name` 스킬을 병합하지 않고 둘 다 노출하므로 `.agents/skills`와 `~/.agents/skills`에 같은 이름을 두면 선택기에서 중복이 생긴다. 프로젝트 전용은 저장소에만 두는 것이 깔끔하다.

### Gaps
- Codex가 `SKILL.md`의 `scripts/`를 실행할 때 샌드박스·승인이 어떻게 적용되는지, 스킬 디렉터리 경로를 모델에 어떤 형태로 전달하는지(상대경로 해석 기준)는 공식 문서에 없다.
- Codex의 `$CODEX_HOME/skills` 레거시 지원 여부는 3rd-party PR 설명뿐이며 공식 확인 불가.
- Codex 스킬 기능의 최초 출시 버전/날짜는 OpenAI 1차 자료로 확인 못 함.
- Cursor·Gemini CLI·Copilot·OpenCode 공식 문서는 직접 열지 않았고(쇼케이스 링크만 확보), 경로는 Vercel 테이블·3rd-party README 기준이다.

---

## Q3. 공식/레퍼런스 스킬, 금융 프로젝트 관련성, 레지스트리와 보안 우려

### Takeaway
Anthropic의 anthropics/skills에는 19개 스킬(문서 4종 docx/pdf/pptx/xlsx는 source-available, 나머지는 대체로 Apache-2.0)이 있고 `/plugin marketplace add anthropics/skills`로 설치한다. OpenAI의 openai/skills 카탈로그는 deprecated 되어 openai/plugins로 이전됐다. 공개 레지스트리(skills.sh, ClawHub, awesome 목록)는 급성장했지만 Snyk ToxicSkills(2026-02) 조사에서 3,984개 중 36.82%가 보안 결함, 76개가 확인된 악성, 악성 샘플의 91%가 prompt injection을 사용해 서드파티 스킬은 npm/PyPI 수준의 공급망 검증이 필요하다.

### Cited Findings

**anthropics/skills (Anthropic 공식 예제·프로덕션 스킬)**
- `skills/` 디렉터리 19개: academy-guide, algorithmic-art, brand-guidelines, canvas-design, claude-api, discernment-nudge, doc-coauthoring, docx, frontend-design, internal-comms, mcp-builder, pdf, pptx, skill-creator, slack-gif-creator, theme-factory, web-artifacts-builder, webapp-testing, xlsx — [GitHub anthropics/skills/skills](https://github.com/anthropics/skills/tree/main/skills)
- 라이선스 원문: "Many skills in this repo are open source (Apache 2.0). We've also included the document creation & editing skills … These are source-available, not open source, but we wanted to share these with developers as a reference for more complex skills that are actively used in a production AI application." 문서 스킬(`skills/docx`, `skills/pdf`, `skills/pptx`, `skills/xlsx`)은 Claude의 파일 생성 기능을 "power … under the hood" — [GitHub anthropics/skills](https://github.com/anthropics/skills)
- 설치: `/plugin marketplace add anthropics/skills` → `/plugin install document-skills@anthropic-agent-skills` 또는 `/plugin install example-skills@anthropic-agent-skills` — [GitHub anthropics/skills](https://github.com/anthropics/skills)
- 면책 원문: "These skills are provided for demonstration and educational purposes only. … Always test skills thoroughly in your own environment before relying on them for critical tasks." 저장소 성격: "This repository contains Anthropic's implementation of skills for Claude. For information about the Agent Skills standard, see agentskills.io." — [GitHub anthropics/skills](https://github.com/anthropics/skills)
- skill-creator의 기능(Claude Code 문서 서술): 벤치마크(with/without pass rate·time·tokens → `benchmark.json`), 두 버전 blind A/B, description tuning(should/should-not trigger 프롬프트 생성·hit rate 측정·수정 제안), HTML 리뷰 뷰어 — [Claude Code Skills](https://code.claude.com/docs/en/skills); 발표 블로그 링크: https://claude.com/blog/improving-skill-creator-test-measure-and-refine-agent-skills

**OpenAI 측 카탈로그**
- "This repository is deprecated." → https://github.com/openai/plugins 로 안내. 구조: `.system`("automatically installed in the latest version of Codex"), `.curated`(이름만으로 설치), `.experimental`(폴더 지정 필요). 설치 예: `$skill-installer gh-address-comments`, `$skill-installer install https://github.com/openai/skills/tree/main/skills/.experimental/create-plan`. "After installing a skill, restart Codex to pick up new skills." 표준 링크와 "Write once, use everywhere." — [GitHub openai/skills](https://github.com/openai/skills)
- OpenAI 블로그 "Using skills to accelerate OSS maintenance"(Agents SDK 맥락) 존재 — [developers.openai.com/blog/skills-agents-sdk](https://developers.openai.com/blog/skills-agents-sdk)

**공개 레지스트리·마켓플레이스**
- Vercel `skills` CLI("The CLI for the open agent skills ecosystem"): Node.js 22.20+; `npx skills add vercel-labs/agent-skills`(`-g` 글로벌), `find`, `list`, `use <source>@<skill>`, `check`, `update`, `remove`, `init [name]`; 실험 기능 `experimental_sync`(npm 의존성에 포함된 스킬 동기화), `experimental_install`(`skills-lock.json` 복원); 지원 에이전트 "OpenCode, Claude Code, Codex, Cursor, and 75 more"; 디렉터리 skills.sh; 텔레메트리 수집(`DISABLE_TELEMETRY=1`/`DO_NOT_TRACK=1`로 해제, 이 경우 "security-audit requests"도 꺼짐) — [GitHub vercel-labs/skills](https://github.com/vercel-labs/skills)
- 큐레이션 목록: VoltAgent/awesome-agent-skills("1000+ agent skills … compatible with Claude Code, Codex, Gemini CLI, Cursor, and more") — [GitHub VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills); MicrosoftDocs/Agent-Skills(Microsoft·Azure 큐레이션) — [GitHub MicrosoftDocs/Agent-Skills](https://github.com/MicrosoftDocs/Agent-Skills); Python CLI `skillsmd` — [PyPI skillsmd](https://pypi.org/project/skillsmd/)
- ClawHub: OpenClaw(쇼케이스 46번째 클라이언트)의 레지스트리 — [Snyk ToxicSkills](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)

**보안·공급망 우려**
- Snyk ToxicSkills(데이터 기준일 2026-02-05): ClawHub·skills.sh의 3,984개 스킬 스캔. 36.82%(1,467개)에 최소 1개 보안 결함, 13.4%(534개)에 critical 이슈, 76개가 사람 검토로 확인된 악성, 공개 시점에 8개가 ClawHub에 여전히 공개. 확인된 악성 스킬의 91%가 prompt injection 사용, 100%가 악성 코드 패턴 포함. 전체 ClawHub 스킬 중 prompt injection 2.6%(상위 100개는 0%), 하드코딩된 비밀정보 노출 10.9%(악성 샘플은 32%), 서드파티 콘텐츠 fetch 17.7%(간접 prompt injection 경로), 검증 불가 런타임 의존성 악성 21% vs 전체 2.9% — [Snyk ToxicSkills, 2026-02](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)
- 관찰된 공격 기법: 설치 지시문에서 비신뢰 바이너리(암호 ZIP 포함) 링크, Base64/Unicode 난독화로 AWS 자격증명 파일 읽어 전송, 원격 스크립트를 셸에 파이프, 보안 경고 비활성화·시스템 서비스 변경 유도 — [Snyk ToxicSkills, 2026-02](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)
- Snyk 권고: `uvx mcp-scan@latest --skills`로 설치 스킬 스캔, 플래그된 저자·이름 제거, 노출된 API 키·클라우드·금융 자격증명 회전, 에이전트 메모리 파일(`SOUL.md`, `MEMORY.md`) 무단 변경 점검, 스킬을 npm/PyPI 패키지와 같은 공급망으로 취급("run with the agent's full permissions") — [Snyk ToxicSkills, 2026-02](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)
- 학술 연구(검색 스니펫 기반, 원문 미열람): "Skill-Inject: Measuring Agent Vulnerability to Skill File Attacks" — [arXiv 2602.20156](https://arxiv.org/pdf/2602.20156); "Under the Hood of SKILL.md: Semantic Supply-chain Attacks on AI Agent Skill Registry"(ClawHub 대상, 스킬 등록·랭킹·선택 단계가 공격면) — [arXiv 2605.11418](https://arxiv.org/html/2605.11418v1); "PhantomSkill: Malicious Code Injection in Agent Skill Ecosystems" — [arXiv 2606.19191](https://arxiv.org/pdf/2606.19191); 수명주기 서베이(ClawHub 스킬의 최대 47%가 악성/보안 이슈라는 보고 인용) — [arXiv 2607.10113](https://arxiv.org/pdf/2607.10113); 정적분석 탐지 한계 "SkillsMetric" — [arXiv 2608.08468](https://arxiv.org/pdf/2608.08468). 핵심 논지: SKILL.md 본문이 "loads directly into agent prompt context with no sanitization layer"이고 파일 전체가 지시문이라 instruction/data 분리 방어가 약함 — [WebSearch 요약](https://obot.ai/blog/mcp-security-agent-skills-supply-chain/)
- Anthropic 공식 입장(2025-10-16): 신뢰할 수 있는 출처의 스킬만 설치하고, 덜 신뢰되는 스킬은 번들 파일·의존성·외부 네트워크 연결 지시를 사용 전에 감사하라 — [Anthropic Engineering, 2025-10-16](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)
- Claude Code 측 완화 장치: 스킬 내 셸 주입을 `disableSkillShellExecution`으로 차단, `Skill(name)` 권한 규칙으로 특정 스킬 deny, `skillOverrides`로 `"off"` — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- Codex 측 완화 장치: `[[skills.config]] enabled=false`, `[skills.bundled] enabled=false`, `policy.allow_implicit_invocation: false`(명시 호출만 허용) — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)

**금융/트레이딩 프로젝트에 관련 있는 공식 스킬**
- `xlsx`(스프레드시트 읽기/쓰기/수식/차트), `pdf`(텍스트·표 추출, 병합/분할, 양식, OCR), `docx`, `pptx`는 Anthropic 프로덕션 문서 스킬(source-available) — [GitHub anthropics/skills](https://github.com/anthropics/skills); 이 세션에 로드된 동일 스킬 설명에 따르면 pdf 스킬은 "reading or extracting text/tables from PDFs … OCR on scanned PDFs", xlsx 스킬은 ".xlsx, .xlsm, .xltx, .csv, .tsv … computing formulas, formatting, charting" 등을 다룸(본 세션의 skill 목록 설명에서 인용)
- `skill-creator`(스킬 생성·eval·description 튜닝), `webapp-testing`(웹앱 테스트), `mcp-builder`(MCP 서버 생성), `claude-api` 등 개발용 스킬 — [GitHub anthropics/skills/skills](https://github.com/anthropics/skills/tree/main/skills)
- Anthropic 베스트프랙티스 문서의 "Pattern 2: Domain-specific organization" 예시가 재무 데이터 분석(`bigquery-skill/reference/finance.md` 등)을 다루며, 도메인별 reference 분리와 `grep -i "revenue" reference/finance.md` 식 탐색을 권장 — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)

### Inferences
- DART 공시 PDF 읽기·엑셀 리포트 생성은 Anthropic `pdf`/`xlsx` 스킬이 직접 해당하지만, 이들은 Claude 플러그인(마켓플레이스) 형태이고 라이선스가 source-available이므로, Codex에서 쓰려면 `.agents/skills`에 복사해 두는 것이 가능하더라도 라이선스 조건(LICENSE.txt) 확인이 필요하다. 프로젝트 전용으로는 pdfplumber·openpyxl 기반의 작은 자체 스킬을 쓰는 편이 양쪽 호환성과 라이선스 면에서 단순하다.
- 거래 봇은 증권사 API 키·텔레그램 토큰을 다루므로, 서드파티 스킬 설치는 "에이전트 전체 권한을 그 스킬에 위임"하는 행위로 보고 Snyk 권고(스캔, 리뷰, 키 회전)를 설치 절차에 포함해야 한다. 공개 레지스트리에서 금융 관련 스킬을 받아 쓰는 것은 특히 위험하다.
- OpenAI 카탈로그가 openai/plugins로 이동했으므로 Codex 측 공식 예제는 그 저장소를 기준으로 삼아야 한다.

### Gaps
- openai/plugins 저장소 내용(플러그인 포맷, 스킬 번들 방식)은 직접 확인하지 못했다.
- anthropics/skills 개별 스킬의 SKILL.md(특히 xlsx/pdf의 의존 패키지와 실행 전제)는 열지 않았다.
- skills.sh 디렉터리의 신뢰·검수 정책, ClawHub의 현재 검수 상태는 확인하지 못했다.
- arXiv 논문들은 검색 스니펫만 확보했으며 본문·수치는 미검증이다.

---

## Q4. 스킬 저작 베스트 프랙티스 (스킬 vs CLAUDE.md/AGENTS.md, description, 스크립트 번들, 테스트·evals, 버저닝)

### Takeaway
스킬은 "항상 로드되는 지시(CLAUDE.md/AGENTS.md)"와 달리 호출될 때만 본문이 로드되므로, 길고 가끔 쓰는 절차·도메인 참조·스크립트를 담는 데 적합하고, 매번 지켜야 하는 규칙은 지시 파일이나 hook에 둔다. description은 3인칭·명령형으로 "무엇을 + 언제"를 키워드와 함께 쓰고, should/should-not trigger 쿼리 ~20개로 트리거율을 측정해 다듬는다. 출력 품질은 `evals/evals.json` + with/without 비교 + assertion 채점으로 반복 개선한다.

### Cited Findings

**스킬 vs 지시 파일(CLAUDE.md/AGENTS.md)**
- "Unlike CLAUDE.md content, a skill's body loads only when it's used, so long reference material costs almost nothing until you need it." — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- Claude가 스킬을 따르다 중단하는 경우의 처방: "Claude skipped a rule that must hold every time: move the rule into a hook. … To keep the rule with the skill, define the hook in the skill's `hooks` frontmatter." 판단이 필요한 지침은 "Run the tests after every edit"처럼 과업 전체에 걸리게 표현; "Claude Code adds the skill's content to the conversation when the skill is invoked and doesn't re-read the file on later turns." compaction 후에는 재호출 필요, 중요한 지시는 SKILL.md 상단에 — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 트리거 뉘앙스: "agents typically only consult skills for tasks that require knowledge or capabilities beyond what they can handle alone. A simple, one-step request like 'read this PDF' may not trigger a PDF skill even if the description matches perfectly" — [agentskills.io optimizing-descriptions](https://agentskills.io/skill-creation/optimizing-descriptions.md)
- Codex는 instruction-only 스킬을 기본으로 하며 "Prefer instructions over scripts unless you need deterministic behavior or external tooling." — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- Anthropic 블로그: 결정적 코드는 모델이 비싸거나 불안정하게 처리하는 작업(예: PDF 양식 필드 추출)을 맡기며, 스킬은 MCP 서버를 보완("complement MCP servers")한다 — [Anthropic Engineering, 2025-10-16](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)

**무엇을 담을지·범위·분량**
- "Add what the agent lacks, omit what it knows": 프로젝트 고유 관례, 도메인 절차, 비자명한 엣지케이스, 특정 도구/API만. "Would the agent get this wrong without this instruction? If the answer is no, cut it." — [agentskills.io best-practices](https://agentskills.io/skill-creation/best-practices.md)
- "Design coherent units": 함수처럼 응집된 단위. "A skill for querying a database and formatting the results may be one coherent unit, while a skill that also covers database administration is probably trying to do too much." — [agentskills.io best-practices](https://agentskills.io/skill-creation/best-practices.md)
- "Aim for moderate detail": 과도하게 포괄적인 스킬은 오히려 해가 됨. 500줄·5,000 tokens 이하 권고, 더 필요하면 `references/`로 분리하되 "telling the agent *when* to load each file" (예: "Read `references/api-errors.md` if the API returns a non-200 status code") — [agentskills.io best-practices](https://agentskills.io/skill-creation/best-practices.md)
- "Start from real expertise": 실제 작업을 수행하며 교정한 내용·런북·API 스펙·코드리뷰 코멘트·VCS 패치 기록에서 추출; LLM 일반지식만으로 생성한 스킬은 "vague, generic procedures" — [agentskills.io best-practices](https://agentskills.io/skill-creation/best-practices.md)
- 패턴: Gotchas 섹션("the highest-value content in many skills"; SKILL.md 안에 둠), 출력 템플릿(`assets/`에 두고 참조), 체크리스트, validation loop, plan-validate-execute(배치·파괴적 작업), 반복 재발명 로직은 `scripts/`로 번들 — [agentskills.io best-practices](https://agentskills.io/skill-creation/best-practices.md)
- 자유도 보정: 다수 접근이 유효하면 "why"를 설명하는 유연한 지시, 취약·순서 중요 작업은 "Run exactly this sequence … Do not modify the command or add additional flags." 기본값 하나 제시("Provide defaults, not menus"), 절차 중심("Favor procedures over declarations") — [agentskills.io best-practices](https://agentskills.io/skill-creation/best-practices.md); [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- Anthropic 추가 지침: 시간 민감 정보 금지(대신 "Old patterns" `<details>` 섹션), 용어 일관성, Windows 경로 금지(항상 `/`), 100줄 넘는 reference 파일은 목차 포함, 참조는 한 단계 깊이, 여러 모델(Haiku/Sonnet/Opus)로 테스트 — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)

**description 작성·트리거 최적화**
- "Always write in third person. The description is injected into the system prompt, and inconsistent point-of-view can cause discovery problems." 좋은 예: "Processes Excel files and generates reports" / 나쁜 예: "I can help you process Excel files" — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- agentskills.io 원칙: "Use imperative phrasing … 'Use this skill when...'", "Focus on user intent, not implementation", "Err on the side of being pushy. Explicitly list contexts where the skill applies, including cases where the user doesn't name the domain directly", "Keep it concise … hard limit of 1024 characters" — [agentskills.io optimizing-descriptions](https://agentskills.io/skill-creation/optimizing-descriptions.md)
- Before/After 예시: `description: Process CSV files.` → "Analyze CSV and tabular data files — compute summary statistics, add derived columns, generate charts, and clean messy data. Use this skill when the user has a CSV, TSV, or Excel file and wants to explore, transform, or visualize the data, even if they don't explicitly mention 'CSV' or 'analysis.'" — [agentskills.io optimizing-descriptions](https://agentskills.io/skill-creation/optimizing-descriptions.md)
- 트리거 eval: `eval_queries.json`에 `{query, should_trigger}` 약 20개(각 8-10개), near-miss 부정 사례가 가장 유용, 각 쿼리 3회 실행해 trigger rate(임계 0.5), train 60%/validation 40% 분할, 5회 반복이면 보통 충분, 실패 쿼리의 키워드를 그대로 넣는 것은 overfitting. Claude Code용 검출 스크립트 예: `claude -p "$query" --output-format json | jq … .name == "Skill" and .input.skill == $skill` — [agentskills.io optimizing-descriptions](https://agentskills.io/skill-creation/optimizing-descriptions.md)
- Claude Code 실전 체크: 트리거 안 되면 description에 사용자가 실제로 말할 키워드 포함, `What skills are available?`로 노출 확인, 너무 자주 트리거되면 description을 좁히거나 `disable-model-invocation: true`; `<`·`>`는 `&lt;`·`&gt;`로 표시되므로 `[branch]`처럼 대괄호 사용 — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- 이름: gerund 형(`processing-pdfs`, `analyzing-spreadsheets`) 권장, 명사구·동사형도 허용; `helper`, `utils`, `documents` 같은 모호한 이름과 예약어 `anthropic-helper`, `claude-tools` 금지 — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)

**스크립트 번들**
- 유틸리티 스크립트의 이점: "Are more reliable than generated code / Save tokens / Save time / Ensure consistency across uses". 실행 vs 참조 의도 명시: "Run `analyze_form.py` to extract fields"(execute) vs "See `analyze_form.py` for the field extraction algorithm"(read) — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- "Solve, don't defer": 오류를 스크립트가 처리(FileNotFoundError 시 기본 파일 생성 등), "voodoo constants" 금지(`REQUEST_TIMEOUT = 30` 이유 주석) — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- 의존성: "Don't assume packages are available … Install required package: `pip install pypdf`" 명시; MCP 도구는 `ServerName:tool_name` 완전수식 — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- SKILL.md에 스크립트 목록 섹션("## Available scripts — `scripts/validate.sh` …")과 워크플로(`bash scripts/validate.sh "$INPUT_FILE"`, `python3 scripts/process.py --input results.json`) 기재 — [agentskills.io using-scripts](https://agentskills.io/skill-creation/using-scripts.md)

**테스트·eval(출력 품질)**
- "Create evaluations BEFORE writing extensive documentation." 절차: 스킬 없이 실행해 실패 기록 → 3개 시나리오 → baseline → 최소 지시 작성 → 반복. 평가 JSON 예: `{"skills": ["pdf-processing"], "query": "...", "files": [...], "expected_behavior": [...]}`; "There is not currently a built-in way to run these evaluations." — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- agentskills.io 형식: 스킬 디렉터리 내 `evals/evals.json` = `{"skill_name", "evals": [{"id", "prompt", "expected_output", "files", "assertions"}]}`; 워크스페이스 `<skill>-workspace/iteration-N/eval-*/{with_skill,without_skill}/{outputs/, timing.json, grading.json}` + `benchmark.json`(pass_rate/time/tokens mean·stddev·delta) + `feedback.json`. 매 실행은 깨끗한 컨텍스트(서브에이전트/별도 세션). 채점: "Require concrete evidence for a PASS"; 기계적 검사는 스크립트, 전체 품질은 blind comparison LLM judge — [agentskills.io evaluating-skills](https://agentskills.io/skill-creation/evaluating-skills.md)
- 분석 규칙: 양쪽 모두 항상 통과하는 assertion 제거, 양쪽 모두 실패하면 assertion/케이스 점검, 스킬 있을 때만 통과하는 것이 가치, stddev 높으면 지시 모호. 개선 시 "Generalize from feedback / Keep the skill lean / Explain the why / Bundle repeated work" — [agentskills.io evaluating-skills](https://agentskills.io/skill-creation/evaluating-skills.md)
- skill-creator가 이 루프(evals 실행·채점·benchmark 집계·HTML 리뷰)를 자동화 — [agentskills.io evaluating-skills](https://agentskills.io/skill-creation/evaluating-skills.md); Claude Code에서 플러그인 스킬은 `claude plugin eval` + `tool_used: Skill` grader로 트리거율 측정 — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- "Develop Skills iteratively with Claude": Claude A(저작)·Claude B(실행) 분리, Claude B의 실패를 Claude A에 피드백; "Claude models handle the Skill format and structure natively." — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- Codex `$skill-creator`는 설명 기반 초안(instruction-only 기본) 및 Record & Replay 초안 — [OpenAI Build skills](https://learn.chatgpt.com/docs/build-skills)

**버저닝·배포**
- 사양의 버전 표기 관례: `metadata: {author: example-org, version: "1.0"}` — [agentskills.io/specification](https://agentskills.io/specification)
- 버전 비교: skill-creator "runs a blind A/B between two versions of the skill so you can confirm an edit is an improvement before committing it"; evaluating-skills는 기존 버전을 `cp -r <skill-path> <workspace>/skill-snapshot/`로 스냅샷해 baseline으로 사용 — [Claude Code Skills](https://code.claude.com/docs/en/skills); [agentskills.io evaluating-skills](https://agentskills.io/skill-creation/evaluating-skills.md)
- 배포 채널: Claude Code는 `.claude/skills/` 커밋·플러그인 `skills/`·managed settings; Codex는 `.agents/skills/` 커밋, `$skill-installer <github url>`; 크로스툴은 `npx skills add <owner/repo>`(`skills-lock.json`, `check`/`update`) — [Claude Code Skills](https://code.claude.com/docs/en/skills); [GitHub openai/skills](https://github.com/openai/skills); [GitHub vercel-labs/skills](https://github.com/vercel-labs/skills)
- 갱신 반영: Codex "detects changes automatically … restart Codex if an update doesn't appear"; Claude Code는 호출 시점 내용을 대화에 넣고 이후 턴에서 재독하지 않음 — [OpenAI Build skills](https://learn.chatgpt.com/docs/build-skills); [Claude Code Skills](https://code.claude.com/docs/en/skills)

### Inferences
- 이 프로젝트에서는 "항상 지켜야 하는 안전 규칙(실거래 금지, 키 노출 금지, 주문 전 모의투자 모드 확인)"은 AGENTS.md/CLAUDE.md(및 Claude Code hook)에 두고, "Kiwoom REST API 호출 절차·에러코드 표·주문 검증 스크립트·DART PDF 파싱·일일 리포트 xlsx 생성" 같은 절차·참조·스크립트를 스킬로 분리하는 것이 두 툴 문서의 권고와 일치한다.
- description은 한국어 사용자 프롬프트를 염두에 두고 영어 설명에 한국어 트리거 키워드(예: "키움", "모의투자", "잔고", "체결", "DART 공시")를 함께 넣어야 트리거율이 오른다(사양은 "specific keywords that help agents identify relevant tasks"를 요구).
- 양쪽 툴에서 동일 eval을 돌리려면 agentskills.io 포맷(`evals/evals.json`)을 정본으로 두고, Claude Code에서는 skill-creator/`claude -p`, Codex에서는 `codex exec` 류 비대화 실행으로 각각 검출 스크립트만 바꾸는 구조가 맞다.

### Gaps
- Codex에서 스킬 호출 여부를 프로그램적으로 검출하는 공식 방법(로그·JSON 출력 필드)은 확인하지 못했다.
- AGENTS.md와 스킬의 우선순위·충돌 시 동작은 어느 쪽 문서에도 명시돼 있지 않다.

---

## Q5. 양쪽 환경에서 동작하는 스크립트 요건과 비밀정보 배제

### Takeaway
사양·가이드는 스크립트를 스킬 루트 기준 상대경로로 참조하고, 비대화식(non-interactive)·`--help`·구조화 stdout/stderr 분리·멱등·`--dry-run`·의미 있는 exit code·출력 크기 제한을 갖추며, Python은 PEP 723 인라인 의존성 + `uv run`으로 자급자족하게 만들라고 권한다. 사양 자체에 비밀정보 조항은 없지만 Snyk 조사에서 레지스트리 스킬의 10.9%가 하드코딩된 비밀을 노출했고, 스킬은 에이전트의 전체 권한으로 실행되므로 자격증명은 환경변수/외부 설정으로 분리하고 스킬 파일에는 절대 넣지 말아야 한다.

### Cited Findings

**환경 비의존 스크립트 작성(공식 가이드)**
- 경로: "Use relative paths from the skill directory root to reference bundled files. The agent resolves these paths automatically — no absolute paths needed." "script execution paths (in code blocks) are relative to the skill directory root, because the agent runs commands from there." — [agentskills.io using-scripts](https://agentskills.io/skill-creation/using-scripts.md)
- 전제조건 명시: "State prerequisites in your `SKILL.md` (e.g., 'Requires Node.js 18+') rather than assuming the agent's environment has them. For runtime-level requirements, use the `compatibility` frontmatter field." 버전 고정("Pin versions") — [agentskills.io using-scripts](https://agentskills.io/skill-creation/using-scripts.md)
- Python 자급자족 스크립트: PEP 723 `# /// script` 블록에 `dependencies = [...]`, `requires-python`; 실행 `uv run scripts/extract.py`(권장) 또는 `pipx run`; `uv lock --script`로 락파일; 단발 도구는 `uvx ruff@0.8.0 check .` — [agentskills.io using-scripts](https://agentskills.io/skill-creation/using-scripts.md)
- 에이전트용 인터페이스 설계: "Avoid interactive prompts. This is a hard requirement of the agent execution environment. Agents operate in non-interactive shells"; 입력은 플래그·환경변수·stdin; `--help`가 "the primary way an agent learns your script's interface"; 오류 메시지는 무엇이 잘못됐고 무엇을 기대했는지("Error: --format must be one of: json, csv, table. Received: 'xml'"); 구조화 출력(JSON/CSV/TSV) + "send structured data to stdout and … diagnostics to stderr" — [agentskills.io using-scripts](https://agentskills.io/skill-creation/using-scripts.md)
- 추가 고려: "Idempotency. Agents may retry commands.", 닫힌 입력 집합(enum), "Dry-run support … for destructive or stateful operations", 실패 유형별 exit code 문서화, "Safe defaults … require explicit confirmation flags (`--confirm`, `--force`)", 출력 크기 예측 가능("Many agent harnesses automatically truncate tool output beyond a threshold (e.g., 10-30K characters)" → 요약 기본, `--offset`, `--output`) — [agentskills.io using-scripts](https://agentskills.io/skill-creation/using-scripts.md)
- 사양: "Scripts should: Be self-contained or clearly document dependencies / Include helpful error messages / Handle edge cases gracefully." — [agentskills.io/specification](https://agentskills.io/specification)
- Anthropic: Windows 경로 금지(`scripts/helper.py`), 패키지 설치 명령 명시, 플랫폼별 제약(claude.ai는 npm/PyPI 설치 가능, Claude API는 네트워크·런타임 설치 불가) — [platform.claude.com best-practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)
- Claude Code 예제 스크립트는 "requires Python 3 but uses only built-in libraries, so there are no packages to install"로 설계 — [Claude Code Skills](https://code.claude.com/docs/en/skills)

**툴 특화 기능 회피 근거**
- Claude Code 전용: `${CLAUDE_SKILL_DIR}`(스크립트 경로 해석용), `$ARGUMENTS`/`$N`, `` !`cmd` `` 주입, `context: fork`, `hooks`, `allowed-tools`의 사전승인 효과 — [Claude Code Skills](https://code.claude.com/docs/en/skills)
- Codex 전용: `agents/openai.yaml`(`interface`, `policy`, `dependencies`), `$skill` 멘션, `[[skills.config]]`; 문서화된 frontmatter는 `name`·`description`뿐 — [OpenAI Build skills (.md)](https://learn.chatgpt.com/docs/build-skills.md)
- Codex `/import` 후 검토 항목에 "Prompt templates or command-style prompts that depend on arguments, shell interpolation, or file-path placeholders"와 "Tool restrictions or permissions in imported skills" 명시 — [OpenAI Import (.md)](https://learn.chatgpt.com/docs/import.md)
- Claude Code 외부(claude.ai/API)에서 비표준 키는 하드 에러: "Allowed properties are: allowed-tools, compatibility, description, license, metadata, name" — [Claude Code Skills](https://code.claude.com/docs/en/skills)

**비밀정보**
- Snyk: ClawHub 스킬의 10.9%가 하드코딩 비밀정보 노출, 악성 샘플은 32%; 악성 스킬이 AWS 자격증명 파일을 읽어 외부로 전송; 권고 — 노출된 "API keys, cloud credentials, or financial credentials" 회전 — [Snyk ToxicSkills, 2026-02](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)
- 스킬은 에이전트 권한 그대로 실행: "they run with the agent's full permissions" — [Snyk ToxicSkills, 2026-02](https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/)
- Anthropic: 덜 신뢰되는 스킬은 "bundled files, dependencies, and any instructions to connect to external networks" 감사 — [Anthropic Engineering, 2025-10-16](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)
- 사양의 `compatibility` 필드는 "network access needs" 같은 환경 요구를 선언하는 용도이지 자격증명 저장 용도가 아님 — [agentskills.io/specification](https://agentskills.io/specification)
- agentskills.io 가이드는 입력을 "command-line flags, environment variables, or stdin"으로 받으라고 하며, 이 중 환경변수가 비밀정보 전달 경로로 제시된 유일한 방식 — [agentskills.io using-scripts](https://agentskills.io/skill-creation/using-scripts.md)

### Inferences
- 크로스툴 스킬의 스크립트 호출 문구는 `python3 scripts/foo.py --flag`(또는 `uv run scripts/foo.py`) 형태의 상대경로로 통일하고, Claude Code에서 무프롬프트 실행이 꼭 필요하면 `allowed-tools: Bash(python3 *)`처럼 `${CLAUDE_SKILL_DIR}` 없이도 매칭되는 패턴을 쓰는 것이 두 툴 모두에 안전하다(Codex는 이 필드를 무시해도 사양상 허용 필드라 에러가 없다).
- Kiwoom appkey/secret·Telegram 토큰은 `.env`/OS 키체인 등 프로젝트 설정에 두고, 스킬 본문에는 "환경변수 `KIWOOM_APP_KEY`를 읽는다. 값이 없으면 종료 코드 2로 안내 메시지 출력" 수준만 적는다. 스킬 디렉터리를 Git에 커밋·공유하는 전제이므로 `assets/`에 실데이터·토큰이 섞이지 않도록 `.gitignore`와 `skills-ref validate`/`claude plugin validate`를 CI에 넣는 것이 타당하다.
- 스크립트는 Codex 샌드박스(네트워크 제한 가능)와 Claude API(네트워크 없음) 양쪽을 고려해 네트워크가 필요한 단계(실제 API 호출)와 순수 로컬 처리(검증·리포트 생성)를 분리하면 재사용성이 높아진다.

### Gaps
- Codex 샌드박스에서 `uv run`(패키지 다운로드 필요)이 기본 허용되는지, 승인 프롬프트가 어떻게 뜨는지는 공식 문서에 없어 실측이 필요하다.
- 사양·양사 문서 모두 "비밀정보를 스킬에 넣지 말라"는 명문 조항은 없고, 근거는 보안 보고서와 일반 원칙에서 추론한 것이다.

---

## 부록: 두 툴 호환 스킬의 최소 템플릿(위 출처들로부터 조합)

```
<repo>/.agents/skills/kiwoom-paper-order/
├── SKILL.md
├── references/
│   └── kiwoom-rest-errors.md
├── scripts/
│   └── validate_order.py      # PEP 723 헤더, --help, --dry-run, JSON stdout
└── evals/
    └── evals.json
<repo>/.claude/skills -> ../.agents/skills   (symlink)
```

```yaml
---
name: kiwoom-paper-order            # 디렉터리명과 일치(사양)
description: Validate and submit Kiwoom Securities REST API paper-trading orders (모의투자 주문) and check balances/fills (잔고, 체결). Use when the user asks to place, check, or cancel a Kiwoom order, mentions 키움, 모의투자, 주문, or Telegram bot order commands. Do not use for live-account trading or for DART filing analysis.
license: MIT
compatibility: Requires Python 3.11+ and uv; needs KIWOOM_APP_KEY/KIWOOM_APP_SECRET env vars and network access to the Kiwoom mock server
metadata:
  author: tr-oh
  version: "0.1"
---
```
근거: 필드 집합은 agentskills.io/specification의 6개 필드로 한정(Claude Code 밖 하드 에러 회피, Codex는 name/description만 문서화); 경로는 Codex `.agents/skills` + Claude Code 심링크 지원; description은 3인칭·명령형·키워드·부정 범위(optimizing-descriptions, platform.claude.com best-practices); `compatibility`는 환경 요구 선언용(사양).
