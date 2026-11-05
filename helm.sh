#!/usr/bin/env bash
# Запуск HELM. При первом запуске создаёт venv (PySide6 берётся системный, если есть).
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
if [ ! -x "$DIR/.venv/bin/python" ]; then
  /usr/bin/python3 -m venv --system-site-packages "$DIR/.venv"
  "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
fi
cd "$DIR" && exec "$DIR/.venv/bin/python" -m helm "$@"
