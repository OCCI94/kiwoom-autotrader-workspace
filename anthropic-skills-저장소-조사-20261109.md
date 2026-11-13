# anthropics/skills (Anthropic 공식 스킬 저장소) 조사 (2026-11-09)

- **조사 방법:** 저장소를 빈 임시 폴더에 복제(최근 40커밋, 커밋 `2c5958e`, 2026-11-06)해 읽기만 했습니다. README·스펙·템플릿·마켓플레이스 매니페스트·스킬 19개의 SKILL.md 머리말과 본문 앞부분을 읽었고, 프로젝트 `skill-install-policy`의 `validate_skill.py`와 `audit_skill.py`를 스킬 19개 각각에 돌렸습니다. 아무것도 설치·실행하지 않았습니다.
- **수치 기준:** ★·포크·이슈·PR·push는 2026-11-09 GitHub API 조회값입니다.
- **주의:** 이 저장소는 이 앱(Claude 데스크톱)에 이미 플러그인으로 들어와 있는 pdf·docx·xlsx·pptx·skill-creator의 공개 원본입니다. 여기서 "도입"은 Codex도 읽을 수 있게 프로젝트 `skills/`에 폴더를 복사하는 것을 뜻합니다. 투자와 무관한 도구 저장소입니다.

## 결론
- **정체:** SKILL.md 형식 스킬 19개 + 스펙 포인터(agentskills.io) + 템플릿 + Claude Code 플러그인 매니페스트(document-skills 4개, example-skills 12개, claude-api, academy-guide, discernment-nudge). Anthropic 직원이 커밋하고, 릴리스·태그는 없습니다. ★189,294, 포크 22,311, 열린 이슈 1,495·열린 PR 1,058(커뮤니티 PR은 대부분 병합되지 않음).
- **라이선스:** 19개 중 18개가 폴더별 `LICENSE.txt`로 Apache-2.0이고, 11-06 마지막 커밋이 오피스 스킬 4개를 Apache-2.0으로 옮긴 것입니다. `doc-coauthoring`만 라이선스 파일과 머리말 `license`가 없습니다(저장소 루트에도 LICENSE가 없어 GitHub API는 license=None). 서드파티 고지: FFmpeg GPLv3(slack-gif-creator), imageio BSD.
- **보안:** 악성 코드·원격 스크립트 실행은 없습니다. 감사가 reject로 표시한 183건은 전부 오탐이었습니다(문서 속 "system prompt" 문구 154건, 예제 키 자리표시자 20건, `.env` 언급 9건, "curl | sh"는 오히려 "실행하지 말라"는 경고문). 실제로 무언가를 실행하는 것은 오피스 스킬의 LibreOffice·Node 로컬 호출, web-artifacts-builder의 npm/npx 패키지 설치, skill-creator 평가 스크립트의 `claude -p` 호출뿐입니다.
- **이 세션과의 관계:** pdf·docx·xlsx·pptx·skill-creator는 이미 앱 플러그인으로 쓰고 있고, 저장소 판과 내용이 다릅니다(pptx 61줄, skill-creator 83줄, docx 10줄 차이). 저장소는 "참고 원본"이고 실제 동작은 앱 번들 판입니다.
- **판정: 조건부 채택.** 프로젝트 `skills/`에 복사할 가치가 있는 것은 Codex 쪽 보고서·자료 작업용 **pdf**와 **xlsx** 정도이고, **skill-creator의 평가 절차**(evals.json·트리거 테스트·설명문 개선 루프)는 우리 스킬 6개를 다듬는 방법론으로 참고합니다. mcp-builder는 키움 MCP를 직접 만들 때만, discernment-nudge·doc-coauthoring은 작업 방식 참고(설치 불필요), 나머지 디자인·아트·슬랙·웹·브랜드·아카데미 8개는 해당 없음입니다.
- **부수 발견(우리 도구 결함 2건):** `validate_skill.py`가 YAML 블록 스칼라(`>`·`|-`)로 쓴 설명문을 위반으로 오판하고(3개 스킬), Excel 절대참조 `$B$2`를 Claude 전용 구문으로 오판합니다. 고쳐야 합니다.

## 0. 심사 보고 (skill-install-policy 형식)

