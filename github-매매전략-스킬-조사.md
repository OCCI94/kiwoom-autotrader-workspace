# GitHub 매매 전략·스킬 조사 (2026-11-02)

- **조사 방법:** 웹 조사만 했습니다. 아무것도 설치하거나 실행하지 않았습니다.
- **수치 기준:** ★과 날짜는 조사 시점에 GitHub에서 본 값입니다. 핵심 저장소는 GitHub API와 페이지 접속으로 존재 여부와 수치를 다시 확인했습니다.
- **주의:** 매매 시스템 설계 참고 자료일 뿐, 투자 권유가 아닙니다.

## 결론
- **비용을 넣고 표본외 검증까지 통과한 "한국 1분봉 단타 엣지" 저장소는 찾지 못했습니다.** 오히려 가장 엄밀하게 검증한 한국 저장소들은 "세금을 빼면 수익 없음"을 공개하고 있습니다.
- **가져올 만한 것은 세 종류입니다.**
  - ① 검증 절차·안전장치를 담은 에이전트 스킬
  - ② 키움 REST·데이터 도구
  - ③ 모의 실험 arm으로 넣을 가설 몇 개
- **비용 기준:** 2026년 매도 거래세는 코스피(0.05%+농특세 0.15%)와 코스닥 모두 0.20%로 보고됐습니다. 저장소마다 옛 세율(0.15·0.18·0.25%)이 섞여 있으니 결과를 볼 때 확인해야 합니다.

## 1. 에이전트 스킬·MCP

