"""
plane_guess.py — which datum line puts a plane where you clicked.

The plane box (fusion_value, mode 'sketch') takes one line: a datum word and how far in
from it ("Front (Gap + Thickness)"). Clicking a face in the viewport while the box is
open asks the opposite question: the face is at 23.4 along Y — which line says that?

Pure arithmetic, no adsk: the Dimension Value add-in reads the model (the pick, the
component's datums in its own frame, the length parameters and their values, what the
face was extruded from) and hands plain numbers in. That keeps every rule here testable
offline — plane_guess_test.py.

Three sources, in this order — a later one runs only when the earlier found nothing:

  PROVENANCE — the face is the end of an extrude whose sketch the plane box placed, so
  the line is that sketch's line plus the extrude distance. `extend` composes it; the
  caller has already checked it lands on the pick numerically.

  DERIVED — the face's own history (Section's pool: the body's extrudes, their sketches
  derived dimension by dimension, sketch and feature patterns, mirrors) says where it
  is as a section_derive Form. `from_form` writes that after each datum. This is the
  answer, not a guess: "Front (3 * Grid_Spacing + Rib_Thickness / 2)" for the third rib.

  SEARCH — only when the history could not be followed. For each datum on the axis,
  the offset still needed (`rest_for`) is matched against signed sums of at most
  MAX_TERMS length parameters. Meet in the middle: pairs are hashed by value, so three
  terms are single + pair and four are pair + pair, never an N^4 walk. `only` narrows
  the pool to the parameters the face's own features use (with small multiples and
  halves of them) — a sum of unrelated parameters that happens to land is a
  coincidence, and offering it is worse than offering nothing.

Ranking (`_key`): provenance, then derived; then the datum NEAREST the pick (a face near
the front is described from the front); then fewer terms; then parameters local to the
component; then fewer minus signs. The nearest datum's plain-number line always comes
last, so there is an answer even when nothing adds up.

Units are Fusion's internal centimetres throughout.

Load by path (like planes.py) — fusion_ui is on no add-in's import path.
"""

import os
import re
import time
import types

