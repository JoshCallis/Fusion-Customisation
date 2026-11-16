"""Offline test for section_derive.py — /usr/bin/python3 section_derive_test.py"""

import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(HERE, 'section_derive.py')
sd = types.ModuleType('section_derive')
sd.__file__ = path
with open(path) as f:
    exec(compile(f.read(), path, 'exec'), sd.__dict__)

fails = []


def check(name, got, want):
    if got != want:
        fails.append('%s: got %r want %r' % (name, got, want))


def close(name, got, want, tol=1e-6):
    if got is None or abs(got - want) > tol:
        fails.append('%s: got %r want %r' % (name, got, want))


P = sd.Params(user={'Right': (72.0, 'L'), 'Thick': (1.8, 'L'), 'Veneer': (0.065, 'L'),
                    'Steel': (0.05, 'L'), 'N': (3.0, 'S'), 'Tilt': (math.radians(30), 'A')},
              model={'d1': ('Right - 15 mm', 'mm'), 'd2': ('d1 * 2', 'mm'),
                     'd3': ('45 deg', 'deg')})

# ── parsing, evaluation, forms ──
close('eval sum', sd.evaluate_text('Right - 15 mm', P), 70.5)
close('eval bare number is mm', sd.evaluate_text('Right - 15', P), 70.5)
close('eval trig', sd.evaluate_text('20 cm * cos(60 deg)', P), 10.0)
close('eval trig bare angle', sd.evaluate_text('2 * cos(60) * 10 mm', P), 1.0)
close('eval model inline', sd.evaluate_text('d2', P), 141.0)
close('eval unitless param', sd.evaluate_text('N * Thick', P), 5.4)
close('eval angle param', sd.evaluate_text('Thick * sin(Tilt)', P), 0.9)

f = sd.form_of('Right - 15 mm', P)
check('form terms', f.t, {'Right': 1.0})
close('form const', f.k, -1.5)
check('render', sd.render(f), 'Right - 15 mm')
f2 = sd.form_of('d2', P)
check('model inlined', f2.t, {'Right': 2.0})
close('model inlined const', f2.k, -3.0)
check('render coef', sd.render(f2), '2 * Right - 30 mm')
f3 = sd.form_of('Veneer + Steel', P)
check('render sum', sd.render(f3), 'Steel + Veneer')
f4 = sd.form_of('N * Thick', P)
check('nonlinear is opaque', list(f4.t), ['N * Thick'])
close('opaque value', sd.form_value(f4, P), 5.4)
check('render opaque', sd.render(f4 - sd.Form.term('Right')), '(N * Thick) - Right')
check('neg render', sd.render(-sd.form_of('Right', P)), '-Right')
check('zero render', sd.render(sd.Form()), '0 mm')
d = sd.form_of('Right', P) - sd.form_of('Right - 15 mm', P)
check('cancel', (d.t, round(d.k, 9)), ({}, 1.5))
check('json round trip', sd.render(sd.Form.from_json(f.to_json())), 'Right - 15 mm')

