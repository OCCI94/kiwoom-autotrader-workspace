# alertprobe-20261110

2026-11-10 장중에 "자동매매 원장: … 완료하지 못했습니다" 알림 반복의 원인을 찾을 때 쓴 스크립트와 원자료다. 설명은 `../codex-요청-원장알림-반복-20261110.md`에 있다.

- `poll_journal.py <상태 디렉터리> <출력 파일> <초>`: 원장, 롤백 저널, TR rate 파일, state.json의 mtime과 크기만 20 ms 간격으로 본다. 파일을 열지 않는다.
- `harness/replay_*.py <harness 디렉터리> <일자> <예산 초> [<읽기 유지 초>]`: 원장 복사본(`harness/logs/.execution-ledger.sqlite3`)과 패키지 사본(`harness/pkg/kiwoom_autotrader`, C는 `harness/pkgC`)으로만 돈다. 네트워크는 스크립트 안에서 막혀 있다. 원장 복사본과 패키지 사본은 용량 때문에 이 폴더에 넣지 않았다.
- 운영 원장이나 운영 패키지를 직접 가리키지 않는다. `HOME`은 빈 격리 디렉터리로 준다.
