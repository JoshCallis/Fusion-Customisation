"""
section_derive.py — where a sketch point is, written in parameters.

The Section Sketch add-in draws the model's section into the sketch you are in, and
anything you tie to that drawing becomes dimensions to the BOUNDS that use the SAME
parameters as the thing you tied to — not a projection, not a reference to the other
sketch. That needs one question answered for every point of every source sketch:

    this point is at u = 70.5 cm — which expression says so?

Answered by walking the source sketch's own dimensions and constraints out from what
is already known (its origin, the bounds points projected into it, fixed geometry) and
writing each coordinate as a LINEAR FORM:

    Form(k, t)   =   k  +  sum(coef * term)        k in internal cm

A term is a user parameter name, or — for anything not linear (a cosine, a product of
two parameters) — the sub-expression's own text, kept whole. Model parameters (d123)
never survive: they are inlined down to user parameters and numbers, so a dimension
built from a form depends on nothing but the parameters the user owns. That is the
point of the whole scheme: two things that match each other both reference the
parameters, neither references the other.

EVERY ANSWER IS CHECKED. A form is accepted only if it evaluates to the coordinate the
geometry actually has (TOL). So a wrong sign, a rule applied to geometry it does not
fit, or an expression this file misreads can only ever lose an answer, never give a
wrong one.

Pure, no adsk: the add-in reads the sketch into plain dicts (see `derive`) and the
parameter table into plain values. Offline test: section_derive_test.py.
Load by path (like planes.py) — fusion_ui is on no add-in's import path.
"""

import math
import re

TOL = 1e-4                      # a micron, in cm — Bounds.TOL
ANGLE_TOL = 1e-6                # radians

# Fusion internal units: cm for length, radians for angle.
LENGTH_UNITS = {'mm': 0.1, 'cm': 1.0, 'm': 100.0, 'in': 2.54, 'ft': 30.48,
                'um': 1e-4, 'micron': 1e-4, 'yd': 91.44}
ANGLE_UNITS = {'deg': math.pi / 180.0, 'rad': 1.0}


# ── Forms ────────────────────────────────────────────────────────────────────
class Form(object):
    """k + sum(coef * term). Immutable in use: every operation returns a new one.

    `weak` marks a form that rests on a number nobody can change — a fixed point, a
    projection that could not be followed. It is still RIGHT now; it just will not
    follow the model, and the add-in says so."""

    __slots__ = ('k', 't', 'weak')

    def __init__(self, k=0.0, t=None, weak=False):
        self.k = float(k)
        self.t = dict(t or {})
        self.weak = bool(weak)

    @staticmethod
    def const(k, weak=False):
        return Form(k, None, weak)

    @staticmethod
    def term(name, coef=1.0):
        return Form(0.0, {name: coef})

    def __add__(self, o):
        t = dict(self.t)
        for n, c in o.t.items():
            t[n] = t.get(n, 0.0) + c
            if abs(t[n]) < 1e-12:
                del t[n]
        return Form(self.k + o.k, t, self.weak or o.weak)

    def __neg__(self):
        return Form(-self.k, {n: -c for n, c in self.t.items()}, self.weak)

    def __sub__(self, o):
        return self + (-o)

    def scale(self, s):
        if abs(s) < 1e-15:
            return Form(0.0, None, self.weak)
        return Form(self.k * s, {n: c * s for n, c in self.t.items()}, self.weak)

    def is_const(self):
        return not self.t

    def terms(self):
        return sorted(self.t)

    def value(self, values):
        """Evaluate against {term: value}. None if a term is unknown."""
        v = self.k
        for n, c in self.t.items():
            if n not in values or values[n] is None:
                return None
            v += c * values[n]
        return v

    def to_json(self):
        return {'k': round(self.k, 9), 't': {n: round(c, 12) for n, c in self.t.items()},
                'w': self.weak}

    @staticmethod
    def from_json(d):
        if not d:
            return None
        return Form(d.get('k', 0.0), d.get('t') or {}, d.get('w', False))

    def __repr__(self):
        return 'Form(%s)' % render(self)


def _num(v):
    s = ('%.6f' % v).rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def _coef_text(c):
    return _num(c)


def _wrap(term):
    """A term as it sits after a coefficient or a minus: names bare, the rest bracketed."""
    return term if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', term) else '(%s)' % term


def render(form, unit='mm'):
    """A form as a Fusion length expression: 'Right - Shelf_Thickness - 15 mm'.

    Positive terms first, then negative, then the constant — so it reads the way a
    person would write it, from the datum inward."""
    scale = LENGTH_UNITS.get(unit, 0.1)
    parts = []
    # Named parameters before bracketed terms (a typed "(5 mm)", a cosine), so the
    # datum's own parameters lead: "Shelf_Drop + (50 mm)".
    plain = lambda n: re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', n) is not None
    names = sorted(form.t, key=lambda n: (form.t[n] < 0, not plain(n), n))
    for n in names:
        c = form.t[n]
        mag = abs(c)
        div = next((d for d in range(2, 17) if abs(mag * d - 1.0) < 1e-9), None)
        body = (_wrap(n) if abs(mag - 1.0) < 1e-12
                else '%s / %d' % (_wrap(n), div) if div   # "Rib / 2", not "0.5 * Rib"
                else '%s * %s' % (_coef_text(mag), _wrap(n)))
        parts.append(('-' if c < 0 else '+', body))
    if abs(form.k) > 1e-9 or not parts:
        parts.append(('-' if form.k < 0 else '+', '%s %s' % (_num(abs(form.k) / scale), unit)))
    out = ''
    for i, (sign, body) in enumerate(parts):
        if i == 0:
            out = ('-' if sign == '-' else '') + body
        else:
            out += ' %s %s' % (sign, body)
    return out


