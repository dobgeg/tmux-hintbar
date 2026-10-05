"""hintbar: turn the keys files into tmux formats and install them.

Runs at load and when a @hintbar option changes; tmux redraws the formats
itself, so nothing polls. A run is one read from tmux, the work here, one
script of writes, then a read and a write to record what was bound. If
nothing hintbar reads has changed since the last run, it stops after the
first read.
"""
import hashlib
import os
import re
import shutil
import sys
import time

from . import layout
from .keys import PANE_MODES, Bind, ConfigError, Hint, Parser, Table, pretty_key
from .style import (C, GLYPHS, PALETTE, Shapes, ThemeError, cols, find_theme, is_colour,
                    load_theme, theme_dirs, theme_names)
from .tmux import RS, US, Script, State, last_field, read_keys, show, tmux

PKG = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.dirname(os.path.dirname(PKG))
ENTRY = os.path.join(DIR, 'scripts', 'render.py')
CLIP_TOOLS = ('wl-copy', 'xclip', 'xsel', 'pbcopy', 'clip.exe')

TAKEN = [
    'status-style', 'status-left', 'status-right', 'window-status-format',
    'window-status-current-format', 'message-style', 'message-command-style',
    'mode-style', 'pane-border-style', 'pane-active-border-style',
    'pane-border-status', 'pane-border-lines', 'pane-border-indicators',
    'pane-border-format', 'mode-keys',
]
NAMES = '''
  @hintbar-keys @hintbar-style @hintbar-glyphs @hintbar-theme @hintbar-bar-bg
  @hintbar-key-fg @hintbar-block-bg @hintbar-block-fg @hintbar-row
  @hintbar-extra-vi @hintbar-reload @hintbar-mouse @hintbar-clipboard
  @hintbar-status-style @hintbar-panes @hintbar-window-bg @hintbar-clock
  @hintbar-lines @hintbar-was-status @hintbar-preview @hintbar-user-binds-n
  @hintbar-preview-first @hintbar-icons @hintbar-toggle-key @hintbar-hidden
  @hintbar-dividers @hintbar-user-binds @hintbar-inputs @hintbar-warnings @hintbar-stock
  status status-left-length status-right-length copy-command version
  @hintbar-markers @hintbar-was-bind-reload prefix @hintbar-send-prefix-key
  @hintbar-was-bind-send-prefix @hintbar-fingerprint @hintbar-sourcing
  @hintbar-was-copy-command @hintbar-was-bind-paste @hintbar-was-paste-old
  status-format[0] status-format[1] status-format[2] status-format[3]
  status-format[4] @hintbar-was-pane-border-tail @hintbar-made
'''.split() + TAKEN + ['@hintbar-was-' + o for o in TAKEN]

# Written last, from what the run itself bound, so left out of the input
# hash: they're a function of everything in it.
RECORDS = ('@hintbar-inputs', '@hintbar-warnings', '@hintbar-user-binds',
           '@hintbar-user-binds-n', '@hintbar-was-bind-')

# re-render when one of these @hintbar options (or the prefix) changes
LIVE = ('keys|style|glyphs|theme|panes|bar-bg|key-fg|block-bg|block-fg|row|lines|preview|'
        'preview-first|icons|dividers|markers|extra-vi|reload|mouse|clipboard|status-style|'
        'window-bg|clock|toggle-key|hidden')

# events that can change the lines auto needs: size, attach, and which pane
# (so which pane mode) shows
RESIZE_HOOKS = ('client-resized', 'client-attached', 'pane-mode-changed',
                'window-pane-changed', 'session-window-changed', 'client-session-changed')

ICON_MOUSE, ICON_DATE, ICON_TIME = '\U000f037d', '\U000f00ed', '\U000f0150'  # Nerd Fonts 3


class Stop(Exception):
    """A fatal problem: shown, and the run ends."""


def is_record(name):
    return name.startswith(RECORDS)


def input_hash(state, files, env):
    """A hash of everything a run reads, minus what it records at the end."""
    h = hashlib.sha1()
    h.update(repr(sorted((k, v) for k, v in state.now.items() if not is_record(k))).encode(
        'utf-8', 'surrogateescape'))
    for line in state.options:
        if not is_record(line.split(' ', 1)[0]):
            h.update(line.encode('utf-8', 'surrogateescape') + b'\n')
    h.update(repr((state.session, sorted(state.bound.items()),
                   sorted((state.keys or {}).items()), files, env)).encode(
        'utf-8', 'surrogateescape'))
    return h.hexdigest()


def stat(path):
    try:
        s = os.stat(path)
        return path, s.st_mtime_ns, s.st_size
    except OSError:
        return path, None


