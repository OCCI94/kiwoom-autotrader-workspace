# 자동매매 에이전트 스킬·MCP·툴킷 기존 아티팩트 카탈로그 (2026-11-03 기준)

조사 범위: 키움 REST API(mockapi.kiwoom.com 모의투자) 기반 Python 자동매매 봇(Telegram 제어, SQLite 원장, 13개 실험 arm, 야간 Telegram 채널 메시지 분류 파이프라인)에 "오늘 설치하거나 개조해 쓸 수 있는" 공개 아티팩트. 개발 도구는 OpenAI Codex CLI + Claude Code이므로 Agent Skills 표준(agentskills.io) SKILL.md 형식 호환 여부를 우선 평가했다. 규칙: LLM은 야간 분류·리서치에만 사용, 실시간 주문 판단에는 사용하지 않음.

검증 방법 메모:
- stars / 최근 push / license / 생성일은 2026-11-03에 GitHub REST API(`/repos/{owner}/{repo}`)로 직접 조회한 값. API 한도 초과 후 조회한 일부 저장소는 github.com HTML의 `stargazerCount`와 `commits.atom` 피드의 최신 커밋일로 대체했다(표에 "atom"으로 표시).
- "비활성" 플래그 기준: 2026-05-03 이후 push 없음(약 6개월).
- README 내용은 WebFetch로 요약 확인. 도구 이름·환경변수명은 README/SETUP 문서에 적힌 그대로 인용.
- GitHub 코드 검색(`filename:SKILL.md 키움` 등)은 인증 토큰이 없어 실행하지 못했다. 따라서 "한국 키워드가 들어간 SKILL.md"는 웹 검색으로 발견된 것만 수록했다(Gaps 참조).

---

## KQ1. 금융 / 퀀트 / 트레이딩 스킬 (백테스트·통계·리스크·포트폴리오·재무제표·레짐·시계열·SQLite/pandas·차트)

### Takeaway
2026-11 현재 SKILL.md 형식의 트레이딩 스킬 모음은 많지만(최대 501개 묶음까지), 거의 전부 미국 주식·크립토 중심이며 한국 시장이나 키움을 다루는 것은 없다. 이 프로젝트에 실제로 가치 있는 것은 (a) tradermonty의 edge 리서치 파이프라인·drawdown/pre-trade 게이트·residual-edge 통계 스킬, (b) HimanshuJ16의 운영·리스크·백테스트 정합성 스킬(단, 단일 저자·검증 부족), (c) K-Dense의 통계(statsmodels/pymc/aeon) 스킬, (d) wshobson의 quantitative-trading 플러그인 정도이며, 전부 "지시형 + 참고 스크립트"라서 키움 봇에 맞게 재작성이 필요하다.

### Cited Findings

| 아티팩트 | URL | 내용 | 성숙도/신뢰 신호 | 실행 vs 지시 / 비밀 | 적합도 |
|---|---|---|---|---|---|
| tradermonty/claude-trading-skills | https://github.com/tradermonty/claude-trading-skills | 74개 스킬 디렉터리. 관련: backtest-expert, drawdown-circuit-breaker, position-sizer, pre-trade-discipline-gate, residual-edge-analyzer(HAC 회귀로 baseline exposure vs residual edge 분리), signal-postmortem, trade-hypothesis-ideator, edge-candidate-agent → edge-strategy-designer → edge-strategy-reviewer(과적합·체결 현실성 검토) → edge-pipeline-orchestrator, weekly-performance-digest, data-quality-checker, dual-axis-skill-reviewer, skill-integration-tester, trader-memory-core | stars 3,570 / forks 782 / 최근 push 2026-11-03 / MIT / 2025-10 생성. `.secrets.baseline`+pre-commit 비밀 스캔, 릴리스 SHA256SUMS, "Execution Safety" 채점 항목 | SKILL.md + 참고자료 + helper 스크립트(`local_calculation` 태그 다수). FMP_API_KEY(무료 250req/일), FINVIZ_API_KEY(유료), ALPACA_API_KEY/SECRET/ALPACA_PAPER. 5개 스킬 스타터 경로는 유료 API 불필요 | 중간~높음(로드맵 4단계 "가설 파이프라인"의 구조 참고용). 단 "완전 자동매매용으로 설계되지 않음, 주문 안 냄, 인간 결정 게이트 중심" 명시. Codex/agentskills 언급 없음, 한국 시장 없음 |
| HimanshuJ16/Algo-Trading-Skills | https://github.com/HimanshuJ16/Algo-Trading-Skills | 501개 스킬, 16개 도메인(risk 39, backtesting 31, broker integration 36, deployment-ops 30, portfolio multi-strategy 28 등). 관련 스킬명(디렉터리 실측): kill-switch-and-drawdown-circuit-breakers, strategy-level-kill-switch-vs-portfolio-level-kill-switch, order-placement-idempotency, paper-to-live-promotion-checklist, backtest-determinism-and-reproducibility, backtest-vs-live-performance-divergence-tracking, factor-research-multiple-testing-correction, walk-forward-validation-setup, monte-carlo-strategy-robustness-testing, regime-detection-for-strategy-switching, capital-reallocation-based-on-live-performance, multi-strategy-capital-allocation-limits, risk-parity-allocation-across-strategies, risk-limit-calibration-against-historical-drawdowns, backtest-database-schema-for-point-in-time-queries, runbook-automation-for-common-incident-types, structured-logging-for-post-incident-forensics, secrets-rotation-without-bot-downtime, canary-releases-for-strategy-code-changes, automated-rollback-triggers-on-anomaly-detection, token-lifecycle-live-probing, websocket-subscription-reconciliation-after-reconnect, api-key-least-privilege-audit-tool, dependency-pinning-and-reproducible-builds | stars 50 / forks 7 / 2026-07-24 생성 / 최근 push 2026-09-14 / Apache-2.0. 각 스킬에 `scripts/` Python 참조 구현 + unittest(총 20,291 테스트, CI Python 3.10/3.12/3.13), 466/501이 stdlib만 사용. 458/501 standards.md가 1차 출처 인용 주장. "Not affiliated with Anthropic or any broker" | 참조 스크립트 실행 가능. 설치 `/plugin marketplace add HimanshuJ16/Algo-Trading-Skills` 또는 `npx skills add HimanshuJ16/Algo-Trading-Skills`; Codex는 AGENTS.md 경유라고만 기술 | 중간(아이디어·체크리스트 소스로 유용). 한국 증권사 없음(Fyers/Zerodha/Upstox/Alpaca/IBKR/Coinbase). 단일 저자, 3.5개월 된 저장소, 50 stars — 내용 품질은 스킬별로 직접 검토 필요 |
| wshobson/agents — quantitative-trading 플러그인 | https://github.com/wshobson/agents/tree/main/plugins/quantitative-trading | skills: backtesting-frameworks(look-ahead/survivorship bias, 거래비용), risk-metrics-calculation; agents: quant-analyst, risk-manager. `.claude-plugin`과 `.codex-plugin` 디렉터리 동시 존재 | 저장소 전체 stars 42,139 / 최근 push 2026-11-02 / MIT. "Multi-harness plugin marketplace for Claude Code, Codex, Cursor, OpenCode, GitHub Copilot, Google Antigravity" | 지시형(스크립트 유무는 미확인) | 중간(범용 백테스트 위생 체크리스트). 한국 시장 없음 |
| agiprolabs/claude-trading-skills | https://github.com/agiprolabs/claude-trading-skills | 68개. Backtesting: vectorbt, backtrader, strategy-framework, walk-forward-validation. Portfolio/Risk: portfolio-analytics, position-sizing, risk-management, kelly-criterion. Quant: options-pricing, fixed-income. 나머지는 Solana/DeFi API·DEX 실행 | stars 508 / forks 101 / 최근 push 2026-09-03 / MIT. "Crypto/DeFi-first" 명시, 주식 스킬 없음 | SKILL.md + 코드 스니펫 + references; 기여 가이드가 스크립트의 `--demo` 모드와 docstring의 필수 env var 명시를 요구. 설치 `/plugin marketplace add agiprolabs/claude-trading-skills`; Codex 경로 `~/.codex/skills/`, `.codex/skills/` 명시 | 낮음~중간(vectorbt/backtrader/walk-forward 4개만 발췌해 쓸 만함). 실행 스킬은 실제 시장과 상호작용 가능 → 설치 시 제외 권장 |
| himself65/finance-skills | https://github.com/himself65/finance-skills | Market Analysis 11개(company-valuation DCF/상대가치, earnings preview/recap, analyst estimates, options payoff, stock correlation, liquidity, yfinance-data), Social Readers 6개(Telegram 리더 포함, `tdl` 사용), Data Providers 6개 | stars 3,498 / forks 394 / 최근 push 2026-10-25 / MIT. Codex 마켓플레이스 설치 지원 명시(단 `npx skills` 경로는 번들 MCP 서버 생략) | 번들 MCP 서버·opencli·tdl 등 실행 컴포넌트 포함 | 낮음(미국·중국 소스 중심, 한국 없음). Telegram 리더는 채널 수집 참고 |
| K-Dense-AI/scientific-agent-skills (구 claude-scientific-skills) | https://github.com/K-Dense-AI/scientific-agent-skills | 177개 과학 스킬. 통계 관련: statsmodels, pymc(Bayesian), aeon/TimesFM(시계열), scientific-visualization, Polars/Dask/Vaex, Experimental Design, Statistical Power, EDA | stars 55,932(HTML) / 최근 커밋 2026-11-02(atom) / MIT(스킬별 license 필드 별도). SECURITY.md, scan_skills.py 존재. "Skills can execute code... Review what you install" 경고 | 스크립트 실행 가능. `npx skills add K-Dense-AI/scientific-agent-skills`, `gh skill install ... --agent claude-code`, Codex `codex plugins install .` | 중간(통계·시계열·시각화 작업의 레퍼런스). bootstrap·SQLite·pandas 전용 스킬은 README에 없음 |
| agent-skills-hub quant-analyst | https://github.com/agent-skills-hub/agent-skills-hub/blob/main/skills/quant-analyst/SKILL.md | 백테스트·VaR/Sharpe·포트폴리오 최적화 일반 스킬 | stars 127 / 최근 push 2026-10-02 / MIT | 지시형 | 낮음(일반론) |
| shakeebshaan/claude-code-quant-skills | https://github.com/shakeebshaan/claude-code-quant-skills | `/backtest-review`: look-ahead·survivorship·overfitting 감사 | stars 2 / 최근 push 2026-04-17 → 비활성(6.5개월) / MIT | 지시형 + 훅 | 낮음 |
| marian2js/trading-skills | https://github.com/marian2js/trading-skills | pre-trade check, position sizing, portfolio risk review, post-trade debrief | stars 25 / 최근 push 2026-03-16 → 비활성(7.5개월) / MIT | 지시형 | 낮음 |
| sickn33/antigravity-awesome-skills (quant-analyst, telegram-bot-builder 포함) | https://github.com/sickn33/antigravity-awesome-skills | 2,884+ 스킬 대형 집합 | stars 49,080(HTML) / 최근 커밋 2026-11-02(atom) / MIT | 혼합 | 낮음(대형 집합이라 품질 편차 큼; 개별 검토 필수) |
| joinQuantish/skills | https://github.com/joinQuantish/skills | 예측시장 트레이딩 스킬 | stars 15 / 최근 커밋 2026-03-29(atom) → 비활성 / MIT | — | 무관 |
| OpenBB (Open Data Platform + openbb-mcp-server) | https://github.com/OpenBB-finance/OpenBB | 금융 데이터 플랫폼, MCP 서버 래퍼; yfinance·FRED 무료 프로바이더 | stars 75,046(HTML) / 최근 커밋 2026-11-02(atom). OpenBB 블로그: 2026-08-25 전체 제품군을 permissive 라이선스로 오픈소스화 발표 — https://openbb.co/blog/openbb-the-interface-that-makes-mcp-work-for-financial-workflows/ | 실행(MCP 서버) | 낮음(KRX 프로바이더 없음, 미국 중심) |
| financial-datasets/mcp-server | https://github.com/financial-datasets/mcp-server | 미국 재무제표·주가·뉴스 MCP(유료 키) | stars 2,317(HTML) / 최근 커밋 2025-06-05(atom) → 비활성(17개월) / MIT | 실행 | 무관 |
| alpacahq/alpaca-mcp-server | https://github.com/alpacahq/alpaca-mcp-server | Alpaca 공식 MCP(미국 주식·옵션·크립토 주문 포함) | stars 1,055(HTML) / 최근 커밋 2026-10-26 / MIT | 실행(주문 가능) | 무관(참고: 공식 브로커 MCP의 주문 게이트 설계 비교용) |
| polygon-io/mcp_polygon (현 massive-com/mcp_massive) | https://github.com/polygon-io/mcp_polygon | 미국 시장 데이터 MCP | stars 400 / 최근 커밋 2026-10-08 / MIT | 실행 | 무관 |

