#!/bin/sh
# clip.sh copy | clip.sh paste PANE -- hintbar's side of the desktop
# clipboard, so tmux behaves like the rest of a Linux desktop.
#
# copy: stdin (tmux's copy-command) goes to the clipboard and, where there is
# one, the primary selection, which middle-click pastes elsewhere.
# paste: the primary selection is pasted into PANE; with none, tmux's own
# buffer is, as stock middle-click does.
#
# The tool is chosen on every run, since the display can change under a
# long-lived tmux server (say, after reattaching over SSH).

tool() {
  if [ -n "${WAYLAND_DISPLAY-}" ] && command -v wl-copy >/dev/null 2>&1; then echo wl
  elif [ -n "${DISPLAY-}" ] && command -v xclip >/dev/null 2>&1; then echo xclip
  elif [ -n "${DISPLAY-}" ] && command -v xsel >/dev/null 2>&1; then echo xsel
  elif command -v pbcopy >/dev/null 2>&1; then echo pb
  elif command -v clip.exe >/dev/null 2>&1; then echo win
  fi
}

# a file rather than a variable: $(...) would drop trailing newlines
tmp="$(mktemp "${TMPDIR:-/tmp}/hintbar-clip.XXXXXX")" || exit 1
trap 'rm -f "$tmp"' EXIT

case "$1" in
  copy)
    cat >"$tmp"
    # xclip and xsel stay in the background to serve the selection, so their
    # output must not hold tmux's pipe open
    case "$(tool)" in
      wl) wl-copy <"$tmp" >/dev/null 2>&1
        wl-copy --primary <"$tmp" >/dev/null 2>&1 ;;
      xclip) xclip -in -selection clipboard <"$tmp" >/dev/null 2>&1
        xclip -in -selection primary <"$tmp" >/dev/null 2>&1 ;;
      xsel) xsel --input --clipboard <"$tmp" >/dev/null 2>&1
        xsel --input --primary <"$tmp" >/dev/null 2>&1 ;;
      pb) pbcopy <"$tmp" ;;
      win) clip.exe <"$tmp" ;;
    esac
    ;;
  paste)
    pane="$2"
    case "$(tool)" in
      wl) wl-paste --primary --no-newline >"$tmp" 2>/dev/null ;;
      xclip) xclip -out -selection primary >"$tmp" 2>/dev/null ;;
      xsel) xsel --output --primary >"$tmp" 2>/dev/null ;;
    esac
    if [ -s "$tmp" ]; then
      tmux load-buffer -b hintbar-primary "$tmp" &&
        tmux paste-buffer -p -d -b hintbar-primary -t "$pane"
    else
      tmux paste-buffer -p -t "$pane"
    fi
    ;;
esac
