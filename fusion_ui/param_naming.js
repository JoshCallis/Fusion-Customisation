/* param_naming.js — the structured parameter-naming convention, window side.
 *
 * A faithful mirror of param_naming.py. The add-ins use the Python; the companion
 * windows use this. Both read the SAME param_naming.json, and param_naming_selftest.py
 * checks the two agree on every exemplar name — so if you change one, change the other
 * and run the test. The file itself is edited in Control Room ▸ Fusion ▸ Parameter Names.
 *
 * A parameter name has three segments, joined with '_' in the real Fusion name:
 *
 *     BootCeiling_Front_Inset_Back
 *     └── feature levels ──┘ type  anchor
 *
 * Feature levels can be any words (including ones that also happen to be anchor or
 * type words — Front above is a LEVEL, not the anchor, because it isn't in trailing
 * position). Only the trailing tokens are ever classified: the vocabulary of type
 * words and the vocabulary of anchor/axis words are disjoint, so parsing is
 * unambiguous — take trailing anchor/axis words until a type word stops them.
 *
 * MORE THAN ONE ANCHOR OR AXIS. A type with `multi` lets a parameter say that two
 * directions both work — Drawer_Width_Span_X_Y is the span on X and on Y, and is
 * suggested on either. Every trailing word is of ONE kind: an anchor already carries
 * its axis, so anchors and axis letters are never mixed in one name.
 *
 * This is a separate, newer convention from variables.json's situational-tag engine
 * (naming.js / naming.py). The two are not merged: an old-convention name simply
 * fails to parse() here and is left alone.
 *
 * Usage:
 *     await ParamNaming.loadVocabulary("http://127.0.0.1:8734/param_naming.json");
 *     const parts = ParamNaming.parse("BootCeiling_Front_Inset_Back");
 *     // {levels: ["BootCeiling","Front"], type: "Inset", anchor: "Back", axis: "Y"}
 *     ParamNaming.compose(parts) === "BootCeiling_Front_Inset_Back"
 *     ParamNaming.display(parts) === "BootCeiling,Front-Inset-Back"
 *
 * And the SPOKEN form the Browser draws — the levels between the owner and the last
 * become groups; the last level (the NAME word), the type and the direction become the
 * row, punctuated like text (see "The spoken form" below):
 *
 *     ParamNaming.displayWord("BootCeiling") === "Boot Ceiling"
 *     ParamNaming.nameWord(parts)            === "Front"
 *     ParamNaming.displayLeaf(parts)         === "Front - Inset - Back"
 *     ParamNaming.displayLeaf(parts, {direction: false}) === "Front - Inset"
 *     ParamNaming.parseLeaf("Front - Inset - Back")
 *       // {name:"Front", type:"Inset", anchors:["Back"], axes:[]}
 */