추가 인용:
- "Top 8 Claude Skills for Finance and Quantitative Developers"(Snyk) — K-Dense 과학 스킬을 데이터 분석·시계열·통계 모델링 용도로 추천 — https://snyk.io/articles/top-claude-skills-finance-quantitative-developers/
- tradermonty 저장소는 "educational, research, and process-improvement purposes only", "not designed for fully automated trading", 도구가 주문을 내지 않음을 명시 — https://github.com/tradermonty/claude-trading-skills
- HimanshuJ16는 "Engineering Guidance, Not Financial, Legal, or Compliance Advice", "a missing claim is better than a wrong or fabricated one", 각 스킬에 "When NOT to Use" 섹션 — https://github.com/HimanshuJ16/Algo-Trading-Skills

### Inferences
- 백테스트 위생(look-ahead, survivorship, walk-forward, 거래비용) 체크리스트는 wshobson·agiprolabs·HimanshuJ16·tradermonty에 중복 존재하므로, 하나를 고르기보다 "키움 KRX 특성(호가단위, 상하한가, 거래세·수수료, 동시호가, 장 운영시간, 휴장일)"을 덧붙인 자체 SKILL.md로 통합하는 편이 낫다.
- 로드맵 1단계(arm당 ~200 트레이드 + sealed holdout 기반 edge 판정)에 직접 대응하는 스킬은 없다. 가장 가까운 조각은 HimanshuJ16의 factor-research-multiple-testing-correction / backtest-vs-live-performance-divergence-tracking / monte-carlo-strategy-robustness-testing, tradermonty의 residual-edge-analyzer, K-Dense의 statsmodels·Statistical Power이다.
- 로드맵 3단계(자동 배분·낙폭 한도)는 HimanshuJ16의 capital-reallocation-based-on-live-performance, multi-strategy-capital-allocation-limits, kill-switch-and-drawdown-circuit-breakers, tradermonty의 drawdown-circuit-breaker가 참고 가능하나 모두 지시형 일반론이고 키움 계좌·SQLite 원장과 결합된 구현은 없다.
- 대형 집합(sickn33 2,884개, HimanshuJ16 501개)은 양에 비해 stars가 낮거나(50) 단일 저자라서 AI 생성 비중이 높을 가능성이 있다(추정). 설치 시 필요한 스킬만 복사하고 전체 마켓플레이스 등록은 피하는 것이 안전하다.

### Gaps
- GitHub 코드 검색(`vectorbt filename:SKILL.md`, `drawdown filename:SKILL.md` 등)을 인증 문제로 실행하지 못해, 위 목록 외 소규모 개인 스킬은 누락됐을 수 있다.
- wshobson quantitative-trading 플러그인의 skills에 스크립트가 포함되는지, HimanshuJ16 각 스킬의 실제 품질(참조 구현이 실무적으로 맞는지)은 개별 파일을 열어보지 못해 확인하지 못했다.
- K-Dense에 bootstrap·SQLite·pandas 전용 스킬이 있는지는 README 요약(10만 자 중 앞부분)만으로는 확인되지 않았다.

---

## KQ2. 한국 시장 특화 아티팩트 (키움 REST, KIS, pykrx, OpenDART, KRX, 네이버/뉴스, Telegram 봇)

### Takeaway
키움증권이 2025-08에 공식 GitHub(샘플 362개, Postman, MCP 서버 2종, CLI `kwcli`)를 열었고 Claude Code·Codex 설치 절차와 "주문은 `KIWOOM_MCP_ALLOW_ORDERS="1"`로만 활성화, 기본 `KIWOOM_MODE=demo`" 같은 안전 설계가 문서화돼 있어 1차 선택지다. 커뮤니티에서는 `kiwoom-client`(pip, mockapi.kiwoom.com 지원, MCP 모드)가 가장 완성도 높은 Python 래퍼이나 stars 2·단일 저자이고, 읽기 전용 MCP `ChunSam/kiwoom-mcp-server`는 2026-11-02자로 유지보수 중단을 선언했다. DART는 `chrisryugj/korean-dart-mcp`(119 stars, 활발)와 `NomaDamas/k-skill`의 `k-dart` 스킬이 있고, KRX 시세는 pykrx 본체(1,109 stars, 활발)와 비활성 상태의 pykrx-mcp가 있다. 한국어 Telegram 봇 전용 스킬은 발견되지 않았다.

### Cited Findings

#### 2-1. 키움증권 REST API

