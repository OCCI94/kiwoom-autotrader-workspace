---
name: arm-change-review
description: Reviews a proposed change to one of the Kiwoom paper-trading bot's strategy arms (13 EXP arms plus native S5) before anyone deploys it. It classifies the change as defect fix vs economic change, traces the impact path file:line, checks prompt/config/code constant consistency, writes the 6-item change record, enforces SHADOW before LIVE, checks the Tue/Thu KST deploy window against the AGENTS.md deploy contract, and lists the post-deploy state.json checks. Use when the user asks about arm 변경, 전략 파라미터 수정, 배포 창, 장중 배포 금지, 지금 배포해도 되는지, 기능 플래그, SHADOW, 변경 기록, 롤백 계획, or whether a TTL/threshold/window edit may go live. It never deploys, never restarts the bot, never edits runtime, settings, ledgers or LaunchAgents, and never promotes a gate to LIVE by itself; it reviews, records and reports.
license: MIT
compatibility: macOS, Python 3.9+ standard library only (zoneinfo for Asia/Seoul). Assumes a KST clock. No network access, no secrets.
metadata:
  author: tr-oh
  version: "0.1"
  created: "2026-11-03"
---

# arm 변경 검토

봇은 `paper_experiment_v1` 프로필로 13개 실험 arm(`EXP_R`, `EXP_A`, `EXP_B`, `EXP_B_TP`, `EXP_E`, `EXP_C`, `EXP_D`, `EXP_ORB_A`, `EXP_ORB_B`, `EXP_GDR`, `EXP_VSS`, `EXP_LDR`, `EXP_ETF_B`)과 네이티브 `S5_steady_uptrend_vwap`, 합쳐 14개 활성 arm을 돌린다. 이 스킬은 arm의 코드·파라미터·프롬프트·설정을 바꾸기 **전에** 하는 검토·기록과 배포 창 검사를 맡는다. 배포는 하지 않는다. 배포는 Codex의 배포 도구와 사용자 승인이 한다.

## 언제 쓰나

- "EXP_D TTL 고쳐도 돼?", "지금 배포해도 돼?", "S5 익절 +1.5%로 바꾸자", "이 변경 롤백 계획 써줘", "SHADOW로 먼저 돌리자", "기능 플래그 상태 보여줘".
- 변경이 다음을 건드릴 때: `kiwoom_autotrader/trading/paper_experiment_*.py`, `trading/paper_policy.py`, `config/settings.py`, `config/paper_hot_policy.py`, 계약 문서(`docs/PAPER_EXPERIMENT.md`, `UNFILLED_BUY_TTL.md`, `docs/S5_STEADY_UPTREND_VWAP.md`), 지시문(`*_INSTRUCTIONS.txt`), 런타임 프로필 값.
- 쓰지 않는 경우: 원장 대조(`ledger-reconciliation`), 헬스체크·장애 분류(`kiwoom-rest-ops-runbook`), plist 작업(`launchd-scheduler`), 단순 코드 읽기.

## 절대 규칙

