#!/bin/sh
set -eu
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,14), "请使用 Python 3.11–3.13，建议 3.13"'
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
printf '\n安装完成。启动：./start.sh\n'
