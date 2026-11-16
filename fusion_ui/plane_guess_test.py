#!/usr/bin/env python3
"""
Cases for plane_guess.py.

    python3 fusion_ui/plane_guess_test.py

Beyond the expected rankings, EVERY line suggested in every case is evaluated back
through planes.offset_expression — the exact path the add-in takes to build the plane —
and must land on the target. A suggestion that reads well and puts the plane somewhere
else is the one bug this module must never have.
"""

import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import planes        # noqa: E402
import plane_guess   # noqa: E402

MM = 0.1   # internal units are cm

# A 600 x 400 component, Y asymmetric (Front at -200, Back at +200 → symmetric in value
# but offered as two faces), X symmetric (X midline + Width face at +300).
FRONT = {'word': 'Front', 'kind': 'face', 'coord': -200 * MM, 'sign': -1,
         'face': 'Front_P', 'axis': 'Y'}
BACK = {'word': 'Back', 'kind': 'face', 'coord': 200 * MM, 'sign': +1,
        'face': 'Back_P', 'axis': 'Y'}
XMID = {'word': 'X', 'kind': 'midline', 'coord': 0.0, 'sign': +1, 'face': '', 'axis': 'X'}
WIDTH = {'word': 'Width', 'kind': 'face', 'coord': 300 * MM, 'sign': +1,
         'face': 'Width_P', 'axis': 'X'}

PARAMS = [
    {'name': 'Gap', 'value': 18 * MM, 'rank': 0},
    {'name': 'Thickness', 'value': 25 * MM, 'rank': 0},
    {'name': 'Rail', 'value': 40 * MM, 'rank': 1},
    {'name': 'Lip', 'value': 3 * MM, 'rank': 2},
    {'name': 'Big', 'value': 1000 * MM, 'rank': 2},
]
VALUES = {p['name']: p['value'] for p in PARAMS}
VALUES.update({'Front_P': 200 * MM, 'Back_P': 200 * MM, 'Width_P': 300 * MM})


def evaluate(text, datums, values):
    """Where the line `text` actually puts the plane, via planes.offset_expression."""
    word, rest = planes.split_input(text)
    d = next(x for x in datums if x['word'] == word)
    expr = planes.offset_expression(d['kind'], d['face'], rest, d['sign'])
    # numbers are typed in mm; parameters are already in cm
    # (a factor after * or / is unitless, as Fusion reads it)
    expr = re.sub(r'(?<![A-Za-z_\d.])(\d+(?:\.\d+)?)(?![A-Za-z_\d.])',
                  lambda m: m.group(1) if re.search(r'[*/]\s*$', m.string[:m.start()])
                  or re.match(r'\s*\*', m.string[m.end():])
                  else repr(float(m.group(1)) * MM), expr)
    return eval(expr, {}, dict(values))


fails = []


def check(label, target, datums, params=PARAMS, want_first=None, want_in=(),
          want_not=(), **kw):
    got = plane_guess.suggest(target, datums, params, **kw)
    texts = [g['text'] for g in got]
    if want_first is not None and (not texts or texts[0] != want_first):
        fails.append('%s: first %r, want %r (all: %r)' % (label, texts[:1], want_first,
                                                            texts))
    for w in want_in:
        if w not in texts:
            fails.append('%s: %r missing from %r' % (label, w, texts))
    for w in want_not:
        if w in texts:
            fails.append('%s: %r should not be offered' % (label, w))
    values = dict(VALUES)
    values.update({p['name']: p['value'] for p in params})
    for t in texts:
        try:
            at = evaluate(t, datums, values)
        except Exception as e:
            fails.append('%s: %r does not evaluate: %s' % (label, t, e))
            continue
        if abs(at - target) > 1e-6:
            fails.append('%s: %r lands at %g, not %g' % (label, t, at, target))
    return texts


Y = [FRONT, BACK]

# On the face itself: the bare word.
check('on front', -200 * MM, Y, want_first='Front')

# One parameter in from the nearer face.
check('gap in from front', -182 * MM, Y, want_first='Front Gap')
check('gap in from back', 182 * MM, Y, want_first='Back Gap')

# Two terms; the nearer face (front) outranks the farther one even with fewer terms there.
check('gap+thickness from front', -157 * MM, Y, want_first='Front (Gap + Thickness)')

# Out beyond the face: a negative rest.
check('outside front', -218 * MM, Y, want_first='Front -Gap')

# Subtraction.
check('rail less lip', -163 * MM, Y, want_in=['Front (Rail - Lip)'])

