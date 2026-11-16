"""param_naming.py — the structured parameter-naming convention, Python side.

A faithful mirror of param_naming.js. The add-ins use this; the companion windows use
the JS twin. Both read the SAME param_naming.json, and param_naming_selftest.py checks
the two agree on every exemplar name — so if you change one, change the other and run
the test. The file itself is edited in Control Room ▸ Fusion ▸ Parameter Names.

A parameter name has three segments, joined with '_' in the real Fusion name:

    BootCeiling_Front_Inset_Back
    └── feature levels ──┘ type  anchor

Feature levels can be any words (including ones that also happen to be anchor or type
words — Front above is a LEVEL, not the anchor, because it isn't in trailing position).
Only the trailing tokens are ever classified: the vocabulary of type words and the
vocabulary of anchor/axis words are disjoint, so parsing is unambiguous — no
backtracking, just take trailing anchor/axis words until a type word stops them.

MORE THAN ONE ANCHOR OR AXIS. A type with "multi" lets a parameter say that two
directions both work — Drawer_Width_Span_X_Y is the span on X and on Y, and is
suggested on either. Every trailing word is of ONE kind: an anchor already carries its
axis, so anchors and axis letters are never mixed in one name.

This is a separate, newer convention from variables.json's situational-tag engine
(naming.py / naming.js). The two are not merged: an old-convention name simply fails
to parse() here and is left alone.

Usage:
    vocab = load_vocabulary("/Users/josh/Code/Scripts/fusion_ui/param_naming.json")
    parts = parse("BootCeiling_Front_Inset_Back", vocab)
    # {'levels': ['BootCeiling', 'Front'], 'type': 'Inset', 'anchor': 'Back', 'axis': 'Y'}
    compose(parts) == "BootCeiling_Front_Inset_Back"
    display(parts) == "BootCeiling,Front-Inset-Back"

And the SPOKEN form the Browser draws — the levels between the owner and the last
become groups; the last level (the NAME word), the type and the direction become the
row, punctuated like text (see "The spoken form" below):

    display_word("BootCeiling")   == "Boot Ceiling"
    name_word(parts)              == "Front"
    display_leaf(parts)           == "Front - Inset - Back"
    display_leaf(parts, direction=False) == "Front - Inset"
    parse_leaf("Front - Inset - Back", v)
        # {'name': 'Front', 'type': 'Inset', 'anchors': ['Back'], 'axes': []}
"""

import json
import re

FUSION_DELIM = "_"
LEVEL_DELIM = ","
SEGMENT_DELIM = "-"
WORD_JOIN = " "
SEG_JOIN = " - "
TAIL_JOIN = ", "


def identifier(text):
    """A raw string (typically a component name) reduced to something Fusion accepts
    as a parameter name/level: strip everything outside [A-Za-z0-9_], then prefix
    "Comp" if what's left is empty or starts with a digit. Mirrors Bounds.py's
    _squash — that add-in-side helper predates this engine and stays independent."""
    out = re.sub(r"[^A-Za-z0-9_]", "", text or "")
    if not out or not re.match(r"[A-Za-z_]", out[0]):
        out = "Comp" + out
    return out


def set_vocabulary(spec):
    types_by_word = {t["word"]: t for t in spec.get("types", [])}
    anchors_by_word = {a["word"]: a for a in spec.get("anchors", [])}
    axes = set(spec.get("axes", []))
    # spanWords: {type, X, Y, Z} — Span's own axis reads back as Width/Depth/Height
    # (see "The spoken form"). Reversed once here, mirroring param_naming.js.
    sw = spec.get("spanWords") or None
    span_type = sw.get("type") if sw else None
    span_axis_to_word, span_word_to_axis = {}, {}
    if sw:
        for k, v in sw.items():
            if k in ("type", "_comment"):
                continue
            span_axis_to_word[k] = v
            span_word_to_axis[v] = k
    # fused: a type's `fused` map ({anchor: word}) — Inset + Back is SAID "Backset".
    # Display only; the Fusion name is still Inset_Back.
    anchor_to_fused, fused_to_anchor = {}, {}
    for t in types_by_word.values():
        if t.get("fused"):
            anchor_to_fused[t["word"]] = t["fused"]
            for a, f in t["fused"].items():
                fused_to_anchor[f] = (t["word"], a)
    return {
        "anchorToFused": anchor_to_fused,
        "fusedToAnchor": fused_to_anchor,
        "spec": spec,
        "types": types_by_word,
        "anchors": anchors_by_word,
        "axes": axes,
        "spanType": span_type,
        "spanAxisToWord": span_axis_to_word,
        "spanWordToAxis": span_word_to_axis,
    }


