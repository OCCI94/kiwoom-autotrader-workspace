#!/bin/zsh
set -eu
exec "$(dirname -- "$0")/start.command" top-turnover --market 001 --exchange 3 --limit 20
