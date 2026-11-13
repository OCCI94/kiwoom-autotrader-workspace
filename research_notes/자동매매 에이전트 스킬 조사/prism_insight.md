# PRISM-INSIGHT (dragon1086) 조사 노트 — 2026-11-03 기준

> 조사 방법: GitHub 저장소를 `git clone --depth 50` (HEAD `2b034d6`, 2026-11-02 15:19 KST 커밋)하여 README·docs·소스·프롬프트를 직접 읽음. 보조로 GitHub REST API, GeekNews 게시물 2건, 라이브 대시보드 JSON을 확인. 파일 경로는 모두 저장소 루트 기준이며 URL은 `https://github.com/dragon1086/prism-insight/blob/main/<경로>`로 열림.
>
> 표기 규칙: **[사실]** = 코드/문서에서 직접 확인, **[주장]** = 저자 자기보고(검증 불가), **[검증가능]** = 공개 데이터로 재계산 가능, **[이전]** = 전이 가능성 메모.

---

## 1. 아키텍처와 기술 스택

### Takeaway
Python 3.10+ 단일 저장소(2,241파일, 테스트 파일 833개)에 KR/US 주식 + BTC 데모 + Telegram/Kakao 봇 + 관측성까지 들어있는 대형 프로젝트. LLM 실행은 `mcp-agent`(저자 포크) + `openai-agents` 위에서 OpenAI Responses API로 돌고, 데이터는 2026-09 이후 한국투자증권(KIS) API 단일 공급자, 스케줄은 cron, 저장은 SQLite + JSONL 이벤트 → ClickHouse.

### Cited Findings