# ── Parsing Fusion expressions ───────────────────────────────────────────────
_TOKEN = re.compile(r'\s*(?:(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)'
                    r'|([A-Za-z_][A-Za-z0-9_]*)|(\*\*|[-+*/^(),]))')


def tokenize(text):
    out, pos, text = [], 0, text or ''
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == '':
                break
            raise ValueError('cannot read %r at %d' % (text, pos))
        pos = m.end()
        if m.group(1):
            out.append(('num', float(m.group(1))))
        elif m.group(2):
            out.append(('name', m.group(2)))
        else:
            out.append(('op', '^' if m.group(3) == '**' else m.group(3)))
    return out


class _Parser(object):
    """Recursive descent to a small AST:
        ('num', value, unit|None) ('name', n) ('neg', a) ('add'|'sub'|'mul'|'div'|'pow', a, b)
        ('call', fn, [args])
    A number directly followed by a unit word takes it ('15 mm', '45 deg')."""

    def __init__(self, tokens):
        self.toks, self.i = tokens, 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        node = self.expr()
        if self.i != len(self.toks):
            raise ValueError('trailing input')
        return node

    def expr(self):
        node = self.term()
        while self.peek() in (('op', '+'), ('op', '-')):
            op = self.take()[1]
            node = ('add' if op == '+' else 'sub', node, self.term())
        return node

    def term(self):
        node = self.unary()
        while self.peek() in (('op', '*'), ('op', '/')):
            op = self.take()[1]
            node = ('mul' if op == '*' else 'div', node, self.unary())
        return node

    def unary(self):
        if self.peek() == ('op', '-'):
            self.take()
            return ('neg', self.unary())
        if self.peek() == ('op', '+'):
            self.take()
            return self.unary()
        return self.power()

    def power(self):
        node = self.atom()
        if self.peek() == ('op', '^'):
            self.take()
            node = ('pow', node, self.unary())
        return node

    def atom(self):
        kind, val = self.take()
        if kind == 'num':
            nxt = self.peek()
            if nxt[0] == 'name' and (nxt[1] in LENGTH_UNITS or nxt[1] in ANGLE_UNITS):
                self.take()
                return ('num', val, nxt[1])
            return ('num', val, None)
        if kind == 'name':
            if self.peek() == ('op', '('):
                self.take()
                args = []
                if self.peek() != ('op', ')'):
                    args.append(self.expr())
                    while self.peek() == ('op', ','):
                        self.take()
                        args.append(self.expr())
                if self.take() != ('op', ')'):
                    raise ValueError('unclosed call')
                return ('call', val.lower(), args)
            if val in LENGTH_UNITS or val in ANGLE_UNITS:
                return ('num', 1.0, val)       # a bare unit word: "mm" alone
            return ('name', val)
        if (kind, val) == ('op', '('):
            node = self.expr()
            if self.take() != ('op', ')'):
                raise ValueError('unclosed bracket')
            return node
        raise ValueError('unexpected %r' % (val,))


def parse(text):
    return _Parser(tokenize(text)).parse()


def unparse(node):
    """AST back to text, fully bracketed where precedence could bite."""
    k = node[0]
    if k == 'num':
        return _num(node[1]) + (' ' + node[2] if node[2] else '')
    if k == 'name':
        return node[1]
    if k == 'neg':
        return '-' + _wrap_node(node[1])
    if k == 'call':
        return '%s(%s)' % (node[1], ', '.join(unparse(a) for a in node[2]))
    sym = {'add': '+', 'sub': '-', 'mul': '*', 'div': '/', 'pow': '^'}[k]
    return '%s %s %s' % (_wrap_node(node[1]) if k != 'add' else unparse(node[1]), sym,
                         _wrap_node(node[2]) if k in ('sub', 'mul', 'div', 'pow') else unparse(node[2]))


def _wrap_node(node):
    if node[0] in ('num', 'name', 'call'):
        return unparse(node)
    return '(%s)' % unparse(node)


# ── The parameter table ──────────────────────────────────────────────────────
class Params(object):
    """What the derivation knows about parameters.

    user   {name: (value, kind)}   kind 'L' length, 'A' angle, 'S' unitless — the ones
                                   that survive into forms as terms
    model  {name: (expression, unit)}  model parameters (d123), inlined on sight. Any
           mapping with `in` and `[]` — the add-in passes one that reads Fusion lazily,
           since a model has thousands of them and a derivation touches a handful.
    default_length / default_angle  what a bare number means in a length / angle context
    """

    def __init__(self, user=None, model=None, default_length='mm', default_angle='deg'):
        self.user = dict(user or {})
        self.model = model if model is not None else {}
        self.default_length = default_length
        self.default_angle = default_angle
        self._inline_cache = {}

    def model_ast(self, name, depth=0):
        if depth > 40:
            raise ValueError('model parameter chain too deep at %s' % name)
        if name not in self._inline_cache:
            expr, _unit = self.model[name]
            self._inline_cache[name] = parse(expr)
        return self._inline_cache[name]

    def term_values(self):
        return {n: v for n, (v, _k) in self.user.items()}