# Three and four terms.
check('three terms', -117 * MM, Y, want_in=['Front (Gap + Thickness + Rail)'])
check('four terms', -114 * MM, Y, want_in=['Front (Gap + Thickness + Rail + Lip)'])

# Never more than four — 18+25+40+3+1000 needs five.
five = check('five never', -200 * MM + 1086 * MM, Y)
if any(t.count('+') + t.count(' - ') >= 4 for t in five):
    fails.append('five never: a 5-term line was offered: %r' % five)

# max_terms respected.
two = check('max 2', -114 * MM, Y, max_terms=2)
if any('Gap + Thickness + Rail' in t for t in two):
    fails.append('max 2: offered a 3-term line: %r' % two)

# Midline: rest is the coordinate itself.
check('x midline', 18 * MM, [XMID, WIDTH], want_first='X Gap')
check('x midline neg', -25 * MM, [XMID, WIDTH], want_first='X -Thickness')

# Nothing adds up → a number from the nearest face, always last.
t = check('number fallback', -200 * MM + 7.5 * MM, Y, params=[])
if t != ['Front 7.5']:
    fails.append('number fallback: %r' % t)

# Locality: two parameters with the same value → the local one first.
same = [{'name': 'Far', 'value': 18 * MM, 'rank': 2},
        {'name': 'Near', 'value': 18 * MM, 'rank': 0}]
check('local first', -182 * MM, Y, params=same, want_first='Front Near')

# Axis-friendly only: a parameter named for another axis never places a plane on Y —
# in either naming convention — while one for Y, several axes, or none, still does.
axed = [{'name': 'Drawer_Width_Span_X', 'value': 18 * MM, 'rank': 0},
        {'name': 'EdgeOffsetX', 'value': 18 * MM, 'rank': 0},
        {'name': 'EdgeBackOffsetY', 'value': 25 * MM, 'rank': 0},
        {'name': 'Drawer_Width_Span_X_Y', 'value': 40 * MM, 'rank': 0},
        {'name': 'Plain', 'value': 3 * MM, 'rank': 2}]
check('other axis out', -182 * MM, Y, params=axed,
      want_not=['Front Drawer_Width_Span_X', 'Front EdgeOffsetX'])
check('own axis in', -175 * MM, Y, params=axed, want_first='Front EdgeBackOffsetY')
check('multi axis in', -160 * MM, Y, params=axed, want_first='Front Drawer_Width_Span_X_Y')
check('general in', -197 * MM, Y, params=axed, want_first='Front Plain')
# …the same X parameter is fine on X, and an explicit `axes: []` (an extrude
# distance) overrides its name.
check('x on x', 18 * MM, [XMID, WIDTH], params=axed,
      want_in=['X EdgeOffsetX'])
check('axes override', -182 * MM, Y, want_first='Front EdgeOffsetX',
      params=[{'name': 'EdgeOffsetX', 'value': 18 * MM, 'rank': -1, 'axes': []}])

# A composite term (an extrude's distance) is bracketed.
comp = [{'name': 'Thickness * 2', 'value': 50 * MM, 'rank': -1}]
check('composite', -150 * MM, Y, params=comp, want_first='Front (Thickness * 2)')

# Provenance goes first, untouched.
prov = [{'text': 'Front (Gap + Thickness)', 'note': 'Extrude1 end'}]
t = check('provenance first', -157 * MM, Y, provenance=prov)
if not t or t[0] != 'Front (Gap + Thickness)' or t.count(t[0]) != 1:
    fails.append('provenance first: %r' % t)

# extend: sketch at Front 18 (Gap), extruded Thickness inward → Front (Gap + Thickness).
e = plane_guess.extend('Front', 'Gap', 18 * MM, 'Thickness', 25 * MM, 43 * MM)
if e != 'Front (Gap + Thickness)':
    fails.append('extend inward: %r' % e)
e = plane_guess.extend('Front', '(Rail - Lip)', 37 * MM, 'Gap', 18 * MM, 19 * MM)
if e != 'Front (Rail - Lip - Gap)':
    fails.append('extend outward: %r' % e)
e = plane_guess.extend('Front', '', 0.0, 'Thickness * 2', 50 * MM, 50 * MM)
if e != 'Front (Thickness * 2)':
    fails.append('extend from bare: %r' % e)
if plane_guess.extend('Front', 'Gap', 18 * MM, 'Thickness', 25 * MM, 99 * MM) is not None:
    fails.append('extend: a miss should be None')