1. **장중 금지.** 거래일 09:00~15:30 KST에는 runtime·운영 설정·원장·LaunchAgent를 건드리지 않는다. 소스 수리와 격리 테스트만 한다.
2. **정상 배포 창.** KST 화·목의 08:30 전 또는 15:40 후, 공식 세션 밖, 사용자 승인 하에만. 화·목 각 정상 시도 1회, 주 최대 2회. 실패·미확정 시도도 횟수(claim)를 소비한다. 장전 배포는 신선한 당일 공식 예정 세션 증거가 필요하다. 그 밖의 시각은 소스·대상·시각을 묶은 일회성 예외 토큰이 있어야 한다. 소스 수리·재시작 승인은 코드 배포 승인이 아니다.
3. **전체 격리 스위트.** 배포 대상의 정확한 소스 manifest/SHA로 전체 APP 격리 스위트(네트워크·키체인·launchctl 차단, 격리 HOME)를 통과해야 한다. focused 테스트만으로 배포하지 않는다. 검증 뒤 소스나 날짜가 바뀌면 다시 돈다. 결과와 manifest는 배포 당일 `reports/<날짜>/`에 남긴다.
4. **백업과 해시.** 배포 전 runtime·설정·원장 백업 경로와 해시를 보고한다. 금융 원장은 과거 백업으로 되돌리지 않는다(새 경제 이벤트가 지워진다). 롤백은 코드·설정만.
5. **배포 후 확인.** 실제 `state.json`의 `pid`·`status`·`paper_experiment.enabled`·`active_arms`(14개)·`halted`를 읽어 그대로 보고한다. 수신기 외 작업자 변경은 별도 승인 범위를 확인한다.
6. **LLM은 주문 경로 밖.** arm 판단·주문·취소는 코드가 한다. 이 스킬은 LLM 호출을 주문 규칙에 넣는 변경을 제안하지도, 승인하지도 않는다. LLM은 야간 분류·리서치 전용이다.
7. **자동 승격 없음.** SHADOW→LIVE는 사람이 기록 6항에 승인자·시각을 적고 레지스트리를 고친 뒤 정상 창에서 배포해야 성립한다. 만료일이 지난 SHADOW는 OFF로 본다.
8. **승인된 프로필 보호.** `paper_experiment_v1`과 14개 활성 arm을 별도 승인 없이 과거 보수 프로필로 되돌리거나 일부를 끄지 않는다. 끄는 것도 변경이다.
9. **스크립트는 읽기 전용.** 이 스킬의 스크립트는 아무것도 수정하지 않는다. 레지스트리·기록 파일은 사람이 고친다. 비밀값은 환경변수 이름으로만 언급한다.

## 절차

1. **분류.** 결함 수정 vs 경제적 변경.
   - 결함 수정: 문서·설계가 말하는 동작과 코드 동작이 달라 문서대로 맞추는 것(예: 15:20~15:30 단일가 매수에 180초 TTL이 걸려 창이 끝나기 전에 취소되던 것).
   - 경제적 변경: 진입·청산 조건, 임계값, 수량, 시간창, 유니버스 등 기대 손익 분포를 바꾸는 것(예: S5 익절 +1%→+1.5%).
   - 결함 수정이라도 체결 분포가 바뀌면 "결함 수정(경제적 부수효과 있음)"으로 적고 측정 지표를 붙인다. 경제적 변경은 §0 단계에서는 보류하고 1단계 가설 사전등록으로 넘기는 것이 기본이다.
2. **영향 경로 추적.** 신호 판정 → 의도(intent) → 주문 → 취소/체결 → 원장 → 상태 보고까지 `file:line`로 적는다. 예(2026-11-03 소스): `paper_experiment_rules.py:401` closing_auction 판정 → `paper_experiment_monitor.py:399` auction_intent → `telegram/listener.py:591` TTL tick(180) → `config/settings.py:210` `unfilled_buy_ttl_seconds` → `execution_log.py` orders/fills. 줄 번호는 검토 시점 mtime과 함께 적는다. Codex가 같은 파일을 동시에 고친다.
3. **정합성 검사.** 문서 ↔ 지시문/프롬프트 ↔ 소스 상수 ↔ 런타임 복사본 해시. 같은 숫자가 세 곳 이상에 흩어져 있으면 하나로 모으거나 값 일치를 단언하는 테스트(날짜·사유 포함)를 둔다. 프롬프트에만 있고 코드 강제가 없는 규칙은 코드로 옮기거나 프롬프트에서 뺀다. 세부는 `references/review-checklist.md` B절.
4. **변경 기록 6항목.** 목적 / 기대 효과와 측정 지표 / 측정 방법과 기간 / 롤백 절차 / 위험 / 승인자·승인 시각. 양식과 작성 예는 `references/change-record-template.md`. 기록 없이는 다음 단계로 가지 않는다. 기록은 봇 소스 트리 밖(`reports/<날짜>/`)에 둔다.
5. **SHADOW 먼저.** 새 로직은 판단만 기록하고 주문을 내지 않는 SHADOW 상태로 레지스트리에 등록한다(만료일 필수). SHADOW 기간에 "LIVE였다면 냈을 주문"을 남겨 기록 2항의 지표를 채운다. 승격은 사용자가 결정한다. 정책은 `references/feature-flag-policy.md`.
6. **배포 창 검사.** `python3 scripts/deploy_window_check.py --kind normal --holidays <공식 휴장일 파일>`. 종료 코드가 0이 아니면 배포 요청을 거절하고 `next_normal_window`를 알려준다. 0이어도 사용자 승인·claim 잔량·세션 증거는 사람이 확인한다.
7. **배포 도구 리허설·스위트·백업.** 수리 시간대에 배포 도구를 런타임 복사본으로 dry-run 한다. 전체 격리 스위트 결과 파일, manifest, 백업 경로·해시가 배포 보고에 있는지 확인한다(체크리스트 C·D·E절). 실제 실행은 Codex 배포 도구와 사용자가 한다.
8. **배포 후 확인.** `state.json` 5개 값을 인용해 보고하고, 변경 기록에 배포 시각·manifest 해시를 덧붙이고, 레지스트리 상태를 사람이 갱신한다. 첫 거래일 마감 후 기록 2항 지표를 한 번 집계한다.

