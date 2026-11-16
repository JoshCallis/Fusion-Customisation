"""Offline test for number_params.py — run with /usr/bin/python3."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import number_params as np  # noqa: E402

FAILS = []


def check(what, got, want):
    if got != want:
        FAILS.append('%s: got %r, want %r' % (what, got, want))


def lits(text, unit='mm'):
    return [s['text'] for s in np.literals(text, unit)]


# Which literals count.
check('whole', lits('10'), ['10'])
check('whole with unit', lits('10 mm'), ['10 mm'])
check('sum', lits('Shelf + 10'), ['10'])
check('difference', lits('Shelf - 2.5 cm'), ['2.5 cm'])
check('sign', lits('-10'), ['10'])
check('factor left', lits('2 * Shelf'), ['2'])
check('factor right', lits('Shelf * 2'), ['2'])
check('divisor', lits('Shelf / 2'), ['2'])
check('numerator', lits('(Shelf + 10) / 2'), ['10', '2'])
check('bracket under factor', lits('2 * (10 + Shelf)'), ['2', '10'])
check('unit under factor', lits('10 mm * 2'), ['10 mm', '2'])
check('two bare', lits('10 * 2'), [])
check('power', lits('Shelf ^ 2'), [])
check('call bare', lits('sqrt(2) * Shelf'), [])
check('call unit', lits('Shelf * cos(30 deg)'), ['30 deg'])
check('zero', lits('Shelf + 0'), [])
check('unitless field', lits('4', ''), [])
check('scale in unitless field', lits('N * 2', ''), [])
check('scale is unitless', [s['unit'] for s in np.literals('Shelf / 2 + 10', 'mm')], ['', 'mm'])
check('nested scale', lits('Shelf * (N * 2)'), ['2'])
check('angle scale', lits('Ang / 2', 'deg'), ['2'])
check('angle field', lits('30', 'deg'), ['30'])
check('no space unit', lits('10mm'), ['10mm'])
check('unreadable', lits('Front 100'), [])
check('names only', lits('Shelf + Gap'), [])
check('units of literals', [s['unit'] for s in np.literals('10 + 2 cm', 'mm')], ['mm', 'cm'])

# Whole literals.
check('whole literal', np.is_whole_literal(' -10 mm '), True)
check('not whole', np.is_whole_literal('10 + Shelf'), False)
check('name not literal', np.is_whole_literal('Shelf'), False)

# Names.
check('is name', np.is_number_name('Num_0123abcd'), True)
check('not name', np.is_number_name('Num_Shelf'), False)
n = np.new_name()
check('new name valid', np.is_number_name(n), True)
check('names_in', np.names_in('Num_0123abcd + Shelf - Num_0123abcd + Num_ffffffff'),
      ['Num_0123abcd', 'Num_ffffffff'])

# Planning: reuse by value, then by order.
old = {'Num_aaaaaaaa': ('10 mm', 'mm'), 'Num_bbbbbbbb': ('5 mm', 'mm')}
spans, left = np.plan('12 + 5', 'mm', old)
check('reuse by value', [s['name'] for s in spans], ['Num_aaaaaaaa', 'Num_bbbbbbbb'])
check('nothing left', left, [])
spans, left = np.plan('5', 'mm', old)
check('keep the equal one', [s['name'] for s in spans], ['Num_bbbbbbbb'])
check('leftover', left, ['Num_aaaaaaaa'])
spans, left = np.plan('1 + 2 + 3', 'mm', old)
check('new past old', [s['name'] for s in spans], ['Num_aaaaaaaa', 'Num_bbbbbbbb', None])
spans, left = np.plan('12', 'mm', {'Num_dddddddd': ('30 deg', 'deg')})
check('never across kinds', ([s['name'] for s in spans], left), ([None], ['Num_dddddddd']))
spans, _ = np.plan('Shelf + 10', 'mm', {})
for s in spans:
    s['name'] = 'Num_cccccccc'
check('rewrite', np.rewrite('Shelf + 10', spans), 'Shelf + Num_cccccccc')
spans, _ = np.plan('10 + 10', 'mm', {})
for i, s in enumerate(spans):
    s['name'] = 'Num_%08d' % i
check('rewrite two', np.rewrite('10 + 10', spans), 'Num_00000000 + Num_00000001')

# Literal values and how a chip reads.
check('value default unit', np.literal_value('10', 'mm'), (10.0, 'mm'))
check('value own unit', np.literal_value('2.5 cm', 'mm'), (2.5, 'cm'))
check('value equal across spelling', np.literal_value('10mm', 'mm') == np.literal_value('10', 'mm'), True)
check('shown same unit', np.shown('10 mm', 'mm'), '10')
check('shown other unit', np.shown('2.5 cm', 'mm'), '2.5 cm')
check('shown bare', np.shown('10', 'mm'), '10')

check('display', np.display('Right - Num_aaaaaaaa', {'Num_aaaaaaaa': '33.5 mm'}), 'Right - 33.5 mm')
check('display signed', np.display('2 * Num_aaaaaaaa', {'Num_aaaaaaaa': '-3 mm'}), '2 * (-3 mm)')
spans, _ = np.plan('Shelf / 2', 'mm', {'Num_eeeeeeee': ('2', '')})
check('reuse a scale', [s['name'] for s in spans], ['Num_eeeeeeee'])
check('display untouched', np.display('Right - Gap', {'Num_aaaaaaaa': '1'}), 'Right - Gap')

# A bracket that multiplies a length is a factor when it is surely unitless — its
# numbers become UNITLESS parameters (Fusion refused "Shelf * (1 mm + 0.1 mm)").
KINDS = {'Shelf': 'L', 'N': 'S', 'Ang': 'A'}.get
lit = lambda t, u, k=None: [s['text'] for s in np.literals(t, u, k)]
check('factor bracket of numbers', lit('Shelf * (1 + 0.1)', 'mm'), ['1', '0.1'])
check('factor bracket, unitless param', lit('Shelf * (N + 1)', 'mm', KINDS), ['1'])
check('length bracket times unitless', lit('(Shelf + 10) * (N + 1)', 'mm', KINDS), ['10', '1'])
check('length bracket times param', lit('(Shelf + 10) * N', 'mm', KINDS), ['10'])
check('unknown param: still a length', lit('(Shelf + 10) * N', 'mm'), ['10'])
check('two number brackets: factors', lit('(1 + 1) * (2 + 3)', 'mm'), [])
check('angle field', lit('Ang * (1 + 0.5)', 'deg', KINDS), ['1', '0.5'])

if FAILS:
    print('\n'.join(FAILS))
    print('%d FAILED' % len(FAILS))
    sys.exit(1)
print('number_params: all passed')