# ── Evaluation with dimensions ───────────────────────────────────────────────
# A value is (number, kind): 'L' length (cm), 'A' angle (rad), 'S' unitless, 'L2'
# area... A bare number is unitless until it meets a length or angle in a sum, where
# Fusion reads it in the default unit — the one place units are contextual.
def evaluate(node, params, want='L', depth=0):
    v, kind = _eval(node, params, depth)
    if kind == 'S' and want == 'L':
        return v * LENGTH_UNITS[params.default_length]
    if kind == 'S' and want == 'A':
        return v * ANGLE_UNITS[params.default_angle]
    return v


def _promote(a, b, params):
    (va, ka), (vb, kb) = a, b
    if ka == kb:
        return va, vb, ka
    if ka == 'S' and kb in ('L', 'A'):
        return va * (LENGTH_UNITS[params.default_length] if kb == 'L'
                     else ANGLE_UNITS[params.default_angle]), vb, kb
    if kb == 'S' and ka in ('L', 'A'):
        return va, vb * (LENGTH_UNITS[params.default_length] if ka == 'L'
                         else ANGLE_UNITS[params.default_angle]), ka
    raise ValueError('cannot add %s to %s' % (ka, kb))


_MUL = {('S', 'S'): 'S', ('L', 'S'): 'L', ('S', 'L'): 'L', ('A', 'S'): 'A', ('S', 'A'): 'A',
        ('L', 'L'): 'L2', ('L2', 'S'): 'L2', ('S', 'L2'): 'L2', ('L', 'A'): 'L', ('A', 'L'): 'L'}
_DIV = {('S', 'S'): 'S', ('L', 'S'): 'L', ('A', 'S'): 'A', ('L', 'L'): 'S', ('A', 'A'): 'S',
        ('L2', 'L'): 'L', ('L2', 'L2'): 'S'}


def _eval(node, params, depth=0):
    k = node[0]
    if k == 'num':
        unit = node[2]
        if unit in LENGTH_UNITS:
            return node[1] * LENGTH_UNITS[unit], 'L'
        if unit in ANGLE_UNITS:
            return node[1] * ANGLE_UNITS[unit], 'A'
        return node[1], 'S'
    if k == 'name':
        n = node[1]
        if n in params.user:
            return params.user[n]
        if n in params.model:
            sub = params.model_ast(n, depth)
            unit = params.model[n][1] or ''
            v, kind = _eval(sub, params, depth + 1)
            if kind == 'S' and unit in LENGTH_UNITS:
                return v * LENGTH_UNITS[params.default_length], 'L'
            if kind == 'S' and unit in ANGLE_UNITS:
                return v * ANGLE_UNITS[params.default_angle], 'A'
            return v, kind
        if n.lower() == 'pi':
            return math.pi, 'S'
        raise KeyError(n)
    if k == 'neg':
        v, kind = _eval(node[1], params, depth)
        return -v, kind
    if k in ('add', 'sub'):
        va, vb, kind = _promote(_eval(node[1], params, depth), _eval(node[2], params, depth), params)
        return (va + vb if k == 'add' else va - vb), kind
    if k == 'mul':
        (va, ka), (vb, kb) = _eval(node[1], params, depth), _eval(node[2], params, depth)
        if (ka, kb) in (('L', 'A'), ('A', 'L')):   # arc length: r * angle
            return va * vb, 'L'
        return va * vb, _MUL.get((ka, kb), 'X')
    if k == 'div':
        (va, ka), (vb, kb) = _eval(node[1], params, depth), _eval(node[2], params, depth)
        return va / vb, _DIV.get((ka, kb), 'X')
    if k == 'pow':
        (va, ka), (vb, _kb) = _eval(node[1], params, depth), _eval(node[2], params, depth)
        return va ** vb, ('S' if ka == 'S' else ('L2' if ka == 'L' and vb == 2 else 'X'))
    if k == 'call':
        fn, args = node[1], node[2]
        if fn in ('cos', 'sin', 'tan'):
            v, kind = _eval(args[0], params, depth)
            if kind == 'S':
                v *= ANGLE_UNITS[params.default_angle]
            return getattr(math, fn)(v), 'S'
        if fn in ('acos', 'asin', 'atan'):
            v, _kind = _eval(args[0], params, depth)
            return getattr(math, fn)(v), 'A'
        if fn == 'sqrt':
            v, kind = _eval(args[0], params, depth)
            return math.sqrt(v), ('L' if kind == 'L2' else kind)
        if fn == 'abs':
            v, kind = _eval(args[0], params, depth)
            return abs(v), kind
        if fn in ('min', 'max'):
            vals = [_eval(a, params, depth) for a in args]
            kind = next((kk for _v, kk in vals if kk != 'S'), 'S')
            nums = [evaluate(a, params, kind if kind in ('L', 'A') else 'S', depth) for a in args]
            return (min(nums) if fn == 'min' else max(nums)), kind
        if fn in ('floor', 'ceil', 'round'):
            v, kind = _eval(args[0], params, depth)
            return float(getattr(math, fn)(v) if fn != 'round' else round(v)), kind
        raise ValueError('unknown function %s' % fn)
    raise ValueError('bad node %r' % (k,))


