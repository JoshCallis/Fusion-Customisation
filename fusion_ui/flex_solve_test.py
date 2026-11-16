"""Offline test for flex_solve.py — /usr/bin/python3 flex_solve_test.py"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(HERE, 'flex_solve.py')
fs = types.ModuleType('flex_solve')
fs.__file__ = path
with open(path) as f:
    exec(compile(f.read(), path, 'exec'), fs.__dict__)

fails = []


def close(name, got, want, tol=1e-6):
    if got is None or abs(got - want) > tol:
        fails.append('%s: got %r want %r' % (name, got, want))


def check(name, got, want):
    if got != want:
        fails.append('%s: got %r want %r' % (name, got, want))


# The powerboard, tethered to the back: its front sits at Back - PB.
values = {'Back': 36.0, 'Front': 36.0, 'PB': 10.0, 'LD': 5.0, 'BC': 10.0}
bounds = {'Back', 'Front'}
target = {'Back': 1.0, 'PB': -1.0}

r = fs.solve(target, -3.0, values, bounds=bounds)
check('plain move ok', r['ok'], True)
close('PB grows by 3', r['changes'].get('PB'), 3.0)
check('bounds untouched', 'Back' in r['changes'], False)

# The gap to the drawer latch recess (tethered to the front) must stay >= 55.
gap = ({'Back': 1.0, 'Front': 1.0, 'PB': -1.0, 'LD': -1.0}, 57.0, 55.0, None, 'latch gap')
r = fs.solve(target, -3.0, values, bounds=bounds, monitors=[gap])
check('blocked by the gap', r['ok'], False)
close('can move 2', r['limit'], -2.0, 1e-6)
check('names the gap', 'latch gap' in r['blocked'], True)
check('front side untouched', 'LD' in r['changes'], False)

r = fs.solve(target, -3.0, values, bounds=bounds, monitors=[gap], allow_other=True)
check('other side allowed', r['ok'], True)
close('PB +3', r['changes'].get('PB'), 3.0)
close('LD gives 1', r['changes'].get('LD'), -1.0)

# A tie: the boot ceiling's front is tied to the powerboard's, so it comes along.
tie = ({'Back': 1.0, 'PB': -1.0}, {'Back': 1.0, 'BC': -1.0})
r = fs.solve(target, -3.0, values, bounds=bounds, ties=[tie])
close('tied BC follows', r['changes'].get('BC'), 3.0)

# Rigid: nothing can move it.
r = fs.solve(target, -3.0, values, bounds=bounds, rigid={'PB'})
check('rigid refuses', (r['ok'], 'rigid' in r['reason']), (False, True))

# Placed by the bounds alone.
r = fs.solve({'Back': 1.0}, 1.0, values, bounds=bounds)
check('bounds alone', 'bounds' in r['reason'], True)
r = fs.solve({'Back': 1.0}, 1.0, values, bounds=bounds, allow_bounds=True)
close('bound moves when allowed', r['changes'].get('Back'), 1.0)

# Locality: the rarely used parameter takes the whole move, alone.
r = fs.solve({'A': 1.0, 'B': 1.0}, 2.0, {'A': 1.0, 'B': 1.0}, weights={'A': 1.0, 'B': 100.0})
check('local first, alone', r['changes'], {'A': 2.0})

# The shelf: top = Top - Drop, bottom = Top - Drop - Thick. Moving the bottom changes
# the thickness (used once); moving the top moves the shelf (Drop).
sv = {'Top': 40.0, 'Drop': 10.0, 'Thick': 1.8}
w = fs.weights_from_usage({'Drop': 2, 'Thick': 1})
r = fs.solve({'Top': 1.0, 'Drop': -1.0, 'Thick': -1.0}, 0.5, sv, bounds={'Top'}, weights=w)
check('bottom up = thinner', {k: round(v, 9) for k, v in r['changes'].items()}, {'Thick': -0.5})
r = fs.solve({'Top': 1.0, 'Drop': -1.0}, -0.5, sv, bounds={'Top'}, weights=w)
check('top down = shelf drops', r['changes'], {'Drop': 0.5})

# A range that stops the local one is REPORTED, not quietly worked around.
r = fs.solve({'A': 1.0, 'B': 1.0}, 2.0, {'A': 1.0, 'B': 1.0}, weights={'A': 1.0, 'B': 100.0},
             ranges={'A': (None, 1.5)})
check('A blocked, says so', (r['ok'], r['chosen'], r['candidates']), (False, 'A', ['A', 'B']))
close('A can do 0.5', r['limit'], 0.5)
r = fs.solve({'A': 1.0, 'B': 1.0}, 2.0, {'A': 1.0, 'B': 1.0}, weights={'A': 1.0, 'B': 100.0},
             ranges={'A': (None, 1.5)}, prefer='B')
check('B when asked', r['changes'], {'B': 2.0})

# Spread: each frozen at its limit, the rest handed on (the active set).
r = fs.solve({'A': 1.0, 'B': 1.0}, 2.0, {'A': 1.0, 'B': 1.0}, weights={'A': 1.0, 'B': 100.0},
             ranges={'A': (None, 1.5), 'B': (None, 2.5)}, prefer='spread')
close('A to its max', r['changes'].get('A'), 0.5)
close('B the rest', r['changes'].get('B'), 1.5)

# Derived parameters expand: Gap = Wall + 2 mm, so moving Gap moves Wall.
r = fs.solve({'Gap': 1.0}, 0.5, {'Wall': 1.0}, derived={'Gap': {'Wall': 1.0}})
close('derived expands', r['changes'].get('Wall'), 0.5)

if fails:
    print('FAIL (%d)' % len(fails))
    for x in fails:
        print('  ' + x)
    sys.exit(1)
print('flex_solve: all passed')
