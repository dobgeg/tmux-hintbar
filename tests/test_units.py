"""Unit tests for the parts of hintbar that don't need tmux.

Run with: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

from hintbar import layout  # noqa: E402
from hintbar.keys import ConfigError, Parser, key_parts, pretty_key  # noqa: E402
from hintbar.style import PALETTE, Shapes, esc, is_colour  # noqa: E402
from hintbar.tmux import parse_bound, quote, unescape_key  # noqa: E402

PLUGIN = '/plugin'


def parser(warnings=None):
    shapes = Shapes('plain', '', '', '', 'default', '', 'colour15', 'colour1', 'colour15',
                    'colour0')
    return Parser(shapes, dict(PALETTE), is_colour, PLUGIN, 'Ctrl-b', 'C-b', 'on',
                  (warnings if warnings is not None else []).append)


def parse(*files):
    """Parse (path, text) pairs in order; returns the parser."""
    p = parser()
    for path, text in files:
        p.parse_lines(text.splitlines(True), path)
    return p


def keys_of(p, table):
    return [h.key for h in p.tables[table].hints]


class Keys(unittest.TestCase):
    def test_pretty_key(self):
        self.assertEqual(pretty_key('C-M-Left'), 'Ctrl-Alt-←')
        self.assertEqual(pretty_key('S-Up'), 'Shift-↑')
        self.assertEqual(pretty_key('NPage'), 'PgDn')
        self.assertEqual(pretty_key('x'), 'x')

    def test_key_parts(self):
        self.assertEqual(key_parts('| -'), ['|', '-'])
        self.assertEqual(key_parts('hjkl'), ['h', 'j', 'k', 'l'])
        self.assertEqual(key_parts('Alt-←↓↑→'), ['Alt-←', 'Alt-↓', 'Alt-↑', 'Alt-→'])
        self.assertEqual(key_parts('Ctrl-o'), ['Ctrl-o'])

    def test_sections_and_groups(self):
        p = parse(('/k', '[prefix] TMUX\nc  WINDOW\n---\nd  DETACH\n'))
        t = p.tables['prefix']
        self.assertEqual((t.badge, t.badge_bg), ('TMUX', 'colour3'))
        self.assertEqual([(h.key, h.group) for h in t.hints], [('c', 0), ('d', 1)])

    def test_plus_section_retires_named_keys(self):
        p = parse(('/a', '[prefix]\n% "  SPLIT\n-  DELETE\nz  ZOOM\n'),
                  ('/b', '[+prefix]\n| -  SPLIT\n'))
        # - DELETE names a key the new hint uses; % " doesn't
        self.assertEqual(keys_of(p, 'prefix'), ['| -', '% "', 'z'])

    def test_section_replaces_and_keeps_badge(self):
        p = parse(('/a', '[root] NORMAL\nx  X\n'), ('/b', '[root]\ny  Y\n'))
        self.assertEqual(keys_of(p, 'root'), ['y'])
        self.assertEqual(p.tables['root'].badge, 'NORMAL')

    def test_bind_lines(self):
        p = parse(('/k', '[prefix]\nbind -r h  - Go left  select-pane -L\n'
                         'bind |  SPLIT  split-window -h\n'))
        self.assertEqual([(b.key, b.repeat, b.label, b.soft) for b in p.binds],
                         [('h', '-r', '- Go left', False), ('|', '', 'SPLIT', False)])
        self.assertEqual(keys_of(p, 'prefix'), ['|'])  # "- note" binds without a hint

    def test_own_files_bind_softly(self):
        p = parse((PLUGIN + '/defaults-vi.conf', '[prefix]\nbind v  COPY  copy-mode\n'))
        self.assertTrue(p.binds[0].soft)

    def test_errors_name_the_line(self):
        for text, msg in (('x  X\n', 'before any [table]'),
                          ('[prefix]\nonespace X\n', 'need two spaces'),
                          ('[prefix]\nc  A\nc  B\n', "'c' is already listed"),
                          ('[tree-mode]\nbind x  X  kill\n', 'no key table'),
                          ('[prefix]\nbind x  X  { kill }\n', 'not in { }')):
            with self.assertRaises(ConfigError) as e:
                parse(('/k', text))
            self.assertIn(msg, str(e.exception))
            self.assertTrue(str(e.exception).startswith('/k:'))

    def test_bad_badge_colour_warns(self):
        w = []
        parser(w).parse_lines(['[prefix] TMUX nosuchcolour\n'], '/k')
        self.assertIn("'nosuchcolour' isn't a colour", w[0])


class Style(unittest.TestCase):
    def test_esc(self):
        self.assertEqual(esc('a,b}#c'), 'a#,b#}##c')

    def test_is_colour(self):
        for ok in ('colour255', 'color0', '#AABBCC', 'brightred', 'default'):
            self.assertTrue(is_colour(ok), ok)
        for bad in ('colour256', '#abc', 'reddish', ''):
            self.assertFalse(is_colour(bad), bad)


class Layout(unittest.TestCase):
    def test_priority_and_more(self):
        # three 10-column hints on one line: narrow terminals drop from the end
        per_line, need = layout.Fit([10, 10, 10], [0, 0, 0], 1, 0).layouts(2)
        self.assertEqual([(w, kind, line) for w, kind, line in per_line[0]],
                         [(0, 'some', ''), (12, 'some', '0 '), (22, 'some', '0 1 '),
                          (30, 'all', '0 1 2 ')])
        self.assertEqual(need, '0:1 ')

    def test_last_line_takes_what_still_fits(self):
        # a wide hint can't hide a narrow one after it
        lines = layout.Fit([5, 50, 5], [0, 0, 0], 1, 0).fill(20, 0)[1]
        self.assertEqual(lines, ['0 2 '])

    def test_wrapping_and_lines_needed(self):
        per_line, need = layout.Fit([10, 10, 10], [0, 0, 0], 2, 0).layouts(2)
        self.assertEqual(need, '0:2 30:1 ')
        self.assertEqual(per_line[0][-1], (30, 'all', '0 1 2 '))

    def test_dividers_between_groups(self):
        lines = layout.Fit([5, 5], [0, 1], 1, 2).fill(20, 0)[1]
        self.assertEqual(lines, ['0 1D '])
        fmt = layout.layout_format('0 1D ', 'all', True, ['@a', '@b'], '|', '…')
        self.assertEqual(fmt, '#{E:@a}|#{E:@b}')

    def test_search_spills_big_branches(self):
        spilled = []
        fmts = ['x' * 3000, 'y' * 3000]
        s = layout.search('@t', [0, 10], fmts, len, lambda o, v: spilled.append(o))
        self.assertEqual(s, '#{E:@t-0-1}')
        self.assertEqual(spilled, ['@t-0-1'])

    def test_need_format(self):
        self.assertEqual(layout.need_format(['a'], {}), '1')
        self.assertEqual(layout.need_format(['a', 'b'], {'a': '0:3 50:2 ', 'b': '0:2 80:1 '}),
                         '#{?#{e|<:#{client_width},50},3,2}')


class Tmux(unittest.TestCase):
    def test_quote(self):
        self.assertEqual(quote("it's"), "'it'\\''s'")
        with self.assertRaises(ValueError):
            quote('two\nlines')

    def test_list_keys_parsing(self):
        out = ('bind-key    -T prefix       \\#     list-buffers\n'
               'bind-key -r -T prefix C-Up  resize-pane -U\n')
        self.assertEqual(parse_bound(out), {'prefix #': 'list-buffers',
                                            'prefix C-Up': 'resize-pane -U'})
        self.assertEqual(unescape_key('C-\\\\'), 'C-\\')


if __name__ == '__main__':
    unittest.main()