**언어·규모·활동**
- Python 3.10+, AGPL-3.0, 저장소 생성 2025-08-15, 마지막 push 2026-11-02, stars 794, forks 267, open issues 7, 전체 issue 47건, PR 1,233건, 태그 99개 — [GitHub API](https://api.github.com/repos/dragon1086/prism-insight) (2026-11-03 조회)
- 기여자 커밋 수: dragon1086 2,881, `claude` 83, rocky-mun 22, tkgo11 13 … (12명) — [contributors API](https://api.github.com/repos/dragon1086/prism-insight/contributors)
- 최근 릴리즈: v2.25.0 (2026-10-23), v2.24.0 (10-04), v2.23.0 (09-26), v2.22.0 (09-15), v2.21.0 (08-26), v2.20.0 (08-05), v2.19.0 (07-21), v2.18.0 (07-12) — [releases](https://github.com/dragon1086/prism-insight/releases)
- v2.25.0 한 버전에만 "제품 변경 커밋 797개 / 병합 PR 224개 … +81,653 / −20,165줄" (3주) — [docs/RELEASE_NOTES_v2.25.0.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RELEASE_NOTES_v2.25.0.md)
- 개발 자체가 Codex/Claude Code 에이전트로 진행됨: `AGENTS.md`(Codex·Claude 공통 가이드), `CLAUDE.md`(`@AGENTS.md` import), `.claude/settings.json` 훅이 `tools/harness/hook.py` 호출, `skills/` 디렉터리(5개 SKILL.md) — [AGENTS.md](https://github.com/dragon1086/prism-insight/blob/main/AGENTS.md), [.claude/settings.json](https://github.com/dragon1086/prism-insight/blob/main/.claude/settings.json)

**LLM 프레임워크**
- `requirements.txt`: `mcp-agent @ git+https://github.com/dragon1086/mcp-agent.git@430b9e4…`(업스트림 PR #648 대기 중 포크 고정), `mcp==1.26.0`, `openai==2.43.0`, `openai-agents==0.7.0`, `anthropic~=0.64.0`, `python-telegram-bot[job-queue]>=20.0`, `APScheduler==3.11.0`, `playwright==1.58.0`(PDF), `yfinance==1.4.1`, `firecrawl-py`, `upstash-redis`, `google-cloud-pubsub`, `firebase-admin`, `mcp-server-sqlite` — [requirements.txt](https://github.com/dragon1086/prism-insight/blob/main/requirements.txt)
- 실행 계층: `ReportAgent -> AgentSpec + LLMParams -> OpenAIAgentsBackend -> OpenAI Responses API`; 매매·거시·US 리포트 정의는 여전히 `mcp_agent.agents.agent.Agent` 사용(두 타입 혼재) — [docs/CLAUDE_AGENTS_ko.md §3](https://github.com/dragon1086/prism-insight/blob/main/docs/CLAUDE_AGENTS_ko.md)
- 백엔드 파일: `cores/llm/backends/openai_agents_backend.py`, `cores/llm/backends/mcp_agent_backend.py`, `cores/llm/codex_oauth_fast_backend.py`, `cores/llm/ports.py`(AgentSpec/LLMParams), `cores/llm/mcp_registry.py` — [cores/llm/](https://github.com/dragon1086/prism-insight/tree/main/cores/llm)
- LangGraph 사용 없음(requirements에 없음) — [사실, requirements.txt]
- ChatGPT Plus/Pro 구독으로 API 키 없이 실행하는 "Codex OAuth 프록시": `python -m cores.chatgpt_proxy.oauth_login`, `PRISM_OPENAI_AUTH_MODE=chatgpt_oauth` — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md), [cores/chatgpt_proxy/](https://github.com/dragon1086/prism-insight/tree/main/cores/chatgpt_proxy); FEATURE_FLAGS에 "OAuth LLM 백엔드(ChatGPT 구독) LIVE … 전 배치 적용" — [docs/FEATURE_FLAGS.md](https://github.com/dragon1086/prism-insight/blob/main/docs/FEATURE_FLAGS.md)

**모델 (문서마다 다름 — 시점 차이)**
- README_ko(현행): 리포트·전략·요약·거시 = "GPT-6 Luna"(`REPORT_MODEL`), 매수·매도 판단 = "GPT-6.1 Sol"(`PRISM_BUY_CODEX_MODEL`/`PRISM_SELL_CODEX_MODEL`), 텔레그램 Q&A = GPT-6.1 Sol, 번역·일지 = GPT-6 Luna, 선택 인사이트 = "Claude Sonnet 5.5"(`INSIGHT_MODEL`) — [README_ko.md "AI 모델"](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)
- `.env.example` 주석 기본값: `REPORT_MODEL=gpt-6-luna`, `PRISM_BUY_CODEX_MODEL=gpt-6-astra`, `TELEGRAM_ANALYSIS_MODEL=gpt-6.1-sol`, `INSIGHT_MODEL=claude-sonnet-5-5` — [.env.example](https://github.com/dragon1086/prism-insight/blob/main/.env.example)
- docs/CLAUDE_AGENTS_ko.md(검증 기준 2026-07-29): 리포트 `gpt-5.6-terra`(medium), 매매 `gpt-5.6-sol`(high), 번역 `gpt-5.6-luna`, 거시·요약평가·일지 `gpt-5.4-mini`, 메모리 압축 `gpt-5.4` — [docs/CLAUDE_AGENTS_ko.md §4](https://github.com/dragon1086/prism-insight/blob/main/docs/CLAUDE_AGENTS_ko.md)
- `mcp_agent.config.yaml.example`: `default_model: gpt-5.1`, `reasoning_effort: high` — [mcp_agent.config.yaml.example](https://github.com/dragon1086/prism-insight/blob/main/mcp_agent.config.yaml.example)
- 역사: 2025-08 GeekNews 시점엔 GPT-4.1(리포트 4개 에이전트) + GPT-5(매매 시뮬) + Claude Sonnet 4(텔레그램 봇) — [GeekNews 2025-08-23](https://news.hada.io/topic?id=22681); 2026-02 시점 GPT-5 + Claude Sonnet 4.5 — [GeekNews 2026-02-01](https://news.hada.io/topic?id=26302)

**MCP 서버·도구 (cores/llm/mcp_servers.yaml)**
- `kospi_kosdaq`: 저장소 내장 KIS 어댑터 `python -m cores.market_data.mcp_server`("Korean market data is served exclusively by the repository KIS adapter"); `firecrawl`: `firecrawl-mcp@3.23.6`; `perplexity`: `@perplexity-ai/mcp-server@1.2.0`(대체: `tavily` → `cores/llm/tavily_mcp_server.py`, `PRISM_WEB_SEARCH_PROVIDER=tavily`); `sqlite`: `mcp-server-sqlite --db-path ../stock_tracking_db.sqlite`; `time`: 내장 `cores.llm.time_mcp_server`; `webresearch`: `@mzxrai/mcp-webresearch`; `deepsearch`: `mcp-remote http://localhost:8000/sse`; `yahoo_finance`: `yahoo-finance-mcp==0.1.2` — [cores/llm/mcp_servers.yaml](https://github.com/dragon1086/prism-insight/blob/main/cores/llm/mcp_servers.yaml)
- US 추가: `sec_edgar`(`sec-edgar-mcp`, 10-K/10-Q/8-K, 내부자거래, 무료) — [mcp_agent.config.yaml.example](https://github.com/dragon1086/prism-insight/blob/main/mcp_agent.config.yaml.example)
- 선택: `ADANOS_API_KEY` → 미국 뉴스에 "구조화된 소셜 심리 정보" — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)

**한국 시장 데이터 소스**
- 2026-09-11 "KIS 단일 제공자 전환": "KRX Open API, 직접 KRX 패키지, FDR/Naver 시장 데이터 fallback을 실행 경로에서 제거"; KIS 제약으로 "현재 PER/PBR/EPS/BPS는 오늘의 한 행", 과거 시총 미지원, 전일 시총은 KIS 마스터 `prdy_avls_scal`; 실측 "2,684종목 일봉, 약 484.5초"(콜드), 캐시 재사용 약 30.2초 — [docs/KIS_ONLY_MIGRATION_20260911.md](https://github.com/dragon1086/prism-insight/blob/main/docs/KIS_ONLY_MIGRATION_20260911.md)
- grep(테스트 제외 .py 파일 수): `pykrx` 14, `dart.fss.or.kr` 16, `FinanceDataReader` 6, `finance.naver` 3, `yfinance` 91, `kis_auth` 34 — 잔존 언급은 가드/주석/레거시 포함(실행 경로는 KIS 전용이 의도) — [사실, 클론 grep]
- KR 뉴스: 네이버 금융 스크랩 → KIS 뉴스 제목 피드(`FHKST01011800`)로 교체(2026-10-06, #936/#938); 시장 전체 뉴스 제목 저장소 `runtime/kr_news_titles.sqlite`(cron 5분) — [docs/THEME_NEWS_ROADMAP_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/THEME_NEWS_ROADMAP_ko.md), [prism_core/kr_news_titles.py](https://github.com/dragon1086/prism-insight/blob/main/prism_core/kr_news_titles.py)
- DART: `cores/dart_deep_analysis.py` + `prism_core/dart_*`·`filing_*` 모듈(공시 HTML 파싱, 재무·사업·위험 3역할) — v2.23.0 "DART 심층분석" — [cores/dart_deep_analysis.py](https://github.com/dragon1086/prism-insight/blob/main/cores/dart_deep_analysis.py), [releases](https://github.com/dragon1086/prism-insight/releases)
- US: yfinance + SEC EDGAR; v2.25.0부터 "미국 운영 가격을 야후(yfinance) 대신 KIS 시세로 읽고" — [docs/RELEASE_NOTES_v2.25.0.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RELEASE_NOTES_v2.25.0.md)
- 실제 주문: 한국투자증권 REST (`trading/kis_auth.py`, `trading/domestic_stock_trading.py`, `trading/config/kis_devlp.yaml.example`) — [trading/](https://github.com/dragon1086/prism-insight/tree/main/trading)

**스케줄링 (docker/crontab, Asia/Seoul)**
- KR: 07:00 `update_stock_data.py`, 09:30 `stock_analysis_orchestrator.py --mode morning`, 15:40 `--mode afternoon`, 11:05/17:10 대시보드 JSON, 17:00 `performance_tracker_batch.py`, 18:05(평일) `tools/shadow_lifecycle.py --apply-expiry`, 일요일 03:00 `compress_trading_memory.py`, 02:00 설정 백업 — [docker/crontab](https://github.com/dragon1086/prism-insight/blob/main/docker/crontab)
- US: 00:15 KST morning, 06:30 afternoon, 07:30 performance tracker, 10:05 `us_pending_order_batch.py`(KIS API 창 밖 주문 큐 처리) — 동일 파일
- 장중 보호 루프는 저장소 crontab에 없고 운영 서버 root crontab에만 등록(2026-08-01 실측): `hardstop_seller` 10분 간격(`0-50/10,6-56/10`), `trend_exit_seller` `2-52/10`, `fill_chaser` 홀수분 2분 간격; "저장소 파일과 운영 서버가 다릅니다" — [docs/PIPELINE_ARCHITECTURE_ko.md §3.4](https://github.com/dragon1086/prism-insight/blob/main/docs/PIPELINE_ARCHITECTURE_ko.md)
- 추가 cron 후보(주석): 주간 Firecrawl 인텔리전스 일요일 11:00 — [weekly_firecrawl_intelligence.py](https://github.com/dragon1086/prism-insight/blob/main/weekly_firecrawl_intelligence.py); systemd 유닛 예시 16개(`deploy/systemd/`) — [deploy/systemd/](https://github.com/dragon1086/prism-insight/tree/main/deploy/systemd)
- GitHub Actions: `ci.yml`(Python 3.11, 라이선스 검사 `tools/license_compliance.py`, 선별 pytest), `cla.yml`, `report-data.yml` — [.github/workflows/](https://github.com/dragon1086/prism-insight/tree/main/.github/workflows)

**저장소·관측성·배포**
- SQLite `stock_tracking_db.sqlite`: `stock_holdings`/`trading_history`(+`us_`), `watchlist_history`(KR 30일 후 삭제), `analysis_performance_tracker`, `trading_journal`, `trading_principles`, `trading_intuitions`, `user_memories`, `loop_a_position_state`/`loop_b_*`/`loop_c_chase_log`(락) — [docs/TWO_WEEK_REVIEW_ko.md §2](https://github.com/dragon1086/prism-insight/blob/main/docs/TWO_WEEK_REVIEW_ko.md), [docs/TRADING_JOURNAL.md §4](https://github.com/dragon1086/prism-insight/blob/main/docs/TRADING_JOURNAL.md), [docs/FEATURE_FLAGS.md](https://github.com/dragon1086/prism-insight/blob/main/docs/FEATURE_FLAGS.md)
- 이벤트 원장: `logs/prism_events.jsonl` → ClickStack(ClickHouse), 거래 1건 = `trace_id` 1개(스크리닝→매수→체결→보유평가→매도), 2026-10-08부터 연결, 보존 180일 — [skills/prism-trade-review/SKILL.md](https://github.com/dragon1086/prism-insight/blob/main/skills/prism-trade-review/SKILL.md), [docs/PRISM_OBSERVABILITY.md](https://github.com/dragon1086/prism-insight/blob/main/docs/PRISM_OBSERVABILITY.md), `observability/` 32파일
- 외부 구독자 배포: GCP Pub/Sub(`docs/GCP_PUBSUB_SETUP.md`, `docs/EXTERNAL_SUBSCRIBER_GUIDE.md`), Upstash Redis, Firebase 브리지(`firebase_bridge.py`, 모바일 앱) — [docs/](https://github.com/dragon1086/prism-insight/tree/main/docs)
- Docker: `FROM ubuntu:24.04`, cron 내장, `docker-compose.yml` / `docker-compose.quickstart.yml`, 헬스체크 = DB 테이블·cron 상태 — [Dockerfile](https://github.com/dragon1086/prism-insight/blob/main/Dockerfile)
- 프론트: `examples/dashboard`(React + Radix UI 계열, `npm run dev` :3000), `examples/streamlit`, `examples/landing`; 라이브 대시보드 https://analysis.stocksimulation.kr/ ; 모바일 앱(Google Play `com.prisminsight.prism_mobile`, App Store id6759331074) — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md), [examples/dashboard/package.json](https://github.com/dragon1086/prism-insight/blob/main/examples/dashboard/package.json)

**Telegram·Kakao**
- `telegram_ai_bot.py`(186KB) 명령: `ask cancel chatid evaluate help history insight journal memories report rules signal start theme triggers us_evaluate us_report us_signal us_theme` — [telegram_ai_bot.py](https://github.com/dragon1086/prism-insight/blob/main/telegram_ai_bot.py) (CommandHandler grep)
- 채널 5개 언어(ko `t.me/stock_ai_agent`, en, ja, zh, es) — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)
- 요약 파이프라인: `telegram_summary_agent.py` + `cores/telegram_summary_workflow.py`(evaluator↔optimizer 루프), `cores/agents/telegram_translator_agent.py`, `telegram_moderation.py`(결정론 신호 → LLM 검토 → 운영자 알림, fail-open) — [telegram_moderation.py](https://github.com/dragon1086/prism-insight/blob/main/telegram_moderation.py)
- KakaoTalk "Prism Lounge"(v2.20.0) `kakao_bot/`(hexagonal: adapters/application/domain/ports/runtime) — [releases](https://github.com/dragon1086/prism-insight/releases)

### Inferences
- "13개 이상 에이전트"는 홍보 숫자이며 저자 문서 스스로 "더 이상 정확한 구조 계약이 아닙니다"라고 함(CLAUDE_AGENTS_ko.md). 실체는 리포트 섹션 6 + 인라인 전략/요약 2 + 매매 2 + 저널/압축 + 커뮤니케이션 3~4 + 동적 상담.
- 프로젝트 방향은 2026년 들어 "LLM 판단 → 결정론 게이트"로 꾸준히 이동(Market Pulse FSM, `cores/buy_gate.py`, 재진입 v3 AI 재점검 제거). 사용자 프로젝트의 "LLM은 야간 분류/리서치 전용" 원칙과 수렴하는 흐름.
- 복잡도가 매우 높아(BTC·Kakao·Stance·전인구 시뮬레이터·관측성 등) 통째로 재사용할 대상이 아니라 모듈·문서 단위 발췌 대상.

### Gaps
- README가 말하는 "GPT-6 Luna/GPT-6.1 Sol/Claude Sonnet 5.5"는 저장소 표기 그대로이며 모델 실존·성능은 이 조사에서 검증하지 않음.
- Codex OAuth 프록시의 OpenAI 약관 적합성은 확인하지 않음.
- 운영 서버의 실제 cron/.env는 공개되지 않아 저장소 기본값과 다를 수 있음(문서 자체가 반복 경고).

---

## 2. 기능 상세 (급등주 포착 · 리포트 · 매매 · 백테스트 · 포트폴리오 · US · UI · 라이선스)

### Takeaway
오전(09:30)/오후(15:40) 배치가 Market Pulse로 실행 여부를 정하고, 6개 결정론 트리거(+주도업종·역발상)로 후보를 뽑아 체제별 가중 재점수로 최대 3종목을 고른 뒤, 종목당 6섹션 리포트 → 매수 에이전트 채점(1~10) → 코드 게이트 → 10슬롯 계좌에서 초분할 매수·시나리오 증액·주도주 보유·규칙 재진입·장중 하드스톱으로 운용한다. 범용 백테스터는 없고 자기 거래 기록 리플레이 연구만 있다.

### Cited Findings

**2.1 급등주 포착 (trigger_batch.py, 검증 기준 2026-07-29)**
- 공통 KR 필터: 거래대금 ≥ 100억(`SCREENING_MIN_TRADE_VALUE = 10_000_000_000`), 거래량 ≥ 시장 평균 20%, 시총 ≥ 5,000억(대부분 표준 트리거), 최대 등락 15% — [docs/TRIGGER_BATCH_ALGORITHMS.md §4.1](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_BATCH_ALGORITHMS.md), [trigger_batch.py](https://github.com/dragon1086/prism-insight/blob/main/trigger_batch.py)
- 오전 3개: 거래량 급증(증가율 ≥30%, 종가>시가; 점수 거래량60+등락40), 갭 상승(갭 ≥1%, 종가>시가; 갭50+등락30+거래량20), 시총 대비 자금유입(거래대금/시총; 비율50+거래대금30+등락20) — 같은 문서 §5.1
- 오후 3개: 일중 상승률(3~15%; 등락60+거래대금40), 마감 강도(종가>시가, 종가≥전일종가, `(종가-저가)/(고가-저가)`; 50+30+20), 거래량 증가 횡보(|등락|≤5%, 거래량 +50%, 종가 ≥0.97×MA20; 60+40) — §5.2
- 추가 후보: Macro Sector Leader(거래대금 상위 100 × 업종 매칭, 30일 RS 30%·거래대금 20%·거시 업종 신뢰도 30%·시총 20%), Contrarian Value(sideways/bear 체제만; 거래대금 상위 50, 당일 상승, 52주 고점 대비 −40%~−15%, PER/PBR 양수) — §6
- 재점수: `agent_fit = risk_reward_score*0.60 + stop_loss_score*0.40`(트리거별 고정 손절 5/7/8%, 목표 최소 +15%), 260거래일 O'Neil 지표, MA20 이격을 ADR로 환산한 extension(≤2 ADR→1, ≥6 ADR→0), RS Rating(`RS_RATING_ENABLED=true` 기본, percentile/99) — §7
- 체제별 혼합 가중(Composite/Agent/RS/Extension): strong_bull 0.20/0.35/0.30/0.15, moderate_bull 0.25/0.35/0.20/0.20, sideways 0.20/0.35/0.15/0.30, bear 0.15/0.35/0.15/0.35 — §7.3
- 탑다운/바텀업 슬롯: strong_bull 2/1, moderate_bull·sideways·moderate_bear 1/2, strong_bear 0/3, 기본 최대 3; `topdown_score = rerank_score * (1 + sector_confidence * 0.3)` — §8
- 트리거 품질 가중(2026-10-04, `prism_core/trigger_quality.py`): 최근 180일 자기 기록으로 "7·14·30일 추적 수익 중 최고값이 +20% 이상인 비율"과 슬롯 단위 실현 손익 평균을 베이즈 축소(사전 강도 20)해 `가중치 = 1 + 0.30 × tanh(품질)`(0.70~1.30); 가중치 ≤0.95인 트리거는 "1등 보장" 제외 — [docs/TRIGGER_QUALITY_PRIORITY_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_QUALITY_PRIORITY_ko.md)
- 시점: 오전 배치 09:30은 당일 시가 직후 30분 데이터, 오후 15:40은 장 마감 후 — [docker/crontab](https://github.com/dragon1086/prism-insight/blob/main/docker/crontab); GeekNews 댓글: "KRX 자체가 20분 지연" — [GeekNews 2026-02-01](https://news.hada.io/topic?id=26302)

**2.2 Market Pulse / 체제 (cores/market_pulse.py, cores/regime_policy.py)**
- 순수 FSM(I/O 없음): 분산일 = 지수 −0.2% 이하 & 거래량 증가, 25거래일 창, 분산일 종가 대비 +5% 회복 시 만료; 0~3 `UPTREND`, 4~5 `UNDER_PRESSURE`, 6+ 또는 고점 대비 −10% `CORRECTION`; 탈출 = 랠리 4일차 이후 +1.25% & 거래량 증가(FTD) 또는 조정 전 고점 회복 — [cores/market_pulse.py docstring](https://github.com/dragon1086/prism-insight/blob/main/cores/market_pulse.py), [docs/TRIGGER_BATCH_ALGORITHMS.md §2](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_BATCH_ALGORITHMS.md)
- 배치 정책(`MARKET_PULSE_MODE=live`일 때): CORRECTION이면 오전 휴지·오후 실행, 나머지는 둘 다 실행, 오류 시 fail-open — 같은 문서 §2.3
- 5상태 선별 체제 KR: MA120 1차, 60/120 정렬, 2주 수익률 ±5% → `strong_bull`…`strong_bear`; US: MA200, 50/200, 4주 수익률, VIX 구간(<15/<20/<25/≥25) — §3

**2.3 AI 분석 리포트**
- KR 6섹션 키: `price_volume_analysis`, `investor_trading_analysis`, `company_status`, `company_overview`, `news_analysis`, `market_index_analysis` → 인라인 `investment_strategy_agent`, `summary_agent` → 차트/선택 비전 → Markdown → PDF(Playwright) — [docs/CLAUDE_AGENTS_ko.md §5.1](https://github.com/dragon1086/prism-insight/blob/main/docs/CLAUDE_AGENTS_ko.md)
- 진입점 `cores.analysis.analyze_stock(company_code, company_name, reference_date="YYYYMMDD", language, macro_context)` → Markdown 문자열 — 같은 문서 §6
- 뉴스 섹션은 종목만이 아니라 "같은 업종의 주도주 2~3개와 업종 흐름" 조사 필수(Perplexity/Firecrawl 근거) — [docs/TRIGGER_BATCH_ALGORITHMS.md §9](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_BATCH_ALGORITHMS.md)
- DART 심층 장(章): 3개 병렬 writer(`finance`=실적·현금흐름·차입·회계, `business`=사업구조·지배구조·자본변동, `risks`=약정·우발위험), "서로의 원고를 볼 수 없습니다" ownership 규칙, 메시지 바이트 상한(`WRITER_MESSAGE_MAX_BYTES=400000`, 총 1.8MB soft/6MB hard) — [cores/dart_deep_analysis.py](https://github.com/dragon1086/prism-insight/blob/main/cores/dart_deep_analysis.py)
- 데모: `./quickstart.sh YOUR_OPENAI_API_KEY` → AAPL 리포트, `python3 demo.py TSLA --language ko` — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)

**2.4 매매 (가상 + 실계좌)**
- 2026-02 시점 "실제 매매: 미운영 → 운영"(KIS API) — [GeekNews 2026-02-01](https://news.hada.io/topic?id=26302); 대시보드 JSON `trading_mode: "real"` — [dashboard_data.json](https://analysis.stocksimulation.kr/dashboard_data.json) (2026-11-03)
- 매수 판단: 에이전트가 1~10점 채점, "현재 시장 국면의 최소 점수, 손익비 기준을 넘고, 손절폭이 국면별 한도(−5%~−7%)보다 넓지 않아야" 진입 — [README_ko.md "PRISM은 지금 이렇게 매매합니다"](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)
- 체제 매트릭스(프롬프트): min_score/R/R floor/최대 손절 = parabolic 4/0.7/−7%, strong_bull 4/1.0/−7%, moderate_bull 4/1.2/−7%, sideways 5/1.3/−6%, moderate_bear 5/1.5/−5%(모멘텀 2+, 확인 1), strong_bear 6/1.8/−5% — [cores/agents/trading_agents.py L149-156](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/trading_agents.py)
- 코드 게이트(프롬프트와 별도): `cores/buy_gate.py`, `prism_core/entry_score_policy.py`, `prism_core/sector_cap.py`, `prism_core/trading_scenario_contract.py`; 포트폴리오 최대 10행, 동일 섹터 최대 3, 보유 4개 이상 시 섹터 비중 30% — [docs/TRIGGER_BATCH_ALGORITHMS.md §10](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_BATCH_ALGORITHMS.md), [skills/prism-trading-change-review/SKILL.md](https://github.com/dragon1086/prism-insight/blob/main/skills/prism-trading-change-review/SKILL.md)
- 포지션 사이징: 계좌를 10슬롯으로 분할, 신규 종목은 변동성에 따라 1슬롯의 30~80%로 시작("초분할", `prism_core/micro_split*.py`), 고득점·강한 트리거는 한 단계 크게 — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md), [docs/micro-split/B3_LIVE_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/micro-split/B3_LIVE_ko.md)
- 증액: 매수 시 AI가 증액 시나리오 2~4개 작성·매일 갱신, 코드는 "평균 매수가보다 위일 때만, 직전 매수분을 넘지 않게 … 최대 1슬롯까지"; 가속 증액 조건 최초가 대비 +8% & 거래량 1.5배 — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md), [docs/micro-split/ADD_SCENARIOS_DESIGN_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/micro-split/ADD_SCENARIOS_DESIGN_ko.md)
- 피라미딩 조건: 체제 strong_bull/parabolic, 기존 행 평균 수익 ≥5%, 동일 종목 행 <3 — [docs/TRIGGER_BATCH_ALGORITHMS.md §11](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_BATCH_ALGORITHMS.md)
- 매도 규칙(결정론): 시나리오 손절 또는 매수가 대비 −7%, 손실 중 50MA 이탈, 수익 후 고점 대비 추적 손절; 장중 하드스톱 `stop_loss × 0.995`(0.5% 꼬리 버퍼) — [docs/PIPELINE_ARCHITECTURE_ko.md §3.3](https://github.com/dragon1086/prism-insight/blob/main/docs/PIPELINE_ARCHITECTURE_ko.md), [cores/agents/trading_agents.py L948-969](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/trading_agents.py)
- 추적 손절 ratchet: "new_stop_loss must be higher than the current stop_loss … the stop only moves up", 노이즈 필터 임계 1.5~5% 동적, 체제 불명 시 peak×0.90 — 같은 파일 L996-1123
- 주도주 보유: 매수 후 4~15거래일 내 종가 +20% & 50MA에서 과도 이격 아님 → 손절을 매수가로, 최대 40거래일 보유, 50MA 아래 마감 또는 매수가 하회 시만 매도; 1~3일 급등형은 제외 — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md), [docs/RUNNER_HOLD_RULE_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RUNNER_HOLD_RULE_ko.md)
- v2.25.0 추가: 목표가 도달 매도 폐지(10/7, 추적 손절로 대체), "증명된 종목"(20거래일 이상·+15%·50MA>200MA) 50MA 2일 연속 이탈 시만 매도, 40거래일·+30% 넘으면 시장당 2개 "장기 보유(코어)" 200MA까지 — [docs/RELEASE_NOTES_v2.25.0.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RELEASE_NOTES_v2.25.0.md)
- 재진입 v3: 손절·보류 종목을 최대 60거래일 감시, 14:00(KR)/13:50(US) 기준 가격 회복 시, 감시 기간당 최대 3회, 시장당 하루 2건; v2.25.0에서 "AI 재점검이 우연보다 나은 선별을 못 해 빼고, 규칙 신호면 삽니다" — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md), [docs/REENTRY_V3_LIVE_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/REENTRY_V3_LIVE_ko.md), [docs/RELEASE_NOTES_v2.25.0.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RELEASE_NOTES_v2.25.0.md)
- 재진입 쿨다운(`reentry_cooldown.py`): 손실 청산 24시간, 이익 청산 0시간, `REENTRY_COOLDOWN_LIVE=true` 기본 — [docs/TRADING_JOURNAL.md §6.2](https://github.com/dragon1086/prism-insight/blob/main/docs/TRADING_JOURNAL.md), [docs/FEATURE_FLAGS.md](https://github.com/dragon1086/prism-insight/blob/main/docs/FEATURE_FLAGS.md)
- 주문 보조: `tools/fill_chaser.py`(미체결 추격, 2분), `prism-us/us_pending_order_batch.py`(KIS 창 밖 주문 큐) — [docs/FEATURE_FLAGS.md](https://github.com/dragon1086/prism-insight/blob/main/docs/FEATURE_FLAGS.md), [docker/crontab](https://github.com/dragon1086/prism-insight/blob/main/docker/crontab)

**2.5 성과 추적·피드백**
- `performance_tracker_batch.py`: "Tracks prices at 7/14/30 days after analysis for all stocks (both traded and watched)" → 트리거별 통계 — [performance_tracker_batch.py](https://github.com/dragon1086/prism-insight/blob/main/performance_tracker_batch.py)
- 피드백 루프: 트리거 성과 → 프롬프트 컨텍스트 → 정수 score adjustment(−3..+3); KR은 제안으로만, US는 `adjusted_score = buy_score + score_adjustment`를 게이트에 직접 사용 — [docs/TRADING_JOURNAL.md §5](https://github.com/dragon1086/prism-insight/blob/main/docs/TRADING_JOURNAL.md)
- 매매일지 에이전트: 청산 후 매수/매도 맥락 비교, JSON `lessons[]`에 `application_context{version,status(current_pipeline|improvement|unreviewed),market,stage(batch_buy…)}` — [cores/agents/trading_journal_agent.py](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/trading_journal_agent.py)
- 주간: `compress_trading_memory.py`(일요일), `weekly_insight_report.py`(매매 요약·매도 평가·트리거 성과·AI 직관), `weekly_firecrawl_intelligence.py`, `tools/two_week_review.py`(읽기 전용, 변경별 발동 빈도·반사실 가격) — [docs/TWO_WEEK_REVIEW_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/TWO_WEEK_REVIEW_ko.md)
- "이 과정은 자율 강화학습이 아닙니다. 시스템이 스스로 프롬프트나 주문 규칙을 수정해 배포하지 않습니다" — [docs/PIPELINE_ARCHITECTURE_ko.md §4.1](https://github.com/dragon1086/prism-insight/blob/main/docs/PIPELINE_ARCHITECTURE_ko.md)

**2.6 백테스트**
- KR 주식용 범용 백테스터 없음(저장소 탐색 결과). 있는 것: 자기 후보/거래 리플레이 연구(`tasks/`, `tools/`, `docs/entry-quality-experiments/`), `prism-btc/backtest/`(BTC), RS Rating "KR/US 2022~2026 백테스트, 49회 리밸런스" — [docs/FEATURE_FLAGS.md](https://github.com/dragon1086/prism-insight/blob/main/docs/FEATURE_FLAGS.md), [docs/RESEARCH_LESSONS_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RESEARCH_LESSONS_ko.md)
- 증거 패킷 도구: `tools/build_entry_quality_evidence_packet.py --input logs/prism_events.jsonl` → 결정론적 Evidence Packet(`packet_id` 재현) — [skills/prism-entry-quality-analysis/SKILL.md](https://github.com/dragon1086/prism-insight/blob/main/skills/prism-entry-quality-analysis/SKILL.md)

**2.7 US·BTC·기타 모듈**
- `prism-us/`(us_stock_analysis_orchestrator, us_trigger_batch, us_stock_tracking_agent; S&P500·Nasdaq100 유니버스, 거래대금 ≥1억달러) — [docs/TRIGGER_BATCH_ALGORITHMS.md §4.2](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_BATCH_ALGORITHMS.md)
- `prism-btc/`(Bybit 데모, 233 테스트 파일, docs/BTC_* 90여 문서) "모두 Bybit 데모이며 실자금 변경과 수익성 입증은 없습니다" — [docs/RELEASE_NOTES_v2.25.0.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RELEASE_NOTES_v2.25.0.md)
- Stance 프로토콜(`stance/`): "실적을 신고받지 말고, 판단을 미리 선언받아라" — `{"protocol":"stance/1","strategy":…,"seq":42,"symbol":"005930","target_weight":0.10}`; 공식 순위는 "63거래일 동안 기록하고, 자산의 1% 이상을 넣었던 거래를 20번 마친 뒤" — [stance/README.md](https://github.com/dragon1086/prism-insight/blob/main/stance/README.md), [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)
- `events/jeoningu_trading.py`: "전인구경제연구소 채널을 모니터링하고, AI로 분석하여, 반대 방향으로 베팅하는 컨트리언 전략" 시뮬레이터(yt-dlp·feedparser) — [events/JEONINGU_TRADING.md](https://github.com/dragon1086/prism-insight/blob/main/events/JEONINGU_TRADING.md)
- 테마 지도(`prism_core/kr_theme_map.py`): 인포스탁 장 초반 동반등락 제목("원일티엔아이(136150) +8.86%, 팬오션 +3.99% …")을 멤버 겹침으로 클러스터링(`JOIN_JACCARD=0.3`, `MERGE_JACCARD=0.5`, `MIN_LINES=5`), "Everything here is deterministic; only naming uses a model"; 1/22~10/6 묶음 5,794건 → 테마 198개를 20초에 생성 — [prism_core/kr_theme_map.py](https://github.com/dragon1086/prism-insight/blob/main/prism_core/kr_theme_map.py), [docs/THEME_NEWS_ROADMAP_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/THEME_NEWS_ROADMAP_ko.md)

**2.8 라이선스**
- 이중 라이선스: AGPL-3.0(개인·비상업·오픈소스) + "SaaS 기업은 별도의 상업 라이선스"(`COMMERCIAL-LICENSE-ko.md`), 기여 시 CLA(`CLA.md`, `.github/workflows/cla.yml`), `THIRD_PARTY_NOTICES.md` — [README_ko.md "라이선스"](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)
- 월 운영비(2026-01): 약 $313(OpenAI $234, Anthropic $11, Firecrawl+Perplexity $36, 서버 $32); "450명 이상이 무료로 사용" — [README_ko.md "후원"](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md) [주장]

### Inferences
- 급등주 포착은 "장중 실시간 감시"가 아니라 하루 2회 스냅샷 스크리닝. 사용자 봇의 장중 arm들과 성격이 다르고, 오히려 "전일 종가/당일 시가 기준 후보 생성" 야간·아침 잡에 가깝다.
- 매매 로직의 경제적 핵심(진입 Enter/No Entry, 점수)은 LLM이지만, 손절·추적·주도주 보유·재진입·사이징은 전부 코드가 집행한다. 즉 "LLM 없이도 돌아가는 뼈대"가 이미 분리돼 있어 발췌가 쉽다.

### Gaps
- 가상 vs 실계좌 구분: 대시보드 `trading_mode: real`이지만 README 표는 "모의 매매 결과"로 표기. 실계좌 금액·슬롯 크기는 비공개.
- `stock_tracking_agent.py`(261KB)·`trigger_batch.py`(105KB) 전체를 읽지 않아 코드 상수 전부를 대조하진 못함(문서 수치는 2026-07-29 검증본).

---

## 3. 프롬프트 구조와 에이전트 역할, 자기평가 단계

### Takeaway
프롬프트는 (1) 리포트 섹션 작가(데이터 수집 지시 + 보고서 구조 + 할루시네이션 금지 + 출력 형식), (2) 매수 전략가(William O'Neil 페르소나, F1~F4 펀더멘털 게이트 → 추세 게이트 → 상습손절 게이트 → 체제 매트릭스 → 모멘텀 카운트 → JSON), (3) 매도 판단(계층형 Tier, 손절 ratchet, DB 직접 UPDATE 금지), (4) 요약 평가자/최적화자 루프, (5) 일지 회고로 나뉜다. 프롬프트 내 수치는 코드 상수와 테스트로 묶여 있다.

### Cited Findings

**리포트 섹션 프롬프트 (예: price_volume)**
- 역할 "You are a stock technical analysis expert", 도구 지정 `kospi_kosdaq-get_stock_ohlcv`, MA 5/20/60/120, "RSI (14-day) … MUST CALCULATE from OHLCV data", MACD(12/26/9), Bollinger(20, ±2σ), 구조 4단(개요·거래량·지표·전망), 제목 고정 `### 1-1.`/`####`, "Present conditional scenarios rather than definitive predictions", "To prevent hallucination, include only content confirmed", "Do not include mentions of tool usage", "Never use the load_all_tickers tool!!" — [cores/agents/stock_price_agents.py L22-80](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/stock_price_agents.py)
- 뉴스 섹션: 수집 순서 강제(`_news_listing_step` KIS 제목 → `_news_source_contract`), 분류 4종(당일 주가 영향/내부/외부/향후), "Sector leader trends (mandatory)", 참고문헌 URL 섹션, "Beware perplexity hallucinations, always verify dates" — [cores/agents/news_strategy_agents.py L104-164](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/news_strategy_agents.py)
- 공통 계약 헬퍼: `report_time_contract`, `report_narrative_contract`(`prism_core/report_presentation.py`), `report_evidence_contract` — [cores/dart_deep_analysis.py](https://github.com/dragon1086/prism-insight/blob/main/cores/dart_deep_analysis.py), `prism_core/` 목록
- 신규 섹션 추가 패턴: `ReportAgent(name, instruction, server_names=["kospi_kosdaq"])` — [docs/CLAUDE_AGENTS_ko.md §7.1](https://github.com/dragon1086/prism-insight/blob/main/docs/CLAUDE_AGENTS_ko.md)

**매수 시나리오 프롬프트 (create_trading_scenario_agent, KR; en/ko 두 벌)**
- SYSTEM CONSTRAINTS: "The decision is made NOW", 조건부 대기 금지, "Decision is "Enter" OR "No Entry"", 사이즈는 시스템이 정함, "Vague concern" 사유 금지, 재진입 감시는 별도 배치 사실 — [cores/agents/trading_agents.py L37-56](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/trading_agents.py)
- 정체성 "You are William O'Neil, creator of the CAN SLIM system", CAN SLIM × 리포트 섹션 매핑 표 — L58-78
- 체제: 결정론 `market_regime`이 "authoritative. Copy its exact enum"; 불가 시에만 KOSPI 20MA로 유도; "Anti-optimism guardrail" — L80-93
- Step 1 펀더멘털 게이트 F1~F4(영업이익 2분기 양수, 부채비율 <200% 또는 업종 평균 이하, ROE ≥5% 또는 2년 매출 성장 ≥10%, 사업 명확성); 강세장은 1개 실패 보상 가능, 횡보/약세는 any fail → No Entry — L95-109
- Step 1.5 개별 추세 게이트 T1(현재가 < 50/60MA), T2(20MA 하락 기울기 & −5% 이하); "Only cite a moving average that appears in the facts block" — L111-130
- Step 1.6 상습 손절 게이트: 2주 내 2회 이상 손절 → 새 구조적 촉매 없으면 No Entry — L132-137
- Step 2 체제 매트릭스 + "Distribution Day Kill Switch": 분산일 ≥6이면 신규 매수만 체제 1단계 강등 — L139-185
- Step 3 모멘텀 신호 5종(거래량 ≥200% 20일 평균, 외인+기관 3일 연속 순매수, 52주 고점 5% 이내, 업종 상승, 박스 상단 돌파); 트리거 유형 자동 1점 — L187-199
- 손익비 계산식 `expected_loss_pct = (current_price - stop_loss)/current_price*100`, `risk_reward_ratio = expected_return/expected_loss` — L291-292, L687-692
- 목표가 규칙(ko): O'Neil +20% 하단을 손익비 기준점으로만 쓰고 "가격 예측도 매도 지점도 아니다", 근거 없으면 target null → 미진입 — L701-712
- 출력 JSON: `buy_score`(1~10), `macro_adjustment`, `effective_score`, `min_score`, `decision`, `rejection_reason`, `stop_loss`, `risk_reward_ratio`, `market_condition`, `target_provenance` 등 — L415-426, L804-815
- 프롬프트↔코드 정합 테스트: 파일 주석 "2026-10-19 BUY prompt/code alignment (tests/test_buy_prompt_alignment_20261019.py)"; 매도는 `tests/test_sell_prompt_consistency.py` 패턴, "reviewed prompt-hash tests with a date and reason" — [cores/agents/trading_agents.py L26-32](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/trading_agents.py), [skills/prism-trading-change-review/SKILL.md §3](https://github.com/dragon1086/prism-insight/blob/main/skills/prism-trading-change-review/SKILL.md)

**매도 판단 프롬프트 (create_sell_decision_agent)**
- Tier 1 절대 매도(−7% 또는 stop×0.995, 장중 자동 집행), 시스템 집행 50MA 손실 이탈(진입일 제외), 추적 손절 활성 전(peak < entry+5%) 초기 손절 유지, "new_stop_loss must NEVER exceed current price", "Never lower the stop", "DO NOT directly UPDATE … stock_holdings table", `confidence` 1~10, `portfolio_adjustment.new_stop_loss` — [cores/agents/trading_agents.py L874-1123](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/trading_agents.py)
- 추세 약화 전량 매도 3조건(3일 연속 종가 하락 & 평균 이상 거래량 & 20MA 아래 마감) — L838
- 문서 경고: 프롬프트는 "종가 기준"을 지시하지만 5분 `hardstop_seller`가 장중가로 먼저 발동 → "AI 가 남긴 판단 로그가 실제 동작을 설명하지 못합니다"(2026-07-31 IP 사례) — [docs/PIPELINE_ARCHITECTURE_ko.md §3.3](https://github.com/dragon1086/prism-insight/blob/main/docs/PIPELINE_ARCHITECTURE_ko.md). 이후 v2.25.0에서 프롬프트에 장중 기준 문구가 반영됨(위 L948-949).

**자기평가·품질 루프**
- 텔레그램 요약 평가자: 기준 6개(정확성=보고서·KIS 검증 데이터 대조, 포맷, 명확성, 관련성, 경고 문구, 길이 "400자 내외"), 등급 EXCELLENT/GOOD/FAIR/POOR, "누락값은 UNKNOWN이며 추측하지 마십시오" — [cores/agents/telegram_summary_evaluator_agent.py](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/telegram_summary_evaluator_agent.py); 최적화자와 "설정된 품질 기준을 만족할 때까지 반복" — [docs/CLAUDE_AGENTS_ko.md §5.6](https://github.com/dragon1086/prism-insight/blob/main/docs/CLAUDE_AGENTS_ko.md)
- 일지 에이전트: "Most lessons must concern checks practicable NOW using the supplied batch report", 근거 없으면 교훈 0개 반환 허용 — [cores/agents/trading_journal_agent.py L236-246](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/trading_journal_agent.py)
- 정의만 있고 미연결: `create_context_retriever_agent`, `create_intuition_validator_agent` — [docs/CLAUDE_AGENTS_ko.md §5.5](https://github.com/dragon1086/prism-insight/blob/main/docs/CLAUDE_AGENTS_ko.md)
- 비전(차트 이미지) 매수 품질검사 S3는 OFF, 렌더 QA는 log-only — [docs/FEATURE_FLAGS.md](https://github.com/dragon1086/prism-insight/blob/main/docs/FEATURE_FLAGS.md)

### Inferences
- 프롬프트는 "LLM에게 규칙을 설명"하는 형식이지만, 실제 집행 권한은 코드 게이트에 두고 프롬프트는 테스트로 코드 상수에 고정한다. 이 "prompt-as-documentation + code-as-authority" 패턴이 가장 큰 설계 교훈.
- 리포트 프롬프트의 "지표를 직접 계산하라"(RSI/MACD) 지시는 LLM 산술 오류 위험을 안고 있다. 사용자 프로젝트에선 지표 계산은 코드, LLM은 해석만 맡기는 편이 안전.

### Gaps
- US 프롬프트(`prism-us/cores/agents/trading_agents.py`)와 거시 에이전트(`cores/agents/macro_intelligence_agent.py`) 본문은 읽지 않음.
- 투자전략·핵심요약 인라인 프롬프트(`cores/report_generation.py`)는 미확인.

---

## 4. 결과와 신뢰성 (성과 수치 · 측정 방식 · GitHub/커뮤니티 지표)

### Takeaway
저자가 공개한 수치는 자기보고이며 측정 방식 설명이 비교적 투명하다(복리 없음, 청산 거래만, 10슬롯 계좌 환산). 시즌 2(2025-09-30~2026-10-02) KR은 거래별 합계 +355%이나 10슬롯 계좌 기준 +35.2%, 승률 40.3%, 같은 기간 KOSPI는 +103.5%(저자 표기)로 지수 대비 열위를 저자가 인정. 외부 독립 검증·블로그 후기는 찾지 못함.

### Cited Findings

**시즌 1 (2025-03~2025-08, 가상)**
- "5개월 누적 수익률 251.39%, 35거래"(GPT-5 시뮬), 저자 댓글 "상승장이었던 덕이 큼… 장기 검증 필요" — [GeekNews 2025-08-23](https://news.hada.io/topic?id=22681) [주장]

**2026-02 중간 보고 (실계좌)**
- 시즌 2(2025-09 시작) "54거래, 승률 44.44%, 실계좌 수익 +14.71%", 저자: 코스피/코스닥 30~50% 상승 대비 "아쉬운 성과"; 에이전트 11→14, 구독자 100+→550+, 실매매 미운영→운영 — [GeekNews 2026-02-01](https://news.hada.io/topic?id=26302) [주장]

**README 시즌 2 표 (2026-10-02 생성, 청산 거래만, 복리 아님)**
- KR 2025-09-30~2026-10-02: 청산 211건, 승률 40.3%(85승), 거래당 평균 +1.68%, 거래별 합계 +355.3%, **10슬롯 계좌 +35.2%**, 계좌 곡선 MDD −9.3%p; 같은 기간 코스피 +103.5%(3,431→6,982), 코스닥 +5.3%; 최고 삼성전기 +86.8%, SK하이닉스 +73.8% — [README_ko.md "매매 실적 — 시즌 2"](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md) [주장/검증가능: dashboard_data.json으로 재계산 가능]
- US 2026-01-28~2026-10-02: 127건, 33.1%, +0.65%, 합계 +82.8%, 10슬롯 +8.3%, MDD −13.7%p; S&P500 +10.8%, 나스닥 +14.8% — 같은 표
- 저자 자평: "이 기간 계좌 수익률은 코스피에 뒤졌습니다"; 원인 진단 "진입 후 60거래일 안에 30% 이상 오른 한국 종목에서 실현 수익의 중앙값은 +2%였지만, 최고 상승폭의 중앙값은 +60%" — 같은 섹션
- 측정 정의: "거래별 수익률 합계 — … 복리가 아니고 투자 비중도 반영하지 않습니다", "10슬롯 계좌 수익률 — 계좌를 10개의 같은 슬롯으로 나눈 모의 계좌의 실현 수익률", "보유 중인 종목은 제외", 출처 `dashboard_data.json`/`us_dashboard_data.json` — 같은 섹션
- 별도 경고: "누적 수익은 거래별 수익률의 합계이며, 미진입 후보의 성과는 사후 관찰값입니다. 시간가중 포트폴리오 수익률이나 실제로 실현 가능한 백테스트 수익률을 뜻하지 않습니다" — [README_ko.md "매매 시스템은 어떻게 실패에서 배웠나"](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)

**라이브 데이터 (2026-11-03 17:10 KST 생성)**
- `summary.trading`: total_trades 219, wins 86 / losses 133, win_rate 39.27%, avg_profit_rate +1.52%, avg holding 4.84일; 보유 1종목(006400 삼성SDI); `trading_mode: "real"`; 키 `generated_at, trading_mode, summary, holdings, real_portfolio, account_summary, operating_costs, trading_history`; 누적수익률·MDD 필드 없음(대시보드가 곡선에서 계산) — [dashboard_data.json](https://analysis.stocksimulation.kr/dashboard_data.json) [검증가능]
- 거래 샘플(ID 213~219): 팬오션 +3.41%, 팬오션 −6.71%, 안랩 −5.07%, HPSP −4.98%, RF머트리얼즈 −0.4%, 로보티즈 −0.57% — 같은 파일(앞부분 10만자만 읽음)

**비용·슬리피지**
- 장중 하드스톱 실측 "평균 2.08%" 슬리피지 반영 후에도 종가 기준 대비 거래당 +0.52pp 우위(신호 688건, `tasks/exit_execution_quality_results.md`) — [docs/PIPELINE_ARCHITECTURE_ko.md §3.3](https://github.com/dragon1086/prism-insight/blob/main/docs/PIPELINE_ARCHITECTURE_ko.md) [주장]
- README 성과 표에 수수료·세금 반영 여부 명시 없음 — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md) (미명시)

**연구 결과 공개 (자기 데이터 리플레이, 2026-10)**
- 기각: 포켓피봇·거래량 확인 트리거(2018~2026 "거래량 조건이 아무 효과가 없었습니다"), 50·200MA 동시 회복 매수("한국에서는 오히려 나빴습니다"), 오닐 8주 규칙 그대로 50MA 보유(KR −43%p, US 약 −40%p), 60분 손절 점검, ATR 손절(털림률 26%→5%지만 수익 열위), 올린 손절선 종가 집행(KR −41~−49%p, US −29~−38%p) — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md), [docs/RESEARCH_LESSONS_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RESEARCH_LESSONS_ko.md)
- 채택: 주도주 보유(KR 7건 기준 +57%p, "표본이 작고 같은 데이터로 고른 결과"), 재진입 최대 3회(1회 제한이 수익 재진입을 KR 4/7, US 13/43 잘라냄), 트리거 품질 우선순위, 거래량 급증 트리거는 상승일만 — 같은 문서
- 뉴스·테마: "뉴스·테마를 매매 근거로 쓰는 연구는 모두 기각되어 설명 자료로만 씁니다" — [docs/RELEASE_NOTES_v2.25.0.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RELEASE_NOTES_v2.25.0.md)
- 시총대비 수급(외인+기관 순매수÷시총) 장중 확인 지표 → 기각(R0-1) — [docs/THEME_NEWS_ROADMAP_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/THEME_NEWS_ROADMAP_ko.md)
- 연구 방법 교훈: "평균은 소수 대박에 끌려간다. 상위 1~2건을 뺀 결과와 중앙값을 함께 본다", "점심 가격과 종가로 같은 규칙을 판정하면 신호 일치율이 한국 65%, 미국 50%" — [docs/RESEARCH_LESSONS_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RESEARCH_LESSONS_ko.md)
- 보고 규칙: "수익률은 한 개의 평균으로 보고하지 않는다 … 승률·건수, 평균 수익률(이긴 매매), 평균 손실률(진 매매), 손익비" — [docs/HARNESS_CORE_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/HARNESS_CORE_ko.md)

**커뮤니티·노출**
- GeekNews 2건(2025-08-23 15P/6댓글, 2026-02-01 30P/6댓글) — [22681](https://news.hada.io/topic?id=22681), [26302](https://news.hada.io/topic?id=26302)
- 홍보 영상 YouTube `zAywb1G0wRA`, 샘플 리포트 영상 `LVOAdVCh1QE` — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)
- Indie Hackers 제품 페이지 존재(HTTP 403으로 본문 미확인; 검색 요약상 "13 Specialized Agents, 약 10개월 운영, 수익 모델 없음") — [indiehackers.com/product/prism-insight](https://www.indiehackers.com/product/prism-insight)
- 플래티넘 스폰서 AI3/WrksAI, GitHub Sponsors — [README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md)
- 외부 이슈 예: #685 "Your no-backfill Stance philosophy, applied to macro — a neutral third-party scorecard", #650 "Security review offer — agent/MCP tool boundaries", #672 OrcaRouter provider, #272 인도 주식 요청 — [issues](https://github.com/dragon1086/prism-insight/issues?q=is%3Aissue)

### Inferences
- 성과의 신뢰도: (+) 대시보드 원시 JSON 공개, 측정 정의 명시, 지수 열위 자인, 사전등록·반사실 비교 문화. (−) 자기 데이터 리플레이(같은 표본으로 규칙 선택), 소표본(7건 +57%p), 수수료/세금 명시 없음, 10슬롯 환산은 가정 계좌, 2026-10 규칙 변경이 커서 시즌 2 수치는 현재 규칙의 성과가 아님.
- 2025-08 "+251%" → 2026-02 "+14.71%" → 2026-10 "10슬롯 +35.2%"로 지표 정의가 계속 바뀌어 시계열 비교가 어렵다.
- 독립 리뷰·티스토리/velog 후기·YouTube 해설은 검색에서 확인되지 않음(저자 자료가 대부분).

### Gaps
- `us_dashboard_data.json` 및 KR JSON 전체(260만자)는 읽지 않아 README 표 수치를 직접 재계산하지 못함.
- GitHub Discussions(HTTP 200) 내용은 조회하지 않음.
- 텔레그램 채널 구독자 수 현재값(README "450명 이상", 2026-02 "550+")의 정확한 시점 불명.

---

## 5. 알려진 한계·실패 모드·비판

### Takeaway
저자 문서가 스스로 가장 많은 한계를 기록한다: 프롬프트와 실제 집행의 불일치, 저장소 crontab과 운영 서버의 괴리, 배선되지 않은 트리거/에이전트, 데이터 지연, LLM 산술·날짜 오류, 그리고 지수 대비 열위. 외부 비판은 GeekNews 댓글의 "실시간 아님(KRX 20분 지연)" 정도.

### Cited Findings
- 프롬프트(종가 기준) vs 집행(장중 5분 루프) 불일치; "두 주체 사이에 조율 장치는 없습니다"; 저자는 장중 집행이 우수하므로 "현재 동작을 종가 기준으로 되돌리지 마십시오" — [docs/PIPELINE_ARCHITECTURE_ko.md §3.3](https://github.com/dragon1086/prism-insight/blob/main/docs/PIPELINE_ARCHITECTURE_ko.md)
- 보호 루프가 `docker/crontab`에 없음 + 코드 기본값 SHADOW; 운영 서버만 LIVE → "코드가 있다"와 "운영 중이다"가 다름 — 같은 문서 §3.4
- `hardstop`과 `trend_exit`은 락 네임스페이스가 분리돼 "같은 분에 돌면 동일 종목 동시 매도 경합" 가능 — 같은 문서 §3.4
- US `value_to_cap` 트리거는 `cap_df=None`으로 호출돼 "후보를 내지 못합니다" — [docs/TRIGGER_BATCH_ALGORITHMS.md §4.2](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_BATCH_ALGORITHMS.md)
- "보유 7종목 이상 → 6점 이상만" 규칙은 "프롬프트 규칙이며 코드 강제가 없습니다" — [docs/TWO_WEEK_REVIEW_ko.md §4-1](https://github.com/dragon1086/prism-insight/blob/main/docs/TWO_WEEK_REVIEW_ko.md)
- 매수 판단의 "사유 없는 보류" 과거 KR 151건 중 이후 큰 수익 44% — 같은 문서 §3
- 재진입 AI 재점검 제거 사유 "우연보다 나은 선별을 못 해"; 미국 재진입 주문 10/13~10/20 전부 실패; 판단 실행이 빠진 날의 가상 포지션이 실매수를 막음 — [docs/RELEASE_NOTES_v2.25.0.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RELEASE_NOTES_v2.25.0.md)
- 미국 주간 압축이 없는 칼럼명을 써서 직관 9건 저장 실패(#920) — 같은 문서
- KIS 초당 호출 한도(`EGW00201`)가 print로만 남고 로그는 일요일마다 비워짐 → 이벤트 추가 — [docs/TWO_WEEK_REVIEW_ko.md §3](https://github.com/dragon1086/prism-insight/blob/main/docs/TWO_WEEK_REVIEW_ko.md)
- KIS 데이터 제약: PER/PBR 과거 시계열 없음, 전체시장 과거 유니버스 미지원 → 생존편향 있는 리플레이 가능성 — [docs/KIS_ONLY_MIGRATION_20260911.md](https://github.com/dragon1086/prism-insight/blob/main/docs/KIS_ONLY_MIGRATION_20260911.md)
- 네이버 금융 뉴스 iframe 스크랩이 빈 결과(2026-10-06 두산로보틱스 사례) → KIS 제목 피드로 교체 — [prism_core/kr_news_titles.py](https://github.com/dragon1086/prism-insight/blob/main/prism_core/kr_news_titles.py)
- 이슈: #448 "매수 시 분산일 적용이 잘 안되는 것 같은 메시지", #449 "음봉인 종목이 변동률 0%로 오표기", #281 "삼성전기 실제 매수인데 매수사인으로 처리 안됨", #283/#285 "ask명령어 툴호출 시 1년 전 데이터 조회", #286 "2.1기업현황 분석 firecrawl tool 결과 안나옴", #1219 Pub/Sub 구독에 재진입 BUY 누락, #263 면책 경고 중복 — [issues?state=closed](https://github.com/dragon1086/prism-insight/issues?q=is%3Aissue+is%3Aclosed)
- GeekNews: "주식 가격은 실시간이 아니네욥" → 저자 "KRX 자체가 20분 지연"; "전인구 투자 실험" 비유 → 저자 "실험 설계가 불완전해 포기" — [GeekNews 2026-02-01](https://news.hada.io/topic?id=26302)
- 리포트 프롬프트가 LLM에 RSI/MACD 직접 계산을 요구 — [cores/agents/stock_price_agents.py L33-36](https://github.com/dragon1086/prism-insight/blob/main/cores/agents/stock_price_agents.py)
- 책 지식 흡수 하네스의 교훈: "책의 '이상형'을 숫자 게이트로 만들지 않는다. 책 사례조차 탈락한다(정답지 0/9)", "7주 규칙은 우리 실제 거래에서 적용 대상이 없었다(KR 0/202, US 1/134)" — [docs/knowledge/README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/knowledge/README_ko.md)
- 운영 복잡도: 1,233 PR·797커밋/3주, 문서 154개 중 BTC 90여 개, `claude` 봇 커밋 — [GitHub API](https://api.github.com/repos/dragon1086/prism-insight), [docs/](https://github.com/dragon1086/prism-insight/tree/main/docs)

### Inferences
- 가장 반복되는 실패 모드는 "문서/프롬프트가 말하는 것 ≠ 코드가 하는 것 ≠ 서버가 돌리는 것"의 3중 괴리. 저자는 이를 `tools/feature_status.py`, 프롬프트 해시 테스트, 이벤트 원장으로 메우는 중.
- LLM을 주문 시점에 쓰는 구간(BUY/SELL)은 계속 축소되고 있어, 사용자 프로젝트의 "주문 시점 LLM 금지" 원칙이 이 프로젝트의 1년 경험과도 일치한다.

### Gaps
- 외부 보안 리뷰(#650) 결과, Codacy 등급 상세는 확인하지 않음.
- 운영 중 장애(배치 미실행 일수 등) 통계는 공개 자료에서 찾지 못함.

---

## 6. 전이 가능성 — Kiwoom REST 페이퍼트레이딩 봇(규칙 기반, LLM 야간 전용)에 맞춘 컴포넌트별 판정

### Takeaway
PRISM의 결정론 부분(트리거·Market Pulse·체제·트리거 품질 가중·청산 규칙·재진입·테마 클러스터링·변경 검토 하네스·교훈 장부)은 거의 그대로 SKILL.md/야간 잡으로 옮길 수 있고, LLM 판단 부분(BUY/SELL 에이전트, 증액 시나리오 작성, 장중 상담)은 "야간 후보 리포트·분류·회고" 용도로만 재배치하면 된다. 가장 가치 있는 전이물은 코드보다 **운영 문서 체계**(FEATURE_FLAGS, RESEARCH_LESSONS, HYPOTHESIS_REGISTRY, 변경 검토 절차, trace_id 원장)다.

### Cited Findings (전이 판정표)

| # | PRISM 컴포넌트 (경로) | 성격 | 사용자 봇에서의 형태 | 판정 |
|---|---|---|---|---|
| 1 | 6개 트리거 + 필터 + 체제별 재점수 (`trigger_batch.py`, [docs/TRIGGER_BATCH_ALGORITHMS.md](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_BATCH_ALGORITHMS.md)) | 결정론 | **SKILL: "surge-stock screener (KR)"** — 수치(거래대금 ≥100억, 거래량 +30%, 갭 ≥1%, 상승 3~15%, 마감강도, 횡보+거래량 +50% & ≥0.97×MA20)와 점수 가중을 그대로 옮김. pykrx 대신 Kiwoom 일봉/거래대금/시총 API로 입력 치환 | 적용 가능 (장 전/장 후 잡) |
| 2 | Market Pulse FSM (`cores/market_pulse.py`) | 결정론, 순수 함수 | **SKILL + 야간 잡: "O'Neil M 상태기계"** — KOSPI/KOSDAQ 일봉으로 분산일 카운트(−0.2%·거래량 증가·25일·+5% 만료) → UPTREND/UNDER_PRESSURE/CORRECTION → 다음날 arm 활성화 게이트 | 적용 가능 (AGPL 코드 직접 복사 시 라이선스 주의, 규칙 자체는 공개 방법론) |
| 3 | 5상태 선별 체제 (`cores/regime_policy.py`) | 결정론 | 야간 잡: MA120/60 정렬 + 2주 수익률 ±5% → arm별 min_score/최대 손절 테이블 | 적용 가능 |
| 4 | 트리거 품질 가중 ([docs/TRIGGER_QUALITY_PRIORITY_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/TRIGGER_QUALITY_PRIORITY_ko.md), `prism_core/trigger_quality.py`) | 결정론, 자기 기록 | **SKILL: "arm quality weighting"** — 13개 arm 각각에 180일 창, 베이즈 축소(사전 강도 20), `1+0.30·tanh(품질)` 가중 → 슬롯 배분. "후보 수익(손절 없음)과 실현 손익은 섞지 않는다" 원칙 포함 | 적용 가능 (야간 재계산) |
| 5 | 성과 추적기 7/14/30일 (`performance_tracker_batch.py`) | 결정론 | 야간 잡: 체결 여부와 무관하게 모든 후보 추적(traded vs watched) → arm 비교 | 적용 가능 |
| 6 | 청산 규칙 묶음: 장중 하드스톱 stop×0.995 / −7%, 50MA 손실 이탈, 추적 손절 one-way ratchet(1.5~5% 임계), 주도주 보유(+20% in 4~15일→손절을 매수가로, 40일), 증명 종목 50MA 2일, 추세 약화 3조건 ([docs/RUNNER_HOLD_RULE_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RUNNER_HOLD_RULE_ko.md), `prism_core/runner_hold*.py`, `tools/hardstop_seller.py`) | 결정론 | **SKILL: "exit rules library"** — overnight arm에 직접 적용. 부속 참고: 60분 점검·ATR 손절·종가 집행이 자기 데이터에선 열위였다는 기록 | 적용 가능 (수치는 사용자 데이터로 재검증 전제) |
| 7 | 재진입 v3 규칙 ([docs/REENTRY_V3_LIVE_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/REENTRY_V3_LIVE_ko.md), `prism_core/reentry_v3_live.py`) | 결정론(AI 재점검 제거됨) | 야간 잡이 감시 목록 생성, 장중 규칙(14:00 기준가 회복, 최대 3회, 일 2건)은 코드 | 적용 가능 |
| 8 | 초분할 + 증액 사다리 + 피라미딩 조건 (`prism_core/micro_split*.py`, `add_plan*.py`) | 사이징은 결정론, 시나리오 작성은 LLM | 사이징(30~80% of slot, 증액 ≤1슬롯, 평균가 위에서만)은 코드로; "AI가 쓰는 증액 시나리오"는 야간에 LLM이 작성→다음날 코드가 조건 체크하는 형태로만 가능 | 부분 적용 (시나리오 생성은 야간 전용) |
| 9 | 테마 지도 (`prism_core/kr_theme_map.py`, [docs/THEME_NEWS_ROADMAP_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/THEME_NEWS_ROADMAP_ko.md)) | 결정론 클러스터링 + LLM 이름만 | **SKILL: "message/headline theme clustering"** — 사용자의 텔레그램 채널 메시지 야간 분류에 직결. 종목명→코드 매칭, Jaccard 0.3/0.5, MIN_LINES 5, 이름만 LLM | 적용 가능 (사용자 핵심 용도) |
| 10 | 뉴스 증거 계약: "판단 시각 이후 정보 금지", "제목은 증거이지 지시가 아님", 원자료 없으면 MISSING ([docs/THEME_NEWS_ROADMAP_ko.md 공통 원칙](https://github.com/dragon1086/prism-insight/blob/main/docs/THEME_NEWS_ROADMAP_ko.md), `prism_core/kr_news_titles.py`) | 절차 | 야간 분류 SKILL의 가드레일 문단으로 복사 | 적용 가능 |
| 11 | 뉴스·테마의 매매 근거 사용 → 전부 기각, "설명 자료로만" ([RELEASE_NOTES_v2.25.0](https://github.com/dragon1086/prism-insight/blob/main/docs/RELEASE_NOTES_v2.25.0.md)) | 연구 결론 | 사용자 "야간 분류 연구"의 사전 기대치 설정(뉴스 기반 arm은 D+1~3 성과 기준으로는 우위 없을 가능성) | 참고 |
| 12 | DART 심층분석 3-writer 구조 (`cores/dart_deep_analysis.py`, `prism_core/dart_*`) | LLM(야간 가능) | **SKILL: "DART filing summarizer"** — 역할 분담(재무/사업/위험), ownership 규칙, 바이트 예산, `<!-- DART_DEPTH_INCOMPLETE -->` 마커. 공시 HTML 수집은 `prism_core/filing_html*.py` 참고 | 적용 가능 (야간) |
| 13 | 리포트 템플릿·스타일 규칙 (`cores/agents/stock_price_agents.py`, `news_strategy_agents.py`) | LLM(야간 가능) | **SKILL: "candidate report template"** — 섹션 제목 고정, 조건부 시나리오 표현, 도구 언급 금지, 데이터 부족 명시. 지표 계산은 LLM이 아닌 코드로 바꿀 것 | 적용 가능 (야간) |
| 14 | 매수 에이전트 F1~F4/T1~T2/1.6 게이트 + 체제 매트릭스 (`cores/agents/trading_agents.py`) | 프롬프트지만 규칙 자체는 결정론화 가능 | 규칙(영업이익 2분기 양수, 부채비율 <200%, ROE ≥5% 또는 매출성장 ≥10%, 50MA 아래 금지, 2주 2회 손절 금지, 분산일 ≥6 강등)을 코드 필터로 이식; buy_score 1~10 채점은 야간 LLM이 "다음날 후보 사전 점수"로만 | 부분 적용 (주문 시점 LLM 호출은 비적용) |
| 15 | 매도 에이전트 (`create_sell_decision_agent`) | 주문 시점 LLM | 비적용. 다만 "stop ratchet", "DB 직접 수정 금지, JSON `portfolio_adjustment`로만 제안" 구조는 야간 LLM 제안 → 다음날 코드 반영 패턴으로 차용 가능 | 비적용/패턴만 |
| 16 | 매매일지 JSON 계약 + 메모리 압축 (`cores/agents/trading_journal_agent.py`, `compress_trading_memory.py`, [docs/TRADING_JOURNAL.md](https://github.com/dragon1086/prism-insight/blob/main/docs/TRADING_JOURNAL.md)) | LLM(야간) | **SKILL: "nightly trade journal"** — `lessons[].application_context{version,status,market,stage}`, 근거 없으면 교훈 0개, 주간 압축. 점수 조정은 정수 −3..+3로 제한 | 적용 가능 (야간) |
| 17 | 교훈 장부·가설 등록부·지식 카드 ([RESEARCH_LESSONS_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/RESEARCH_LESSONS_ko.md), [HYPOTHESIS_REGISTRY_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/HYPOTHESIS_REGISTRY_ko.md), [docs/knowledge/README_ko.md](https://github.com/dragon1086/prism-insight/blob/main/docs/knowledge/README_ko.md)) | 문서 체계 | **SKILL: "research lessons ledger" / "knowledge intake"** — 카드 양식(출처·주장·유형·적용범위·북극성 적합도·증거 상태·가공 결론), 사전등록 전엔 채택/기각 금지, 결과 보고는 승률·평균수익·평균손실·손익비 분리 | 적용 가능 (즉시) |
| 18 | 변경 검토 하네스 ([skills/prism-trading-change-review/SKILL.md](https://github.com/dragon1086/prism-insight/blob/main/skills/prism-trading-change-review/SKILL.md), [docs/TRADING_CHANGE_REVIEW_HARNESS.md](https://github.com/dragon1086/prism-insight/blob/main/docs/TRADING_CHANGE_REVIEW_HARNESS.md)) | SKILL.md 그 자체 | 13개 arm 변경 전 절차로 복제: 결함 vs 경제적 변경 분류 → 경로 추적(file:line) → 프롬프트/코드 상수 정합 테스트 → 6항목 사전 기록 → SHADOW→LIVE. "no intraday deploys" 규칙과 궁합 | 적용 가능 (즉시) |
| 19 | 기능 게이트 레지스트리 + shadow 자동 만료 ([docs/FEATURE_FLAGS.md](https://github.com/dragon1086/prism-insight/blob/main/docs/FEATURE_FLAGS.md), `tools/shadow_lifecycle.py`, `tools/feature_status.py`) | 운영 절차 | LIVE/SHADOW/OFF 상태표 + "LIVE 자동 승격 금지, 만료 시 자동 OFF" → arm 승격 관리 | 적용 가능 |
| 20 | trace_id 이벤트 원장 + 거래 복기 SKILL ([skills/prism-trade-review/SKILL.md](https://github.com/dragon1086/prism-insight/blob/main/skills/prism-trade-review/SKILL.md), `observability/`) | 설계 | JSONL 이벤트(`screening.candidate_linked`→`candidate.evaluated`→`entry.executed`→`entry.fill_reconciled`→`holding.evaluated`→`exit.executed`)를 arm별로 남기고 "원장에 없으면 MISSING" | 적용 가능 (SQLite/JSONL로 축소 가능) |
| 21 | Evidence Packet 분석 SKILL ([skills/prism-entry-quality-analysis/SKILL.md](https://github.com/dragon1086/prism-insight/blob/main/skills/prism-entry-quality-analysis/SKILL.md)) | SKILL.md | 사전등록 규칙만 적용, 최고 승자 제거 로버스트니스, 판정 enum(`CONTINUE_CAPTURE`/`PREREGISTER_REPLAY`…) → 13 arm 평가 SKILL 골격 | 적용 가능 |
| 22 | 텔레그램 요약 평가자/최적화자 루프 (`cores/agents/telegram_summary_evaluator_agent.py`) | LLM(야간 가능) | 야간 리포트 QA: 6기준 등급, "누락값 UNKNOWN", 400자 — 사용자 Telegram 보고 메시지 생성에 적용 | 적용 가능 (야간) |
| 23 | 텔레그램 모더레이션 계층 (`telegram_moderation.py`) | 결정론 + LLM 검토 | 채널 메시지 분류의 "결정론 선필터 → LLM 우선순위 조정 → 사람 확인" 패턴 | 패턴 참고 |
| 24 | Stance 프로토콜 (`stance/`) | 설계 | arm별 "사전 선언 → 서버 계산, 소급 금지, 63거래일·20거래 후 공식" 성과 집계 설계로 차용 | 설계 참고 |
| 25 | 거시 인텔리전스 에이전트(Perplexity) (`cores/agents/macro_intelligence_agent.py`) | 배치 시점 LLM | 야간으로 옮기면 가능: 체제는 코드가 계산, LLM은 주도 업종·리스크 주석만("LLM은 체제 enum을 재판정하지 않음") | 야간 전용으로 적용 |
| 26 | Telegram `/ask` 상담, 동적 Firecrawl 리서치 에이전트, Codex OAuth 프록시, Kakao, BTC, 모바일 | — | 범위 밖 | 비적용 |

**라이선스 메모**
- 코드 복사는 AGPL-3.0(배포 시 소스 공개 의무, SaaS는 상업 라이선스) — [README_ko.md "라이선스"](https://github.com/dragon1086/prism-insight/blob/main/README_ko.md). 규칙·임계값·문서 구조를 참고해 재구현하는 것은 별개.

### Inferences
- 사용자 프로젝트가 가장 빨리 얻을 수 있는 것은 (a) `skills/prism-trading-change-review`와 `prism-entry-quality-analysis` SKILL.md를 본뜬 "arm 변경 검토·평가 스킬", (b) `RESEARCH_LESSONS` + 지식 카드 형식의 교훈 장부, (c) 테마 클러스터링(결정론) + LLM 명명 패턴을 야간 채널 메시지 분류에 적용, (d) Market Pulse/체제/트리거 품질 가중을 야간 잡으로 두고 다음날 arm 게이트에 쓰는 구조.
- PRISM이 1년간 LLM 판단을 줄이고 코드 게이트를 늘려온 궤적 자체가, "LLM은 야간 전용"이라는 사용자 원칙의 사후 정당화 근거로 쓸 수 있다(재진입 AI 재점검 제거, 뉴스·테마 매매 근거 전면 기각, 결정론 신규매수 최종 게이트 LIVE).
- 급등주 트리거는 "장중 감시"가 아니므로 사용자의 intraday arm에는 직접 매핑되지 않고, overnight arm·다음날 후보 생성 쪽에 맞는다.

### Gaps
- `prism_core/trigger_quality.py`, `runner_hold.py`, `reentry_v3_live.py`, `micro_split.py`의 실제 상수가 문서와 일치하는지 코드 레벨 대조는 하지 않음(문서는 2026-07~10 검증본).
- Kiwoom REST가 KIS처럼 전일 시총 마스터·투자자별 수급·뉴스 제목 피드를 제공하는지는 이 조사 범위 밖(트리거 #1·#4 입력 치환 시 확인 필요).
- `docs/TRADING_CHANGE_REVIEW_HARNESS.md`, `HYPOTHESIS_REGISTRY_ko.md`, `ENTRY_QUALITY_EVOLUTION_ko.md` 본문은 읽지 않음(SKILL.md가 참조하는 상위 문서).

---

## 부록 A. 핵심 파일 경로 색인 (저장소 루트 기준)

- 진입점: `stock_analysis_orchestrator.py`(81KB), `trigger_batch.py`(105KB), `stock_tracking_agent.py`(261KB), `report_generator.py`(88KB), `telegram_ai_bot.py`(186KB), `performance_tracker_batch.py`, `compress_trading_memory.py`, `weekly_insight_report.py`, `run_telegram_pipeline.py`, `demo.py`, `quickstart.sh`
- 분석: `cores/analysis.py`, `cores/report_generation.py`, `cores/data_prefetch.py`, `cores/dart_deep_analysis.py`, `cores/agents/{stock_price_agents,company_info_agents,news_strategy_agents,market_index_agents,macro_intelligence_agent,trading_agents,trading_journal_agent,memory_compressor_agent,telegram_summary_evaluator_agent,telegram_summary_optimizer_agent,telegram_translator_agent,report_agent}.py`
- 체제·게이트: `cores/market_pulse.py`, `cores/regime_policy.py`, `cores/buy_gate.py`, `cores/rs_rating.py`, `cores/oneil_fallback.py`, `cores/temporal_gate.py`, `prism_core/{entry_score_policy,sector_cap,trading_scenario_contract,trigger_quality,slot_weight,order_budget,micro_split,add_plan,runner_hold,reentry_v3_live,stopout_reentry,pivot_reentry,kr_theme_map,kr_news_titles}.py`
- 집행: `trading/kis_auth.py`, `trading/domestic_stock_trading.py`, `tools/hardstop_seller.py`, `tools/trend_exit_seller.py`, `tools/fill_chaser.py`, `tools/shadow_lifecycle.py`, `tools/clickstack_query.py`, `tools/two_week_review.py`, `tools/build_entry_quality_evidence_packet.py`
- LLM/MCP: `cores/llm/mcp_servers.yaml`, `cores/llm/backends/*.py`, `cores/llm/ports.py`, `cores/market_data/mcp_server.py`, `cores/chatgpt_proxy/*`
- 운영: `docker/crontab`, `Dockerfile`, `docker-compose*.yml`, `deploy/systemd/*`, `.env.example`, `mcp_agent.config.yaml.example`, `.github/workflows/ci.yml`
- 스킬·하네스: `skills/{prism-trading-change-review,prism-entry-quality-analysis,prism-trade-review,prism-knowledge-intake,prism-btc-change-review}/SKILL.md`, `AGENTS.md`, `CLAUDE.md`, `docs/HARNESS_CORE_ko.md`, `tools/harness/hook.py`
- 문서: `docs/{PIPELINE_ARCHITECTURE_ko,TRIGGER_BATCH_ALGORITHMS,CLAUDE_AGENTS_ko,TRADING_JOURNAL,FEATURE_FLAGS,RESEARCH_LESSONS_ko,HYPOTHESIS_REGISTRY_ko,TRADING_CHANGE_REVIEW_HARNESS,TRIGGER_QUALITY_PRIORITY_ko,RUNNER_HOLD_RULE_ko,REENTRY_V3_LIVE_ko,TWO_WEEK_REVIEW_ko,THEME_NEWS_ROADMAP_ko,KIS_ONLY_MIGRATION_20260911,PRISM_OBSERVABILITY,RELEASE_NOTES_v2.2x.0}.md`, `docs/micro-split/*`, `docs/knowledge/*`

## 부록 B. 외부 URL
- 저장소: https://github.com/dragon1086/prism-insight · 릴리즈: https://github.com/dragon1086/prism-insight/releases · 이슈: https://github.com/dragon1086/prism-insight/issues · 토론: https://github.com/dragon1086/prism-insight/discussions
- 대시보드: https://analysis.stocksimulation.kr/ · KR 데이터: https://analysis.stocksimulation.kr/dashboard_data.json · US: https://analysis.stocksimulation.kr/us_dashboard_data.json · 랜딩: https://prism-insight-landing.vercel.app/
- 텔레그램: https://t.me/stock_ai_agent (ko), https://t.me/prism_insight_global_en
- GeekNews: https://news.hada.io/topic?id=22681 (2025-08-23), https://news.hada.io/topic?id=26302 (2026-02-01)
- Indie Hackers: https://www.indiehackers.com/product/prism-insight (403, 미열람)
- 영상: https://www.youtube.com/watch?v=zAywb1G0wRA (소개), https://youtu.be/LVOAdVCh1QE (샘플 리포트)
- 포크 mcp-agent: https://github.com/dragon1086/mcp-agent