MAX_TERMS = 4
TOL = 1e-4          # a micron in cm — Bounds.TOL
LIMIT = 20
BUDGET_S = 0.4      # the whole search, per pick; it stops early rather than beachball

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, fname):
    path = os.path.join(_HERE, fname)
    m = types.ModuleType(name)
    m.__file__ = path
    with open(path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


planes = _load('planes', 'planes.py')


# ── Which axis a parameter is for ─────────────────────────────────────────────
# A plane on Y is never placed by a parameter that says it is for X. Both naming
# conventions can say so: the structured one (param_naming — Drawer_Width_Span_X,
# Shelf_Inset_Back) through its axes, the older situational one (naming —
# EdgeOffsetX, EdgeBackOffsetY) through its axis tag. A name neither reads, or one that
# states no axis, is general and stays in. Loaded lazily and failing OPEN: a vocabulary
# that won't load filters nothing rather than everything.
_axis_readers = None


def _readers():
    global _axis_readers
    if _axis_readers is None:
        pn = pv = nm = None
        try:
            pn = _load('param_naming', 'param_naming.py')
            pv = pn.load_vocabulary(os.path.join(_HERE, 'param_naming.json'))
        except Exception:
            pn = None
        try:
            nm = _load('naming', 'naming.py')
            nm.load_vocabulary()
        except Exception:
            nm = None
        _axis_readers = (pn, pv, nm)
    return _axis_readers


def param_axes(name):
    """The axes a parameter's name says it is for — [] when it says none."""
    pn, pv, nm = _readers()
    if pn is not None:
        try:
            parts = pn.parse(name, pv)
            if parts is not None:
                return list(parts.get('axes') or [])
        except Exception:
            pass
    if nm is not None:
        try:
            a = nm.analyse(name).axis
            return [a] if a else []
        except Exception:
            pass
    return []


def axis_friendly(p, axis):
    """Is parameter `p` allowed on `axis`? Its `axes` if given, else read off its name."""
    if not axis:
        return True
    axes = p['axes'] if 'axes' in p else param_axes(p['name'])
    return not axes or axis in axes


# ── Where a line puts the plane ───────────────────────────────────────────────
def rest_for(datum, target):
    """The offset typed after `datum`'s word that puts the plane at `target`.

    The inverse of planes.offset_expression: a face datum at coordinate c with sign s
    places `rest` at s * (|c| - rest), so rest = s * (c - target); a midline places it
    at rest itself."""
    if datum.get('kind') == 'midline':
        return target
    return datum['sign'] * (datum['coord'] - target)


def _num(v):
    return ('%.6f' % v).rstrip('0').rstrip('.')


_SUM = re.compile(r'(?<=\S)\s*[-+]')


def _term_text(t):
    """A term as it reads inside an expression: a SUM is bracketed; a product or a
    quotient ("2 * Grid", "Rib / 2") binds tighter than the + it sits in, so it is not."""
    text = t['name'].strip()
    return '(%s)' % text if planes.needs_brackets(text) and _SUM.search(text) and not (
        text.startswith('(') and text.endswith(')')) else text


def rest_text(terms):
    """[(sign, term)] → "A + B - C", positive terms first so a leading minus is rare."""
    out = ''
    for sign, t in terms:
        word = _term_text(t)
        if not out:
            out = word if sign > 0 else '-' + word
        else:
            out += (' + ' if sign > 0 else ' - ') + word
    return out


def line(word, rest):
    """The whole line, bracketed exactly as the box would bracket it if typed."""
    inner = planes.bracket(rest)
    return word if not inner else '%s %s' % (word, inner)


def _strip_brackets(text):
    t = (text or '').strip()
    if t.startswith('(') and t.endswith(')') and planes._balanced(t[1:-1]):
        return t[1:-1].strip()
    return t


def extend(word, rest, rest_value, extra, extra_value, want, tol=TOL):
    """`word rest` moved on by `extra` (a distance, always positive) so it reads `want`.

    Tries + then -; None if neither lands. `rest` may be '' (the sketch sat right on
    its datum), in which case the line is just the extra."""
    for sign in (+1, -1):
        if abs(rest_value + sign * extra_value - want) > tol:
            continue
        ex = _term_text({'name': extra})
        base = _strip_brackets(rest)
        if not base:
            text = ex if sign > 0 else '-' + ex
        else:
            text = '%s %s %s' % (base, '+' if sign > 0 else '-', ex)
        return line(word, text)
    return None


# ── From the face's own history ──────────────────────────────────────────────
def from_form(form, datums, target, render, value_of, note='', tol=TOL):
    """[{text, note}] — `form` (where the face's own features put it: a section_derive
    Form in the component frame, the caller's) written after each datum whose own Form
    is known (`d['form']`; a midline is 0). rest = sign * (datum - form), so the
    datum's parameter cancels when the face was built from it. Fewest terms first, then
    the nearest datum. Every line is checked to land on `target` (`value_of`)."""
    if form is None or getattr(form, 'weak', False):
        return []
    rows = []
    for d in datums:
        if d.get('kind') == 'midline':
            rest = form
        else:
            df = d.get('form')
            if df is None or df.weak:
                continue
            rest = (df - form).scale(d['sign'])
        if rest.k and abs(rest.k) <= tol:          # cancelled but for float noise
            rest = rest + type(rest).const(-rest.k)
        want = rest_for(d, target)
        v = value_of(rest)
        if v is None or abs(v - want) > tol:
            continue
        n = len(rest.t) + (1 if rest.k else 0)
        text = d['word'] if n == 0 else line(d['word'], render(rest))
        rows.append(((n, abs(want)), text))
    rows.sort(key=lambda r: r[0])
    return [{'text': t, 'note': note or 'from the face’s own sketch'} for _k, t in rows]


# ── The search ───────────────────────────────────────────────────────────────
MULTIPLES = (2, 3, 4, 5, 6, 7, 8)


def _narrow(params, only):
    """The search pool held to `only` (names the face's own features use), plus small
    whole multiples and the half of each — the third rib is 3 * spacing, its side is
    thickness / 2. A hint term (rank -1, the pick's own extrude distance) always stays."""
    out = []
    for p in params:
        if p.get('rank', 2) < 0:
            out.append(p)
            continue
        if p['name'] not in only:
            continue
        out.append(p)
        for k in MULTIPLES:
            out.append(dict(p, name='%d * %s' % (k, p['name']), value=k * p['value'],
                            base=p['name']))
        out.append(dict(p, name='%s / 2' % p['name'], value=p['value'] / 2.0,
                        base=p['name']))
    return out


def _pool(params, tol):
    """The parameters worth combining: non-zero, and at most three names per value —
    ten parameters that are all 18 mm would otherwise make every sum ten ways."""
    by_name = {}
    for p in params:                # one entry per name, the most local kept (a hint
        q = by_name.get(p['name'])  # term can repeat a parameter the list has too)
        if q is None or p.get('rank', 2) < q.get('rank', 2):
            by_name[p['name']] = p
    by_val = {}
    for p in by_name.values():
        v = p.get('value')
        if v is None or abs(v) <= tol:
            continue
        by_val.setdefault(round(abs(v) / tol), []).append(p)
    out = []
    for group in by_val.values():
        group.sort(key=lambda p: (p.get('rank', 2), p['name']))
        out.extend(group[:3])
    return out


def _bucket(v, tol):
    return int(round(v / tol))


def _near(table, v, tol):
    k = _bucket(v, tol)
    for kk in (k - 1, k, k + 1):
        for hit in table.get(kk, ()):
            yield hit


# The pair table is most of the cost (N² entries) and depends only on the parameters,
# which do not change between two clicks in one box — so the last complete one is kept.
_pair_cache = {'key': None, 'pairs': None}


def _pairs(pool, tol, deadline):
    key = (tol, tuple((p['name'], p['value']) for p in pool))
    if _pair_cache['key'] == key:
        return _pair_cache['pairs']
    pairs = {}
    n = len(pool)
    complete = True
    for i in range(n):
        vi = pool[i]['value']
        for j in range(i + 1, n):
            vj = pool[j]['value']
            for si in (+1, -1):
                for sj in (+1, -1):
                    v = si * vi + sj * vj
                    if abs(v) <= tol:
                        continue            # A - A: nothing, in two terms
                    pairs.setdefault(_bucket(v, tol), []).append((v, ((si, i), (sj, j))))
        if time.time() > deadline:
            complete = False
            break
    if complete:
        _pair_cache['key'], _pair_cache['pairs'] = key, pairs
    return pairs


def _combos(pool, r, max_terms, limit, tol, deadline):
    """Signed index tuples ((sign, i), …) summing to r, fewest terms first."""
    found = []
    seen = set()

    def add(terms):
        key = tuple(sorted(terms))
        # one entry per PARAMETER: "2 * Spacing + Spacing" is not a different answer
        idx = [pool[i].get('base', pool[i]['name']) for _s, i in key]
        if len(set(idx)) != len(idx) or key in seen:
            return
        seen.add(key)
        found.append(key)

    singles = {}
    for i, p in enumerate(pool):
        for s in (+1, -1):
            singles.setdefault(_bucket(s * p['value'], tol), []).append(((s, i),))
    for hit in _near(singles, r, tol):
        add(hit)
    if max_terms < 2 or len(found) >= limit:
        return found

    pairs = _pairs(pool, tol, deadline)
    for _v, hit in _near(pairs, r, tol):
        add(hit)
    if max_terms < 3 or len(found) >= limit:
        return found

    for i, p in enumerate(pool):
        for s in (+1, -1):
            for _v, hit in _near(pairs, r - s * p['value'], tol):
                add(((s, i),) + hit)
        if time.time() > deadline or len(found) >= limit * 4:
            break
    if max_terms < 4 or len(found) >= limit:
        return found

    for bucket in list(pairs.values()):
        for v, hit in bucket:
            for _v2, hit2 in _near(pairs, r - v, tol):
                add(hit + hit2)
        if time.time() > deadline or len(found) >= limit * 4:
            break
    return found


def _expressions(pool, r, max_terms, limit, tol, deadline):
    """[((terms, locality, minus), "A + B")] summing to `r` — the ranking key after the
    source, fewest terms first, then the most local, then the fewest minus signs."""
    out = []
    for combo in _combos(pool, r, max_terms, limit, tol, deadline):
        terms = sorted(((s, pool[i]) for s, i in combo),
                       key=lambda st: (-st[0], st[1].get('rank', 2), st[1]['name']))
        locality = sum(t.get('rank', 2) for _s, t in terms)
        minus = sum(1 for s, _t in terms if s < 0)
        out.append(((len(terms), locality, minus), rest_text(terms)))
    return out


def suggest(target, datums, params, provenance=(), fmt=None,
            max_terms=MAX_TERMS, limit=LIMIT, tol=TOL, budget=BUDGET_S, axis=None,
            derived=(), only=None):
    """Ranked [{text, note}] for a plane at `target`.

    datums      [{word, kind: 'face'|'midline', coord, sign}] — one axis only
    params      [{name, value, rank}] — length parameters; `name` may be a composite
                expression (an extrude's distance), bracketed when it is used. rank:
                lower is more local (-1 the pick's own extrude, 0 this component, 1 an
                ancestor, 2 anything else). Optional `axes` overrides reading them off
                the name — [] for a term known to be on this axis (an extrude distance)
    provenance  [{text, note}] already built by `extend` — go first as they are
    fmt         internal cm → the number as the user types it (the add-in hands in the
                units manager); '%g' of mm otherwise
    axis        the axis being placed on; defaults to the datums'. A parameter whose
                name says it is for a DIFFERENT axis is never used (`axis_friendly`)
    derived     [{text, note}] from `from_form` — the face's own history. With these
                (or provenance) there is no search: the answer is known
    only        names the face's own features use, when its history was readable but
                did not land: the search uses just those (`_narrow`). None = unknown
                history, the whole pool
    """
    if fmt is None:
        fmt = lambda v: _num(v * 10.0)
    deadline = time.time() + budget
    max_terms = max(0, min(int(max_terms), MAX_TERMS))
    if axis is None:
        axis = next((d.get('axis') for d in datums if d.get('axis')), '')
    if provenance or derived:
        max_terms = 0
    if only is not None:
        params = _narrow(params, only)

    ranked = sorted(datums, key=lambda d: abs(rest_for(d, target)))
    pool = _pool([p for p in params if axis_friendly(p, axis)], tol)
    rows = []           # (key, text, note)
    texts = set()

    def push(key, text, note):
        if text and text not in texts:
            texts.add(text)
            rows.append((key, text, note))

    for n, p in enumerate(provenance):
        push((0, 0, n), p['text'], p.get('note', ''))
    for n, p in enumerate(derived):
        push((0, 1, n), p['text'], p.get('note', ''))

    for rank, d in enumerate(ranked):
        r = rest_for(d, target)
        if abs(r) <= tol:
            push((1, rank, 0, 0, 0), d['word'], 'on %s' % d['word'])
            continue
        if max_terms == 0 or time.time() > deadline:
            continue
        for key, text in _expressions(pool, r, max_terms, limit, tol, deadline):
            push((1, rank) + key, line(d['word'], text), 'from %s' % d['word'])

    if ranked:
        d = ranked[0]
        r = rest_for(d, target)
        if abs(r) > tol:
            push((2,), '%s %s%s' % (d['word'], '-' if r < 0 else '', fmt(abs(r))),
                 'as a number')

    rows.sort(key=lambda row: row[0])
    return [{'text': t, 'note': note} for _k, t, note in rows[:limit]]


# ── A distance, for the dimension box ────────────────────────────────────────
def suggest_distance(distance, params, provenance=(), fmt=None,
                     max_terms=MAX_TERMS, limit=LIMIT, tol=TOL, budget=BUDGET_S, axis=''):
    """Ranked [{text, note}] expressions worth `distance` — a sketch dimension's value,
    read off a viewport pick (the Dimension Value add-in's _watch_dim_pick).

    The plane search with no datum word in front: the add-in has already decided what
    the dimension is measured FROM and hands in how far the pick is from it.

    distance    > 0, internal cm
    params      as for `suggest`; `axis` keeps out parameters named for another axis
    provenance  [{text, note}] expressions the pick's own history says — go first
    """
    if fmt is None:
        fmt = lambda v: _num(v * 10.0)
    deadline = time.time() + budget
    max_terms = max(0, min(int(max_terms), MAX_TERMS))
    rows, texts = [], set()

    def push(key, text, note):
        if text and text not in texts:
            texts.add(text)
            rows.append((key, text, note))

    for n, p in enumerate(provenance):
        push((0, n), p['text'], p.get('note', ''))
    if distance > tol and max_terms:
        pool = _pool([p for p in params if axis_friendly(p, axis)], tol)
        for key, text in _expressions(pool, distance, max_terms, limit, tol, deadline):
            push((1,) + key, text, '')
    if distance > tol:
        push((2,), fmt(distance), 'as a number')
    rows.sort(key=lambda row: row[0])
    return [{'text': t, 'note': note} for _k, t, note in rows[:limit]]
