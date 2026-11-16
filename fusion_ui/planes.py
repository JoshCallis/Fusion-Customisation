"""
planes — turning "Width 100 - MembraneThickness" into a construction plane.

The sketch-create dialog takes one line: a DATUM, then optionally how far in from it.

    Width                    the side face
    Width 100                100mm in from the side
    Width 100 - Membrane     100mm in from the side, less the membrane
    X                        the midline
    X 250                    250mm out from the midline, in +X
    Top 18                   18mm down from the top face

The datum names an axis, which names the plane the sketch sits on — X is measured
along, so the sketch is on YZ — and a signed distance from the origin.

INWARD FROM A FACE, OUTWARD FROM A MIDLINE
──────────────────────────────────────────
What follows the datum always means the useful direction, which is not the same
direction twice:

  - after a FACE, it moves INWARD, because that is the only way anything ever sits
    relative to a bound. So the offset is (face - rest), and on a low face the whole
    thing is negated, since a low face's world coordinate is negative.
  - after a MIDLINE, there is no inward, so it moves along the positive axis: (+rest).

BRACKETS
────────
Everything after the datum is subtracted AS A WHOLE. "Width 100 - Membrane" means the
side, less (100 - Membrane), NOT side - 100 - Membrane — which is arithmetically a
different number and never what was meant. So the moment what follows contains an
operator, it is bracketed.

No Fusion API here, so the rules can be tested against text (planes_test.py) rather
than against a model.
"""

import re

# Which construction plane a sketch measured along each axis actually sits on: the one
# the axis is normal to.
BASE_PLANE = {'X': 'YZ', 'Y': 'XZ', 'Z': 'XY'}

# ANGLED PLANES. An angle turns the offset plane about a line lying IN it, and the two
# axes that lie in each base plane are the two candidate pivots. The default is the
# first of X, Y, Z that lies in the plane; the box has a toggle to the other. The
# pivot line is built as the intersection of the offset plane with the ORIGIN plane
# that also contains the pivot axis — that gives a line parallel to the axis, sitting
# in the offset plane, and Fusion keeps it there as the offset moves.
#   axis measured along -> (default pivot, the other pivot)
PIVOT_AXES = {'X': ('Y', 'Z'), 'Y': ('X', 'Z'), 'Z': ('X', 'Y')}
# The origin plane containing a pivot axis that is NOT the base plane of the measured
# axis: (measured axis, pivot axis) -> origin plane key.
PIVOT_PLANE = {('X', 'Y'): 'XY', ('X', 'Z'): 'XZ',
               ('Y', 'X'): 'XY', ('Y', 'Z'): 'YZ',
               ('Z', 'X'): 'XZ', ('Z', 'Y'): 'YZ'}


def pivot_axes(measured):
    """(default pivot, other pivot) for a plane measured along `measured`."""
    return PIVOT_AXES.get((measured or '').upper(), ('', ''))


def pivot_plane(measured, pivot):
    """The origin plane whose intersection with the offset plane gives the pivot line."""
    return PIVOT_PLANE.get(((measured or '').upper(), (pivot or '').upper()), '')


def angle_expression(text, flip=False, plus90=False):
    """What Fusion is given for the angle: the typed text, reversed and/or quarter-
    turned as the box's toggles say — the toggles never rewrite what was typed."""
    t = (text or '').strip()
    if not t:
        return ''
    inner = bracket(t)
    if flip:
        inner = '-%s' % (inner if inner.startswith('(') else '(%s)' % inner)
    if plus90:
        inner = '%s + 90 deg' % inner
    return inner

# An operator BETWEEN terms is what forces brackets. A leading sign is part of the first
# term ("-5" is a number, not an operation), so the search starts past it.
_OPERATOR = re.compile(r'[+\-*/]')
_LEADING_SIGN = re.compile(r'^\s*[-+]')


def needs_brackets(rest):
    """Does what follows the datum contain an operation between terms?"""
    if not rest:
        return False
    text = rest.strip()
    start = 1 if _LEADING_SIGN.match(text) else 0
    return _OPERATOR.search(text, start) is not None


def bracket(rest):
    """`rest`, parenthesised if it is more than one term. Already-wrapped stays as it is,
    so typing the brackets yourself is not punished with a second pair."""
    text = (rest or '').strip()
    if not text:
        return ''
    if text.startswith('(') and text.endswith(')') and _balanced(text[1:-1]):
        return text
    return '(%s)' % text if needs_brackets(text) else text


def _balanced(text):
    depth = 0
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def split_input(text):
    """('Width', '100 - Membrane') — the datum word and everything after it."""
    stripped = (text or '').strip()
    if not stripped:
        return '', ''
    parts = stripped.split(None, 1)
    return parts[0], (parts[1].strip() if len(parts) > 1 else '')


def offset_expression(datum_kind, face_name, rest, sign):
    """The signed distance from the origin, as an expression Fusion can evaluate.

    datum_kind  'face' or 'midline'
    face_name   the parameter naming that face — ignored for a midline, which is 0
    rest        what was typed after the datum, or ''
    sign        +1 for a high face (Right/Back/Top), -1 for a low one
    """
    inner = bracket(rest)

    if datum_kind == 'midline':
        # Out from the midline along the positive axis. Nothing after it means the
        # midline itself, which is the origin.
        return inner or '0'

    if not face_name:
        return ''
    if not inner:
        body = face_name
    else:
        body = '%s - %s' % (face_name, inner)

    if sign > 0:
        return body
    # A low face sits at a negative coordinate, so the whole distance is negated —
    # brackets around it, or the negation would only reach the first term.
    return '-(%s)' % body if inner else '-%s' % face_name
