"""Keys files: [table] sections of hints, and bind lines."""
import re
from collections import namedtuple

# pane modes whose keys are built into tmux rather than kept in key tables
PANE_MODES = frozenset(['view-mode', 'tree-mode', 'buffer-mode', 'client-mode',
                        'options-mode', 'clock-mode'])

ARROWS = '←↓↑→'
PRETTY = {'Up': '↑', 'Down': '↓', 'Left': '←', 'Right': '→', 'BSpace': 'Backspace',
          'NPage': 'PgDn', 'PPage': 'PgUp', 'DC': 'Delete', 'IC': 'Insert'}
MOD_RE = re.compile(r'^([CMS])-(.+)$')
HEADER_RE = re.compile(r'^\[(\+?)([^]]+)\](.*)$')

# fmt: the hint's format; key: its key text as written; width: its columns;
# group: its --- group
Hint = namedtuple('Hint', 'fmt key width group')
# a bind line; soft ones (hintbar's own files) only take free or stock keys
Bind = namedtuple('Bind', 'table key repeat label command soft where')


class ConfigError(Exception):
    """A keys file line hintbar can't use; the message names it."""


class Table:
    """One key table's (or pane mode's) badge and hints, in priority order.
    refs names the option holding each hint where it isn't the table's own
    (NORMAL's preview reuses the prefix row's)."""

    def __init__(self, name):
        self.name, self.badge, self.badge_bg = name, '', ''
        self.hints, self.refs = [], []


def pretty_key(k):
    """A tmux key name as the bar spells it (C-h -> Ctrl-h, Up -> ↑)."""
    mods = ''
    m = MOD_RE.match(k)
    while m:
        mods += {'C': 'Ctrl-', 'M': 'Alt-', 'S': 'Shift-'}[m.group(1)]
        k = m.group(2)
        m = MOD_RE.match(k)
    return mods + PRETTY.get(k, k)


def key_parts(text):
    """The single keys a hint's key text names: "| -" -> | and -, "hjkl" ->
    letters, "Alt-←↓↑→" -> four Alt-arrows."""
    parts = []
    for tok in text.split():
        if any(a in tok for a in ARROWS):
            base = ''.join(c for c in tok if c not in ARROWS)
            parts += [base + a for a in ARROWS if a in tok]
        elif re.match(r'^([a-z]{2,}|[A-Z]{2,})$', tok):
            parts += list(tok)
        else:
            parts.append(tok)
    return parts


def default_badge_bg(name, pal, block_bg):
    if name == 'root':
        return block_bg
    if name == 'prefix':
        return pal['prefix']
    if name.startswith('copy-mode') or name.endswith('-mode'):
        return pal['copy']
    return pal['other']