## 스크립트

둘 다 표준 라이브러리만 쓰고, 네트워크를 쓰지 않으며, 아무것도 수정하지 않는다. 스킬 루트에서 상대경로로 부른다.

- `scripts/deploy_window_check.py [--at "YYYY-MM-DD HH:MM"] [--holidays PATH] [--kind normal|exception|repair] [--json]`
  - `--at` KST, 기본 지금. `--holidays`는 `"YYYY-MM-DD"` 문자열 배열(또는 `{"holidays": [...]}`); 없으면 평일을 전부 거래일로 가정하고 stderr에 경고한다. 형식 예는 `references/holidays.example.json`(공식 달력이 아니다).
  - `--kind repair`: 소스 수리·격리 테스트. 항상 허용. `normal`(기본): 화·목 거래일의 pre-open(<08:30) 또는 after-close(≥15:40)만 허용. `exception`: 거래일 09:00~15:30 밖이면 허용하되 일회성 예외 토큰이 필요하다고 출력한다.
  - 출력: `at`, `weekday`, `trading_day`, `in_session`, `window`(pre-open / pre-session-gap / session / post-close-gap / after-close), `allowed`, `reason`, `notes`, `next_normal_window`(다음 화·목 슬롯 시작; pre-open 슬롯은 00:00부터로 표기), `rule_source`.
  - 종료 코드 0 허용, 1 불허, 3 입력 오류. 규칙 출처는 AGENTS.md "배포 검증 계약 (2026-11-03 사용자 지정)"이며 요약은 `references/deploy-contract-summary.md`.
- `scripts/feature_status.py [--registry PATH] [--at YYYY-MM-DD] [--json]`
  - 레지스트리 JSON(`{name, state: LIVE|SHADOW|OFF, owner, since, expires, evidence, notes}` 배열 또는 `{"entries": [...]}`)을 표 또는 JSON으로 낸다. 기본 경로는 `references/feature-flags.example.json`(예시일 뿐이다).
  - 경고(종료 1): `expires`가 지났는데 OFF가 아님, 증거 없는 LIVE, 중복 이름, 만료일 없는 SHADOW, 알 수 없는 필드. 구조 오류(알 수 없는 state, 이름 없음, 날짜 형식)는 종료 3.
  - 레지스트리를 절대 고치지 않는다. 상태 전환은 사람이 변경 기록과 함께 손으로 한다.

## 기록 양식

변경 기록은 아래 6항목을 모두 채운다. 빈 항목은 "해당 없음"이 아니라 "미작성"이다.

```
# arm 변경 기록: <게이트 이름>
- 분류: 결함 수정 | 경제적 변경 | 결함 수정(경제적 부수효과 있음)
- 대상 arm / 영향 경로(file:line, mtime) / 소스 manifest·SHA / 상태 전환
## 1. 목적                      문서가 말하는 것과 코드가 하는 것의 차이 한 문장
## 2. 기대 효과와 측정 지표       원장에서 셀 수 있는 건수·비율·금액만
## 3. 측정 방법과 기간           테이블·열·쿼리, 거래일 수 또는 건수, SHADOW 만료일, 비교 기준
## 4. 롤백 절차                  코드·설정만, 백업 경로·해시, 창·승인·claim 소요
## 5. 위험                       실패 모드마다 감지 방법 한 줄
## 6. 승인자 / 승인 시각         수리 승인과 배포 승인, SHADOW→LIVE 승인을 따로
```