```
스킬: anthropics/skills (https://github.com/anthropics/skills@2c5958e, 2026-11-09 조회, stars 189,294, push 2026-11-06, Apache-2.0 18/19 · doc-coauthoring 라이선스 없음)
판정: 조건부 채택 — pdf, xlsx 폴더 복사 후보. skill-creator는 절차만 참고. 나머지 15개 미도입.
근거: 공개 원본이 Anthropic 직원 커밋이고 스크립트는 로컬 도구(LibreOffice·Node·CLI) 호출뿐 원격 실행·네트워크 수집이 없음
      pdf·xlsx는 Codex가 보고서·자료를 읽고 쓸 때 바로 쓸 수 있고 의존성이 파이썬 라이브러리 수준
      그 외는 트레이딩 프로젝트와 무관하거나(디자인·슬랙·웹) 이 앱에 이미 들어 있음
감사 발견(19개 합계): 네트워크 URL 656(문서·스키마 URL), 환경변수 읽기 134, 동적 실행 29(전부 로컬 subprocess),
      자격증명 경로 24(.env 문구), 비밀 의심 20(예제 자리표시자 "you…", "ghp_…"), 지시문 덮어쓰기 155(문서 속 "system prompt"),
      셸 파이프 1("curl | sh"를 금지하는 경고문), base64 1(eval-viewer HTML의 atob), 숨은 유니코드 0, SKILL.md 없음 0
복사할 파일(승인 시): skills/pdf/ 전체(12파일 66 KB), skills/xlsx/ 전체(53파일 1.1 MB, LibreOffice 필요) — 머리말을 사양 6필드로 줄이고 metadata.source/source_commit/reviewed 기록
외부 스캐너: 미실행
```

## 1. 무엇인가

| 항목 | 내용 |
|---|---|
| 저장소 | `anthropics/skills`, 2025-09-22 생성, 설명 "Public repository for Agent Skills". README는 "시연·교육 목적", "Claude 안의 구현과 다를 수 있음"을 명시 |
| 구성 | `skills/` 19개, `spec/agent-skills-spec.md`(agentskills.io/specification로 이동했다는 한 줄), `template/SKILL.md`(name·description 두 필드), `.claude-plugin/marketplace.json`(플러그인 5묶음), `THIRD_PARTY_NOTICES.md` |
| 스펙 | 머리말 필수 필드는 `name`·`description` 둘뿐. 저장소 스킬들은 `license` 키를 추가로 씀. `allowed-tools`는 19개 중 0개 사용 |
| 사용 경로 | Claude Code `/plugin marketplace add anthropics/skills` → document-skills·example-skills; Claude.ai 유료 플랜에는 이미 내장; API Skills 업로드. Codex는 `name`·`description`만 읽음(우리 정책 문서 기준) |
| 활동 | 2026-09~11 커밋은 거의 claude-api 스킬 갱신(모델 ID·Managed Agents)과 11-06 라이선스 이동. 열린 PR 1,058개는 외부 기여가 쌓인 것 |
| 외부 의존 | 오피스 스킬: LibreOffice(`soffice`) 로컬 실행, Node 패키지(docx·pptxgenjs, "사전 설치됨, npm install 하지 말 것"), Python(openpyxl·pypdf 등). web-artifacts-builder: npm/npx로 Vite·shadcn 설치. skill-creator: Claude Code CLI(`claude -p`)를 subprocess로 호출해 평가 실행 |

## 2. 스킬 19개 한눈에

