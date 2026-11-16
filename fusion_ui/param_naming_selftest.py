#!/usr/bin/env python3
"""
param_naming_selftest — param_naming.py and param_naming.js must agree, exactly.

    python3 param_naming_selftest.py

Two implementations of one rule set is a standing invitation for them to drift, and a
drift here is invisible in the same way naming.py/.js drift is: the window would offer
or accept a name the add-in parses differently (or not at all). This runs both over a
shared list of exemplar names — including the deliberately awkward ones (a level word
that collides with an anchor/type word, every "no anchor/axis at all" type, every
invalid shape) — and diffs parse()'s full result for each.

The JS half runs under node if it is on PATH, otherwise under the JavaScriptCore
interpreter macOS already ships (jsc). If neither works the run FAILS rather than
passing quietly, matching naming_selftest.py's rule.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import param_naming as pn  # noqa: E402

# name -> expected parse() result, or None for names that must be rejected.
CASES = [
    ('BootCeiling_Front_Inset_Back',
     {'levels': ['BootCeiling', 'Front'], 'type': 'Inset', 'anchor': 'Back', 'axis': 'Y', 'end': 'high'}),
    # A level word equal to an anchor word stays a level when it isn't trailing.
    ('Drawer_Front_Span_X',
     {'levels': ['Drawer', 'Front'], 'type': 'Span', 'anchor': None, 'axis': 'X', 'end': None}),
    ('Drawer_Width_Span_X',
     {'levels': ['Drawer', 'Width'], 'type': 'Span', 'anchor': None, 'axis': 'X', 'end': None}),
    ('Handle_Radius',
     {'levels': ['Handle'], 'type': 'Radius', 'anchor': None, 'axis': None, 'end': None}),
    ('Boot_Wall_Thickness',
     {'levels': ['Boot', 'Wall'], 'type': 'Thickness', 'anchor': None, 'axis': None, 'end': None}),
    ('Drawer_Count',
     {'levels': ['Drawer'], 'type': 'Count', 'anchor': None, 'axis': None, 'end': None}),
    ('Drawer_Multiplier',
     {'levels': ['Drawer'], 'type': 'Multiplier', 'anchor': None, 'axis': None, 'end': None}),
    ('Lid_Angle',
     {'levels': ['Lid'], 'type': 'Angle', 'anchor': None, 'axis': None, 'end': None}),
    ('Shelf_Offset_Z',
     {'levels': ['Shelf'], 'type': 'Offset', 'anchor': None, 'axis': 'Z', 'end': None}),
    ('Gear_Spacing_Y',
     {'levels': ['Gear'], 'type': 'Spacing', 'anchor': None, 'axis': 'Y', 'end': None}),
    ('Rack_Count_X',
     {'levels': ['Rack'], 'type': 'Count', 'anchor': None, 'axis': 'X', 'end': None}),
    # Outset takes an axis LETTER, not an anchor word (only Inset takes an anchor).
    ('Deep_Nested_Feature_Outset_Z',
     {'levels': ['Deep', 'Nested', 'Feature'], 'type': 'Outset', 'anchor': None, 'axis': 'Z', 'end': None}),
    # More than one direction: 'X and Y both work'. Only a type with multi takes it,
    # the tail is all of one kind, and the singular fields are the first of each.
    ('Drawer_Width_Span_X_Y',
     {'levels': ['Drawer', 'Width'], 'type': 'Span', 'anchor': None, 'axis': 'X', 'end': None,
      'anchors': [], 'axes': ['X', 'Y'], 'ends': []}),
    ('Boot_Ceiling_Inset_Front_Back',
     {'levels': ['Boot', 'Ceiling'], 'type': 'Inset', 'anchor': 'Front', 'axis': 'Y', 'end': 'low',
      'anchors': ['Front', 'Back'], 'axes': ['Y'], 'ends': ['low', 'high']}),
    ('Boot_Ceiling_Inset_Left_Top',
     {'levels': ['Boot', 'Ceiling'], 'type': 'Inset', 'anchor': 'Left', 'axis': 'X', 'end': 'low',
      'anchors': ['Left', 'Top'], 'axes': ['X', 'Z'], 'ends': ['low', 'high']}),
    # Span and Thickness now take an OPTIONAL axis — General.
    ('Drawer_Span',
     {'levels': ['Drawer'], 'type': 'Span', 'anchor': None, 'axis': None, 'end': None}),
    ('Boot_Wall_Thickness_Z',
     {'levels': ['Boot', 'Wall'], 'type': 'Thickness', 'anchor': None, 'axis': 'Z', 'end': None}),
    # "Make as group": the type word appears again as an ordinary LEVEL (typeGroupAt),
    # disambiguated purely by position — parse() needs no change to read it. Entry is
    # still the name word (typeGroupAt's index, 2, isn't the last level, 3).
    ('Drawer_Shaft_Span_Entry_Span_X',
     {'levels': ['Drawer', 'Shaft', 'Span', 'Entry'], 'type': 'Span',
      'anchor': None, 'axis': 'X', 'end': None}),
    # A PINNED last level: a double underscore says where the groups end. Before the
    # last level, it is the name (even with only one entry past the owner); after it, a
    # group (even with two or more). Anywhere else it is only a seam.
    ('Drawer_Shelf__Curve_Span_X',
     {'levels': ['Drawer', 'Shelf', 'Curve'], 'type': 'Span', 'anchor': None, 'axis': 'X',
      'end': None, 'lastIs': 'name'}),
    ('Drawer__Shelf_Radius',
     {'levels': ['Drawer', 'Shelf'], 'type': 'Radius', 'anchor': None, 'axis': None,
      'end': None, 'lastIs': 'name'}),
    ('Drawer_Shelf_Curve__Radius',
     {'levels': ['Drawer', 'Shelf', 'Curve'], 'type': 'Radius', 'anchor': None, 'axis': None,
      'end': None, 'lastIs': 'group'}),
    # Invalid shapes: required segment omitted, or too short, or not this convention.
    ('Drawer_Inset', None),          # Inset needs a real anchor
    ('Drawer_Outset', None),         # Outset needs an axis
    ('Drawer', None),                # no type at all
    ('EdgeBackOffsetY', None),       # the OLD (CamelCase, no underscores) convention
    ('Rack_Count_X_Y', None),        # Count is not a multi type
    ('Drawer_Span_X_Back', None),    # an anchor already says its axis: never both
]

# The situations fits() is cross-checked in. {axis, end, datum, axes, ends} is
# everything the rules can read: the dimension's nearest axis and which end of it
# (kept for single-axis callers), the fuller 45°-pool axes/ends, and what it is
# measured FROM ('origin' | 'bounds' | '' for a dimension whose datum isn't known).
SITUATIONS = [
    {'axis': 'X', 'end': 'high', 'datum': 'origin', 'axes': ['X'], 'ends': {'X': 'high'}},
    {'axis': 'Y', 'end': 'high', 'datum': 'bounds', 'axes': ['Y'], 'ends': {'Y': 'high'}},
    {'axis': 'Y', 'end': 'low', 'datum': 'bounds', 'axes': ['Y'], 'ends': {'Y': 'low'}},
    {'axis': 'Z', 'end': '', 'datum': '', 'axes': ['Z'], 'ends': {}},
    {'axis': '', 'end': '', 'datum': 'origin', 'axes': [], 'ends': {}},
    {'axis': '', 'end': '', 'datum': '', 'axes': [], 'ends': {}},
    # A dimension exactly 45° between X and Y — BOTH pools, 45° itself included.
    {'axis': '', 'end': '', 'datum': 'origin', 'axes': ['X', 'Y'],
     'ends': {'X': 'high', 'Y': 'low'}},
]

# The spoken form, the half the Browser draws (words / display_leaf / parse_leaf).
# One token -> how it reads. The awkward ones are the point: an acronym that must not
# be split letter by letter, a digit that stays attached, an already-spaced level.
WORD_CASES = [
    ('BootCeiling', 'Boot Ceiling'),
    ('Boot', 'Boot'),
    ('X', 'X'),
    ('XY', 'XY'),
    ('IDNumber', 'ID Number'),
    ('Panel2', 'Panel 2'),
    ('Front_Left', 'Front Left'),
    ('', ''),
]

# Typed leaf text -> the name it makes once the owner ['Drawer'] is put back in
# front of it (and the name word, if the text starts with one), or None when the
# convention can't take it. Spacing is forgiving, so the same leaf is written several
# ways. An optional third field is the parameter being renamed (`keep`): a leaf typed
# without its direction keeps that one, as the Browser's rename box relies on.
LEAF_CASES = [
    ('Inset - Back', 'Drawer_Inset_Back'),
    ('inset - back', 'Drawer_Inset_Back'),
    ('Inset-Back', 'Drawer_Inset_Back'),
    ('Span - X, Y', 'Drawer_Span_X_Y'),
    ('Span   -   X ,  Y', 'Drawer_Span_X_Y'),
    ('Radius', 'Drawer_Radius'),
    ('Wall Thickness', None),       # "WallThickness" is no type word
    ('Inset', None),                # Inset needs its anchor
    ('Span', 'Drawer_Span'),        # Span's axis is optional now: General, no axis
    ('Count - X, Y', None),         # Count is not a multi type
    ('Span - X, Back', None),       # an anchor already says its axis: never both
    ('', None),
    ('Curve - Inset - Back', 'Drawer_Curve_Inset_Back'),
    ('curve - inset-back', 'Drawer_Curve_Inset_Back'),
    ('Boot Ceiling - Radius', 'Drawer_BootCeiling_Radius'),
    ('Count - Radius', 'Drawer_Count_Radius'),     # a type word as the name
    ('Width - Span - X, Y', 'Drawer_Width_Span_X_Y'),
    ('Curve - Inset', 'Drawer_Curve_Inset_Back', 'Drawer_Curve_Inset_Back'),
    ('Curve - Inset', 'Drawer_Curve_Inset_Front', 'Drawer_Lid_Inset_Front'),
    ('Width - Span', 'Drawer_Width_Span_X_Y', 'Drawer_Width_Span_X_Y'),
    # Span's axis is optional now: a name with no direction at all is still valid, so
    # this no longer refuses for lack of one — it's just "Width" the General Span.
    ('Width - Span', 'Drawer_Width_Span', 'Drawer_Curve_Inset_Back'),
    ('Width - Radius', 'Drawer_Width_Radius', 'Drawer_Width_Span_X'),
    ('Width - Count', 'Drawer_Width_Count', 'Drawer_Width_Span_X_Y'),  # one axis only: dropped
    # Span's own axis, read/typed back as Width/Depth/Height instead of a direction.
    ('Width', 'Drawer_Span_X'),
    ('Depth, Height', 'Drawer_Span_Y_Z'),
    ('Entry - Width', 'Drawer_Entry_Span_X'),
    ('Width - Back', None),          # Span takes no anchor after its axis
    ('Thickness - Z', 'Drawer_Thickness_Z'),
]

# A TYPE-GROUPED leaf never carries its type in the text — the group already says it
# (keep's own type_group_at >= 0). text, keep (the grouped parameter being renamed),
# expected full name once its own group_levels() are put back in front (None if the
# text can't be read as this leaf at all).
GROUPED_LEAF_CASES = [
    ('Entry - X', 'Drawer_Shaft_Span_Entry_Span_X', 'Drawer_Shaft_Span_Entry_Span_X'),
    ('Entry - Width', 'Drawer_Shaft_Span_Entry_Span_X', 'Drawer_Shaft_Span_Entry_Span_X'),
    ('X', 'Drawer_Shaft_Span_Entry_Span_X', 'Drawer_Shaft_Span_Span_X'),  # tail alone: no name
]

# nameWord/groupLevels promoting a SHARED last level to a group instead of a name —
# each tuple is every name in the group, and the leaf each one is expected to draw
# once nameIndex() sees all of them at once.
NAME_INDEX_CASES = [
    # Span's axis reads as its own word (spanWordsFor), so the shared leaf is just
    # "Width"/"Depth" — the name that would have repeated ("Entry") is the one dropped.
    (['Drawer_Shaft_Entry_Span_X', 'Drawer_Shaft_Entry_Span_Y'],
     {'Drawer_Shaft_Entry_Span_X': 'Width', 'Drawer_Shaft_Entry_Span_Y': 'Depth'}),
    # Not shared (only one parameter has this chain): the ordinary rule still names it.
    (['Drawer_Shaft_Entry_Span_X'],
     {'Drawer_Shaft_Entry_Span_X': 'Entry - Width'}),
]

# raw text -> expected identifier() result.
IDENTIFIER_CASES = [
    ('Front Panel', 'FrontPanel'),
    ('3D Print', 'Comp3DPrint'),
    ('', 'Comp'),
    ('a-b.c', 'abc'),
    ('Boot', 'Boot'),
]


JS_DRIVER = r'''
ParamNaming.setVocabulary(VOCAB);
var out = {};
for (var i = 0; i < NAMES.length; i++) {
  out[NAMES[i]] = ParamNaming.parse(NAMES[i]);
}
var idOut = {};
for (var i = 0; i < IDENTIFIER_NAMES.length; i++) {
  idOut[i] = ParamNaming.identifier(IDENTIFIER_NAMES[i]);
}
// The suggestion rules, over every name × situation and every type × situation —
// the half that decides what the dialog OFFERS, and as easy to drift as parse().
var fitOut = {}, suggestOut = {};
for (var s = 0; s < SITUATIONS.length; s++) {
  for (var i = 0; i < NAMES.length; i++) {
    var parts = ParamNaming.parse(NAMES[i]);
    if (parts) fitOut[s + '|' + NAMES[i]] = ParamNaming.fits(parts, SITUATIONS[s]);
  }
  for (var t = 0; t < TYPE_WORDS.length; t++) {
    suggestOut[s + '|' + TYPE_WORDS[t]] = ParamNaming.suggests(TYPE_WORDS[t], SITUATIONS[s]);
  }
}
// The spoken form: one token's words, each valid name's leaf label, and the leaf
// text read back. Drift here is a row that reads differently in the window from the
// way the add-in would say it.
var wordOut = {}, leafOut = {}, parseLeafOut = {};
for (var i = 0; i < WORD_TOKENS.length; i++) {
  wordOut[i] = ParamNaming.words(WORD_TOKENS[i]);
}
for (var i = 0; i < NAMES.length; i++) {
  var p = ParamNaming.parse(NAMES[i]);
  if (p) { leafOut[NAMES[i]] = ParamNaming.displayLeaf(p);
           leafOut['spoken|' + NAMES[i]] = ParamNaming.displaySpoken(p); }
}
for (var i = 0; i < LEAF_TEXTS.length; i++) {
  var lp = ParamNaming.parseLeaf(LEAF_TEXTS[i],
                                LEAF_KEEPS[i] ? ParamNaming.parse(LEAF_KEEPS[i]) : undefined);
  parseLeafOut[i] = lp ? ParamNaming.compose({ levels: lp.name ? ['Drawer', lp.name] : ['Drawer'],
                                               type: lp.type,
                                               anchors: lp.anchors, axes: lp.axes })
                       : null;
}
// A TYPE-GROUPED leaf: the text never carries the type, keep's own group_levels()
// supply it, same as the Browser's rename box.
var groupedOut = {};
for (var i = 0; i < GROUPED_TEXTS.length; i++) {
  var keepParts = ParamNaming.parse(GROUPED_KEEPS[i]);
  var glp = ParamNaming.parseLeaf(GROUPED_TEXTS[i], keepParts);
  groupedOut[i] = glp
    ? ParamNaming.compose({ levels: ParamNaming.groupLevels(keepParts).concat(glp.name ? [glp.name] : []),
                            type: glp.type, anchors: glp.anchors, axes: glp.axes })
    : null;
}
// nameWord()/groupLevels()/displayLeaf() with a shared-chain nameIndex.
var indexOut = {};
for (var g = 0; g < INDEX_GROUPS.length; g++) {
  var parsedList = INDEX_GROUPS[g].map(function (n) { return ParamNaming.parse(n); });
  var idx = ParamNaming.nameIndex(parsedList);
  for (var i = 0; i < INDEX_GROUPS[g].length; i++) {
    indexOut[g + '|' + INDEX_GROUPS[g][i]] = ParamNaming.displayLeaf(parsedList[i], { index: idx });
  }
}
PRINT(JSON.stringify({ parse: out, identifier: idOut, fits: fitOut, suggests: suggestOut,
                       words: wordOut, leaf: leafOut, parseLeaf: parseLeafOut,
                       grouped: groupedOut, indexed: indexOut }));
'''

JSC = ('/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/'
       'Helpers/jsc')


def _js_program(names, identifier_names, type_words, printer):
    with open(os.path.join(HERE, 'param_naming.js')) as fh:
        lib = fh.read()
    with open(os.path.join(HERE, 'param_naming.json')) as fh:
        vocab_json = fh.read()
    return '\n'.join([
        lib,
        'var VOCAB = ' + vocab_json + ';',
        'var NAMES = ' + json.dumps(names) + ';',
        'var IDENTIFIER_NAMES = ' + json.dumps(identifier_names) + ';',
        'var TYPE_WORDS = ' + json.dumps(type_words) + ';',
        'var SITUATIONS = ' + json.dumps(SITUATIONS) + ';',
        'var WORD_TOKENS = ' + json.dumps([c[0] for c in WORD_CASES]) + ';',
        'var LEAF_TEXTS = ' + json.dumps([c[0] for c in LEAF_CASES]) + ';',
        'var LEAF_KEEPS = ' + json.dumps([c[2] if len(c) > 2 else None
                                          for c in LEAF_CASES]) + ';',
        'var GROUPED_TEXTS = ' + json.dumps([c[0] for c in GROUPED_LEAF_CASES]) + ';',
        'var GROUPED_KEEPS = ' + json.dumps([c[1] for c in GROUPED_LEAF_CASES]) + ';',
        'var INDEX_GROUPS = ' + json.dumps([names for names, _ in NAME_INDEX_CASES]) + ';',
        'var PRINT = ' + printer + ';',
        JS_DRIVER,
    ])


def _leaf_name(text, vocab, keep=None):
    """LEAF_CASES' expectation, the Python way: the typed leaf put back under the
    owner ['Drawer'] (plus its name word), or None if it isn't a leaf at all."""
    lp = pn.parse_leaf(text, vocab, pn.parse(keep, vocab) if keep else None)
    if lp is None:
        return None
    levels = ['Drawer'] + ([lp['name']] if lp['name'] else [])
    return pn.compose({'levels': levels, 'type': lp['type'],
                       'anchors': lp['anchors'], 'axes': lp['axes']})