def load_vocabulary(path):
    with open(path) as f:
        return set_vocabulary(json.load(f))


def _unique(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def parse(fusion_name, vocab):
    """Split a Fusion parameter name into its parts, or None if it isn't one.

    {levels, type, anchor, axis, end, anchors, axes, ends}

    `anchors` is every anchor WORD the name carries (['Back'] — or ['Front', 'Back']
    on a multi type), empty when the type takes no anchor. `axes` is every axis LETTER
    the value is on, given directly (Span/Outset/Offset/…) or implied by the anchors
    (Inset). The singular `anchor`/`axis`/`end` are the first of each — what every
    caller that only ever deals in one of them still reads.

    `lastIs` ("name" | "group", absent when unpinned) is the PINNED last level — a
    double underscore marks where the groups end (param_naming.js, PIN):
    Drawer_Shelf__Curve_Width pins Curve as the name, Drawer_Shelf_Curve__Width as a
    group.
    """
    raw = (fusion_name or "").split(FUSION_DELIM)
    out = _parse_tokens([t for t in raw if t], vocab)
    if out is None:
        return None
    mark, seen = -1, 0
    for k, t in enumerate(raw):
        if t:
            seen += 1
        elif seen > 0 and k < len(raw) - 1 and mark < 0:
            mark = seen
    n = len(out["levels"])
    if n >= 2 and mark == n - 1:
        out["lastIs"] = "name"
    elif n >= 2 and mark == n:
        out["lastIs"] = "group"
    return out


def _parse_tokens(tokens, vocab):
    if len(tokens) < 2:
        return None

    # Walk back over the trailing anchor/axis words. A type word stops the walk (the
    # two vocabularies are disjoint), and so does running out of tokens — a name is at
    # least one level and a type.
    tail = []
    i = len(tokens)
    while i > 2:
        word = tokens[i - 1]
        if word in vocab["types"]:
            break
        if word not in vocab["anchors"] and word not in vocab["axes"]:
            break
        tail.insert(0, word)
        i -= 1

    type_tok = tokens[i - 1]
    type_def = vocab["types"].get(type_tok)
    if type_def is None:
        return None
    levels = tokens[: i - 1]
    if not levels:
        return None

    kinds = set("anchor" if w in vocab["anchors"] else "axis" for w in tail)
    if len(kinds) > 1:
        return None      # an anchor already says its axis: never both in one name
    if len(tail) > 1 and not type_def.get("multi"):
        return None      # this type takes one direction only

    if not tail:
        if type_def.get("anchor", "none") == "required":
            return None  # a required anchor was omitted — not a valid name
        if type_def.get("axis", "none") == "required":
            return None  # a required axis was omitted — not a valid name
        return {"levels": levels, "type": type_tok, "anchor": None, "axis": None,
                "end": None, "anchors": [], "axes": [], "ends": []}

    if "axis" in kinds:
        if type_def.get("axis", "none") not in ("required", "optional"):
            return None
        return {"levels": levels, "type": type_tok, "anchor": None, "axis": tail[0],
                "end": None, "anchors": [], "axes": list(tail), "ends": []}

    if type_def.get("anchor", "none") != "required":
        return None
    axes = _unique([vocab["anchors"][w]["axis"] for w in tail])
    ends = [vocab["anchors"][w]["end"] for w in tail]
    return {"levels": levels, "type": type_tok, "anchor": tail[0], "axis": axes[0],
            "end": ends[0], "anchors": list(tail), "axes": axes, "ends": ends}


def tail_words(parts):
    """The anchor/axis words a composed name ends with — several on a multi type,
    one or none otherwise. Takes the plural fields when they are there, so a caller
    that only ever set `anchor`/`axis` still composes."""
    # Named `tail` rather than `words`: words() is a module function now, and a local
    # of that name here reads as a shadow of it every time this is opened.
    tail = parts.get("anchors") or ([parts["anchor"]] if parts.get("anchor") else [])
    if not tail:
        tail = parts.get("axes") or ([parts["axis"]] if parts.get("axis") else [])
    return list(tail)


def compose(parts):
    """The parts -> the real Fusion name."""
    lv = list(parts["levels"])
    head = FUSION_DELIM.join(lv)
    if len(lv) >= 2 and parts.get("lastIs") == "name":
        head = FUSION_DELIM.join(lv[:-1]) + FUSION_DELIM * 2 + lv[-1]
    elif len(lv) >= 2 and parts.get("lastIs") == "group":
        head += FUSION_DELIM   # joined on below: the double underscore after it
    return FUSION_DELIM.join([head, parts["type"]] + tail_words(parts))


def display(parts):
    """The parts -> the delimited UI form the user picks."""
    out = LEVEL_DELIM.join(parts["levels"]) + SEGMENT_DELIM + parts["type"]
    tail = tail_words(parts)
    if tail:
        out += SEGMENT_DELIM + LEVEL_DELIM.join(tail)
    return out


# ── The spoken form ─────────────────────────────────────────────────────────
# display() above is the DELIMITED form — the one the stepper's crumb shows while a
# name is being built, delimiters and all, because there it is the delimiters you are
# choosing. What the Browser draws is a different thing: the name read out loud.
#
#     Drawer_Front_Curve_Inset_Back
#     └ owner ┘└group┘└ name ┘└ type ┘└anchor┘
#          ↓       ↓      └──────┬───────┘
#     the component a group     the row
#
# So the group levels never appear in a label at all (they ARE the groups), and what
# is left is punctuated the way text is: " - " between segments, ", " between several
# directions, and a space wherever a word was joined to the next. The direction is
# left off the row unless a sibling needs it (display_leaf's `direction`) — that
# choice is the Browser's.
#
#     Drawer_Front_Curve_Inset_Back  ->  Drawer > Front > "Curve - Inset"
#     Drawer_Width_Span_X            ->  Drawer > "Width - Span"
#     ...and beside it Drawer_Width_Span_Y  ->  "Width - Span - X", "Width - Span - Y"
#     BootCeiling_Handle_Radius      ->  Boot Ceiling > "Handle - Radius"
#
# words() is the inverse of capitalise.js's data-joinwords field: "boot ceiling" was
# typed and joined into BootCeiling on the way in, so it is split back at the same
# seams on the way out. Best-effort and DISPLAY ONLY — nothing ever composes a Fusion
# name from it, so an acronym it fails to split ("XY") or a digit it leaves attached
# ("Panel2") costs a reading, never a name.

def words(token):
    """One name token -> its words, spaced and each capitalised. An underscore inside
    a token (identifier() lets them through) is a seam too."""
    text = re.sub(r"_+", " ", token or "")
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)      # bootCeiling -> boot Ceiling
    text = re.sub(r"([a-z])([0-9])", r"\1 \2", text)         # Panel2      -> Panel 2
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)   # IDNumber    -> ID Number
    return WORD_JOIN.join(w[0].upper() + w[1:] for w in text.split() if w)