# ── a real sketch: Drawer Ceiling / Profile - Y - Front, as read live ──
pts = {
    'P0': (0.0, 0.0), 'P1': (0.0, -2.0), 'P2': (0.0, -0.25), 'P3': (76.358, 13.892),
    'P4': (56.358, 13.892), 'P5': (70.5, -0.25), 'P6': (76.358, -0.25),
    'P7': (76.358, -0.135), 'P8': (70.385, -0.135),
}
sketch = {
    'points': {k: {'u': u, 'v': v, 'anchor': {}} for k, (u, v) in pts.items()},
    'lines': {'L12': {'a': 'P1', 'b': 'P2'}, 'L43': {'a': 'P4', 'b': 'P3'},
              'L36': {'a': 'P3', 'b': 'P6'}, 'L54': {'a': 'P5', 'b': 'P4'},
              'L65': {'a': 'P6', 'b': 'P5'}, 'L78': {'a': 'P7', 'b': 'P8'}},
    'dims': [
        {'kind': 'aligned', 'ents': ['P5', 'P4'], 'expr': '200.00 mm'},
        {'kind': 'h', 'ents': ['P2', 'P5'], 'expr': 'Right - 15 mm'},
        {'kind': 'angle', 'ents': ['L43', 'L54'], 'expr': '45.00 deg'},
        {'kind': 'aligned', 'ents': ['P4', 'P3'], 'expr': '200.00 mm'},
        {'kind': 'offset', 'ents': ['L65', 'L78'], 'expr': 'Veneer + Steel'},
    ],
    'cons': [
        {'kind': 'vertical', 'ents': ['L36']},
        {'kind': 'hpoints', 'ents': ['P5', 'P2']},
        {'kind': 'horizontal', 'ents': ['L43']},
        {'kind': 'horizontal', 'ents': ['L65']},
        {'kind': 'coincident', 'ents': ['P7', 'L36']},
        {'kind': 'coincident', 'ents': ['P8', 'L54']},
        {'kind': 'perpendicular', 'ents': ['L78', 'L36']},
    ],
}
# the origin, and the two fixed points (weak literals)
sketch['points']['P0']['anchor'] = {'u': sd.Form.const(0), 'v': sd.Form.const(0)}
for k in ('P1', 'P2'):
    u, v = pts[k]
    sketch['points'][k]['anchor'] = {'u': sd.Form.const(u, True), 'v': sd.Form.const(v, True)}

out = sd.derive(sketch, P, tol=2e-3)
kp = out['points']
check('P5.u', sd.render(kp['P5']['u']), 'Right - 15 mm')
check('P5.v weak', kp['P5']['v'].weak, True)
check('P6.v from horizontal', sd.render(kp['P6']['v']), '-2.5 mm')
check('P4 u found by angle', kp['P4']['u'] is not None, True)
close('P4.u value', sd.form_value(kp['P4']['u'], P), 56.358, 2e-3)
check('P4.u mentions Right', 'Right' in kp['P4']['u'].t, True)
check('P3.u found', kp['P3']['u'] is not None, True)
close('P3.u value', sd.form_value(kp['P3']['u'], P), 76.358, 2e-3)
check('P7.u from vertical line', kp['P7']['u'] is not None, True)
check('P7.v from offset', sd.render(kp['P7']['v']), 'Steel + Veneer - 2.5 mm')
check('L54 angle known', out['lines']['L54']['angle'] is not None, True)

# a wrong sign can never be accepted: a dimension the geometry contradicts is skipped
bad = {'points': {'A': {'u': 0.0, 'v': 0.0, 'anchor': {'u': sd.Form.const(0), 'v': sd.Form.const(0)}},
                  'B': {'u': 5.0, 'v': 0.0, 'anchor': {}}},
       'lines': {}, 'dims': [{'kind': 'h', 'ents': ['A', 'B'], 'expr': '40 mm'}], 'cons': []}
check('contradicted dim rejected', sd.derive(bad, P)['points']['B']['u'], None)

