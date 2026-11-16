"""
number_params.py — a typed number becomes a parameter nobody has to name.

"Everything needs to be parameters": a dimension typed as 10 is a number nothing else
can follow, so the section references, the tether sketches and anything replayed from
the sketch would carry a frozen 10. Instead every length or angle literal typed into one
of our windows (and, once, every one already in the document — the sweep) becomes a
NUMBER PARAMETER: a user parameter named Num_<8 hex>, holding the literal as its
expression. The user still reads and types "10" — the value window shows the chip as
its value in its own colour (theme.css --chip-numparam) — and changing that chip changes
the parameter, so whatever was built from it follows.

Which literals: a number WITH a length/angle unit anywhere ("10 mm", "cos(30 deg)"),
and a bare number in a VALUE position of a length/angle expression — the whole thing,
or a term of a sum ("Shelf + 10"), through brackets, signs and the numerator of a
division. A bare number that multiplies or divides a length or angle ("2 * Shelf",
"Shelf / 2", "Shelf * (1 + 0.1)") is a SCALE: it becomes a unitless number parameter
the same way (user, 2026-10-09: "times 2 is a typed number too"). What stays: a power
("x ^ 2"), a function's bare argument ("sqrt(2)"), two bare numbers that multiply
("10 * 2" — nothing says which is the length), zeros, and anything in a unitless field
(a count, a scale factor).

Pure, no adsk: AddIns/Number Params.py does the Fusion side. Offline test:
number_params_test.py (/usr/bin/python3).
"""

import re
import uuid

PREFIX = 'Num_'
NAME_RE = re.compile(r'^Num_[0-9a-f]{8}$')
COMMENT = 'Typed number (hidden)'

LENGTH_UNITS = {'mm', 'cm', 'm', 'in', 'ft', 'um', 'micron', 'yd'}
ANGLE_UNITS = {'deg', 'rad'}

_TOKEN = re.compile(r'\s*(?:(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)'
                    r'|([A-Za-z_][A-Za-z0-9_]*)|(\*\*|[-+*/^(),]))')


def is_number_name(name):
    return bool(name) and NAME_RE.match(name) is not None


def new_name(taken=()):
    while True:
        n = PREFIX + uuid.uuid4().hex[:8]
        if n not in taken:
            return n


def kind_of_unit(unit):
    """'L' length, 'A' angle, '' anything else (a count, a factor, no unit)."""
    u = (unit or '').strip()
    if u in LENGTH_UNITS:
        return 'L'
    if u in ANGLE_UNITS:
        return 'A'
    return ''


def _tokens(text):
    """[(kind, value, a, b)] with spans; None when the text is not one we can read."""
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == '':
                break
            return None
        pos = m.end()
        if m.group(1):
            out.append(('num', float(m.group(1)), m.start(1), m.end(1)))
        elif m.group(2):
            out.append(('name', m.group(2), m.start(2), m.end(2)))
        else:
            op = m.group(3)
            out.append(('op', '^' if op == '**' else op, m.start(3), m.end(3)))
    return out


