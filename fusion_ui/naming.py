"""
naming — the situational variable engine.

Josh's parameter names carry their situation in them: EdgeBackOffsetY is the back Y
edge, EdgeOffsetX the X one. This module reads those names, strips the situational
words out to leave a STEM, and records what it stripped as TAGS. Names that differ
only in their variables collapse into one family:

    EdgeOffsetBackY  EdgeOffsetFrontY  EdgeOffsetX  EdgeOffsetZ
        └──────────────── family "EdgeOffset", four members ────────────┘

which is the whole point: you type "EdgeOffset", and the situation you are dimensioning
in picks the member. Fusion still receives the real name.

The vocabulary lives in variables.json beside this file, so it can be edited without
touching code, and naming.js is a faithful mirror of this module for the companion
windows. Keep the two in step — the JS file says the same thing at the top.

WHAT IS DELIBERATELY NOT HERE
─────────────────────────────
No Fusion API, no I/O beyond reading variables.json, no notion of a component or a
bounds record. This is pure text, so it can be unit-tested against the real parameter
list offline (naming_report.py does exactly that) and reused by the add-ins, the
servers and the report alike.
"""

import json
import os
import re

VARIABLES_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'variables.json')

# CamelCase word split. The first alternative takes a run of capitals NOT followed by a
# lowercase letter, which is what keeps acronyms whole: "HIPSThickness" splits as
# HIPS + Thickness and "MDFThickness" as MDF + Thickness, so neither can yield a bare
# axis letter. "SomethingXY" likewise yields "XY", which matches no vocabulary word.
WORDS_RE = re.compile(r'[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+')

_vocab_cache = None
_vocab_mtime = None


# ── Vocabulary ───────────────────────────────────────────────────────────────
class Vocabulary(object):
    """The variable words, indexed for lookup.

    `by_word` maps a lowercased word to {dimension, word, axis, end}. Lowercased
    because a name typed in the value box may not be capitalised the way the parameter
    is, and a case-sensitive miss there would silently drop a tag.
    """

    def __init__(self, spec):
        self.spec = spec
        self.dimensions = spec.get('dimensions', [])
        self.relative_types = set(
            w.lower() for w in spec.get('relativeTypes', []) if isinstance(w, str))
        self.by_word = {}
        for dim in self.dimensions:
            for val in dim.get('values', []):
                word = val.get('word', '')
                if not word:
                    continue
                self.by_word[word.lower()] = {
                    'dimension': dim.get('id', ''),
                    'symmetric_only': bool(dim.get('symmetricOnly')),
                    'word': word,
                    'axis': val.get('axis', ''),
                    'end': val.get('end', ''),
                }

    def dimension_ids(self):
        return [d.get('id', '') for d in self.dimensions]

    def words_of(self, dimension_id):
        for dim in self.dimensions:
            if dim.get('id') == dimension_id:
                return [v.get('word', '') for v in dim.get('values', [])]
        return []


def load_vocabulary(path=None):
    """The vocabulary, re-read whenever variables.json changes on disk.

    Cached on mtime rather than loaded once, because the edit button in the Browser
    window writes this file and every consumer should see the change on its next tick
    without a Fusion reload.
    """
    global _vocab_cache, _vocab_mtime
    path = path or VARIABLES_JSON
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if _vocab_cache is not None and mtime == _vocab_mtime:
        return _vocab_cache
    with open(path) as fh:
        spec = json.load(fh)
    _vocab_cache = Vocabulary(spec)
    _vocab_mtime = mtime
    return _vocab_cache


# ── Splitting ────────────────────────────────────────────────────────────────
def words(name):
    """[(word, start index)] for a CamelCase name. "BoxWidthX" -> Box@0 Width@3 X@8."""
    return [(m.group(0), m.start()) for m in WORDS_RE.finditer(name or '')]