# midpoint, symmetry, tangent + radius
sk2 = {
    'points': {'O': {'u': 0.0, 'v': 0.0, 'anchor': {'u': sd.Form.const(0), 'v': sd.Form.const(0)}},
               'A': {'u': -3.0, 'v': 2.0, 'anchor': {}}, 'B': {'u': 3.0, 'v': 2.0, 'anchor': {}},
               'M': {'u': 0.0, 'v': 2.0, 'anchor': {}}, 'C': {'u': 10.0, 'v': 3.8, 'anchor': {}},
               'Y1': {'u': 0.0, 'v': -5.0, 'anchor': {'u': sd.Form.const(0), 'v': sd.Form.const(-5)}},
               'Y2': {'u': 0.0, 'v': 5.0, 'anchor': {'u': sd.Form.const(0), 'v': sd.Form.const(5)}}},
    'lines': {'AB': {'a': 'A', 'b': 'B'}, 'AX': {'a': 'Y1', 'b': 'Y2'}},
    'circles': {'K': {'c': 'C', 'r': 1.8}},
    'dims': [{'kind': 'v', 'ents': ['O', 'A'], 'expr': '20 mm'},
             {'kind': 'h', 'ents': ['A', 'B'], 'expr': 'Right - 660 mm'},
             {'kind': 'radius', 'ents': ['K'], 'expr': 'Thick'},
             {'kind': 'h', 'ents': ['O', 'C'], 'expr': '100 mm'}],
    'cons': [{'kind': 'horizontal', 'ents': ['AB']}, {'kind': 'midpoint', 'ents': ['M', 'AB']},
             {'kind': 'symmetry', 'ents': ['A', 'B', 'AX']}, {'kind': 'tangent', 'ents': ['K', 'AB']}],
}
o2 = sd.derive(sk2, P)['points']
check('B.u by symmetry or dim', o2['B']['u'] is not None, True)
close('A.u value', sd.form_value(o2['A']['u'], P), -3.0)
check('M.v midpoint', sd.render(o2['M']['v']), '20 mm')
check('C.v tangent', sd.render(o2['C']['v']), 'Thick + 20 mm')

# offset dimension line <-> point (addOffsetDimension(line, point)) — 260612's Powerboard
# profile is anchored only by these: a horizontal line to the origin, a vertical to a bound.
P3 = sd.Params(user={'Inset': (6.31, 'L'), 'Back': (36.0, 'L'), 'Gap': (16.94, 'L'),
                     'SpanZ': (4.75, 'L')})
sk3 = {
    'points': {'O': {'u': 0.0, 'v': 0.0, 'anchor': {'u': sd.Form.const(0), 'v': sd.Form.const(0)}},
               'B': {'u': 36.0, 'v': 0.0, 'anchor': {'u': sd.Form.term('Back'), 'v': sd.Form.const(0)}},
               'T': {'u': 19.06, 'v': -3.935, 'anchor': {}}, 'D': {'u': 19.06, 'v': -8.685, 'anchor': {}},
               'M': {'u': 19.06, 'v': -6.31, 'anchor': {}}, 'H': {'u': 17.36, 'v': -6.31, 'anchor': {}}},
    'lines': {'TD': {'a': 'D', 'b': 'T'}, 'HM': {'a': 'H', 'b': 'M'}},
    'circles': {},
    'dims': [{'kind': 'offset', 'ents': ['HM', 'O'], 'expr': 'Inset'},
             {'kind': 'offset', 'ents': ['TD', 'B'], 'expr': 'Gap'},
             {'kind': 'aligned', 'ents': ['D', 'T'], 'expr': 'SpanZ'}],
    'cons': [{'kind': 'vertical', 'ents': ['TD']}, {'kind': 'horizontal', 'ents': ['HM']},
             {'kind': 'midpoint', 'ents': ['M', 'TD']}],
}
o3 = sd.derive(sk3, P3)['points']
check('offset line-point v', sd.render(o3['H']['v']), '-Inset')
check('offset line-point u', sd.render(o3['T']['u']), 'Back - Gap')
check('offset line-point chain', o3['D']['v'] is not None and o3['T']['v'] is not None, True)

# ── a sketch rectangular pattern: ribs ──
# One rib's two sides (A at Off, B at Off + Rib) patterned 4 times along u at Spacing.
# Each copy is paired with BOTH originals (both are translates of it); only the one a
# whole number of spacings away may answer — B3 is Off + Rib + 3 * Spacing, never
# Off + something * Spacing that happens to land.
P4 = sd.Params(user={'Off': (2.0, 'L'), 'Rib': (0.6, 'L'), 'Spacing': (5.0, 'L')})
zero = sd.Form.const(0.0)
pts4 = {'O': {'u': 0.0, 'v': 0.0, 'anchor': {'u': zero, 'v': zero}},
        'A0': {'u': 2.0, 'v': 0.0, 'anchor': {'v': zero}},
        'B0': {'u': 2.6, 'v': 0.0, 'anchor': {'v': zero}}}