# suggest_distance — the dimension box. Every text must EVALUATE to the distance.
def check_distance(label, distance, params=PARAMS, want_first=None, want_not=(), **kw):
    got = plane_guess.suggest_distance(distance, params, **kw)
    texts = [g['text'] for g in got]
    if want_first is not None and (not texts or texts[0] != want_first):
        fails.append('%s: first %r, want %r (all: %r)' % (label, texts[:1], want_first,
                                                            texts))
    for w in want_not:
        if w in texts:
            fails.append('%s: %r should not be offered' % (label, w))
    values = dict(VALUES)
    values.update({p['name']: p['value'] for p in params})
    for g in got:
        if g.get('note'):         # the number, or provenance — not a sum of these
            continue
        expr = re.sub(r'[A-Za-z_]\w*', lambda m: repr(values[m.group(0)]), g['text'])
        if abs(eval(expr) - distance) > 1e-6:
            fails.append('%s: %r is %r, not %r' % (label, g['text'], eval(expr), distance))


check_distance('one parameter', 25 * MM, want_first='Thickness')
check_distance('a sum', 43 * MM, want_first='Gap + Thickness')
check_distance('local before remote', 40 * MM, want_first='Rail')
check_distance('nothing adds up → the number', 12.345 * MM, want_first='12.345')
check_distance('provenance first', 43 * MM, want_first='Depth18',
               provenance=[{'text': 'Depth18', 'note': 'end of Extrude3'}])
check_distance('axis filter', 25 * MM, axis='X', want_not=('ThicknessY',),
               params=PARAMS + [{'name': 'ThicknessY', 'value': 25 * MM, 'rank': -1,
                                 'axes': ['Y']}])
if plane_guess.suggest_distance(0.0, PARAMS):
    fails.append('zero distance: should suggest nothing')

# ── The face's own history ──
# Ribs from Front: the third rib's far side is Front + 3 * Spacing + Rib / 2. A prong
# radius that happens to equal that distance must never be offered in its place.
import types   # noqa: E402
_sdp = os.path.join(HERE, 'section_derive.py')
sd = types.ModuleType('section_derive')
with open(_sdp) as _f:
    exec(compile(_f.read(), _sdp, 'exec'), sd.__dict__)
RIBP = sd.Params(user={'Front_P': (200 * MM, 'L'), 'Back_P': (200 * MM, 'L'),
                       'Spacing': (50 * MM, 'L'), 'Rib': (6 * MM, 'L'),
                       'Prong_Radius': (153 * MM, 'L')})
RIBS = [dict(FRONT, form=sd.Form.term('Front_P', -1.0)),
        dict(BACK, form=sd.Form.term('Back_P', 1.0))]
rib_form = sd.form_of('-Front_P + 3 * Spacing + Rib / 2', RIBP)
rib_at = sd.form_value(rib_form, RIBP)
derived = plane_guess.from_form(rib_form, RIBS, rib_at, sd.render,
                                lambda f: sd.form_value(f, RIBP), note='Ribs')
RIB_PARAMS = [{'name': n, 'value': v, 'rank': 0} for n, (v, _k) in RIBP.user.items()
              if not n.endswith('_P')]
for p in RIB_PARAMS:
    VALUES[p['name']] = p['value']
check('rib: derived first', rib_at, Y, params=RIB_PARAMS, derived=derived,
      want_first='Front (Rib / 2 + 3 * Spacing)', want_not=('Front Prong_Radius',))
check('rib: no history → open search finds the coincidence', rib_at, Y,
      params=RIB_PARAMS, want_first='Front Prong_Radius')
check('rib: narrowed search uses the history and multiples', rib_at, Y,
      params=RIB_PARAMS, only={'Spacing', 'Rib'},
      want_first='Front (3 * Spacing + Rib / 2)', want_not=('Front Prong_Radius',))
if plane_guess.from_form(sd.Form.const(1.0, True), RIBS, 1.0, sd.render,
                         lambda f: sd.form_value(f, RIBP)):
    fails.append('from_form: a weak form must give nothing')

# Speed: a few hundred parameters, four-term search, inside the budget.
many = [{'name': 'P%d' % i, 'value': (7 + i * 1.37) * MM, 'rank': 2} for i in range(300)]
t0 = time.time()
plane_guess.suggest(-200 * MM + 12345.678 * MM, Y, many)
dt = time.time() - t0
if dt > 1.0:
    fails.append('speed: %.2fs for 300 parameters' % dt)

if fails:
    print('FAIL (%d)' % len(fails))
    for f in fails:
        print('  ' + f)
    sys.exit(1)
print('ok — plane_guess (search %.2fs on 300 params)' % dt)
