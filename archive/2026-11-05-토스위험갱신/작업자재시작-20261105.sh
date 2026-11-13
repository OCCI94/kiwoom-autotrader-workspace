#!/bin/bash
# 2026-11-05 사용자 승인("재시작+HALT 해제 승인", 14:58) 범위의 작업자 재시작.
# 대상: stability-watchdog -> toss-readonly -> telegram-candidates(HALT 보존 이동 후).
# 수신기(telegram)·telegram-account·telegram-nightly는 건드리지 않는다.
# 사람이 터미널에서 실행한다. 각 단계는 이전 프로세스가 완전히 사라진 뒤에만 다음으로 간다.
set -u
U=$(id -u)
LA="$HOME/Library/LaunchAgents"
PFX="com.local.kiwoom-autotrader"

current_pid() { /bin/launchctl print "gui/$U/$PFX.$1" 2>/dev/null | awk '/^\tpid = /{print $3}'; }

wait_gone() {
  local pid=$1 i=0
  while kill -0 "$pid" 2>/dev/null; do
    i=$((i+1))
    if [ "$i" -ge 90 ]; then echo "!! pid $pid 가 90초 뒤에도 살아 있음. 중단."; exit 1; fi
    sleep 1
  done
  echo "   pid $pid 종료 확인"
}

step() {
  local label=$1 old
  old=$(current_pid "$label")
  echo "== $label (현재 pid: ${old:-없음})"
  /bin/launchctl bootout "gui/$U/$PFX.$label" || echo "   bootout 반환 $? (미기동 상태면 정상)"
  [ -n "$old" ] && wait_gone "$old"
  if ! /bin/launchctl bootstrap "gui/$U" "$LA/$PFX.$label.plist"; then
    echo "!! bootstrap 실패. 중단. 이 작업자는 정지 상태로 남음."; exit 1
  fi
  sleep 3
  /bin/launchctl print "gui/$U/$PFX.$label" | grep -E '^\s*(state|pid) =' | head -2 | sed 's/^/   /'
}

step stability-watchdog
step toss-readonly

cd "$HOME/Library/Application Support/KiwoomAutotrader/config/research" || { echo "!! config/research 이동 실패"; exit 1; }
if [ -f telegram-candidates-HALT.json ]; then
  mv telegram-candidates-HALT.json "telegram-candidates-HALT.reviewed-$(date +%Y%m%d-%H%M%S).json" && echo "== HALT 보존 이동 완료"
else
  echo "== HALT 파일 없음(이미 이동됨)"
fi
step telegram-candidates

echo "== 완료 $(date '+%Y-%m-%d %H:%M:%S')"