class Run:
    """One render. The steps run in order from main(); each reads and
    writes the attributes the earlier ones set."""

    def __init__(self):
        self.warnings = []
        self.script = Script()
        self.claimed = []  # (name, "table key") hintbar bound itself

    def warn(self, msg):
        print('hintbar: ' + msg, file=sys.stderr)
        self.warnings.append(msg)

    def opt(self, name, default=''):
        return self.now.get(name) or default

    # ── read ──────────────────────────────────────────────────────────
    def read(self):
        self.state = State(NAMES)
        self.now, self.bound = self.state.now, self.state.bound
        if self.state.session:
            s = self.state.session
            self.warn("%s set without -g (this session only) is ignored: hintbar's options "
                      "are global; clear with set -u %s" % (' '.join(s), s[0]))
        # A reload is sourcing the config and runs hintbar once at the end.
        # A guard older than a minute is from a reload that died.
        if self.opt('@hintbar-sourcing'):
            try:
                if time.time() - int(self.now['@hintbar-sourcing']) <= 60:
                    return False
            except ValueError:
                pass
        # tmux version as 302 for 3.2; builds that don't say (master) count
        # as new. 3.2 brought the numeric format operators and hook arguments.
        m = re.search(r'([0-9]+)\.([0-9]+)', self.now['version'])
        self.v = int(m.group(1)) * 100 + int(m.group(2)) if m else 9999
        if self.v < 302:
            raise Stop('needs tmux 3.2 or newer; this is tmux ' + self.now['version'])
        self.xdg = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
        keys_file = self.opt('@hintbar-keys', os.path.join(self.xdg, 'tmux-hintbar', 'keys.conf'))
        if keys_file.startswith('~'):
            keys_file = os.environ.get('HOME', '') + keys_file[1:]
        self.keys_file = keys_file
        self.themes = theme_dirs(self.xdg, DIR)
        self.theme_file = find_theme(self.now['@hintbar-theme'], self.themes) \
            if self.opt('@hintbar-theme') else None
        return True

    def inputs(self):
        """Files and environment a run depends on, besides tmux's state."""
        files = [stat(os.path.join(PKG, f)) for f in sorted(os.listdir(PKG)) if f.endswith('.py')]
        files += [stat(p) for p in (ENTRY, os.path.join(DIR, 'defaults.conf'),
                                    os.path.join(DIR, 'defaults-vi.conf'), self.keys_file,
                                    self.theme_file or '')]
        env = [os.environ.get(k) for k in ('VISUAL', 'EDITOR', 'HOME', 'XDG_CONFIG_HOME',
                                           'HINTBAR_DUMP', 'HINTBAR_STRICT')]
        env.append('VISUAL' in os.environ)
        env.append(tuple(bool(shutil.which(t)) for t in CLIP_TOOLS))
        self.clip_tool = any(env[-1])
        return files, env

    # ── settings ──────────────────────────────────────────────────────
    def settings(self):
        opt = self.opt
        self.style = opt('@hintbar-style', 'plain')
        # ANSI colours by default, so the bar follows the terminal's theme.
        # A theme replaces them; the single colour options override either.
        pal = dict(PALETTE)
        if opt('@hintbar-theme'):
            if not self.theme_file:
                self.warn("unknown @hintbar-theme '%s', using terminal colours (themes: %s)"
                          % (self.now['@hintbar-theme'], ' '.join(theme_names(self.themes))))
            else:
                try:
                    pal.update(load_theme(self.theme_file))
                except ThemeError as e:
                    self.warn(str(e))
        pal['key'] = pal['key'] or pal['alert']
        self.pal = pal

        def colour(name, default):
            v = self.now.get(name, '')
            if not v:
                return default
            if is_colour(v):
                return v.lower()
            self.warn("%s '%s' isn't a colour tmux knows, using %s" % (name, v, default))
            return default

        # "default" means the bar's own colour, whoever set it, so hintbar
        # blends into another theme's bar.
        bar_bg = colour('@hintbar-bar-bg', 'default')
        # The bar's concrete colour, to draw arrow left edges in swapped
        # colours: the bar option, else hintbar's own status-style, else a
        # bg= in the user's. Empty when unknown or transparent.
        bar_fill = bar_bg
        if bar_fill == 'default':
            ss = self.now['status-style']
            m = re.search(r',bg=([^,]+),', ',' + ss + ',')
            if opt('@hintbar-status-style', 'on') == 'on' and (
                    ss == 'bg=green,fg=black' or (self.now['@hintbar-was-status-style'] and
                                                  ss == self.now['@hintbar-was-status-style'])):
                bar_fill = pal['bar']
            else:
                bar_fill = m.group(1).lower() if m else ''
        if bar_fill in ('default', 'terminal'):
            bar_fill = ''
        key_fg = colour('@hintbar-key-fg', pal['key'])
        block_bg = colour('@hintbar-block-bg', pal['block'])
        block_fg = colour('@hintbar-block-fg', pal['label'])
        self.win_bg = colour('@hintbar-window-bg', pal['window'])
        self.line_settings()

        if self.style in GLYPHS:
            enter, leave, swap = GLYPHS[self.style]
        elif self.style == 'custom':
            enter, leave = (self.now['@hintbar-glyphs'].split() + ['', ''])[:2]
            swap = ''
            if not leave:
                self.warn("@hintbar-style custom needs two glyphs in @hintbar-glyphs, e.g. "
                          "'▐ ▌'; using plain")
                enter = leave = ''
        else:
            self.warn("unknown @hintbar-style '%s', using plain (styles: plain pill slant "
                      "arrow custom)" % self.style)
            enter, leave, swap = GLYPHS['plain']
        self.shapes = Shapes(self.style, enter, leave, swap, bar_bg, bar_fill, pal['text'],
                             key_fg, block_bg, block_fg)
        # Icons need a Nerd Font, which a shaped style usually means; off
        # covers terminals that draw the shapes without one.
        icons = opt('@hintbar-icons', 'auto')
        if icons not in ('on', 'off'):
            if icons != 'auto':
                self.warn("@hintbar-icons should be on, off or auto, not '%s'; using auto"
                          % icons)
            icons = 'on' if self.style in ('pill', 'slant', 'arrow') else 'off'
        self.icons = icons

    def line_settings(self):
        """Hint lines: tmux has at most 5 status lines, starting at row.
        auto lays out the maximum; the resize hooks show as many as needed."""
        opt = self.opt
        try:
            self.row = int(opt('@hintbar-row', '1'))
        except ValueError:
            raise Stop("@hintbar-row should be a number, not '%s'" % self.now['@hintbar-row'])
        lines = opt('@hintbar-lines', '1')
        max_lines = max(5 - self.row, 1)
        if lines == 'auto':
            nlines = max_lines
        elif re.match(r'^[1-9][0-9]*$', lines):
            nlines = int(lines)
            if nlines > max_lines:
                self.warn("@hintbar-lines %s doesn't fit (tmux has 5 status lines, hints "
                          "start at line %d); using %d" % (lines, self.row, max_lines))
                nlines = max_lines
        else:
            self.warn("@hintbar-lines should be a number or auto, not '%s'; using 1" % lines)
            lines, nlines = '1', 1
        self.lines, self.nlines = lines, nlines
        # @hintbar-hidden (the toggle key) keeps the layouts but shows no lines
        self.shown = 0 if self.now['@hintbar-hidden'] else nlines
        self.extra_vi = opt('@hintbar-extra-vi', 'off')
        self.reload = opt('@hintbar-reload', 'on')

    # ── hintbar's own bindings ────────────────────────────────────────
    def manage_key(self, name, table, key, flag, stock, stock_note, note, want, *command):
        """On: bind KEY if it's free, stock or still ours. Off: restore
        stock, with its note, if the key is still ours."""
        q = self.script
        cur = self.bound.get(table + ' ' + key, '')
        was = self.now.get('@hintbar-was-bind-' + name, '')
        if want == 'on':
            if not cur or cur == stock or (was and cur == was):
                q('bind-key', *([flag] if flag else []), '-N', note, '-T', table, key, *command)
                self.claimed.append((name, table + ' ' + key))
        elif was and cur == was:
            if stock:
                q('bind-key', *(['-N', stock_note] if stock_note else []), '-T', table, key,
                  *stock.split())
            else:
                q('unbind-key', '-T', table, key)
            q('set-option', '-gu', '@hintbar-was-bind-' + name)

    def own_bindings(self):
        q, now = self.script, self.now
        # shell-quoted with backslashes, as it also sits inside tmux's '...'
        self.self_cmd = re.sub(r'([^A-Za-z0-9_./-])', r'\\\1', ENTRY)
        # prefix r sources the config files tmux started with, then reruns
        # hintbar, so options anywhere in the file apply. It's a command
        # alias, also typeable at the prompt, called through run-shell -C
        # because tmux expands aliases when a binding is defined. Slot 7213
        # avoids the user's own. The guard stops renders triggered while
        # sourcing; it's a timestamp so a reload that dies can't leave it set.
        reload_sh = ('tmux set -g @hintbar-sourcing "$(date +%s)"; c=#{q:config_files}; IFS=,; '
                     'set -f; for f in $c; do tmux source-file "$f"; done; '
                     'tmux set -gu @hintbar-sourcing; ')
        q('set-option', '-s', 'command-alias[7213]',
          "hintbar-reload=run-shell '%s%s' ; refresh-client ; display-message 'Reloaded "
          "config'" % (reload_sh, self.self_cmd))
        self.manage_key('reload', 'prefix', 'r', '', 'refresh-client',
                        'Redraw the current client', 'Reload the tmux config and redraw',
                        self.reload, 'run-shell', '-C', 'hintbar-reload')

        # With a prefix other than C-b, bind it to send-prefix too (stock
        # only does C-b C-b), where the key is free or still ours; undone
        # when it changes.
        pfx, sp_old = now['prefix'], now['@hintbar-send-prefix-key']
        if sp_old and sp_old != pfx:
            if self.bound.get('prefix ' + sp_old, '') == now['@hintbar-was-bind-send-prefix']:
                q('unbind-key', '-T', 'prefix', sp_old)
                self.bound['prefix ' + sp_old] = ''
            q('set-option', '-gu', '@hintbar-send-prefix-key')
            q('set-option', '-gu', '@hintbar-was-bind-send-prefix')
        if pfx not in ('C-b', 'None'):
            cur = self.bound.get('prefix ' + pfx, '')
            if not cur or cur == now['@hintbar-was-bind-send-prefix']:
                q('bind-key', '-N', 'Send the prefix key', '-T', 'prefix', pfx, 'send-prefix')
                q('set-option', '-g', '@hintbar-send-prefix-key', pfx)
                self.claimed.append(('send-prefix', 'prefix ' + pfx))
                self.bound['prefix ' + pfx] = 'send-prefix'
        # The SEND PREFIX hint shows the key that actually sends it, if any.
        if self.bound.get('prefix ' + pfx) == 'send-prefix':
            self.send_key = pfx
        else:
            self.send_key = next((k[7:] for k, c in self.bound.items()
                                  if k.startswith('prefix ') and c == 'send-prefix'), '')

    # ── parse ─────────────────────────────────────────────────────────
    def parse(self):
        self.pfx_shown = pretty_key(self.now['prefix'])
        p = Parser(self.shapes, self.pal, is_colour, DIR, self.pfx_shown, self.send_key,
                   self.reload, self.warn)
        try:
            p.parse(os.path.join(DIR, 'defaults.conf'))
            if self.extra_vi == 'on':
                p.parse(os.path.join(DIR, 'defaults-vi.conf'))
            if os.access(self.keys_file, os.R_OK):
                p.parse(self.keys_file)
        except ConfigError as e:
            raise Stop(str(e))
        self.tables, self.binds = p.tables, p.binds
        # @hintbar-toggle-key KEY binds prefix KEY to hide and show the
        # hint lines. It goes first, so a keys file binding the same key
        # wins, and is undone like a removed bind line when unset.
        if self.now['@hintbar-toggle-key']:
            self.binds.insert(0, Bind(
                'prefix', self.now['@hintbar-toggle-key'], '', "- Show or hide hintbar's hints",
                "if-shell -F '#{@hintbar-hidden}' 'set-option -gu @hintbar-hidden' "
                "'set-option -g @hintbar-hidden on'", False, '@hintbar-toggle-key'))

    # ── bind lines ────────────────────────────────────────────────────
    # Bind lines are bound on every run. @hintbar-user-binds records what
    # hintbar bound and what was there before, so removing a line restores
    # the old binding unless the key was rebound meanwhile. Records are
    # separated by RS; fields (US): table, key, ours, old repeat, old note,
    # old command.
    @staticmethod
    def bind_args(b):
        args = ['bind-key'] + (['-r'] if b.repeat else [])
        if b.label.startswith('- '):
            args += ['-N', b.label[2:]]
        elif b.label != '-':
            args += ['-N', b.label]
        return args + ['-T', b.table, b.key, b.command]

    def put_back(self, t, k, r, note, cmd):
        # list-keys prints several commands as cmd1 \; cmd2; as one string,
        # tmux wants cmd1 ; cmd2
        self.script('bind-key', *(['-r'] if r == '1' else []), *(['-N', note] if note else []),
                    '-T', t, k, cmd.replace(' \\; ', ' ; '))

    def stock_keys(self, wanted):
        """tmux's stock bindings for the keys in WANTED ("table US key"):
        cached per tmux version in @hintbar-stock, else read from a
        server-less tmux in a private directory (so nobody else can answer
        on the socket path)."""
        cached = self.now['@hintbar-stock'].split(RS)
        stock = {}
        if cached[0] == self.now['version']:
            for e in cached[1:]:
                t, k, c = (e.split(US, 2) + ['', ''])[:3]
                stock[t + US + k] = c
        if all(tk in stock for tk in wanted):
            return stock
        import tempfile  # only until the stock table is cached
        with tempfile.TemporaryDirectory(prefix='hintbar-stock.') as d:
            every = read_keys(('tmux', '-S', os.path.join(d, 's'), '-f', '/dev/null')) or {}
        stock = {tk: last_field(every.get(tk, '')) for tk in set(wanted) | set(stock)}
        self.script('set-option', '-g', '@hintbar-stock', RS.join(
            [self.now['version']] + [tk + US + c for tk, c in sorted(stock.items())]))
        return stock

    def bind_lines(self):
        """Queue the bind lines and the restores for removed ones; returns
        what it bound and what each key did before."""
        self.track = bool(self.binds) or int(self.opt('@hintbar-user-binds-n', '0')) > 0
        self.ub_try, self.ub_prev, self.ub_ours, self.want = [], {}, {}, set()
        if not self.track:
            return
        kb = self.state.keys or {}
        for rec in self.now['@hintbar-user-binds'].split(RS):
            rec = rec.rstrip('\n')
            if rec:
                t, k, ours, pr, pn, pc = (rec.split(US, 5) + [''] * 6)[:6]
                self.ub_ours[t + US + k] = ours
                self.ub_prev[t + US + k] = pr + US + pn + US + pc
        # Soft (vi) bind lines take a key only if it's free, stock or still
        # ours; yours always do, and win over the soft ones.
        soft = [b.table + US + b.key for b in self.binds if b.soft]
        stock = self.stock_keys(soft) if soft else {}
        hard = {b.table + US + b.key for b in self.binds if not b.soft}
        for b in self.binds:
            tk = b.table + US + b.key
            if b.soft:
                if tk in hard:
                    continue
                cur = last_field(kb.get(tk, ''))
                if cur and cur != stock.get(tk, '') and cur != self.ub_ours.get(tk, ''):
                    continue
            self.want.add(tk)
            self.ub_try.append(b)
            # first time hintbar binds this key: remember what it did
            self.ub_prev.setdefault(tk, kb.get(tk, ''))
        for b in self.ub_try:
            self.script(*self.bind_args(b))
        self.restore_removed(self.want)

    def restore_removed(self, want):
        """Restore the old binding of every recorded key not in WANT, if the
        key is still what hintbar bound."""
        kb = self.state.keys
        for tk, ours in list(self.ub_ours.items()):
            if tk in want:
                continue
            t, k = tk.split(US, 1)
            if kb is not None and last_field(kb.get(tk, '')) == ours:
                pr, pn, pc = (self.ub_prev[tk].split(US, 2) + ['', ''])[:3]
                if pc:
                    self.put_back(t, k, pr, pn, pc)
                else:
                    self.script('unbind-key', '-T', t, k)
            self.ub_prev.pop(tk, None)
            del self.ub_ours[tk]

    # ── preview ───────────────────────────────────────────────────────
    def preview(self):
        """NORMAL shows the prefix, a preview of the prefix row (like
        zellij's base line), then its own hints. With the preview off:
        <Ctrl-b> PREFIX."""
        tables, s = self.tables, self.shapes
        root = tables.get('root') or tables.setdefault('root', Table('root'))
        own = root.hints
        prefix = tables.get('prefix')
        lead_w = cols(self.pfx_shown)
        if self.opt('@hintbar-preview', 'on') == 'on' and prefix and prefix.hints:
            # @hintbar-preview-first leads as its own group, then the prefix row
            first, placed = [], set()
            for fk in self.opt('@hintbar-preview-first', 'd ? : r').split():
                i = next((i for i, h in enumerate(prefix.hints)
                          if h.key == fk and i not in placed), None)
                if i is not None:
                    first.append(i)
                    placed.add(i)
            order = first + [i for i in range(len(prefix.hints)) if i not in placed]
            bump = 1 if first else 0
            pv = [prefix.hints[i]._replace(group=0 if j < len(first) else
                                           prefix.hints[i].group + bump)
                  for j, i in enumerate(order)]
            top = max([0] + [h.group for h in pv])
            # reuse the prefix row's hint options rather than copying them
            root.refs = (['@hintbar-hint-root-0'] +
                         ['@hintbar-hint-prefix-%d' % i for i in order] +
                         ['@hintbar-hint-root-%d' % (i + 1 + len(order)) for i in range(len(own))])
            lead = s.lead('{prefix} +')
            root.hints = ([Hint(lead, '', lead_w + 4, 0)] + pv +  # " Ctrl-b + "
                          [h._replace(group=h.group + top + 1) for h in own])
        else:
            root.hints = [Hint(s.item('{prefix}', 'PREFIX'),
                               '', lead_w + cols('PREFIX') + 5 + s.glyphs, 0)] + own

    # ── fit ───────────────────────────────────────────────────────────
    def fit(self):
        """Write each table's hint options and its layout searches, unless
        the parsed hints and line settings match the last run's."""
        q, s, tables = self.script, self.shapes, self.tables
        div_w, div = 0, ''
        if self.opt('@hintbar-dividers', 'off') == 'on':
            div_w, div = 2, '#[fg=%s%sbg=%s] │' % (self.pal['muted'], C, s.bar_bg)
        h = hashlib.sha1(repr((self.nlines, self.shown, self.lines, self.row, div_w, s.text_fg,
                               self.pal['muted'], s.bar_bg,
                               [(t.name, t.hints, t.badge, t.badge_bg) for t in tables.values()])
                              ).encode('utf-8', 'surrogateescape'))
        for f in sorted(os.listdir(PKG)):
            if f.endswith('.py'):
                with open(os.path.join(PKG, f), 'rb') as fh:
                    h.update(fh.read())
        self.fp = h.hexdigest()
        self.cached = self.fp == self.now['@hintbar-fingerprint'] and all(
            '@hintbar-row-' in self.now['status-format[%d]' % (self.row + l)]
            for l in range(self.shown))
        self.rows = {}  # (table, line) -> the row option's format
        self.need_at = {}  # table -> "width:lines " at each change, for auto
        self.made = []  # every hint and layout option this run sets
        if self.cached:
            return
        more = '#[fg=%s%sbg=%s] …' % (s.text_fg, C, s.bar_bg)
        dump = os.environ.get('HINTBAR_DUMP')
        dump = open(dump, 'a', encoding='utf-8') if dump else None

        def spill(opt, value):
            q('set-option', '-g', opt, value)
            self.made.append(opt)

        for t in tables.values():
            if not t.hints:
                continue
            refs = [t.refs[i] if i < len(t.refs) else '@hintbar-hint-%s-%d' % (t.name, i)
                    for i in range(len(t.hints))]
            for o, hint in zip(refs, t.hints):
                # a shared option is written with its own table
                if o.startswith('@hintbar-hint-%s-' % t.name):
                    spill(o, hint.fmt)
            per_line, need_at = layout.Fit([h.width for h in t.hints],
                                           [h.group for h in t.hints], self.nlines,
                                           div_w).layouts(2)
            self.need_at[t.name] = need_at
            for l, lays in enumerate(per_line):
                fmts = [layout.layout_format(line, kind, l == self.nlines - 1, refs, div, more)
                        for _, kind, line in lays]
                if dump:
                    for (w, _, _), f in zip(lays, fmts):
                        dump.write('%s\t%d\t%d\t%s\n' % (t.name, l, w, f))
                row_opt = '@hintbar-row-%s-%d' % (t.name, l)
                spill(row_opt, layout.search('@hintbar-tree-%s-%d' % (t.name, l),
                                             [w for w, _, _ in lays], fmts,
                                             lambda x: len(x.encode('utf-8', 'surrogateescape')),
                                             spill))
                self.rows[t.name, l] = '#{E:%s}' % row_opt
            if dump:
                dump.write('NEED\t%s\t%s\n' % (t.name, need_at))
        if dump:
            dump.close()

    # ── assemble ──────────────────────────────────────────────────────
    def assemble(self, l):
        """Status format for hint line L, and the mode badge. Root nests
        inside copy mode inside named key tables, so the prefix row wins
        even in copy mode."""
        s, rows = self.shapes, self.rows
        tables = self.tables

        def row(t):
            return rows.get((t, l), '')

        root = tables['root']
        row_fmt, badge = row('root'), s.badge(root.badge, root.badge_bg)
        # pick the copy table live, so mode-keys can be set anywhere
        copy_row = copy_badge = ''
        for name in ('copy-mode', 'copy-mode-vi'):
            if name not in tables:
                continue
            t = tables[name]
            r, b = row(name), s.badge(t.badge, t.badge_bg)
            if not copy_row + copy_badge:
                copy_row, copy_badge = r, b
            else:
                copy_row = '#{?#{==:#{mode-keys},vi},%s,%s}' % (r, copy_row)
                copy_badge = '#{?#{==:#{mode-keys},vi},%s,%s}' % (b, copy_badge)
        # view mode (command output shown in a pane) uses the copy-mode keys
        if copy_row + copy_badge:
            cond = '#{||:#{==:#{pane_mode},copy-mode},#{==:#{pane_mode},view-mode}}'
            row_fmt = '#{?%s,%s,%s}' % (cond, copy_row, row_fmt)
            badge = '#{?%s,%s,%s}' % (cond, copy_badge, badge)
        # the other pane modes have no key tables, so they match on pane_mode
        for t in tables.values():
            if t.name in PANE_MODES:
                cond = '#{==:#{pane_mode},%s}' % t.name
                row_fmt = '#{?%s,%s,%s}' % (cond, row(t.name), row_fmt)
                badge = '#{?%s,%s,%s}' % (cond, s.badge(t.badge, t.badge_bg), badge)
        for t in tables.values():
            if t.name == 'root' or t.name.startswith('copy-mode') or t.name in PANE_MODES:
                continue
            cond = '#{==:#{client_key_table},%s}' % t.name
            row_fmt = '#{?%s,%s,%s}' % (cond, row(t.name), row_fmt)
            badge = '#{?%s,%s,%s}' % (cond, s.badge(t.badge, t.badge_bg), badge)
        return row_fmt, badge

    # ── status lines ──────────────────────────────────────────────────
    def status(self):
        """Size the status lines (and the resize hooks for auto) while
        status is stock or what hintbar set; the user's own value sticks.
        tmux has no hook for key-table changes, so auto fits the widest
        table."""
        q, now = self.script, self.now
        own = now['status'] == 'on' or (now['@hintbar-was-status'] and
                                        now['status'] == now['@hintbar-was-status'])
        # status takes on, off or 2-5; tmux rejects 1
        total = '#{e|+:%d,#{E:@hintbar-lines-need}}' % self.row
        value = '#{?#{e|>:%s,1},%s,on}' % (total, total)
        auto = self.lines == 'auto' and self.shown
        if not own or not auto:
            for h in RESIZE_HOOKS:
                q('set-hook', '-gu', h + '[7213]')
        if not own:
            return
        if not auto:
            q('set-option', '-gu', '@hintbar-lines-need')
            n = self.row + self.shown
            q('set-option', '-g', 'status', str(n) if n > 1 else 'on')
            q('set-option', '-g', '@hintbar-was-status', str(n) if n > 1 else 'on')
            return
        # Pane modes have a hook, so each sizes for its own row; key tables
        # don't, so NORMAL sizes for the widest of them. Cached runs keep
        # the existing format.
        if not self.cached:
            names = list(self.tables)
            need = self.need_at
            fmt = layout.need_format([t for t in names if not t.startswith('copy-mode')
                                      and t not in PANE_MODES], need)
            for t in names:
                if t in PANE_MODES:
                    fmt = '#{?#{==:#{pane_mode},%s},%s,%s}' % (t, layout.need_format([t], need),
                                                               fmt)
            copies = [t for t in names if t.startswith('copy-mode')]
            if copies:
                fmt = ('#{?#{||:#{==:#{pane_mode},copy-mode},#{==:#{pane_mode},view-mode}},'
                       '%s,%s}' % (layout.need_format(copies, need), fmt))
            q('set-option', '-g', '@hintbar-lines-need', fmt)
        resize = ("if-shell -F '#{==:#{status},#{@hintbar-was-status}}' \"set-option -gF status "
                  "'%s' ; set-option -gF @hintbar-was-status '#{status}'\"" % value)
        for h in RESIZE_HOOKS:
            q('set-hook', '-g', h + '[7213]', resize)
        # and now, for the current client
        q('set-option', '-gF', 'status', value)
        q('set-option', '-gF', '@hintbar-was-status', '#{status}')

    # ── what hintbar changes ──────────────────────────────────────────
    def ours(self, name, stock):
        """The option still holds tmux's STOCK value or exactly what hintbar
        last set (claim); otherwise the user or a theme owns it."""
        last = self.now.get('@hintbar-was-' + name, '')
        return self.now[name] == stock or bool(last and self.now[name] == last)

    def claim(self, name, value):
        self.script('set-option', '-g', name, value)
        self.script('set-option', '-g', '@hintbar-was-' + name, value)

    def mouse_and_clipboard(self):
        q, now = self.script, self.now
        # tmux ships mouse bindings (drag and 2x/3x-click to copy,
        # middle-click to paste) but leaves the mouse off.
        if self.opt('@hintbar-mouse', 'on') == 'on':
            q('set-option', '-g', 'mouse', 'on')
        # Desktop clipboard via scripts/clip.sh (which picks the tool per
        # run, as the display can change under a long-lived server). Copies
        # go to the clipboard and the primary selection; middle-click pastes
        # the primary selection, else tmux's buffer. OSC 52 still covers
        # terminals over SSH. Only while stock or ours, and where a
        # clipboard tool exists.
        clip = "'%s/scripts/clip.sh'" % DIR.replace('#', '##')
        paste_cmd = ("select-pane -t = ; if-shell -F '#{||:#{pane_in_mode},#{mouse_any_flag}}' "
                     "{ send-keys -M } { run-shell -b \"%s paste #{pane_id}\" }" % clip)
        cur = self.bound.get('root MouseDown2Pane', '')
        was = now['@hintbar-was-bind-paste']
        stock = 'select-pane -t = \\; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" '
        if self.opt('@hintbar-clipboard', 'on') == 'on' and self.clip_tool:
            if self.ours('copy-command', ''):
                self.claim('copy-command', clip + ' copy')
            # stock differs by version (3.2 quotes, later braces), so match
            # its shape and keep what was there to put back
            if not cur or cur == was or (cur.startswith(stock) and 'paste' in cur[len(stock):]):
                if cur != was:
                    q('set-option', '-g', '@hintbar-was-paste-old', cur)
                q('bind-key', '-T', 'root', 'MouseDown2Pane', paste_cmd)
                self.claimed.append(('paste', 'root MouseDown2Pane'))
        else:
            if now['@hintbar-was-copy-command'] and self.ours('copy-command', ''):
                q('set-option', '-s', 'copy-command', '')
                q('set-option', '-gu', '@hintbar-was-copy-command')
            if was and cur == was:
                if now['@hintbar-was-paste-old']:
                    self.put_back('root', 'MouseDown2Pane', '0', '', now['@hintbar-was-paste-old'])
                else:
                    q('unbind-key', '-T', 'root', 'MouseDown2Pane')
                q('set-option', '-gu', '@hintbar-was-bind-paste')
                q('set-option', '-gu', '@hintbar-was-paste-old')

    def mode_keys(self):
        # Extra vi keys bring mode-keys vi (their v and y are copy-mode-vi
        # keys), while mode-keys is tmux's default or ours. tmux defaults to
        # vi if $VISUAL, or else $EDITOR, contains vi; a set but empty
        # $VISUAL counts.
        env = os.environ
        editor = env['VISUAL'] if 'VISUAL' in env else env.get('EDITOR', '')
        stock = 'vi' if 'vi' in editor.rsplit('/', 1)[-1] else 'emacs'
        if self.extra_vi == 'on':
            if self.ours('mode-keys', stock):
                self.claim('mode-keys', 'vi')
        elif self.now['@hintbar-was-mode-keys']:
            if self.now['mode-keys'] == self.now['@hintbar-was-mode-keys']:
                self.script('set-option', '-g', 'mode-keys', stock)
            self.script('set-option', '-gu', '@hintbar-was-mode-keys')

    def status_style(self):
        if self.opt('@hintbar-status-style', 'on') != 'on':
            return
        q, pal, s, now = self.script, self.pal, self.shapes, self.now
        win_bg, ours, claim = self.win_bg, self.ours, self.claim
        # a solid bar, so the shapes stand out
        if ours('status-style', 'bg=green,fg=black'):
            claim('status-style', 'bg=%s,fg=%s' % (pal['bar'], pal['text']))
        # messages, prompt and selection: stock's yellow and black, in the
        # palette
        if ours('message-style', 'bg=yellow,fg=black,fill=yellow'):
            claim('message-style', 'bg=%s,fg=%s,fill=%s' % (pal['prefix'], pal['label'],
                                                            pal['prefix']))
        if ours('message-command-style', 'bg=black,fg=yellow,fill=black'):
            claim('message-command-style', 'bg=%s,fg=%s,fill=%s' % (pal['label'], pal['prefix'],
                                                                    pal['label']))
        if ours('mode-style', 'noattr,bg=yellow,fg=black'):
            claim('mode-style', 'noattr,bg=%s,fg=%s' % (pal['prefix'], pal['label']))
        # pane borders: stock's state colours (copy, synchronised), in the
        # palette
        if ours('pane-border-style', 'default'):
            claim('pane-border-style', 'fg=' + pal['border'])
        if ours('pane-active-border-style',
                '#{?pane_in_mode,fg=yellow,#{?synchronize-panes,fg=red,fg=green}}'):
            claim('pane-active-border-style',
                  '#{?pane_in_mode,fg=%s,#{?synchronize-panes,fg=%s,fg=%s}},bold'
                  % (pal['copy'], pal['alert'], win_bg))
        # window list: the current window as a shape; both must be ours
        stock_win = '#I:#W#{?window_flags,#{window_flags}, }'
        if ours('window-status-format', stock_win) and \
                ours('window-status-current-format', stock_win):
            q('set-option', '-g', 'window-status-separator', '')
            claim('window-status-format', '#[fg=%s,bg=%s] %s ' % (s.text_fg, s.bar_bg, stock_win))
            claim('window-status-current-format',
                  '%s#[fg=%s,bg=%s,bold] %s #[fg=%s,bg=%s,nobold]%s'
                  % (s.in_edge(win_bg), s.block_fg, win_bg, stock_win, win_bg, s.bar_bg, s.leave))
        # badge and session; stock status-left-length (10) is too short
        if ours('status-left', '[#{session_name}] '):
            claim('status-left', '#{E:@hintbar-badge}#[fg=%s,bg=%s] #S ' % (s.text_fg, s.bar_bg))
            if now['status-left-length'] == '10':
                q('set-option', '-g', 'status-left-length', '40')
        # Right side: markers for states that change how tmux behaves, then
        # the clock. Stock's window-offset indicator is kept.
        stock_right = ('#{?window_bigger,[#{window_offset_x}#,#{window_offset_y}] ,}'
                       '"#{=21:pane_title}" %H:%M %d-%b-%y')
        if ours('status-right', stock_right):
            claim('status-right', '#[fg=%s,bg=%s]#{?window_bigger,[#{window_offset_x}#,'
                  '#{window_offset_y}] ,}%s%s' % (s.text_fg, s.bar_bg, self.markers(),
                                                  self.clock()))
            # stock status-right-length (40) would cut the markers off
            if now['status-right-length'] == '40':
                q('set-option', '-g', 'status-right-length', '80')

    def clock(self):
        """Clock parts (split at 2+ spaces) as segments, the last in the
        window colour, flush right. With icons, parts with hours or minutes
        get a clock, the rest a calendar."""
        s = self.shapes
        parts = re.split(' {2,}', self.opt('@hintbar-clock', '%a %b %d  %H:%M'))
        out, behind = '', ''
        for i, seg in enumerate(parts):
            if not seg:
                continue
            bg = self.win_bg if i == len(parts) - 1 else s.block_bg
            out += s.in_edge(bg, behind)
            if self.icons == 'on':
                seg = (ICON_TIME if re.search('%[HIklMRTr]', seg) else ICON_DATE) + ' ' + seg
            out += '#[fg=%s%sbg=%s%sbold] %s #[nobold]' % (s.block_fg, C, bg, C, seg)
            behind = bg
        return out

    def markers(self):
        """Markers for states that change how tmux behaves: mouse, then
        zoomed, synchronised and marked panes."""
        if self.opt('@hintbar-markers', 'on') != 'on':
            return ''
        s, pal = self.shapes, self.pal
        mouse = ICON_MOUSE + ' mouse' if self.icons == 'on' else 'mouse'
        out = '#{?mouse,#[fg=%s%sbg=%s] %s ,}' % (pal['muted'], C, s.bar_bg, mouse)
        for flag, text, colour in (('window_zoomed_flag', 'ZOOM', pal['prefix']),
                                   ('synchronize-panes', 'SYNC', pal['alert']),
                                   ('pane_marked_set', 'MARKED', pal['other'])):
            out += '#{?%s,%s#[bg=%s] ,}' % (flag, s.block(text, colour), s.bar_bg)
        return out

    def panes(self):
        """Pane titles, heavy lines and active-pane arrows, each gated on
        the tmux version that added it."""
        if self.opt('@hintbar-panes', 'on') != 'on':
            return
        now, s, ours, claim = self.now, self.shapes, self.ours, self.claim
        if ours('pane-border-status', 'off'):
            claim('pane-border-status', 'top')
        if self.v >= 302 and ours('pane-border-lines', 'single'):
            claim('pane-border-lines', 'heavy')
        if self.v >= 303 and ours('pane-border-indicators', 'colour'):
            claim('pane-border-indicators', 'both')
        # stock's title varies by version (3.7 adds buttons): match its start
        # and keep the rest
        stock = '#{?pane_active,#[reverse],}#{pane_index}#[default] "#{pane_title}"'
        cur, was = now['pane-border-format'], now['@hintbar-was-pane-border-format']
        if cur.startswith(stock) or (was and cur == was):
            tail = cur[len(stock):] if cur.startswith(stock) else now['@hintbar-was-pane-border-tail']
            title = ' #P #{pane_current_command} '
            claim('pane-border-format',
                  '#{?pane_active,#[fg=%s#,bg=default]%s#[fg=%s#,bg=%s#,bold]%s'
                  '#[fg=%s#,bg=default#,nobold]%s,#[fg=%s]%s}#[default]%s'
                  % (self.win_bg, s.enter, s.block_fg, self.win_bg, title, self.win_bg, s.leave,
                     self.pal['muted'], title, tail))
            self.script('set-option', '-g', '@hintbar-was-pane-border-tail', tail)

    def install_rows(self):
        """The hint lines and badge, and unsetting what an earlier run made
        and this one didn't."""
        if self.cached:
            return
        q, s = self.script, self.shapes
        badge = ''
        rows = []
        for l in range(self.nlines):
            r, badge = self.assemble(l)
            rows.append(r)
        for l in range(self.shown):
            name = 'status-format[%d]' % (self.row + l)
            q('set-option', '-g', name, '#[align=left,fill=%s]%s' % (s.bar_bg, rows[l]))
            self.made.append(name)
        q('set-option', '-g', '@hintbar-badge', badge)
        made = set(self.made)
        for line in self.state.options:
            o = line.split(' ', 1)[0]
            if re.match(r'^@hintbar-(hint|layout|tree|row)-', o) and o not in made:
                q('set-option', '-gu', o)
        for o in self.now['@hintbar-made'].split():
            if o.startswith('status-format') and o not in made:
                q('set-option', '-gu', o)
        q('set-option', '-g', '@hintbar-made',
          ' '.join(o for o in self.made if o.startswith('status-format')))
        q('set-option', '-g', '@hintbar-fingerprint', self.fp)

    def live_hook(self):
        # Re-render when a @hintbar option or the prefix changes, so set -g
        # applies at once. hintbar's own writes don't match the pattern, so
        # it can't loop. Slot 7213 avoids the user's own after-set-option hooks.
        self.script('set-hook', '-g', 'after-set-option[7213]',
                    "if-shell -F '#{m/r:^(@hintbar-(%s)|prefix)$,#{hook_argument_0}}' "
                    "{ run-shell '%s' }" % (LIVE, self.self_cmd))

    # ── apply and record ──────────────────────────────────────────────
    def apply(self):
        """Send the script. If tmux reported errors, find the bind lines it
        refused (each is retried alone), warn, and put back what a refused
        key had before."""
        rc, err = self.script.apply()
        self.ub_done = list(self.ub_try)
        if rc == 0:
            return
        refused = []
        for b in self.ub_try:
            rc1, out, err1 = tmux(*self.bind_args(b))
            if rc1:
                refused.append(b)
                self.warn("%s: tmux couldn't bind %s: %s"
                          % (b.where, b.key, (out + err1).rstrip('\n') or 'it refused the line'))
        if not refused:
            self.warn('tmux: ' + err)
            return
        self.ub_done = [b for b in self.ub_try if b not in refused]
        # an earlier run's binding of a refused key is undone like a removed line
        self.restore_removed(self.want - {b.table + US + b.key for b in refused})
        self.script.apply()

    def record(self, files, env):
        """Record what the bind lines and hintbar itself bound, as tmux
        prints it, then the input hash and warnings for the next run."""
        after = State(NAMES)
        q = self.script
        if self.track:
            recs = []
            if after.keys is not None:
                for b in self.ub_done:
                    tk = b.table + US + b.key
                    # a previously free key: three empty fields
                    recs.append(tk + US + last_field(after.keys.get(tk, '')) + US +
                                (self.ub_prev.get(tk) or US + US) + RS)
            q('set-option', '-g', '@hintbar-user-binds', ''.join(recs))
            q('set-option', '-g', '@hintbar-user-binds-n', str(len(recs)))
        for name, tk in self.claimed:
            q('set-option', '-g', '@hintbar-was-bind-' + name, after.bound.get(tk, ''))
        q('set-option', '-g', '@hintbar-warnings', RS.join(self.warnings))
        q('set-option', '-g', '@hintbar-inputs', input_hash(after, files, env))
        self.script.apply()


def main():
    r = Run()
    try:
        if not r.read():
            return
        files, env = r.inputs()
        # nothing hintbar reads has changed since it last ran: nothing to do
        # but repeat its warnings
        if r.now['@hintbar-inputs'] == input_hash(r.state, files, env):
            if r.now['@hintbar-warnings']:
                show('; '.join(r.now['@hintbar-warnings'].split(RS)))
            return
        r.settings()
        r.own_bindings()
        r.parse()
        r.bind_lines()
        r.preview()
        r.fit()
        r.status()
        r.mouse_and_clipboard()
        r.mode_keys()
        r.status_style()
        r.panes()
        r.install_rows()
        r.live_hook()
        r.apply()
        r.record(files, env)
    except Stop as e:
        print('hintbar: ' + str(e), file=sys.stderr)
        show(str(e))
        sys.exit(1 if os.environ.get('HINTBAR_STRICT') else 0)
    if r.warnings:
        show('; '.join(r.warnings))