# ── Analysis ─────────────────────────────────────────────────────────────────
class Analysis(object):
    """What one parameter name means.

    stem       the name with every variable word removed, words rejoined in order.
    tags       {dimension id: word} for each variable found.
    axis       the world axis this name is about, or '' — resolved from the explicit
               axis word first, then from whatever a direction or span word implies.
    end        'high' | 'low' | '' — which end of that axis, from a direction word.
    hits       [(dimension id, word, index)] in the order they appear, so a UI can
               highlight exactly which characters were read as variables.
    conflicts  human-readable notes where the name contradicts itself, e.g. a direction
               word implying one axis while an explicit axis word says another. Never
               raised as an error: an odd name still analyses, it just gets flagged.
    """

    __slots__ = ('name', 'stem', 'tags', 'axis', 'end', 'hits', 'conflicts', 'frame')

    def __init__(self, name, stem, tags, axis, end, hits, conflicts, frame='origin'):
        self.name = name
        self.stem = stem
        self.tags = tags
        self.axis = axis
        self.end = end
        self.hits = hits
        self.conflicts = conflicts
        # 'origin', or a direction word. WHAT THIS VALUE IS MEASURED FROM — see analyse.
        self.frame = frame

    @property
    def is_tagged(self):
        return bool(self.tags)

    def situation_key(self):
        """The (axis, end) this name answers to. Two members of a family sharing one of
        these are ambiguous — no situation can choose between them."""
        return (self.axis, self.end)

    def as_dict(self):
        return {
            'name': self.name, 'stem': self.stem, 'tags': dict(self.tags),
            'axis': self.axis, 'end': self.end,
            'hits': [list(h) for h in self.hits], 'conflicts': list(self.conflicts),
            'frame': self.frame,
        }

    def __repr__(self):
        return 'Analysis(%r -> %r %r)' % (self.name, self.stem, self.tags)


def analyse(name, vocab=None):
    """Split `name` into a stem plus its situational tags.

    VARIABLES ARE A TRAILING RUN
    ────────────────────────────
    Josh's convention is: what is being measured, then the type of measurement, then the
    axis or direction. So variables only ever appear at the END of a name, and the parse
    is simply to scan backwards collecting vocabulary words until one is not:

        BootCeilingBackOffsetFront
        └──── stem ──────┘└ tags ┘        Front is collected; the scan stops at Offset;
                                          Back is never reached, so it stays a descriptor

    That single rule is what makes the convention unambiguous with no separator and no
    list of measurement types — the type word is simply whatever stops the scan. It also
    means the contract Josh has to keep is exactly one sentence: NOTHING BUT VARIABLES
    COMES AFTER THE MEASUREMENT TYPE.

    A direction word before the type is a descriptor and is left alone, which is what
    tells DrawerBackGearOffsetZ (the back gear's Z offset) from EdgeOffsetBackY (the
    back edge, in Y). Reading the first as a Y situation was the old parse's worst
    failure, and position now settles it rather than a heuristic about agreeing axes.
    """
    vocab = vocab or load_vocabulary()
    ws = words(name)

    cut = len(ws)
    while cut > 0 and ws[cut - 1][0].lower() in vocab.by_word:
        cut -= 1

    conflicts = []
    stem_words = [w for w, _ in ws[:cut]]
    tags, hits = {}, []

    for word, at in ws[cut:]:
        entry = vocab.by_word[word.lower()]
        dim = entry['dimension']
        if dim in tags:
            conflicts.append(
                'two %s words (%s and %s); the second is left in the stem'
                % (dim, tags[dim], word))
            stem_words.append(word)
            continue
        tags[dim] = entry['word']
        hits.append((dim, entry['word'], at))

    def axis_from(dim):
        word = tags.get(dim)
        return vocab.by_word[word.lower()]['axis'] if word else ''

    # An axis or span word names the axis the value is measured along; a direction word
    # only names it when nothing else does. The two disagreeing means the trailing run
    # says two different things at once — a malformed name under the convention — so the
    # direction is demoted into the stem and the disagreement reported.
    measured = axis_from('axis') or axis_from('span')
    direction_axis = axis_from('direction')

    if measured and direction_axis and direction_axis != measured:
        word = tags.pop('direction')
        conflicts.append('%s implies %s but the measured axis is %s; read as a '
                         'descriptor' % (word, direction_axis, measured))
        hits = [h for h in hits if h[0] != 'direction']
        stem_words.append(word)

    axis = measured or direction_axis
    end = ''
    if 'direction' in tags:
        end = vocab.by_word[tags['direction'].lower()]['end']

    stem = ''.join(stem_words)
    if not stem:
        # The whole name is variables — "Depth", "Width", "BackY". The TAGS are still
        # true and are exactly what situational filtering needs, so they are kept; only
        # the stem is a problem, because an empty one would collide every such name into
        # a single family. The name stands as its own stem instead.
        #
        # Getting this wrong made bare Depth and Height match every situation, since a
        # name with no axis is treated as general and never filtered.
        stem = name

    # THE FRAME: what this value is measured FROM.
    #
    # The measurement type decides. An "offset" goes FROM the direction named beside it,
    # so ShelfOffsetBack sits 30mm in from the BACK bound. Any other type merely locates
    # something in world space — FrontPosition is the front's position measured, like
    # everything else, from the origin. An offset carrying only an axis is likewise
    # origin-framed: ShelfOffsetY is 300mm from the Y origin.
    #
    # This is what lets a dimension that starts at the origin stop offering parameters
    # measured from a bound, and vice versa.
    #
    # On a SYMMETRIC axis there is no single direction to name — Left and Right are the
    # same distance away — so the span word is what names that bound: Josh's rule is
    # that the bound in that direction IS the width/depth/height. BootCeilingOffsetWidth
    # is therefore measured in from the side, not out from the centre, and a span frame
    # means "the nearer bound of this axis" rather than one particular end.
    frame = 'origin'
    if stem_words and stem_words[-1].lower() in vocab.relative_types:
        if 'direction' in tags:
            frame = tags['direction']
        elif 'span' in tags:
            frame = tags['span']

    return Analysis(name, stem, tags, axis, end, hits, conflicts, frame)


