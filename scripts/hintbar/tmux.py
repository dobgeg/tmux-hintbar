"""Talking to tmux: one combined read, and writes sent as one script.

Each tmux client process costs a few milliseconds, which is most of what a
render spends, so reads are chained into a single call and writes go
through `source-file -`. tmux reads that script itself, so the ~16KB limit
on a single command line doesn't apply, and a failing command doesn't stop
the ones after it.
"""
import re
import subprocess

US, RS = '\x1f', '\x1e'
MARK = US + 'hintbar' + US  # printed between the sections of the read

KEY_FORMAT = US.join(['#{key_table}', '#{key_string}', '#{key_repeat}', '#{key_note}',
                      '#{key_command}'])
BIND_RE = re.compile(r'^bind-key +(-r +)?-T +(\S+) +(\S+) +(.*)$')


# Text crosses to and from tmux as UTF-8 whatever the locale (run-shell
# often has none), with stray bytes carried through unchanged.
def enc(s):
    return s.encode('utf-8', 'surrogateescape')


def dec(b):
    return b.decode('utf-8', 'surrogateescape')


def run(argv, stdin=None):
    """Run ARGV, feeding it STDIN (text); returns (exit code, stdout, stderr)."""
    p = subprocess.run([enc(a) for a in argv], input=None if stdin is None else enc(stdin),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.returncode, dec(p.stdout), dec(p.stderr)


def tmux(*args):
    return run(['tmux'] + list(args))


def show(msg):
    """Show MSG until a key press; with no client attached, nowhere."""
    if tmux('display-message', '-d', '0', 'hintbar: ' + msg)[0]:
        tmux('display-message', 'hintbar: ' + msg)


def quote(s):
    """S as one word of a tmux config file. Single quotes keep everything
    literal ($, ~, #{}, braces, ;); a ' closes them, is escaped, and
    reopens them."""
    if '\n' in s:
        raise ValueError('tmux config words cannot contain newlines: %r' % s)
    return "'" + s.replace("'", "'\\''") + "'"


class Script:
    """tmux commands collected in order and applied in one source-file."""

    def __init__(self):
        self.cmds = []

    def __call__(self, cmd, *args):
        self.cmds.append((cmd,) + args)

    def apply(self):
        """Run every queued command; returns (exit code, error text). tmux
        keeps going past a failing command and reports each one."""
        if not self.cmds:
            return 0, ''
        text = ''.join(' '.join((c[0],) + tuple(quote(a) for a in c[1:])) + '\n'
                       for c in self.cmds)
        self.cmds = []
        rc, out, err = run(['tmux', 'source-file', '-'], text)
        return rc, (out + err).strip('\n')


def unescape_key(k):
    """Undo list-keys' escaping (C-\\\\, \\", \\#)."""
    return re.sub(r'\\(.)', r'\1', k)


def parse_bound(text):
    """`list-keys -T` output as "table key" -> command, runs of spaces
    squeezed (that's how earlier versions recorded them)."""
    bound = {}
    for line in text.split('\n'):
        m = BIND_RE.match(re.sub(' {2,}', ' ', line))
        if m:
            bound[m.group(2) + ' ' + unescape_key(m.group(3))] = m.group(4)
    return bound


def parse_keys(text, formatted):
    """Every binding as "table US key" -> "repeat US note US command", from
    `list-keys -F KEY_FORMAT` (FORMATTED) or plain `list-keys`, which has no
    notes."""
    keys = {}
    for line in text.split('\n'):
        if formatted:
            if line:
                t, k, r, nt, c = (line.split(US, 4) + [''] * 5)[:5]
                keys[t + US + k] = r + US + nt + US + c
            continue
        m = BIND_RE.match(line)
        if m:
            keys[m.group(2) + US + unescape_key(m.group(3))] = \
                ('1' if m.group(1) else '0') + US + US + m.group(4)
    return keys


def read_keys(tm=('tmux',)):
    """Every binding (see parse_keys); list-keys -F needs tmux 3.7, so older
    servers fall back to plain list-keys. None if tmux couldn't be asked."""
    rc, out, _ = run(list(tm) + ['list-keys', '-F', KEY_FORMAT])
    if rc == 0:
        return parse_keys(out, True)
    rc, out, _ = run(list(tm) + ['list-keys'])
    return None if rc else parse_keys(out, False)


def last_field(rec):
    """The command of a "repeat US note US command" record."""
    return rec.rsplit(US, 1)[-1]


class State:
    """Everything hintbar reads from tmux, from one call where possible.

    now      option -> value, for NAMES (hintbar's own read with -g)
    options  `show-options -g` lines (hint and layout options are found here)
    session  hintbar options set without -g
    bound    "table key" -> command for the prefix and root tables
    keys     every binding (see parse_keys), or None if unreadable
    """

    def __init__(self, names):
        # hintbar's own options are read with -g, one per command: some hold
        # US-separated records, which would break the US-separated list
        own = [n for n in names if n.startswith('@hintbar-')]
        tmux_names = [n for n in names if not n.startswith('@hintbar-')]
        sections = [
            ['show-options', '-g'],
            ['show-options', '-q'],
            ['list-keys', '-T', 'root', ';', 'list-keys', '-T', 'prefix'],
        ]
        args = ['display-message', '-p', ''.join('#{%s}%s' % (n, US) for n in tmux_names)]
        # a US line after each value, since unset options print nothing
        for n in own:
            args += [';', 'show-options', '-gqv', n, ';', 'display-message', '-p', US]
        for s in sections:
            args += [';', 'display-message', '-p', MARK, ';'] + s
        mark = [';', 'display-message', '-p', MARK, ';']
        formatted = True
        rc, out, _ = tmux(*(args + mark + ['list-keys', '-F', KEY_FORMAT]))
        if MARK not in out:
            # tmux before 3.7 rejects -F while parsing the whole list, so
            # nothing ran: again, with plain list-keys (no notes)
            formatted = False
            rc, out, _ = tmux(*(args + mark + ['list-keys']))
        parts = out.split(MARK + '\n')
        # a section that didn't run (an earlier one failed) is read alone
        got = parts[1:] if rc == 0 else parts[1:-1]
        texts = [got[i] if i < len(got) else tmux(*s)[1] for i, s in enumerate(sections)]
        self.keys = parse_keys(got[3], formatted) if rc == 0 and len(got) == 4 else read_keys()

        lines = parts[0].split('\n')
        vals = lines[0].split(US)
        self.now = dict.fromkeys(own, '')
        self.now.update((n, vals[i] if i < len(vals) else '') for i, n in enumerate(tmux_names))
        i, val = 0, ''
        for line in lines[1:]:
            if line == US:
                if i < len(own):
                    self.now[own[i]] = val
                i, val = i + 1, ''
            else:
                val += line
        self.options = texts[0].split('\n')
        self.session = [l.split()[0] for l in texts[1].split('\n') if l.startswith('@hintbar-')]
        self.bound = parse_bound(texts[2])
