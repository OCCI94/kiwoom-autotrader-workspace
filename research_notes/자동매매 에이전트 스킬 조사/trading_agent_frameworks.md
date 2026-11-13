# 오픈소스 LLM 트레이딩 에이전트 프레임워크 및 한국 자동매매 프로젝트 조사 (2026-11-03 기준)

조사 범위: dragon1086/prism-insight 제외. 모든 GitHub 수치(stars, 최근 commit)는 2026-11-03에 확인한 값. 최근 commit 일자는 GitHub commits atom feed(`/commits/<branch>.atom`)의 첫 `<updated>` 값. "확인된 사실"과 "논문/README의 자기 주장(claim)"을 구분해 표기.

---

## Key Question 1. 글로벌 오픈소스 LLM 트레이딩 에이전트 프레임워크: 구조·데이터·백테스트·결과·라이선스·활동성·오프라인 재사용 부품

### Takeaway
TradingAgents(LangGraph 기반 역할 분담 + bull/bear 토론 + 사후 reflection 메모리 로그)가 사실상 표준 레퍼런스이며 2026-10까지 활발히 개발 중이다. 반면 FinMem(2024-08 이후 중단), StockAgent(시뮬레이션 전용), Alpha-GPT(코드 미공개) 등은 아이디어 소스로만 유효하다. 재사용 가치가 가장 높은 부품은 "라이브 의사결정 로직"이 아니라 **결정 로그→결과 정산(settlement)→2~4문장 reflection** 파이프라인, **5단계 rating + REVIEW(파싱 실패를 Hold로 취급하지 않음)** 규칙, **grid 백테스트(셀 독립, 벤치마크 대비 alpha로 채점)**, FinGPT의 **금융 감성 gold-label 벤치마크(FPB/FiQA-SA/TFNS)**, RD-Agent(Q)의 **가설→구현→백테스트→피드백 루프** 같은 오프라인 리서치 절차다.

### Cited Findings