def run_js(names, identifier_names, type_words):
    tmp = tempfile.mkdtemp(prefix='param_naming_selftest_')
    for runtime, printer in (('node', 'function (s) { process.stdout.write(s); }'),
                             (JSC, 'function (s) { print(s); }')):
        if runtime == JSC and not os.path.exists(JSC):
            continue
        path = os.path.join(tmp, 'program_%s.js' % os.path.basename(runtime))
        with open(path, 'w') as fh:
            fh.write(_js_program(names, identifier_names, type_words, printer))
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


def _norm(parts):
    """None stays None; a parse result is compared field by field. A hand-written case
    may leave the plural fields out — they are then whatever the singular ones say,
    which is how every one-direction name reads."""
    if parts is None:
        return None
    anchor, axis = parts.get('anchor'), parts.get('axis')
    end = parts.get('end')
    return {'levels': list(parts['levels']), 'type': parts['type'],
            'anchor': anchor, 'axis': axis, 'end': end,
            'anchors': list(parts.get('anchors', [anchor] if anchor else [])),
            'axes': list(parts.get('axes', [axis] if axis else [])),
            'ends': list(parts.get('ends', [end] if end else [])),
            'lastIs': parts.get('lastIs')}


def main():
    vocab = pn.load_vocabulary(os.path.join(HERE, 'param_naming.json'))
    failures = []

    # 1. Hand-written cases against the Python.
    for name, want in CASES:
        got = pn.parse(name, vocab)
        if _norm(got) != _norm(want):
            failures.append('case %s: expected %r, got %r' % (name, want, got))
        if want is not None:
            # compose() must round-trip back to the exact name that was parsed.
            recomposed = pn.compose(got)
            if recomposed != name:
                failures.append('roundtrip %s: composed %r' % (name, recomposed))

    # 1b. identifier() hand-written cases against the Python.
    for text, want in IDENTIFIER_CASES:
        got = pn.identifier(text)
        if got != want:
            failures.append('identifier %r: expected %r, got %r' % (text, want, got))

    # 1c. The spoken form's hand-written cases against the Python.
    for token, want in WORD_CASES:
        got = pn.words(token)
        if got != want:
            failures.append('words %r: expected %r, got %r' % (token, want, got))
    for text, want, *keep in LEAF_CASES:
        got = _leaf_name(text, vocab, *keep)
        if got != want:
            failures.append('parse_leaf %r: expected %r, got %r' % (text, want, got))
    # A valid name's leaf must survive the round trip through the text the row shows:
    # what is drawn is what a rename box opens with, so anything it can't read back is
    # a name the user would be unable to retype.
    for name, want in CASES:
        if want is None:
            continue
        parts = pn.parse(name, vocab)
        leaf_text = pn.display_leaf(parts, vocab)
        back = pn.parse_leaf(leaf_text, vocab, parts if pn.type_group_at(parts) >= 0 else None)
        if back is None:
            failures.append('leaf roundtrip %s: %r reads back as nothing'
                            % (name, leaf_text))
            continue
        again = pn.compose({'levels': pn.group_levels(parts)
                                      + ([back['name']] if back['name'] else []),
                            'type': back['type'], 'lastIs': parts.get('lastIs'),
                            'anchors': back['anchors'], 'axes': back['axes']})
        # And the row as the Browser usually draws it — direction hidden — read back
        # with the parameter itself as `keep`, must be the same name again.
        hidden = pn.parse_leaf(pn.display_leaf(parts, vocab, direction=False), vocab, parts)
        if hidden is None or pn.compose({
                'levels': pn.group_levels(parts) + ([hidden['name']] if hidden['name'] else []),
                'type': hidden['type'], 'anchors': hidden['anchors'],
                'axes': hidden['axes'], 'lastIs': parts.get('lastIs')}) != name:
            failures.append('hidden-direction roundtrip %s: %r' % (name, hidden))
        if again != name:
            failures.append('leaf roundtrip %s: came back as %s' % (name, again))

    # 1d. Type-grouped leaves — the text never carries the type, keep's own
    # group_levels() supply it, same as the Browser's rename box.
    for text, keep_name, want in GROUPED_LEAF_CASES:
        keep_parts = pn.parse(keep_name, vocab)
        lp = pn.parse_leaf(text, vocab, keep_parts)
        got = pn.compose({'levels': pn.group_levels(keep_parts) + ([lp['name']] if lp and lp['name'] else []),
                          'type': lp['type'], 'anchors': lp['anchors'], 'axes': lp['axes']}) if lp else None
        if got != want:
            failures.append('grouped leaf %r (keep %s): expected %r, got %r'
                            % (text, keep_name, want, got))

    # 1e. A shared last level is promoted to a GROUP instead of each parameter's own
    # name — nameWord()/groupLevels()/display_leaf() with a nameIndex().
    for names, want in NAME_INDEX_CASES:
        parsed = [pn.parse(n, vocab) for n in names]
        idx = pn.name_index(parsed)
        for n, p in zip(names, parsed):
            got = pn.display_leaf(p, vocab, index=idx)
            if got != want[n]:
                failures.append('indexed leaf %s (with %r): expected %r, got %r'
                                % (n, names, want[n], got))

    # 2. Python vs JS over the same cases.
    names = [c[0] for c in CASES]
    identifier_names = [c[0] for c in IDENTIFIER_CASES]
    type_words = [t['word'] for t in vocab['spec'].get('types', [])]
    js, runtime = run_js(names, identifier_names, type_words)
    if js is None:
        failures.append('could not run param_naming.js under node or jsc, so the two '
                        'implementations were never compared')
    else:
        for name in names:
            mine = _norm(pn.parse(name, vocab))
            theirs = _norm(js['parse'].get(name))
            if mine != theirs:
                failures.append('py/js disagree on %s: py=%r js=%r'
                                % (name, mine, theirs))
        for i, text in enumerate(identifier_names):
            mine = pn.identifier(text)
            theirs = js['identifier'].get(str(i))
            if mine != theirs:
                failures.append('py/js disagree on identifier(%r): py=%r js=%r'
                                % (text, mine, theirs))
        for s, situation in enumerate(SITUATIONS):
            for name in names:
                parts = pn.parse(name, vocab)
                if parts is None:
                    continue
                key = '%d|%s' % (s, name)
                mine = pn.fits(parts, situation, vocab)
                theirs = js['fits'].get(key)
                if mine != theirs:
                    failures.append('py/js disagree on fits(%s, %r): py=%r js=%r'
                                    % (name, situation, mine, theirs))
            for word in type_words:
                key = '%d|%s' % (s, word)
                mine = pn.suggests(word, situation, vocab)
                theirs = js['suggests'].get(key)
                if mine != theirs:
                    failures.append('py/js disagree on suggests(%s, %r): py=%r js=%r'
                                    % (word, situation, mine, theirs))
        for i, (token, _) in enumerate(WORD_CASES):
            mine = pn.words(token)
            theirs = js['words'].get(str(i))
            if mine != theirs:
                failures.append('py/js disagree on words(%r): py=%r js=%r'
                                % (token, mine, theirs))
        for name in names:
            parts = pn.parse(name, vocab)
            if parts is None:
                continue
            for label, mine in (('', pn.display_leaf(parts, vocab)),
                                ('spoken|', pn.display_spoken(parts, vocab))):
                theirs = js['leaf'].get(label + name)
                if mine != theirs:
                    failures.append('py/js disagree on %sleaf(%s): py=%r js=%r'
                                    % (label, name, mine, theirs))
        for i, (text, _, *keep) in enumerate(LEAF_CASES):
            mine = _leaf_name(text, vocab, *keep)
            theirs = js['parseLeaf'].get(str(i))
            if mine != theirs:
                failures.append('py/js disagree on parse_leaf(%r): py=%r js=%r'
                                % (text, mine, theirs))
        for i, (text, keep_name, _) in enumerate(GROUPED_LEAF_CASES):
            keep_parts = pn.parse(keep_name, vocab)
            lp = pn.parse_leaf(text, vocab, keep_parts)
            mine = pn.compose({'levels': pn.group_levels(keep_parts)
                                          + ([lp['name']] if lp and lp['name'] else []),
                               'type': lp['type'], 'anchors': lp['anchors'],
                               'axes': lp['axes']}) if lp else None
            theirs = js['grouped'].get(str(i))
            if mine != theirs:
                failures.append('py/js disagree on grouped parse_leaf(%r, keep=%s): py=%r js=%r'
                                % (text, keep_name, mine, theirs))
        for g, (group_names, _) in enumerate(NAME_INDEX_CASES):
            parsed = [pn.parse(n, vocab) for n in group_names]
            idx = pn.name_index(parsed)
            for n, p in zip(group_names, parsed):
                mine = pn.display_leaf(p, vocab, index=idx)
                theirs = js['indexed'].get('%d|%s' % (g, n))
                if mine != theirs:
                    failures.append('py/js disagree on indexed leaf(%s in group %d): py=%r js=%r'
                                    % (n, g, mine, theirs))
        print('compared against %s' % runtime)

    if failures:
        print('%d FAILURES:' % len(failures))
        for f in failures:
            print('  ' + f)
        return 1
    print('all %d cases agree' % len(names))
    return 0


if __name__ == '__main__':
    sys.exit(main())
