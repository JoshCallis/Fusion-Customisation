#!/usr/bin/env python3
"""
Cases for planes.py.

    python3 fusion_ui/planes_test.py

Pure text in, pure text out, so this needs neither Fusion nor a model. The arithmetic
is the point: "Width 100 - Membrane" must come out as the side LESS (100 - Membrane),
because side - 100 - Membrane is a different number and never the one that was meant.

EVERY CASE RUNS TWICE. planes.js is the same rules again, for the window — the add-in
composes the expression it writes into the plane, and the dialog shows the brackets and
previews the result — so the two are diffed here exactly as naming_selftest diffs the
naming engines. If the JS cannot be run at all this FAILS: a self-test that quietly
stops covering half of what it covers is worse than none.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import planes  # noqa: E402

JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/'
       'Helpers/jsc')

# (label, datum kind, face parameter, what followed, sign, expected)
CASES = [
    # A face on its own is that face.
    ('the side',            'face', 'Right', '',                 +1, 'Right'),
    ('the low face',        'face', 'Left',  '',                 -1, '-Left'),

    # One term after it moves inward.
    ('in from the side',    'face', 'Right', '100',              +1, 'Right - 100'),
    ('in from the low face', 'face', 'Left', '100',              -1, '-(Left - 100)'),

    # Several terms are subtracted AS A WHOLE. This is the case brackets exist for.
    ('two terms',           'face', 'Right', '100 - Membrane',   +1,
     'Right - (100 - Membrane)'),
    ('two terms, low face', 'face', 'Left',  '100 - Membrane',   -1,
     '-(Left - (100 - Membrane))'),
    ('a sum',               'face', 'Top',   'A + B',            +1, 'Top - (A + B)'),

    # A leading sign belongs to the first term; it is not an operation between two.
    ('a negative number',   'face', 'Right', '-5',               +1, 'Right - -5'),

    # Brackets already typed are not doubled.
    ('already bracketed',   'face', 'Right', '(100 - M)',        +1, 'Right - (100 - M)'),

    # A midline has no inward, so what follows goes along the positive axis.
    ('the midline',         'midline', '',    '',                +1, '0'),
    ('out from the midline', 'midline', '',   '250',             +1, '250'),
    ('out, two terms',      'midline', '',    '250 - M',         +1, '(250 - M)'),
]

SPLITS = [
    ('Width',                    ('Width', '')),
    ('Width 100',                ('Width', '100')),
    ('Width 100 - Membrane',     ('Width', '100 - Membrane')),
    ('  Top   18  ',             ('Top', '18')),
    ('',                         ('', '')),
]

BRACKETS = [
    ('100', False), ('', False), ('-5', False), ('+5', False),
    ('100 - M', True), ('A+B', True), ('A * 2', True), ('A/2', True),
]


# (angle text, flip, plus90) -> what Fusion is given. The field shows the text alone.
ANGLES = [
    ('30', False, False, '30'),
    ('30', True, False, '-(30)'),
    ('30', False, True, '30 + 90 deg'),
    ('30', True, True, '-(30) + 90 deg'),
    ('A + 5', False, False, '(A + 5)'),
    ('A + 5', True, False, '-(A + 5)'),
    ('', True, True, ''),
]


# jsc has no require() and no fs, so planes.js and the inputs are concatenated ahead of
# this. It reads only the globals the runner defines.
JS_DRIVER = r'''
var out = { splits: [], brackets: [], offsets: [], base: {} };
for (var i = 0; i < SPLITS.length; i++) out.splits.push(Planes.splitInput(SPLITS[i]));
for (var i = 0; i < RESTS.length;  i++) out.brackets.push(Planes.needsBrackets(RESTS[i]));
for (var i = 0; i < OFFSETS.length; i++) {
  var c = OFFSETS[i];
  out.offsets.push(Planes.offsetExpression(c[0], c[1], c[2], c[3]));
}
out.base = Planes.BASE_PLANE;
out.pivots = Planes.PIVOT_AXES;
out.angles = [];
for (var i = 0; i < ANGLES.length; i++) {
  var a = ANGLES[i];
  out.angles.push(Planes.angleExpression(a[0], a[1], a[2]));
}
PRINT(JSON.stringify(out));
'''


def run_js(splits, rests, offsets):
    """What planes.js makes of the same tables, or None if it could not be run."""
    with open(os.path.join(HERE, 'planes.js')) as fh:
        lib = fh.read()
    tmp = tempfile.mkdtemp(prefix='planes_test_')

    for runtime, printer in (('node', 'function (s) { process.stdout.write(s); }'),
                             (JSC, 'function (s) { print(s); }')):
        if runtime == JSC and not os.path.exists(JSC):
            continue
        program = '\n'.join([
            lib,
            'var SPLITS = ' + json.dumps(splits) + ';',
            'var RESTS = ' + json.dumps(rests) + ';',
            'var OFFSETS = ' + json.dumps(offsets) + ';',
            'var ANGLES = ' + json.dumps([[a, f, p] for a, f, p, _w in ANGLES]) + ';',
            'var PRINT = ' + printer + ';',
            JS_DRIVER,
        ])
        path = os.path.join(tmp, 'program_%s.js' % os.path.basename(runtime))
        with open(path, 'w') as fh:
            fh.write(program)
        try:
            res = subprocess.run([runtime, path], capture_output=True, text=True,
                                 timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if res.returncode != 0:
            print('%s failed:\n%s' % (os.path.basename(runtime), res.stderr.strip()))
            continue
        try:
            return json.loads(res.stdout.strip())
        except ValueError:
            print('%s produced unparseable output' % os.path.basename(runtime))
            continue
    return None


def main():
    fails = []

    for text, want in SPLITS:
        got = planes.split_input(text)
        if got != want:
            fails.append('split %r: got %r want %r' % (text, got, want))

    for rest, want in BRACKETS:
        got = planes.needs_brackets(rest)
        if got != want:
            fails.append('needs_brackets %r: got %r want %r' % (rest, got, want))

    for label, kind, face, rest, sign, want in CASES:
        got = planes.offset_expression(kind, face, rest, sign)
        if got != want:
            fails.append('%s: got %r want %r' % (label, got, want))

    # The plane a sketch measured along each axis sits on.
    for axis, want in (('X', 'YZ'), ('Y', 'XZ'), ('Z', 'XY')):
        if planes.BASE_PLANE[axis] != want:
            fails.append('base plane for %s: %r' % (axis, planes.BASE_PLANE[axis]))

    # The same tables again, through planes.js. Two implementations of one rule set
    # drift silently: the window would bracket what the add-in does not, and the
    # expression written into the plane would differ from the one previewed.
    js = run_js([t for t, _w in SPLITS],
                [r for r, _w in BRACKETS],
                [[kind, face, rest, sign] for _l, kind, face, rest, sign, _w in CASES])
    if js is None:
        fails.append('could not run planes.js under node or jsc, so the two '
                     'implementations were never compared')
    else:
        for (text, _want), got in zip(SPLITS, js['splits']):
            mine = list(planes.split_input(text))
            if list(got) != mine:
                fails.append('js split %r: got %r want %r' % (text, got, mine))
        for (rest, _want), got in zip(BRACKETS, js['brackets']):
            mine = planes.needs_brackets(rest)
            if bool(got) != mine:
                fails.append('js needs_brackets %r: got %r want %r'
                             % (rest, got, mine))
        for (label, kind, face, rest, sign, _want), got in zip(CASES, js['offsets']):
            mine = planes.offset_expression(kind, face, rest, sign)
            if got != mine:
                fails.append('js %s: got %r want %r' % (label, got, mine))
        if js['base'] != planes.BASE_PLANE:
            fails.append('js base planes: %r' % (js['base'],))
        if {k: list(v) for k, v in js['pivots'].items()} != \
                {k: list(v) for k, v in planes.PIVOT_AXES.items()}:
            fails.append('js pivot axes: %r' % (js['pivots'],))
        for (text, flip, p90, _want), got in zip(ANGLES, js['angles']):
            mine = planes.angle_expression(text, flip, p90)
            if got != mine:
                fails.append('js angle %r: got %r want %r' % (text, got, mine))

    for text, flip, p90, want in ANGLES:
        got = planes.angle_expression(text, flip, p90)
        if got != want:
            fails.append('angle %r flip=%s +90=%s: got %r want %r' % (text, flip, p90, got, want))
    for measured, (d, o) in planes.PIVOT_AXES.items():
        for pv in (d, o):
            if not planes.pivot_plane(measured, pv):
                fails.append('no pivot plane for %s about %s' % (measured, pv))
            elif planes.pivot_plane(measured, pv) == planes.BASE_PLANE[measured]:
                fails.append('pivot plane for %s about %s is the base plane' % (measured, pv))

    if fails:
        print('FAIL (%d)' % len(fails))
        for f in fails:
            print('  - ' + f)
        return 1
    print('OK — %d offsets, %d splits, %d bracket cases, each in both engines'
          % (len(CASES), len(SPLITS), len(BRACKETS)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