| 아티팩트 | URL | 내용 | 성숙도/신뢰 신호 | 실행 vs 지시 / 비밀 | 적합도 |
|---|---|---|---|---|---|
| Kiwoom-Securities/Kiwoom-REST-API (공식) | https://github.com/Kiwoom-Securities/Kiwoom-REST-API | `kiwoom/`(OAuth·REST·WebSocket 런타임 + 337개 API 스펙), `examples/` 362개 Python 샘플(OAuth 2, 국내 226, 해외 134), `postman/`(306개 API, PRD/MOCK 변형), `mcp_exec/`, `mcp_spec/`, `mcpb/`(Claude Desktop 번들), SETUP-CLI.md, SETUP-MCP.md, .env.example, pyproject.toml, uv.lock | stars 74 / forks 23 / 2025-08-26 생성 / 최근 push 2026-09-15 / LICENSE.md 있으나 SPDX 미식별(NOASSERTION). Python 3.13+, uv | MCP 실행형. 기본은 클라이언트 설정 파일에 APP_KEY/APP_SECRET 평문 기입; Claude Code는 `${APP_KEY}` env 치환, Codex는 `env_vars` 대안. "에이전트가 키를 채워 넣지 말 것", "툴 실행 자동 승인 금지" 명시 | 매우 높음(공식). 단 MCP는 로컬 stdio 전용, 첫 `uv sync`가 ~140MB |
| └ SETUP-MCP.md 세부 | https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/SETUP-MCP.md | kiwoom-spec 도구: `spec_search`, `spec_show`, `spec_groups`, `get_example`(키 불필요). kiwoom-exec 기본 도구: `kiwoom_commands`, `kiwoom_help`, `kiwoom_query`; 주문 게이트 `KIWOOM_MCP_ALLOW_ORDERS`가 정확히 `"1"`일 때만 `kiwoom_order_preview`, `kiwoom_order_submit` 추가(`true`/`yes`는 무효). `KIWOOM_MODE`는 `demo`(모의투자, 기본) 또는 `real`; 키 불일치 시 8001 오류. Claude Code: `claude mcp add kiwoom-exec -s user -e KIWOOM_MODE=demo -e APP_KEY=... -e APP_SECRET=... -- <UV_PATH> run --frozen --directory <REPO>/mcp_exec kiwoom-exec-mcp`; Codex: `~/.codex/config.toml`에 `[mcp_servers.kiwoom_spec]`, `[mcp_servers.kiwoom_exec]`, `[mcp_servers.kiwoom_exec.env]` 추가 후 `codex mcp list`. 검증용 `verify_tools_list.py`(stdlib) 제공 | 상동 | 상동 | 상동. `KIWOOM_MCP_DEBUG_HEADERS`도 추가 도구를 노출하는 게이트로 언급됨 |
| └ kwcli (공식 CLI, PyPI) | https://pypi.org/project/kwcli/ | `uv tool install kwcli` → 명령 `kiwoomcli`. `kiwoomcli setup`이 App Key/Secret을 OS 자격증명 저장소(macOS Keychain 등)에 저장, `.env`는 폴백. 주문 명령은 `--confirm` 필요. 조건검색식 생성·수정은 영웅문 HTS에서만, CLI는 조회·구독. `spec search`로 API 탐색 | 최신 파일 0.1.1(검색 기준). 주의: `kiwoom-cli`(하이픈)는 별개의 비공식 패키지 | 실행형, 키체인 저장 | 높음(운영 중 수동 점검·헬스체크 스크립트의 기반으로 적합) |
| younghwan91/kiwoom-client | https://github.com/younghwan91/kiwoom-client | `pip install kiwoom-client`. REST 182 엔드포인트 + 조건검색 4 + 실시간 WS 19종, `KiwoomAPI`/`AsyncKiwoomAPI`, 토큰 자동 발급·만료 전 갱신·401 시 재발급, `is_mock=True`(REST `mockapi.kiwoom.com`, WS `wss://mockapi.kiwoom.com:10000`; 모의는 KRX만). MCP 모드 `kiwoom-client-mcp`: 15개 모듈 전부 + 조건검색 4종 노출, 실계좌 주문 도구는 `KIWOOM_MCP_ALLOW_LIVE_ORDERS=true`일 때만 등록(모의 주문은 항상 가능; 게이트는 MCP 경로에만 적용) | stars 2 / forks 3 / 2026-09-01 생성 / 최근 push 2026-10-18 / MIT / Python 3.10+. tests/ + CI 배지, WS 실시간 REAL 프레임 필드명 "장 마감 중 테스트라 미확인" 명시 | 실행형. env `KIWOOM_APP_KEY`, `KIWOOM_APP_SECRET`, `KIWOOM_IS_MOCK`, `.env.example` | 높음(현재 봇의 수동 requests 래퍼를 대체하거나 비교 검증용). 단 신생·단일 저자·stars 2 → 코드 리뷰 후 핀 고정 필요 |
| ChunSam/kiwoom-mcp-server | https://github.com/ChunSam/kiwoom-mcp-server | 읽기 전용 MCP(시세·차트·순위·수급·공매도·ETF·관심종목·테마·계좌 잔고/체결/미체결·거래내역·ISA 세금계산기 `calc_isa_tax_status`). 주문·정정·취소 설계상 제외. `KIWOOM_MODE` 기본 `VIRTUAL`, `REAL` 선택. stdio 기본, `--http`(`/mcp`, `/healthz`, 127.0.0.1 바인딩)는 `MCP_AUTH_TOKEN` 필수(없으면 기동 거부, `--no-auth`로만 우회), claude.ai용 OAuth 2.0 PKCE. `npx -y kiwoom-mcp-server`, Node 22+ | stars 1 / 최근 push 2026-11-02 / MIT. README에 "2026-11-02부로 유지보수 종료, 최종 v0.52.6, 키움 API 변경 시 깨질 수 있음" 공지 | 실행형(읽기 전용). "REAL 모드는 등록 IP에 묶여 미등록 호스트(클라우드 VM)에서 실패 가능" | 중간(읽기 전용 설계·인증 토큰 강제·헬스체크 엔드포인트는 참고 가치 높음) — 그러나 유지보수 종료라 포크 전제 |
| java-jaydev/kiwoom-mcp | https://github.com/java-jaydev/kiwoom-mcp | 조회 전용 MCP(주문 없음) | stars 4 / 2026-03-06 생성·push → 비활성(8개월) / MIT | 실행 | 낮음 |
| KKimj/Kiwoom-REST-API | https://github.com/KKimj/Kiwoom-REST-API | OpenAPI 3.1.0 명세 + MCP(npm `kiwoom-rest-api`), TypeScript·Dart | stars 0 / 최근 push 2026-06-19 / 라이선스 미식별 | 실행 | 낮음(OpenAPI 명세 파일은 코드 생성·검증에 참고 가능) |
| ljy2855/agent-trader | https://github.com/ljy2855/agent-trader | 키움 REST 기반 개인 MCP(계좌·시세) | stars 1 / 최근 push 2026-09-28 / 라이선스 없음 | 실행 | 낮음 |
| suapapa/go_kioom | https://github.com/suapapa/go_kioom | Go 래퍼 + MCP | stars 0 / 최근 push 2026-10-28 / MIT | 실행 | 낮음(언어 불일치) |

교육 자료(코드 아님): 인프런 "Claude Code로 키움 API 자동매매 시스템 구축"(MCP·Agents·Skills 활용) — https://www.inflearn.com/en/course/ai-stock-auto-tradin ; 크몽 전자책(키움 REST API, requests만 사용) — https://kmong.com/gig/764435 ; 패스트캠퍼스 — https://fastcampus.co.kr/fin_online_aistockagent . 검색에서 velog/tistory의 실운영 후기나 키움 전용 SKILL.md 파일은 발견되지 않았다(검색 결과 요약 — https://www.gpters.org/data-science/post/claudes-perfect-summary-stock-M9dB8NNLr6PtPSb 는 KIS 기준 프롬프트 예시).

#### 2-2. 한국투자증권(KIS) 및 기타 증권사

| 아티팩트 | URL | 내용 | 성숙도/신뢰 신호 | 실행 vs 지시 / 비밀 | 적합도 |
|---|---|---|---|---|---|
| koreainvestment/open-trading-api (공식) | https://github.com/koreainvestment/open-trading-api | `MCP/` 하위 "KIS Code Assistant MCP"(API 자연어 검색·샘플코드, `npx -y @koreainvestment/kis-code-assistant-mcp`, HTTP 8081 `/mcp`, `/health`)와 "Kis Trading MCP"(국내·해외 시세, 잔고·체결·손익, 현물·신용·선물옵션 주문/정정/취소, 순위·투자자 동향). Trading MCP는 Docker+SSE(`MCP_ACCESS_TOKEN` Bearer, `http://localhost:3000/sse`) 또는 stdio(`uv ... run python server.py`, `KIS_APP_KEY`/`KIS_APP_SECRET`, 예시 `"ENV": "live"`). 저장소에 `backtester`, `strategy_builder`, `examples_llm`, `llms.txt`, `kis_devlp.yaml`(모의 키·계좌 필드 분리, `auth(svr="vps")`) | stars 1,655 / forks 837 / 최근 push 2026-10-27 / 라이선스 파일 없음. Python 3.11+(Trading), 3.12+·Node 18+(Code Assistant) | 실행형(주문 포함). 연결 가이드에 주문 도구 비활성화 옵션 언급 없음; Claude Code·Codex 전용 절차 없음(Claude Desktop·Cursor만) | 낮음~중간(이 프로젝트는 키움 사용). 참고 가치: 공식 `backtester`·`strategy_builder`·`llms.txt` 구조. 보도: 2025-09 KIS가 증권업계 최초 MCP 서비스 출시 — https://www.smarttoday.co.kr/ko-kr/articles/91252 ; GeekNews 소개 — https://news.hada.io/topic?id=23084 |
| └ 연결 가이드 | https://github.com/koreainvestment/open-trading-api/blob/main/MCP/MCP%20AI%20%EB%8F%84%EA%B5%AC%20%EC%97%B0%EA%B2%B0%20%EB%B0%A9%EB%B2%95.md | 상동 | 상동 | 상동 | 상동 |
| migusdn/KIS_MCP_Server | https://github.com/migusdn/KIS_MCP_Server | REST 카탈로그 기반(목록·상세·호출 도구), `KIS_ENABLE_TRADING`으로 주문 API 기본 비활성 | stars 31 / forks 18 / 최근 push 2026-06-15(4.5개월, 경계) / MIT | 실행 | 낮음(브로커 불일치) — "주문 기본 OFF 플래그" 설계는 참고 |
| ez2sarang/kstock-mcp | https://github.com/ez2sarang/kstock-mcp | 읽기 전용: KIS 시세/OHLCV/잔고 + OpenDART 공시·재무 + IPO 일정, `uvx kstock-mcp` | stars 0 / 2026-10-18 생성 / 최근 push 2026-10-19 / MIT | 실행 | 낮음 |
| 115dkk/KIS-MCP | https://github.com/115dkk/KIS-MCP | 조회 전용, Cloudflare Workers | stars 0 / 최근 push 2026-10-07 / 라이선스 미식별 | 실행(원격) | 낮음 |
| nangchang/stock-kis-mcp | https://github.com/nangchang/stock-kis-mcp | 국내·해외 시세·잔고, Claude Code `.mcp.json` 예시 | stars 0 / 최근 push 2026-10-26 / 라이선스 없음 | 실행 | 낮음 |
| meringue5/KIS_Portfolio_MCP | https://github.com/meringue5/KIS_Portfolio_MCP | 다계좌 포트폴리오, OAuth 원격 MCP(비공식 명시) | fork / stars 0 / 최근 push 2026-11-02 / MIT | 실행 | 낮음 |
| meritz-securities/open-api-mcp (메리츠증권 공식) | https://github.com/meritz-securities/open-api-mcp | 시세·계좌·주문 MCP | stars 2(HTML) / 최근 커밋 2026-10-29(atom) / MIT | 실행(주문 포함) | 무관(브로커 비교 참고) |
| JeongSeongMok/tossinvest-openapi-mcp | https://github.com/JeongSeongMok/tossinvest-openapi-mcp | 토스증권 Open API 탐색·연동 MCP(stdio), Claude·Cursor·Codex | stars 4 / 최근 push 2026-09-28 / MIT | 실행 | 무관 |
| DBsecurities/dbsec-open-api (DB증권 공식) | https://github.com/DBsecurities/dbsec-open-api | Python 샘플·SDK | stars 17 / 최근 push 2026-08-20 | 실행 | 무관 |

