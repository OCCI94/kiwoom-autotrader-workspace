# Kronos 오프라인 일봉 평가 (2026-11-09)

- 출처: https://github.com/shiyu-coder/Kronos @ 67b630e67f6a18c9e9be918d9b4337c960db1e9a (2026-04-13), MIT. 복사한 것은 model/ 3파일, LICENSE, tests/ 회귀 테스트와 데이터뿐.
- 가중치: NeoQuasar/Kronos-Tokenizer-base@0e0117387f39004a9016484a186a908917e22426, NeoQuasar/Kronos-small@901c26c1332695a2a8f243eb2f37243a37bea320 (저장소 tests가 고정한 revision). HF_HOME=./hf-cache, 텔레메트리 끔.
- 규칙: 장중(09:00~15:30) 실행 금지, 08:30 이후 새 배치 시작 금지. 수신기·runtime·outputs/ai-stock 접근 없음. 브로커 API 호출 없음.
- 사전 등록 주 지표: 룩백 90, 논문 설정(T 0.6, top_p 0.9, 샘플 10), 기준일 ≥ 2024-07-01, 10거래일 간격, 10일 기대수익률의 횡단면 RankIC 평균 > 0.02 이고 t > 2, 비용 후 상위 분위 스프레드 > 0, 베이스라인 3개(전일 수익률, 20일 모멘텀, 무작위) 모두에 우위.

## 환경 (2026-11-09 00:4x KST 설치)
- venv: 봇 파이썬 3.12.14(codex-primary-runtime)로 만든 격리 venv `venv/` (912 MB). torch 2.15.0(MPS 사용 가능), numpy 2.5.4, pandas 2.3.3, einops 0.8.1, huggingface_hub 0.33.1, safetensors 0.8.0, pytest, pip-audit.
- 복사본 sha256(앞 16자리): model/__init__.py f8f856ca3fedadca, model/kronos.py 0a5f90282e2039c2, model/module.py a07edbadc0e96804.
- 데이터: `data/daily_panel.csv` 244종목 × 600거래일(2024-05-13~2026-10-30), 144,727행. 점프(|일수익률|>0.30) 79건·58종목 → ±5행 무효, 거래량·가격 0인 행 201, 날짜 누락 1,673행. 제외 목록 `data/exclusions.json`.
- 비용 가정: 실계좌 왕복 0.23%(수수료 0.015%×2 + 매도세 0.20%), 모의 관측 왕복 0.55%(ka10074 관측 수수료 0.35% + 매도세 0.20%).
- 스크립트: `scripts/prepare_daily.py`(표준 라이브러리, 읽기 전용), `scripts/run_kronos.py`(venv, 롤링 예측, 08:30 KST 마감 가드, 재개 가능), `scripts/score.py`(RankIC·10분위·베이스라인·무작위 구간·관문).

## 검증·속도 (00:37 KST)
- pip-audit: 알려진 취약점 0건. pytest 4건 통과(CPU, 회귀 rel diff ≤ 1.1e-6, MSE 기대치 일치). 가중치 캐시 110 MB, revision 일치.
- 속도(47종목×10샘플, 룩백 90, 예측 10): CPU 4스레드 22.6초·maxrss 2.67 GB, MPS 16.1초·maxrss 0.59 GB. CPU·MPS er10 상관 0.991. 본 실행은 MPS, nice 10.
- 격자: ① paper L90(주 지표) ② default L90 ③ paper L256. 기준일은 2024-09-23부터(데이터 시작 05-13 + 90행) 10거래일 간격.

## 결과 (2026-11-09 09:08 KST, 세 격자 채점 완료, results/DONE)
- 실행: ① paper L90 51/51 기준일 72.8분(앵커당 73~92 s) ② default L90 51/51 7.2분 ③ paper L256 14/34 428.8분 — 앵커당 345→3,879 s로 느려져 08:30 가드가 14번째 앵커 뒤 09:07:51에 작동. ③은 09:00~09:07 장중에 걸침(규칙 위반, 아래 교훈).
- 사전 등록 관문(① er10→f10): 미달. IC +0.0072 (t 0.38), 무작위 95% 구간 [−0.0180, +0.0169] 안. 10분위 스프레드 −1.16%p (t −1.16). 6개 중 2개만 통과(상위 비용 후 >0, |IC|>mom20).
- ② er10→f10 −0.0031. ③(부분) +0.0044, 스프레드 −2.60%p. 기준 r1d→f10 +0.0325 (t 1.47), mom20 스프레드 +1.38%p (t 1.42).
- 사후 발견 er1→f1 IC +0.0205(①)/+0.0322(②)는 1일 10분위 상위 +0.23~0.26% vs 유니버스 +0.08%로 비용(0.23%/0.55%) 이하. 채택 아님.
- 예측 수준: er10 평균 −3.3%(L90) / −15.1%(L256) — 무수정 KRX 일봉에서 일관된 하락 예측(보정 안 됨).
- 판정: 거부(현 구성). 조사 문서 8절.
- 교훈: 마감 가드는 배치 단위로, 앵커 소요가 예상의 2배를 넘으면 자동 중단, 절대 마감 06:00. 느려진 원인 미확인(maxrss 일정 0.62 GB, pmset 수면 기록 없음).
- 수신기 영향(09:12 점검, 09:15 재확인): 00 스트림 09:12 ready=False → 09:15 자가 복구(ready=True). 06:46~09:01 sqlite_busy 저장 실패 12회(이 로그 파일에서 오늘 처음), Kronos 앵커가 가장 느렸던 06:30~09:07과 겹침. 인과 방향 미확인. 조사 문서 8-6절.

## 정리 (2026-11-09 09:2x KST, 사용자 지시 "venv만 삭제")
- `venv/`(912 MB) 삭제. 남은 것: data, hf-cache(110 MB, 고정 revision 가중치), kronos_pkg, logs, results, scripts — 총 123 MB.
- 재구축이 필요하면: `/Users/tr-oh/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m venv venv && venv/bin/pip install -r kronos_pkg/requirements-eval.txt` (당시 버전: torch 2.15.0, numpy 2.5.4, pandas 2.3.3, einops 0.8.1, huggingface_hub 0.33.1, safetensors 0.8.0). 장외 시간에만.
