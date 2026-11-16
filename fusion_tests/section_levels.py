"""Offline test for Section.py's reference levels (_levels) — which references must sit
on different levels so that no loop of them closes into a profile.

Run it:

    /usr/bin/python3 "/Users/josh/Code/Scripts/fusion_tests/section_levels.py"

_levels is pure arithmetic inside an adsk module, so it is lifted out of Section.py
by name (ast) and run on its own. Pins:

    1. curves sharing an end differ
    2. an end on a line's interior (a T) differs from the line
    3. arcs/circles on one circle differ — a rib cut to fit a tube closed the tube
       circle's deliberate gap on the same level, and the tube was a profile
       (scratch doc, 2026-10-08)
    4. an end landing on a circle differs from it
    5. a held level is kept unless a neighbour has it
"""
import ast
import math
import os

SECTION = os.path.expanduser(
    '~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/Section.py')

fails = []


def check(label, got, want=True):
    ok = (got == want)
    print(('  ok   ' if ok else '  FAIL ') + label
          + ('' if ok else '   got %r want %r' % (got, want)))
    if not ok:
        fails.append(label)


def _lift(name):
    src = open(SECTION, encoding='utf-8').read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            lines = src.splitlines(True)[node.lineno - 1:node.end_lineno]
            scope = {'math': math}
            exec(compile(''.join(lines), SECTION, 'exec'), scope)
            return scope[name]
    raise SystemExit('no %s in Section.py' % name)


_levels = _lift('_levels')


def line(a, b):
    return ([a, b], True, None)


def arc(a, b, c, r):
    return ([a, b], False, (c[0], c[1], r))


def circle(c, r):
    return ([], False, (c[0], c[1], r))


print('1. shared ends')
lv = _levels([line((0, 0), (1, 0)), line((1, 0), (1, 1)), line((1, 1), (0, 0))])
check('a triangle of lines: all three differ', len(set(lv)), 3)

print('2. a T onto a line')
lv = _levels([line((0, 0), (4, 0)), line((2, 0), (2, 3))])
check('the stem differs from the bar', lv[0] != lv[1])

print('3. one circle')
s = math.sqrt(4 - 0.25)
lv = _levels([circle((40, 0), 2.0), arc((40 + s, 0.5), (40 + s, -0.5), (40, 0), 2.0)])
check('a tube circle and a rib arc on it differ', lv[0] != lv[1])
lv = _levels([arc((2, 0), (0, 2), (0, 0), 2.0), arc((1.414214, 1.414214), (-2, 0), (0, 0), 2.0)])
check('two overlapping arcs of one circle differ', lv[0] != lv[1])
lv = _levels([circle((0, 0), 2.0), circle((0, 0), 1.5)])
check('a tube\'s two walls may share a level', lv, [1, 1])

print('4. an end on a circle')
lv = _levels([circle((40, 0), 2.0), line((40 + s, 0.5), (46, 0.5))])
check('a rib side ending on the tube differs from it', lv[0] != lv[1])

print('5. held levels')
lv = _levels([line((0, 0), (1, 0)), line((5, 5), (6, 5))], [3, 3])
check('unrelated curves keep their levels', lv, [3, 3])
lv = _levels([line((0, 0), (1, 0)), line((1, 0), (2, 0))], [2, 2])
check('a clash keeps the first, moves the second', lv[0] == 2 and lv[1] != 2)
check('old two-tuples still read', _levels([([(0, 0), (1, 0)], True)]), [1])

print()
if fails:
    print('FAILED: %d' % len(fails))
    raise SystemExit(1)
print('all passed')
