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
    print 'Python 3.11 이상이 필요합니다. README.md의 실행 안내를 확인하세요.'
    read -r '?Enter를 누르면 닫습니다. '
    exit 1
  fi
fi

"$task_python" -m kiwoom_autotrader demo --config config.example.json
read -r '?Enter를 누르면 닫습니다. '