# ── Direction lookups ────────────────────────────────────────────────────────
def direction_at(axis, end, vocab=None):
    """The direction word naming one end of an axis: ('Y', 'high') -> 'Back'."""
    vocab = vocab or load_vocabulary()
    axis, end = (axis or '').upper(), (end or '').lower()
    for entry in vocab.by_word.values():
        if entry['dimension'] == 'direction' and entry['axis'] == axis \
                and entry['end'] == end:
            return entry['word']
    return ''


def opposite_direction(word, vocab=None):
    """The direction facing the other way down the same axis: 'Back' -> 'Front'.

    What a SPAN crosses. Naming Depth from the front bound carries you to the back one,
    which is why the frame filter reverses the moment a span appears in the expression.
    """
    vocab = vocab or load_vocabulary()
    entry = vocab.by_word.get((word or '').lower())
    if not entry or entry['dimension'] != 'direction':
        return ''
    other = 'low' if entry['end'] == 'high' else 'high'
    return direction_at(entry['axis'], other, vocab)


# ── Families ─────────────────────────────────────────────────────────────────
def families(names, vocab=None):
    """{stem: [Analysis, ...]} over a list of parameter names, order preserved."""
    vocab = vocab or load_vocabulary()
    out = {}
    for name in names:
        a = analyse(name, vocab)
        out.setdefault(a.stem, []).append(a)
    return out


def stem_collisions(names, vocab=None):
    """Stems that are ALSO the name of a real parameter.

    WingWebThickness and WingFrontWebThickness form the family "WingWebThickness", which
    is itself a parameter. Offering that stem would mean typing an existing name in full
    and having it silently resolve to a DIFFERENT parameter, so consumers must not offer
    it — a name that exists means the parameter that has it. Listed here because it is a
    naming-convention problem worth seeing, not just a case to skip.

    Returns [{'stem', 'members'}] for each collision.
    """
    vocab = vocab or load_vocabulary()
    taken = set(names)
    out = []
    for stem, members in families(names, vocab).items():
        if len(members) > 1 and stem in taken:
            out.append({'stem': stem, 'members': [a.name for a in members]})
    out.sort(key=lambda g: g['stem'])
    return out


def ambiguous(members):
    """Members of one family that share a situation key, i.e. that no situation can tell
    apart. Returns {(axis, end): [Analysis, ...]} for the clashing keys only."""
    by_key = {}
    for a in members:
        by_key.setdefault(a.situation_key(), []).append(a)
    return dict((k, v) for k, v in by_key.items() if len(v) > 1)