def display_word(word):
    """One level word as its group row reads: "BootCeiling" -> "Boot Ceiling"."""
    return words(word)


def type_group_at(parts):
    """The index of the level that IS the parameter's own type word — "Make as group"
    (fusion_value's stepper) writes the type into the level chain itself
    (Owner_Shaft_Span_Entry_Span_X) rather than a sidecar record, so the Browser can
    still read the whole tree off the names alone. -1 if none."""
    levels = parts["levels"]
    for i in range(1, len(levels)):
        if levels[i] == parts["type"]:
            return i
    return -1


def name_word(parts, index=None):
    """The parameter's own NAME word — the last level, typed just before the type — or
    None when it isn't one. The first level is the owner component; everything after
    it is a group UNLESS there are two or more entries after the owner, in which case
    the LAST one is the name, not a group (Drawer_Shelf_Width has no name word; Shelf
    is the group it sits in — Drawer_Shelf_Curve_Width does: Shelf is the group, Curve
    is the name). A name ending in its own type word (type_group_at) never offers that
    level as the name.

    `index` (optional, from name_index() below): when TWO OR MORE parsed parameters
    share the exact same level chain, that shared last level is promoted to a GROUP
    instead. Positional and display-side only: compose()/parse() don't change."""
    levels = parts["levels"]
    last = len(levels) - 1
    if parts.get("lastIs") == "group":
        return None    # pinned (see parse) — beats both rules
    if type_group_at(parts) == last:
        return None
    if parts.get("lastIs") == "name" and last >= 1:
        return levels[last]
    if last < 2:
        return None
    if index is not None:
        key = tuple(levels)
        if index.get(key, 0) > 1:
            return None
    return levels[last]