#### 2-3. KRX 시세 / pykrx / FinanceDataReader

| 아티팩트 | URL | 내용 | 성숙도/신뢰 신호 | 적합도 |
|---|---|---|---|---|
| sharebook-kr/pykrx | https://github.com/sharebook-kr/pykrx | KRX 스크래핑 라이브러리 | stars 1,109 / forks 311 / 최근 push 2026-10-29 / 라이선스 필드 None(API 기준) | 높음(이미 쓰고 있을 가능성; 야간 데이터 보강·휴장일·지수 데이터) |
| sharebook-kr/pykrx-mcp | https://github.com/sharebook-kr/pykrx-mcp | 23개 조회 도구(주식 11·지수 5·공매도 4·외국인 1·ETF 2), FastMCP, `uvx pykrx-mcp`, Python 3.10+ | stars 3 / forks 5 / 2026-01-26 생성 / 최근 push 2026-02-01 → 비활성(9개월) / MIT. README에 스크래핑 한도·레이트리밋 언급 없음 | 낮음~중간(비활성; 리서치 세션용으로만) |
| hyosei75-sys/pykrx-mcp | https://github.com/hyosei75-sys/pykrx-mcp | KRX 시세 읽기 전용 원격 MCP(Claude 커스텀 커넥터) | stars 0 / 2026-10-26 생성 / 최근 push 2026-10-30 / 라이선스 없음 | 낮음 |
| FinanceData/FinanceDataReader | https://github.com/FinanceData/FinanceDataReader | KRX·해외 시세 리더 | stars 1,561 / 최근 push 2026-05-13(5.7개월, 경계) / MIT | 중간 |
| jjlabsio/korea-stock-mcp | https://github.com/jjlabsio/korea-stock-mcp | DART 4도구(`get_disclosure_list`, `get_corp_code`, `get_disclosure`, `get_financial_statement`) + KRX 3도구(`get_stock_base_info`, `get_stock_trade_info`, `get_market_type`), `npx -y korea-stock-mcp@latest`, `DART_API_KEY` + `KRX_API_KEY`(KRX는 6개 서비스 개별 신청, 승인 ~1일), Node 18+ | stars 184(HTML) / forks 42 / 최근 커밋 2026-08-04(atom) / ISC. "새 서비스 준비 중" 언급 | 중간(공식 KRX Open API 키 기반이라 스크래핑보다 안정적) |
| Mrbaeksang/korea-stock-analyzer-mcp | https://github.com/Mrbaeksang/korea-stock-analyzer-mcp | 원격 호스팅 MCP(Railway) 5도구: `search_company`, `get_quote`, `get_financials`(연간 3~10년), `get_valuation`(bear/base/bull 범위), `get_risk_flags`. DART + FinanceDataReader(일별 스냅샷). 자체 호스팅 시 `DART_API_KEY`, 레이트리밋 30req/분 | stars 25 / forks 8 / 최근 push 2026-09-30 / Apache-2.0. "추정치·하드코딩 폴백 PR 거부" 정책 | 낮음~중간(리서치용; 원격 서버 의존은 야간 파이프라인에 부적합) |
| greatSumini/naver-finance-crawl-mcp | https://github.com/greatSumini/naver-finance-crawl-mcp | 네이버 금융 크롤링 MCP(TS, Smithery) | stars 5(HTML) / 최근 커밋 2025-10-29(atom) → 비활성(12개월) / MIT | 낮음 |

#### 2-4. OpenDART / 공시

| 아티팩트 | URL | 내용 | 성숙도/신뢰 신호 | 실행 / 비밀 | 적합도 |
|---|---|---|---|---|---|
| chrisryugj/korean-dart-mcp | https://github.com/chrisryugj/korean-dart-mcp | OpenDART 83 API → 15 도구: `resolve_corp_code`, `search_disclosures`(22 프리셋, 90일 자동 분할), `get_company`, `get_financials`, `download_document`, `get_xbrl`, `get_periodic_report`(29 섹션), `get_shareholders`, `get_executive_compensation`, `get_major_holdings`, `get_corporate_event`(36 유형), `insider_signal`, `disclosure_anomaly`(0~100), `buffett_quality_snapshot`, `get_attachments`(HWP/HWPX/PDF/DOCX/XLSX → markdown, kordoc 엔진). `npx -y korean-dart-mcp setup`, Claude Code `/plugin marketplace add chrisryugj/korean-dart-mcp` → `/plugin install korean-dart`. Node 20.19+ | stars 119 / forks 21 / 2026-04-17 생성 / 최근 push 2026-10-26 / MIT. v0.9.1에서 ZIP slip/bomb 방어, HTTPS-only 스크래핑, 청크 상한, 재귀 깊이 가드 | 실행형. `DART_API_KEY`(무료, ~20,000req/일)를 클라이언트 설정에 평문 저장 또는 `.env`. 첫 실행 시 corp_code 덤프(~116k)를 `~/.korean-dart-mcp/corp_code.sqlite`에 캐시(24h TTL). Codex 미언급 | 높음(야간 리서치·공시 요약 파이프라인용). 공시 본문은 신뢰할 수 없는 입력이므로 LLM에 넘길 때 데이터로만 취급 필요(README는 prompt injection 미언급) |
| NomaDamas/k-skill `k-dart` 등 | https://github.com/NomaDamas/k-skill | 한국인용 스킬 모음. 금융: `korean-stock-search`(KRX 종목·일별 시세), `korean-bond-search`, `k-dart`(14 엔드포인트), `company-analysis`, `toss-investment`(읽기 전용 OAuth2), `daishin-report-search`, `kosis-stats`, `bok-ecos-stats`(기준금리·환율·CPI), `government-bond-analysis`, `multi-asset-morning-briefing`, `market-event-impact`; 뉴스: `geeknews-search`, `naver-news-search`, `hankookilbo-news`; `korean-holiday-calendar`(천문연 휴일). 설치 `npx --yes skills add NomaDamas/k-skill --all -g` 또는 `--skill <name>`, Claude Code `/plugin marketplace add NomaDamas/k-skill`, 설치 후 `k-skill-setup`으로 자격증명 설정 | stars 8,251(HTML) / 최근 커밋 2026-11-02(atom) / MIT(프록시 디렉터리 `packages/k-skill-proxy/`는 AGPL-3.0-only). Codex·OpenCode·OpenClaw 지원 명시 | SKILL.md + 스크립트. 일부 스킬은 공공데이터포털 API를 "프록시 경유"로 호출 | 중간~높음(리서치·휴장일·거시지표). Telegram 스킬 없음. 프록시 경유 호출은 요청 내용이 제3자 서버를 지나므로 야간 파이프라인에 넣기 전 어떤 스킬이 프록시를 쓰는지 확인 필요 |
| procpalee/OpenDART-MCP-Server | https://github.com/procpalee/OpenDART-MCP-Server | OpenDART 83 API, Vercel 배포형 | stars 3 / forks 50 / 최근 push 2026-10-01 / 라이선스 없음. skybull76·banabada·ssanae85-beep·nam00914-lab·lbm-cloud 등 다수가 동일 설명의 포크 | 실행(원격) | 낮음(라이선스 없음, 포크 난립) |
| josw123/dart-fss | https://github.com/josw123/dart-fss | DART 크롤링 Python 라이브러리 | stars 382 / 최근 push 2026-10-28 / MIT | 라이브러리 | 중간(Python 파이프라인에 직접 삽입 가능) |
| RealYoungk/opendart-mcp | https://github.com/RealYoungk/opendart-mcp | OpenDART MCP | stars 3 / 최근 push 2026-02-20 → 비활성 / MIT | 실행 | 낮음 |
| Dayoooun/dart-mcp | https://github.com/Dayoooun/dart-mcp | OpenDART MCP | stars 4(HTML) / 최근 커밋 2026-01-09(atom) → 비활성 / MIT | 실행 | 낮음 |
| chexteam1-svg/opendart-mcp | https://github.com/chexteam1-svg/opendart-mcp | 80개 API | stars 0 / 2026-10-30 생성 / 라이선스 없음 | 실행 | 낮음 |

#### 2-5. 한국 뉴스 / Telegram

