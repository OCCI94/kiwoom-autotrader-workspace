#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"

task_python="${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [[ ! -x "$task_python" ]]; then
  if [[ -x /opt/homebrew/bin/python3 ]]; then
    task_python=/opt/homebrew/bin/python3
  elif [[ -x /usr/local/bin/python3 ]]; then
    task_python=/usr/local/bin/python3
  else
    print 'Python 3.11 이상이 필요합니다. README.md를 확인하세요.'
    if [[ -t 0 ]]; then read -r '?Enter를 누르면 닫습니다. '; fi
    exit 1
  fi
fi

# Credentials are read by Python from Keychain; never pass them as arguments.
task_exit=0
"$task_python" -m kiwoom_autotrader "${@:-telegram-listen}" || task_exit=$?
if [[ -t 0 ]]; then read -r '?Enter를 누르면 닫습니다. '; fi
exit "$task_exit"
