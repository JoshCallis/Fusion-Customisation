"""Offline test for section_geom.py — /usr/bin/python3 section_geom_test.py"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, fname):
    path = os.path.join(HERE, fname)
    m = types.ModuleType(name)
    m.__file__ = path
    with open(path) as f:
        exec(compile(f.read(), path, 'exec'), m.__dict__)
    return m


sg = _load('section_geom', 'section_geom.py')
sd = _load('section_derive', 'section_derive.py')
Form = sd.Form
fails = []


def check(name, got, want):
    if got != want:
        fails.append('%s: got %r want %r' % (name, got, want))


# The Drawer Ceiling sketch's frame: origin (0,-22,0), x along X, y along -Z.
amap = sg.axis_map((0, -22, 0), (1, 0, 0), (0, 0, -1))
check('u axis', amap['u'], ('X', 1))
check('v axis', amap['v'], ('Z', -1))
check('normal', amap['n'], ('Y', 1))
check('to sketch', sg.to_sketch(amap, (5, -22, -3)), (5, 3))
check('back to world', sg.world_of(amap, 5, 3), (5, -22, -3))
check('tilted refused', sg.axis_map((0, 0, 0), (0.7, 0.7, 0), (0, 0, 1)), None)
check('which key', sg.sketch_axis_of(amap, 'Z'), 'v')

P = sd.Params(user={'Right': (72.0, 'L'), 'Top': (0.0, 'L'), 'Front': (36.0, 'L'),
                    'Gap': (2.0, 'L')})
fz = sd.form_of('Top - Gap', P)                        # a world Z coordinate
sf = sg.sketch_form(amap, 'v', fz, Form)               # in sketch v: -(Top - Gap)
check('sketch form', sd.render(sf), 'Gap - Top')
check('world form round trip', sd.render(sg.world_form(amap, 'v', sf, Form)), 'Top - Gap')

pool = sg.Pool()
pool.add('X', 70.5, sd.form_of('Right - 15 mm', P), 1, 'ceiling')
pool.add('X', 70.5, Form.const(70.5, True), 5, 'fixed')
pool.add('X', 70.5, Form.const(70.5), 1, 'literal')
f, label = pool.match('X', 70.50001)
check('pool prefers strong + terms at equal priority? (priority wins)', label, 'fixed')
pool2 = sg.Pool()
pool2.add('X', 70.5, sd.form_of('Right - 15 mm', P), 1, 'ceiling')
pool2.add('X', 70.5, Form.const(70.5), 1, 'literal')
check('pool prefers terms', pool2.match('X', 70.5)[1], 'ceiling')
check('pool miss', pool2.match('X', 71.0), (None, ''))

pool3 = sg.Pool()
pool3.add('X', 15.0, sd.form_of('Gap', P) + Form.const(13.0), 3, 'slot', group='ribs')
pool3.add_pattern('ribs', 'X', 5.0, sd.form_of('50 mm', P), 4)
pool3.add_mirror('ribs', 'X', 0.0, Form.const(0.0))
f, label = pool3.match('X', 25.0, group='ribs')
check('pattern instance', (sd.render(f) if f else None, label), ('Gap + 230 mm', 'slot pattern'))
f, label = pool3.match('X', -15.0, group='ribs')
check('mirrored', (sd.render(f) if f else None, label), ('-Gap - 130 mm', 'slot mirrored'))
f, label = pool3.match('X', -20.0, group='ribs')
check('mirrored instance', f is not None and abs(sd.form_value(f, P) + 20.0) < 1e-9, True)
check('other group sees no pattern', pool3.match('X', 25.0, group='main'), (None, ''))

ends = sg.extent_forms(sd.form_of('Front', P), [sd.form_of('18 mm', P)])
check('extent candidates', sorted(sd.render(e) for e in ends),
      sorted(['Front', 'Front + 18 mm', 'Front - 18 mm']))

check('edge key dedupe', sg.edge_key('line', [(1, 2, 3), (4, 5, 6)]),
      sg.edge_key('line', [(4, 5, 6), (1, 2, 3)]))

# tethers: x = Right - 15mm, anchors at the Right face (x = Right) and the origin
anchors = [('origin', Form.const(0), 0.0), ('right', sd.form_of('Right', P), 72.0)]
aid, diff, how = sg.choose_tether(sd.form_of('Right - 15 mm', P), 70.5, anchors,
                                  {'Right', 'Top', 'Front'}, Form)
check('tether to right', (aid, sd.render(diff), how), ('right', '15 mm', 'dim'))
aid, diff, how = sg.choose_tether(sd.form_of('Right', P), 72.0, anchors,
                                  {'Right', 'Top', 'Front'}, Form)
check('tether align', (aid, how), ('right', 'align'))
aid, diff, how = sg.choose_tether(sd.form_of('Gap + 20 mm', P), 4.0, anchors,
                                  {'Right', 'Top', 'Front'}, Form)
check('tether origin', (aid, sd.render(diff)), ('origin', 'Gap + 20 mm'))

r = sg.plane_from_picks([{'kind': 'face', 'normal': (0, 1, 0), 'point': (1, 4, 2)},
                         {'kind': 'line', 'a': (0, 6, 0), 'b': (0, 10, 0)}])
check('face + line', (r['axis'], r['value'], r['from']), ('Y', 8.0, 1))
r = sg.plane_from_picks([{'kind': 'face', 'normal': (0, 0, -1), 'point': (1, 4, 2)},
                         {'kind': 'line', 'a': (0, 6, 2), 'b': (0, 10, 2)}])
check('line in the face ignored', (r['axis'], r['value'], r['ignored']), ('Z', 2, [1]))
check('tilted face', 'error' in sg.plane_from_picks(
    [{'kind': 'face', 'normal': (0.7, 0.7, 0), 'point': (0, 0, 0)}]), True)

# weld_edges — Sketch37 on 260612 (plane X = 0): one body's cap sat 25 nm off its
# neighbour's line, and the sketch computed no profiles at all.
x0, xs = 0.115, 0.11499750109669016
edges = [{'kind': 'line', 'pts': [(0, x0, -35.885), (0, x0, 35.885)]},
         {'kind': 'line', 'pts': [(0, x0, 1.5), (0, x0, 0.815)]},
         {'kind': 'line', 'pts': [(0, xs, 35.847), (0, xs, 35.678)]},
         {'kind': 'arc', 'pts': [(0, 2.29, 29.25), (0, 1.0, 33.0), (0, xs, 35.847)],
          'c': (0, -8.79, 29.25), 'r': 11.08},
         {'kind': 'line', 'pts': [(0, 3.0, 1.0), (0, 3.0005, 1.0)]}]
w = sg.weld_edges(edges, 'X')
check('weld: stray cap onto its neighbour', w[2]['pts'], [(0, x0, 35.847), (0, x0, 35.678)])
check('weld: the arc end follows', w[3]['pts'][2], (0, x0, 35.847))
check('weld: the majority line untouched', w[0]['pts'], [(0, x0, -35.885), (0, x0, 35.885)])
check('weld: 5 µm apart is real', w[4]['pts'], [(0, 3.0, 1.0), (0, 3.0005, 1.0)])
check('weld: input left alone', edges[2]['pts'][0][1], xs)
check('weld: what moved is marked', [bool(r.get('welded')) for r in w],
      [False, False, True, True, False])
w = sg.weld_edges([{'kind': 'line', 'pts': [(0, 1.0, 0), (0, 1.0, 5)]},
                   {'kind': 'line', 'pts': [(0, 1.00000003, 5), (0, 1.00000003, 0)]}], 'X')
check('weld: a near copy is dropped', len(w), 1)
w = sg.weld_edges([{'kind': 'line', 'pts': [(0, 0, 0, ), (0, 4, 3)]},
                   {'kind': 'line', 'pts': [(0, 2.0, 1.500003), (0, 2.0, 9.0)]}], 'X')
p = w[1]['pts'][0]
check('weld: onto a slanted line', abs(p[1] * 3 - p[2] * 4) < 1e-12, True)
w = sg.weld_edges([{'kind': 'line', 'pts': [(0, 0, 0), (0, 0.00002, 0)]}], 'X')
check('weld: a line welded to nothing goes', w, [])

# A tube cut across: two concentric circles — one key each (the bore was dropped).
w = sg.weld_edges([{'kind': 'circle', 'pts': [(0, 1, 1)], 'c': (0, 1, 1), 'r': 2.0},
                   {'kind': 'circle', 'pts': [(0, 1, 1)], 'c': (0, 1, 1), 'r': 1.5}], 'X')
check('weld: concentric circles both kept', sorted(r['r'] for r in w), [1.5, 2.0])
w = sg.weld_edges([{'kind': 'circle', 'pts': [(0, 1, 1)], 'c': (0, 1, 1), 'r': 2.0},
                   {'kind': 'circle', 'pts': [(0, 1, 1)], 'c': (0, 1, 1), 'r': 2.0}], 'X')
check('weld: one circle twice is one', len(w), 1)
# A run of values each within eps of the next: nothing moves further than eps.
w = sg.weld_edges([{'kind': 'line', 'pts': [(0, i * 0.9e-4, 0), (0, i * 0.9e-4, 5)]}
                   for i in range(20)], 'X')
moved = max(abs(r['pts'][0][1] - round(r['pts'][0][1] / 0.9e-4) * 0.9e-4) for r in w)
check('weld: a chain is not collapsed', len(w) >= 6, True)
check('weld: nothing snapped further than eps', all(
    min(abs(r['pts'][0][1] - i * 0.9e-4) for i in range(20)) <= 1e-4 + 1e-12 for r in w), True)
# One arc from each of two touching bodies, run opposite ways: one curve.
w = sg.weld_edges([{'kind': 'arc', 'pts': [(0, 1, 0), (0, 0.7071, 0.7071), (0, 0, 1)],
                    'c': (0, 0, 0), 'r': 1.0},
                   {'kind': 'arc', 'pts': [(0, 0, 1), (0, 0.7071, 0.7071), (0, 1, 0)],
                    'c': (0, 0, 0), 'r': 1.0}], 'X')
check('weld: a reversed copy of an arc is dropped', len(w), 1)
check('edge_key: arc either way round', sg.edge_key('arc', [(0, 1), (2, 3), (4, 5)]),
      sg.edge_key('arc', [(4, 5), (2, 3), (0, 1)]))

if fails:
    print('FAIL (%d)' % len(fails))
    for x in fails:
        print('  ' + x)
    sys.exit(1)
print('section_geom: all passed')