| 아티팩트 | URL | 내용 | 성숙도/신뢰 신호 | 적합도 |
|---|---|---|---|---|
| uju777/mcp-server-naver-search | https://github.com/uju777/mcp-server-naver-search | 네이버 검색 API(뉴스·쇼핑·카페) MCP, 네이버 개발자 키 필요 | stars 1(HTML) / 최근 커밋 2026-01-12(atom) → 비활성 / MIT | 낮음 |
| SongT-50/korean-news-mcp | https://github.com/songt-50/korean-news-mcp | 한국 뉴스 + 글로벌 테크 뉴스 MCP(키 불필요) | stars 1 / 최근 커밋 2026-03-08(atom) → 비활성(8개월) / MIT | 낮음 |
| greeun/notify-telegram | https://github.com/greeun/notify-telegram | Claude Code 작업 완료·입력 대기·권한 요청 시 Telegram 알림 스킬. `~/.claude/skills/telegram-notifier/`에 SKILL.md + `scripts/send_telegram.py` + references/setup-guide.md, hooks의 Notification에 등록, `TELEGRAM_CHAT_ID` 필수 | stars 1(HTML) / 최근 커밋 2026-09-23(atom) / 라이선스 미확인 | 낮음~중간(개발 세션 알림용; 봇 운영 알림과는 별개) |
| daedalus/mcp-telegram-bot | https://github.com/daedalus/mcp-telegram-bot | Python MCP(PyPI `mcp-telegram-bot`): `send_message`, `get_me`, `get_updates`; 저장소에 SKILL.md 포함 | stars 1 / 최근 push 2026-04-21 → 비활성(6.5개월) / MIT | 낮음 |
| qrak/telegram-commandcode | https://github.com/qrak/telegram-commandcode | 코딩 에이전트에서 메시지·파일·사진 전송 MCP, python-telegram-bot 20~21 데몬, 공개 가능한 skill 폴더 | stars 3 / 최근 push 2026-10-26 / MIT | 낮음~중간 |
| Eweol/telegram-bot-mcp | https://github.com/Eweol/telegram-bot-mcp | FastMCP, Bot API 도구(메시지·사진·투표), stdio/streamable HTTP | stars 0 / 최근 push 2026-11-01 / MIT | 낮음 |
| Telegram-mcp/telegram-mcp | https://github.com/Telegram-mcp/telegram-mcp | Telethon + MTProto 사용자 클라이언트 MCP(채널 읽기 가능) | stars 1 / 2026-09-05 생성 / 최근 push 2026-10-31 / MIT | 낮음~중간(채널 메시지 수집 방식 참고; 사용자 세션 자격증명을 MCP에 맡기는 구조라 보안상 봇 운영 경로에는 부적합) |
| anthropics/claude-plugins-official `external_plugins/telegram` | https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins | 공식 마켓플레이스 외부 플러그인 목록에 `telegram`, `discord`, `imessage` 존재(디렉터리 실측) | 마켓플레이스 stars 45,924 / 최근 push 2026-11-03 / Apache-2.0(플러그인별 상이) | 미확인(내용 열람 못함 — Gaps) |

### Inferences
- 키움 공식 저장소의 `mcp_exec` 설계(주문 게이트 env 값을 `"1"`로만 인정, 기본 demo, 클라이언트 승인 화면을 마지막 안전장치로 명시)는 이 프로젝트의 "LLM은 주문 판단에 쓰지 않는다" 규칙과 일치하므로, 개발 세션에서는 `kiwoom-spec`(키 불필요)만 등록하고 `kiwoom-exec`는 `KIWOOM_MCP_ALLOW_ORDERS` 미설정 + demo 키로만 쓰는 구성이 적절하다.
- 운영 봇 코드에서는 MCP가 아니라 라이브러리가 필요하다. 후보는 공식 `kiwoom/` 런타임(저장소 내 패키지, pip 미배포)과 `kiwoom-client`(pip, async·토큰 갱신·mock 전환 내장). 후자는 stars 2라서 포크 후 버전 핀 고정과 코드 리뷰가 전제다.
- 야간 리서치 파이프라인(공시·뉴스·거시)은 `korean-dart-mcp` + `k-skill`(k-dart, bok-ecos-stats, korean-holiday-calendar, geeknews-search)로 대부분 커버된다. 둘 다 Claude Code 플러그인/`npx skills add` 양쪽 경로가 있어 Codex와 공유 가능하다(k-skill은 Codex 지원 명시, korean-dart-mcp는 MCP라 Codex `config.toml`로 등록 가능).
- `ChunSam/kiwoom-mcp-server`의 유지보수 종료 공지는 "키움 API가 예고 없이 바뀐다"는 운영 리스크의 간접 증거다. 공식 저장소의 push(2026-09-15)와 Postman 컬렉션을 변경 감지 기준선으로 삼을 만하다.

### Gaps
- 키움 공식 LICENSE.md의 라이선스 종류, `mcp_exec`의 주문 확인 흐름 세부(preview→submit 사이의 추가 확인 여부)는 문서만으로 확인되지 않았다.
- KIS Trading MCP의 정확한 도구 이름 목록, 주문 도구 비활성화 플래그 유무는 연결 가이드에 없었다.
- `claude-plugins-official/external_plugins/telegram`의 내용(봇 전송인지 사용자 세션인지)은 열람하지 못했다.
- 한국어 Telegram 봇 전용 SKILL.md, 키움 전용 SKILL.md는 웹 검색으로는 발견되지 않았다(GitHub 코드 검색 미실행).
- velog/tistory/GeekNews의 "Claude Code 스킬로 키움 자동매매" 실운영 후기는 검색에서 찾지 못했다(강의·전자책·프롬프트 모음만 존재).

---

## KQ3. 운영 / 엔지니어링 스킬 (runbook·incident·헬스체크·배포 게이트·로그/SQLite 포렌식·pytest·launchd/cron·Telegram 알림·리포트 생성)

### Takeaway
개발 방법론 스킬(TDD·체계적 디버깅·검증)은 `obra/superpowers`가 사실상 표준이고 Claude Code 공식 마켓플레이스와 Codex 플러그인 양쪽에서 설치된다. 그러나 "트레이딩 봇 운영"에 특화된 runbook/헬스체크/배포 게이트/원장 포렌식 스킬은 없고, 가장 가까운 것은 HimanshuJ16의 deployment-ops 도메인(검증 부족)과 wshobson의 incident-response·observability-monitoring·deployment-validation 플러그인(일반 SRE)이다. launchd 헬퍼 스킬은 소규모 개인 스킬만 있고 한 감사에서 "접두사 매칭으로 남의 plist까지 제거 가능" 문제가 지적됐다.

### Cited Findings

