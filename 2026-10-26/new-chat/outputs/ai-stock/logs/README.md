# 자동매매 체결 기록

실행 중 `YYYYMMDD.csv`와 비공개 SQLite 원장이 이 폴더에 생성됩니다.
테스트용 체결을 실제 기록처럼 제공하지 않습니다. 첫 자동주문부터 전략·접수·체결을 추적합니다.

Mac 백그라운드 설치본의 경로는 `~/Library/Application Support/KiwoomAutotrader/runtime/logs`입니다.
프로젝트의 main.py를 직접 실행하거나 Windows start.bat로 실행하면 이 프로젝트의 logs 폴더를 사용합니다.
원장 DB도 함께 보존해야 재시작 후 중복 기록을 막고 과거 체결과 주문 의도를 복구할 수 있습니다.
