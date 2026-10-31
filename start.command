#!/bin/bash
# Zoomollo for Mac (and Linux): double-click to start.
# First run sets everything up (a minute or two); after that it starts in seconds.
cd "$(dirname "$0")" || exit 1

pick_python() {
  for p in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  return 1
}

if [ ! -x .venv/bin/zoomollo ]; then
  PY=$(pick_python)
  if [ -z "$PY" ]; then
    echo "Zoomollo needs Python 3.10 or newer."
    echo "Opening the download page: install it, then double-click start.command again."
    open "https://www.python.org/downloads/" 2>/dev/null || xdg-open "https://www.python.org/downloads/" 2>/dev/null
    read -r -p "Press Enter to close." _
    exit 1
  fi
  echo "Setting up Zoomollo (first run only)..."
  "$PY" -m venv .venv && .venv/bin/python -m pip install --quiet --upgrade pip && .venv/bin/python -m pip install --quiet -e . || {
    echo "Setup failed. Check your internet connection and try again."; read -r -p "Press Enter to close." _; exit 1; }
fi

[ -f zoomollo-settings.txt ] || cp zoomollo-settings.example.txt zoomollo-settings.txt
.venv/bin/zoomollo serve --open