| 아티팩트 | URL | 내용 | 성숙도/신뢰 신호 | 실행 vs 지시 | 적합도 |
|---|---|---|---|---|---|
| obra/superpowers | https://github.com/obra/superpowers | 스킬: test-driven-development(테스트 안티패턴 참고 포함), systematic-debugging(root-cause tracing·defense-in-depth·condition-based-waiting 포함), verification-before-completion, brainstorming, writing-plans, executing-plans, dispatching-parallel-agents, requesting/receiving-code-review, using-git-worktrees, finishing-a-development-branch, subagent-driven-development, writing-skills, using-superpowers, diagnosing-superpowers. `.claude-plugin`, `.codex-plugin`, `.cursor-plugin`, `.opencode` 등 멀티 하네스 디렉터리 | stars 311,559 / 최근 push 2026-11-03 / MIT. Claude Code `/plugin install superpowers@claude-plugins-official`; Codex 공식 플러그인 마켓플레이스(`/plugins`)로 설치 | 지시형 + hooks(설치 시 승인). brainstorming 비주얼 컴패니언이 외부 로고를 로드(버전만 전송, `SUPERPOWERS_DISABLE_TELEMETRY`로 차단); diagnosing-superpowers는 세션 트랜스크립트를 읽어 "scrubbed" 번들 생성 | 높음(로드맵 0단계 "운영 안정성·실험 처리량 10배"의 개발 규율용). Claude Code와 Codex 양쪽 공식 경로 존재 |
| anthropics/claude-plugins-official | https://github.com/anthropics/claude-plugins-official | 내부 플러그인(디렉터리 실측): claude-security, security-guidance, code-review, pr-review-toolkit, commit-commands, feature-dev, hookify, mcp-server-dev, plugin-dev, skill-creator, session-report, receipts, ralph-loop, project-artifact, agent-sdk-dev, code-simplifier, code-modernization, LSP 플러그인 다수. 외부: telegram, discord, github, gitlab, playwright, serena, context7, terraform, linear, asana, firebase, imessage, laravel-boost, fakechat | stars 45,924 / 최근 push 2026-11-03 / Apache-2.0(플러그인별 LICENSE 상이). README: "Anthropic은 플러그인이 포함한 MCP 서버·파일·소프트웨어를 통제하지 않으며 의도대로 동작하는지 검증할 수 없음" | 혼합 | 중간(금융·SRE 전용 플러그인은 없음; code-review·security-guidance·hookify·session-report는 범용 운영 보조) |
| anthropics/skills | https://github.com/anthropics/skills | 스킬(디렉터리 실측): docx, pdf, pptx, xlsx, skill-creator, mcp-builder, webapp-testing, web-artifacts-builder, claude-api, doc-coauthoring, frontend-design, canvas-design, theme-factory, brand-guidelines, internal-comms, academy-guide, algorithmic-art, slack-gif-creator, discernment-nudge. `/plugin marketplace add anthropics/skills` → `document-skills` / `example-skills` | stars 186,971 / 최근 push 2026-10-28. docx/pdf/pptx/xlsx는 source-available(오픈소스 아님), 나머지는 Apache-2.0 | 스크립트 실행형(문서 스킬) | 중간(xlsx/pdf 리포트 생성, skill-creator로 자체 스킬 작성, mcp-builder). Codex 언급 없음(SKILL.md 표준이라 복사 가능) |
| wshobson/agents 운영 플러그인 | https://github.com/wshobson/agents | 플러그인(디렉터리 실측): incident-response, observability-monitoring, deployment-validation, deployment-strategies, tdd-workflows, unit-testing, debugging-toolkit, error-diagnostics, data-validation-suite, signed-audit-trails, protect-mcp, security-scanning, python-development, plugin-eval, machine-learning-ops, llm-application-dev | stars 42,139 / 최근 push 2026-11-02 / MIT, `.codex-plugin` 포함 | 지시형 위주 | 중간(일반 SRE 체크리스트) |
| HimanshuJ16 deployment-ops·risk 도메인 | https://github.com/HimanshuJ16/Algo-Trading-Skills | paper-to-live-promotion-checklist, canary-releases-for-strategy-code-changes, automated-rollback-triggers-on-anomaly-detection, runbook-automation-for-common-incident-types, structured-logging-for-post-incident-forensics, secrets-rotation-without-bot-downtime, token-lifecycle-live-probing, broker-status-page-monitoring-integration, clock-drift-monitoring-alerting-thresholds, dependency-pinning-and-reproducible-builds, order-placement-idempotency, websocket-subscription-reconciliation-after-reconnect, audit-logging-for-configuration-changes, risk-control-bypass-audit-logging | stars 50 / 최근 push 2026-09-14 / Apache-2.0 | 참조 스크립트 + unittest | 중간(로드맵 2단계 소액 실계좌 전환 체크리스트 초안으로 유용; 키움·한국 규제 맥락은 없음) |
| borghei/Claude-Skills runbook-generator | https://github.com/borghei/Claude-Skills/blob/main/engineering/runbook-generator/SKILL.md | 코드베이스 분석으로 runbook 생성(배포·incident triage→diagnose→mitigate→resolve→postmortem·DB 유지보수·스케일링·모니터링) | 저장소 stars 1,036 / 최근 push 2026-10-23 / 라이선스 SPDX 미식별(NOASSERTION); "385 skills, 76 agents, 906 stdlib Python tools" | 지시형 | 낮음~중간(라이선스 확인 필요) |
| wilsto/claude-code-starter-kit incident-response | https://github.com/wilsto/claude-code-starter-kit/blob/main/.claude/skills/incident-response/SKILL.md | 5단계(triage·investigation·resolution·communication·postmortem) 워크플로, 단계별 파일 생성, runbook 템플릿 | stars 0 / 최근 push 2026-08-29 / MIT | 지시형 | 낮음 |
| Anthropic 쿡북: SRE incident responder(Managed Agents) / site reliability agent(Agent SDK) | https://platform.claude.com/cookbook/managed-agents-sre-incident-responder ; https://platform.claude.com/cookbook/claude-agent-sdk-03-the-site-reliability-agent | "인프라를 만지기 전 runbook 참조", 수정은 runbook을 인용한 PR로만; `runbook` 스킬이 `execute_runbook` 도구를 investigate/remediate 단계에서 호출 | Anthropic 공식 예제 | 코드 예제 | 중간(읽기 전용 헬스체크 에이전트의 설계 패턴 참고) |
| launchd 스킬들: `macos-launchd`(alphaonedev/openclaw-graph), `launchd-helper`(SZoloth/skills), `launchagent-builder`(0xjitsu), `loop_macos`(os-tools) | https://skills.sh/alphaonedev/openclaw-graph/macos-launchd ; https://www.skills.sh/szoloth/skills/launchd-helper ; https://sharedcontext.ai/skills/external/0xjitsu/launchagent-builder ; https://www.claudepluginhub.com/skills/alex-kopylov-os-tools-plugins-os-tools/loop_macos | `~/Library/LaunchAgents` plist 생성·검증·launchctl 제어; launchagent-builder는 로드 전 항상 확인; loop_macos는 "캘린더 스케줄이 수면 후 놓친 실행을 보충" | SZoloth/skills stars 0 / 최근 커밋 2026-02-06(atom) → 비활성; 나머지는 디렉터리 페이지만 확인(stars 미확인) | 지시형(plist 작성) | 낮음(내용이 짧아 자체 작성이 더 안전). 제3자 감사: 한 LaunchDaemon 스케줄러 스킬의 remove/stop이 사용자 접두사와 일치하는 임의의 plist에 작동 — https://clawhub.ai/skkypy/skills/launchctl/security-audit |
| tradermonty `launchd/` 디렉터리 | https://github.com/tradermonty/claude-trading-skills | 저장소 루트에 `launchd/`, `workflows/`, `state/` 디렉터리 존재(실측) — 스킬 워크플로의 macOS 스케줄링 예시로 추정 | 상동 | 미확인 | 참고 |
| pytest 스킬: KakoRel/AISkills pytest-test-writer; benchflow-ai/skillsbench testing-python | https://github.com/KakoRel/AISkills/blob/main/cursor-skills/pytest-test-writer/SKILL.md ; https://github.com/benchflow-ai/skillsbench/blob/main/tasks/fix-build-agentops/environment/skills/testing-python/SKILL.md | fixture·parametrize·mocking 체크리스트; "테스트는 원자적·자기완결적" | KakoRel: stars 0 / 2026-06-08 / 라이선스 없음(러시아어 저장소) | 지시형 | 낮음(superpowers TDD + wshobson unit-testing이 우선) |
| SQLite 포렌식: mukul975/Anthropic-Cybersecurity-Skills `performing-sqlite-database-forensics` | https://github.com/mukul975/Anthropic-Cybersecurity-Skills ; https://agentskill.sh/@mukul975/performing-sqlite-database-forensics/security | 삭제 레코드 복구(freelist·WAL), B-tree 페이지 구조, 브라우저/모바일 DB 중심. `npx skills add mukul975/Anthropic-Cybersecurity-Skills --skill performing-sqlite-database-forensics` | 저장소 stars 35,496(HTML) / 최근 커밋 2026-08-31(atom) / Apache-2.0. agentskill.sh 감사: 13건 이슈(대부분 외부 URL 참조, temp-mail·pastebin 링크 포함) — "Anthropic" 이름을 쓰지만 비공식 | 지시형 | 낮음(원장 정합성 검증이 아니라 증거 복구 목적) |
| modelcontextprotocol sqlite 서버 | https://github.com/modelcontextprotocol/servers-archived | 공식 레퍼런스 sqlite 서버는 2025-05-28 아카이브(현행 레퍼런스: everything, fetch, filesystem, git, memory, sequentialthinking, time) | servers stars 91,765; servers-archived archived=True | 실행 | 낮음(아카이브됨; SQLite 분석은 스킬 내 Python/sqlite3로 처리하는 편이 안전) |
| 리포트 생성 | https://github.com/anthropics/skills (xlsx/pdf/docx) | 위 참조 | 상동 | 실행형 | 중간(주간 다이제스트 xlsx/pdf). markdown 리포트는 전용 스킬 불필요 |