def evaluate_text(text, params, want='L'):
    return evaluate(parse(text), params, want)


# ── Expression -> Form ───────────────────────────────────────────────────────
def _scalar(node, params):
    """The node's value if it is a pure unitless constant (2, 0.5, 3/4), else None."""
    k = node[0]
    if k == 'num' and node[2] is None:
        return node[1]
    if k == 'neg':
        s = _scalar(node[1], params)
        return -s if s is not None else None
    if k in ('add', 'sub', 'mul', 'div'):
        a, b = _scalar(node[1], params), _scalar(node[2], params)
        if a is None or b is None:
            return None
        return {'add': a + b, 'sub': a - b, 'mul': a * b,
                'div': a / b if b else None}[k]
    if k == 'name' and node[1] in params.user and params.user[node[1]][1] == 'S':
        return None          # a unitless PARAMETER is a term, not a constant
    return None


def _inline(node, params, depth=0):
    """The AST with every model parameter replaced by its own (inlined) expression, so
    an opaque term never names a d123."""
    k = node[0]
    if k == 'name' and node[1] in params.model:
        if depth > 40:
            raise ValueError('model parameter chain too deep')
        return _inline(params.model_ast(node[1]), params, depth + 1)
    if k in ('neg',):
        return (k, _inline(node[1], params, depth))
    if k in ('add', 'sub', 'mul', 'div', 'pow'):
        return (k, _inline(node[1], params, depth), _inline(node[2], params, depth))
    if k == 'call':
        return (k, node[1], [_inline(a, params, depth) for a in node[2]])
    return node


def to_form(node, params, depth=0):
    """A LENGTH expression's AST as a Form (cm). Linear structure is kept; anything else
    becomes one opaque term, its value checked in `opaque_values`."""
    k = node[0]
    if k == 'num':
        if node[2] in LENGTH_UNITS:
            return Form.const(node[1] * LENGTH_UNITS[node[2]])
        if node[2] is None:
            return Form.const(node[1] * LENGTH_UNITS[params.default_length])
        raise ValueError('an angle where a length was wanted')
    if k == 'name':
        n = node[1]
        if n in params.user:
            if params.user[n][1] != 'L':
                raise ValueError('%s is not a length' % n)
            return Form.term(n)
        if n in params.model:
            if depth > 40:
                raise ValueError('model parameter chain too deep')
            return to_form(params.model_ast(n), params, depth + 1)
        raise KeyError(n)
    if k == 'neg':
        return -to_form(node[1], params, depth)
    if k == 'add':
        return to_form(node[1], params, depth) + to_form(node[2], params, depth)
    if k == 'sub':
        return to_form(node[1], params, depth) - to_form(node[2], params, depth)
    if k == 'mul':
        sa, sb = _scalar(node[1], params), _scalar(node[2], params)
        if sb is not None:
            return to_form(node[1], params, depth).scale(sb)
        if sa is not None:
            return to_form(node[2], params, depth).scale(sa)
    if k == 'div':
        sb = _scalar(node[2], params)
        if sb:
            return to_form(node[1], params, depth).scale(1.0 / sb)
    # Not linear: one opaque term, named by its inlined text.
    return Form.term(unparse(_inline(node, params)))


def form_of(text, params):
    """A length expression's text as a Form, or None if it cannot be read."""
    try:
        return to_form(parse(text), params)
    except Exception:
        return None


def opaque_values(form, params):
    """{term: value} for every term of `form` — user parameters straight off the table,
    opaque terms evaluated. None if any cannot be."""
    out = {}
    for n in form.t:
        if n in params.user:
            out[n] = params.user[n][0]
            continue
        try:
            out[n] = evaluate_text(n, params, 'L')
        except Exception:
            return None
    return out


def form_value(form, params):
    if form is None:
        return None
    vals = opaque_values(form, params)
    return None if vals is None else form.value(vals)


# ── Angles ───────────────────────────────────────────────────────────────────
class Angle(object):
    """A line's direction as an angle expression: text (Fusion syntax) + its value in
    radians. Lines are undirected, so equality is modulo pi."""

    __slots__ = ('text', 'value')

    def __init__(self, text, value):
        self.text, self.value = text, value

    def plus(self, text, value, sign):
        if self.text in ('0 deg', ''):
            t = text if sign > 0 else '-(%s)' % text
        else:
            t = '%s %s (%s)' % (self.text, '+' if sign > 0 else '-', text)
        return Angle(t, self.value + sign * value)

    def turned(self, deg):
        if self.text in ('0 deg', ''):
            return Angle('%d deg' % deg, math.radians(deg))
        return Angle('%s + %d deg' % (self.text, deg), self.value + math.radians(deg))


def _mod_pi_close(a, b, tol=1e-4):
    d = (a - b) % math.pi
    return d < tol or math.pi - d < tol


