#!/bin/bash
# =====================================================================
#  WPU CET Examination Portal - run on the room's own network (Mac/Linux)
#
#     chmod +x start-exam-lan.sh     (once)
#     ./start-exam-lan.sh
# =====================================================================
set -e
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null || { echo "Python 3 is not installed."; exit 1; }

if [ ! -x ".venv/bin/python" ]; then
  echo "First run - setting up. This takes a few minutes, once."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m pip install waitress
fi

exec .venv/bin/python run_lan.py --port 8000
