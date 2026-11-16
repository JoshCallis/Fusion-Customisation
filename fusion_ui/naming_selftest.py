#!/usr/bin/env python3
"""
naming_selftest — naming.py and naming.js must agree, exactly.

    python3 naming_selftest.py                 # live, from the params server
    python3 naming_selftest.py params.json     # a saved /params snapshot

Two implementations of one rule set is a standing invitation for them to drift, and a
drift here is invisible: the window would offer a family the add-in cannot resolve, or
resolve to a different member than the one shown. So this runs BOTH over the same names
and diffs stem, tags, axis and end for every one, plus a set of hand-written cases that
pin the behaviour the real names do not happen to cover.

The JS half runs under node if it is on PATH, otherwise under the JavaScriptCore
interpreter macOS already ships (jsc). If neither works the run FAILS rather than
passing quietly — a self-test that silently stops testing half of what it covers is
worse than no self-test.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import naming  # noqa: E402

PARAMS_URL = 'http://127.0.0.1:8731/params'

# Cases the real parameter list does not cover, pinning behaviour we rely on.
# (name, expected stem, expected axis, expected end)
CASES = [
    # The convention: what is measured, then the type, then the axis or direction.
    # Variables are the TRAILING run; the scan stops at the measurement type.
    ('EdgeOffsetBackY',            'EdgeOffset',                'Y', 'high'),
    ('EdgeOffsetFrontY',           'EdgeOffset',                'Y', 'low'),
    ('EdgeOffsetX',                'EdgeOffset',                'X', ''),
    ('EdgeOffsetZ',                'EdgeOffset',                'Z', ''),
    # A direction on its own implies its axis, so the axis letter is optional.
    ('EdgeOffsetBack',             'EdgeOffset',                'Y', 'high'),
    ('EdgeOffsetFront',            'EdgeOffset',                'Y', 'low'),
    # Josh's own example: Back is before the type, so it is a descriptor and survives
    # into the stem; only Front is situational.
    ('BootCeilingBackOffsetFront', 'BootCeilingBackOffset',     'Y', 'low'),
    # A direction before the type names WHICH FEATURE, never the situation.
    ('DrawerBackGearOffsetZ',      'DrawerBackGearOffset',      'Z', ''),
    ('DrawerFrontGearOffsetZ',     'DrawerFrontGearOffset',     'Z', ''),
    # Nothing after the type, so nothing is situational — this used to be read as a Y
    # situation, which was the old parse's worst failure.
    ('DrawerBackGearMotionAngle',  'DrawerBackGearMotionAngle', '',  ''),
    ('WingFrontWebThickness',      'WingFrontWebThickness',     '',  ''),
    # An axis with no direction, and a type with neither.
    ('ThicknessY',                 'Thickness',                 'Y', ''),
    ('Thickness',                  'Thickness',                 '',  ''),
    # Span words carry an axis of their own.
    ('CarcassWidth',               'Carcass',                   'X', ''),
    ('CarcassHeight',              'Carcass',                   'Z', ''),
    # Acronyms must not yield a bare axis letter.
    ('HIPSThickness',              'HIPSThickness',             '',  ''),
    ('MDFThickness',               'MDFThickness',              '',  ''),
    # A capital run containing axis letters is one word and matches nothing.
    ('PlateXY',                    'PlateXY',                   '',  ''),
    # A name that is ENTIRELY variables still carries its tags — they are what
    # situational filtering runs on. Only the stem falls back to the name, so that these
    # do not all collide into one empty-stemmed family.
    ('Depth',                      'Depth',                     'Y', ''),
    ('Width',                      'Width',                     'X', ''),
    ('Height',                     'Height',                    'Z', ''),
    ('BackY',                      'BackY',                     'Y', 'high'),
    ('HalfDepth',                  'Half',                      'Y', ''),
]

# (name, expected frame) — what a value is measured FROM. The measurement type decides:
# an "offset" goes FROM the direction beside it, anything else is origin-framed.
FRAME_CASES = [
    ('ShelfOffsetBack',            'Back'),
    ('ShelfOffsetFront',           'Front'),
    ('ShelfOffsetY',               'origin'),   # an axis, not a bound, so from zero
    ('FrontPosition',              'origin'),   # not an offset: a world position
    ('BootCeilingBackOffsetFront', 'Front'),    # the trailing Front is the datum
    # On a symmetric axis the span word names the bound: there is no single direction to
    # name, so the width IS the datum. An offset carrying only an axis stays origin-framed.
    ('BootCeilingOffsetWidth',     'Width'),
    ('CarcassOffsetHeight',        'Height'),
    ('CarcassPositionWidth',       'origin'),   # not an offset: a world position
    ('Width',                      'origin'),
    ('Thickness',                  'origin'),
]

# One driver for both runtimes. jsc has no require() and no fs, so naming.js, the
# vocabulary and the name list are all CONCATENATED ahead of this by _js_program below;
# it only ever reads the globals VOCAB and NAMES, which both runtimes can be given.
JS_DRIVER = r'''
Naming.setVocabulary(VOCAB);
var out = {};
for (var i = 0; i < NAMES.length; i++) {
  var a = Naming.analyse(NAMES[i]);
  out[NAMES[i]] = { stem: a.stem, tags: a.tags, axis: a.axis, end: a.end,
                    frame: a.frame };
}
PRINT(JSON.stringify(out));
'''

JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/'
       'Helpers/jsc')


def _js_program(names, printer):
    """naming.js + the inputs + the driver, as one self-contained script."""
    with open(os.path.join(HERE, 'naming.js')) as fh:
        lib = fh.read()
    with open(os.path.join(HERE, 'variables.json')) as fh:
        vocab_json = fh.read()
    return '\n'.join([
        lib,
        'var VOCAB = ' + vocab_json + ';',
        'var NAMES = ' + json.dumps(names) + ';',
        'var PRINT = ' + printer + ';',
        JS_DRIVER,
    ])


def load_names(source):
    if source is None:
        with urllib.request.urlopen(PARAMS_URL, timeout=3) as fh:
            return [p['name'] for p in json.load(fh)['params']]
    with open(source) as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get('params', [])
    return [p['name'] if isinstance(p, dict) else str(p) for p in data]


def run_js(names):
    """{name: {stem, tags, axis, end}} from naming.js, and which runtime produced it.

    Returns (result, runtime) — result is None when no runtime could be used, and the
    caller treats that as a failure.
    """
    tmp = tempfile.mkdtemp(prefix='naming_selftest_')

    for runtime, printer in (('node', 'function (s) { process.stdout.write(s); }'),
                             (JSC, 'function (s) { print(s); }')):
        if runtime == JSC and not os.path.exists(JSC):
            continue
        path = os.path.join(tmp, 'program_%s.js' % os.path.basename(runtime))
        with open(path, 'w') as fh:
            fh.write(_js_program(names, printer))
        try:
            res = subprocess.run([runtime, path], capture_output=True, text=True,
                                 timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if res.returncode != 0:
            print('%s failed:\n%s' % (os.path.basename(runtime), res.stderr.strip()))
            continue
        try:
            return json.loads(res.stdout.strip()), os.path.basename(runtime)
        except ValueError:
            print('%s produced unparseable output' % os.path.basename(runtime))
            continue

    return None, ''


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else None
    names = load_names(source)
    vocab = naming.load_vocabulary()
    failures = []

    # 1. Hand-written cases against the Python.
    for name, want in FRAME_CASES:
        got = naming.analyse(name, vocab).frame
        if got != want:
            failures.append('frame %s: expected %r, got %r' % (name, want, got))

    for name, stem, axis, end in CASES:
        a = naming.analyse(name, vocab)
        if (a.stem, a.axis, a.end) != (stem, axis, end):
            failures.append('case %s: expected (%r, %r, %r), got (%r, %r, %r)'
                            % (name, stem, axis, end, a.stem, a.axis, a.end))

    # 2. Python vs JS over the real names plus the cases.
    all_names = list(names) + [c[0] for c in CASES] + [c[0] for c in FRAME_CASES]
    js, runtime = run_js(all_names)
    if js is None:
        failures.append('could not run naming.js under node or jsc, so the two '
                        'implementations were never compared')
    else:
        for name in all_names:
            a = naming.analyse(name, vocab)
            b = js.get(name)
            if b is None:
                failures.append('js missing %s' % name)
                continue
            mine = {'stem': a.stem, 'tags': a.tags, 'axis': a.axis, 'end': a.end,
                    'frame': a.frame}
            theirs = {'stem': b['stem'], 'tags': b['tags'], 'axis': b['axis'],
                      'end': b['end'], 'frame': b['frame']}
            if mine != theirs:
                failures.append('drift %s:\n    py %r\n    js %r' % (name, mine, theirs))

    # 3. No family may be internally ambiguous — two members no situation can separate.
    for stem, members in naming.families(names, vocab).items():
        for key, group in naming.ambiguous(members).items():
            failures.append('ambiguous family %s at %r: %s'
                            % (stem, key, ', '.join(a.name for a in group)))

    if failures:
        print('FAIL (%d)' % len(failures))
        for f in failures:
            print('  - ' + f)
        return 1
    print('OK — %d cases, %d real names, python and js (%s) agree, '
          'no ambiguous families' % (len(CASES), len(names), runtime))
    return 0


if __name__ == '__main__':
    sys.exit(main())