def group_levels(parts, index=None):
    """The levels that ARE groups — the owner and the chain after it, name left off."""
    name = name_word(parts, index)
    return list(parts["levels"][:-1] if name else parts["levels"])


def name_index(parsed_list):
    """Count of parsed {levels, ...} dicts sharing each exact level chain — feed to
    name_word()/group_levels() so two parameters that would each claim the same word
    as their own name instead share it as a group."""
    out = {}
    for parts in parsed_list:
        key = tuple(parts["levels"])
        out[key] = out.get(key, 0) + 1
    return out


def span_words_for(parts, vocab):
    """Span's own axis, read back as words (Width/Depth/Height) instead of a trailing
    direction. None for any other type; [] for a Span with no axis."""
    if not vocab.get("spanType") or parts["type"] != vocab["spanType"]:
        return None
    axes = parts.get("axes") or ([parts["axis"]] if parts.get("axis") else [])
    return [vocab["spanAxisToWord"].get(a, a) for a in axes]


def leaf_shown(parts, vocab, direction=True, index=None, show=None):
    """Which parts of the row are said — {name, type, dir}, each True/False, or None
    when the name has no such part. Mirrors leafShown() in param_naming.js: the
    defaults below, with `show` ({name?, type?, dir?} booleans) overriding any part
    the user chose to show or hide."""
    grouped = type_group_at(parts) >= 0
    span_w = span_words_for(parts, vocab)
    has_dir = bool(span_w) if span_w is not None else bool(tail_words(parts))
    auto = {
        "name": True if name_word(parts, index) else None,
        "type": not grouped and not span_w,
        "dir": None if not has_dir else (True if span_w is not None else bool(direction)),
    }
    show = show or {}
    return {k: (None if auto[k] is None
                else show[k] if isinstance(show.get(k), bool) else auto[k])
            for k in ("name", "type", "dir")}