# ── Resolution ───────────────────────────────────────────────────────────────
def resolve(members, axis='', end='', symmetric=False, vocab=None):
    """The member of a family that fits the situation, or None.

    Preference order, most specific first:
      1. axis and end both match          EdgeBackOffsetY for (Y, high)
      2. axis matches, member has no end  EdgeOffsetX      for (X, anything)
      3. the only member, when the situation says nothing at all

    A member tagged with a span word (Width/Height/Depth) is only eligible when that
    axis is symmetric on the component — on an asymmetric axis those words are out of
    the vocabulary entirely, and offering one there would name a midline that is not a
    modelling feature.
    """
    vocab = vocab or load_vocabulary()

    def eligible(a):
        for dim, word in a.tags.items():
            entry = vocab.by_word.get(word.lower())
            if entry and entry['symmetric_only'] and not symmetric:
                return False
        return True

    pool = [a for a in members if eligible(a)]
    if not pool:
        return None

    if axis:
        if end:
            exact = [a for a in pool if a.axis == axis and a.end == end]
            if exact:
                return exact[0]
        loose = [a for a in pool if a.axis == axis and not a.end]
        if loose:
            return loose[0]
        same_axis = [a for a in pool if a.axis == axis]
        if same_axis:
            return same_axis[0]
        return None

    untagged = [a for a in pool if not a.axis]
    if untagged:
        return untagged[0]
    return pool[0] if len(pool) == 1 else None


def resolve_name(names, stem, axis='', end='', symmetric=False, vocab=None):
    """Convenience: the real parameter name for `stem` in this situation, or ''."""
    vocab = vocab or load_vocabulary()
    fam = families(names, vocab).get(stem)
    if not fam:
        return ''
    hit = resolve(fam, axis=axis, end=end, symmetric=symmetric, vocab=vocab)
    return hit.name if hit else ''


# ── Candidate variables ──────────────────────────────────────────────────────
# Words that qualify a feature rather than name one. Used only to RANK what the
# structural search below turns up: a group whose words are all in here is worth a
# look, one whose words are feature nouns almost certainly is not. Extend it freely —
# nothing breaks if a word is missing, the group just sorts lower.
QUALIFIERS = set("""
    inner outer upper lower open closed minimum maximum min max middle centre center
    near far start end first second primary secondary male female positive negative
    high low big small large long short thick thin deep shallow wide narrow
    internal external forward rear leading trailing
""".split())

def candidate_variables(names, vocab=None, min_group=2):
    """Words that BEHAVE like variables but are not in the vocabulary yet.

    Found structurally rather than from a list of guesses: drop one more word from each
    already-stemmed name and see which names then coincide. If removing "Inner" from one
    name and "Outer" from another leaves the same reduced stem, those two words are
    filling the same slot — which is what a variable is.

        LowerInnerRunnerOffsetX  ─drop Inner─┐
                                             ├─> "LowerRunnerOffset"  => Inner | Outer
        LowerOuterRunnerOffsetX  ─drop Outer─┘

    The catch is that this finds ANY word that differs, so "Boot | Drawer around
    CeilingOffset" comes out alongside "Inner | Outer around RunnerOffset" — one is a
    pair of feature names, the other a real qualifier, and nothing structural separates
    them. So each group is also marked `likely` when every one of its words is a known
    qualifier (QUALIFIERS below). Structure finds the candidates; that list ranks them.
    Neither decides — this is a shortlist to judge, not a verdict.

    Returns [{'words': sorted words, 'reduced': stem, 'names': [...], 'likely': bool}]
    ordered likely-first, then by how many names the group covers.
    """
    vocab = vocab or load_vocabulary()

    groups = {}
    for name in names:
        a = analyse(name, vocab)
        stem_words = words(a.stem)
        for i, (word, _) in enumerate(stem_words):
            reduced = ''.join(w for j, (w, _) in enumerate(stem_words) if j != i)
            if not reduced:
                continue
            groups.setdefault(reduced, {}).setdefault(word, set()).add(name)

    out = []
    for reduced, by_word in groups.items():
        if len(by_word) < min_group:
            continue
        covered = set()
        for names_for_word in by_word.values():
            covered |= names_for_word
        words_here = sorted(by_word)
        out.append({
            'words': words_here,
            'reduced': reduced,
            'names': sorted(covered),
            'likely': all(w.lower() in QUALIFIERS for w in words_here),
        })
    out.sort(key=lambda g: (not g['likely'], -len(g['names']), -len(g['words']),
                            g['reduced']))
    return out
