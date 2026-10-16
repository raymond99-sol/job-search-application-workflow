#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  printf '请先运行 ./setup.sh\n' >&2
  exit 1
fi
exec .venv/bin/python -m career_desk "$@"