**TradingAgents (TauricResearch) — 활성, 사실상 표준**
- 구조: Fundamentals/Sentiment/News/Technical 분석가(병렬) → Bull/Bear 리서처 토론(`max_debate_rounds`) → Trader → Risk(aggressive/conservative/neutral debator, `TRADINGAGENTS_MAX_RISK_ROUNDS`) → Portfolio Manager 승인/거절, LangGraph 기반 — [GitHub README](https://github.com/TauricResearch/TradingAgents)
- 데이터 벤더(README 명시): SEC EDGAR(키 불필요), Yahoo Finance, StockTwits, Reddit, FRED(옵션), Alpha Vantage(키 필요), Polymarket, TypeSafe Jev(소셜 포스트 스크리닝, 옵션). 소스 트리 확인: `tradingagents/dataflows/vendors/{alpha_vantage,yahoo,fred,polymarket,reddit,sec_edgar,stocktwits}` — [GitHub README](https://github.com/TauricResearch/TradingAgents); 파일 트리는 codeload tarball(main, 2026-11-03)로 직접 확인
- LLM: OpenAI/Google/Anthropic/xAI/DeepSeek/Qwen/GLM/MiniMax/OpenRouter/Mistral/Kimi/Groq/NVIDIA NIM, Azure/Bedrock, Ollama 및 OpenAI 호환 엔드포인트; `deep_think_llm`(리서치/포트폴리오 매니저)과 `quick_think_llm`(분석가/리서처/트레이더) 이중 티어 — [GitHub README](https://github.com/TauricResearch/TradingAgents)
- 메모리/reflection: 결정마다 메모리 로그에 기록, 이후 실행 시 실현 수익률·alpha로 정산(settle)하고 짧은 reflection 생성; Portfolio Manager가 같은 종목의 최근 결정과 다른 종목의 교훈을 읽음 — [GitHub README](https://github.com/TauricResearch/TradingAgents)
- 구현 파일(소스 직접 확인): `tradingagents/memory/log.py`(append-only markdown, `<!-- ENTRY_END -->` 구분자, 같은 ticker+date 재기록 차단하는 idempotency guard, `memory_log_max_entries` 회전), `tradingagents/memory/settlement.py`(`resolve_benchmark`: `benchmark_ticker` 설정 → 접미사 맵(예: `.T`→^N225) → 기본 SPY; 당일 bar는 아직 거래 중이므로 채점 제외), `tradingagents/memory/reflection.py`(`Reflector._system_prompt`: "정확히 2~4문장 평문, (1) N일 alpha 수치 인용 및 판단 불가면 명시 (2) 테제의 어느 부분이 지지/반박되는지 (3) 다음 유사 분석에 적용할 구체 교훈 1개") — 파일 경로/내용은 tarball에서 확인; 저장소: [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- rating 규칙(소스 확인): `tradingagents/agents/rating.py` — Buy/Overweight/Hold/Underweight/Sell 5단계를 Research Manager·Portfolio Manager·메모리 로그가 공유; 파싱 실패 시 `REVIEW` 반환, "읽을 수 없는 결정은 Hold가 아니다"(이슈 #1170 참조) — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 소셜 포스트 스크리닝(소스 확인): `tradingagents/agents/post_screen.py` — 포스트마다 "이 종목에 관한 글인가", "방향성은" 두 질문을 확률로 답받아 off-topic 확률 0.3 미만만 drop, stance는 신뢰도 0.5 이상만 집계, 요청 실패 시 미스크리닝 상태로 보존하고 블록에 명시 — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 설정(소스 확인): `tradingagents/default_config.py` — `TRADINGAGENTS_*` 환경변수→config 키 매핑 단일 테이블, 잘못된 값(예: `treu`)은 기본값으로 조용히 떨어지지 않고 `ValueError`로 시작 시 실패("unattended run을 조용히 오설정하지 않기 위해") — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 백테스트: `tradingagents backtest NVDA,AAPL --start ... --end ... --every 7`; `run_backtest`/`iter_grid`/`summarize`가 (ticker, date) 격자를 돌리고 지역 벤치마크 대비 실현 alpha로 rating별 채점, 첫 종가 진입, 같은 `run_id` 재실행 시 완료 셀 skip — [GitHub README](https://github.com/TauricResearch/TradingAgents); `tradingagents/backtest.py` docstring: "결정 품질 평가 도구이며 포트폴리오 시뮬레이터가 아니고 그렇게 커지면 안 된다(수량·체결가·현금 원장이 없음), 셀은 독립" (소스 확인)
- 재현성 경고(README): 실행은 비결정적; reasoning 모델은 `temperature`를 거의 무시하므로 재현성이 필요하면 non-reasoning 모델 사용; 라이브 뉴스/소셜 데이터는 시간에 따라 변함; "백테스트 결과가 공개 수치와 일치한다고 보장하지 않음" — [GitHub README](https://github.com/TauricResearch/TradingAgents)
- 라이선스 Apache-2.0, 약 120.2k stars, 최신 릴리스 v0.7.1(2026-10), 최근 commit 2026-10-27 — [GitHub README](https://github.com/TauricResearch/TradingAgents); [commits atom](https://github.com/TauricResearch/TradingAgents/commits/main.atom)
- 논문(arXiv 2412.20138, v7 2025-06-03): 실험 2024-01-01~2024-03-29, AAPL/GOOGL/AMZN, 베이스라인 Buy&Hold/MACD/KDJ&RSI/ZMR/SMA, LLM gpt-4o-mini·gpt-4o(quick)·o1-preview(deep). Table 1: AAPL CR 26.62% / Sharpe 8.21 / MDD 0.91%, GOOGL 24.36% / 6.39 / 1.69%, AMZN 23.21% / 5.60 / 2.11%. 거래비용 명시 없음. 저자 스스로 "3개월 창은 예측당 LLM 11회·툴 20+회 호출 비용 때문", "최고 Sharpe는 경험적 기대 범위를 초과(해당 기간 pullback이 적었음)"라고 인정 — [arXiv HTML v7](https://arxiv.org/html/2412.20138v7); [arXiv abs](https://arxiv.org/abs/2412.20138)

**FinRobot (AI4Finance) — 활성, 플랫폼형**
- V0(AutoGen, 교육용·비프로덕션, 백서 코드), V1(OpenAI Agents SDK, 셀프호스트 equity research 웹앱), V2(PydanticAI, 데스크톱; "32 deterministic compute operators, 7 pipelines, 56 analyst skill playbooks" 주장; Lead orchestrator + Data/Analysis/Modeling/Synthesis/Report + Bull/Bear/Judge 토론 등 9 에이전트), V3(DeepSeek-Harness, 개발 중) — [GitHub README](https://github.com/AI4Finance-Foundation/FinRobot)
- 데이터: V2는 FMP/Finnhub/yfinance/SEC EDGAR/Adanos/NewsAggregator/FX 7개 공급자 failover; V0 `data_source`는 finnhub/finnlp/fmp/sec/yfinance/reddit — [GitHub README](https://github.com/AI4Finance-Foundation/FinRobot)
- 튜토리얼 노트북 5종: annual report(10-K→PDF), FinGPT forecaster, trade strategist(전략 작성+백테스트), mplfinance 멀티모달, SMA-crossover 최적화 — [GitHub README](https://github.com/AI4Finance-Foundation/FinRobot)
- 성과 수치 없음(기능 설명만); 면책: "live trading 권고 아님"; 데스크톱 앱은 Apple notarize 안 됨, 로컬 웹 UI는 브라우저 모드에서 무인증 API — [GitHub README](https://github.com/AI4Finance-Foundation/FinRobot)
- Apache-2.0(+TRADEMARK_POLICY.md), 8.4k stars, 최근 commit 2026-10-24 — [GitHub README](https://github.com/AI4Finance-Foundation/FinRobot); [commits atom](https://github.com/AI4Finance-Foundation/FinRobot/commits/master.atom)

**FinGPT (AI4Finance) — 활성(커밋 기준), 모델 릴리스는 2023-11이 마지막**
- 제공물: LoRA 튜닝 감성 모델 v3.x(Llama2-13B/7B, ChatGLM2-6B), FinGPT-Forecaster(DOW30 기준 학습; 입력=티커·기준일·과거 N주 뉴스·기초 재무 옵션, 출력=분석+다음 주 주가 방향), 멀티태스크(감성/관계추출/헤드라인 분류/NER), FinGPT-RAG, FinGPT-Benchmark — [GitHub README](https://github.com/AI4Finance-Foundation/FinGPT)
- 벤치마크(weighted F1, README 표): FinGPT v3.3 FPB 0.882 / FiQA-SA 0.874 / TFNS 0.903 / NWGI 0.643; GPT-4 0.833 / 0.630 / 0.808 / —; FinBERT 0.880 / 0.596 / 0.733 / 0.538; OpenAI fine-tune 0.878 / 0.887 / 0.883 — [GitHub README](https://github.com/AI4Finance-Foundation/FinGPT) (주의: README의 "GPT-4보다 우수" 주장은 FiQA-SA에서는 OpenAI fine-tune이 더 높아 모든 데이터셋에 성립하지 않음)
- 학습 비용 주장: LoRA/QLoRA, RTX 3090 1장, "런당 $300 미만" — [GitHub README](https://github.com/AI4Finance-Foundation/FinGPT)
- MIT, 21.5k stars, 최근 commit 2026-10-25, "What's New" 최신 항목은 2023-11 모델 — [GitHub README](https://github.com/AI4Finance-Foundation/FinGPT); [commits atom](https://github.com/AI4Finance-Foundation/FinGPT/commits/master.atom)

**FinMem (pipiku915) — 비활성(최근 commit 2024-08-18)**
- 구조: Profiling / Memory(working memory + 3층 장기기억) / Decision. 검색 점수 = recency(e^(−δ/Q), Q=14/90/365일) + relevancy(cosine, text-embedding-ada-002) + importance(v∈{40,60,80}×α^δ, α=0.9/0.967/0.988); recency<0.05 또는 importance<5이면 purge; 결정에 인용된 메모리는 importance +5 및 상위 층으로 승격 — [arXiv HTML 2311.13743v2](https://arxiv.org/html/2311.13743v2)
- 캐릭터: risk-seeking / risk-averse / self-adaptive(단기(예: 3일) 누적수익 <0이면 위험회피로 전환) — [arXiv HTML](https://arxiv.org/html/2311.13743v2)
- 실험: TSLA/NFLX/AMZN/MSFT/COIN, 학습 2021-08-17~2022-10-05, 테스트 2022-10-06~2023-04-10, 데이터 Yahoo 가격·Alpaca News(Benzinga)·SEC 10-K/10-Q, GPT-4-Turbo temp 0.7, K=5, 5회 평균. 결과(CR/Sharpe/MDD): TSLA 61.78%/2.68/10.80% vs B&H −18.63%/−0.54/55.32%; NFLX 36.45%/2.02/15.85% vs 35.51%/1.41/20.93%; AMZN 4.89%/0.23/22.93%; MSFT 23.26%/1.44/14.99%; COIN 34.98%/0.72/35.75% — [arXiv HTML](https://arxiv.org/html/2311.13743v2)
- 중요한 caveat(논문 자체 기술): 종목별 보고된 risk 설정은 "테스트 기간 누적수익이 가장 높은 것"을 택함(테스트 결과가 선택에 반영됨); Llama2-70b-chat은 TSLA에서 −52.72%; GPT-3.5-Turbo 16.15%, davinci-003 1.63% — [arXiv HTML](https://arxiv.org/html/2311.13743v2)
- 코드(소스 트리 확인): `puppy/memory_functions/{recency,decay,importance_score,compound_score,access_counter}.py`, `puppy/prompts.py`(JSON 출력 강제, 근거 메모리 id 요구), `puppy/reflection.py`(guardrails `ValidChoices`로 id 검증), `data-pipeline/08-Wilcoxon-Test.py`(두 전략의 누적 log-return 시계열에 Wilcoxon signed-rank), `data-pipeline/07-metrics.py`, `config/tsla_{gpt,gemini,tgi}_config.toml`; `run.py sim-checkpoint`는 "OpenAI API 불안정" 때문에 존재 — [GitHub README](https://github.com/pipiku915/FinMem-LLM-StockTrading)
- MIT, 969 stars, 최근 commit 2024-08-18, README에 커밋된 `.env` 존재 경고, 스크립트명 불일치 — [GitHub README](https://github.com/pipiku915/FinMem-LLM-StockTrading); [commits atom](https://github.com/pipiku915/FinMem-LLM-StockTrading/commits/main.atom)

**StockAgent (MingyuJ666) — 시장 시뮬레이션, 실데이터 백테스트 없음**
- LLM 트레이더 다수가 가상 종목 A/B/C/D(B는 신규 상장), 게시판, 대출(1/2/3년 금리) 환경에서 거래; Initial/Trading/Post-Trading/Special Events 4단계; gemini-pro 기본, GPT 옵션 — [GitHub README](https://github.com/MingyuJ666/Stockagent); 프롬프트는 `prompt/agent_prompt.py`(procoder NamedBlock, JSON 출력) — 소스 트리 확인
- 논문 arXiv 2407.18957; 실시장 백테스트는 없고 "test-set leakage 방지 설계" 주장 — [arXiv abs](https://arxiv.org/abs/2407.18957); [GitHub README](https://github.com/MingyuJ666/Stockagent)
- 714 stars, 최근 commit 2026-06-16, 라이선스 페이지 미표시 — [GitHub README](https://github.com/MingyuJ666/Stockagent); [commits atom](https://github.com/MingyuJ666/Stockagent/commits/main.atom)

**QuantAgent (Y-Research-SBU) — 활성, 차트 이미지 기반**
- Indicator(RSI/MACD/Stochastic 등 5개) / Pattern(차트 형태 매칭) / Trend(추세선 채널 주석) / Decision(LONG/SHORT + 진입·청산·손절) 4 에이전트, LangGraph, yfinance 최근 30봉, 1분~일봉; "이미지 입력 가능한 LLM 필수" — [GitHub README](https://github.com/Y-Research-SBU/QuantAgent)
- README에 백테스트 방법·수치 없음(논문 arXiv 2509.09995 "QuantHarness"); MIT, 2.9k stars, 최근 commit 2026-08-18 — [GitHub README](https://github.com/Y-Research-SBU/QuantAgent); [commits atom](https://github.com/Y-Research-SBU/QuantAgent/commits/main.atom)
- 주의: `fscheu/QuantAgent`는 동명의 제3자 구현이며 공식 아님 — [검색 결과](https://github.com/fscheu/QuantAgent)

**Alpha-GPT / 알파 팩토리 계열**
- Alpha-GPT(arXiv 2308.00016, v2 2025-09-20, EMNLP 2025 System Demo): LLM 프롬프트 엔지니어링으로 리서처 아이디어를 알파 식으로 변환하는 human-AI interactive 알파 마이닝; 공식 코드 저장소 미확인 — [arXiv abs](https://arxiv.org/abs/2308.00016); awesome list에도 코드 링크 없음 — [Awesome-LLM-Quantitative-Trading-Papers](https://github.com/Tom-roujiang/Awesome-LLM-Quantitative-Trading-Papers)
- RD-Agent(Q)(arXiv 2505.15155, NeurIPS 2025, v2 2025-09-25): Research(목표→가설→태스크) / Development(Co-STEER 코드 생성→백테스트) / Feedback 루프 + multi-armed bandit 스케줄러로 팩터·모델 공동 최적화; 저자 주장 "고전 팩터 라이브러리 대비 최대 2배 연환산 수익, 팩터 70% 적게" — [arXiv abs](https://arxiv.org/abs/2505.15155); 코드 microsoft/RD-Agent 최근 commit 2026-09-23 — [commits atom](https://github.com/microsoft/RD-Agent/commits/main.atom); 후속 논문 FaVOR의 비교표에서는 RD-Agent-Quant가 일부 설정에서 음수·비LLM 베이스라인 이하로 나타남(독립 재현 아님, 표 전체 미검증) — [FaVOR arXiv PDF](https://arxiv.org/pdf/2608.30192)
- 관련 공개 코드가 있는 팩터 마이닝: QuantaAlpha(SUFE, 2026-02), AlphaQuanter(HKUST, 2025-10), Alpha-R1(Finstep, 2025-12) — [Awesome-LLM-Quantitative-Trading-Papers](https://github.com/Tom-roujiang/Awesome-LLM-Quantitative-Trading-Papers)

**ai-hedge-fund (virattt) — 가장 많은 star, 교육용**
- 투자자 페르소나 에이전트 기반 PoC; 데이터 Financial Datasets API(또는 x402로 호출당 과금); `aihf` 터미널 앱에서 백테스트 실행, 결과 `~/.hedge-fund/research/` 저장; "실제로 거래하지 않음, 교육·연구 목적"; MIT, 64.5k stars, 최근 commit 2026-11-02 — [GitHub README](https://github.com/virattt/ai-hedge-fund); [commits atom](https://github.com/virattt/ai-hedge-fund/commits/main.atom)

**백테스팅 보조/래퍼 계열**
- KIS 공식 저장소에 `strategy_builder/`(80개 지표, 10개 프리셋 전략, BUY/SELL/HOLD 시그널, Node.js 18+), `backtester/`(Docker 기반 QuantConnect Lean, HTML 리포트), `MCP/`(KIS Code Assistant·Trading MCP) 포함 — [koreainvestment/open-trading-api](https://github.com/koreainvestment/open-trading-api)
- Kiwoom 공식 저장소에 `mcp_exec/`, `mcp_spec/`(AI 에이전트용 MCP 서버), `mcpb/`(Claude Desktop 번들) 포함 — [Kiwoom-Securities/Kiwoom-REST-API](https://github.com/Kiwoom-Securities/Kiwoom-REST-API)
- 생태계 지도: ContestTrade(FinStep-AI, 2025-08), TradeTrap(2025-12, 코드 있음), DeepFund(HKUST, NeurIPS 2025, live arena), AI-Trader(HKU, 2025-12), Trading-R1(Tauric, 2025-09), FinMCP-Bench(2026-03), QuantCode-Bench(2026-04) — [Awesome-LLM-Quantitative-Trading-Papers](https://github.com/Tom-roujiang/Awesome-LLM-Quantitative-Trading-Papers)
- 서베이(arXiv 2605.19337, 2026-05-19, 77편 중 폐루프 19편): 19편 중 시간 일관 split 추출 가능 2편, 명시적 거래비용 모델 1편, 유니버스/생존편향 처리 1편, 실행 타이밍 보고 11편, 재현성 R0 등급 15편(R3 없음) — "protocol incomparability" — [arXiv HTML 2605.19337v1](https://arxiv.org/html/2605.19337v1)

### Inferences
- TradingAgents의 가치는 "토론 그래프"가 아니라 주변 인프라(메모리 로그·정산·reflection·rating·fail-loud config·grid 평가)에 있다. 규칙 기반 봇에는 LangGraph 토론부를 통째로 빼고 이 주변부만 Python 모듈로 이식하는 것이 합리적이다.
- 논문 결과(TradingAgents 3개월·Sharpe 5~8, FinMem 6개월·5종목·테스트셋으로 risk 설정 선택)는 모두 짧은 창·소수 종목·비용 미반영이며, 이를 "LLM이 알파를 낸다"의 근거로 쓰면 안 된다(Q3 참조).
- FinGPT의 가치는 Forecaster가 아니라 **gold-label 벤치마크 관행**(FPB/FiQA-SA/TFNS에 weighted F1로 비교)이다. 야간 분류기의 품질 게이트를 같은 형식(라벨셋 고정, F1 추적)으로 만드는 것이 바로 적용 가능하다.
- RD-Agent(Q)의 "가설→코드→백테스트→피드백 + bandit" 구조는 로드맵 5단계(가설 파이프라인)의 설계 레퍼런스가 되지만, 독립 비교(FaVOR)에서 결과가 흔들리므로 자동 채택보다 "후보 생성기"로 한정하는 것이 안전하다.

### Gaps
- Alpha-GPT 공식 코드 저장소를 찾지 못함(논문·awesome list 모두 링크 없음).
- TradingAgents README가 말하는 `backtest`·`portfolio` 디렉터리의 상세 동작(수수료·슬리피지 모델)은 README·docstring 범위 밖; docstring은 명시적으로 "포트폴리오 시뮬레이터 아님"이라 비용 모델이 없을 가능성이 높으나 미검증.
- FinRobot V2의 "32 deterministic operators / 56 playbooks"는 README 주장만 확인, 내용 미열람.
- RD-Agent(Q) 논문의 시장·기간·IC·IR 수치는 abs 페이지에 없고 검색 요약(IC≈0.0532, ARR≈14.21%, IR≈1.74)은 Microsoft 중국어 요약 인용이라 미검증.

---

## Key Question 2. 한국 오픈소스 Kiwoom/KIS 자동매매 프로젝트: 유지보수 상태, 커넥터, 세션/호가/VI 관행

### Takeaway
2026-11 기준 활발히 유지되는 축은 **공식 저장소 2개**(Kiwoom-Securities/Kiwoom-REST-API 2026-09-15, koreainvestment/open-trading-api 2026-10-27)와 **데이터 라이브러리 3개**(pykrx 2026-10-29, OpenDartReader 2026-08-10, FinanceDataReader 2026-05-13), 커뮤니티 래퍼 younghwan91/kiwoom-client(2026-10-18, 호가단위·TR별 토큰버킷·401/429 재시도 내장)이다. 개인 자동매매 봇(KIS_Auto_Trader, quant-trading-rest, stock-analyzer)은 star 0~9이지만 risk_manager/circuit breaker, mock-only 환경변수 게이트, 08:00~09:00 주문 금지창 같은 운영 관행을 담고 있다. 세션·VI·호가단위 규칙은 어떤 저장소도 체계적으로 코드화하지 않았고(README에 "미언급"이 대부분), 증권사 NXT 안내 페이지·뉴스가 1차 소스다.

### Cited Findings

**공식 저장소**
- Kiwoom-Securities/Kiwoom-REST-API: `kiwoom/`(OAuth·REST·WebSocket, 337개 API 전수), `examples/` 362개 Python(OAuth 2, 국내주식 226, 미국주식 134), Postman 306 HTTP API×PRD/MOCK 환경(612 요청), WebSocket 31개는 Python 예제만, `mcp_exec/`·`mcp_spec/`·`mcpb/`; Python 3.13+, uv; 운영/모의투자 키 별도 발급; CLI `kwcli`(PyPI, 명령 `kiwoomcli`)는 OS 자격증명 저장소(Keychain/Credential Manager/Secret Service)에 키 저장, 저장 전 read-only 조회로 연결 검증, 계좌 영향 명령에 `--confirm` 플래그 요구; `.env` fallback(`KIWOOM_MODE`, `APP_KEY`, `APP_SECRET`, `APP_KEY_MOCK`, `APP_SECRET_MOCK`); 예외 `CredentialsNotFoundError`/`ModeNotConfiguredError`/`KeyringUnavailableError`; rate limit·호가단위·VI·동시호가·시간외 **언급 없음**; 74 stars, 최근 commit 2026-09-15 — [GitHub README](https://github.com/Kiwoom-Securities/Kiwoom-REST-API); [commits atom](https://github.com/Kiwoom-Securities/Kiwoom-REST-API/commits/main.atom)
- koreainvestment/open-trading-api: `examples_llm/`(API 함수당 1파일 + `chk_*.py` 검증 파일), `examples_user/`(카테고리별 `_functions.py`/`_examples.py`, `_ws` 변형), `kis_auth.py`(토큰 발급·관리, `svr="prod"`/`"vps"` 전환, WebSocket), `kis_devlp.yaml`(실전 `my_app/my_sec`, 모의 `paper_app/paper_sec`, 모의 계좌번호 별도); "토큰 재발급 - 1분당 1회", 초당 거래건수 초과 시 `EGW00201`, "모의투자 계좌는 REST API 호출 제한이 낮음", 파라미터 최적화 등 연속 호출은 실전계좌 권장; `strategy_builder/`, `backtester/`(Lean), `MCP/`; 1.7k stars, 최근 commit 2026-10-27 — [GitHub README](https://github.com/koreainvestment/open-trading-api); [commits atom](https://github.com/koreainvestment/open-trading-api/commits/main.atom)

**커뮤니티 Kiwoom REST 래퍼**
- younghwan91/kiwoom-client: REST 182 엔드포인트/15 모듈 + 조건검색 4 + 실시간 19 타입(히어로 이미지는 23 WebSocket으로 표기돼 불일치), `KiwoomAPI`/`AsyncKiwoomAPI`, 토큰 첫 호출 발급·만료 전 갱신·401 시 재발급 후 재시도, WebSocket PING/재접속/구독 복원, `request_all()` 연속조회, `to_number()/normalize()/to_dataframe()`, MCP 서버(`kiwoom-client-mcp`); `is_mock=True`→`mockapi.kiwoom.com`, 모의/실전 키 불일치 시 인증 실패, **모의투자는 KRX만 지원**; rate limit은 TR(api_id)별로 적용되며 실측 지속 약 1 req/s·버스트 약 2, 기본 TR별 토큰버킷, 429 시 backoff 재시도(`rate_limit`, `rate_burst`, `max_retries`); `tick_size(price)`·`round_to_tick(price)`(예: 70123→70100) 제공, "지정가는 호가단위 배수여야 하며 아니면 거부되거나 의도치 않은 가격에 체결"; MCP 주문 도구는 모의에서는 항상, 실전에서는 `KIWOOM_MCP_ALLOW_LIVE_ORDERS=true`일 때만 등록; 저자 명시 caveat: WebSocket REAL 프레임 필드명(`item`/`values`)은 장 마감 중 테스트라 미확인; 거래일 캘린더·시간외 **미언급**; MIT, 2 stars, 최근 commit 2026-10-18 — [GitHub README](https://github.com/younghwan91/kiwoom-client); [commits atom](https://github.com/younghwan91/kiwoom-client/commits/main.atom)
- 기타 PyPI 래퍼(미검증): pykiwoom-rest, kiwoom-rest-trade(httpx 비동기), kiwoompy, bamjun/kiwoom-rest-api — [PyPI kiwoom-client](https://pypi.org/project/kiwoom-client/); [PyPI pykiwoom-rest](https://pypi.org/project/pykiwoom-rest/); [PyPI kiwoom-rest-trade](https://pypi.org/project/kiwoom-rest-trade/); [bamjun/kiwoom-rest-api](https://github.com/bamjun/kiwoom-rest-api)
- 토큰 운영(블로그, 2026-08-16): 공식 가이드 인용분 — `au10001 POST /oauth2/token`(요청 `grant_type=client_credentials, appkey, secretkey`; 응답 `expires_dt, token_type, token, return_code, return_msg`, 토큰 키는 `token`), `au10002 POST /oauth2/revoke`, 호스트 `https://api.kiwoom.com` / `https://mockapi.kiwoom.com`; `expires_dt` 예시 `"20241107083713"`(14자리, 블로그 해석 KST 절대시각), 스펙에 유효기간(시간) 명시 없음("24시간"은 관측치). 블로그 자체 권고: 120초 마진으로 선갱신, UTC 서버의 9시간 오판 주의, 인증 오류 시 1회만 재발급·재시도, 여러 프로세스가 앱키를 공유할 때 종료 시 자동 revoke 금지(한 프로세스의 revoke가 다른 프로세스 토큰을 무효화한 실제 사고) — [AlgoLab 블로그](https://algolab.co.kr/blog/kiwoom-rest-api-token-expiry-revoke-2026)

**개인 자동매매 봇**
- CyberFlower/quant-trading-rest: signal condition을 gate(0~1개, Order UI 수량과 동기) + entry(1개 이상) 체인으로 조합, 마지막 entry가 trader에 바인딩; Kiwoom(국장)+KIS(미국장) REST; `order/<broker>/condition_profiles.toml`(종목별), `trading_profiles.toml`(run profile→키/주문파일), 주문 입력은 `.xlsx`; crontab 일일 운영, 세션별 별도 프로세스(국내 주간/미국 야간); **국내 08:00~09:00은 가격 갱신만 하고 주문 없음**; 요청 간 `sleep` 권고(과호출 시 끊김); `exchange_calendars` 의존성 포함(용도 설명 없음); 호가단위는 `order/README.md` 항목으로 존재(내용 미노출); 백테스트·SQLite·Telegram·손절 **미언급**; Apache-2.0, 9 stars, 최근 commit 2026-07-19 — [GitHub README](https://github.com/CyberFlower/quant-trading-rest); [commits atom](https://github.com/CyberFlower/quant-trading-rest/commits/main.atom)
- dalpan-cyber/KIS_Auto_Trader: 라오어 무한매수법 v2.0 전담, GitHub Actions 무인 실행(`run_infinite_buying.py`) + Streamlit 대시보드(`app.py`) + Google Sheets 원장(`google_sheet_manager.py`) + `telegram_notifier.py`(옵션, 환경변수); 앱 내 스케줄러 09:00 손절 점검 / 5분 장중 점검 / 15:10 스크리닝 / 15:15 매수 / 15:30 정산(대시보드 프로세스 의존); `risk_manager.py` 손절 −3%·익절 +7%·트레일링 −2%·연속 주문/API 오류 시 circuit breaker; `kis_api.py` OAuth2 + rate limiter; `settings.json`의 `mock_trading`을 `MOCK_TRADING_ONLY` 환경변수가 override, 사이드바에 현재 모드 표시, AppKey 없이 도는 simulation 모드; `tests/`는 외부 연결 차단·상태파일 격리, `scripts/`는 실제 KIS 호출이라 수동 실행; README 내부 불일치(전략 목록 vs 파일표); 0 stars, 238 commits, 최근 commit 2026-10-16, 라이선스 미표시 — [GitHub README](https://github.com/dalpan-cyber/KIS_Auto_Trader); [commits atom](https://github.com/dalpan-cyber/KIS_Auto_Trader/commits/main.atom)
- ryu9634/stock-analyzer: 미국·한국 급등 후보 일일 분석(거래량 급증·MA 정배열·RSI/MACD/볼린저·모멘텀, PER/PBR/매출성장/영업이익률/부채비율, 쌍바닥·변동성 squeeze·연속 양봉) → Telegram(`TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` secrets) + GitHub Pages(`data/analysis.json`, `data/history.json`); `analyze.yml`/`deploy.yml`; 데이터 소스·LLM 사용·**과거 추천 성과 검증 없음**; 0 stars, 345 commits, 최근 commit 2026-11-03 — [GitHub README](https://github.com/ryu9634/stock-analyzer); [commits atom](https://github.com/ryu9634/stock-analyzer/commits/main.atom)
- 기타 Telegram 알림 프로젝트(미열람): yjongsukook/stock-alert(보유·관심 종목 급변 + 09:00 브리핑), fbqweasd/stock_telegram_bot(20선 이탈·볼린저·RSI 이벤트, 표준 라이브러리만) — [검색 결과](https://github.com/yjongsukook/stock-alert); [검색 결과](https://github.com/fbqweasd/stock_telegram_bot)
- KIS 라이브러리: Soju06/python-kis 최근 commit 2025-10-13 — [commits atom](https://github.com/Soju06/python-kis/commits/main.atom); pjueon/pykis 최근 commit 2022-09-03(**비활성**) — [commits atom](https://github.com/pjueon/pykis/commits/main.atom); kis-trader(PyPI)는 CLI `kis` + MCP 서버/스킬 제공 주장 — [PyPI kis-trader](https://pypi.org/project/kis-trader/)
- CountJung/KISAutoTrade: Rust/Tauri v2 + React 18 데스크톱, Discord 알림(Telegram 아님), MA 전략 토글 — [검색 결과](https://github.com/CountJung/KISAutoTrade)

**데이터 라이브러리**
- sharebook-kr/pykrx: KRX/Naver 스크래핑; 티커 목록·OHLCV(일/월/연, 수정주가)·시가총액·DIV/BPS/PER/EPS/PBR·투자자별 거래실적(순매수 상위 포함)·외국인 소진율·지수 구성종목·공매도(잔고·상위 50)·ETF/ETN/ELW(PDF·NAV 괴리·추적오차)·국채수익률·`get_stock_major_changes`; Python ≥3.10; KRX 로그인 API는 `KRX_ID`/`KRX_PW` 필요; **종목별 요청 간 1초 지연 권고(과호출 시 KRX 차단)**; 당일 투자자별 최종치는 18시 이후 공표, 공매도는 약 T+2 지연, 외국인 보유는 장시작 스냅샷(D-2 유효, D-1은 0일 수 있음), 비거래일·상폐 종목 종가 0 가능; 약 1.1k stars, 최근 commit 2026-10-29, 라이선스 페이지 미표시 — [GitHub README](https://github.com/sharebook-kr/pykrx); [commits atom](https://github.com/sharebook-kr/pykrx/commits/master.atom)
- FinanceData/OpenDartReader: `list(corp,start,end,kind,final)`(기본 1년), `document(rcept_no)`/`document_all`, `company`/`company_by_name`, `finstate`/`finstate_all`/`finstate_xml`, `major_shareholders`/`major_shareholders_exec`, `report(corp,item,year)`, `attach_files`/`download`; `DART_API_KEY`; `list_date_ex`는 당일 공시 목록을 **시각 포함**으로 반환; `final=False`로 정정 공시 포함; 일 호출 제한 README 미언급; MIT, 478 stars, 최근 commit 2026-08-10 — [GitHub README](https://github.com/FinanceData/OpenDartReader); [commits atom](https://github.com/FinanceData/OpenDartReader/commits/master.atom)
- FinanceData/FinanceDataReader: 최근 commit 2026-05-13 — [commits atom](https://github.com/FinanceData/FinanceDataReader/commits/master.atom); 조직 페이지 — [FinanceData](https://github.com/financedata)

**세션·호가단위·VI 규칙(1차 소스: 증권사 안내·뉴스)**
- NXT(넥스트레이드) 2025-03-04 출범, 일부 종목 08:00~20:00 12시간 거래 — [한국경제 2025-03-03](https://www.hankyung.com/article/2025030395371)
- 신한투자증권 NXT 안내: 프리마켓 08:00~08:50(지정가·최유리·최우선), 휴장 08:50~09:00:30(취소만), 메인마켓 09:00:30~15:20(지정가·최유리·최우선·시장가 IOC/FOK·중간가·스톱지정가), 휴장 15:20~15:30(취소만; SOR은 KRX 지정가로 배분), 애프터마켓 시가단일가 15:30~15:40(지정가만), 애프터마켓 15:40~20:00(지정가·최유리·최우선); NXT는 일반 시장가 미접수; GTP 미체결은 08:50 자동취소; KRX 시가 예상체결가 노출 08:40→08:50~09:00로 단축; KRX 정규장 미체결은 마감과 함께 소멸(애프터마켓 이월 없음), NXT 주문은 애프터마켓까지 유지; **KRX 시간외단일가(16:00~18:00) 종료, 애프터마켓으로 대체**(페이지 주장, 일자 미기재); 수수료 메이커 0.0013%/테이커 0.0018%(KRX 0.0023%); 가격제한폭 ±30%(KRX 동일); 정적 VI(NXT 신규) 2분 단일가(기준은 페이지 내 "기준가 대비 일정 비율"과 비교표 "직전 단일가 체결가 ±10%"가 혼재), VI 중 지정가·스톱지정가만 가능, 동적 VI는 KRX와 동일(직전가 ±3~6%)·2분 단일가; CB 1·2단계 30분 중단 후 30초 단일가, 3단계 장 종료; SOR 배분표(08:00~08:50 NXT 가능종목만, 08:50~09:00:30 KRX, 09:00:30~15:20 유리한 곳, 15:20~15:30 KRX, 15:30~16:00 NXT, 16:00~20:00 유리한 곳); 호가단위·결제 **미기재** — [신한투자증권 NXT 안내](https://open.shinhansec.com/mobilealpha/html/CS/NXTPolicyGuide.html)
- NXT VI 제도 변경 보도: 정적 VI는 직전 단일가·기준가 대비 ±10%에서 발동, 2분간 호가를 모아 단일가로 결정; 동적 VI도 기존 2분 정지에서 단일가 결정으로 변경; 시행일은 기사마다 상이("9월 14일" vs "내달 14일") — [디지털타임스](https://www.dt.co.kr/article/12081241); [네이트뉴스 2026-08-31](https://m.news.nate.com/view/20260831n25393)
- Kiwoom 공식 대체거래소 안내 책자(PDF, 미열람) — [Kiwoom NXT 가이드 PDF](https://ocrw.kiwoom.com/pdf/guide/Kiwoom_Securities_Alternative_Exchange_Guide_Booklet_Single-Sided.pdf)
- KRX 호가가격단위 개편(2022-11-01 기사, 2023-01 시행 예고): 1,000~2,000원 5→1원, 2,000~5,000원 5원 유지, 10,000~20,000원 50→10원, 20,000~50,000원 50원 유지, 100,000~200,000원 500→100원, KOSPI/KOSDAQ/KONEX 통일; 1,000원 미만·5,000~10,000·50,000~100,000·200,000 이상 구간은 기사에 없음 — [다음뉴스](https://v.daum.net/v/qGS7P0BEoG)

### Inferences
- "세션 캘린더·호가단위·VI·동시호가" 레퍼런스 스킬은 기존 저장소에서 복사할 수 없다. kiwoom-client의 `tick_size/round_to_tick`(코드), 신한 NXT 안내(세션표·SOR·VI), 다음뉴스 호가단위 기사(부분)를 합쳐 직접 작성해야 하며, 구간 누락(1,000원 미만, 5,000~10,000 등)과 NXT 호가단위는 KRX/Kiwoom 공식 문서로 메워야 한다.
- 모의투자 환경의 함정 3가지가 반복 등장한다: (a) 모의/실전 키 분리·불일치 시 인증 실패(Kiwoom 공식, kiwoom-client), (b) 모의는 KRX만(kiwoom-client) → NXT/애프터마켓 전략은 모의로 검증 불가, (c) 모의 호출 제한이 더 낮음(KIS). 로드맵 1단계(운영 안정성)의 체크리스트 항목이 된다.
- 대상 봇의 "동시호가 매수→익일 시가 매도" arm은 KRX 15:20~15:30 종가단일가와 NXT 애프터마켓(15:30~)이 겹치는 시간대이므로, 주문 라우팅(KRX 전용 vs SOR)과 "KRX 정규장 미체결 소멸" 규칙을 ledger에 명시적으로 기록해야 사후 분석이 가능하다.
- 개인 봇들의 공통 관행(실전 주문 게이트를 환경변수로 2중화, 연속 오류 circuit breaker, 08:00~09:00 주문 금지창, 요청 간 sleep, 1분 1회 토큰 재발급 제한)은 그대로 deploy-gate 스킬 항목으로 옮길 수 있다.

### Gaps
- Kiwoom REST API 공식 rate limit 수치, 토큰 유효기간, 모의투자에서 미지원되는 TR 목록, NXT 주문 시 거래소 구분 파라미터는 공개 저장소·검색에서 확인 불가(포털 로그인 필요로 추정). .NET 래퍼 문서의 "초당 20회"와 kiwoom-client 실측 "약 1 req/s"가 충돌 — 직접 실측 필요.
- 한국 공개 저장소 중 DART 기반 이벤트 스터디(공시→수익률) 구현체, 과거 추천 성과를 백테스트하는 급등주 탐지기는 찾지 못함(stock-analyzer는 성과 검증 없음).
- KRX 호가단위 최종 확정표(전 구간)와 시행일 공식 공지는 미확인(기사는 2022-11 예고 단계).
- FinanceDataReader README는 미열람(활동 일자만 확인).
- quant-trading-rest의 `order/README.md`(호가단위 항목)와 `exchange_calendars` 사용 방식 미열람.

---

## Key Question 3. LLM 트레이딩 에이전트의 성과와 실패 모드에 대한 공개 증거 (뉴스/공시 분류 신호 포함)

### Takeaway
2025~2026년 독립 평가는 일관되게 "짧은 창·소수 종목에서 보고된 LLM 에이전트 우위는 장기·광범위 평가에서 크게 줄고(FINSABER), 라이브에서는 프레임워크마다 극단적으로 다르며 Buy&Hold를 체계적으로 이기지 못하고(AMA), 구성요소 하나의 작은 교란이 과도 집중·노출 폭주로 번진다(TradeTrap)"고 보고한다. 반면 뉴스 헤드라인→방향 분류 자체는 신호가 있다는 증거(Lopez-Lira & Tang, FinGPT 벤치마크)가 있으나 look-ahead/암기 편향 통제가 전제이며, 비용·지연 반영 시 수익성은 별도 문제다.

### Cited Findings
- **라이브 벤치마크 AMA(When Agents Trade, arXiv 2510.11695 v2 2025-10-30)**: 2025-08-01~09-30 라이브(워밍업 05-01~07-31), TSLA/BMRN/BTC/ETH, 4 프레임워크(InvestorAgent/TradeAgent/HedgeFundAgent/DeepFundAgent)×5 백본(GPT-4o, GPT-4.1, Gemini-2.0-flash, Claude-3.5-haiku, Claude-sonnet-4), 일 1회 BUY/SELL/HOLD, temp 0.5. Buy&Hold: TSLA CR 46.88% / SR 6.00, BMRN −6.89%, BTC 0.66%, ETH 18.56%. TSLA에서 어떤 에이전트도 B&H CR을 못 넘음(InvestorAgent GPT-4.1 40.83%, SR 6.47로 Sharpe만 우위); TradeAgent GPT-4.1 TSLA −38.72%; HedgeFundAgent TSLA −29.15%·BTC −9.09%·ETH +39.66%; HedgeFundAgent는 5개 백본 모두 **동일한 수치**(논문 설명 없음); 전 에이전트가 9월 말 BTC 숏스퀴즈를 놓침; 뉴스 요약의 날짜 정확도 87.5%·커버리지 92.5%(20일 표본); 저자 결론 "백본보다 프레임워크가 결과를 더 좌우"; 2개월·4자산·구성당 1회 실행, 유의성·분산 미보고 — [arXiv HTML 2510.11695v2](https://arxiv.org/html/2510.11695v2); [arXiv abs](https://arxiv.org/abs/2510.11695)
- **장기 백테스트 FINSABER(arXiv 2505.07078, v6 2026-06-26, KDD '26)**: 20년·100+ 종목으로 타이밍 전략을 재평가하면 기존 보고된 LLM 우위가 "deteriorate significantly"; LLM 전략은 강세장에서 과도하게 보수적(패시브 대비 열위), 약세장에서 과도하게 공격적(큰 손실); 권고는 "구조 복잡도 확장보다 추세 탐지·regime-aware 위험 통제"; FinAgent(GPT-4o-mini) 결과 정정 이력 — [arXiv abs 2505.07078](https://arxiv.org/abs/2505.07078)
- **견고성 TradeTrap(arXiv 2512.02261, 2025-12-01)**: market intelligence / strategy formulation / portfolio·ledger / execution 4개 구성요소에 통제된 시스템 수준 교란을 가해 미국 주식 폐루프 백테스트; 한 구성요소의 작은 교란이 결정 루프를 타고 전파되어 adaptive·procedural 양쪽에서 극단적 집중·노출 폭주·대규모 드로다운; 코드 공개 — [arXiv abs 2512.02261](https://arxiv.org/abs/2512.02261); [Yanlewen/TradeTrap](https://github.com/Yanlewen/TradeTrap); 후속 논문은 TradeTrap이 white-box 접근을 전제해 실제 공격자 능력을 과대평가한다고 지적 — [arXiv PDF 2608.24069](https://arxiv.org/pdf/2608.24069)
- **평가 편향 프레임워크(arXiv 2602.14233, 2026-02-15; Oxford·UNIST·UF·Booth·BlackRock 등)**: 5가지 편향 — look-ahead(파라메트릭 누출+검색 누출), survivorship, narrative(근거 없는 일관된 스토리; LLM 요약이 중립/부정 텍스트를 긍정으로 이동시킨다는 Alessa et al. 인용), objective(기권보다 자신감 보상), cost(거래비용·추론비용·지연 무시). 2023~2025 논문 164편 중 look-ahead 언급 26.8%, survivorship 1.2%, 어떤 편향도 28% 초과 언급 없음. 사용자 조사(50명 완료) 74~76%가 평가 도구 부족. Structural Validity Framework 5항목 pass/fail(시간 위생·동적 유니버스·근거 견고성·보정·현실적 제약) 중 하나라도 실패하면 "배포 주장이 아닌 PoC" — [arXiv HTML 2602.14233v1](https://arxiv.org/html/2602.14233v1)
- **Look-Ahead-Bench(arXiv 2601.13770, 2026-01-20)**: 시간적으로 분리된 시장 regime 간 alpha decay로 look-ahead 편향 측정; Llama 3.1 8B/70B·DeepSeek 3.2 등 표준 LLM은 유의한 look-ahead 편향, Point-in-Time 추론 모델(Pitinf)은 안정; 수치는 abs에 없음; 코드 benstaf/lookaheadbench — [arXiv abs 2601.13770](https://arxiv.org/abs/2601.13770)
- **서베이(arXiv 2605.19337, 2026-05-19)**: 뉴스 타임스탬프가 "이용 가능 시각"이 아닌 "발행 시각"이라 인제스트 지연이 모델링되지 않음; 정정 공시 사용 시 미래정보 누출; 검색된 에피소드에 사후 결과 서사가 섞이는 "Oracle Fallacy"→"Outcome Embargo" 제안; MCTS 류 탐색의 다중검정 위험→탐색 예산 보고·walk-forward 권고; CoT 근거의 비충실성 — [arXiv HTML 2605.19337v1](https://arxiv.org/html/2605.19337v1)
- **프레임워크 자체 보고의 한계**: TradingAgents 3개월·Sharpe 5.6~8.2가 "경험적 기대 범위 초과"라고 저자 인정, 거래비용 미명시 — [arXiv HTML 2412.20138v7](https://arxiv.org/html/2412.20138v7); FinMem은 테스트 결과로 risk 설정을 선택했고 Llama2-70b는 −52.72% — [arXiv HTML 2311.13743v2](https://arxiv.org/html/2311.13743v2); TradingAgents README "실행 비결정적, reasoning 모델은 temperature 무시" — [GitHub README](https://github.com/TauricResearch/TradingAgents)
- **뉴스 헤드라인 분류 신호(Lopez-Lira & Tang, arXiv 2304.07619 v6 2025-10-28)**: 모델 지식 컷오프 이후 헤드라인 사용; GPT-4는 "거래 불가능한 초기 반응"에 대해 약 90% portfolio-day hit rate; GPT-4 점수가 이후 drift도 유의하게 예측(소형주·부정 뉴스에서 특히); 예측력은 모델 크기와 함께 상승; LLM 채택 증가에 따라 전략 수익 감소(가격 효율성 개선으로 해석) — [arXiv abs 2304.07619](https://arxiv.org/abs/2304.07619). 독립 재현 연구는 찾지 못했고, Chen·Didisheim·Somoza(2025)가 유사 결론, 저자들 자신의 후속(Lopez-Lira·Tang·Zhu 2025)이 LLM의 금융 데이터 암기(look-ahead) 문제를 제기 — [검색 요약·2차](https://larryswedroe.substack.com/p/can-chatgpt-forecast-stock-price)
- **감성 분류 gold-label 벤치마크**: FPB/FiQA-SA/TFNS에서 LoRA 튜닝 FinGPT v3.3(weighted F1 0.882/0.874/0.903)이 GPT-4 zero-shot(0.833/0.630/0.808)보다 높음 — [GitHub README](https://github.com/AI4Finance-Foundation/FinGPT)
- **통계 기준(비LLM 고전 문헌)**: Harvey & Liu "Backtesting"는 Sharpe→t-ratio 변환 후 다중검정 haircut(Bonferroni/Holm/BHY), 단일검정 t=3.0도 다중검정에서 약해질 수 있음, haircut은 비선형(상위 Sharpe는 적게, 경계는 크게), 시도 횟수 가정 필요, profit hurdle 제시 — [Duke PDF](https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF); [CME 배포본](https://www.cmegroup.com/content/dam/cmegroup/education/files/backtesting.pdf) (PDF 본문 파싱 실패, 검색 요약 기반); Bailey & López de Prado Deflated Sharpe Ratio(JPM 2014): 비정규성·표본길이·시도 수·시도 Sharpe 분산을 반영 — [SSRN 2460551](https://papers.ssrn.com/abstract=2460551); Bailey·Borwein·López de Prado·Zhu "Pseudo-Mathematics and Financial Charlatanism"(2014) Minimum Backtest Length 개념 — [carmamaths PDF](https://carmamaths.org/jon/backtest.pdf); 2차 요약의 "5년 표본에서 45개 이상 구성을 시험하면 IS Sharpe 1·OOS Sharpe 0인 구성이 거의 확실히 나온다"는 원문 미검증 — [hackernoon 2차](https://hackernoon.com/why-more-backtesting-can-make-your-trading-strategy-worse)

### Inferences
- 대상 프로젝트의 설계 전제(LLM은 야간 리서치·분류만, 라이브 주문 결정 금지)는 위 증거와 정합적이다. 특히 TradeTrap과 AMA의 HedgeFundAgent 사례는 "LLM이 포지션 크기를 결정하면 노출 폭주" 위험을 직접 보여준다.
- 야간 뉴스/공시 분류를 신호로 쓸 때 통제해야 할 편향은 (1) 발행 시각 vs 이용 가능 시각(한국 공시는 `list_date_ex`의 시각 필드로 ingestion 시각을 함께 기록), (2) 정정 공시(`final=False`)로 인한 누출, (3) narrative 편향(요약이 긍정으로 쏠림 → 라벨 스키마에 "unknown/no-trade" 필수), (4) 모델 암기(컷오프 이후 데이터로만 gold-label 평가).
- 로드맵 2단계 "~200 trades/arm" 기준은 통계적 근거를 별도로 세워야 한다. 13개 arm은 다중검정이므로 Harvey-Liu haircut 또는 DSR을 적용하고 시도 수(arm 수×파라미터 변형 수)를 ledger에 기록하는 것이 문헌이 요구하는 최소 조건이다.
- FINSABER의 "강세장 보수·약세장 공격" 비대칭은 regime 지표를 arm별 결과와 함께 기록해야 하는 이유를 직접 제공한다.

### Gaps
- Lopez-Lira & Tang의 거래비용 손익분기·롱숏 수익률·Sharpe 수치는 PDF 파싱 실패로 미확인(abs 요약만 인용).
- FINSABER의 정량 수치(얼마나 줄었는지, 종목 리스트, 테스트된 프레임워크 전체 목록)는 abs에 없음.
- Harvey & Liu PDF 본문은 파싱 실패; "t>3.0" 권고는 검색 요약·2차 자료 기반.
- 한국 시장(KOSPI/KOSDAQ 뉴스·공시)에 대해 LLM 분류 신호를 평가한 공개 연구는 이번 검색에서 찾지 못함.
- AMA는 "ongoing deployment"라 2025-10 이후 결과 미확인.

---

## Key Question 4. 스킬(SKILL.md/스크립트/레퍼런스)로 옮길 가치가 있는 구체 패턴과 예시 파일

### Takeaway
스킬화 가치가 높은 것은 (a) TradingAgents의 결정 로그→정산→reflection 3종 파일, (b) FinGPT식 gold-label F1 게이트 + TradingAgents `post_screen.py`식 확률 임계 분류, (c) RD-Agent(Q)·Bias 논문의 가설 사전등록/Structural Validity 체크리스트, (d) Harvey-Liu/DSR/MinBTL 기반 다중 arm 평가 규칙, (e) 개인 봇들의 deploy 게이트(모의/실전 키 분리, 환경변수 2중 게이트, `--confirm`, circuit breaker, 주문 금지창, 토큰 재발급 제한), (f) kiwoom-client·신한 NXT 안내 기반 세션/호가/VI 레퍼런스다. 각 패턴에 참고할 구체 파일/프롬프트를 아래에 매핑했다.

### Cited Findings

**패턴 1. 야간 뉴스/공시 분류 + gold label**
- 라벨 품질 게이트 형식: FinGPT-Benchmark의 FPB/FiQA-SA/TFNS/NWGI weighted F1 표 — [FinGPT README](https://github.com/AI4Finance-Foundation/FinGPT)
- 확률 임계 기반 2질문 분류(해당 종목 관련 여부 / 방향성), 실패 시 "미스크리닝" 명시 보존: `tradingagents/agents/post_screen.py`(`_OFF_TOPIC_BELOW=0.3`, `_STANCE_CONFIDENCE=0.5`, 429/529 backoff, 16 workers) — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 구조화 출력 강제 + 근거 id 인용: FinMem `puppy/prompts.py`(JSON 스키마 `{"summary_reason", "short_memory_index", ...}`), `puppy/reflection.py`(guardrails `ValidChoices(id_list, on_fail="reask")`) — [FinMem](https://github.com/pipiku915/FinMem-LLM-StockTrading)
- 뉴스 품질 감사 지표: AMA의 날짜 정확도 87.5%·커버리지 92.5%(20일 표본 수작업 검수) — [arXiv HTML 2510.11695v2](https://arxiv.org/html/2510.11695v2)
- 공시 시각·정정 처리: OpenDartReader `list_date_ex`(시각 포함), `final=False`(정정 포함) — [OpenDartReader](https://github.com/FinanceData/OpenDartReader); "Outcome Embargo"(사후 결과 서사 차단) — [arXiv 2605.19337v1](https://arxiv.org/html/2605.19337v1)
- 기권 라벨 필수: Bias 논문 "no trade / don't know를 유효 출력으로 채점" — [arXiv 2602.14233v1](https://arxiv.org/html/2602.14233v1)
- 사전 파이프라인 예시: FinMem `data-pipeline/01_Alpaca_News_API_download.py`, `01_SEC_API_10k10q_download.py`, `02-Raw_News_Data_Cleaning_Refinitiv.py`, `03-summary.py`, `05-get_sentiment_by_ticker.py` — 소스 트리 확인, [FinMem](https://github.com/pipiku915/FinMem-LLM-StockTrading)

**패턴 2. 사전등록 가설 템플릿**
- RD-Agent(Q) Research 단계: 목표→가설→태스크→구현→백테스트→피드백, bandit 스케줄러로 방향 선택 — [arXiv abs 2505.15155](https://arxiv.org/abs/2505.15155)
- Structural Validity Framework 5항목(시간 위생: 모델 컷오프 공개·PIT 데이터·프롬프트/검색문서/결정 타임스탬프 로그; 동적 유니버스; 근거 추적·entity substitution·scrambled negative control; 보정·기권; 비용·지연·예산 매칭 베이스라인) — [arXiv 2602.14233v1](https://arxiv.org/html/2602.14233v1)
- 시도 횟수 기록 의무: Harvey-Liu(시도 수 가정 필요) — [Duke PDF](https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF); DSR(시도 수·시도 Sharpe 분산 입력) — [SSRN 2460551](https://papers.ssrn.com/abstract=2460551)
- 탐색 예산 보고·walk-forward — [arXiv 2605.19337v1](https://arxiv.org/html/2605.19337v1)

**패턴 3. 실험 kill rule**
- 연속 주문/API 오류 시 거래 중단 circuit breaker: KIS_Auto_Trader `risk_manager.py` — [KIS_Auto_Trader](https://github.com/dalpan-cyber/KIS_Auto_Trader)
- 파싱 불가 결정은 Hold가 아니라 `REVIEW`(사람/재실행 요구): `tradingagents/agents/rating.py` — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 잘못된 설정값은 조용한 기본값 대신 시작 실패: `tradingagents/default_config.py` `_coerce` — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- regime 비대칭(강세장 보수/약세장 공격) → regime별 kill 기준 필요 — [arXiv 2505.07078](https://arxiv.org/abs/2505.07078)
- 교란 테스트 설계(구성요소별 perturbation 후 결정 궤적·최종 자산 비교): TradeTrap — [arXiv 2512.02261](https://arxiv.org/abs/2512.02261)

**패턴 4. 다중 arm 통계 평가(최소 거래 수·holdout)**
- grid 백테스트 구현(셀 독립, 벤치마크 대비 alpha, rating별 집계, `run_id` 재개, "포트폴리오 시뮬레이터 아님"): `tradingagents/backtest.py`, CLI `tradingagents backtest ... --every 7` — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 전략 간 유의성 검정 예시: FinMem `data-pipeline/08-Wilcoxon-Test.py`(누적 log-return 시계열 쌍대 Wilcoxon), 5회 반복 평균 — [FinMem](https://github.com/pipiku915/FinMem-LLM-StockTrading); [arXiv 2311.13743v2](https://arxiv.org/html/2311.13743v2)
- 다중검정 haircut(Bonferroni/Holm/BHY), t≈3.0 hurdle — [Duke PDF](https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF); DSR — [SSRN 2460551](https://papers.ssrn.com/abstract=2460551); MinBTL — [carmamaths PDF](https://carmamaths.org/jon/backtest.pdf)
- 1회 실행·2개월·유의성 미보고의 반례: AMA — [arXiv 2510.11695v2](https://arxiv.org/html/2510.11695v2)
- 비용 반영 net 지표를 1차 결과로: Bias 논문 cost bias — [arXiv 2602.14233v1](https://arxiv.org/html/2602.14233v1)

**패턴 5. regime 지표 기록**
- 벤치마크 대비 정산(접미사→지역 벤치마크 매핑, 당일 bar 제외): `tradingagents/memory/settlement.py` — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 단기 누적수익 부호로 risk 모드 전환(3일): FinMem self-adaptive — [arXiv 2311.13743v2](https://arxiv.org/html/2311.13743v2)
- 강세/약세 regime별 성과 분리 보고 — [arXiv 2505.07078](https://arxiv.org/abs/2505.07078)

**패턴 6. 포스트모템 / 인시던트 런북**
- reflection 프롬프트(2~4문장, 보유기간이 테제보다 짧으면 판단 유보 명시, alpha 수치 인용, 교훈 1개, "메모리 로그에 그대로 저장되어 미래 분석가가 읽음"): `tradingagents/memory/reflection.py` — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- append-only 로그 + 중복 기록 차단 + 회전: `tradingagents/memory/log.py` — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 실제 사고 사례: 앱키 공유 프로세스 중 하나의 revoke가 다른 프로세스 토큰 무효화 → "종료 시 자동 revoke 금지" — [AlgoLab 블로그](https://algolab.co.kr/blog/kiwoom-rest-api-token-expiry-revoke-2026)
- "장 마감 중 테스트라 실시간 프레임 필드명 미확인" → 장중 검증 항목 — [kiwoom-client README](https://github.com/younghwan91/kiwoom-client)
- API 불안정 대비 checkpoint 재개: FinMem `run.py sim-checkpoint` — [FinMem README](https://github.com/pipiku915/FinMem-LLM-StockTrading); TradingAgents `checkpoint_enabled` — [TradingAgents README](https://github.com/TauricResearch/TradingAgents)

**패턴 7. deploy 게이트**
- 계좌 영향 명령에 `--confirm`, 키는 OS 자격증명 저장소, 저장 전 read-only 조회로 검증: Kiwoom 공식 `kwcli` — [Kiwoom-REST-API README](https://github.com/Kiwoom-Securities/Kiwoom-REST-API)
- 모의/실전 키 분리·불일치 시 인증 실패, 실전 MCP 주문은 `KIWOOM_MCP_ALLOW_LIVE_ORDERS=true`일 때만 — [kiwoom-client README](https://github.com/younghwan91/kiwoom-client)
- `MOCK_TRADING_ONLY` 환경변수가 설정파일을 override, UI에 모드 표시, 테스트는 외부 연결 차단 — [KIS_Auto_Trader README](https://github.com/dalpan-cyber/KIS_Auto_Trader)
- 토큰 재발급 1분 1회, `EGW00201`, 모의 호출 제한 낮음 — [open-trading-api README](https://github.com/koreainvestment/open-trading-api)
- 08:00~09:00 가격 갱신만·주문 금지, 세션별 별도 프로세스, 요청 간 sleep — [quant-trading-rest README](https://github.com/CyberFlower/quant-trading-rest)
- 비결정성 경고·동일 `run_id` 재개 — [TradingAgents README](https://github.com/TauricResearch/TradingAgents)

**패턴 8. read-only 원장 감사**
- 메모리 로그를 결과표로 사용(별도 기록 불필요), 설정 전/후 상태 구분: `tradingagents/backtest.py` docstring, `memory/log.py` — [TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 연구 결과 로컬 저장(`~/.hedge-fund/research/`) — [ai-hedge-fund README](https://github.com/virattt/ai-hedge-fund)
- Google Sheets 원장 + `completed_trades.json` + `infinite_trader_state.json` 상태 파일 분리 — [KIS_Auto_Trader README](https://github.com/dalpan-cyber/KIS_Auto_Trader)
- 데이터 지연 규칙(투자자별 18시 이후, 공매도 T+2, 외국인 D-2)을 감사 시 반영 — [pykrx README](https://github.com/sharebook-kr/pykrx)

**패턴 9. 한국 세션/호가/VI 레퍼런스(스킬 reference.md 후보)**
- `tick_size()/round_to_tick()` 구현 참고 — [kiwoom-client](https://github.com/younghwan91/kiwoom-client); 호가단위 변경 기사 — [다음뉴스](https://v.daum.net/v/qGS7P0BEoG)
- NXT/KRX 세션표·SOR 배분·VI·수수료 — [신한투자증권 NXT 안내](https://open.shinhansec.com/mobilealpha/html/CS/NXTPolicyGuide.html); VI 제도 변경 보도 — [디지털타임스](https://www.dt.co.kr/article/12081241), [네이트뉴스](https://m.news.nate.com/view/20260831n25393)
- 거래일 캘린더: quant-trading-rest가 `exchange_calendars` 사용 — [quant-trading-rest README](https://github.com/CyberFlower/quant-trading-rest); pykrx는 날짜 미지정 시 최근 영업일을 내부 탐색 — [pykrx README](https://github.com/sharebook-kr/pykrx)

### Inferences
- 스킬 1(야간 분류): 라벨 스키마에 `unknown/no_trade`를 넣고, 각 항목에 `published_at`·`ingested_at`·`is_correction` 3개 타임스탬프/플래그를 강제하며, 주간 gold-label 표본(예: 20건 수작업 검수)으로 F1·날짜 정확도를 추적하는 구조가 위 소스들의 교집합이다.
- 스킬 2(가설 사전등록): 템플릿 필드는 arm 이름, 진입/청산 규칙, 기대 방향, 최소 거래 수, 성공/실패 판정 기준, 시도 번호(누적 시도 수), 적용 regime, 비용 가정, holdout 기간 봉인 일자 — Structural Validity 5항목 + DSR 입력값을 커버하도록 구성.
- 스킬 4(다중 arm 평가): "~200 trades/arm"은 arm 간 다중검정을 고려해 DSR 또는 Harvey-Liu haircut을 적용한 후 판정하고, 13 arm×파라미터 변형 수를 시도 수 N으로 기록해야 한다. TradingAgents `backtest.py`의 "셀 독립, 벤치마크 대비 alpha" 설계를 arm별 SQLite 집계 쿼리로 옮기면 된다.
- 스킬 6(포스트모템): TradingAgents reflection 프롬프트를 규칙 기반 봇용으로 바꾸면 "실패한 거래의 N일 alpha 수치 → 규칙의 어느 조건이 어긋났나 → 규칙/파라미터에 반영할 교훈 1개"로 변형 가능하며, LLM은 야간에만 호출.
- 스킬 7(deploy 게이트)은 Kiwoom `kwcli --confirm`·kiwoom-client 환경변수 게이트·KIS_Auto_Trader `MOCK_TRADING_ONLY`를 합친 3중 게이트(설정파일 + 환경변수 + 명령별 확인)로 바로 작성 가능하다.

### Gaps
- 한국 시장용 "pre-registered hypothesis" 또는 "kill rule"을 명문화한 공개 저장소는 찾지 못함 — 위 패턴은 해외 프레임워크·논문·개인 봇 관행을 조합한 것.
- TradingAgents `portfolio/` 모듈과 `tradingagents/agents/context.py`·`schemas.py`의 구체 구조(structured output 스키마)는 미열람.
- Kiwoom 공식 MCP(`mcp_spec/`, `mcp_exec/`)와 KIS MCP의 도구 목록·주문 게이트 방식은 미열람(skill 설계 시 비교 필요).
- 한국 거래일 캘린더(임시 휴장·NXT 운영일)를 코드화한 공개 라이브러리 사용 사례는 `exchange_calendars` 의존성 언급 외 확인되지 않음.