# ── The sketch graph ─────────────────────────────────────────────────────────
def derive(sketch, params, tol=TOL, max_rounds=60):
    """Every point's coordinates as Forms.

    sketch = {
      'points': {pid: {'u': float, 'v': float,
                       'anchor': {'u': Form|None, 'v': Form|None}}},   # known up front
      'lines':  {lid: {'a': pid, 'b': pid}},
      'circles': {cid: {'c': pid, 'r': float, 'a': pid|None, 'b': pid|None}},  # arcs too
      'dims':   [{'kind': 'h'|'v'|'aligned'|'offset'|'angle'|'radius'|'diameter',
                  'ents': [id, ...], 'expr': str}],
      'cons':   [{'kind': ..., 'ents': [id, ...]}],
      'patterns': [{'pairs': [(copy pid, original pid)],                # optional
                    'dirs': [{'u', 'v' (unit direction), 'form': Form of one spacing,
                              'value', 'q' (quantity)}]}],
    }
    An entity id is the pid/lid/cid itself; ids must be unique across the three maps.
    Constraint kinds: coincident, horizontal, vertical, hpoints, vpoints, midpoint,
    collinear, parallel, perpendicular, concentric, tangent, symmetry, equal.

    Returns {'points': {pid: {'u': Form|None, 'v': Form|None}},
             'lines': {lid: {'angle': (text, value)|None}},
             'radii': {cid: Form|None}}
    """
    pts = sketch.get('points') or {}
    lines = sketch.get('lines') or {}
    circles = sketch.get('circles') or {}
    dims = sketch.get('dims') or []
    cons = sketch.get('cons') or []
    patterns = sketch.get('patterns') or []

    known = {pid: {'u': None, 'v': None} for pid in pts}
    geom = {pid: {'u': p['u'], 'v': p['v']} for pid, p in pts.items()}
    radii = {cid: None for cid in circles}
    angles = {}

    def ok(form, want):
        v = form_value(form, params)
        return v is not None and abs(v - want) <= tol

    def setp(pid, ax, form):
        if pid not in known or form is None or known[pid][ax] is not None:
            return False
        if not ok(form, geom[pid][ax]):
            return False
        known[pid][ax] = form
        return True

    # Anchors first.
    for pid, p in pts.items():
        for ax in ('u', 'v'):
            f = (p.get('anchor') or {}).get(ax)
            if f is not None:
                setp(pid, ax, f)

    def line_dir(lid):
        a, b = lines[lid]['a'], lines[lid]['b']
        return (geom[b]['u'] - geom[a]['u'], geom[b]['v'] - geom[a]['v'])

    def line_axis(lid):
        """'h' if the line is horizontal (constant v), 'v' if vertical, else ''."""
        du, dv = line_dir(lid)
        if abs(dv) <= tol and abs(du) > tol:
            return 'h'
        if abs(du) <= tol and abs(dv) > tol:
            return 'v'
        return ''

    def const_axis(lid):
        """The axis a straight axis-aligned line holds constant: 'v' for horizontal."""
        return {'h': 'v', 'v': 'u'}.get(line_axis(lid), '')

    for lid in lines:
        la = line_axis(lid)
        if la == 'h':
            angles[lid] = Angle('0 deg', 0.0)
        elif la == 'v':
            angles[lid] = Angle('90 deg', math.pi / 2)

    def coord(ent, ax):
        """The known Form of an entity along `ax`: a point's coordinate, or an
        axis-aligned line's constant coordinate, or a circle's centre."""
        if ent in pts:
            return known[ent][ax]
        if ent in lines:
            if const_axis(ent) != ax:
                return None
            a, b = lines[ent]['a'], lines[ent]['b']
            return known[a][ax] or known[b][ax]
        if ent in circles:
            return known[circles[ent]['c']][ax]
        return None

    def gval(ent, ax):
        if ent in pts:
            return geom[ent][ax]
        if ent in lines:
            return geom[lines[ent]['a']][ax]
        if ent in circles:
            return geom[circles[ent]['c']][ax]
        return None

    def setc(ent, ax, form):
        """Set an entity's coordinate along ax. A line sets both its ends."""
        if ent in pts:
            return setp(ent, ax, form)
        if ent in lines:
            if const_axis(ent) != ax:
                return False
            a, b = lines[ent]['a'], lines[ent]['b']
            return setp(a, ax, form) | setp(b, ax, form)
        if ent in circles:
            return setp(circles[ent]['c'], ax, form)
        return False

    def share(e1, e2, ax):
        f1, f2 = coord(e1, ax), coord(e2, ax)
        changed = False
        if f1 is not None and f2 is None:
            changed |= setc(e2, ax, f1)
        elif f2 is not None and f1 is None:
            changed |= setc(e1, ax, f2)
        return changed

    def offset_pair(e1, e2, ax, dist):
        """e2 = e1 +/- dist along ax, sign from the geometry."""
        g1, g2 = gval(e1, ax), gval(e2, ax)
        if g1 is None or g2 is None:
            return False
        s = 1.0 if g2 >= g1 else -1.0
        f1, f2 = coord(e1, ax), coord(e2, ax)
        changed = False
        if f1 is not None and f2 is None:
            changed |= setc(e2, ax, f1 + dist.scale(s))
        elif f2 is not None and f1 is None:
            changed |= setc(e1, ax, f2 - dist.scale(s))
        elif f1 is None and f2 is None:
            # Neither end known, but a point is pinned to their midpoint: ends are mid -/+ dist/2.
            m = mids.get(frozenset((e1, e2)))
            fm = known[m][ax] if m is not None else None
            if fm is not None:
                half = dist.scale(0.5 * s)
                changed |= setc(e1, ax, fm - half) | setc(e2, ax, fm + half)
        return changed

    # {frozenset((a, b)): midpoint pid} — for a length measured between a line's ends.
    mids = {}
    for c in cons:
        if c['kind'] == 'midpoint' and len(c['ents']) == 2:
            p, ln = (c['ents'][0], c['ents'][1]) if c['ents'][0] in pts else (c['ents'][1], c['ents'][0])
            if ln in lines and p in pts:
                mids[frozenset((lines[ln]['a'], lines[ln]['b']))] = p

    def dim_form(d):
        if '_form' not in d:
            d['_form'] = form_of(d.get('expr', ''), params)
        return d['_form']

    aligned = _aligned_rule(known, lines, angles, setp)
    groups = _locked_groups(cons, lines, pts, line_axis)

    def lock_rule():
        """Points the constraints hold level (see _locked_groups) take a known
        member's coordinate."""
        moved = False
        for ax in ('u', 'v'):
            for g in groups[ax]:
                f = next((known[p][ax] for p in g if known[p][ax] is not None), None)
                if f is not None:
                    for p in g:
                        if known[p][ax] is None:
                            moved |= setp(p, ax, f)
        return moved

    def pattern_rule(pat):
        """A sketch rectangular pattern: each copy point sits at its original moved by
        n spacings along a direction, n a whole number within the quantity — read off
        the geometry, so 'the third rib' writes 3 * spacing. Only directions square to
        u or v; a coordinate the pattern does not move is the original's."""
        moved = False
        for c, o in pat['pairs']:
            for ax in ('u', 'v'):
                if c not in known or o not in known:
                    continue
                if known[c][ax] is not None or known[o][ax] is None:
                    continue
                delta = geom[c][ax] - geom[o][ax]
                if abs(delta) <= tol:
                    moved |= setp(c, ax, known[o][ax])
                    continue
                for dr in pat['dirs']:
                    comp = dr[ax]
                    if abs(abs(comp) - 1.0) > 1e-6 or not dr['value']:
                        continue
                    n = delta / (dr['value'] * comp)
                    k = int(round(n))
                    if k == 0 or abs(n - k) > 1e-6 or abs(k) > dr['q'] - 1:
                        continue
                    if setp(c, ax, known[o][ax] + dr['form'].scale(k * comp)):
                        moved = True
                        break
        return moved

    def angle_of_dim(d):
        if '_ang' not in d:
            try:
                d['_ang'] = (unparse(_inline(parse(d['expr']), params)),
                             evaluate_text(d['expr'], params, 'A'))
            except Exception:
                d['_ang'] = None
        return d['_ang']

    for _round in range(max_rounds):
        changed = False

        for c in cons:
            kind, ents = c['kind'], c['ents']
            try:
                if kind == 'coincident' and len(ents) == 2:
                    a, b = ents
                    if a in pts and b in pts:
                        changed |= share(a, b, 'u') | share(a, b, 'v')
                    else:
                        p, other = (a, b) if a in pts else (b, a)
                        if other in lines and const_axis(other):
                            changed |= share(p, other, const_axis(other))
                        elif other in circles and p == circles[other].get('c'):
                            pass
                elif kind in ('horizontal', 'vertical') and len(ents) == 1 and ents[0] in lines:
                    ln = lines[ents[0]]
                    changed |= share(ln['a'], ln['b'], 'v' if kind == 'horizontal' else 'u')
                elif kind in ('hpoints', 'vpoints') and len(ents) == 2:
                    changed |= share(ents[0], ents[1], 'v' if kind == 'hpoints' else 'u')
                elif kind == 'midpoint' and len(ents) == 2:
                    p, ln = (ents[0], ents[1]) if ents[0] in pts else (ents[1], ents[0])
                    if ln in lines:
                        a, b = lines[ln]['a'], lines[ln]['b']
                        for ax in ('u', 'v'):
                            fa, fb, fp = known[a][ax], known[b][ax], known[p][ax]
                            if fa is not None and fb is not None and fp is None:
                                changed |= setp(p, ax, (fa + fb).scale(0.5))
                            elif fp is not None and fa is not None and fb is None:
                                changed |= setp(b, ax, fp.scale(2.0) - fa)
                            elif fp is not None and fb is not None and fa is None:
                                changed |= setp(a, ax, fp.scale(2.0) - fb)
                elif kind == 'collinear' and len(ents) == 2:
                    l1, l2 = ents
                    if l1 in lines and l2 in lines and const_axis(l1) and const_axis(l1) == const_axis(l2):
                        changed |= share(l1, l2, const_axis(l1))
                elif kind == 'concentric' and len(ents) == 2:
                    c1, c2 = circles.get(ents[0]), circles.get(ents[1])
                    if c1 and c2:
                        changed |= share(c1['c'], c2['c'], 'u') | share(c1['c'], c2['c'], 'v')
                elif kind == 'tangent' and len(ents) == 2:
                    cid = next((e for e in ents if e in circles), None)
                    lid = next((e for e in ents if e in lines), None)
                    if cid and lid and const_axis(lid) and radii.get(cid) is not None:
                        changed |= offset_pair(lid, circles[cid]['c'], const_axis(lid), radii[cid])
                    if cid and lid and const_axis(lid):
                        # The touching end of the arc sits square to the centre: on a
                        # horizontal line it shares the centre's u (and the line's v).
                        other = 'u' if const_axis(lid) == 'v' else 'v'
                        ends = {lines[lid]['a'], lines[lid]['b']}
                        for e in (circles[cid].get('a'), circles[cid].get('b')):
                            if e in ends:
                                changed |= share(e, circles[cid]['c'], other)
                elif kind == 'symmetry' and len(ents) == 3:
                    e1, e2, axis_line = ents
                    if axis_line in lines and const_axis(axis_line):
                        ax = const_axis(axis_line)
                        other = 'v' if ax == 'u' else 'u'
                        mid = coord(axis_line, ax)
                        f1, f2 = coord(e1, ax), coord(e2, ax)
                        if mid is not None:
                            if f1 is not None and f2 is None:
                                changed |= setc(e2, ax, mid.scale(2.0) - f1)
                            elif f2 is not None and f1 is None:
                                changed |= setc(e1, ax, mid.scale(2.0) - f2)
                            elif f1 is None and f2 is None:
                                # Both free, but a width between them is dimensioned:
                                # each sits half of it either side of the axis.
                                want = 'h' if ax == 'u' else 'v'
                                for d in dims:
                                    if d['kind'] in (want, 'aligned') and set(d['ents']) == {e1, e2}:
                                        w = dim_form(d)
                                        if w is not None:
                                            half = w.scale(0.5)
                                            for e in (e1, e2):
                                                s = 1.0 if gval(e, ax) >= gval(axis_line, ax) else -1.0
                                                changed |= setc(e, ax, mid + half.scale(s))
                                        break
                        if e1 in pts and e2 in pts:
                            changed |= share(e1, e2, other)
                elif kind in ('parallel', 'perpendicular') and len(ents) == 2:
                    l1, l2 = ents
                    if l1 in lines and l2 in lines:
                        turn = 0 if kind == 'parallel' else 90
                        for x, y in ((l1, l2), (l2, l1)):
                            if x in angles and y not in angles:
                                cand = angles[x].turned(turn) if turn else angles[x]
                                du, dv = line_dir(y)
                                if _mod_pi_close(math.atan2(dv, du), cand.value):
                                    angles[y] = cand
                                    changed = True
            except Exception:
                continue

        for d in dims:
            kind, ents = d['kind'], d['ents']
            try:
                if kind in ('radius', 'diameter') and ents and ents[0] in circles:
                    cid = ents[0]
                    if radii[cid] is None:
                        f = dim_form(d)
                        if f is not None:
                            f = f.scale(0.5) if kind == 'diameter' else f
                            if ok(f, circles[cid]['r']):
                                radii[cid] = f
                                changed = True
                    continue
                if kind == 'angle' and len(ents) == 2:
                    l1, l2 = ents
                    ang = angle_of_dim(d)
                    if ang is None or l1 not in lines or l2 not in lines:
                        continue
                    for x, y in ((l1, l2), (l2, l1)):
                        if x in angles and y not in angles:
                            du, dv = line_dir(y)
                            actual = math.atan2(dv, du)
                            for sign in (1, -1):
                                cand = angles[x].plus(ang[0], ang[1], sign)
                                if _mod_pi_close(actual, cand.value):
                                    angles[y] = cand
                                    changed = True
                                    break
                    continue
                f = dim_form(d)
                if f is None or len(ents) != 2:
                    continue
                e1, e2 = ents
                if kind == 'h':
                    changed |= offset_pair(e1, e2, 'u', f)
                elif kind == 'v':
                    changed |= offset_pair(e1, e2, 'v', f)
                elif kind == 'offset':
                    if e1 in lines and e2 in lines and const_axis(e1) and const_axis(e1) == const_axis(e2):
                        changed |= offset_pair(e1, e2, const_axis(e1), f)
                    elif (e1 in lines) != (e2 in lines):
                        # addOffsetDimension(line, point) — square to the line, as aligned.
                        ln, p = (e1, e2) if e1 in lines else (e2, e1)
                        if const_axis(ln):
                            changed |= offset_pair(ln, p, const_axis(ln), f)
                elif kind == 'aligned':
                    if e1 in pts and e2 in pts:
                        du = geom[e2]['u'] - geom[e1]['u']
                        dv = geom[e2]['v'] - geom[e1]['v']
                        if abs(dv) <= tol:
                            changed |= offset_pair(e1, e2, 'u', f)
                        elif abs(du) <= tol:
                            changed |= offset_pair(e1, e2, 'v', f)
                        else:
                            changed |= aligned(e1, e2, f, du, dv)
                    elif (e1 in lines) != (e2 in lines):
                        # A point measured square to an axis-aligned line.
                        ln, p = (e1, e2) if e1 in lines else (e2, e1)
                        if const_axis(ln):
                            changed |= offset_pair(ln, p, const_axis(ln), f)
                    elif e1 in lines and e2 in lines and const_axis(e1) and const_axis(e1) == const_axis(e2):
                        changed |= offset_pair(e1, e2, const_axis(e1), f)
            except Exception:
                continue

        for pat in patterns:
            try:
                changed |= pattern_rule(pat)
            except Exception:
                continue
        changed |= lock_rule()

        if not changed:
            break

    return {'points': known,
            'lines': {lid: {'angle': (angles[lid].text, angles[lid].value) if lid in angles else None}
                      for lid in lines},
            'radii': radii}


