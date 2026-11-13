# Codex 요청: 미입증 SOURCE 3건 → ASSUMPTION 재분류 (값 불변)

- 사용자 승인: 2026-11-05 12:20 KST "재분류 진행" (근거: `archive/2026-11-05-주도주/ai-stock-점검-20261105-주도주-보강검토.md` §2)
- 경로 변경 안내: 책 추출 텍스트는 `/Users/tr-oh/Documents/Codex/archive/2026-11-05-주도주/주도주투자_전체통합정리-추출텍스트-20261105.md`로 옮겼다(SHA 동일 `faaccc7b…`). `LEADER_FORMULAS.md`나 보고서가 옛 경로를 적고 있으면 이 경로로 고친다. 행 번호는 그대로다.
- 대상: `outputs/ai-stock-research/leader_constants.py`의 `RULES` 3건. **값은 바꾸지 않는다.** `provenance.kind`와 `reference`만 바꾼다.
- 규칙: 연구 영역만 수정. APP 소스·운영 런타임·설정·원장·작업자 무접촉. 외부 API 호출 0회. 기존 보고·계산 파일 덮어쓰기 없음.

## 1. 변경 내용

| 키 | 값(유지) | 새 kind | 새 reference(요지) |
|---|---|---|---|
| `core.min_upside` | `0.21` | `ASSUMPTION` | 책 982~983·2059~2060행은 7% 손절 × 3:1 = 21%를 예시로 든다. 모든 진입의 절대 하한 21%는 책에 없다. 값은 7%×3 파생으로 유지. 손절폭 < 7%일 때 책보다 엄격함을 명시. 사용자 결정 2026-11-05 12:20 |
| `core.price_return_windows` | `{1w:5, 1m:20, 3m:60}` | `ASSUMPTION` | 책 455~456·478행은 1주/1개월/3개월만 제시. 거래일 환산 5/20/60은 가정. `common.trading_days_per_year=260`과 같은 계열(260/52=5, 260/12≈21.7→20 관행값). 사용자 결정 2026-11-05 12:20 |
| `core.sell_priority` | `EMERGENCY>EXIT>REDUCE>CONDITIONAL_CHECK>HOLD` | `ASSUMPTION` | 책 986~1018·1035~1036행은 병렬 기준과 −20% 최후 안전망만 말한다. 5단계 완전 순서는 사용자 명세(합본 프롬프트 §0(4), 2026-11-05). 사용자 결정 2026-11-05 12:20 |

- 세 건 모두 `original_independently_verified`는 ASSUMPTION에 해당 없음으로 처리(필드 제거 또는 `None`, 기존 ASSUMPTION 61건과 같은 방식).
- 이 변경으로 집계는 **SOURCE 44 / ASSUMPTION 80 / 총 124**가 된다. SOURCE 44건은 전부 `original_independently_verified=True`여야 한다.

## 2. 함께 고칠 것

- `LEADER_FORMULAS.md`: 출처 집계 문장(47/77 → 44/80)과 1-1 대조표의 "불일치/미입증" 3행을 "ASSUMPTION 재분류(2026-11-05 12:20 사용자 결정)"로 갱신. 3:1 게이트 절에 "0.21 하한은 7%×3 파생 가정"이라는 한 문장 추가.
- 테스트: SOURCE/ASSUMPTION 건수나 `original_independently_verified` 전수를 단언하는 테스트가 있으면 새 집계(44/80, SOURCE 전부 verified)로 갱신. 없으면 추가: "SOURCE는 전부 검증됨, 미검증 SOURCE 0건".
- `provenance_catalog()` 출력에서 세 키가 ASSUMPTION으로 나오는지 테스트 1건.

## 3. 하지 말 것

- 세 키의 `value` 변경, 다른 121건 변경, 게이트 로직(`risk_reward_gate`, `evaluate_sell_rules`, 수익률 창 계산) 변경. 동작은 전과 같아야 한다.
- APP·운영 접촉, API 호출, Q3 재수집.

## 4. 보고 형식

```
재분류 3건: 키 / kind SOURCE→ASSUMPTION / 값 불변 확인
집계: SOURCE 44(verified 44) / ASSUMPTION 80 / 총 124
동작 불변: risk_reward_gate·evaluate_sell_rules·수익률 창 기존 테스트 통과 (변경 0)
스위트: <경로> tests_run / skip / fail 0
SHA256 변경: leader_constants.py, LEADER_FORMULAS.md, <테스트 파일> / 불변: 나머지
APP 소스 변경 0, 운영 무접촉, API 0회
```