| 저장소 | 규모·상태 | 내용 | 쓸 곳 |
|---|---|---|---|
| [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) | ★3.5k, MIT, 매우 활발 | SKILL.md 70여 개. 분석·계획·매매일지만 다루고 주문은 없음. 데이터는 미국 중심 | `backtest-expert`(슬리피지 1.5~2배 가정, walk-forward, 표본외 성과가 표본내의 50% 미만이면 경고, 거래 수 30/100/200 기준), `signal-postmortem`, `edge-strategy-reviewer`, `drawdown-circuit-breaker`의 검증 규칙을 기존 kiwoom-market-research 스킬로 옮기기 |
| [ChunSam/kiwoom-mcp-server](https://github.com/ChunSam/kiwoom-mcp-server) | ★1, MIT, 2026-10-31 | 키움 REST 조회 전용 MCP. 주문 기능을 설계 단계에서 뺐고 모의가 기본. 테스트와 CI 있음 | 장후 복기 스킬의 조회 백엔드. 생긴 지 얼마 안 된 1인 프로젝트라 코드를 직접 확인하고 버전을 고정해야 함 |
| [gejyn14/kiwoom-plugin](https://github.com/gejyn14/kiwoom-plugin) | ★1, Apache-2.0 | Claude Code 플러그인. 조회용과 주문용을 분리. dry_run·모의 기본, 사전점검 → 미리보기 → 확인, 중복 주문 방지 키 | 설치보다는 봇에 안전장치 설계를 가져오는 용도 |
| [unohee/pykiwoom-rest](https://github.com/unohee/pykiwoom-rest) | ★56, MIT | Python 키움 REST 클라이언트 + CLI + MCP. 주문은 confirm=true일 때만 실행 | 자체 클라이언트와 비교하는 용도 |
| [Kiwoom-Securities/Kiwoom-REST-API](https://github.com/Kiwoom-Securities/Kiwoom-REST-API) | ★66, 키움 공식 | API 명세 337개, 예제 362개, MCP | 엔드포인트 기준 자료 |
| [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) | ★15k, MIT | LLM이 팩터·모델을 제안하고 Qlib으로 백테스트하는 과정을 반복. 비용을 넣고 기간을 나눠 검증한 NeurIPS 2025 결과(중국 CSI300) | 연구 방법론 참고. 리눅스 Docker가 필요해 부담이 큼 |
| [dragon1086/prism-insight](https://github.com/dragon1086/prism-insight) | ★793, AGPL-3.0 | 급등주 탐지 → LLM 리포트 → 시뮬레이션. 텔레그램 연동, 한국투자 실주문 가능 | 구조 참고만. 공개 성과는 +35.2%로 같은 기간 코스피 +103.5%에 못 미침. AGPL이라 코드 복사에 주의 |

**유명하지만 가치가 낮은 것:** TradingAgents(★120k), ai-hedge-fund(★64k), FinRobot. 모두 비용을 넣은 표본외 성과가 없고 미국 데이터 중심의 데모입니다.

## 2. 한국 시장 데이터·연구

| 저장소 | 내용 | 평가 |
|---|---|---|
| [japark22/kr-foreign-flow](https://github.com/japark22/kr-foreign-flow) | 외국인 순매수 신호 검증. 가설 사전등록, 상장폐지 종목 포함, 2026년 세율 반영, 플라시보 검정 | 방법론 모범 사례. 결과는 IC +0.0039, 손익분기 비용이 편도 2.4bp라 매매 불가. **수급 추종 단타는 하지 말라는 근거** |
| [jhg-park-0304/kr-equity-mean-reversion](https://github.com/jhg-park-0304/kr-equity-mean-reversion) | 갭하락 반전, 시가 매수 → 다음날 시가 매도. 비용 35bp, IS/OOS 분리 | 규칙이 명확해서 모의 실험 후보. 다만 OOS 샤프 2.10은 지나치게 좋고 세율이 옛 기준이라 다시 계산해야 함 |
| [mark461182/kr-intraday-momentum-research](https://github.com/mark461182/kr-intraday-momentum-research) | 코스닥 거래량 급증 모멘텀(분봉) | 사용자 B 규칙과 같은 구조. 61일 전부 표본내이고 비용 없이 PF 3.1이라 결과는 믿기 어려움. 실험 설계만 참고 |
| [yuchan-research/shipbuilding-quant-case](https://github.com/yuchan-research/shipbuilding-quant-case) | 조선주: 공시·페어·미국 야간 신호 | 신호는 유의(t=3.68)하지만 비용 후 손실. ETF로 세금을 피해도 유의하지 않음 |
| [breadum/kiwoom-restful](https://github.com/breadum/kiwoom-restful) | 키움 REST async, 호출 제한 자동 관리, 분봉 CSV 수집 | 몇 달치 분봉 수집에 적합 |
| [sharebook-kr/pykrx](https://github.com/sharebook-kr/pykrx) | 일봉, 투자자별 매매, 시총 | 쓸 만함. float16 반올림 문제 지적이 있음 |
| [FinanceData/marcap](https://github.com/FinanceData/marcap) | 1995년부터 전종목 일별 스냅샷 | 날짜별 유니버스 구성(생존편향 방지)에 쓸 수 있음 |
| FinanceDataReader | 데이터 수집 | 상장폐지 종목 조회가 빈 결과를 돌려준다는 이슈(#289)가 있어 생존편향 위험 |
| [junhoyeo/hades](https://github.com/junhoyeo/hades) | Rust로 구현한 KRX 호가단위·가격제한·VI 규칙 | 규칙 구현 참고 |

**피할 것:**
- rltrader: 책 예제용 강화학습 코드입니다.
- pipios4006-boop/KIS-API-Python-Trading-Bot-Example: 자가 업데이트 기능이 있어 공급망 위험이 있습니다.
- 구 OCX용 라이브러리(pykiwoom, koapy): REST 환경에서 쓸 수 없습니다.
- 코인 전용 변동성돌파 봇: 주식 매도세를 고려하지 않습니다.

## 3. 단기 전략 연구 (논문 + 재현 코드)
- **지수·ETF 대상 일중 전략:** 지수 ORB, "Beat the Market", 시장 일중모멘텀은 거래당 기대수익이 약 2~3bp라서 왕복 30bp 비용을 넘지 못합니다. 2025~26년 독립 재현에서도 대부분 약해졌습니다.
  - [zarattini-2023-orb-qqq](https://github.com/giovannibrusco/zarattini-2023-orb-qqq): 슬리피지 2센트에서 샤프가 1.06 → 0.23으로 떨어집니다.
  - [spy-intraday-momentum-oos](https://github.com/PazSheimy/spy-intraday-momentum-oos): 표본외 샤프 0.39입니다.
- **한국 증거:** KAIST 석사논문(코스피 2011–20)에서는 첫 30분 모멘텀이 없었고, 마지막 1시간은 반전이었습니다.
- **비용을 넘을 가능성이 있는 두 방향:**
  - ① 개별 종목의 큰 움직임을 노림 (거래량 급증 종목 ORB)
  - ② 여러 날 보유로 비용을 나눔 (거래량 충격 스윙, 고회전 단기모멘텀)
- **익절과 기대값:** ORB류는 승률이 17~45%이고, 수익은 소수의 큰 승리에서 나옵니다. "빨리 익절하면 기대값이 무너진다"는 우리 백테스트 결과와 같습니다.

## 4. 모의 실험 arm 후보

| arm | 규칙 요약 | 근거 | 기대 |
|---|---|---|---|
| ORB-SIP (Stocks in Play) | 09:05에 첫 5분 상대거래량(직전 14일 같은 구간 대비) 2 이상인 상위 5종목 중 첫 5분봉이 양봉인 종목만. 시가범위 고가 +1틱에 매수 스톱(09:05~10:30). 손절은 ATR의 10%와 25%(또는 시가범위 저가) 두 가지 비교. 익절 없이 종가 단일가 청산. 롱만 | Zarattini·Barbon·Aziz 2024 (미국 7,000여 종목, 저자 샤프 2.81, 슬리피지 불명확, 독립 표본외 결과 없음) | 중 |
| 갭하락 반전 오버나잇 | 갭 −15%~−3%, 갭과 20일 모멘텀 점수 상위 5종목을 시가에 매수 → 다음날 시가 매도. 장중 −2% 손절 | kr-equity-mean-reversion (한국 OOS, 결과가 과하게 좋음) | 중 |
| 거래량 충격 스윙 | 당일 거래량이 20일 평균의 3배 이상이고 수익률 0~+8%(급등일 제외)이면 종가 매수. 5일·20일 보유, 종가 −8% 손절 | 고거래량 프리미엄(41개국). 한국 유통업은 반전 사례도 있음 | 중 |
| 막판 반전 + 오버나잇 | 15:15 직전 60분 −2.5% 이하이고 당일 −10%보다 덜 빠진 종목을 종가 단일가 매수 → 다음날 시가 매도. 악재 공시 제외 | KAIST(마지막 1시간 반전), 한국 야간 수익률 연구 | 중~낮음 |
| ETF 버전 | 같은 1분봉 신호를 KODEX 200·코스닥150 ETF에 적용. 매도세 면제로 왕복비용 약 0.2%p 절감 | 비용 구조 실험. 조선주 사례에서는 유의하지 않았음 | 낮음~중 |

**공통 조건:**
- 보통주만 대상으로 합니다. 관리·투자경고·SPAC는 제외합니다.
- 20일 평균 거래대금 30억 원 이상만 씁니다.
- VI 발동 중이거나 상한가까지 5% 이내이면 진입하지 않습니다.
- 비용은 왕복 0.3%에 1틱 슬리피지를 더합니다.
- 대조군은 무작위 진입(R)입니다.

**하지 말 것:**
- 외국인·기관 순매수 추종 단타 (kr-foreign-flow 근거)
- 미국 야간 신호
- 지수 일중모멘텀 단독 전략

## 5. 검증 도구

| 도구 | 용도 | 비고 |
|---|---|---|
| [bashtage/arch](https://github.com/bashtage/arch) | 블록 부트스트랩 신뢰구간, SPA·MCS 검정(여러 arm을 동시에 볼 때 데이터 스누핑 보정) | NCSA 라이선스, 활발 |
| [esvhd/pypbo](https://github.com/esvhd/pypbo) | 과최적화 확률(PBO), DSR | AGPL이라 코드에 포함하면 라이선스 주의 |
| [skfolio](https://github.com/skfolio/skfolio) | Combinatorial Purged CV | BSD-3 |
| vectorbt | 1분봉 다종목 파라미터 스윕 | Commons Clause(판매 금지) |

**운영 원칙:** 시도한 변형 수를 모두 기록하고, 마지막 기간은 봉인 홀드아웃으로 남기고, 상장폐지 종목을 포함한 데이터를 씁니다.