보고 형식(사용자에게):

```
변경: <게이트 이름> (<분류>, 대상 arm ...)
기록: reports/<날짜>/arm-change-<이름>.md (6항목 완료 / 미완 항목: ...)
정합성: 문서/프롬프트/코드/런타임 일치 | 불일치: file:line ...
창: deploy_window_check <at> kind=<...> → allowed=<bool>, 다음 창 <...>
상태: SHADOW(만료 <날짜>) | LIVE 승격 대기 | 배포 미완료(claim 소비 N)
남은 사람 몫: 승인, claim 확인, 스위트·백업·배포 실행, state.json 인용
```

## 함정

- **2026-11-03 밤의 교훈.** 세 차례 배포 시도가 모두 배포 도구 자체 결함으로 실패했고 코드는 문제가 없었다. 계약상 실패·미확정 시도도 claim을 소비하므로 도구 결함이 배포 기회를 태웠다. 도구는 창 전 수리 시간대에 런타임 복사본으로 리허설하고, 창 안에서 도구를 고치지 않는다.
- **원장 해시 비교 함정.** 수신기를 멈추기 전후로 원장 파일 전체 해시를 비교하는 도구는 수신기가 정상적으로 append한 관측 행(`execution_observations`, `broker_observations`, `strategy_holding_observations`, `profit_observations` 등)을 "변경"으로 본다. 금융 행(`intents`, `order_decisions`, `orders`, `fills`)만 열 단위로 비교하고 관측 행 append는 허용해야 한다. 테이블명은 `execution_log.py`의 CREATE TABLE로 재확인한다.
- **프롬프트 ≠ 코드 ≠ 서버 3중 괴리(prism-insight 교훈).** 프롬프트는 종가 기준이라 말하고, 코드는 장중 5분 루프로 먼저 팔고, 저장소 crontab에는 없는 보호 루프가 서버에서만 돌던 사례. 이 프로젝트에서는 계약 문서·`*_INSTRUCTIONS.txt`·`settings.py` 상수·런타임 복사본의 네 곳이 같은 숫자를 말해야 한다. "코드가 소스에 있다"와 "런타임에서 돈다"와 "레지스트리가 LIVE라 한다"는 다른 사실이다.
- **줄 번호는 금방 낡는다.** 예로 든 `file:line`은 2026-11-03 소스 기준이며, 검토 때마다 다시 찾고 mtime을 적는다.
- **장전 창의 함정.** 화·목 08:30 전이라도 당일 공식 예정 세션 증거가 신선하지 않으면 정상 배포가 아니다. 임시휴장·조기폐장 공지를 먼저 본다.
- **수리 승인 ≠ 배포 승인 ≠ 재시작 승인.** 셋을 한 문장으로 받지 않는다. 기록 6항에 따로 적는다.
- **휴장일 파일 없이 돌린 결과**는 "평일 = 거래일" 가정이다. 결과를 보고할 때 `holidays_checked`를 같이 말한다.
- **OFF도 변경이다.** 성과가 나쁘다고 arm을 바로 끄는 것은 AGENTS.md 23행 위반이다. 6항목 기록과 승인을 거친다.
- **예시 파일을 실제로 착각하지 않는다.** `references/feature-flags.example.json`과 `references/holidays.example.json`은 형식 예시다. 실제 레지스트리와 공식 휴장일은 사용자가 경로를 준다.

## 참조 파일

- `references/deploy-contract-summary.md`: AGENTS.md 15~23행 계약 요약과 시간 창 표.
- `references/change-record-template.md`: 6항목 양식과 2026-11-03 TTL 수정 작성 예.
- `references/feature-flag-policy.md`: LIVE/SHADOW/OFF 의미, 자동 승격 금지, 만료 시 OFF, 전환 권한.
- `references/review-checklist.md`: 분류·정합성·테스트·도구 리허설·백업/롤백·창·배포 후 체크리스트.
- `references/feature-flags.example.json`: 레지스트리 형식 예시 3건(EXP_D/auction_window_ttl SHADOW, EXP_VSS LIVE, S5_steady_uptrend_vwap LIVE).
- `references/holidays.example.json`: 휴장일 파일 형식 예시(공식 달력 아님).
- `evals/evals.json`: 평가 프롬프트 3건.