### Inferences
- Claude Code와 Codex 모두에서 같은 파일로 쓰려면 `superpowers`(양쪽 공식 마켓플레이스)와 `.agents/skills/` 규약(agentskills.io 통합 가이드가 cross-client 공유 경로로 권장 — https://agentskills.io/integrate-skills)을 기준 삼는 것이 가장 마찰이 적다.
- "읽기 전용 헬스체크" 스킬은 공개된 것이 없으나, kwcli(`kiwoomcli` 상태 조회)와 ChunSam 서버의 `/healthz`·읽기 전용 도구 설계를 합치면 자체 작성 범위가 작다.
- launchd 스킬은 작고 검증이 약하므로, plist 템플릿과 `launchctl` 명령을 자체 SKILL.md의 references/에 고정 텍스트로 넣는 편이 제3자 스킬 설치보다 안전하다.

### Gaps
- `claude-plugins-official`의 `session-report`, `receipts`, `hookify` 세부 기능은 열람하지 못했다.
- 트레이딩 원장(SQLite) 대조·재조정(브로커 체결내역 vs 로컬 ledger) 전용 스킬은 발견되지 않았다.
- launchd 스킬들의 stars·최근 갱신·라이선스는 디렉터리 페이지만 보여 대부분 미확인이다.

---

## KQ4. 리서치 파이프라인 스킬 (웹 리서치·뉴스 중복 제거·텍스트 분류/라벨링·골드 라벨 검토·실험 추적·A/B·다중 arm 평가 통계)

### Takeaway
라벨링 워크플로(택소노미·사전 주석·IAA/Kappa 체크)와 적응형 실험 설계(sequential testing·bandit) 스킬은 소수 존재하지만 모두 범용·소규모이며, "Telegram 채널 한국어 금융 메시지 분류 + 골드 라벨 검토"나 "13 arm × ~200 트레이드 + sealed holdout" 같은 이 프로젝트의 구체 요구를 다루는 스킬은 없다. 실험 추적(experiment tracking) 스킬은 발견되지 않았다. 스킬 자체를 A/B 평가하는 도구(agent-crucible, skillevaluation, skillproof)는 dev/holdout 분리·Fisher exact·95% CI 등 이 프로젝트의 edge 판정 방법론과 구조가 닮아 참고 가치가 있다.

### Cited Findings

| 아티팩트 | URL | 내용 | 성숙도/신뢰 신호 | 적합도 |
|---|---|---|---|---|
| seb1n/awesome-ai-agent-skills `data-labeling` | https://github.com/seb1n/awesome-ai-agent-skills ; https://www.skills.sh/seb1n/awesome-ai-agent-skills/data-labeling | 라벨 택소노미 정의 → 모델 예측 사전 주석 → QC → inter-annotator agreement; weak supervision용 텍스트 분류 라벨링 함수 예시 | stars 239(HTML) / 최근 커밋 2026-08-09(atom) / MIT, 103개 스킬, Codex 포함 다중 에이전트 지원 명시 | 중간(야간 분류 파이프라인의 골드셋 절차 템플릿) |
| pjt222/agent-almanac `label-training-data` | https://openskillindex.com/skills/pjt222-agent-almanac-label-training-data ; https://github.com/pjt222/agent-almanac | Label Studio 기반, Kappa < 0.4면 가이드라인 재검토 | stars 42(HTML) / 최근 커밋 2026-10-29(atom) / MIT | 낮음~중간 |
| OpenMed `building-gold-corpus`(Tessl 레지스트리) | https://tessl.io/registry/skills/github/maziyarpanahi/openmed/building-gold-corpus | 골드 표준 주석 프로젝트 스캐폴딩(라벨 스키마·가이드라인·BRAT/Label Studio 설정·train/dev/test 분리, 커밋되는 골드 데이터는 합성만) | 의료 NER 대상 | 낮음(구조만 참고) |
| lvtd-llc/skills `adaptive-experimentation-strategy` | https://www.skills.sh/lvtd-llc/skills/adaptive-experimentation-strategy ; https://github.com/lvtd-llc/skills | 고정 지평 A/B 대신 sequential testing·multi-armed bandit·Thompson sampling·contextual bandit 적용 여부 판단과 데이터/엔지니어링 준비 상태 | stars 1(HTML) / 최근 커밋 2026-09-11(atom) / MIT(Django SaaS용 모음) | 낮음~중간(3단계 자동 배분 설계 시 의사결정 프레임 참고) |
| alonso-skills `arm-bandits-expert`(Tessl) | https://tessl.io/registry/alonso-skills/arm-bandits-expert/skills | Thompson Sampling·UCB·epsilon-greedy·LinUCB·EXP3·contextual bandit 구현·평가·배포, 참조 파일 6개 | 레지스트리 페이지만 확인(stars·갱신일 미확인) | 낮음~중간 |
| simota `Experiment`(TypingMind) | https://www.typingmind.com/skills/simota-experiment | A/B 설계·유의성·sequential testing; 조기 종료 시 anytime-valid(confidence sequences/mSPRT) 권장, "sequential test는 패자를 조기 탐지하지 승자를 앞당겨 선언하지 않음" | 레지스트리 페이지만 확인 | 낮음~중간(1단계 edge 판정의 조기 중단 규칙 설계 참고) |
| agent-crucible (PyPI) | https://pypi.org/project/agent-crucible/ | 스킬 유무로 동일 태스크 반복 실행, 태스크 성공 비교에 Fisher's exact, development/holdout 분리로 과적합 방지 | PyPI 패키지 0.5.0 | 낮음(대상은 스킬 평가) — 방법론 유사 |
| Akhilgarg07/skillproof | https://github.com/Akhilgarg07/skillproof | 스킬 설치 유무 N회씩 실행, pass rate 차이와 95% CI, 스킬이 실제 로드됐는지 검사 | stars 0 / 2026-10-17 생성 / MIT | 낮음 |
| skillevaluation (PyPI) | https://pypi.org/project/skillevaluation/ | eval.yaml 기반 러너: pass rate·속도·토큰·턴·도구 호출 비교 | PyPI 0.5.0 | 낮음 |
| himself65/finance-skills Social Readers(Telegram, `tdl`) | https://github.com/himself65/finance-skills | Telegram·Discord·X 등 소셜 소스 읽기 스킬 | stars 3,498 / MIT | 낮음~중간(채널 수집 구현 참고) |
| NomaDamas/k-skill 뉴스 스킬(`geeknews-search`, `naver-news-search`, `hankookilbo-news`) | https://github.com/NomaDamas/k-skill | 공개 RSS/Atom·검색 기반 | stars 8,251 / 2026-11-02 / MIT | 중간(한국 뉴스 수집) |
| HimanshuJ16 `factor-research-multiple-testing-correction`, `backtest-vs-live-performance-divergence-tracking`, `ensemble-signal-combination-without-overfitting`, `multi-year-regime-coverage-requirement` | https://github.com/HimanshuJ16/Algo-Trading-Skills | 다중검정 보정·백테스트/실거래 괴리 추적 | stars 50 / Apache-2.0 | 중간(1단계 edge 판정 통계의 체크리스트) |
| tradermonty `residual-edge-analyzer`, `signal-postmortem`, `trade-hypothesis-ideator`, `edge-*` 파이프라인 | https://github.com/tradermonty/claude-trading-skills | HAC 회귀 residual edge, 가설 생성→설계→리뷰 파이프라인 | stars 3,570 / MIT | 중간~높음(4단계 가설 파이프라인 구조 참고) |

### Inferences
- 야간 분류 파이프라인용으로는 "라벨 택소노미 + 사전 주석 + IAA" 절차(seb1n data-labeling)와 "골드 데이터는 합성/익명화만 커밋"(OpenMed) 원칙을 가져오되, 한국어 금융 텍스트·Telegram 메시지 구조·중복 제거(동일 뉴스 다채널 전파)는 자체 작성이 필요하다.
- 다중 arm 평가는 "스킬 평가 도구"들이 쓰는 dev/holdout 분리 + Fisher exact/95% CI 구조와 HimanshuJ16의 다중검정 보정을 조합한 자체 SKILL.md가 현실적이다. 13 arm에 대한 다중검정 보정(Bonferroni/BH)과 sealed holdout 봉인 절차를 명시한 공개 스킬은 없다.

### Gaps
- 뉴스 중복 제거(near-duplicate) 전용 스킬은 발견되지 않았다.
- 실험 추적(MLflow/W&B 류)을 SQLite 원장과 연결하는 스킬은 발견되지 않았다.
- Tessl·TypingMind·skills.sh 레지스트리 항목(arm-bandits-expert, simota experiment, adaptive-experimentation-strategy)은 레지스트리 페이지만 확인했고 원본 저장소의 stars·갱신일·내용 품질은 검증하지 못했다.

---

## KQ5. 생태계·설치 경로·보안 신호 (Agent Skills 표준, Codex 지원, skills CLI, 레지스트리, 공급망·prompt injection 위험)

### Takeaway
Agent Skills 표준은 Anthropic이 만들어 공개했고 agentskills.io는 스펙과 클라이언트 목록만 제공할 뿐 공식 중앙 레지스트리는 없다. 설치 경로는 (a) Claude Code `/plugin marketplace add`, (b) Vercel `npx skills add owner/repo`(skills.sh 디렉터리, Claude Code·Codex·Cursor·OpenCode 등 지원), (c) Codex `~/.codex/skills/` + `$skill-installer`(openai/skills는 2026-06 이후 deprecated, openai/plugins로 이동) 세 갈래다. 보안 측면에서 Snyk ToxicSkills(2026-02-05)는 3,984개 스킬 중 36.82%에 보안 이슈, 76개 확정 악성, 악성 스킬의 91%가 prompt injection, ClawHub 스킬의 10.9%에 하드코딩된 비밀을 보고했으며 2026-01 ClawHavoc 캠페인은 12개 계정으로 1,184개 악성 스킬을 배포했다. 따라서 금융 API 키를 다루는 이 프로젝트는 대형 집합의 전체 설치를 피하고 개별 SKILL.md를 읽고 복사하는 방식이 맞다.

### Cited Findings

표준·설치 경로:
- Agent Skills는 "originally developed by Anthropic, released as an open standard"; 홈페이지는 Client Showcase와 스펙·GitHub(agentskills/agentskills)·Discord만 안내하고 레지스트리 언급 없음 — https://agentskills.io/
- agentskills/agentskills 저장소: stars 26,908 / 최근 push 2026-08-09 / Apache-2.0 — https://github.com/agentskills/agentskills
- 통합 가이드: `.agents/skills/`가 cross-client 공유 규약; "프로젝트 레벨 스킬은 신뢰되지 않은 저장소에서 올 수 있으므로 trust check로 게이트"; progressive disclosure(카탈로그 50~100 토큰/스킬, 본문 <5000 토큰 권장) — https://agentskills.io/integrate-skills
- Vercel `skills` CLI: `npx skills add/find/list/use/check/update/remove/init`, `skills-lock.json`, Node 22.20+, Claude Code·Codex·Cursor·OpenCode + 75개 에이전트, 보안 감사 명령 문서화 없음, 텔레메트리(`DISABLE_TELEMETRY=1`) — https://github.com/vercel-labs/skills (stars 37,889 / 최근 push 2026-11-03 / MIT). Vercel 발표 — https://vercel.com/blog/introducing-skills
- 비공식 주장: skills CLI가 GitHub 단축 표기를 네임스페이스 검증 없이 해석해 오타·유사 org 저장소를 끌어올 수 있음(단일 블로그, 미검증) — https://dev.to/mech_app_ai/vercel-skills-cli-npm-for-agent-tool-boundaries-2d9p
- Codex: `~/.codex/skills/**/SKILL.md` 재귀 탐색, 시작 시 name/description/path만 로드(2025-12 문서 스냅샷, experimental) — https://forge.lthn.ai/core/core-agent-ide/src/commit/0a32acaa2deb8883e9dcbfb80e84957837d39b74/docs/skills.md ; openai/skills는 `.system`/`.curated`/`.experimental` 3단계, `$skill-installer`로 설치, README가 deprecated 표시 후 openai/plugins 안내 — https://github.com/openai/skills (stars 28,488 / 최근 커밋 2026-06-24) ; openai/plugins는 `plugins/<name>/.codex-plugin/plugin.json` + `skills/` 구조, figma·notion·build-web-apps 등, 금융 없음 — https://github.com/openai/plugins (최근 커밋 2026-10-08)
- "mdskills.ai 류 가이드는 Codex가 SKILL.md를 네이티브 지원하지 않는다고 하나 위 1차 문서와 충돌(구식 추정)" — https://www.mdskills.ai/learn/how-to-install-skills-codex-cli
- Claude Code 공식 마켓플레이스 설치: `/plugin install {name}@claude-plugins-official`; "Anthropic은 플러그인 내 MCP 서버·파일을 통제·검증하지 않음" — https://github.com/anthropics/claude-plugins-official
- skill-bundle 형식(plugin.json 없이 `strict: false` + `skills: [...]`, git-subdir + sha 고정) — 상동

레지스트리/디렉터리(2차 소스, 자체 검증 없음): skills.sh(Vercel), SkillsMP(96,000+~1.6M SKILL.md 집계, 보안 심사 없음 — https://smartscope.blog/en/blog/skillsmp-marketplace-guide/ ; https://cisomarketplace.com/blog/agent-skills-next-ai-attack-surface/), Tessl, skills.lc, claudemarketplaces.com, mcpmarket.com, Glama/PulseMCP/Smithery(MCP). Glama의 pykrx-mcp·meritz 등재 — https://glama.ai/mcp/servers/meritz-securities/open-api-mcp ; PulseMCP pykrx — https://www.pulsemcp.com/servers/pykrx

보안 데이터:
- Snyk ToxicSkills(2026-02-05): 3,984개(ClawHub·skills.sh), 1,467개(36.82%) ≥1 이슈, 534개(13.4%) critical, 76개 확정 악성(8개 ClawHub에 잔존), 악성의 91%가 prompt injection(전체 ClawHub의 2.6%), ClawHub 10.9%에 하드코딩 비밀(악성의 32%), 17.7%가 제3자 콘텐츠 fetch(악성의 54%), 2.9%가 검증 불가 의존성. 권고: `uvx mcp-scan@latest --skills`, 노출된 자격증명 로테이션, `SOUL.md`/`MEMORY.md` 변조 점검, npm/PyPI와 같은 공급망 엄격성 — https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/
- ClawHavoc: 12개 퍼블리셔 계정, 1,184개 악성 스킬, 2026-01-22 Snyk 발견(OWASP 페이지 자체에 불일치 있음) — https://owasp.org/www-project-agentic-skills-top-10/case-studies.html ; 동일 사건을 "1,184 Skills 체계적 침해"로 기술한 arXiv — https://arxiv.org/html/2605.28588v1 (주입 탐지 플래그: skills.sh 상위 100개 0.0%, clawhub 전체 2.6%, 확정 악성 91%)
- Skill-Inject 벤치마크: 스킬 파일만으로 데이터 유출·랜섬웨어·파괴적 작업 유도 가능 — https://arxiv.org/html/2602.20156v3 ; "Do Not Mention This to the User" 악성 스킬 실태 — https://arxiv.org/pdf/2602.06547 ; 위협 분류 — https://arxiv.org/html/2604.02837v1
- K-Dense README: "Skills can execute code and influence your coding agent's behavior... 설치 파일이 의존성을 설치하지 않음, 필요한 것만 설치, 제3자 스킬은 스캔" — https://github.com/K-Dense-AI/scientific-agent-skills
- 키움 공식: 키를 설정 파일에 평문 저장이 기본, 커밋·dotfiles 동기화·공유 금지, "툴 실행 자동 승인 금지" — https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/SETUP-MCP.md ; kwcli는 OS 자격증명 저장소 사용 — https://github.com/Kiwoom-Securities/Kiwoom-REST-API
- korean-dart-mcp: 수동 설정 시 키가 클라이언트 설정 파일에 평문 — https://github.com/chrisryugj/korean-dart-mcp
- ChunSam: HTTP 모드 `MCP_AUTH_TOKEN` 없으면 기동 거부, `--no-auth`를 외부망에서 쓰면 계좌 조회 도구가 노출 — https://github.com/ChunSam/kiwoom-mcp-server
- superpowers: 외부 로고 로드(버전만 전송), 트랜스크립트 읽는 진단 스킬, hooks 설치 승인 — https://github.com/obra/superpowers

### Inferences
- 이 프로젝트는 브로커 키·Telegram 봇 토큰·DART 키를 다루므로, 스킬 설치 정책은 "마켓플레이스 전체 등록 금지, 개별 SKILL.md를 읽고 `.agents/skills/`(또는 `.claude/skills/`+`.codex/skills/` 심볼릭 링크)에 복사, 번들 스크립트는 실행 전 검토, 네트워크 호출하는 스킬은 야간 파이프라인에서 격리"가 합리적이다. ToxicSkills가 지적한 "제3자 콘텐츠 fetch"는 공시·뉴스·Telegram 메시지를 읽는 리서치 스킬 전부에 해당하므로, 수집 텍스트를 지시가 아닌 데이터로 취급하는 문구를 자체 스킬에 명시해야 한다.
- Codex와 Claude Code 양쪽 호환이 중요하면 `.codex-plugin`·`.claude-plugin`을 함께 가진 저장소(superpowers, wshobson/agents)와 `npx skills add`로 양쪽에 배치 가능한 저장소(k-skill, K-Dense, HimanshuJ16)가 우선이다. tradermonty·korean-dart-mcp는 Codex 전용 절차가 없지만 전자는 SKILL.md 복사, 후자는 `config.toml` MCP 등록으로 해결된다.

### Gaps
- skills CLI의 네임스페이스 해석 취약점 주장은 단일 비공식 출처라 미검증이다.
- SkillsMP 등 집계 레지스트리의 규모 수치는 2차 블로그 출처뿐이다.
- 검색 요약에 "번들 스크립트가 있는 스킬은 위험 odds ratio 2.12×"라는 분석이 언급됐으나 원문을 특정하지 못해 수치를 채택하지 않았다.

---

## KQ6. 기존 스킬이 없는 갭 — 자체 작성이 필요한 항목

### Takeaway
조사 결과 "키움 REST + KRX 규칙 + SQLite 원장 + Telegram 운영"의 교집합에 해당하는 공개 스킬은 하나도 없었다. 아래 항목은 공개 아티팩트의 조각(괄호)을 참고해 자체 SKILL.md로 작성해야 한다.

### Cited Findings
- 조사한 모든 트레이딩 스킬 집합(tradermonty, agiprolabs, HimanshuJ16, wshobson, himself65)은 한국 시장·키움·KIS를 언급하지 않음 — https://github.com/tradermonty/claude-trading-skills ; https://github.com/agiprolabs/claude-trading-skills ; https://github.com/HimanshuJ16/Algo-Trading-Skills ; https://github.com/himself65/finance-skills
- 한국 특화 아티팩트는 전부 MCP 서버·SDK·데이터 스킬이며 운영·실험 절차 스킬이 아님 — https://github.com/Kiwoom-Securities/Kiwoom-REST-API ; https://github.com/NomaDamas/k-skill ; https://github.com/chrisryugj/korean-dart-mcp
- 실험 추적 스킬은 검색에서 발견되지 않음(검색 결과 요약 — https://pypi.org/project/agent-crucible/ 등은 스킬 평가 도구)
- 트레이딩 전용 SQLite 원장 포렌식/대조 스킬 없음(유일한 SQLite 스킬은 삭제 레코드 복구용) — https://agentskill.sh/@mukul975/performing-sqlite-database-forensics/security
- 키움 공식 저장소가 push 2026-09-15 이후 정지 상태이고 ChunSam 서버가 "API 변경으로 깨질 수 있음"을 이유로 유지보수 종료 — https://github.com/ChunSam/kiwoom-mcp-server

### Inferences — 자체 작성 권장 목록(우선순위 순)
1. `kiwoom-rest-ops-runbook`: 토큰 만료/8001 모드 불일치/IP 등록/장 운영시간·휴장일(k-skill `korean-holiday-calendar` 참조)/mockapi 한계(KRX만, 일부 계좌 조회 미지원 — ChunSam README) 대응 절차. 읽기 전용 헬스체크 명령(kwcli `kiwoomcli` 상태 + 잔고 조회)만 허용.
2. `ledger-reconciliation`: SQLite 원장 vs 키움 체결·잔고 API 대조, 미체결·중복 주문 탐지(HimanshuJ16 `order-placement-idempotency`, `multi-source-price-reconciliation-tie-breaking` 참고), 수수료·거래세 반영 PnL 재계산.
3. `arm-edge-verdict`: 13 arm × ~200 트레이드 기준 edge 판정 — sealed holdout 봉인·해제 절차, 다중검정 보정, bootstrap CI, 조기 중단 규칙(anytime-valid), 결과 리포트 템플릿(HimanshuJ16 `factor-research-multiple-testing-correction`, tradermonty `residual-edge-analyzer`, lvtd `adaptive-experimentation-strategy`, agent-crucible의 dev/holdout 구조 참고).
4. `paper-to-live-gate`: 소액 실계좌 전환 체크리스트(키 분리·`KIWOOM_MODE=real` 전환 시점·일일 손실 한도·kill switch·롤백; HimanshuJ16 `paper-to-live-promotion-checklist`, `kill-switch-and-drawdown-circuit-breakers`, `alpaca-paper-live-key-separation` 참고).
5. `allocation-and-drawdown-limits`: 3단계용 자동 배분·낙폭 한도 규칙(HimanshuJ16 `capital-reallocation-based-on-live-performance`, `multi-strategy-capital-allocation-limits`; tradermonty `drawdown-circuit-breaker`).
6. `telegram-message-classification`: 채널 수집(Telethon 또는 tdl) → 중복 제거 → 택소노미 라벨링 → 골드셋 검토(IAA) → 야간 배치; 수집 텍스트를 데이터로만 취급하는 prompt-injection 방어 문구 포함(seb1n `data-labeling`, OpenMed 골드 코퍼스 원칙 참고).
7. `nightly-research-brief`: korean-dart-mcp + k-skill(k-dart, bok-ecos-stats, geeknews-search) 호출 순서와 출력 형식(markdown/xlsx — anthropics/skills xlsx) 고정.
8. `launchd-scheduler`: 봇·야간 파이프라인용 plist 템플릿·`launchctl` 절차·로그 경로를 references/에 고정(제3자 launchd 스킬은 신뢰 신호 부족).
9. `hypothesis-pipeline`(4단계): tradermonty `edge-*` 체인 구조를 KRX 데이터·SQLite 원장 기준으로 재작성.
10. `skill-install-policy`: 제3자 스킬 도입 시 체크리스트(ToxicSkills 권고: 스크립트 검토, 네트워크 호출 여부, 비밀 요구 여부, `mcp-scan --skills`, 버전/sha 고정).

### Gaps
- GitHub 코드 검색 미실행으로, 개인 저장소에 묻힌 "키움·pykrx·DART 키워드 SKILL.md"가 존재할 가능성을 완전히 배제하지 못했다. 인증된 `gh api search/code -f q="키움 filename:SKILL.md"`로 보강 조사가 필요하다.
- 위 자체 작성 목록은 공개 아티팩트 부재에서 도출한 추론이며, 프로젝트 내부에 이미 유사 문서가 있는지는 이 조사 범위 밖이다.
