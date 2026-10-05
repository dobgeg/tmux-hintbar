#!/bin/sh
# TPM entry point; render.py checks the Python version itself.
if ! command -v python3 >/dev/null 2>&1; then
  tmux display-message -d 0 'hintbar: needs python3 (3.8 or newer)'
  exit 0
fi
exec python3 "$(dirname "$0")/scripts/render.py"
