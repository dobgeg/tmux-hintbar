"""Colours, themes and the shapes hints are drawn in."""
import os
import re

C = '#,'  # a comma inside #{?...} branches

PALETTE = dict(bar='colour0', text='colour15', block='colour15', label='colour0', key='',
               prefix='colour3', copy='colour4', window='colour2', other='colour5',
               border='colour8', muted='colour7', alert='colour1')

COLOUR_RE = re.compile(
    r'^(#[0-9a-f]{6}|colou?r([0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])'
    r'|default|terminal|(bright)?(black|red|green|yellow|blue|magenta|cyan|white))$')

# edge glyphs per style: enter, leave, and the arrow's swapped-colour enter
GLYPHS = {
    'plain': ('', '', ''),
    'pill': ('', '', ''),
    'slant': ('', '', ''),
    # U+E0B0 in swapped colours where the bar colour is known, so renderers
    # that draw Powerline triangles draw both sides alike; U+E0D7 otherwise
    'arrow': ('', '', ''),
}


class ThemeError(Exception):
    """A theme that can't be used; the message says why."""


def is_colour(v):
    """tmux accepts V as a colour. Callers lowercase hex: in a format, #D
    and #F expand to the pane id and window flags."""
    return bool(COLOUR_RE.match(v.lower()))


def theme_dirs(xdg, plugin_dir):
    return [os.path.join(xdg, 'tmux-hintbar', 'themes'), os.path.join(plugin_dir, 'themes')]


def find_theme(name, dirs):
    """Path of theme NAME's file, the user's first; None if there's none."""
    for d in dirs:
        p = os.path.join(d, name + '.conf')
        if os.access(p, os.R_OK):
            return p
    return None


def theme_names(dirs):
    return [f[:-5] for d in dirs if os.path.isdir(d)
            for f in sorted(os.listdir(d)) if f.endswith('.conf')]


def load_theme(path):
    """Colours from a theme file: one 'name colour' per line. Raises
    ThemeError for an unknown name or a colour tmux would reject (one bad
    colour would stop every write)."""
    got = {}
    with open(path, encoding='utf-8', errors='surrogateescape') as f:
        for line in f:
            words = line.split()
            if not words or words[0].startswith('#'):
                continue
            k, v = words[0], (words[1] if len(words) > 1 else '')
            if k not in PALETTE:
                raise ThemeError("%s: unknown colour name '%s', using terminal colours"
                                 % (path, k))
            if not is_colour(v):
                raise ThemeError("%s: '%s' for %s isn't a colour tmux knows, using terminal "
                                 "colours" % (path, v, k))
            got[k] = v.lower()
    return got


def esc(s):
    """Text for inside #{?...} branches, where a bare , or } would end the
    branch and # starts a format."""
    return s.replace('#', '##').replace(',', '#,').replace('}', '#}')


def cols(s):
    """Display width, one column per character (undercounts wide CJK and
    emoji)."""
    return len(s)


def live(s):
    """Swap {prefix} and {mouse} (escaped) for formats tmux keeps current,
    so changing either needs no re-render."""
    return (s.replace('{prefix#}', '#{s/M-/Alt-/:#{s/C-/Ctrl-/:prefix}}')
            .replace('{mouse#}', '#{?mouse,ON,OFF}'))


class Shapes:
    """Builds the formats for hints, badges and blocks in one style and set
    of colours."""

    def __init__(self, style, enter, leave, enter_swap, bar_bg, bar_fill, text_fg, key_fg,
                 block_bg, block_fg):
        self.style, self.enter, self.leave, self.enter_swap = style, enter, leave, enter_swap
        self.bar_bg, self.bar_fill, self.text_fg = bar_bg, bar_fill, text_fg
        self.key_fg, self.block_bg, self.block_fg = key_fg, block_bg, block_fg
        # Edgeless styles get a bar-coloured gap to keep shapes apart, and
        # pills too: their rounded edges look merged without one.
        self.gap = ' ' if not enter + leave or style == 'pill' else ''
        # columns each hint spends on edges and gap
        self.glyphs = cols(enter + leave) + len(self.gap)

    def in_edge(self, shape, behind=''):
        """Left edge of a SHAPE-coloured shape on the bar, or on a
        BEHIND-coloured shape."""
        fill = behind or self.bar_fill
        if self.enter_swap and fill:
            return '#[fg=%s%sbg=%s]%s' % (fill, C, shape, self.enter_swap)
        return '#[fg=%s%sbg=%s]%s' % (shape, C, behind or self.bar_bg, self.enter)

    def badge(self, text, bg):
        """A mode badge: no left edge, flush with the terminal's side."""
        return '#[fg=%s%sbg=%s%sbold] %s #[fg=%s%sbg=%s%snobold]%s' % (
            self.block_fg, C, bg, C, esc(text), bg, C, self.bar_bg, C, self.leave)

    def block(self, text, bg):
        """A BG-coloured shape holding bold TEXT."""
        return self.in_edge(bg) + self.badge(text, bg)

    def item(self, key, label):
        """One hint shape, " <KEY> LABEL "."""
        return live(
            '#[bg=%s]%s%s#[fg=%s%sbg=%s%snobold] <#[fg=%s%sbold]%s#[fg=%s%snobold]> '
            '#[bold]%s #[fg=%s%sbg=%s%snobold]%s' % (
                self.bar_bg, self.gap, self.in_edge(self.block_bg), self.block_fg, C,
                self.block_bg, C, self.key_fg, C, esc(key), self.block_fg, C, esc(label),
                self.block_bg, C, self.bar_bg, C, self.leave))

    def lead(self, text):
        """Bold text on the bar, leading into the hints."""
        return live('#[fg=%s%sbg=%s%sbold] %s #[nobold]'
                    % (self.text_fg, C, self.bar_bg, C, esc(text)))

    def width(self, key, label, prefix_shown):
        """Columns a hint takes, measuring {prefix} as it is now (a prefix
        change re-renders) and {mouse} at its widest, OFF."""
        k = key.replace('{prefix}', prefix_shown).replace('{mouse}', 'OFF')
        lb = label.replace('{prefix}', prefix_shown).replace('{mouse}', 'OFF')
        return cols(k) + cols(lb) + 5 + self.glyphs