def display_leaf(parts, vocab, direction=True, index=None, show=None):
    """Everything BELOW the groups — the name word, the type (or Span's axis words)
    and the direction(s) — as the row itself reads. This is the parameter's label in
    the Browser; the groups are what it is sitting in, so they are deliberately not in
    it. direction=False leaves the anchor/axis out: the Browser only shows it when a
    sibling differs by nothing else. A TYPE-GROUPED name (type_group_at) leaves the
    type word off the row too — the group it is sitting in already says it — except
    Span's axis words, which are never merely the type spelled out. `show` overrides
    any of it per part (leaf_shown)."""
    shown = leaf_shown(parts, vocab, direction, index, show)
    name = name_word(parts, index)
    out = [words(name)] if name and shown["name"] else []
    fused_map = vocab.get("anchorToFused", {}).get(parts["type"])
    anchors = parts.get("anchors") or []
    if (fused_map and len(anchors) == 1
            and shown["type"] and shown["dir"] and fused_map.get(anchors[0])):
        out.append(words(fused_map[anchors[0]]))      # Inset + Back -> "Backset"
    else:
        if shown["type"]:
            out.append(words(parts["type"]))
        if shown["dir"]:
            span_w = span_words_for(parts, vocab)
            tail = span_w if span_w is not None else tail_words(parts)
            out.append(TAIL_JOIN.join(words(w) for w in tail))
    if not out:
        out.append(words(parts["type"]))
    return SEG_JOIN.join(out)


def display_spoken(parts, vocab, index=None):
    """The whole name spoken, groups included — for a tooltip, or anywhere a row is
    shown out of its tree and has to say where it belongs."""
    return SEG_JOIN.join([words(w) for w in group_levels(parts, index)]
                          + [display_leaf(parts, vocab, index=index)])


def parse_leaf(text, vocab, keep=None):
    """The inverse of display_leaf: what the user typed back into a leaf's rename box,
    as {'type', 'anchors', 'axes'} — or None if it isn't a leaf this convention knows.

    Spacing is forgiving (any run of spaces round the delimiters), and each word is
    re-joined the way capitalise.js would have joined it, so "inset - back" and
    "Inset-Back" both come back as the Inset/Back leaf.

    It returns the PARTS, not a name: the groups belong to wherever the row is filed,
    and only the caller knows that. 'name' is the name word ("Curve - Inset" ->
    "Curve"), or None when the text starts at the type. The second segment decides it
    — a type word there means the first was a name; type words and direction words are
    disjoint, so this never guesses.

    `keep` (optional) is the parameter's current parts. The Browser hides a direction
    no sibling needs, so a leaf typed back without one keeps the one it had — if the
    new type still takes that kind (anchor or axis). A TYPE-GROUPED leaf (see
    type_group_at) never carries its type in the text at all — the group already says
    it — so keep['type'] supplies it and the text is read as name/tail only."""
    segs = [s.strip() for s in re.split(r"\s*-\s*", str(text or "")) if s.strip()]
    if not segs:
        return None

    def join(s):
        return "".join(w[0].upper() + w[1:] for w in s.split() if w)

    def span_axes_of(s):
        if not vocab.get("spanType"):
            return None
        words_ = [w for w in (join(x) for x in re.split(r"\s*,\s*", s)) if w]
        if not words_:
            return None
        axes = [vocab["spanWordToAxis"].get(w) for w in words_]
        return axes if all(axes) else None

    grouped = bool(keep and type_group_at(keep) >= 0)
    # A fused word ("Backset") is the type + anchor said as one: split it back.
    i = 0
    while i < len(segs):
        f = vocab.get("fusedToAnchor", {}).get(join(segs[i]))
        if f:
            if grouped:
                if keep["type"] == f[0]:
                    segs[i:i + 1] = [f[1]]
            else:
                segs[i:i + 1] = [f[0], f[1]]
                i += 1
        i += 1
    name = None
    if grouped:
        type_tok = keep["type"]

        def looks_like_tail(s):
            if span_axes_of(s):
                return True
            ws = [w for w in (join(x) for x in re.split(r"\s*,\s*", s)) if w]
            return all(w in vocab["anchors"] or w in vocab["axes"] for w in ws)

        if len(segs) > 1 or not looks_like_tail(segs[0]):
            name = identifier(join(segs.pop(0)))
    else:
        if len(segs) > 1 and (join(segs[1]) in vocab["types"] or span_axes_of(segs[1])):
            name = identifier(join(segs.pop(0)))
        if join(segs[0]) in vocab["types"]:
            type_tok = join(segs.pop(0))
        elif span_axes_of(segs[0]):
            # The axis words stay in segs[0] — read below, translated the same as any
            # grouped Span tail.
            type_tok = vocab["spanType"]
        else:
            return None

    type_def = vocab["types"].get(type_tok)
    if not type_def:
        return None
    # Span's own axis words translate to their letters here, same for a grouped tail
    # (no type in the text at all) as for an ordinary one — harmless for any other
    # type, since Width/Depth/Height are never anchor or axis words themselves.
    tail = [vocab["spanWordToAxis"].get(w, w)
            for w in (join(s) for s in re.split(r"\s*,\s*", TAIL_JOIN.join(segs))) if w]
    if not tail and keep:
        kept = tail_words(keep)
        if type_def.get("anchor", "none") == "required":
            fits = all(w in vocab["anchors"] for w in kept)
        elif type_def.get("axis", "none") in ("required", "optional"):
            fits = all(w in vocab["axes"] for w in kept)
        else:
            fits = False
        if fits and (len(kept) < 2 or type_def.get("multi")):
            tail = kept
    if not tail:
        if type_def.get("anchor", "none") == "required":
            return None
        if type_def.get("axis", "none") == "required":
            return None
        return {"name": name, "type": type_tok, "anchors": [], "axes": []}
    if len(tail) > 1 and not type_def.get("multi"):
        return None
    kinds = _unique(["anchor" if w in vocab["anchors"]
                     else "axis" if w in vocab["axes"] else "?" for w in tail])
    if len(kinds) > 1 or kinds[0] == "?":
        return None
    if kinds[0] == "axis":
        if type_def.get("axis", "none") not in ("required", "optional"):
            return None
        return {"name": name, "type": type_tok, "anchors": [], "axes": tail}
    if type_def.get("anchor", "none") != "required":
        return None
    return {"name": name, "type": type_tok, "anchors": tail, "axes": []}