class _Reader(object):
    """section_derive's grammar, walked for where each literal sits rather than for its
    value. Every node is (kind, role-free info, span) and every literal is collected as
    {a, b, value, unit, role}; the role is decided top-down as the tree is built."""

    def __init__(self, toks, kinds=None):
        self.toks, self.i, self.found = toks, 0, []
        self.kinds = kinds or (lambda name: None)

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None, 0, 0)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def read(self):
        node = self.expr()
        if self.i != len(self.toks):
            raise ValueError('trailing input')
        self.mark(node, 'value')
        return self.found

    # A node: ('lit', rec) | ('name',) | ('neg', a) | ('add', a, b) | ('mul', a, b) |
    #         ('div', a, b) | ('pow', a, b) | ('call', [args]) | ('group', a)
    def expr(self):
        node = self.term()
        while self.peek()[:2] in (('op', '+'), ('op', '-')):
            self.take()
            node = ('add', node, self.term())
        return node

    def term(self):
        node = self.unary()
        while self.peek()[:2] in (('op', '*'), ('op', '/')):
            op = self.take()[1]
            node = ('mul' if op == '*' else 'div', node, self.unary())
        return node

    def unary(self):
        if self.peek()[:2] in (('op', '-'), ('op', '+')):
            self.take()
            return ('neg', self.unary())
        return self.power()

    def power(self):
        node = self.atom()
        if self.peek()[:2] == ('op', '^'):
            self.take()
            node = ('pow', node, self.unary())
        return node

    def atom(self):
        kind, val, a, b = self.take()
        if kind == 'num':
            nxt = self.peek()
            if nxt[0] == 'name' and (nxt[1] in LENGTH_UNITS or nxt[1] in ANGLE_UNITS):
                self.take()
                return ('lit', {'a': a, 'b': nxt[3], 'value': val, 'unit': nxt[1]})
            return ('lit', {'a': a, 'b': b, 'value': val, 'unit': None})
        if kind == 'name':
            if self.peek()[:2] == ('op', '('):
                self.take()
                args = []
                if self.peek()[:2] != ('op', ')'):
                    args.append(self.expr())
                    while self.peek()[:2] == ('op', ','):
                        self.take()
                        args.append(self.expr())
                if self.take()[:2] != ('op', ')'):
                    raise ValueError('unclosed call')
                return ('call', args)
            return ('name', val)
        if (kind, val) == ('op', '('):
            node = self.expr()
            if self.take()[:2] != ('op', ')'):
                raise ValueError('unclosed bracket')
            return ('group', node)
        raise ValueError('unexpected %r' % (val,))

    @staticmethod
    def _bare(node):
        """A bare unitless number (through brackets and signs) — a factor's shape."""
        while node[0] in ('group', 'neg'):
            node = node[1]
        return node[0] == 'lit' and node[1]['unit'] is None

    def _unitless(self, node):
        """Whether `node` is surely unitless — a factor's shape: numbers only, or a sum
        holding a parameter `kinds` says is unitless ('S'). "Shelf * (1 + 0.1)" and
        "Shelf * (N + 1)" are lengths times a factor; reading the bracket's numbers as
        lengths made "Shelf * (1 mm + 0.1 mm)", which Fusion refuses (2026-10-08)."""
        k = node[0]
        if k == 'lit':
            return node[1]['unit'] is None
        if k == 'name':
            return self.kinds(node[1]) == 'S'
        if k in ('neg', 'group'):
            return self._unitless(node[1])
        if k in ('add', 'mul', 'div'):
            # Every part: a parameter nobody said is unitless may be the length.
            return self._unitless(node[1]) and self._unitless(node[2])
        if k == 'pow':
            return self._unitless(node[1])
        return False

    def mark(self, node, role):
        """role: 'value' (a length/angle), 'scale' (a unitless factor of one) or None
        (stays a number)."""
        k = node[0]
        scale = role and 'scale'           # what a factor under `role` is
        if k == 'lit':
            rec = dict(node[1])
            rec['role'] = role
            self.found.append(rec)
        elif k in ('neg', 'group'):
            self.mark(node[1], role)
        elif k == 'add':
            self.mark(node[1], role)
            self.mark(node[2], role)
        elif k == 'mul':
            a, b = node[1], node[2]
            ba, bb = self._bare(a), self._bare(b)
            if ba and bb:
                # "10 * 2": nothing says which one is the length.
                self.mark(a, None)
                self.mark(b, None)
            elif not ba and not bb:
                # Neither a bare number: a side that is surely unitless is the factor
                # ("Shelf * (1 + 0.1)"); the other carries the value.
                ua, ub = self._unitless(a), self._unitless(b)
                if ua and ub:              # "(1 + 1) * (2 + 3)": like "10 * 2"
                    role = scale = None
                self.mark(a, scale if ua else role)
                self.mark(b, scale if ub else role)
            else:
                self.mark(a, scale if ba else role)
                self.mark(b, scale if bb else role)
        elif k == 'div':
            self.mark(node[1], role)
            self.mark(node[2], scale)
        elif k == 'pow':
            self.mark(node[1], None)
            self.mark(node[2], None)
        elif k == 'call':
            for arg in node[1]:
                self.mark(arg, None)


def literals(text, unit, kinds=None):
    """[{a, b, text, unit}] — the literals of `text` that become number parameters, in
    order; `unit` is the parameter's to make (the literal's own, else the field's).
    [] when the text cannot be read (nothing is converted on a guess). `kinds`, if
    given, is name -> 'L' / 'A' / 'S' (unitless) / None for the parameters the text
    names: it tells a factor in brackets from a length ("Shelf * (N + 1)")."""
    text = text or ''
    toks = _tokens(text)
    if not toks:
        return []
    try:
        found = _Reader(toks, kinds).read()
    except Exception:
        return []
    field = kind_of_unit(unit)
    out = []
    for rec in found:
        if rec['value'] == 0:
            continue
        if rec['unit'] is not None:
            u = rec['unit']
        elif rec['role'] == 'value' and field:
            u = unit.strip()
        elif rec['role'] == 'scale' and field:
            u = ''
        else:
            continue
        out.append({'a': rec['a'], 'b': rec['b'], 'text': text[rec['a']:rec['b']],
                    'unit': u, 'value': rec['value']})
    return out


