#!/bin/bash
# Standalone documentation conversion; does not start any trading service.
set -e
tr_docs_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
tr_docs_python=""
for tr_docs_candidate in \
  "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3
do
  if [ -x "$tr_docs_candidate" ]; then
    tr_docs_python="$tr_docs_candidate"
    break
  fi
done
if [ -z "$tr_docs_python" ]; then
  tr_docs_python="$(command -v python3 || true)"
fi
if [ -z "$tr_docs_python" ]; then
  printf '%s\n' 'Python 3가 필요합니다. Python을 설치한 뒤 다시 실행하세요.' >&2
  exit 1
fi
if ! "$tr_docs_python" -c 'import openpyxl' >/dev/null 2>&1; then
  printf '%s\n' 'openpyxl이 필요합니다. 다음 명령을 한 번 실행하세요:' >&2
  printf '  %q -m pip install -r %q\n' "$tr_docs_python" "$tr_docs_dir/requirements-tr-docs.txt" >&2
  exit 1
fi
exec "$tr_docs_python" "$tr_docs_dir/export_tr_docs.py" "$@"