def allowed_anchors(type_word, vocab, symmetry=None):
    """Anchor-word choices for `type_word`, or [] if it takes none.

    `symmetry`: optional {axis: bool}. On a symmetric axis the low-end word (Front,
    Left, Bottom) is dropped — only the high-end word (Back, Right, Top) is offered.
    """
    type_def = vocab["types"].get(type_word, {})
    if type_def.get("anchor", "none") != "required":
        return []
    out = []
    for word, a in vocab["anchors"].items():
        if symmetry and symmetry.get(a["axis"]) and a["end"] == "low":
            continue
        out.append(word)
    return out


def type_def(type_word, vocab):
    """The type's own definition dict, or None — for callers that need a field
    parse()/allowed_anchors()/allowed_axes() don't already expose (e.g. multi)."""
    return vocab["types"].get(type_word)


def takes_many(type_word, vocab):
    """Does this type let more than one anchor/axis be chosen?"""
    return bool((vocab["types"].get(type_word) or {}).get("multi"))


def allowed_axes(type_word, vocab):
    """Axis-letter choices for `type_word` ('required' or 'optional'), else []."""
    type_def = vocab["types"].get(type_word, {})
    if type_def.get("axis", "none") not in ("required", "optional"):
        return []
    return sorted(vocab["axes"])


def types_for_unit(unit, vocab):
    """Type words whose units include `unit` ('' for unitless). Unknown unit -> all."""
    if unit is None:
        return list(vocab["types"].keys())
    out = [w for w, t in vocab["types"].items() if unit in t.get("units", [])]
    return out or list(vocab["types"].keys())


# ── When a type is offered, and when a name is offered back ──────────────────
#
# One rule set, two questions. Naming a value asks "which types belong in THIS
# situation?" (suggests); completing an expression asks "does this existing
# parameter belong in it?" (fits). Both read the type's own `suggest` block, so the
# answer to the two can never drift apart — and both follow the house rule that only
# a STATED disagreement filters: a situation that doesn't know its datum, or its
# axis, disqualifies nothing.
#
# A situation is {axis, end, datum, axes, ends}: axis/end are the dimension's nearest
# world axis and which side of it (kept for single-axis callers); axes/ends are the
# fuller answer — EVERY world axis the dimension is within 45 degrees of (45 itself
# included, so an exactly-diagonal dimension is in both neighbouring pools) and, per
# axis, which end. datum is what it is measured FROM ('origin' | 'bounds' | '' for
# unknown).