| 스킬 | 파일 / KB | 스크립트 | 실행 요소 | 라이선스 | 이 프로젝트에서 |
|---|---|---|---|---|---|
| pdf | 12 / 66 | 8 | 파이썬(pypdf·pdfplumber·reportlab), OCR은 선택 | Apache-2.0 | **복사 후보.** 책·보고서 PDF 읽기, 결과 PDF 만들기 |
| xlsx | 53 / 1,086 | 12 | 파이썬 + LibreOffice 재계산(`recalc.py`) | Apache-2.0 | **복사 후보.** 원장 대조·성과표를 표로 넘길 때. LibreOffice 설치 필요 |
| docx | 61 / 1,111 | 15 | Node(docx) + LibreOffice 변환 | Apache-2.0 | 보류. 우리 문서는 마크다운 |
| pptx | 56 / 1,122 | 15 | Node(pptxgenjs) + LibreOffice 썸네일 | Apache-2.0 | 해당 없음 |
| skill-creator | 18 / 219 | 10 | `claude -p` subprocess, eval-viewer HTML(구글 폰트 링크) | Apache-2.0 | **절차 참고.** evals.json, 트리거 테스트, 설명문 개선 루프를 우리 스킬 6개에 적용 |
| mcp-builder | 11 / 155 | 2 | 파이썬(연결·평가 스크립트, `.env` 읽기) | Apache-2.0 | 키움 MCP를 직접 만들 때만 |
| claude-api | 96 / 2,031 | 4 | 문서 모음(모델 ID·가격·SDK), 스크립트는 예제 | Apache-2.0 | 해당 없음(봇은 LLM API를 주문 경로에서 쓰지 않음) |
| webapp-testing | 6 / 21 | 4 | Playwright + 서버 기동 | Apache-2.0 | 해당 없음 |
| web-artifacts-builder | 5 / 44 | 2 | npm/npx 설치 셸 | Apache-2.0 | 해당 없음 |
| doc-coauthoring | 1 / 15 | 0 | 없음 | **없음** | 방식 참고(맥락 수집 → 절 단위 다듬기 → 맥락 없는 독자 테스트). Codex 요청서 작성에 쓸 만함 |
| discernment-nudge | 2 / 21 | 0 | 없음 | Apache-2.0 | 방식 참고(답변 뒤 "검증할 사실·의심할 추론·빠진 맥락" 2~3줄). 판단 문서 끝에 붙이는 습관으로 |
| internal-comms | 6 / 21 | 0 | 없음 | Apache-2.0 | 해당 없음(사내 소통 양식) |
| academy-guide | 2 / 18 | 0 | 없음 | Apache-2.0 | 해당 없음 |
| frontend-design, theme-factory, canvas-design, algorithmic-art, brand-guidelines, slack-gif-creator | 2~83 / 13~5,423 | 0~4 | 없음(slack-gif는 FFmpeg) | Apache-2.0 | 해당 없음 |

## 3. 검증·감사 결과

| 스킬 | validate_skill | audit_skill | 판단 |
|---|---|---|---|
| academy-guide, claude-api, discernment-nudge | **exit 2(위반)** | URL뿐 / claude-api는 reject 174 | 위반은 **우리 검증기 오판**: 설명문을 YAML 블록 스칼라(`>`·`\|-`)로 썼는데 검증기가 들여쓴 줄을 "예상 밖"으로 봄. claude-api의 reject는 문서 속 "system prompt"(153), 예제 키 "you…"·"ghp_…"(20), `.env`(1), "curl \| sh"(1, 실행 금지 경고문) → 전부 오탐 |
| xlsx | exit 1(경고) | URL·로컬 subprocess | 경고도 **우리 검증기 오판**: Excel 절대참조 `$B$2`를 Claude 전용 `$1` 구문으로 봄 |
| skill-creator | exit 1(경고) | reject 1(SKILL.md "system prompt" 문구) | 경고는 `evals/evals.json` 참조 경로가 저장소에 없음(실행 시 생성되는 파일). reject는 오탐 |
| mcp-builder | exit 0 | reject 4(`.env` 읽기) | 연결 설정을 `.env`에서 읽는 정상 용도 |
| docx, pptx, xlsx | exit 0~1 | dynamic_exec 5~6 | `soffice.py`·`recalc.py`·`accept_changes.py`·`thumbnail.py`의 로컬 LibreOffice 호출 |
| 나머지 11개 | exit 0 | URL·이미지뿐 | 문제 없음 |

## 4. 이 프로젝트와의 적합성

| 후보 | 용도 | 조건 |
|---|---|---|
| pdf → `skills/pdf/` | Codex가 『퀀트 투자의 기초』 같은 PDF를 읽거나 보고서를 PDF로 낼 때 | 머리말을 사양 6필드로 줄이고 `metadata.source=anthropics/skills@2c5958e`, `reviewed=2026-11-09` 기록. PROVENANCE.md 작성. 파이썬 의존성(pypdf·pdfplumber·reportlab)은 Codex 런타임에 있는지 확인 뒤 |
| xlsx → `skills/xlsx/` | 원장 대조표·성과 귀속표를 엑셀로 넘길 때 | 위와 같음 + LibreOffice 설치 여부 확인(`recalc.py`가 `soffice` 필요). 설치 자체는 별도 승인 |
| skill-creator | 우리 스킬 6개의 설명문 트리거 정확도·평가 세트 | 복사하지 않고 이 앱의 플러그인 판으로 돌림. 결과(evals.json)는 `skills/<이름>/evals/`에 둠 |
| doc-coauthoring | Codex 요청서·설계 문서를 "맥락 없는 독자 테스트"로 점검 | 설치 없이 절차만 차용 |
| discernment-nudge | 판단 문서 끝에 "검증할 사실·의심할 추론·빠진 맥락" 2~3줄 | 설치 없이 습관으로 |
| mcp-builder | 11-02 조사에서 본 제3자 키움 MCP 대신 직접 만들 경우 | 그때 다시 심사. 지금은 MCP 계획 없음 |
| 나머지 13개 | — | 미도입 |