const ParamNaming = (function () {
  "use strict";

  const FUSION_DELIM = "_";
  const LEVEL_DELIM = ",";
  const SEGMENT_DELIM = "-";

  let vocab = null;

  /* ── Vocabulary ─────────────────────────────────────────────────────────── */
  function setVocabulary(spec) {
    const types = {};
    for (const t of (spec && spec.types) || []) types[t.word] = t;
    const anchors = {};
    for (const a of (spec && spec.anchors) || []) anchors[a.word] = a;
    const axes = new Set((spec && spec.axes) || []);
    // spanWords: {type, X, Y, Z} — Span's own axis reads back as Width/Depth/Height
    // (see "The spoken form"). Reversed once here so displayLeaf/parseLeaf don't walk
    // the object either way every call.
    const sw = (spec && spec.spanWords) || null;
    const spanType = sw ? sw.type : null;
    const spanAxisToWord = {}, spanWordToAxis = {};
    if (sw) for (const k of Object.keys(sw)) {
      if (k === "type" || k === "_comment") continue;
      spanAxisToWord[k] = sw[k];
      spanWordToAxis[sw[k]] = k;
    }
    // fused: a type's `fused` map ({anchor: word}) — Inset + Back is SAID "Backset".
    // Display only; the Fusion name is still Inset_Back.
    const fusedType = {}, anchorToFused = {}, fusedToAnchor = {};
    for (const t of Object.values(types)) if (t.fused) {
      anchorToFused[t.word] = t.fused;
      for (const a of Object.keys(t.fused)) fusedToAnchor[t.fused[a]] = { type: t.word, anchor: a };
    }
    vocab = { spec, types, anchors, axes, spanType, spanAxisToWord, spanWordToAxis,
              anchorToFused, fusedToAnchor };
    return vocab;
  }

  async function loadVocabulary(url) {
    const r = await fetch(url, { cache: "no-store" });
    return setVocabulary(await r.json());
  }

  function ready() { return vocab !== null; }

  /* ── Sanitising ─────────────────────────────────────────────────────────── */
  // A raw string (typically a component name) reduced to something Fusion accepts
  // as a parameter name/level: strip everything outside [A-Za-z0-9_], then prefix
  // "Comp" if what's left is empty or starts with a digit. Mirrors Bounds.py's
  // _squash — that add-in-side helper predates this engine and stays independent.
  function identifier(text) {
    let out = (text || "").replace(/[^A-Za-z0-9_]/g, "");
    if (!out || !/[A-Za-z_]/.test(out[0])) out = "Comp" + out;
    return out;
  }

  // A name as TYPED -> the name Fusion is given: every space is a word seam, joined
  // the way capitalise.js's data-joinwords would have ("boot ceiling" -> "bootCeiling",
  // "Panel 2" -> "Panel2"). The first word keeps its case. Only whitespace is touched —
  // anything else invalid still reaches the caller's check. words() splits it back
  // for display, so the space the user typed is the space they see.
  function joinName(text) {
    return String(text || "").trim().split(/\s+/).filter(Boolean)
      .map((w, i) => i ? w.charAt(0).toUpperCase() + w.slice(1) : w).join("");
  }

  const unique = list => list.filter((x, i) => list.indexOf(x) === i);

  /* ── Parsing ────────────────────────────────────────────────────────────── */
  // {levels, type, anchor, axis, end, anchors, axes, ends} — the plural fields are
  // every anchor/axis the name carries, the singular ones the first of each (what a
  // caller that only ever deals in one of them still reads).
  /* THE PINNED LAST LEVEL. By default the last level is a name or a group by position
     and sharing (nameWord below). A DOUBLE underscore pins it, and says which: it is
     where the groups end. Drawer_Shelf__Curve_Width — the groups end before Curve, so
     Curve is the name, whatever else shares it; Drawer_Shelf_Curve__Width — they end
     after it, so Curve is a group even with one parameter in it. parse() reads it back
     as `lastIs` ("name" | "group", absent when unpinned), compose() writes it. Anywhere
     else a double underscore is simply a seam. Written by fusion_value's stepper
     ("Make name" / "Make group" on the last group chip). */
  const PIN = FUSION_DELIM + FUSION_DELIM;
  function parse(fusionName) {
    const raw = (fusionName || "").split(FUSION_DELIM);
    const out = parseTokens(raw.filter(Boolean));
    if (!out) return null;
    // How many real tokens come before the first empty one between two tokens.
    let mark = -1, seen = 0;
    for (let k = 0; k < raw.length; k++) {
      if (raw[k]) seen += 1;
      else if (seen > 0 && k < raw.length - 1 && mark < 0) mark = seen;
    }
    const n = out.levels.length;
    if (n >= 2 && mark === n - 1) out.lastIs = "name";
    else if (n >= 2 && mark === n) out.lastIs = "group";
    return out;
  }
  function parseTokens(tokens) {
    if (tokens.length < 2) return null;

    // Walk back over the trailing anchor/axis words. A type word stops the walk (the
    // two vocabularies are disjoint), and so does running out of tokens — a name is
    // at least one level and a type.
    const tail = [];
    let i = tokens.length;
    while (i > 2) {
      const word = tokens[i - 1];
      if (vocab.types[word]) break;
      if (!vocab.anchors[word] && !vocab.axes.has(word)) break;
      tail.unshift(word);
      i -= 1;
    }

    const typeTok = tokens[i - 1];
    const typeDef = vocab.types[typeTok];
    if (!typeDef) return null;
    const levels = tokens.slice(0, i - 1);
    if (!levels.length) return null;

    const kinds = unique(tail.map(w => (vocab.anchors[w] ? "anchor" : "axis")));
    if (kinds.length > 1) return null;   // an anchor already says its axis
    if (tail.length > 1 && !typeDef.multi) return null;  // one direction only

    if (!tail.length) {
      if ((typeDef.anchor || "none") === "required") return null;
      if ((typeDef.axis || "none") === "required") return null;
      return { levels, type: typeTok, anchor: null, axis: null, end: null,
               anchors: [], axes: [], ends: [] };
    }

    if (kinds[0] === "axis") {
      if (!["required", "optional"].includes(typeDef.axis || "none")) return null;
      return { levels, type: typeTok, anchor: null, axis: tail[0], end: null,
               anchors: [], axes: tail.slice(), ends: [] };
    }

    if ((typeDef.anchor || "none") !== "required") return null;
    const axes = unique(tail.map(w => vocab.anchors[w].axis));
    const ends = tail.map(w => vocab.anchors[w].end);
    return { levels, type: typeTok, anchor: tail[0], axis: axes[0], end: ends[0],
             anchors: tail.slice(), axes: axes, ends: ends };
  }

  /* ── Composing ──────────────────────────────────────────────────────────── */
  // The anchor/axis words a composed name ends with — several on a multi type, one or
  // none otherwise. Reads the plural fields when they are there, so a caller that only
  // ever set anchor/axis still composes.
  function tailWords(parts) {
    let words = (parts.anchors && parts.anchors.length) ? parts.anchors
              : (parts.anchor ? [parts.anchor] : []);
    if (!words.length) {
      words = (parts.axes && parts.axes.length) ? parts.axes
            : (parts.axis ? [parts.axis] : []);
    }
    return words.slice();
  }

  function compose(parts) {
    const lv = parts.levels;
    let head = lv.join(FUSION_DELIM);
    if (lv.length >= 2 && parts.lastIs === "name")
      head = lv.slice(0, -1).join(FUSION_DELIM) + PIN + lv[lv.length - 1];
    else if (lv.length >= 2 && parts.lastIs === "group")
      head += FUSION_DELIM;          // joined on below: the double underscore after it
    return [head, parts.type].concat(tailWords(parts)).join(FUSION_DELIM);
  }

  function display(parts) {
    let out = parts.levels.join(LEVEL_DELIM) + SEGMENT_DELIM + parts.type;
    const tail = tailWords(parts);
    if (tail.length) out += SEGMENT_DELIM + tail.join(LEVEL_DELIM);
    return out;
  }

  /* ── The spoken form ──────────────────────────────────────────────────────
     display() above is the DELIMITED form — the one the stepper's crumb shows
     while a name is being built, delimiters and all, because there it is the
     delimiters you are choosing. What the Browser draws is a different thing:
     the name read out loud.

     The real Fusion name is machine text — CamelCase, underscores, no spaces —
     and every seam in it means something the browser already shows another way:

         Drawer_Front_Curve_Inset_Back
         └ owner ┘└group┘└ name ┘└ type ┘└anchor┘
              ↓       ↓      └──────┬───────┘
         the component a group     the row

     So the group levels never appear in a label at all (they ARE the groups), and
     what is left is punctuated the way text is: " - " between segments, ", " between
     several directions, and a space wherever a word was joined to the next. The
     direction is left off the row unless a sibling needs it (displayLeaf's
     `direction`) — that choice is the Browser's.

         Drawer_Front_Curve_Inset_Back  →  Drawer ▸ Front ▸ "Curve - Inset"
         Drawer_Width_Span_X            →  Drawer ▸ "Width - Span"
         …and beside it Drawer_Width_Span_Y  →  "Width - Span - X", "Width - Span - Y"
         BootCeiling_Handle_Radius      →  Boot Ceiling ▸ "Handle - Radius"

     words() is the inverse of capitalise.js's data-joinwords field: "boot ceiling"
     was typed and joined into BootCeiling on the way in, so it is split back at
     the same seams on the way out. Best-effort and DISPLAY ONLY — nothing ever
     composes a Fusion name from it, so an acronym it fails to split ("XY") costs a
     reading, never a name. A digit after a lowercase letter is a seam too — joinName
     turns a typed "Panel 2" into Panel2, and this is where the space comes back. */
  const WORD_JOIN = " ";
  const SEG_JOIN  = " - ";
  const TAIL_JOIN = ", ";

  // One name token -> its words, spaced and each capitalised. An underscore inside
  // a token (identifier() lets them through) is a seam too.
  function words(token) {
    return (token || "")
      .replace(/_+/g, " ")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")      // bootCeiling -> boot Ceiling
      .replace(/([a-z])([0-9])/g, "$1 $2")         // Panel2      -> Panel 2
      .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")   // IDNumber    -> ID Number
      .split(/\s+/).filter(Boolean)
      .map(w => w.charAt(0).toUpperCase() + w.slice(1))
      .join(WORD_JOIN);
  }

  // One level word as its group row reads: "BootCeiling" -> "Boot Ceiling".
  function displayWord(word) { return words(word); }

  /* THE TYPE-GROUP LEVEL. "Make as group" (fusion_value's stepper) writes the type
     word into the level chain itself — Owner_Shaft_Span_Entry_Span_X — rather than in
     a sidecar record, so the Browser can still read the whole tree off the names
     alone. Whichever level equals the parameter's own type word is that group; -1 if
     none. A level that happens to share a type's spelling by coincidence is read the
     same way — rare, and the stepper is what writes these, not a hand-typed level. */
  function typeGroupAt(parts) {
    for (let i = 1; i < parts.levels.length; i++) {
      if (parts.levels[i] === parts.type) return i;
    }
    return -1;
  }

  /* THE NAME WORD. The first level is the owner component; everything after it is a
     group UNLESS there are two or more entries after the owner, in which case the
     LAST one is the parameter's own NAME, not a group — it belongs to the row, not
     the tree. One entry after the owner is always just a group — Drawer_Shelf_Width
     has no name word, Shelf is the group it sits in — but Drawer_Shelf_Curve_Width
     does: Shelf is the group, Curve is the name. A name that ends in its own type
     word (typeGroupAt) never offers that level as the name — there is nothing left
     to be a name of.

     `index` (optional, from nameIndex() below): when TWO OR MORE parsed parameters
     share the exact same level chain, that shared last level is promoted to a GROUP
     instead — "Entry" holds "Span X" and "Span Y" rather than each one calling
     itself Entry. Without an index this half is skipped (a lone name is still a
     name); every caller that draws more than one parameter at a time should pass
     one. Positional and display-side only: the Fusion name carries no marker beyond
     the type-group level, so compose() and parse() don't change for this rule. */
  function nameWord(parts, index) {
    const levels = parts.levels;
    const last = levels.length - 1;
    if (parts.lastIs === "group") return null;     // pinned (see PIN) — beats both rules
    if (typeGroupAt(parts) === last) return null;  // the type-group IS the last entry
    if (parts.lastIs === "name" && last >= 1) return levels[last];
    if (last < 2) return null;                    // owner + one entry: always a group
    if (index) {
      const key = levels.join("\u0000");
      if ((index[key] || 0) > 1) return null;      // shared with another parameter
    }
    return levels[last];
  }
  // The levels that ARE groups — the owner and the chain after it, the name left off.
  function groupLevels(parts, index) {
    const name = nameWord(parts, index);
    return name ? parts.levels.slice(0, -1) : parts.levels.slice();
  }

  // Count of parsed {levels,...} objects sharing each exact level chain — feed to
  // nameWord()/groupLevels() so two parameters that would otherwise each claim the
  // same word as their own name instead share it as a group. Build once per render,
  // not per row: a caller drawing N parameters that each called nameWord() without
  // this would be answering "is this shared?" by re-scanning all N, N times over.
  function nameIndex(parsedList) {
    const out = {};
    for (const parts of parsedList) {
      const key = parts.levels.join("\u0000");
      out[key] = (out[key] || 0) + 1;
    }
    return out;
  }

  // Span's own axis, read back as words (Width/Depth/Height) instead of shown as a
  // trailing direction — "Width - Span" would say nothing an anchor-carrying name
  // doesn't already say better. null for any other type; [] for a Span with no axis
  // (still spelled out as "Span" by displayLeaf).
  function spanWordsFor(parts) {
    if (!vocab.spanType || parts.type !== vocab.spanType) return null;
    const axes = (parts.axes && parts.axes.length) ? parts.axes
               : (parts.axis ? [parts.axis] : []);
    return axes.map(a => vocab.spanAxisToWord[a] || a);
  }

  // Everything BELOW the groups — the name word, the type (or Span's axis words) and
  // the direction(s) — as the row itself reads. This is the parameter's label in the
  // Browser; the groups are what it is sitting in, so they are deliberately not in
  // it. `direction: false` leaves the anchor/axis out: the Browser only shows it when
  // a sibling differs by nothing else. `index`, see nameWord(). A TYPE-GROUPED name
  // (typeGroupAt) leaves the type word off the row too — the group it is sitting in
  // already says it — except Span's axis words, which are never merely the type
  // spelled out and stay even when grouped.
  function displayLeaf(parts, opts) {
    opts = opts || {};
    const shown = leafShown(parts, opts);
    const out = [];
    const name = nameWord(parts, opts.index);
    if (name && shown.name) out.push(words(name));
    const fusedMap = vocab.anchorToFused[parts.type];
    const one = (parts.anchors || []).length === 1;
    if (fusedMap && one && shown.type && shown.dir && fusedMap[parts.anchors[0]]) {
      out.push(words(fusedMap[parts.anchors[0]]));      // Inset + Back → "Backset"
    } else {
      if (shown.type) out.push(words(parts.type));
      if (shown.dir) {
        const spanW = spanWordsFor(parts);
        const tail = spanW || tailWords(parts);
        out.push(tail.map(words).join(TAIL_JOIN));
      }
    }
    if (!out.length) out.push(words(parts.type));  // nothing else survived: fall back
    return out.join(SEG_JOIN);
  }

  /* WHICH PARTS OF THE ROW ARE SAID — {name, type, dir}, each true (said), false (left
     off) or null (the name has no such part: no name word, no direction). The rules
     above decide by default: the name word always, the type unless it is grouped (or is
     a Span whose axis words say it), the direction only where a twin needs it (Span's
     axis words always). `opts.show` — {name?, type?, dir?}, each true/false — overrides
     a part the user chose to show or hide (the stepper's ⌘E; the Browser keeps them per
     parameter). `opts.show` absent is the default reading, unchanged. */
  function leafShown(parts, opts) {
    opts = opts || {};
    const grouped = typeGroupAt(parts) >= 0;
    const spanW = spanWordsFor(parts);
    const hasDir = spanW ? spanW.length > 0 : tailWords(parts).length > 0;
    const auto = {
      name: nameWord(parts, opts.index) ? true : null,
      type: !grouped && !(spanW && spanW.length),
      dir: !hasDir ? null : (spanW ? true : opts.direction !== false),
    };
    const ov = opts.show || {};
    const out = {};
    for (const k of ["name", "type", "dir"])
      out[k] = auto[k] === null ? null : (typeof ov[k] === "boolean" ? ov[k] : auto[k]);
    return out;
  }

  // The whole name spoken, groups included — for a tooltip, or anywhere a row is
  // shown out of its tree and has to say where it belongs.
  function displaySpoken(parts) {
    return groupLevels(parts).map(words).concat([displayLeaf(parts)]).join(SEG_JOIN);
  }

  /* The inverse of displayLeaf: what the user typed back into a leaf's rename box,
     as {type, anchors, axes} — or null if it isn't a leaf this convention knows.
     Spacing is forgiving (any run of spaces round the delimiters), and each word is
     re-joined the way capitalise.js would have joined it, so "inset - back" and
     "Inset-Back" both come back as the Inset/Back leaf.

     It returns the PARTS, not a name: the groups belong to wherever the row is
     filed, and only the caller knows that. `name` is the name word ("Curve - Inset"
     -> "Curve"), or null when the text starts at the type. The second segment decides
     it — a type word there means the first was a name; type words and direction
     words are disjoint, so this never guesses.

     `keep` (optional) is the parameter's current parts. The Browser hides a direction
     that no sibling needs, so a leaf typed back without one keeps the one it had — if
     the new type still takes that kind (anchor or axis). A TYPE-GROUPED leaf (see
     typeGroupAt) never carries its type in the text at all — the group already says
     it — so `keep.type` supplies it and the text is read as name/tail only; a Span
     read back this way still takes its axis as Width/Depth/Height, same as ungrouped. */
  function parseLeaf(text, keep) {
    const segs = String(text || "").split(/\s*-\s*/).map(s => s.trim()).filter(Boolean);
    if (!segs.length) return null;
    const join = s => s.split(/\s+/).filter(Boolean)
                       .map(w => w.charAt(0).toUpperCase() + w.slice(1)).join("");
    // A segment naming Span's own axis word(s) — "Width" or "Width, Depth" — same
    // question typeDefn asks of an ordinary type word, answered against spanWordToAxis
    // instead. Returns the axis letters, or null if the segment isn't purely that.
    const spanAxesOf = s => {
      if (!vocab.spanType) return null;
      const words_ = s.split(/\s*,\s*/).map(join).filter(Boolean);
      if (!words_.length) return null;
      const axes = words_.map(w => vocab.spanWordToAxis[w]);
      return axes.every(Boolean) ? axes : null;
    };
    const grouped = !!(keep && typeGroupAt(keep) >= 0);
    // A fused word ("Backset") is the type + anchor said as one: split it back.
    for (let i = 0; i < segs.length; i++) {
      const f = vocab.fusedToAnchor[join(segs[i])];
      if (!f) continue;
      if (grouped) { if (keep.type === f.type) segs.splice(i, 1, f.anchor); }
      else segs.splice(i, 1, f.type, f.anchor);
    }

    let name = null, typeTok = null;
    if (grouped) {
      typeTok = keep.type;
      // One remaining segment is the NAME unless it reads as this type's own tail (an
      // axis/anchor word, or Span's axis words) — then it IS the tail and there is no
      // name. Two or more segments: the first is always the name.
      const looksLikeTail = s => spanAxesOf(s) ||
        s.split(/\s*,\s*/).map(join).filter(Boolean).every(w => vocab.anchors[w] || vocab.axes.has(w));
      if (segs.length > 1 || !looksLikeTail(segs[0])) name = identifier(join(segs.shift()));
    } else {
      if (segs.length > 1 && (vocab.types[join(segs[1])] || spanAxesOf(segs[1])))
        name = identifier(join(segs.shift()));
      if (vocab.types[join(segs[0])]) {
        typeTok = join(segs.shift());
      } else if (spanAxesOf(segs[0])) {
        typeTok = vocab.spanType;   // the axis words stay in segs[0] — read below,
                                     // translated the same as any grouped Span tail.
      } else {
        return null;
      }
    }
    const typeDefn = vocab.types[typeTok];
    if (!typeDefn) return null;
    // Span's own axis words translate to their letters here, same for a grouped tail
    // (no type in the text at all) as for an ordinary one — harmless for any other
    // type, since Width/Depth/Height are never anchor or axis words themselves.
    let tail = segs.join(TAIL_JOIN)
                   .split(/\s*,\s*/).map(join).filter(Boolean)
                   .map(w => vocab.spanWordToAxis[w] || w);
    if (!tail.length && keep) {
      const kept = tailWords(keep);
      const fits = (typeDefn.anchor || "none") === "required" ? kept.every(w => vocab.anchors[w])
                 : ["required", "optional"].includes(typeDefn.axis || "none")
                   ? kept.every(w => vocab.axes.has(w)) : false;
      if (fits && (kept.length < 2 || typeDefn.multi)) tail = kept;
    }
    if (!tail.length) {
      if ((typeDefn.anchor || "none") === "required") return null;
      if ((typeDefn.axis || "none") === "required") return null;
      return { name, type: typeTok, anchors: [], axes: [] };
    }
    if (tail.length > 1 && !typeDefn.multi) return null;
    const kinds = unique(tail.map(w => vocab.anchors[w] ? "anchor"
                                     : vocab.axes.has(w) ? "axis" : "?"));
    if (kinds.length > 1 || kinds[0] === "?") return null;
    if (kinds[0] === "axis") {
      if (!["required", "optional"].includes(typeDefn.axis || "none")) return null;
      return { name, type: typeTok, anchors: [], axes: tail };
    }
    if ((typeDefn.anchor || "none") !== "required") return null;
    return { name, type: typeTok, anchors: tail, axes: [] };
  }

  /* ── Filtering for the stepper ──────────────────────────────────────────── */
  // On a symmetric axis the low-end word (Front, Left, Bottom) is dropped — only the
  // high-end word (Back, Right, Top) is offered. `symmetry`: {axis: bool} or null.
  function allowedAnchors(typeWord, symmetry) {
    const typeDef = vocab.types[typeWord] || {};
    if ((typeDef.anchor || "none") !== "required") return [];
    const out = [];
    for (const word in vocab.anchors) {
      const a = vocab.anchors[word];
      if (symmetry && symmetry[a.axis] && a.end === "low") continue;
      out.push(word);
    }
    return out;
  }

  // The type's own definition object, or null — for callers that need a field
  // parse()/allowedAnchors()/allowedAxes() don't already expose (e.g. multi).
  function typeDef(typeWord) {
    return vocab.types[typeWord] || null;
  }

  // Does this type let more than one anchor/axis be chosen?
  function takesMany(typeWord) {
    return !!((vocab.types[typeWord] || {}).multi);
  }

  function allowedAxes(typeWord) {
    const typeDef = vocab.types[typeWord] || {};
    if (!["required", "optional"].includes(typeDef.axis || "none")) return [];
    return Array.from(vocab.axes).sort();
  }

  // Type words whose units include `unit` ('' for unitless). Unknown unit -> all.
  function typesForUnit(unit) {
    const all = Object.keys(vocab.types);
    if (unit === null || unit === undefined) return all;
    const out = all.filter(w => (vocab.types[w].units || []).includes(unit));
    return out.length ? out : all;
  }

  /* ── When a type is offered, and when a name is offered back ──────────────
     One rule set, two questions. Naming a value asks "which types belong in THIS
     situation?" (suggests); completing an expression asks "does this existing
     parameter belong in it?" (fits). Both read the type's own `suggest` block, so
     the answer to the two can never drift apart — and both follow the house rule
     that only a STATED disagreement filters: a situation that doesn't know its
     datum, or its axis, disqualifies nothing.

     A situation is {axis, end, datum, axes, ends}: axis/end are the dimension's
     nearest world axis and which side of it (kept for the single-axis callers this
     predates); axes/ends are the fuller answer — EVERY world axis the dimension is
     within 45° of (45° itself included, so an exactly-diagonal dimension is in both
     neighbouring pools) and, per axis, which end. `datum` is what it is measured FROM
     ('origin' | 'bounds' | '' for unknown). Only axis/axis is required — a caller with
     just the single-axis fields still gets the same answer for that one axis. */
  function suggestOf(td) {
    const s = (td && td.suggest) || {};
    return { datum: s.datum || "any", axis: s.axis || "same" };
  }

  // `faces`: bound faces sitting AT an origin datum — the dimension is measured from
  // both at once, so a bounds type passes too.
  function datumOk(td, situation) {
    const want = suggestOf(td).datum;
    const have = (situation && situation.datum) || "";
    if (want === "bounds" && situationFaces(situation).length) return true;
    return want === "any" || !have || want === have;
  }

  function situationFaces(situation) {
    return ((situation && situation.faces) || []).map(f => String(f).toLowerCase());
  }

  // Is this TYPE worth offering in `situation`? The datum rule alone — which axis the
  // value ends up on is the next step's question, not this one's.
  function suggests(typeWord, situation) {
    const td = vocab.types[typeWord];
    if (!td) return false;
    return datumOk(td, situation);
  }

  // typesForUnit narrowed to the ones this situation is for, in file order. Falls
  // back to the unit's full list rather than offering nothing.
  function suggestedTypes(situation, unit) {
    const words = typesForUnit(unit);
    const out = words.filter(w => suggests(w, situation));
    return out.length ? out : words;
  }

  // Does an already-named parameter belong in `situation`? Its type's datum rule,
  // then — unless the type is offered on any axis — a parameter that carries NO axis
  // of its own (Span/Thickness left on the optional axis step) is general and always
  // fits; one that does fits when it shares ANY axis with the dimension's 45° pool
  // (any ONE of a multi name's axes will do), and for an anchored type at the same
  // end on that shared axis as well.
  function fits(parts, situation) {
    if (!parts) return true;
    const td = vocab.types[parts.type];
    if (!td) return true;
    if (!datumOk(td, situation)) return false;
    if (suggestOf(td).axis === "any") return true;
    const situAxis = (situation && situation.axis) || "";
    const situAxes = (situation && situation.axes && situation.axes.length) ? situation.axes
                    : (situAxis ? [situAxis] : []);
    const situEnds = Object.assign({}, situAxis ? { [situAxis]: (situation && situation.end) || "" } : {},
                                    (situation && situation.ends) || {});
    const axes = (parts.axes && parts.axes.length) ? parts.axes
               : (parts.axis ? [parts.axis] : []);
    if (!axes.length) return true;              // general — states no axis of its own
    if (!situAxes.length) return true;           // the situation states no opinion
    const shared = axes.filter(a => situAxes.includes(a));
    if (!shared.length) return false;
    const anchors = (parts.anchors && parts.anchors.length) ? parts.anchors
                  : (parts.anchor ? [parts.anchor] : []);
    if (!anchors.length) return true;
    // An anchored name states an end for each of its axes. It fits if any anchor is
    // on a SHARED axis and at that axis's own end — unless that axis's end isn't
    // known, in which case a stated disagreement can't be found, so it passes.
    const faces = situationFaces(situation);
    return anchors.some(w => {
      const a = vocab.anchors[w] || {};
      if (!shared.includes(a.axis)) return false;
      // Anchored on the very face the dimension starts from: the end the dimension
      // runs toward says nothing against it (an Inset from Top runs DOWN from it).
      if (faces.includes(String(w).toLowerCase())) return true;
      const end = situEnds[a.axis];
      return !end || a.end === end;
    });
  }

  // Next-level words under `prefix` (an array), from an array of already-parsed
  // {levels, ...} objects (already scoped to the caller's component).
  function branches(parsedList, prefix) {
    const seen = [];
    for (const parts of parsedList) {
      const levels = parts.levels;
      if (levels.length > prefix.length &&
          prefix.every((w, i) => levels[i] === w)) {
        const word = levels[prefix.length];
        if (!seen.includes(word)) seen.push(word);
      }
    }
    return seen;
  }

  return {
    setVocabulary, loadVocabulary, ready,
    identifier, joinName,
    parse, compose, display, tailWords,
    words, displayWord, displayLeaf, leafShown, displaySpoken, parseLeaf, nameWord, groupLevels,
    nameIndex, typeGroupAt, spanWordsFor,
    allowedAnchors, allowedAxes, typesForUnit, typeDef, takesMany,
    suggests, suggestedTypes, fits, branches
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = ParamNaming;