def is_whole_literal(text):
    """'10', '-10 mm', ' 2.5cm ' — one literal and nothing else. A user parameter's own
    value written like that IS already a parameter, so it is left as it is."""
    toks = _tokens(text or '')
    if not toks:
        return False
    while toks and toks[0][:2] in (('op', '-'), ('op', '+')):
        toks = toks[1:]
    if not toks or toks[0][0] != 'num':
        return False
    rest = toks[1:]
    return not rest or (len(rest) == 1 and rest[0][0] == 'name' and
                        (rest[0][1] in LENGTH_UNITS or rest[0][1] in ANGLE_UNITS))


def names_in(text):
    """The number parameters `text` names, in order (repeats kept once)."""
    out = []
    for kind, val, _a, _b in _tokens(text or '') or ():
        if kind == 'name' and is_number_name(val) and val not in out:
            out.append(val)
    return out


def literal_value(text, default_unit):
    """(value, unit) of a literal's text — '10' in mm is (10.0, 'mm'); None if not one."""
    toks = _tokens(text or '')
    if not toks:
        return None
    sign = 1.0
    while toks and toks[0][:2] in (('op', '-'), ('op', '+')):
        if toks[0][1] == '-':
            sign = -sign
        toks = toks[1:]
    if not toks or toks[0][0] != 'num':
        return None
    unit = default_unit
    if len(toks) == 2 and toks[1][0] == 'name':
        unit = toks[1][1]
    elif len(toks) != 1:
        return None
    return (sign * toks[0][1], unit)


def plan(text, unit, old=None, kinds=None):
    """Which number parameter each literal of `text` becomes.

    old  {name: (expression, unit)} — the number parameters the field held before this
         edit, in the order it held them. A literal equal to one of them keeps that one
         (nothing moves); the rest take what is left over in order, so a chip changed
         from 10 to 12 CHANGES its parameter rather than leaving it behind; anything
         past that is new.

    Returns (spans, leftover): spans [{a, b, text, unit, name or None (make one)}],
    leftover the old names no literal took (the caller deletes them if nothing else
    uses them)."""
    spans = literals(text, unit, kinds)
    old = list((old or {}).items())
    free = [n for n, _v in old]
    kinds = {n: kind_of_unit((literal_value(e, u) or (0, u))[1]) for n, (e, u) in old}
    for s in spans:
        s['name'] = None
    for s in spans:
        mine = literal_value(s['text'], s['unit'])
        for n, (expr, ounit) in old:
            if n in free and mine is not None and literal_value(expr, ounit) == mine:
                s['name'] = n
                free.remove(n)
                break
    for s in spans:
        if s['name'] is None:
            # Only one of the same kind: a length's parameter never becomes an angle.
            k = kind_of_unit(s['unit'])
            n = next((n for n in free if kinds[n] == k), None)
            if n is not None:
                s['name'] = n
                free.remove(n)
    return spans, free


def rewrite(text, spans):
    """`text` with each span replaced by its name."""
    out = text
    for s in sorted(spans, key=lambda s: -s['a']):
        out = out[:s['a']] + s['name'] + out[s['b']:]
    return out


def display(text, values):
    """`text` with every number parameter written as its literal — what a list that
    never shows them (the Browser, the Params window) reads and edits. values
    {name: expression}. Editing it back is safe: set_expression keeps the parameter a
    literal equal to it already has."""
    toks = _tokens(text or '')
    if not toks:
        return text
    out = text
    for kind, val, a, b in reversed(toks):
        if kind == 'name' and val in values:
            lit = ' '.join((values[val] or '').split())
            if lit[:1] in '-+':
                lit = '(%s)' % lit
            out = out[:a] + lit + out[b:]
    return out


def shown(expr, field_unit=''):
    """How a number parameter's chip reads: its literal, without the field's own unit
    ('10 mm' in a mm field is '10')."""
    t = ' '.join((expr or '').split())
    lv = literal_value(t, field_unit)
    if lv is None:
        return t
    v, u = lv
    num = ('%.6f' % v).rstrip('0').rstrip('.')
    return num if (not u or u == field_unit) else '%s %s' % (num, u)