def _locked_groups(cons, lines, pts, line_axis):
    """{'u': [set of pids], 'v': [...]} — points the CONSTRAINTS hold at one u (or v),
    found on the constraint graph alone, before any value is known.

    Beyond the explicit vertical/horizontal/h-v-points constraints:
      - a line held square by a chain of parallel/perpendicular/collinear constraints
        from one that is (a rectangle with one vertical side has four held sides)
      - a held line's midpoint sits level with its ends
      - two parallel held lines whose ends are pairwise held level across (a
        rectangle's top and bottom) have midpoints level across too — so a slot's
        centre line is vertical without a constraint saying so
      - a line whose two ends are held level IS held
    A line counts as held only if it is also drawn that way (`line_axis`) — a guard,
    since every answer is value-checked anyway."""
    parent = {'u': {}, 'v': {}}

    def find(ax, p):
        par = parent[ax]
        while par.get(p, p) != p:
            par[p] = par.get(par[p], par[p])
            p = par[p]
        return p

    def union(ax, p, q):
        if p not in pts or q not in pts:
            return False
        a, b = find(ax, p), find(ax, q)
        if a == b:
            return False
        parent[ax][a] = b
        parent[ax].setdefault(b, b)
        return True

    held, mid_of = {}, {}
    for c in cons:
        kind, ents = c['kind'], c['ents']
        if kind in ('horizontal', 'vertical') and len(ents) == 1 and ents[0] in lines:
            held[ents[0]] = 'h' if kind == 'horizontal' else 'v'
        elif kind in ('hpoints', 'vpoints') and len(ents) == 2:
            union('v' if kind == 'hpoints' else 'u', ents[0], ents[1])
        elif kind == 'midpoint' and len(ents) == 2:
            p, ln = (ents[0], ents[1]) if ents[0] in pts else (ents[1], ents[0])
            if ln in lines and p in pts:
                mid_of[ln] = p
    rel = [(c['kind'], c['ents'][0], c['ents'][1]) for c in cons
           if c['kind'] in ('parallel', 'perpendicular', 'collinear') and len(c['ents']) == 2
           and c['ents'][0] in lines and c['ents'][1] in lines]
    level = {'h': 'v', 'v': 'u'}            # the coordinate a held line keeps
    held_mids = [ln for ln in mid_of]

    for _round in range(50):
        changed = False
        for kind, l1, l2 in rel:
            for x, y in ((l1, l2), (l2, l1)):
                if x in held and y not in held:
                    want = held[x] if kind != 'perpendicular' else ('v' if held[x] == 'h' else 'h')
                    if line_axis(y) == want:
                        held[y] = want
                        changed = True
        for lid, ln in lines.items():
            if lid not in held:
                for o, ax in (('v', 'u'), ('h', 'v')):
                    if line_axis(lid) == o and find(ax, ln['a']) == find(ax, ln['b']):
                        held[lid] = o
                        changed = True
                        break
        for lid, o in held.items():
            ax = level[o]
            a, b = lines[lid]['a'], lines[lid]['b']
            changed |= union(ax, a, b)
            if lid in mid_of:
                changed |= union(ax, a, mid_of[lid])
        for i, l1 in enumerate(held_mids):
            for l2 in held_mids[i + 1:]:
                o = held.get(l1)
                if o is None or held.get(l2) != o:
                    continue
                across = 'u' if o == 'h' else 'v'
                a1, b1 = lines[l1]['a'], lines[l1]['b']
                a2, b2 = lines[l2]['a'], lines[l2]['b']
                f = lambda p, q: find(across, p) == find(across, q)
                if (f(a1, a2) and f(b1, b2)) or (f(a1, b2) and f(b1, a2)):
                    changed |= union(across, mid_of[l1], mid_of[l2])
        if not changed:
            break

    out = {}
    for ax in ('u', 'v'):
        g = {}
        for p in parent[ax]:
            g.setdefault(find(ax, p), set()).add(p)
        out[ax] = [s for s in g.values() if len(s) > 1]
    return out


