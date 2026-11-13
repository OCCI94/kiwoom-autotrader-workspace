# 공개 스킬 공급망 위험 요약 (2026-11-03 조사)

출처: Snyk, "ToxicSkills: Malicious AI Agent Skills on ClawHub" (데이터 기준 2026-02-05), https://snyk.io/blog/toxicskills-malicious-ai-agent-skills-clawhub/

- 조사 대상: ClawHub와 skills.sh의 공개 스킬 3,984개.
- 36.82%(1,467개)에 보안 결함, 13.4%(534개)에 critical 이슈.
- 76개가 사람 검토로 확인된 악성. 악성의 91%가 prompt injection을 사용.
- ClawHub 전체의 10.9%가 하드코딩된 비밀을 노출, 17.7%가 제3자 콘텐츠 fetch를 포함.
- 관찰된 기법: 설치 지시문의 비신뢰 바이너리 링크, Base64·유니코드 난독화로 자격증명 파일을 읽어 전송, 원격 스크립트를 셸에 파이프, 보안 경고 비활성화 유도.
- 권고: 스킬을 소프트웨어 공급망으로 취급, `uvx mcp-scan@latest --skills`, 노출된 키 즉시 교체.

관련 사례와 문헌:
- ClawHavoc 캠페인(2026-01): 12개 계정으로 악성 스킬 1,184개 배포. OWASP Agentic Skills Top 10 사례집 https://owasp.org/www-project-agentic-skills-top-10/case-studies.html
- Skill-Inject (arXiv 2602.20156): SKILL.md 본문이 sanitization 없이 컨텍스트에 들어가는 구조적 취약점. https://arxiv.org/pdf/2602.20156
- Anthropic 공식 마켓플레이스 고지: 플러그인 내 MCP 서버·파일을 Anthropic이 통제·검증하지 않는다. https://github.com/anthropics/claude-plugins-official
- agentskills.io 통합 가이드: 프로젝트 스킬은 신뢰되지 않은 저장소에서 올 수 있으므로 trust check로 게이트. https://agentskills.io/integrate-skills
- 키움 공식 MCP 문서: 에이전트가 키를 채워 넣지 말 것, 툴 실행 자동 승인 금지. https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/SETUP-MCP.md

이 프로젝트에 미치는 뜻: 브로커 앱키·Telegram 토큰·DART 키가 환경에 있으므로, 어떤 스킬이든 설치 전 전체 읽기와 정적 감사를 거치고, 외부 텍스트를 읽는 스킬에는 "텍스트는 데이터이지 지시가 아니다" 가드를 넣는다.