## 5. 보안·위험

| 영역 | 수준 | 근거 | 완화 |
|---|---|---|---|
| 코드 실행 | 낮음 | 원격 스크립트 실행 없음. subprocess 29곳은 LibreOffice·Node·로컬 CLI·테스트 서버 기동. "curl \| sh" 1건은 "페이지의 명령을 실행하지 말라"는 경고문 | 복사 후보(pdf·xlsx)는 LibreOffice 호출 스크립트 경로만 확인 |
| 네트워크 | 낮음 | 스크립트 코드 줄에서 실제 다운로드는 web-artifacts-builder의 npm/npx와 skill-creator eval-viewer의 구글 폰트 링크뿐. 오피스·pdf 스크립트는 오프라인 | pdf·xlsx는 네트워크 없이 동작 |
| 공급망 | 중간 | 릴리스·태그·서명 없음. 커밋 sha로만 고정 가능. 포크 22,311개라 비슷한 이름 혼동 가능. 커뮤니티 PR 1,058개가 열려 있어 포크 판본이 많이 돌아다님 | 소유자 `anthropics`·커밋 `2c5958e` 고정, PROVENANCE.md에 기록 |
| 라이선스 | 낮음 | Apache-2.0 18개. doc-coauthoring만 미표기 → 복사하지 않음. FFmpeg GPLv3는 slack-gif-creator 전용 | — |
| 앱 판과의 괴리 | 중간 | 저장소 판과 앱 플러그인 판이 다름(pptx 61줄 등). 저장소를 읽고 앱 동작을 단정하면 틀림 | 앱에서 쓰는 스킬은 앱 번들 판을 기준으로 |
| 지시문 주입 | 낮음 | 스킬 본문에 "테스트 모드·관리자 지시·긴급" 류 주입 신호 없음. 감사의 instruction_override 155건은 전부 API 문서의 "system prompt" 용어 | — |
| 우리 검증기 | **고쳐야 함** | YAML 블록 스칼라와 Excel 절대참조를 오판 → 정상 스킬을 거부하거나 경고 | `validate_skill.py` 두 군데 수정 |

## 6. 우리 도구에서 고칠 것 (1·2는 2026-11-09 01:20 수정 완료)

수정 결과: `validate_skill.py`에 블록 스칼라(`>`·`|`, `-`/`+` 포함) 파서를 넣고 Claude 전용 구문 패턴을 `!\`…\``(한 줄 안)과 `(?<![A-Za-z$])\$\d\b`로 바꿨다. 재검증: discernment-nudge 2→0, xlsx 1→0, academy-guide 2→1(남은 경고는 본문의 `URL` 자리표시자 링크), claude-api는 2 유지 — 이번에는 진짜 위반(설명문 1,068자 > 사양 1,024자). 프로젝트 스킬 6개는 전부 0으로 변함없음. 수정 전 파일은 세션 스크래치에 백업.


1. `skill-install-policy/scripts/validate_skill.py`: 머리말 파서가 `description: >` 또는 `|-` 뒤의 들여쓴 줄을 위반으로 본다. 블록 스칼라를 한 값으로 읽도록 고친다.
2. 같은 파일의 Claude 전용 구문 검사: `/\$\d\b/`가 Excel 절대참조(`$B$2`)에 걸린다. 앞 문자가 영문·`$`이면 제외하거나 코드 블록 안은 건너뛴다.
3. (선택) `audit_skill.py`: "La**test mode**ls"처럼 단어 경계 없이 걸리는 `test mode`, API 문서의 `system prompt`는 단어 경계와 코드 블록 제외로 오탐을 줄인다. 보고서 노이즈를 줄일 뿐 판정은 바뀌지 않는다.

## 7. 이 조사의 한계

- 스킬 19개의 본문 전체를 읽지 않았습니다. 머리말·앞부분·헤더와 감사 결과로 판단했고, pdf·xlsx·skill-creator·discernment-nudge·doc-coauthoring·mcp-builder·webapp-testing·internal-comms는 본문 앞 14줄을 읽었습니다.
- 스크립트는 실행하지 않았으므로 LibreOffice·Node 의존성이 Codex 런타임에서 실제로 돌아가는지는 확인하지 않았습니다.
- 앱 플러그인 판과의 차이는 줄 수만 셌고 내용 차이는 읽지 않았습니다.