def _aligned_rule(known, lines, angles, setp):
    """The aligned-dimension rule: two points a known distance apart along a line whose
    angle is known. B = A + f * (cos θ, sin θ), θ the line's angle — plus pi when the
    line runs the other way from A to B. One opaque term per axis, whose text is the
    whole product, so the value check covers the trig too."""

    def rule(e1, e2, f, du, dv):
        lid = next((l for l, ln in lines.items()
                    if {ln['a'], ln['b']} == {e1, e2} and l in angles), None)
        if lid is None:
            return False
        ang = angles[lid]
        actual = math.atan2(dv, du)
        same = abs(((actual - ang.value + math.pi) % (2 * math.pi)) - math.pi) < 1e-4
        theta_text = ang.text if same else '%s + 180 deg' % ang.text
        fexpr = render(f)
        cu = Form.term('(%s) * cos(%s)' % (fexpr, theta_text))
        cv = Form.term('(%s) * sin(%s)' % (fexpr, theta_text))
        changed = False
        for ax, comp in (('u', cu), ('v', cv)):
            fa, fb = known[e1][ax], known[e2][ax]
            if fa is not None and fb is None:
                changed |= setp(e2, ax, fa + comp)
            elif fb is not None and fa is None:
                changed |= setp(e1, ax, fb - comp)
        return changed

    return rule
