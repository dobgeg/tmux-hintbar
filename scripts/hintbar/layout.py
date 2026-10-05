"""Every distinct layout of a table's hints by client width.

Hints flow across lines in priority order; the last line takes any that
still fit, so one long hint can't hide shorter ones after it. Which fit
depends on those before, so every layout is computed here and tmux picks
one with a binary search on #{client_width}: tmux draws nothing for a
format that takes over 100ms, so the per-redraw work must stay small.
"""

NEVER = 999999  # a width no terminal reaches
BRANCH_MAX = 4000  # bytes; tmux refuses a single command over ~16KB


class Fit:
    """Lays one table's hints over NLINES lines.

    widths, groups  per hint: columns, and --- group (a divider of DIV_W
                    columns goes between groups on one line)
    """

    def __init__(self, widths, groups, nlines, div_w):
        self.widths, self.groups, self.nlines, self.div_w = widths, groups, nlines, div_w

    def fill(self, cap, res):
        """Lay the hints over lines of CAP columns, RES kept free at the end
        of the last. Returns (last line used, the lines as hint indexes with
        a D suffix where a divider goes, whether all fit, and the smallest
        wider cap at which any choice here flips: every width below it gives
        this same layout, so the search skips straight there)."""
        final = self.nlines - 1
        lines = [''] * self.nlines
        l, used, last, every, nxt = 0, 0, -1, True, NEVER
        for i, (w, g) in enumerate(zip(self.widths, self.groups)):
            while True:
                d, cost = '', w
                if last >= 0 and g != last:
                    d, cost = 'D', cost + self.div_w
                lcap, need = cap, used + cost
                if l == final:
                    lcap, need = cap - res, need + res
                if used + cost <= lcap:
                    lines[l] += '%d%s ' % (i, d)
                    used, last = used + cost, g
                    break
                nxt = min(nxt, need)
                # no room: move to the next line (unless this one is empty,
                # i.e. the hint is wider than a line); the last line skips it
                if l < final and used > 0:
                    l, used, last = l + 1, 0, -1
                    continue
                every = False
                break
        return l, lines, every, nxt

    def layouts(self, more_w):
        """Every distinct layout, in order of the width it starts at:
        returns (per line, a list of (start width, kind, line)), where kind
        is 'all' or 'some' (hints hidden: the last line ends in a …), and
        "width:lines " at each change in the lines needed, for auto."""
        n = self.nlines
        # below the hints' total width over all lines not everything can
        # fit, so only the layout that reserves room for the … is needed
        fits_from = (sum(self.widths) + n - 1) // n
        out = [[] for _ in range(n)]
        prev = ['-'] * n
        w, prev_need, need_at = 0, 0, ''
        while True:
            every, nxt = False, fits_from
            if w >= fits_from:
                r, lines, every, nxt = self.fill(w, 0)
            if every:
                kind, need = 'all', r + 1
            else:
                kind, need = 'some', n
                _, lines, _, nxt2 = self.fill(w, more_w)
                nxt = min(nxt, nxt2)
            if need != prev_need:
                need_at += '%d:%d ' % (w, need)
                prev_need = need
            # a new layout per line only where that line changes
            for l in range(n):
                key = kind + ' ' + lines[l]
                if key != prev[l]:
                    prev[l] = key
                    out[l].append((w, kind, lines[l]))
            # even once everything fits, wider clients pull hints onto
            # earlier lines; stop when no choice can change
            if nxt >= NEVER:
                return out, need_at
            w = nxt


def layout_format(line, kind, last, refs, div, more):
    """The format for one laid-out line: each hint's option, dividers
    where marked, and the … if hints are hidden and this is the LAST line."""
    s = ''
    for p in line.split():
        if p.endswith('D'):
            s, p = s + div, p[:-1]
        s += '#{E:' + refs[int(p)] + '}'
    return s + more if kind == 'some' and last else s


def search(name, starts, fmts, size, spill):
    """Binary search format choosing among FMTS by #{client_width}, FMTS[i]
    starting at STARTS[i]. Branches over BRANCH_MAX bytes (by SIZE) move
    into options of their own via SPILL(option, value)."""
    def tree(lo, hi):
        if lo == hi:
            return fmts[lo]
        mid = (lo + hi + 1) // 2
        s = '#{?#{e|<:#{client_width},%d},%s,%s}' % (starts[mid], tree(lo, mid - 1),
                                                       tree(mid, hi))
        if size(s) > BRANCH_MAX:
            opt = '%s-%d-%d' % (name, lo, hi)
            spill(opt, s)
            s = '#{E:' + opt + '}'
        return s
    return tree(0, len(fmts) - 1)


def need_format(which, need_at):
    """Format on #{client_width} for the most lines any table in WHICH needs,
    from each table's "width:lines " changes in NEED_AT."""
    pairs = {t: [tuple(int(x) for x in p.split(':')) for p in need_at.get(t, '').split()]
             for t in which}
    ws = sorted({w for ps in pairs.values() for w, _ in ps})
    if not ws:
        return '1'
    most = {w: max([1] + [([n for pw, n in ps if pw <= w] or [1])[-1] for ps in pairs.values()])
            for w in ws}
    # keep only the changes, then nest from the widest down
    keep = [w for i, w in enumerate(ws) if i == 0 or most[w] != most[ws[i - 1]]]
    s = str(most[keep[-1]])
    for i in range(len(keep) - 1, 0, -1):
        s = '#{?#{e|<:#{client_width},%d},%d,%s}' % (keep[i], most[keep[i - 1]], s)
    return s