def _suggest(type_def_):
    s = (type_def_ or {}).get("suggest") or {}
    return {"datum": s.get("datum", "any"), "axis": s.get("axis", "same")}


def _situation_faces(situation):
    return [str(f).lower() for f in ((situation or {}).get("faces") or [])]


def _datum_ok(type_def_, situation):
    """`faces`: bound faces sitting AT an origin datum — the dimension is measured
    from both at once, so a bounds type passes too."""
    want = _suggest(type_def_)["datum"]
    have = (situation or {}).get("datum") or ""
    if want == "bounds" and _situation_faces(situation):
        return True
    return want == "any" or not have or want == have


def suggests(type_word, situation, vocab):
    """Is this TYPE worth offering in `situation`? The datum rule alone — which axis
    the value ends up on is the next step's question, not this one's."""
    td = vocab["types"].get(type_word)
    if td is None:
        return False
    return _datum_ok(td, situation)


def suggested_types(situation, unit, vocab):
    """types_for_unit narrowed to the ones this situation is for, in file order.
    Falls back to the unit's full list rather than offering nothing."""
    words = types_for_unit(unit, vocab)
    out = [w for w in words if suggests(w, situation, vocab)]
    return out or words


def fits(parts, situation, vocab):
    """Does an already-named parameter belong in `situation`? Its type's datum rule,
    then — unless the type is offered on any axis — a parameter with NO axis of its
    own (Span/Thickness left on the optional axis step) is general and always fits;
    one that does fits when it shares ANY axis with the dimension's 45° pool
    (situation['axes']; any ONE of a multi name's axes will do), and for an anchored
    type at the same end on that shared axis as well."""
    if not parts:
        return True
    td = vocab["types"].get(parts.get("type"))
    if td is None:
        return True
    if not _datum_ok(td, situation):
        return False
    if _suggest(td)["axis"] == "any":
        return True
    situation = situation or {}
    situ_axis = situation.get("axis") or ""
    situ_axes = situation.get("axes") or ([situ_axis] if situ_axis else [])
    situ_ends = dict({situ_axis: situation.get("end") or ""} if situ_axis else {},
                      **(situation.get("ends") or {}))
    axes = parts.get("axes") or ([parts["axis"]] if parts.get("axis") else [])
    if not axes:
        return True             # general — states no axis of its own
    if not situ_axes:
        return True              # the situation states no opinion
    shared = [a for a in axes if a in situ_axes]
    if not shared:
        return False
    anchors = parts.get("anchors") or ([parts["anchor"]] if parts.get("anchor") else [])
    if not anchors:
        return True
    # An anchored name states an end for each of its axes. It fits if any anchor is
    # on a SHARED axis and at that axis's own end — unless that axis's end isn't
    # known, in which case a stated disagreement can't be found, so it passes.
    faces = _situation_faces(situation)
    for word in anchors:
        a = vocab["anchors"].get(word) or {}
        if a.get("axis") not in shared:
            continue
        # Anchored on the very face the dimension starts from: the end it runs toward
        # says nothing against it (an Inset from Top runs DOWN from it).
        if str(word).lower() in faces:
            return True
        end = situ_ends.get(a.get("axis"))
        if not end or a.get("end") == end:
            return True
    return False


def branches(parsed_list, prefix):
    """Next-level words under `prefix` (a list), from an iterable of already-parsed
    {levels, ...} dicts (already scoped to the caller's component)."""
    seen = []
    for parts in parsed_list:
        levels = parts["levels"]
        if len(levels) > len(prefix) and levels[: len(prefix)] == list(prefix):
            word = levels[len(prefix)]
            if word not in seen:
                seen.append(word)
    return seen
