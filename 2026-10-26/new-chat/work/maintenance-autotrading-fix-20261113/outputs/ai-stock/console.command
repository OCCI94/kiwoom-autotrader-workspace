#!/bin/zsh
set -eu
exec "$(dirname -- "$0")/start.command" telegram-console
