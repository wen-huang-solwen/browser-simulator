#!/usr/bin/env bash
# Facebook Session Creator — macOS / Linux launcher.
# Double-click this file (or run: bash run.sh) to start.

set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo ""
    echo "ERROR: Python is not installed."
    echo ""
    echo "Please install Python 3.9 or newer from:"
    echo "    https://www.python.org/downloads/"
    echo ""
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
fi

# Create a local virtual environment on first run. This avoids PEP 668
# (externally-managed-environment) errors on Homebrew / system Pythons.
if [ ! -x ".venv/bin/python" ]; then
    echo "First-time setup — creating local Python environment..."
    "$PY" -m venv .venv
fi

VENV_PY=".venv/bin/python"
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV_PY" fb_session.py
status=$?

if [ $status -ne 0 ]; then
    echo ""
    read -n 1 -s -r -p "Press any key to close..."
fi
