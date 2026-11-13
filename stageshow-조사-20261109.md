# benji.org/stageshow (benjitaylor/skills · stageshow) 조사 (2026-11-09)

- **질문:** "이 페이지를 참고해서 매매 페이지를 만드는 건 어때?"
- **조사 방법:** 글(https://benji.org/stageshow)을 내장 브라우저로 끝까지 읽고, 저장소 `benjitaylor/skills`를 빈 임시 폴더에 얕게 복제(커밋 `299140f`, 2026-11-05)해 README·AGENTS.md·SKILL.md 앞부분·package.json·scripts 13개의 실행·네트워크 호출·LICENSE·NOTICE를 읽었습니다. 프로젝트 `skill-install-policy` 검증기·감사기를 돌렸습니다. 아무것도 설치·실행하지 않았습니다. SKILL.md 19 KB와 references 15개 중 본문은 앞부분과 craft.md 목차만 봤습니다.
- **봇 쪽 확인(읽기 전용):** 소스에 웹 대시보드는 없습니다. HTML을 만드는 곳은 `diagnostics/render.py`(오프라인 기록 감사 보고서, 스크립트 없음, CSP 적용, "현재 매매 준비 상태: 미확인" 배너)와 `telegram/account_setup.py`(127.0.0.1 일회성 설정 페이지) 두 곳뿐입니다.

## 결론
- **Stageshow는 "가짜 UI(faux UI)"를 만드는 스킬입니다.** 홈페이지 히어로·기능 카드·벤토 그리드처럼 *그림으로 보이는* 제품 화면을 React로 그리고, 커서가 움직이는 짧은 데모를 촬영해 점검합니다. 글 자체가 "authored illustrations, not browser automation"이라 못 박고, 실제 글자는 3~7개만 두고 나머지는 회색 막대(placeholder)로 채우라고 가르칩니다. 즉 **데이터가 들어가는 화면이 아니라 마케팅용 삽화**입니다.
- **그래서 매매 페이지(실제 잔고·주문·arm 상태를 보는 화면)를 Stageshow로 만드는 것은 맞지 않습니다.** 숫자가 가짜인 예쁜 화면이 나오고, 그것이 실제 계좌 화면처럼 보이는 것이 이 프로젝트에서는 오히려 위험합니다(모의·실계좌·백테스트 결과를 섞어 보지 않는다는 원칙과 충돌). React·Vite·Node 20 툴체인도 파이썬 프로젝트에 무겁습니다.
- **그래도 가져올 것:** 디자인 규율. `references/craft.md`의 절제(Restraint)·타이포·간격·밀도 규칙과 "다섯 가지 실패(너무 많음·와이어프레임·상자에 가둠·평평함·밋밋함)"는 진짜 관제 화면을 설계할 때도 그대로 쓸 수 있습니다. 특히 "모든 셀에 진짜 글자를 채우지 말고 이야기가 있는 곳에만" 원칙은 수십 개 지표를 한 화면에 쏟는 흔한 대시보드 실패를 막아 줍니다.
- **권고:** 매매 페이지를 만들려면 **읽기 전용 관제 페이지**로 정의하고, Stageshow 없이 지금 프로젝트 방식(`render.py`처럼 정적 HTML, 스크립트 최소, 데이터 시각·미확인 배너 명시)으로 만드는 것이 맞습니다. 주문·취소 버튼은 넣지 않습니다(주문 경로는 수신기 하나, 토큰 공유 규칙). 데이터는 `state.json`·원장 **복사본**(mode=ro)·날짜별 JSONL·watchdog 샘플에서만 읽고, 장중에는 봇 프로세스를 건드리지 않습니다.
- **Stageshow 설치 판정: 보류(필요 시 조건부).** 안전성은 양호하지만 이 프로젝트에 지금 쓸 데가 없습니다. 나중에 설명 자료·발표용 삽화가 필요하면 그때 복사합니다.

## 0. 심사 보고 (skill-install-policy 형식)

```
스킬: stageshow (https://github.com/benjitaylor/skills@299140f, 2026-11-09 조회, stars 2,602, push 2026-11-05, MIT(stageshow/LICENSE; 저장소 루트 LICENSE 없음), SKILL.md v1.1.1)
판정: 보류 — 이 프로젝트에 용도 없음. 삽화·데모가 필요해지면 조건부 채택(아래 조건).
근거: 목적이 마케팅용 faux UI(가짜 데이터·커서 데모)라 실데이터 관제 화면과 맞지 않음
      React/TypeScript/Vite, Node ≥20.19 툴체인 추가가 파이썬 프로젝트에 무거움
      디자인 규칙(craft.md·framing.md)은 참고 가치 있음
감사 발견: validate_skill PASS. audit: 비텍스트 파일 327(.tsx/.css), 네트워크 URL 198(package-lock.json의 registry.npmjs.org 125 등 메타데이터, 실행 시 외부 호출 없음 — NOTICE.md "playground does not fetch fonts or photographs from external services at runtime"),
      자격증명 경로 2(kit/util.ts의 process.env.NODE_ENV, 오탐). child_process: scripts/dev.mjs(vite 실행)·film.mjs(자기 재실행) 2곳. 촬영 스크립트는 playwright-core로 로컬 Chrome(CHROME_PATH) 실행. postinstall 없음. 텔레메트리 없음.
      제3자 자산: kit/brands 아이콘(SVGL·Simple Icons·payment-icons·crypto-flat 라이선스 동봉), assets/app-icons.ts는 각 앱 번들 저작물로 MIT 범위 밖(NOTICE.md).
복사할 파일: 없음(지금). 조건부 채택 시 references/craft.md·framing.md만 디자인 참고로.
외부 스캐너: 미실행
```

## 1. 글이 말하는 것 (요약)

| 항목 | 내용 |
|---|---|
| 한 문장 | "실제 제품처럼 읽히고, 보는 사람에게 필요한 만큼만 단순화하고, 의도를 갖고 프레이밍하고, 모든 움직임에 원인이 있다" |
| 실패 5가지 | 너무 많음(모든 셀에 진짜 글자) · 와이어프레임 · 상자에 가둠(창 전체를 카드에 축소) · 평평함(모든 요소 같은 무게) · 밋밋함(브리프의 스타일이 안 보임) |
| 작업 7단계 | 매체·크기 → 룩(토큰 매핑) → 이야기 3~5박자 → 가장 가까운 gold 예제 복사 → 화면(진짜 문자열 3~7개) → 안무 → 리허설(촬영·프레임 검토) |
| 구성 | SKILL.md(19 KB), references 15개(craft·motion·framing·bento·looks·…), examples 70여 개(linear·slack·notion·excel·telegram 등 실제 제품 모사 포함), kit(React 부품·브랜드 아이콘·모션), scripts(film/jumps/layout/motion-watch), playground(Vite) |
| 검증 방식 | 데모를 촬영해 콘택트 시트로 펼치고 한 프레임 점프·텍스트 넘침을 스크립트가 잡음. "코드 읽기·정지 스크린샷으로는 중간 움직임을 못 본다"는 주장 |
| 저자의 한계 고백 | 검사기는 실수를 잡지 따분함을 못 잡음. 그림(일러스트·사진)은 못 그림. 모델이 좋아지면 이 스킬은 불필요해질 것 |

## 2. 매매 페이지에 맞는 것과 아닌 것

| 필요 | Stageshow | 우리 방식 권고 |
|---|---|---|
| 실제 잔고·주문·체결·arm 상태 표시 | 아님(가짜 데이터 전제) | `state.json`·원장 복사본·JSONL에서 읽어 정적 HTML로 렌더(기존 `render.py` 계열) |
| 장중 안전 | 해당 없음 | 봇 프로세스·설정·원장 원본을 건드리지 않음. 브로커 API 호출 없음 |
| 주문·취소 조작 | 해당 없음 | 넣지 않음. 주문 경로는 수신기 하나(토큰 공유, 원장 단일 기록자) |
| 신선도·불확실성 표시 | 해당 없음 | 데이터 시각·"미확인" 배너를 화면 맨 위에(기존 `render.py` 관행) |
| 보기 좋은 위계·절제 | 좋음(craft.md) | 핵심 7개 안팎만 숫자, 나머지는 접기·막대 |
| 발표·설명용 삽화 | 좋음 | 필요해지면 그때 조건부 채택 |

## 3. 읽기 전용 관제 페이지를 만든다면 (초안 범위)

- **한 화면 위계:** ① 수신기 상태(PID·단일 인스턴스·스트림 ready/gap·토큰 만료까지) ② 오늘 주문·체결·TTL 취소 건수와 미정산 ③ 보유 종목(kt00018 관측 스냅샷)과 arm별 포지션 ④ 틱 길이 추이(70~200 s 문제 추적) ⑤ 손익(수수료·거래세 파라미터 명시). 나머지는 접힘.
- **데이터 원천:** `telegram/state.json`, `runtime/logs/.execution-ledger.sqlite3`의 **복사본**(mode=ro), `YYYYMMDD.*.jsonl`, `observability/watchdog/`. 장중엔 파일 읽기만.
- **형태:** 봇 밖의 별도 스크립트가 정적 HTML을 생성(또는 127.0.0.1 전용 읽기 서버). 버튼 없음. 비밀값·앱키·토큰 미표시(`render.py`와 같은 원칙).
- **구현 주체:** Codex 요청서로 작성해 야간/주말에 진행. 수신기 코드와 분리된 새 모듈이라 배포 창 제약은 받지 않지만, 원장 파일을 직접 여는 코드는 금지(복사 후 읽기).

## 4. 이 조사의 한계
- SKILL.md 본문 전체·references 15개를 다 읽지 않았습니다(앞부분·목차·NOTICE·scripts 위주).
- "매매 페이지"가 관제(보기) 화면인지 조작 화면인지는 사용자가 명시하지 않아, 관제 화면으로 가정하고 조작 기능은 규칙상 제외했습니다.
