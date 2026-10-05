#!/usr/bin/env bash
# Attach a real terminal (script(1)) at each width and print the row tmux
# draws, before and after the prefix. Nothing is faked, so it catches what
# only a real client hits, such as a format too slow to draw.
# Usage: tests/attached.sh [width...]   (needs util-linux script, tmux 3.4)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCK="hintbar-attached-$$"
t() { tmux -L "$SOCK" "$@"; }

[ "$#" -gt 0 ] || set -- 80 120 200
for w in "$@"; do
  t kill-server 2>/dev/null || true
  # hold stdin open: script forwards EOF as Ctrl-D, ending the session.
  # A client needs a terminal type, and CI runners set no TERM.
  script -qfc "stty rows 30 cols $w; TERM=xterm-256color tmux -L $SOCK -f /dev/null new" /dev/null \
    < <(sleep 30) >/dev/null 2>&1 &
  sp=$!
  for _ in $(seq 50); do
    [ -n "$(t list-clients -F x 2>/dev/null)" ] && break
    sleep 0.1
  done
  t set-environment -g HINTBAR_STRICT 1
  # the defaults only, whatever is in ~/.config/tmux-hintbar
  t set-option -g @hintbar-keys /nonexistent
  t run-shell "$DIR/hintbar.tmux"
  cl="$(t list-clients -F '#{client_name}')"
  row() { t display -p -c "$cl" '#{E:status-format[1]}' | sed 's/#\[[^]]*\]//g'; }
  printf '%4s normal |%s\n' "$w" "$(row)"
  t send-keys -K -c "$cl" C-b
  printf '%4s prefix |%s\n' "$w" "$(row)"
  t send-keys -K -c "$cl" Escape
  t kill-server
  kill "$sp" 2>/dev/null || true
  wait "$sp" 2>/dev/null || true
done