class Parser:
    """Reads keys files into TABLES (name -> Table, in first-seen order) and
    BINDS. [table] replaces an earlier section of that name, so a keys file
    lists only what it changes; [+table] adds to it.

    shapes      a style.Shapes, to format and measure hints
    pal         the palette, for default badge colours
    is_colour   tells colours tmux accepts
    plugin_dir  files under it are hintbar's own (soft binds, reload hint)
    prefix      the prefix as displayed, for widths
    send_key    the key that sends the prefix, or ''
    reload      'on' if prefix r reloads
    warn        called with non-fatal problems
    """

    def __init__(self, shapes, pal, is_colour, plugin_dir, prefix, send_key, reload, warn):
        self.shapes, self.pal, self.is_colour = shapes, pal, is_colour
        self.own_prefix = plugin_dir.rstrip('/') + '/'
        self.prefix, self.send_key, self.reload, self.warn = prefix, send_key, reload, warn
        self.tables, self.binds = {}, []

    def hint(self, key, label, group):
        s = self.shapes
        return Hint(s.item(key, label), key, s.width(key, label, self.prefix), group)

    def parse(self, path):
        with open(path, encoding='utf-8', errors='surrogateescape') as f:
            self.parse_lines(f, path)

    def parse_lines(self, lines, path):
        table, group, old, retired, seen = None, 0, None, [], {}
        own = path.startswith(self.own_prefix)

        def end_section():
            # for a [+table]: the earlier hints follow the new ones, minus
            # any naming a key the new section uses (<|> replaces <| ->)
            if old is None:
                return
            top = (table.hints[-1].group + 1) if table.hints else 0
            in_use = {p for k in [h.key for h in table.hints] + retired for p in key_parts(k)}
            table.hints += [h._replace(group=h.group + top) for h in old
                            if not any(p in in_use for p in key_parts(h.key))]

        for n, line in enumerate(lines, 1):
            line = line.rstrip('\n').replace('\t', '  ').strip(' ')
            if not line or line.startswith('#'):
                continue
            where = '%s:%d' % (path, n)

            m = HEADER_RE.match(line)
            if m:
                end_section()
                name = m.group(2)
                b, c = (m.group(3).split() + ['', ''])[:2]
                table = self.tables.get(name) or self.tables.setdefault(name, Table(name))
                old = list(table.hints) if m.group(1) else None
                table.hints, group, retired = [], 0, []
                seen.clear()
                if c and not self.is_colour(c):
                    self.warn("%s: '%s' isn't a colour tmux knows, using the default badge "
                              "colour" % (where, c))
                    c = ''
                # a bare header keeps the badge an earlier file gave this table
                table.badge = b or table.badge or name.upper()
                table.badge_bg = (c or table.badge_bg or
                                  default_badge_bg(name, self.pal, self.shapes.block_bg)).lower()
                continue

            if table is None:
                raise ConfigError(where + ': key before any [table] header')
            # --- starts a group
            if line == '---':
                if table.hints:
                    group += 1
                continue

            # bind KEY  LABEL  COMMAND. Label - binds without a hint;
            # "- note" does too but keeps the note for prefix ?
            if line.startswith('bind '):
                if table.name in PANE_MODES:
                    raise ConfigError("%s: tmux has no key table for %s, so keys can't be "
                                      "bound there" % (where, table.name))
                rest, rep = line[5:], ''
                if rest.startswith('-r '):
                    rep, rest = '-r', rest[3:]
                key = rest.split('  ')[0]
                after = rest[len(key):].strip(' ')
                label = after.split('  ')[0]
                cmd = after[len(label):].strip(' ')
                if not key or not label or not cmd or key == rest:
                    raise ConfigError(where + ': bind needs a key, a label and a command, '
                                      'each two spaces apart')
                if cmd.startswith('{'):
                    raise ConfigError(where + ": write several commands as 'cmd1 ; cmd2', "
                                      "not in { }")
                # bound keys are checked apart from hint keys: one hint line
                # often covers several bindings
                if 'bind ' + key in seen:
                    raise ConfigError("%s: '%s' is already bound in [%s] on line %d"
                                      % (where, key, table.name, seen['bind ' + key]))
                seen['bind ' + key] = n
                self.binds.append(Bind(table.name, key, rep, label, cmd, own, where))
                if label == '-' or label.startswith('- '):
                    # still retires earlier hints for this key in a [+table]
                    retired.append(pretty_key(key))
                else:
                    table.hints.append(self.hint(pretty_key(key), label, group))
                continue

            # split at the first run of 2+ spaces, so either side may
            # contain single spaces
            key = line.split('  ')[0]
            if key == line:
                raise ConfigError(where + ': need two spaces between key and label')
            label = line[len(key):].strip(' ')
            if key.startswith('\\#'):
                key = key[1:]
            # {send-prefix} follows the real binding; no binding, no hint
            if key == '{send-prefix}':
                if not self.send_key:
                    continue
                key = pretty_key(self.send_key)
            if self.reload != 'on' and own and key == 'r' and label == 'RELOAD':
                continue
            # a duplicate key in one section is almost always a typo
            if key in seen:
                raise ConfigError("%s: '%s' is already listed in [%s] on line %d"
                                  % (where, key, table.name, seen[key]))
            seen[key] = n
            table.hints.append(self.hint(key, label, group))
        end_section()