pairs4 = []
for k in (1, 2, 3):
    for side, base in (('A', 2.0), ('B', 2.6)):
        pts4['%s%d' % (side, k)] = {'u': base + 5.0 * k, 'v': 0.0, 'anchor': {'v': zero}}
        pairs4 += [('%s%d' % (side, k), 'A0'), ('%s%d' % (side, k), 'B0')]
sk4 = {'points': pts4, 'lines': {}, 'circles': {}, 'cons': [],
       'dims': [{'kind': 'h', 'ents': ['O', 'A0'], 'expr': 'Off'},
                {'kind': 'h', 'ents': ['A0', 'B0'], 'expr': 'Rib'}],
       'patterns': [{'pairs': pairs4,
                     'dirs': [{'u': 1.0, 'v': 0.0, 'form': sd.form_of('Spacing', P4),
                               'value': 5.0, 'q': 4}]}]}
o4 = sd.derive(sk4, P4)['points']
check('pattern rib 3 far side', sd.render(o4['B3']['u']), 'Off + Rib + 3 * Spacing')
check('pattern rib 1 near side', sd.render(o4['A1']['u']), 'Off + Spacing')
check('pattern copy keeps v', sd.render(o4['B2']['v']), '0 mm')
# ── a slot: rectangle held by one vertical + perpendiculars, centred by midpoints ──
# Rib Slots on 260612: the slot's centre line joins the midpoints of its top and
# bottom (no vertical constraint of its own); its midpoint C is at Half; the span is
# measured along the bottom. The sides must come out Half -/+ Span / 2.
P5 = sd.Params(user={'Half': (2.5, 'L'), 'Span': (2.0, 'L'), 'T': (0.3, 'L')})
pts5 = {'O': {'u': 0.0, 'v': 0.0, 'anchor': {'u': zero, 'v': zero}},
        'C': {'u': 2.5, 'v': 0.0, 'anchor': {}},
        'BL': {'u': 1.5, 'v': -0.15, 'anchor': {}}, 'BR': {'u': 3.5, 'v': -0.15, 'anchor': {}},
        'TL': {'u': 1.5, 'v': 0.15, 'anchor': {}}, 'TR': {'u': 3.5, 'v': 0.15, 'anchor': {}},
        'MB': {'u': 2.5, 'v': -0.15, 'anchor': {}}, 'MT': {'u': 2.5, 'v': 0.15, 'anchor': {}}}
lines5 = {'L': {'a': 'BL', 'b': 'TL'}, 'R': {'a': 'BR', 'b': 'TR'},
          'B': {'a': 'BL', 'b': 'BR'}, 'T': {'a': 'TL', 'b': 'TR'}, 'M': {'a': 'MB', 'b': 'MT'}}
sk5 = {'points': pts5, 'lines': lines5, 'circles': {},
       'dims': [{'kind': 'aligned', 'ents': ['BL', 'BR'], 'expr': 'Span'},
                {'kind': 'h', 'ents': ['O', 'C'], 'expr': 'Half'}],
       'cons': [{'kind': 'vertical', 'ents': ['L']},
                {'kind': 'perpendicular', 'ents': ['T', 'L']},
                {'kind': 'perpendicular', 'ents': ['R', 'T']},
                {'kind': 'perpendicular', 'ents': ['B', 'R']},
                {'kind': 'midpoint', 'ents': ['MB', 'B']}, {'kind': 'midpoint', 'ents': ['MT', 'T']},
                {'kind': 'midpoint', 'ents': ['C', 'M']}]}
o5 = sd.derive(sk5, P5)['points']
check('slot left side', sd.render(o5['TL']['u']) if o5['TL']['u'] else None, 'Half - Span / 2')
check('slot right side', sd.render(o5['BR']['u']) if o5['BR']['u'] else None, 'Half + Span / 2')
check('render half',sd.render(sd.form_of('Rib / 2 + Off', P4)), 'Off + Rib / 2')

if fails:
    print('FAIL (%d)' % len(fails))
    for x in fails:
        print('  ' + x)
    sys.exit(1)
print('section_derive: all passed')
