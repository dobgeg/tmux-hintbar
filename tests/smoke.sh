#!/usr/bin/env bash
# Load hintbar into a throwaway tmux server and print each mode's row, styles
# stripped; extra hint lines are joined by ⏎.
# Usage: [WIDTH=n] [VI=on] [HINT_LINES=n|auto] tests/smoke.sh [keys-file] [style]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCK="hintbar-smoke-$$"
t() { tmux -L "$SOCK" "$@"; }
trap 't kill-server 2>/dev/null || true' EXIT

t -f /dev/null new-session -d
# make render exit non-zero on errors
t set-environment -g HINTBAR_STRICT 1
# without a keys file argument, ignore any in ~/.config
t set -g @hintbar-keys "$(realpath "${1:-/nonexistent}" 2>/dev/null || echo /nonexistent)"
[ -n "${2:-}" ] && t set -g @hintbar-style "$2"
[ -z "${VI:-}" ] || t set -g @hintbar-extra-vi on
[ -z "${HINT_LINES:-}" ] || t set -g @hintbar-lines "$HINT_LINES"
t run-shell "$DIR/hintbar.tmux"

# a second load takes the cached path and must change nothing
state() { t show -g | grep -E '^(@hintbar-|status)' | grep -v '^@hintbar-was-' | md5sum; }
before="$(state)"
t run-shell "$DIR/hintbar.tmux"
if [ "$(state)" != "$before" ]; then
  echo "FAIL: loading again changed hintbar's options" >&2
  exit 1
fi

# every hint line, joined by RS for show() to split again
nl=$(( $(t show -gv status | sed 's/^on$/1/') - 1 ))
row=''
for ((l = 1; l <= nl; l++)); do
  [ "$l" -gt 1 ] && row+=$'\x1e'
  row+="$(t show -gv "status-format[$l]")"
done
badge="$(t show -gv @hintbar-badge)"

# No client is attached, so substitute WIDTH into the width searches, one
# option at a time (tmux's command size limit). Re-renders overwrite them,
# so this runs again after the prefix change below.
fake_width() {
  local name v
  while read -r name _; do
    [[ $name == @hintbar-tree-* || $name == @hintbar-row-* ]] || continue
    v="$(t show -gv "$name")"
    t set -g "$name" "${v//'#{client_width}'/${WIDTH:-1000}}"
  done < <(t show -g)
}
fake_width

# show NAME TABLE [PANE_MODE] -- pin the key table and pane mode, expand
show() { # show NAME TABLE [PANE_MODE]
  local r="$row" b="$badge"
  r="${r//'#{client_key_table}'/$2}"; r="${r//'#{pane_mode}'/${3-}}"
  b="${b//'#{client_key_table}'/$2}"; b="${b//'#{pane_mode}'/${3-}}"
  printf '%-7s %s|%s\n' "$1" \
    "$(t display -p "$b" | sed 's/#\[[^]]*\]//g')" \
    "$(IFS=$'\x1e'; for line in $r; do t display -p "$line" | sed 's/#\[[^]]*\]//g'; done | awk 'NR > 1 { printf " ⏎ " } { printf "%s", $0 }')"
}

echo "status: $(t show -gv status)"
t set -g mode-keys emacs
show root   root
show prefix prefix
show copy   root   copy-mode
show view   root   view-mode
show both   prefix copy-mode
show resize resize
for m in tree buffer client options clock; do show "$m" root "$m-mode"; done
t set -g mode-keys vi
show copy-vi root  copy-mode
t set -g prefix C-a
fake_width
show C-a    root
