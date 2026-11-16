/* ─────────────────────────────────────────────────────────────────────────────
   chips.js — expressions typed as ITEMS rather than as a line of characters.

   Shared by the companion windows, exactly as theme.css and naming.js are: the
   window's server serves this file, the page calls Chips.attach() on each of its
   expression fields, and everything below is about text — so the Params and Browser
   windows can adopt it later without any of it moving.

   THE IDEA
   ────────
   A parameter name in an expression is not really text: it is a THING. Once
   "PowerboardWidth" is in the box there is no sense in which its eleventh character
   can be edited — you either mean that parameter or you don't. So the moment what
   you have typed is a name that exists, it stops being letters and becomes an item:
   one object, deleted whole, dragged whole. Same for the constants and functions,
   which are things Fusion knows about rather than words you are spelling.

   HOW IT IS BUILT — and why it is not a contenteditable
   ────────────────────────────────────────────────────
   The field stays an ordinary <textarea> (or <input>). Its text is the one truth,
   its caret is the real caret, its selection, wrapping and autosize are the
   browser's. What is added is a LAYER painted underneath it, holding exactly the
   same characters in exactly the same metrics, with the item runs wrapped in spans.
   The field's own text is then made transparent, so what you read is the layer and
   what you type into is the field.

   That is the whole trick, and it is what makes everything else cheap:

     · every line of the dialog that already speaks .value / .selectionStart keeps
       working, and so does the headless test harness, which has no layout at all;
     · a chip cannot desynchronise from the text, because it IS the text — the atoms
       are recomputed from the string on every render rather than remembered;
     · IME, spellcheck-off and the caret all behave natively.

   UNDO IS THE ONE THING THAT COULD NOT STAY THE BROWSER'S, because which "(" is a
   bubble is state beside the text rather than in it: see Undo below.

   The one rule this imposes: A CHIP MAY NOT CHANGE THE LAYOUT. If a chip were 3px
   wider than its letters, the layer and the field would drift apart and the caret
   would land in the wrong place. So chips are painted with a background, a radius
   and a box-shadow SPREAD — all of which paint outside the inline box without
   occupying any — and never with padding, borders or a different font size. See
   .chip in theme.css.

   WHEN A NAME BECOMES AN ITEM
   ───────────────────────────
   As soon as the caret is not in it. Typing "Wid|" is a word being spelled; typing
   the "h" of Width and then a space, a bracket or an operator ends the word and it
   becomes a chip. Accepting a suggestion seals it there and then (see seal below),
   because choosing from a list is a statement that you meant that parameter.

   A NUMBER IS AN ITEM TOO — 25, or 25 mm, which is one value and not two digits and a
   unit. It chips on the same rule: the space that ends it — and the unit is WRITTEN ON
   for you (opts.unit), because a bare 25 in a box measured in millimetres already means
   twenty-five millimetres and a chip should say what it means.

   THE SPACEBAR IS THE COMMIT KEY, AND NEVER A CHARACTER
   ────────────────────────────────────────────────────
   It never puts a space in the field. There is no use for one: the gaps between items
   are written by this file, not typed. What it does is say "I have finished saying
   that", and what that means depends on what is in front of it.

   A word being typed is COMMITTED. A parameter, a constant, a function or a unit stays
   and becomes a chip, with the caret just past it (not holding it), so the next
   keystroke starts the next item. One of the OPERATOR LETTERS — f d s a for + - * /, w e r o i u for the
   trig calls — becomes the operator it stands for, since no parameter is one letter
   long. And a word that is none of those is a typo with nothing left to become, so it
   goes: leaving it in means the expression fails to parse at the far end for a reason
   the box never showed.

   With nothing in front of it the space does nothing at all. With a chip SELECTED it is
   not a commit at all — see SELECTING IS NOT TYPING.

   TWO ITEMS SIDE BY SIDE ARE ADDED
   ────────────────────────────────
   "Width Depth" is a width and a depth, and the only thing that can mean is their
   sum. The "+" is written into the text rather than left implied, because an operator
   you cannot see is one you cannot drag, delete or change.

   CLICKING a chip selects it, whole. Clicking it again lets it go, leaving the caret at
   its end. Backspace is how you mean to be rid of it. Escape is not this file's key
   at all — see the note above onKeyDown — so it is always the dialog's own Escape,
   chip held or not.

   SELECTING IS NOT TYPING
   ───────────────────────
   With a chip held (or several ⌘/⇧-clicked) the box takes no text at all: a chip is a
   thing you pointed at, not a run of characters you happened to highlight. Four
   letters are commands instead — F opens it (a chip for editing, a bubble to walk its
   insides), Space lets go with the caret just past it (or bubbles several), A lets go
   with the caret at the end, D steps back out of a bubble. See onSelectingKey. And a
   letter typed with the caret parked at a chip's end starts a NEW item beside it —
   the only way into a chip's own letters is F.

   AND A PRESS ANYWHERE ELSE only moves the caret. Dragging across the box does not
   sweep a highlight over it: what is in the field is a row of things, not a paragraph,
   and the way to take hold of one of them is to click it. The press is answered by
   this file and the field never sees it. For the same reason the pointer is a POINTER
   over the chips, and an I-beam only where there is text to work on: an open chip, a
   word not yet committed, and the room past the last chip.

   A RIGHT-CLICK IS NOT THE FIELD'S EITHER. WebKit's own menu here is a text field's —
   Cut, Paste, Look Up, spelling — and none of it applies to a row of objects. It is
   refused outright, the chip under the pointer is taken hold of, and the page is asked
   to put up its own (opts.menu).

   THE ARROWS MOVE THE CARET, a whole item at a time. A chip has no inside to walk
   through, so the only places the caret can be are the gaps between the items — which
   is where the next thing you type goes, and is the reason you reached for the arrow.
   ⌘ WITH THEM takes hold of the items instead, one at a time: selecting is a deliberate
   act, the keyboard twin of clicking a chip. Inside a word being typed or a chip being
   edited both are the ordinary arrows, right up to its edge.

   SPACE HELD is a modifier, not a commit: with a chip held, Space+← / Space+→ MOVE
   that chip along the row, the same permutation dragging it makes.

   DRAGGING one never selects it: a press that turns into a drag is a move, and
   highlighting what you are moving is the wrong answer to "what did that press do".
   So the selection is decided on the way UP, and only if the pointer stayed put.

   FUNCTIONS ABSORB THEIR ARGUMENT
   ───────────────────────────────
   cos, sqrt and the rest take something after them, so the call and what is inside
   it are one item: cos(Taper) drags as a unit and deletes as a unit. The argument
   is still ordinary text inside that item, so clicking it opens it and typing
   changes which parameter is being cosined without disturbing the call.

   DRAGGING
   ────────
   An expression at one bracket level is a row of OPERANDS with OPERATORS between
   them. Dragging an item permutes the operands and leaves the operator skeleton
   where it is — "A + B * C" with C dragged to the front is "C + B * A", never
   "C A + B *". The text is rewritten live as you drag, so the preview is the real
   thing rather than a picture of it, and Escape puts it back.

   Dropping into an EMPTY function call is the one move that crosses levels, since
   it is the only one that needs no operator invented for it: that is how a
   parameter already typed gets wrapped in a cos().
   ──────────────────────────────────────────────────────────────────────────── */

var Chips = (function () {
  "use strict";

  /* ── What Fusion knows about ────────────────────────────────────────────────
     Fusion's expression language, as far as this matters here. `arg` is what the
     function's argument is measured in, which is the same question the dialog's
     unit filter already asks — "angle" for the trig pair, "" for no opinion.
     `takes` is how many arguments it wants; only the one-argument functions absorb
     what follows them into a single item, since min(a, b) has two things inside it
     and neither is "the" argument. */
  var FUNCTIONS = {
    sin:  { arg: "angle" }, cos:  { arg: "angle" }, tan:  { arg: "angle" },
    asin: { arg: "" },      acos: { arg: "" },      atan: { arg: "" },
    sinh: { arg: "" },      cosh: { arg: "" },      tanh: { arg: "" },
    sqrt: { arg: "" },      abs:  { arg: "" },      exp:  { arg: "" },
    ln:   { arg: "" },      log:  { arg: "" },      sign: { arg: "" },
    floor:{ arg: "" },      ceil: { arg: "" },      round:{ arg: "" },
    min:  { arg: "", takes: 2 }, max: { arg: "", takes: 2 },
    pow:  { arg: "", takes: 2 }, atan2: { arg: "", takes: 2 }
  };

  /* Fusion writes it PI, uppercase. The lowercase spelling is accepted here as a
     name to complete FROM — the list offers "PI" for "pi" — but only the real
     spelling is ever a chip, because only the real spelling parses. */
  var CONSTANTS = { PI: { unit: "" } };

  /* ── The operator letters ──────────────────────────────────────────────────
     An operator is one character, and one character is exactly what the home row is
     for. A letter typed and then committed with a space is not a word — no parameter
     is called "f" — so the space can be read as "I meant the operator this key stands
     for", and the four arithmetic ones plus the six trig calls come off the left hand
     without reaching for shift or for the bracket keys.

     f d s a are the four in the order they are used; w e r are sin cos tan on the row
     above, and o i u their inverses on the right hand mirroring them. */
  var OPERATOR_WORDS = {
    f: "+", d: "-", s: "*", a: "/",
    w: "sin", e: "cos", r: "tan", o: "asin", i: "acos", u: "atan"
  };

  // U+2060 (word joiner, zero-width) opens a NUMBER PARAMETER's short name, so "10" can
  // be a word: see "Short names".
  var IDENT_START = /[A-Za-z_\u2060]/;
  // The last five only ever occur inside a SHORT NAME (see "Short names" below): a
  // narrow no-break space, a no-break hyphen and a low comma, standing in for the " ",
  // "-" and "," of a parameter's reading so the reading can be one word in the field;
  // the word joiner and a one-dot leader (a number's ".") for a number parameter.
  var IDENT_CHAR  = /[A-Za-z0-9_\u202f\u2011\u201a\u2060\u2024]/;
  // Whitespace, ASCII only — never \s, which counts the narrow no-break space inside a
  // short name as a gap between two items.
  var WS = /[ \t\n\r\f\v]/;

  function isFunction(word) {
    return Object.prototype.hasOwnProperty.call(FUNCTIONS, word);
  }
  function isConstant(word) {
    return Object.prototype.hasOwnProperty.call(CONSTANTS, word);
  }
  /* Lowercased, because capitalise.js has already been at it: every window capitalises
     the first letter of a word as it is typed, so the "f" that reaches here is an "F".
     Only one-letter words can match at all, and there is no parameter one letter long,
     so there is nothing for the fold to take by mistake. */
  function operatorFor(word) {
    return OPERATOR_WORDS[String(word).toLowerCase()];
  }
  function isOperatorWord(word) {
    return operatorFor(word) !== undefined;
  }

  /* ── Text ──────────────────────────────────────────────────────────────── */

  /* The index of the ")" closing the "(" at `open`, or -1 if it is never closed —
     which is the ordinary state of an expression halfway through being typed, and
     is why an unclosed call is left as plain letters rather than becoming a chip. */
  function matchParen(text, open, to) {
    var depth = 0;
    for (var i = open; i < to; i++) {
      if (text[i] === "(") depth++;
      else if (text[i] === ")") { depth--; if (depth === 0) return i; }
    }
    return -1;
  }

  /* The live bubble starting exactly at `i`, or null. A bubble is tracked by
     position rather than read off the text (there is nothing in "(" and ")"
     themselves that says which pair is a bubble and which is a plain typed
     bracket), so this is the one place that position is turned back into a
     bracket the scanner treats specially — and it is deliberately re-checked
     against the text on every scan rather than trusted, so a bubble whose own
     parens got edited away quietly reverts to being a plain bracket instead of
     misreading whatever now sits at its old position. */
  function bubbleAt(ctx, i, text, to) {
    var list = ctx.bubbles;
    if (!list) return null;
    for (var k = 0; k < list.length; k++) {
      if (list[k].a !== i) continue;
      var close = matchParen(text, i, to);
      if (close >= 0 && close + 1 === list[k].b) return list[k];
    }
    return null;
  }

  /* ── The atoms ─────────────────────────────────────────────────────────────
     One pass over a region of the text, returning the items in it in order. Pure:
     no DOM, no element state — the tests drive this directly.

     ctx tells it what counts as a name and where the caret is:
        isParam(word)   is this an existing parameter
        caret           the collapsed caret, or null (a selection, or unfocused)
        typingAt        where the word currently BEING TYPED starts, or -1. See
                        isOpen: it is the difference between a word you are still
                        spelling and one you have merely parked the caret after.   */
  function scan(text, ctx) {
    var out = [];
    scanRegion(text, 0, text.length, out, 0, ctx);
    return out;
  }

  var OPERATOR = /[+\-*\/^%,]/;

  /* Is the operator at `i` BINARY — something to its left for it to work on? A + or -
     with nothing but an operator, a bracket or the start of the line behind it is a
     SIGN on what follows, not an operation between two things. The distinction is the
     same one splitItems makes, and it is what keeps "-5" from being painted as an
     operator and spaced apart into "- 5". */
  function isBinary(text, i, from) {
    for (var k = i - 1; k >= from; k--) {
      var ch = text[k];
      if (WS.test(ch)) continue;
      return ch === ")" || IDENT_CHAR.test(ch);
    }
    return false;
  }

  function scanRegion(text, from, to, out, depth, ctx) {
    var i = from;
    while (i < to) {
      // An operator and a bracket are items in the sense that matters here: they are
      // Fusion's punctuation rather than anything you are spelling, so they are painted
      // as their own thing. They are never dragged and never deleted whole — an
      // operator is one character, and moving the punctuation is exactly what dragging
      // must not do (see planMove: the skeleton stays put and the operands move).
      if (OPERATOR.test(text[i])) {
        if (isBinary(text, i, from)) {
          out.push({ kind: "op", name: text[i], a: i, b: i + 1, depth: depth });
          i++;
          continue;
        }
        // Not binary, so a SIGN — and a sign is part of the number it signs. "-5" is
        // minus five, one item, never a "-" sitting next to a "5".
        var signed = numberAt(text, i + 1, to, ctx);
        if (signed > 0) { pushNumber(text, i, signed, out, depth, ctx); i = signed; continue; }
        i++;
        continue;
      }
      if (text[i] === "(") {
        var bub = bubbleAt(ctx, i, text, to);
        if (bub) {
          var gatom = {
            kind: "group", a: i, b: bub.b, depth: depth,
            inner: { a: i + 1, b: bub.b - 1 }, items: []
          };
          scanRegion(text, gatom.inner.a, gatom.inner.b, gatom.items, depth + 1, ctx);
          out.push(gatom);
          i = bub.b;
          continue;
        }
      }
      if (text[i] === "(" || text[i] === ")") {
        out.push({ kind: "bracket", name: text[i], a: i, b: i + 1, depth: depth });
        i++;
        continue;
      }
      // A number is as much a THING as a name is: 25 mm is one value you either mean
      // or don't, not two digits and a unit to be edited a character at a time. So it
      // chips on exactly the rule a name does — the moment you stop typing it.
      var num = numberAt(text, i, to, ctx);
      if (num > 0) { pushNumber(text, i, num, out, depth, ctx); i = num; continue; }
      if (!IDENT_START.test(text[i])) { i++; continue; }

      var a = i, b = i;
      while (b < to && IDENT_CHAR.test(text[b])) b++;
      var word = text.slice(a, b);

      // A call: the name, any spaces, then a bracket that is actually closed.
      var j = b;
      while (j < to && (text[j] === " " || text[j] === "\t")) j++;
      if (isFunction(word) && text[j] === "(") {
        var close = matchParen(text, j, to);
        if (close >= 0) {
          var atom = {
            kind: "fn", name: word, a: a, b: close + 1, depth: depth,
            arg: { a: j + 1, b: close }, items: []
          };
          scanRegion(text, atom.arg.a, atom.arg.b, atom.items, depth + 1, ctx);
          // The caret inside the NAME is someone rewriting which function this is,
          // so it goes back to letters; the caret inside the ARGUMENT is the normal
          // way to use it and leaves the call whole.
          if (isOpen(a, b, ctx)) out.push.apply(out, atom.items);
          else out.push(atom);
          i = close + 1;
          continue;
        }
      }

      if (!isOpen(a, b, ctx)) {
        if (ctx.isParam && ctx.isParam(word)) {
          out.push({ kind: "param", name: word, a: a, b: b, depth: depth });
        } else if (isConstant(word)) {
          out.push({ kind: "const", name: word, a: a, b: b, depth: depth });
        }
      }
      i = b;
    }
  }

  /* Where the number starting at `i` ends, or -1 if there is not one there.

     The UNIT is part of it. "25 mm" is one value written the way it is said, and
     splitting it would leave a "mm" that is neither a name nor an operator sitting
     loose in the line. Only a short word, and only one that is not something else
     already: a parameter, a constant, a function, or one of the operator letters on
     its way to becoming an operator. */
  function numberAt(text, i, to, ctx) {
    if (i >= to) return -1;
    if (!/[0-9]/.test(text[i]) &&
        !(text[i] === "." && /[0-9]/.test(text[i + 1] || ""))) return -1;
    var b = i;
    while (b < to && /[0-9.]/.test(text[b])) b++;
    if (b + 1 < to && /[eE]/.test(text[b]) && /[0-9]/.test(text[b + 1])) {
      b += 2;
      while (b < to && /[0-9]/.test(text[b])) b++;
    }
    var k = b;
    while (k < to && (text[k] === " " || text[k] === "\t")) k++;
    if (k < to && IDENT_START.test(text[k])) {
      var w = k;
      while (w < to && IDENT_CHAR.test(text[w])) w++;
      var word = text.slice(k, w);
      if (word.length <= 4 && !isFunction(word) && !isConstant(word) &&
          !isOperatorWord(word) && !(ctx.isParam && ctx.isParam(word))) b = w;
    }
    return b;
  }

  function pushNumber(text, a, b, out, depth, ctx) {
    if (isOpen(a, b, ctx)) return;                 // still being typed: letters
    out.push({ kind: "num", name: text.slice(a, b), a: a, b: b, depth: depth });
  }

  /* Is the word at [a, b) the one being typed, rather than one that is finished?

     Inside it, obviously — that is what clicking into a chip does, and it is how a
     chip is edited.

     At its END, only while it is actually being TYPED. "Widt|" must not chip the
     instant it passes through a name that exists, and "Width|" must not chip while
     the next keystroke could still make it WidthX — so a word the last keystroke
     added to stays letters. But a caret merely PARKED at the end of a finished word
     is a different thing entirely: it is where Backspace is aimed, and the whole
     point of an item is that Backspace takes all of it. So the moment typing stops —
     a space, a click, an arrow key, a suggestion accepted, or the deletion of the
     character that followed it — the word closes up into an item again.

     At its START it is closed: the caret is BEFORE the item, not in it. */
  function isOpen(a, b, ctx) {
    // Being EDITED is explicit. A chip opened with the space has all of its text
    // selected, and a selection says nothing about where the caret is — so nothing about
    // the caret could tell this apart from a chip merely sitting under a selection.
    if (ctx.editing && a < ctx.editing.b && b > ctx.editing.a) return true;
    var c = ctx.caret;
    if (c === null || c === undefined) return false;
    if (c > a && c < b) return true;
    // typingAt is where the WORD the last keystroke added to begins, which for
    // "25 mm" is the "mm" rather than the "25": anywhere inside the item counts, or
    // typing the unit onto a number would seal the number under your fingers.
    if (c === b) return ctx.typingAt >= a && ctx.typingAt < b;
    // Arrowed back to the start of the word being typed: still that word, still open.
    if (c === a) return ctx.typingAt === a;
    return false;
  }

  /* The atom containing or touching `pos`, deepest first — the argument chip inside
     a call rather than the call itself, since that is the one under the pointer. */
  function atomAt(atoms, pos, edge) {
    for (var i = 0; i < atoms.length; i++) {
      var at = atoms[i];
      var inside = edge ? (pos >= at.a && pos <= at.b) : (pos >= at.a && pos < at.b);
      if (!inside) continue;
      if (at.items && at.items.length) {
        var deeper = atomAt(at.items, pos, edge);
        if (deeper) return deeper;
      }
      return at;
    }
    return null;
  }

  /* The kinds you can take hold of. Punctuation is painted and nothing else: it is the
     shape of the expression rather than a thing in it. */
  function isItem(at) { return at.kind !== "op" && at.kind !== "bracket"; }

  /* Every atom, flattened, so a lookup by range does not have to walk the tree. */
  function flatten(atoms, out) {
    out = out || [];
    for (var i = 0; i < atoms.length; i++) {
      out.push(atoms[i]);
      if (atoms[i].items) flatten(atoms[i].items, out);
    }
    return out;
  }

  /* ── Spacing ───────────────────────────────────────────────────────────────
     Items do not touch. "Width+Depth" is three items typed without room between them,
     and it is written back as "Width + Depth" — the space is not punctuation you have
     to remember, it is the gap that makes a row of items readable.

     Only around an OPERATOR, and only a binary one: a sign belongs to the number it
     signs ("-5", never "- 5"), and a bracket sits tight against what it encloses,
     which is the same rule tidyBrackets applies on the way to Fusion.

     Only on INSERTION, too — never while deleting. Adding the space back the instant
     Backspace removes it would be a field that cannot be edited: you would press the
     key, see nothing happen, and press it again.

     Pure: text and caret in, text and caret out. */
  function space(text, caret, ctx) {
    var joined = join(text, caret, ctx);
    text = joined.text; caret = joined.caret;
    var atoms = flatten(scan(text, { caret: null, typingAt: -1, isParam: ctx.isParam }));
    var out = text, c = caret, shift = 0;
    for (var i = 0; i < atoms.length; i++) {
      if (atoms[i].kind !== "op") continue;
      var a = atoms[i].a + shift, b = atoms[i].b + shift;
      if (a > 0 && !WS.test(out[a - 1])) {
        out = out.slice(0, a) + " " + out.slice(a);
        // A caret standing right before the operator is at the end of whatever was just
        // typed there, and stays with it: carried over the gap it would be parked off
        // the letters, and the next keystroke would start a word of its own.
        if (c > a) c++;
        shift++; a++; b++;
      }
      if (b < out.length && !WS.test(out[b])) {
        out = out.slice(0, b) + " " + out.slice(b);
        if (c >= b) c++;
        shift++;
      }
    }
    return { text: out, caret: c };
  }

  /* ── Two things side by side are being added ───────────────────────────────
     "Width Depth" is a width and a depth with nothing said about what to do with
     them, and the only thing that can mean is their sum: a row of lengths laid out
     next to each other adds up. So + is the assumption, and it is WRITTEN IN rather
     than left implied — an expression on screen should be the expression Fusion is
     given, and a "+" you cannot see is one you cannot drag, delete or change.

     Only between two finished items. While the second is still being spelled there is
     nothing to add yet, which is why this takes the live ctx rather than the closed
     one the spacing pass uses.                                                       */
  function join(text, caret, ctx) {
    var gaps = adjacent(scan(text, ctx), []);
    gaps.sort(function (p, q) { return p.a - q.a; });
    var out = text, c = caret, shift = 0;
    for (var i = 0; i < gaps.length; i++) {
      // A guarded word is not this file's to reason about at all — the plane box's
      // datum is a face, and "Front 100" is a face and an offset, not a sum.
      if (ctx.guard && (ctx.guard(text, gaps[i].prev.a, gaps[i].prev.b) ||
                        ctx.guard(text, gaps[i].next.a, gaps[i].next.b))) continue;
      var a = gaps[i].a + shift, b = gaps[i].b + shift;
      if (!/^[ \t]*$/.test(out.slice(a, b))) continue;    // something between them
      out = out.slice(0, a) + " + " + out.slice(b);
      var delta = 3 - (b - a);
      if (c >= b) c += delta;
      else if (c > a) c = a + 3;
      shift += delta;
    }
    return { text: out, caret: c };
  }

  /* Every place one item is directly followed by another, as the gap between them.
     Siblings only: a call and what is inside it are nested, not side by side. */
  function adjacent(atoms, out) {
    for (var i = 0; i < atoms.length; i++) {
      if (i > 0 && isItem(atoms[i - 1]) && isItem(atoms[i])) {
        out.push({ a: atoms[i - 1].b, b: atoms[i].a,
                   prev: atoms[i - 1], next: atoms[i] });
      }
      if (atoms[i].items) adjacent(atoms[i].items, out);
    }
    return out;
  }

  /* ── No stray spaces ───────────────────────────────────────────────────────
     A space in this field is never something you typed — it is the gap this file puts
     between two items, and it is only ever ONE, only ever between two things. Anything
     else is left over: a run of them from a paste, one at the front of a line, one
     trailing after the item in front of it was deleted. They go.

     The exception is a trailing space the CARET IS STANDING IN. That one is the slot the
     next item goes into — the plane line's gap after its datum is exactly this — and
     taking it away as you arrive in it would be taking away the place you are.

     Pure: text and caret in, text and caret out.                                       */
  function tidy(text, caret) {
    var out = "", dropped = 0;
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      var drop = false;
      if (ch === " " || ch === "\t") {
        var prev = out.length ? out.charAt(out.length - 1) : "";
        var rest = text.slice(i + 1);
        var trailing = /^[ \t]*(\n|$)/.test(rest);
        drop = prev === "" || prev === " " || prev === "\n" ||
               (trailing && caret <= i);
      }
      if (drop) { if (i < caret) dropped++; }
      else out += ch;
    }
    return { text: out, caret: Math.max(0, Math.min(caret - dropped, out.length)) };
  }

  /* ── Operands and operators ────────────────────────────────────────────────
     A region split into the things being combined and what combines them:

         "100 * cos(Taper) + Width"
          └op┘ └s┘ └──op───┘ └s┘└op┘

     Items are what dragging permutes; separators stay exactly where they are, which
     is what keeps the arithmetic the shape it was. A leading sign belongs to the
     item it signs ("-5" is minus five, not a subtraction with nothing on its left),
     which is decided by position: at the start of an item slot, +/- is a sign. */
  function splitItems(text, from, to) {
    var items = [], seps = [], i = from;
    var lead = "", tail = "";

    while (i < to && WS.test(text[i])) i++;
    lead = text.slice(from, i);

    while (i < to) {
      var start = i;
      // A sign, then one operand.
      if (text[i] === "+" || text[i] === "-") i++;
      while (i < to && WS.test(text[i])) i++;
      if (i >= to) { i = start; break; }

      if (text[i] === "(") {
        var close = matchParen(text, i, to);
        i = close < 0 ? to : close + 1;
      } else if (IDENT_START.test(text[i])) {
        while (i < to && IDENT_CHAR.test(text[i])) i++;
        var j = i;
        while (j < to && (text[j] === " " || text[j] === "\t")) j++;
        if (text[j] === "(") {                    // a call is one operand
          var c2 = matchParen(text, j, to);
          i = c2 < 0 ? to : c2 + 1;
        }
      } else if (/[0-9.]/.test(text[i])) {
        while (i < to && /[0-9.eE]/.test(text[i])) i++;
        // A unit follows a number with or without a space: "25 mm", "25mm".
        var k = i;
        while (k < to && (text[k] === " " || text[k] === "\t")) k++;
        if (k < to && IDENT_START.test(text[k])) {
          var w = k;
          while (w < to && IDENT_CHAR.test(text[w])) w++;
          var word = text.slice(k, w);
          if (!isFunction(word) && !isConstant(word) && word.length <= 4) i = w;
        }
      } else {
        // An operator where an operand was expected — a half-typed line. Stop; what
        // is left becomes the tail, and dragging simply does not apply to it.
        i = start;
        break;
      }

      var end = i;
      while (end > start && WS.test(text[end - 1])) end--;
      items.push({ a: start, b: end });

      // The separator: whitespace and operator characters up to the next operand.
      var s = end;
      while (i < to && WS.test(text[i])) i++;
      if (i < to && /[+\-*/^%,]/.test(text[i])) {
        i++;
        while (i < to && WS.test(text[i])) i++;
        seps.push({ a: s, b: i });
      } else {
        i = s;
        break;
      }
    }

    tail = text.slice(items.length ? items[items.length - 1].b : from + lead.length, to);
    if (seps.length >= items.length && seps.length) {
      // A trailing operator with nothing after it: it is not a separator, it is tail.
      var last = seps.pop();
      tail = text.slice(last.a, to);
    }
    return { a: from, b: to, lead: lead, items: items, seps: seps, tail: tail };
  }

  /* Every container in the text: the whole line, the argument of each call, and the
     inside of each bubble — the levels a drag can move an item within. A bubble's
     inside is a container exactly the way a call's argument is: dragging a chip to
     the front of one, or out of one into an empty call, is the same permutation
     either way. */
  function containers(text, atoms) {
    var out = [splitItems(text, 0, text.length)];
    var all = flatten(atoms);
    for (var i = 0; i < all.length; i++) {
      if (all[i].kind === "fn") {
        out.push(splitItems(text, all[i].arg.a, all[i].arg.b));
      } else if (all[i].kind === "group") {
        out.push(splitItems(text, all[i].inner.a, all[i].inner.b));
      }
    }
    return out;
  }

  /* ── Moving an item ────────────────────────────────────────────────────────
     Returns { text, range } — the new line and where the moved item ended up — or
     null when the move is one this refuses to invent an operator for.

     Within one container it is a permutation: the items are re-laid in the new
     order into the SAME separators, so every operator stays where it was.

     Across containers it is allowed only into an EMPTY one (an unfilled call),
     because there is then nothing to join it to. What it leaves behind takes the
     neighbouring separator with it, since "100 * " with the operand gone is not
     something to hand to Fusion.                                                  */
  function planMove(text, src, dstContainer, dstIndex, allContainers) {
    var from = containerOf(allContainers, src);
    if (!from) return null;
    var srcIndex = indexOfItem(from, src);
    if (srcIndex < 0) return null;

    if (from === dstContainer) {
      if (dstIndex === srcIndex) return null;
      var order = [];
      for (var i = 0; i < from.items.length; i++) if (i !== srcIndex) order.push(i);
      var at = dstIndex > srcIndex ? dstIndex - 1 : dstIndex;
      order.splice(at, 0, srcIndex);
      var body = from.lead;
      var moved = null;
      for (var k = 0; k < order.length; k++) {
        var it = from.items[order[k]];
        var piece = text.slice(it.a, it.b);
        if (order[k] === srcIndex) moved = { a: from.a + body.length, b: from.a + body.length + piece.length };
        body += piece;
        if (k < from.seps.length) body += text.slice(from.seps[k].a, from.seps[k].b);
      }
      body += from.tail;
      var out = text.slice(0, from.a) + body + text.slice(from.b);
      return { text: out, range: moved };
    }

    if (dstContainer.items.length) return null;      // no operator to invent

    // Take the item out, with the separator that would be left dangling. The one
    // AFTER it, because an expression reads left to right as operand-then-operator:
    // "100 * Taper + cos()" is [100 *][Taper +][cos()], so taking Taper out takes the
    // "+" with it and leaves 100 still multiplying what is left. The last item has no
    // separator after it and gives up the one in front of it instead.
    var cut = { a: src.a, b: src.b };
    if (from.items.length > 1) {
      if (srcIndex < from.seps.length) cut.b = from.seps[srcIndex].b;
      else cut.a = from.seps[srcIndex - 1].a;
    }
    var piece2 = text.slice(src.a, src.b);
    var insertAt = dstContainer.a + dstContainer.lead.length;
    if (insertAt >= cut.a && insertAt <= cut.b) return null;   // dropping into itself

    var t = text.slice(0, cut.a) + text.slice(cut.b);
    var shift = insertAt > cut.a ? (cut.b - cut.a) : 0;
    var pos = insertAt - shift;
    t = t.slice(0, pos) + piece2 + t.slice(pos);
    return { text: t, range: { a: pos, b: pos + piece2.length } };
  }

  /* Is this text a WHOLE expression on its own — two or more operands with an operator
     between each pair, and nothing dangling off either end? What "make a parameter"
     asks of a selection before offering itself: a chip and the "+" next to it is a
     perfectly good selection, but "Width +" is not a value and Fusion would refuse to
     make a parameter of it. Two operands is the floor because one chip on its own is
     already a name — there is nothing to name a second time. Pure. */
  function wholeExpr(text) {
    var s = splitItems(text, 0, text.length);
    if (s.items.length < 2) return false;
    if (s.seps.length !== s.items.length - 1) return false;
    return !/\S/.test(s.tail);
  }

  function containerOf(all, range) {
    var best = null;
    for (var i = 0; i < all.length; i++) {
      var c = all[i];
      if (range.a >= c.a && range.b <= c.b) {
        if (!best || (c.b - c.a) < (best.b - best.a)) best = c;
      }
    }
    return best;
  }
  function indexOfItem(container, range) {
    for (var i = 0; i < container.items.length; i++) {
      if (container.items[i].a === range.a && container.items[i].b === range.b) return i;
    }
    return -1;
  }

  /* ── Bubbles: making one ───────────────────────────────────────────────────
     The span a bubble around these chip ranges would cover — the earliest start
     to the latest end. Wrapping a span in brackets is a contiguous operation in
     any editor; if the ranges are not already contiguous siblings (Cmd-click
     can pick the 1st and 3rd, skipping the 2nd), whatever sits
     between them comes along for the ride rather than the request being refused.
     Refused outright under two ranges — a one-chip bubble is destroyed on the
     same keystroke that would create it, see collapseSingleChipBubbles, so there
     is no point writing one. */
  function groupSpan(ranges) {
    if (!ranges || ranges.length < 2) return null;
    var a = ranges[0].a, b = ranges[0].b;
    for (var i = 1; i < ranges.length; i++) {
      if (ranges[i].a < a) a = ranges[i].a;
      if (ranges[i].b > b) b = ranges[i].b;
    }
    return { a: a, b: b };
  }

  /* Wrap [a, b) in a new bubble: real parens written into the text — see the
     "core mechanism" note in the phase-2 plan for why they have to be real. Done
     as two single-character insertions, the ")" then the "(", each shifting the
     bubbles already tracked the ordinary way one insertion would (shiftBubbles) —
     rather than one edit spanning both, which would look, to shiftBubbles, like
     it touched the parens of any bubble already nested inside the span being
     wrapped, and drop it. Pure: text and the existing bubbles in; the new text
     and bubbles — this one appended — out. */
  function insertGroup(text, a, b, bubbles) {
    var afterOpen = text.slice(0, a) + "(" + text.slice(a, b) + ")" + text.slice(b);
    // Shifted by hand, as removeGroup does: a "(" put in front of a bubble's own "("
    // reads to shiftBubbles' diff as typed just INSIDE it, stretching that bubble over
    // the new one instead of moving it along.
    var pos = function (p) { return p + (p >= a ? 1 : 0) + (p >= b ? 1 : 0); };
    var shifted = [];
    for (var i = 0; i < (bubbles || []).length; i++) {
      var bub = bubbles[i];
      shifted.push(moved(bub, pos(bub.a), bub.b <= a ? bub.b : bub.b + (bub.b > b ? 2 : 1)));
    }
    return { text: afterOpen, bubbles: shifted.concat([{ a: a, b: b + 2 }]) };
  }

  /* The opposite: un-bubble one "group" atom, already known to have collapsed to
     one chip or none (collapseSingleChipBubbles). Same two-single-edit reasoning
     as insertGroup, in reverse — the ")" removed first, then the "(" — so a
     bubble nested inside the one being destroyed shifts rather than vanishing
     with it. shiftBubbles drops the destroyed bubble itself along the way, since
     each removal touches one of its own paren characters. */
  function removeGroup(text, hit, bubbles) {
    var afterClose = text.slice(0, hit.b - 1) + text.slice(hit.b);
    var afterOpen = afterClose.slice(0, hit.a) + afterClose.slice(hit.a + 1);
    // Shifted by hand, not by shiftBubbles: its diff cannot tell "((" from "((" — taking
    // the outer "(" off "((A + B))" reads to it as taking the INNER one, and the bubble
    // that should survive was the one dropped.
    var pos = function (p) { return p - (p > hit.b - 1 ? 1 : 0) - (p > hit.a ? 1 : 0); };
    var shifted = [];
    for (var i = 0; i < bubbles.length; i++) {
      var bub = bubbles[i];
      if (bub.a === hit.a && bub.b === hit.b) continue;
      shifted.push(moved(bub, pos(bub.a), pos(bub.b)));
    }
    return { text: afterOpen, bubbles: shifted };
  }

  /* ── Painting ──────────────────────────────────────────────────────────────
     The layer holds every character of the text exactly once, so it wraps where the
     field wraps. The only additions are element boundaries, which occupy nothing. */
  var ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;" };
  function esc(s) { return String(s).replace(/[&<>]/g, function (c) { return ESC[c]; }); }

  var labelOf = null;                  // opts.label of the field being painted — see render
  var ghostAt = -1;                    // its operator slot's placeholder — see takeOperator

  function paint(text, atoms, from, to, tagsOf) {
    var html = "", i = from;
    for (var k = 0; k < atoms.length; k++) {
      var at = atoms[k];
      if (at.a > i) html += esc(text.slice(i, at.a));
      html += chipHtml(text, at, tagsOf);
      i = at.b;
    }
    if (i < to) html += esc(text.slice(i, to));
    return html;
  }

  function chipHtml(text, at, tagsOf) {
    var cls = "chip chip-" + at.kind;
    var where = ' data-a="' + at.a + '" data-b="' + at.b + '"';
    if (at.kind === "op" || at.kind === "bracket") {
      if (at.a === ghostAt) cls += " chip-ghost";
      return '<span class="' + cls + '"' + where + ">" +
             esc(text.slice(at.a, at.b)) + "</span>";
    }
    var tag = "";
    if (at.kind === "param" && isNumberShort(at.name)) cls += " chip-numparam";
    if (at.kind === "param" && tagsOf) {
      var t = tagsOf(at.name);
      if (t && t.axis) { cls += " tagged"; tag = ' data-axis="' + esc(t.axis) + '"'; }
    }
    // A parameter READS as the page says it does (opts.label — the Browser's spoken
    // form, "Boot Ceiling - Dimension - Width" for BootCeiling_Dimension_Span_X) —
    // almost always because the field holds that reading as its SHORT NAME (see "Short
    // names"), which needs nothing here. Only a parameter left without one (a reading
    // another parameter shares) comes through with a label to lay on. Its
    // real name stays in the layer, letter for letter and transparent, because the
    // layer must hold exactly what the field holds; the reading is laid OVER it and
    // fitted to that width (fitLabels), and walker() never counts its letters.
    var label = "";
    if (at.kind === "param" && labelOf) {
      var shown = labelOf(at.name);
      if (shown && shown !== at.name) {
        cls += " labelled";
        label = '<span class="chip-label" aria-hidden="true">' + esc(shown) + "</span>";
      }
    }
    var attrs = ' class="' + cls + '" data-a="' + at.a + '" data-b="' + at.b + '"' + tag;
    if (label) return "<span" + attrs + '><span class="chip-raw">' +
                      esc(text.slice(at.a, at.b)) + "</span>" + label + "</span>";

    if (at.kind === "group") {
      // Handles: the "(" and ")" this file wrote, never shown as text (a bubble has
      // no visible brackets), but still real characters occupying their own cell —
      // the layer has to hold exactly what the field holds. Painted with no "chip"
      // class of their own, so a click that lands on one resolves (via chipUnder /
      // atomAt) to the bubble as a whole rather than descending into its contents,
      // exactly the way clicking "cos(" selects the call rather than opening it.
      return "<span" + attrs + ' data-depth="' + at.depth + '">' +
               '<span class="chip-group-handle">' + esc(text.slice(at.a, at.a + 1)) + "</span>" +
               paint(text, at.items, at.inner.a, at.inner.b, tagsOf) +
               '<span class="chip-group-handle">' + esc(text.slice(at.b - 1, at.b)) + "</span>" +
             "</span>";
    }

    if (at.kind !== "fn") return "<span" + attrs + ">" + esc(text.slice(at.a, at.b)) + "</span>";

    // A call paints as one chip with the argument inside it, so the argument can
    // carry a chip of its own without the two backgrounds reading as one thing.
    var openEnd = at.arg.a;                                  // "cos(" — name and bracket
    return "<span" + attrs + ">" +
             '<span class="chip-fn-name">' + esc(text.slice(at.a, openEnd)) + "</span>" +
             '<span class="chip-arg">' + paint(text, at.items, at.arg.a, at.arg.b, tagsOf) + "</span>" +
             '<span class="chip-fn-name">' + esc(text.slice(at.arg.b, at.b)) + "</span>" +
           "</span>";
  }

  /* ── Per-field state ───────────────────────────────────────────────────────── */

  var fields = [];                    // [{el, layer, opts, …}] — a handful at most
  function stateOf(el) {
    for (var i = 0; i < fields.length; i++) if (fields[i].el === el) return fields[i];
    return null;
  }

  /* ── Short names ───────────────────────────────────────────────────────────
     A parameter chip reads the way the page says it does (opts.label — "Curve - Inset"
     for Drawer_Front_Curve__Inset_Back), and it is exactly as WIDE as that reading: the
     field itself holds the reading, not the Fusion name. The layer must hold what the
     field holds letter for letter (see the header), so a chip the size of its reading
     means a field holding its reading. The old way — the real name in the field, the
     reading laid over it — gave every chip the real name's width, and gave F the real
     name to edit.

     The reading's " ", "-" and "," would be gaps and operators here, so they go in as a
     NARROW no-break space (U+202F), a no-break hyphen and a low comma (IDENT_CHAR takes
     all three, WS takes none): the reading is ONE word in the field, and looks the same.
     That word is the parameter's SHORT NAME. Not the plain no-break space: CSS counts it
     as a word separator, so the field's 12px word-spacing (the gap BETWEEN chips, see
     theme.css) would open up every space inside the reading too; U+202F is not one, and
     never breaks the line either.

     The page never sees one. With opts.label given, the element's value,
     selectionStart/End and setSelectionRange are wrapped on the element itself, so the
     page reads and writes the REAL expression at REAL positions exactly as before, while
     this file works on the field's own text through raw/setRaw/selA/selB/setSel.
     Everything handed back to the page (chipAtCaret, selectionOf, accept…) is
     translated at the door — see "What the page asks about".

     A reading two parameters share, or one that is also a real name or a word this
     file means something by, makes no short name: that parameter stays its real name,
     with the reading laid over it as before (chipHtml's `labelled`). */
  function raw(st) { return st.nat ? st.nat.get() : st.el.value; }
  function setRaw(st, v) { if (st.nat) st.nat.set(v); else st.el.value = v; }
  function selA(st) { return st.nat ? st.nat.ss() : st.el.selectionStart; }
  function selB(st) { return st.nat ? st.nat.se() : st.el.selectionEnd; }
  function setSel(st, a, b) { if (st.nat) st.nat.sel(a, b); else st.el.setSelectionRange(a, b); }

  function own(map, k) { return Object.prototype.hasOwnProperty.call(map, k); }

  var SHORT_OK = /^[A-Za-z][A-Za-z0-9_ ,\-]*[A-Za-z0-9_]$/;
  function shortText(label) {
    label = String(label == null ? "" : label).trim();
    if (NUM_READING.test(label)) {
      return NUM_MARK + label.replace(/\./g, "\u2024").replace(/ /g, "\u202f")
                             .replace(/-/g, "\u2011");
    }
    if (!SHORT_OK.test(label)) return null;
    return label.replace(/ /g, "\u202f").replace(/-/g, "\u2011").replace(/,/g, "\u201a");
  }

  /* A NUMBER PARAMETER (AddIns/Number Params.py \u2014 a typed number made a parameter
     nobody names) reads as its value: opts.label hands back "10" or "2.5 cm". A word
     cannot start with a digit, so its short name opens with a zero-width word joiner
     (NUM_MARK) and carries its "." as a one-dot leader. Two of them reading "10" are
     told apart by more word joiners at the end \u2014 invisible, and kept per parameter
     across rebuilds (st.numShort), so a short name in the field never comes to mean a
     different parameter. A number chip opened and retyped ("\u206012") is no parameter's
     short name: it reads back as the plain number (numberText), and the add-in's
     commit changes the parameter it replaced (number_params.plan). */
  var NUM_MARK = "\u2060";
  var NUM_READING = /^-?(\d+\.?\d*|\.\d+)( [A-Za-z]+)?$/;
  function isNumberShort(w) { return w != null && String(w).charAt(0) === NUM_MARK; }
  function numberText(w) {
    return String(w).replace(/\u2060/g, "").replace(/\u2024/g, ".")
                    .replace(/\u202f/g, " ").replace(/\u2011/g, "-");
  }

  /* The field's short names, from the page's names and readings. Rebuilt on every render
     and every value the page writes, so a new parameter or a changed reading is picked up;
     `seen` remembers every short name ever given out, so one still in the field after its
     reading changed still reads back as its parameter (render then rewrites it). */
  function buildShort(st, names) {
    var u = st.user;
    names = names || (u.names && u.names()) || [];
    var real = Object.create(null), count = Object.create(null), cand = [], nums = [], i;
    for (i = 0; i < names.length; i++) real[names[i]] = true;
    for (i = 0; i < names.length; i++) {
      var l = null;
      try { l = u.label(names[i]); } catch (_) {}
      if (!l || l === names[i]) continue;
      var s = shortText(l);
      if (!s) continue;
      if (isNumberShort(s)) { nums.push([names[i], s]); continue; }
      cand.push([names[i], s]);
      count[s] = (count[s] || 0) + 1;
    }
    var toShort = Object.create(null), toReal = Object.create(null), all = names.slice();
    for (i = 0; i < cand.length; i++) {
      var n = cand[i][0], sh = cand[i][1];
      if (count[sh] > 1 || real[sh] || isFunction(sh) || isConstant(sh) ||
          isOperatorWord(sh)) continue;
      toShort[n] = sh; toReal[sh] = n; st.seen[sh] = n;
      all.push(sh);
    }
    // Number parameters: every one gets a short name, the same reading told apart by
    // trailing word joiners — the one it had last time first, while it still reads so.
    var keep = st.numShort || (st.numShort = Object.create(null)), taken = Object.create(null);
    var fresh = [];
    for (i = 0; i < nums.length; i++) {
      var prev = keep[nums[i][0]];
      if (prev && prev.replace(/⁠+$/, "") === nums[i][1] && !taken[prev]) taken[prev] = nums[i][0];
      else fresh.push(nums[i]);
    }
    for (i = 0; i < fresh.length; i++) {
      var ns = fresh[i][1];
      while (taken[ns]) ns += NUM_MARK;
      taken[ns] = fresh[i][0];
      keep[fresh[i][0]] = ns;
    }
    for (var shn in taken) {
      toShort[taken[shn]] = shn; toReal[shn] = taken[shn]; st.seen[shn] = taken[shn];
      all.push(shn);
    }
    st.shortVer++;
    st.short = { toShort: toShort, toReal: toReal, real: real, all: all, names: names };
    return st.short;
  }
  function shortMaps(st) { return st.short || buildShort(st); }

  function realName(st, w) {
    if (!st || !st.nat || w == null) return w;
    var m = shortMaps(st);
    if (own(m.toReal, w)) return m.toReal[w];
    if (!own(m.real, w) && own(st.seen, w)) return st.seen[w];
    return isNumberShort(w) ? numberText(w) : w;
  }
  function shortName(st, n) {
    if (!st || !st.nat || n == null) return n;
    var m = shortMaps(st);
    return own(m.toShort, n) ? m.toShort[n] : n;
  }

  /* Swap whole words of `text` through `lookup` (null = leave it). Returns the new text
     and where each swap sits, before and after: {fa, fb} → {ta, tb}. */
  var SHORT_RUN = /[A-Za-z_\u2060][A-Za-z0-9_\u202f\u2011\u201a\u2060\u2024]*/g;
  var NAME_RUN  = /[A-Za-z_][A-Za-z0-9_]*/g;
  function swapWords(text, re, lookup) {
    var out = "", segs = [], last = 0, m;
    re.lastIndex = 0;
    while ((m = re.exec(text))) {
      var w = m[0], a = m.index;
      if (a > 0 && /[0-9.]/.test(text[a - 1])) continue;      // a unit on a number
      // Part of a short name being edited ("Boot Ceil|"), not a name of its own.
      if (/[\u202f\u2011\u201a]/.test((text[a - 1] || "") + (text[a + w.length] || ""))) continue;
      var to = lookup(w);
      if (to == null || to === w) continue;
      out += text.slice(last, a);
      segs.push({ fa: a, fb: a + w.length, ta: out.length, tb: out.length + to.length });
      out += to;
      last = a + w.length;
    }
    return { text: out + text.slice(last), segs: segs };
  }
  function invert(segs) {
    return segs.map(function (s) { return { fa: s.ta, fb: s.tb, ta: s.fa, tb: s.fb }; });
  }
  /* A position in the "from" text → the same place in the "to" text. One inside a
     swapped word keeps its offset into it, clamped to the word's new length. */
  function mapPos(segs, p) {
    var off = 0;
    for (var i = 0; i < segs.length; i++) {
      var s = segs[i];
      if (p <= s.fa) break;
      if (p >= s.fb) { off = s.tb - s.fb; continue; }
      return Math.min(s.ta + (p - s.fa), s.tb);
    }
    return p + off;
  }

  /* The field's text read back as the real expression — cached, since the page asks for
     .value and the caret over and over between two edits. `segs` maps field positions
     to real ones, `back` the other way. */
  function realView(st) {
    var disp = st.nat.get(), c = st.xl;
    if (c && c.disp === disp && c.ver === st.shortVer) return c;
    var r = swapWords(disp, SHORT_RUN, function (w) {
      var n = realName(st, w);
      return n === w ? null : n;
    });
    st.xl = { disp: disp, ver: st.shortVer, text: r.text, segs: r.segs, back: invert(r.segs) };
    return st.xl;
  }
  function realText(st, text) {
    if (!st || !st.nat) return text;
    return swapWords(text, SHORT_RUN, function (w) {
      var n = realName(st, w);
      return n === w ? null : n;
    }).text;
  }
  function shortView(st, text) {
    var m = shortMaps(st);
    return swapWords(text, NAME_RUN, function (w) {
      return own(m.toShort, w) ? m.toShort[w] : null;
    });
  }
  function realPos(st, p) { return st && st.nat ? mapPos(realView(st).segs, p) : p; }
  function fieldPos(st, p) { return st && st.nat ? mapPos(realView(st).back, p) : p; }

  /* Wrap the element so the page sees the real expression. The native accessors are
     taken off the prototype first — they are what raw()/setRaw()/… use. The headless
     harness's stand-in has plain data properties instead; those are moved into a box. */
  function installShort(st) {
    var el = st.el;
    function find(prop) {
      for (var o = Object.getPrototypeOf(el); o; o = Object.getPrototypeOf(o)) {
        var d = Object.getOwnPropertyDescriptor(o, prop);
        if (d) return d;
      }
      return null;
    }
    var dv = find("value"), ds = find("selectionStart"), de = find("selectionEnd");
    var nat;
    if (dv && dv.get && dv.set && ds && ds.get && de && de.get &&
        !Object.prototype.hasOwnProperty.call(el, "value")) {
      var ssr = el.setSelectionRange;
      nat = {
        get: function () { return dv.get.call(el); },
        set: function (v) { dv.set.call(el, v); },
        ss: function () { return ds.get.call(el); },
        se: function () { return de.get.call(el); },
        sel: function (a, b, dir) { ssr.call(el, a, b, dir); }
      };
    } else {
      var box = { v: String(el.value == null ? "" : el.value),
                  a: el.selectionStart || 0, b: el.selectionEnd || 0 };
      nat = {
        get: function () { return box.v; },
        set: function (v) { box.v = String(v); box.a = box.b = box.v.length; },
        ss: function () { return box.a; },
        se: function () { return box.b; },
        sel: function (a, b) { box.a = a; box.b = b; }
      };
    }
    try {
      Object.defineProperty(el, "value", { configurable: true, enumerable: true,
        get: function () { return realView(st).text; },
        set: function (v) {
          buildShort(st);
          nat.set(shortView(st, String(v == null ? "" : v)).text);
        } });
      Object.defineProperty(el, "selectionStart", { configurable: true, enumerable: true,
        get: function () { return mapPos(realView(st).segs, nat.ss()); },
        set: function (p) {
          var d = mapPos(realView(st).back, p);
          nat.sel(d, Math.max(d, nat.se()));
        } });
      Object.defineProperty(el, "selectionEnd", { configurable: true, enumerable: true,
        get: function () { return mapPos(realView(st).segs, nat.se()); },
        set: function (p) {
          var d = mapPos(realView(st).back, p);
          nat.sel(Math.min(d, nat.ss()), d);
        } });
      Object.defineProperty(el, "setSelectionRange", { configurable: true, writable: true,
        value: function (a, b, dir) {
          var v = realView(st);
          nat.sel(mapPos(v.back, a), mapPos(v.back, b), dir);
        } });
    } catch (_) { return false; }
    st.nat = nat;
    return true;
  }

  /* The options this file works with, for a field with short names: the page's own, with
     the names that exist including the short ones, and every name handed back to the page
     (tags, the right-click menu) its real one. */
  function shortOpts(st, u) {
    var o = {};
    for (var k in u) if (own(u, k)) o[k] = u[k];
    // Rebuilt whenever the page's list is not the one they were made from (a new
    // parameter, another session's vocabulary) — not only at the next render.
    o.names = function () {
      var n = (u.names && u.names()) || [], m = st.short;
      if (!m || m.names.length !== n.length || m.names.some(function (x, i) { return x !== n[i]; }))
        m = buildShort(st, n);
      return m.all;
    };
    if (u.tags) o.tags = function (w) { return u.tags(realName(st, w)); };
    o.label = function (w) { return own(shortMaps(st).toReal, w) ? null : u.label(w); };
    if (u.menu) o.menu = function (info, e) {
      return u.menu(info && { x: info.x, y: info.y, chip: realChip(st, info.chip),
                              selection: realSel(st, info.selection) }, e);
    };
    return o;
  }
  function realChip(st, c) {
    if (!c || !st || !st.nat) return c;
    return { kind: c.kind, name: realName(st, c.name), a: realPos(st, c.a), b: realPos(st, c.b) };
  }
  function realSel(st, s) {
    if (!s || !st || !st.nat) return s;
    var out = {};
    for (var k in s) if (own(s, k)) out[k] = s[k];
    out.a = realPos(st, s.a); out.b = realPos(st, s.b);
    out.expr = realText(st, s.expr);
    return out;
  }

  function onCopy(st, e, cutting) {
    if (!st.nat || !e.clipboardData) return;
    var a = selA(st), b = selB(st);
    if (a === b) return;
    e.preventDefault();
    e.clipboardData.setData("text/plain", realText(st, raw(st).slice(a, b)));
    if (cutting) cut(st, a, b);
  }

  /* A real name in the field that has a short one — typed out in full and committed,
     pasted, left over from a reading that has since changed — becomes it. Never under
     the caret of a word being typed or a chip open for editing, and never mid-drag. */
  function settleShort(st) {
    if (!st.nat || st.drag || st.editing || st.typingAt >= 0) return false;
    var disp = st.nat.get();
    var v = realView(st), s = shortView(st, v.text);
    if (s.text === disp) return false;
    var there = function (p) { return mapPos(s.segs, mapPos(v.segs, p)); };
    var a = there(st.nat.ss()), b = there(st.nat.se());
    st.bubbles = st.bubbles.map(function (bb) {
      var c = {};
      for (var k in bb) if (own(bb, k)) c[k] = bb[k];
      c.a = there(bb.a); c.b = there(bb.b);
      return c;
    });
    st.multi = []; st.slot = null; st.opSlot = null;
    st.nat.set(s.text);
    st.nat.sel(a, b);
    return true;
  }

  /* The metrics the layer has to copy for its text to land on the field's text. Read
     off the live element rather than duplicated in CSS, so a field with its own
     padding or a different size is covered without this file knowing about it. */
  var COPY = ["fontFamily", "fontSize", "fontWeight", "fontStyle", "lineHeight",
              "letterSpacing", "wordSpacing", "textIndent", "textTransform",
              "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
              "borderTopWidth", "borderRightWidth", "borderBottomWidth",
              "borderLeftWidth", "boxSizing", "wordBreak", "overflowWrap"];

  function syncMetrics(st) {
    if (typeof getComputedStyle !== "function") return;
    var cs = getComputedStyle(st.el);
    for (var i = 0; i < COPY.length; i++) st.layer.style[COPY[i]] = cs[COPY[i]];
    st.layer.style.whiteSpace = st.multiline ? "pre-wrap" : "pre";
    st.layer.style.borderStyle = "solid";
    st.layer.style.borderColor = "transparent";
  }

  function syncScroll(st) {
    st.layer.scrollTop = st.el.scrollTop;
    st.layer.scrollLeft = st.el.scrollLeft;
  }

  /* ── Attach ────────────────────────────────────────────────────────────────
     opts:
       names()          the parameter names that exist, as an array
       tags(name)       the explicit tags on a parameter, or null
       guard(text,a,b)  true to leave the word at [a, b) entirely alone — not turned
                        into an operator, and above all not deleted for being a word
                        that is nothing. The plane box's datum is a face, not a
                        parameter, and the only thing here that could know that is
                        the field
       autoClose()      false to stop "(" writing its own ")" — again the plane box,
                        which grows brackets of its own around the offset
       unit()           what this field measures in ("mm", "deg", …). A bare number
                        committed in it is written with that unit, since that is what
                        the number means; "" or absent leaves numbers bare
       menu(info, e)    a right-click on the field, answered by the page: the native
                        menu is not this box's menu. info is {chip, selection, x, y}
                        — see onContextMenu                                          */
  function attach(el, opts) {
    if (!el || stateOf(el)) return stateOf(el);
    if (!el.parentNode || typeof document === "undefined" ||
        typeof document.createElement !== "function") return null;

    var layer;
    try {
      layer = document.createElement("div");
      layer.className = "chip-layer";
      el.parentNode.insertBefore(layer, el);
    } catch (_) { return null; }

    var st = {
      el: el, layer: layer, opts: opts || {},
      multiline: (el.tagName || "").toLowerCase() === "textarea",
      typingAt: -1, atoms: [], drag: null, busy: false, editing: null,
      // The space bar physically down over a held chip, deferred to its keyup — see
      // onKeyDown. `used` is set by an arrow that moved the chip while it was down,
      // which is what tells the keyup it was a modifier rather than a commit.
      space: null,
      // Where a deleted operator was, the caret waiting for its replacement — see
      // "An operator taken out". opFill carries a key typed into it to onInput.
      opSlot: null, opFill: false,
      // The gap this file wrote for an item that is not there yet (onInput's slot
      // rules). Kept so that deleting the word back to nothing takes the gap with it
      // rather than leaving a hole in the line — see the slot note in onInput.
      slot: null,
      // Bubbles and a multi-chip selection, on top of everything above: see the
      // "core mechanism" note in the phase-2 plan. bubbles is which "(" positions
      // are bubbles rather than plain typed brackets — tracked because the text
      // alone can't tell the two apart, and shifted across every edit (setValue)
      // rather than invalidated by one the way editing/typingAt are. The one you are
      // inside (F on a held bubble) carries scope: true — see scopeOf. multi is a
      // ⇧/⌘-click set of chips, waiting to become one (Space, or Cmd+G).
      bubbles: [], multi: [],
      // ⌘Z / ⌃Z — see "Undo" below. `last` is what the field held at the end of the
      // previous render, which is what an edit is measured against.
      hist: { past: [], future: [], last: null, stamp: 0, small: false },
      // Short names — see "Short names". `user` is the page's own options; `opts` is
      // what this file reads, which for a field with readings (opts.label) is
      // shortOpts's wrapping of them. nat is null for a field without.
      user: opts || {}, nat: null, short: null, shortVer: 0, seen: Object.create(null),
      xl: null
    };
    if (typeof st.user.label === "function") {
      var wrapped = shortOpts(st, st.user);
      if (installShort(st)) {
        st.opts = wrapped;
        buildShort(st);
        // What the page put in the field before this: the real expression, now shown short.
        st.nat.set(shortView(st, st.nat.get()).text);
      }
    }
    fields.push(st);

    try { el.parentNode.classList.add("chipbox"); } catch (_) {}
    try { el.classList.add("chipped"); } catch (_) {}
    syncMetrics(st);

    on(el, "input", function (e) { onInput(st, e); });
    on(el, "scroll", function () { syncScroll(st); placeCaret(st); });
    // A held arrow key moves the caret with no keyup until it lets go; the drawn caret
    // (placeCaret) has to follow it, or the field's own stays hidden where it no longer
    // should be.
    on(document, "selectionchange", function () {
      if (document.activeElement === el) placeCaret(st);
    });
    on(el, "focus", function () { render(st); });
    on(el, "blur", function () {
      st.typingAt = -1; st.editing = null; st.multi = []; st.space = null; st.slot = null;
      setScope(st, null); render(st);
    });
    on(el, "keydown", function (e) { onKeyDown(st, e); });
    on(el, "keyup", function (e) { onKeyUp(st, e); });
    on(el, "mousedown", function (e) { onMouseDown(st, e); });
    on(el, "contextmenu", function (e) { onContextMenu(st, e); });
    // ⌘C / ⌘X out of a field with short names: the clipboard gets the real expression,
    // never the readings' stand-in characters (see "Short names").
    on(el, "copy", function (e) { onCopy(st, e, false); });
    on(el, "cut", function (e) { onCopy(st, e, true); });
    on(el, "mousemove", function (e) { onMouseMove(st, e); });
    on(el, "mouseleave", function () { try { el.style.cursor = ""; } catch (_) {} });
    on(el, "select", function () { render(st); });

    render(st);
    return st;
  }

  /* A word is being typed when the keystroke that just landed ADDED a character to
     it. Anything else — a space, an operator, a deletion, a paste, a drag — ends the
     word, and it becomes an item on that keystroke rather than the next one.

     THE SPACEBAR IS THE COMMIT KEY. Everything the field decides about a word it
     decides here, on the space that ends it: see commitWord. */
  function onInput(st, e) {
    if (st.drag) { render(st); return; }       // the drag rewrites the text itself
    // The echo of fire() below: this file's own rewrite, already done. Handled as an
    // edit it would read as NOT typing — close the word, join it with a "+" and seal
    // it — which is what turned "25 3" into "25 + [3]" after one digit.
    // But the page may have answered that echo by rewriting the text again (the plane
    // line's brackets round its offset): that is taken on as it stands, or the layer
    // would go on painting the text from before it.
    if (st.busy) {
      if (st.prevValue !== undefined && raw(st) !== st.prevValue) {
        st.bubbles = shiftBubbles(st.bubbles, st.prevValue, raw(st));
        render(st);
      }
      return;
    }
    // What the field held before THIS keystroke — not st.el.value, which by the time
    // an `input` event fires has already had the browser's own edit applied to it.
    var before = st.prevValue !== undefined ? st.prevValue : raw(st);
    var t = raw(st), c = selA(st);
    var was = t, wasCaret = c;
    var collapsed = c === selB(st);
    var deleting = !!(e && e.inputType && /delete/i.test(e.inputType));
    var typed = (e && e.inputType === "insertText" && typeof e.data === "string")
      ? e.data : "";
    // A word is being TYPED only when this event actually put a character in it. The
    // other things that reach here — a paste, an undo, and above all the `input` this
    // file fires after rewriting the text itself — leave a caret sitting at the end of
    // a FINISHED word, and reading that as typing would re-open the chip the space had
    // just sealed.
    var typing = !deleting && collapsed && typed !== "";
    // This keystroke landed in an operator slot (opSlotKey took the placeholder out).
    var opFill = st.opFill; st.opFill = false;
    var reslot = null;

    if (collapsed && !deleting) {
      // A letter that landed on the end of a finished chip starts a NEW item beside it
      // (the gap written in, then space/join below add the "+") — see slotNeeded. A
      // DIGIT gets its "+" now: a number can only be an operand, so there is nothing
      // to wait for, and the number stays open for the rest of its digits. A letter
      // waits for its space — it may yet be an operator letter or a unit.
      if (typing && typed.length === 1 && !st.editing &&
          slotNeeded(st.atoms, c - 1, typed) && before.slice(0, c - 1) === t.slice(0, c - 1)) {
        var gap = /[0-9]/.test(typed) ? " + " : " ";
        t = t.slice(0, c - 1) + gap + t.slice(c - 1);
        c += gap.length;
        st.slot = { gap: gap, before: true, op: opFill };
      } else if (typing && typed.length === 1 && !st.editing &&
                 slotBefore(st.atoms, c - 1, typed) && before.slice(c - 1) === t.slice(c)) {
        // The same at a finished item's START: the letter is a new item in front of it,
        // with the gap written in AFTER it, so the caret stays on what is being typed.
        var gapAfter = /[0-9]/.test(typed) ? " + " : " ";
        t = t.slice(0, c) + gapAfter + t.slice(c);
        st.slot = { gap: gapAfter, before: false };
      }
      if (typed === "(" && autoCloses(st.opts)) {
        // A bracket opened is a bracket that has to be closed, so it is closed now and
        // the caret parked inside it: what you are about to type is the contents.
        // (Only a call gets here: any other "(" is a bubble — see openBubble — and ")"
        // never arrives as text at all, closeBubble having answered it on keydown.)
        t = t.slice(0, c) + ")" + t.slice(c);
      }
      // The spacebar never reaches here: it is answered in onKeyDown and is not a
      // character. What is left is ordinary typing, which only needs the gaps kept.
      var spaced = space(t, c, at(st, c, typing ? typingStart(t, c) : -1));
      var neat = tidy(spaced.text, spaced.caret);
      t = neat.text; c = neat.caret;
    }

    // THE GAP GOES BACK WITH THE WORD IT WAS MADE FOR. Typing beside a chip writes a
    // gap (and, for a digit, the "+" that joins it) before there is anything to put in
    // it — and deleting those letters again used to leave that hole behind: a line with
    // "A +  + B" in it, or a "+ " dangling off the end, made by the field rather than
    // typed. So the slot is remembered while the word is being spelled, and the moment
    // the word is deleted back to nothing the gap is taken out with it. Only ever the
    // gap THIS file wrote: an operator you typed yourself is yours, and stays.
    if (collapsed && deleting && st.slot && typingStart(t, c) === -1) {
      var g = st.slot.gap;
      if (st.slot.before && t.slice(c - g.length, c) === g) {
        t = t.slice(0, c - g.length) + t.slice(c); c -= g.length;
      } else if (!st.slot.before && t.slice(c, c + g.length) === g) {
        t = t.slice(0, c) + t.slice(c + g.length);
      }
      var tidied = tidy(t, c);
      t = tidied.text; c = tidied.caret;
      // Typed into an operator slot and spelled back to nothing: the slot again, with
      // its placeholder "+", rather than two items left touching.
      if (st.slot.op && c > 0 && t[c] === " ") {
        t = t.slice(0, c) + " +" + t.slice(c);
        reslot = { a: c + 1, c: c };
      }
      st.slot = null;
    } else if (!typing && typingStart(t, c) === -1) {
      st.slot = null;                     // no word left for the gap to belong to
    }

    // setValue reads st.el.value as "before" — no good here, since the browser has
    // already applied the keystroke to it by the time an `input` event fires. The
    // true "before" is whatever chips.js last rendered, tracked across every event
    // this file causes a render from (see render()'s own prevValue line).
    st.bubbles = shiftBubbles(st.bubbles, before, t);
    if (t !== was || c !== wasCaret) {
      setRaw(st, t);
      setSel(st, c, c);
    }
    st.typingAt = typing ? typingStart(t, c) : -1;
    if (reslot) st.opSlot = reslot;
    // An open chip grows and shrinks with what is typed into it — the arrows are held
    // to its letters (onArrow), so every edit lands inside it and moves only its end.
    if (st.editing) {
      st.editing = { a: st.editing.a,
                     b: Math.max(st.editing.a, st.editing.b + t.length - before.length) };
    }
    render(st);

    // The page listens for `input` too — the preview, the suggestion list, the width.
    // Rewriting the text here without saying so would leave all three answering for
    // the characters that were typed rather than the ones that are in the box.
    if (t !== was && !st.busy) {
      st.busy = true;
      try { fire(st); } finally { st.busy = false; }
    }
  }

  /* Where the word the caret is sitting at the END of begins, or -1.

     A "." counts too, as long as it isn't glued to a letter — a decimal point
     is still being typed, and treating it as the word's end committed "25" the
     moment the "." landed, before the digits meant to follow it. */
  function dotContinuesNumber(text, i) {
    return text[i] === "." && !/[A-Za-z_]/.test(text[i - 1] || "");
  }
  function typingStart(text, caret) {
    if (caret <= 0) return -1;
    if (!IDENT_CHAR.test(text[caret - 1] || "") &&
        !dotContinuesNumber(text, caret - 1)) return -1;
    var a = caret;
    while (a > 0 && (IDENT_CHAR.test(text[a - 1]) || dotContinuesNumber(text, a - 1))) a--;
    return a;
  }

  /* ── The space commits the word ────────────────────────────────────────────
     THE SPACEBAR IS NOT A CHARACTER. It never puts a space in the field — there is no
     use for one, since the gaps between items are this file's to write. It is the key
     that says "I have finished saying that", and what it does is decided by what is in
     front of it. Behind a word there are only four kinds:

       · an OPERATOR LETTER — f d s a for the four arithmetic operators, w e r o i u
         for the trig calls. It becomes the operator it stands for, because no
         parameter is one letter long and the space says you have finished saying it;
       · something FUSION KNOWS: a parameter, a constant, a function. It stays, and
         the space is what turns it into a chip;
       · the UNIT on the number in front of it, which is part of that number;
       · NONE OF THOSE — and once the space is pressed there is nothing it can still
         turn into. It goes. A word that means nothing is a typo, and leaving it in
         makes the expression fail to parse later for a reason the box never showed.

     Pure: text and a caret in, text and a caret out — `caret` is the END of the word,
     with no space anywhere near it. Returns null only when there is no word there at
     all, which is what tells the field the key had nothing to do.                    */
  function commit(opts, text, caret) {
    opts = opts || {};
    var w = wordBefore(text, caret);
    if (!w) return null;
    var word = text.slice(w.a, caret);
    // The field can be told a word is not up for any of this — the plane box's datum
    // is a face rather than a parameter, and would otherwise be swept away as a word
    // that is nothing.
    if (opts.guard && opts.guard(text, w.a, caret)) return null;
    // A number is already an item, and so is the unit written after one.
    //
    // A BARE number gets the field's own unit written onto it. "25" in a box measured
    // in millimetres is twenty-five millimetres and nothing else, and the unit is part
    // of the value rather than a word after it (see the num atom) — so the chip that
    // number becomes should say so, the same way the one Fusion hands back does. Only
    // a number with no unit of its own: "25mm" and "1in" are already complete, and a
    // field that does not know its unit (opts.unit absent, or a brand-new parameter
    // whose unit Fusion has yet to infer) adds nothing.
    if (/^[0-9]/.test(word)) {
      var u = unitOf(opts);
      if (u && /^[0-9.]+$/.test(word)) {
        return { text: text.slice(0, caret) + " " + u + text.slice(caret),
                 caret: caret + 1 + u.length };
      }
      return { text: text, caret: caret };
    }

    var op = operatorFor(word);
    if (op) {
      if (isFunction(op)) {
        var call = op + (autoCloses(opts) ? "()" : "(");
        return { text: text.slice(0, w.a) + call + text.slice(caret),
                 caret: w.a + op.length + 1 };
      }
      // With the gap after it, since that is where the next operand goes and space()
      // will not add one at the very end of the line — it keeps the caret glued to an
      // operator you have just TYPED, which is the opposite of what this key means.
      var gap = /^[ \t]/.test(text.slice(caret)) ? "" : " ";
      return { text: text.slice(0, w.a) + op + gap + text.slice(caret),
               caret: w.a + 1 + gap.length };
    }

    if (isFunction(word) || isConstant(word) || isKnown(opts, word) ||
        isUnit(opts, text, w.a, caret)) return { text: text, caret: caret };
    // Nothing at all, then — unless the vocabulary has not arrived yet, in which case
    // nothing is a name and this would quietly empty the box.
    if (!isKnown(opts, null)) return { text: text, caret: caret };
    // A name spelled in the wrong case is still that name. capitalise.js makes a typed
    // "hips" into "Hips", which a short name HIPS (an all-caps reading) never equals —
    // so the space swept away the very name it was typed out in full to make. Only when
    // exactly one name answers: a driven alias differs from its dimension by case alone.
    var fixed = knownSpelling(opts, word);
    if (fixed) return { text: text.slice(0, w.a) + fixed + text.slice(caret),
                        caret: w.a + fixed.length };
    return { text: text.slice(0, w.a) + text.slice(caret), caret: w.a };
  }

  /* The word ending exactly at `caret`, or null. */
  function wordBefore(text, caret) {
    var a = caret;
    while (a > 0 && IDENT_CHAR.test(text[a - 1])) a--;
    return a === caret ? null : { a: a, b: caret };
  }

  /* Is `word` a parameter this field is completing against? `null` asks the weaker
     question the deletion rule needs first: is there a vocabulary at all yet. */
  function isKnown(opts, word) {
    var names = (opts.names && opts.names()) || [];
    if (word === null) return names.length > 0;
    for (var i = 0; i < names.length; i++) if (names[i] === word) return true;
    return false;
  }
  /* The one name `word` spells ignoring case, or null — none, or more than one. */
  function knownSpelling(opts, word) {
    var names = (opts.names && opts.names()) || [], lw = word.toLowerCase(), hit = null;
    for (var i = 0; i < names.length; i++) {
      if (names[i].toLowerCase() !== lw) continue;
      if (hit !== null && hit !== names[i]) return null;
      hit = names[i];
    }
    return hit;
  }

  /* Is [a, b) the unit on a number, rather than a word of its own? Asked of the scan
     rather than of a list of units, so there is one place that decides what "25 mm"
     is and it is the same place that paints it. */
  function isUnit(opts, text, a, b) {
    var all = flatten(scan(text, { caret: null, typingAt: -1,
                                   isParam: function (w) { return isKnown(opts, w); } }));
    for (var i = 0; i < all.length; i++) {
      if (all[i].kind === "num" && all[i].a < a && all[i].b >= b) return true;
    }
    return false;
  }

  /* What this field measures in ("mm", "deg", …), or "" when it has no opinion — the
     unit a bare number typed into it is written in. The page's to answer: only it
     knows what the dimension or parameter being edited is measured in. */
  function unitOf(opts) {
    var v = opts && opts.unit;
    var u = typeof v === "function" ? v() : v;
    return typeof u === "string" ? u.trim() : "";
  }

  function autoCloses(opts) {
    var v = opts.autoClose;
    if (typeof v === "function") return v() !== false;
    return v !== false;
  }

  /* The field's context with the caret and the typing state said explicitly, for the
     passes that run over text the field does not hold yet. */
  function at(st, caret, typingAt) {
    var c = context(st);
    c.caret = caret; c.typingAt = typingAt;
    return c;
  }

  function on(el, type, fn) {
    if (el && typeof el.addEventListener === "function") el.addEventListener(type, fn);
  }

  /* ── Render ────────────────────────────────────────────────────────────────── */

  function context(st) {
    var names = (st.opts.names && st.opts.names()) || [];
    var set = {};
    for (var i = 0; i < names.length; i++) set[names[i]] = true;
    var focused = (typeof document !== "undefined" && document.activeElement === st.el);
    var caret = null;
    if (focused && selA(st) === selB(st)) caret = selA(st);
    return {
      caret: caret,
      typingAt: st.typingAt,
      editing: st.editing || null,
      guard: st.opts.guard || null,
      isParam: function (w) { return set[w] === true; },
      bubbles: st.bubbles
    };
  }

  function render(st) {
    // The page's names and readings as they are now, and any real name in the field
    // that has a short one made it — see "Short names".
    if (st.nat) { buildShort(st); settleShort(st); }
    // Out of a bubble since the last render (setScope): the one-chip rule it was spared
    // while you were in it applies again.
    if (st.settle && !st.drag) {
      st.settle = false;
      var pre = raw(st);
      collapseSingleChipBubbles(st);
      if (raw(st) !== pre) fire(st);
    }
    if (dropBareUnits(st)) fire(st);
    if (!st.drag && repairBrackets(st)) fire(st);
    var text = raw(st) || "";
    st.atoms = scan(text, context(st));
    // A bubble you were inside that has since stopped being one (its parens edited
    // away, or collapsed to a single chip) is simply not a place you can be any more.
    var sc = scopeOf(st);
    if (sc && !atomExactly(st, sc.a, sc.b)) setScope(st, null);
    // …and a caret standing INSIDE a bubble is in it, however it got there — a page that
    // wrote a pair round what was being typed (the plane line's offset) parks the caret
    // in it, and an arrow or a click there must work on its items, not take it whole.
    var cin = context(st).caret;
    if (!scopeOf(st) && cin !== null && !st.drag) {
      var all = flatten(st.atoms), inner = null;
      for (var gi = 0; gi < all.length; gi++) {
        var g = all[gi];
        if (g.kind === "group" && cin > g.a && cin < g.b &&
            (!inner || g.b - g.a < inner.b - inner.a)) inner = g;
      }
      if (inner) setScope(st, { a: inner.a, b: inner.b });
    }
    // An operator slot lasts only while its placeholder and the caret both are where it
    // left them — a click, an arrow, a blur, an edit elsewhere, and it is simply the "+".
    var os = st.opSlot;
    if (os && (text[os.a] !== "+" || typeof document === "undefined" ||
               document.activeElement !== st.el ||
               selA(st) !== os.c || selB(st) !== os.c)) st.opSlot = null;
    // What onInput compares the NEXT keystroke against — see its own "before" note.
    // Tracked here rather than at each mutation site because render() is the one
    // place every one of them already ends up.
    st.prevValue = text;
    // …and what UNDO compares it against. Ahead of the paint, so a field with no layer
    // at all (the headless harness) still keeps its history — see recordHistory.
    recordHistory(st);
    if (!st.layer) return;
    try {
      labelOf = st.opts.label || null;
      ghostAt = st.opSlot ? st.opSlot.a : -1;
      try {
        st.layer.innerHTML = paint(text, st.atoms, 0, text.length,
                                   st.opts.tags || null) + "​";
      } finally { labelOf = null; ghostAt = -1; }
      fitLabels(st);
      markHeld(st);
      markMulti(st);
      markScope(st);
      // Marked on every repaint, not only on the ones the drag itself causes: the page
      // repaints too (the preview, the width, the suggestion list all listen for the
      // `input` a drag step fires), and a repaint that did not re-mark left the chip in
      // hand dimmed along with the row it is moving through — which is exactly the one
      // thing that must stay bright.
      markDragged(st);
      syncScroll(st);
    } catch (_) {}
    placeCaret(st);
    // Several chips taken hold of leave the selection collapsed at the last one — a
    // blinking caret there would say typing goes in, and it does not (see Selecting).
    try { st.el.classList.toggle("multiholding", st.multi.length >= 2); } catch (_) {}
    // For a page that shows something off what is selected (the value box's "make a
    // parameter" button): every selection change this file makes ends up here, and
    // none of them fire an event the page could hear.
    if (st.opts.onRender) { try { st.opts.onRender(); } catch (_) {} }
  }

  /* A unit the field says goes WITHOUT saying (opts.bareUnits — "mm" in a millimetre
     box) is never left on a number: "25 mm" is the chip "25", "Width + 150mm" is
     "Width + 150". Done here, on every render, because render() is where every way text
     arrives ends up — a value the page loads, a paste, a suggestion, a commit — so there
     is no path that can put one back. Only on a FINISHED number: one still being typed
     (scan leaves it out, see isOpen) keeps its letters until the space that ends it.
     Returns whether it changed anything. */
  function dropBareUnits(st) {
    var v = st.opts.bareUnits;
    var units = (typeof v === "function" ? v() : v) || [];
    if (!units.length) return false;
    var text = raw(st) || "";
    var all = flatten(scan(text, context(st)));
    var cuts = [];
    for (var i = 0; i < all.length; i++) {
      if (all[i].kind !== "num") continue;
      var m = /[0-9.]([ \t]*)([A-Za-z]+)$/.exec(text.slice(all[i].a, all[i].b));
      if (!m || units.indexOf(m[2]) < 0) continue;
      cuts.push({ a: all[i].b - m[1].length - m[2].length, b: all[i].b });
    }
    if (!cuts.length) return false;
    var a = selA(st), b = selB(st);
    for (var k = cuts.length - 1; k >= 0; k--) {         // right to left: offsets hold
      var c = cuts[k], n = c.b - c.a;
      var adj = function (pos) { return pos >= c.b ? pos - n : pos > c.a ? c.a : pos; };
      a = adj(a); b = adj(b);
      setValue(st, raw(st).slice(0, c.a) + raw(st).slice(c.b));
    }
    try { setSel(st, a, b); } catch (_) {}
    return true;
  }

  /* A HELD chip is one you have taken hold of, and it says so with a ring drawn round
     it — not with the blue wash a text box puts under selected characters. The wash is
     the wrong answer twice over: it says "these letters are selected", when what is
     selected is one object; and it is the very thing that makes the row look like a line
     of text. So while the selection is exactly an item the field's own ::selection is
     painted away (.chipped.holding in theme.css) and the ring stands in for it.

     A HOLD IS A THING THE FOCUSED FIELD HAS. A blurred field keeps its selection range
     — that is how the browser restores it when you come back — so reading the range
     alone leaves a ring drawn round a chip in a box nobody is typing in. Tab through a
     dialog and every field you pass ends up ringed. Only the focused field may paint
     one. */
  function markHeld(st) {
    var focused = (typeof document !== "undefined" && document.activeElement === st.el);
    var a = selA(st), b = selB(st);
    var whole = focused && a !== b && wholeChips(st, a, b);
    try { st.el.classList.toggle("holding", !!whole); } catch (_) {}
    if (!whole) return;
    // Painted in document order, which is the order the atoms come out in — punctuation
    // included, and punctuation carries no data attributes to be found by.
    // Only the OUTERMOST chips in the range: a held bubble is one thing and gets one
    // ring, not a ring round each item inside it as well.
    var els = st.layer.querySelectorAll(".chip");
    for (var i = 0; i < els.length; i++) {
      var from = +els[i].dataset.a, to = +els[i].dataset.b;
      if (from >= a && to <= b && !outerIn(els[i], a, b)) els[i].classList.add("sel");
    }
  }

  /* THE CARET STANDS OFF A CHIP. A chip's fill spreads 4px past its letters (see .chip
     in theme.css) and the field's caret sits at the letters' edge — so a caret at a
     chip's end is drawn INSIDE the chip, reading as more letters on it. The field's own
     caret cannot be moved without moving the text, so beside a chip it is hidden
     (.owncaret) and this one is drawn in its place:

       · between two chips with a gap between them — the middle of the gap, whichever
         side of the space the caret is really on (the space is this file's, not a
         character anyone typed, so both sides are the one place);
       · against one chip, with nothing on the other side — off its edge by the same
         distance the middle case sits off each of its two, so the gap after a chip at
         the end of the line and the gap after the left chip of a pair are the same;
       · at the end of letters with a gap after them — against the last letter, since
         WebKit draws its own caret past the (word-spaced) space instead;
       · anywhere else (tight against a bracket, inside a word, no chip near) — the
         field's own caret, untouched.                                                */
  function placeCaret(st) {
    var el = st.el, cur = st.caret;
    var spot = null;
    try { spot = caretSpot(st); } catch (_) { spot = null; }
    try { el.classList.toggle("owncaret", !!spot); } catch (_) {}
    if (!spot) { if (cur) cur.style.display = "none"; return; }
    if (!cur) {
      cur = st.caret = document.createElement("div");
      cur.className = "chip-caret";
      el.parentNode.appendChild(cur);
    }
    cur.style.display = "block";
    cur.style.left = spot.x + "px";
    cur.style.top = spot.y + "px";
    cur.style.height = spot.h + "px";
    // Moving restarts the blink, the way the native caret shows solid while you type.
    cur.classList.remove("blink"); void cur.offsetWidth; cur.classList.add("blink");
  }

  var SPREAD = 4;                      // .chip's box-shadow spread, theme.css

  function caretSpot(st) {
    var el = st.el;
    if (typeof document === "undefined" || document.activeElement !== el) return null;
    var c = selA(st);
    if (c !== selB(st) || st.multi.length >= 2) return null;
    var text = raw(st) || "";
    var box = el.parentNode.getBoundingClientRect();
    var fr = el.getBoundingClientRect();
    // In an operator slot: the middle of the gap the operator left.
    var ghost = st.opSlot ? st.layer.querySelector(".chip-ghost") : null;
    var gr = ghost && firstRect(ghost);
    if (gr) return { x: Math.round((gr.left + gr.right) / 2 - box.left - 0.5),
                     y: gr.top - box.top, h: gr.height };
    // The chip ending / starting exactly here, and across one gap on either side. None
    // while a chip is OPEN (F): the caret is in its letters, not standing off a chip —
    // only the last case below (WebKit's caret past a space) can apply.
    var chips = !st.editing;
    var L = chips ? chipEdge(st, "b", c) : null, R = chips ? chipEdge(st, "a", c) : null;
    var sp = 0; while (text[c + sp] === " ") sp++;
    var sb = 0; while (text[c - sb - 1] === " ") sb++;
    var R2 = chips && !R && sp ? chipEdge(st, "a", c + sp) : null;
    var L2 = chips && !L && sb ? chipEdge(st, "b", c - sb) : null;
    var left = L || L2, right = R || R2;
    var lr = left && lastRect(left), rr = right && firstRect(right);
    var x, ref;
    if (lr && rr && (L2 || R2) && Math.abs(lr.top - rr.top) < 2) {
      x = (lr.right + rr.left) / 2; ref = lr;                  // the middle of a gap
    } else if (lr && !R && (!right || !sameLine(lr, rr)) &&
               (c === text.length || text[c] === "\n" || text[c] === " " || isHandle(st, c))) {
      x = lr.right + standOff(st); ref = lr;                   // after the last chip
    } else if (rr && !L && (!left || !sameLine(lr, rr)) &&
               (c === 0 || text[c - 1] === "\n" || text[c - 1] === " " || isHandle(st, c - 1))) {
      x = rr.left - standOff(st); ref = rr;                    // before the first chip
    } else if (!L && c > 0 && text[c] === " " && /\S/.test(text[c - 1])) {
      // Letters with a gap after them — a word typed in front of a chip. WebKit draws
      // its own caret on the far side of a word-spaced space, a whole gap off the
      // letters, so it is drawn here instead: against the last letter typed.
      var lr2 = rectIn(walker(st.layer), c - 1, c);
      if (!lr2) return null;
      x = lr2.right; ref = lr2;
    } else return null;
    // Scrolled out of the field: no caret at all rather than one drawn over the edge.
    if (x < fr.left || x > fr.right || ref.bottom < fr.top || ref.top > fr.bottom) return null;
    return { x: Math.round(x - box.left - 0.5), y: ref.top - box.top, h: ref.height };
  }

  function sameLine(p, q) { return !!p && !!q && Math.abs(p.top - q.top) < 2; }

  /* Is the character at `pos` one of a bubble's own (unpainted) brackets? Beside one, a
     chip is as good as at the end of the line: nothing visible follows it. */
  function isHandle(st, pos) {
    for (var i = 0; i < st.bubbles.length; i++) {
      if (st.bubbles[i].a === pos || st.bubbles[i].b - 1 === pos) return true;
    }
    return false;
  }

  /* The outermost painted chip whose [a, b) has `side` at `pos` — outermost, since that
     is the fill that reaches furthest. */
  function chipEdge(st, side, pos) {
    var els = st.layer.querySelectorAll(".chip[data-" + side + '="' + pos + '"]');
    for (var i = 0; i < els.length; i++) {
      var up = els[i].parentElement && els[i].parentElement.closest
        ? els[i].parentElement.closest('.chip[data-' + side + '="' + pos + '"]') : null;
      if (!up) return els[i];
    }
    return null;
  }
  function firstRect(e) { var r = e.getClientRects(); return r.length ? r[0] : null; }
  function lastRect(e)  { var r = e.getClientRects(); return r.length ? r[r.length - 1] : null; }

  /* How far off a lone chip's letters the caret sits: half of what one gap between two
     chips measures, so a caret at the end matches a caret in the middle of a pair. Read
     off the layer's own metrics (a space plus the field's word-spacing), cached once the
     font has laid out. */
  function standOff(st) {
    if (st.standOff) return st.standOff;
    var probe = document.createElement("span");
    probe.textContent = "x x";
    st.layer.appendChild(probe);
    var w3 = probe.getBoundingClientRect().width;
    probe.textContent = "xx";
    var w2 = probe.getBoundingClientRect().width;
    st.layer.removeChild(probe);
    var gap = w3 - w2;                                  // one space, word-spacing and all
    var off = gap > 0 ? gap / 2 : SPREAD + 3;
    if (gap > 0) st.standOff = off;
    return Math.max(off, SPREAD + 2);
  }

  /* Is there a chip around `el` that is itself wholly inside [a, b)? */
  function outerIn(el, a, b) {
    var up = el.parentElement && el.parentElement.closest ? el.parentElement.closest(".chip") : null;
    return !!up && +up.dataset.a >= a && +up.dataset.b <= b;
  }

  /* Inside a bubble (see enterScope), everything outside it steps back. A bubble that
     CONTAINS the one you are in is not outside it — only its own fill fades, so what
     you are in is still read against it. */
  function markScope(st) {
    var sc = scopeOf(st);
    try { st.layer.classList.toggle("scoped", !!sc); } catch (_) {}
    if (!sc) return;
    var els = st.layer.querySelectorAll(".chip");
    for (var i = 0; i < els.length; i++) {
      var from = +els[i].dataset.a, to = +els[i].dataset.b;
      if (from === sc.a && to === sc.b) els[i].classList.add("scope");
      else if (from < sc.a && to > sc.b) els[i].classList.add("out-anc");
      else if (!(from > sc.a && to < sc.b)) els[i].classList.add("out");
    }
  }

  /* A multi-selected chip (Shift-click — see shiftSelect) is several things about to
     become one bubble, so it gets its own mark rather than borrowing .sel: that ring
     means "this one object is held," and several rings drawn at once would read as
     several separate holds rather than one thing being built out of them. Self-
     healing like everything else here — a range that no longer matches a live atom
     (its chip was since deleted, say) is dropped rather than left stale. */
  function markMulti(st) {
    if (!st.multi.length) return;
    var all = flatten(st.atoms), valid = [];
    for (var i = 0; i < st.multi.length; i++) {
      var m = st.multi[i];
      for (var k = 0; k < all.length; k++) {
        if (all[k].a === m.a && all[k].b === m.b) { valid.push(m); break; }
      }
    }
    st.multi = valid;
    if (!valid.length) return;
    var els = st.layer.querySelectorAll(".chip");
    for (var e2 = 0; e2 < els.length; e2++) {
      var from = +els[e2].dataset.a, to = +els[e2].dataset.b;
      for (var v = 0; v < valid.length; v++) {
        if (valid[v].a === from && valid[v].b === to) { els[e2].classList.add("multi"); break; }
      }
    }
  }

  /* Does [a, b) hold WHOLE chips and nothing part-way through one? One chip is the
     ordinary case; the whole line is the other, which is what a field arrives selected
     as. A selection that cuts through a chip is a text selection and paints as one.
     A bubble or call AROUND the range is not cut through, it is where the range is: a
     chip held inside a bubble you entered (F) read as a text selection across the
     bubble, and the box painted it as selected letters instead of a ring. */
  function wholeChips(st, a, b) {
    var all = flatten(st.atoms), any = false;
    for (var i = 0; i < all.length; i++) {
      var at = all[i];
      if (at.b <= a || at.a >= b) continue;               // outside it
      if (at.a >= a && at.b <= b) { any = true; continue; }  // wholly inside
      if (at.items && at.a <= a && at.b >= b) continue;   // a container around it
      return false;                                        // cut through
    }
    return any;
  }

  /* Re-read the field after the page wrote to it directly — assigning to .value fires
     no event, so nothing else here would notice. Whatever was written was not TYPED,
     so nothing in it is a half-spelled word. */
  function refresh(el) {
    var st = stateOf(el);
    if (st) reread(st);
  }

  function reread(st) {
    st.typingAt = -1;
    st.editing = null;
    st.slot = null;
    adoptBrackets(st);
    // A pair that arrived around one thing ("-(Width)", "((A + B))") is not a bubble —
    // the same rule typing obeys — so its brackets go now, not on the first edit.
    // render() takes the new text as the baseline, so nothing downstream sees an edit.
    collapseSingleChipBubbles(st);
    render(st);
  }

  /* A bracket is never drawn in this box (see Bubbles: typing one), so a value that
     ARRIVES with plain ones — a parameter's own "(A + B) * 2" — has each matched pair
     taken on as a bubble. A call's own brackets are the call's, and are left alone.
     Every refresh re-reads the same text, so a pair already tracked is not added again
     — once per refresh, it used to be, and each copy painted a bubble of its own. */
  function adoptBrackets(st) {
    if (!autoCloses(st.opts)) return;
    var text = raw(st) || "";
    var all = flatten(scan(text, context(st))), add = [];
    for (var i = 0; i < all.length; i++) {
      if (all[i].kind !== "bracket" || all[i].name !== "(") continue;
      var close = matchParen(text, all[i].a, text.length);
      if (close < 0) continue;
      var a = all[i].a, b = close + 1, have = false;
      for (var k = 0; k < st.bubbles.length; k++) {
        if (st.bubbles[k].a === a && st.bubbles[k].b === b) { have = true; break; }
      }
      if (!have) add.push({ a: a, b: b });
    }
    if (add.length) st.bubbles = st.bubbles.concat(add);
  }

  /* The same rule held on EVERY render, not only on arrival: in a field whose brackets
     are bubbles, a plain bracket is never a state to stay in. Whatever edit lost a
     bubble's tracking (or took one of its parens), a matched pair is a bubble again and
     a lone bracket goes — so a slip shows for no keystrokes at all, rather than leaving
     "(" and ")" on screen for every key after it to misread. Says whether the text
     changed. */
  function repairBrackets(st) {
    if (!autoCloses(st.opts)) return false;
    var before = raw(st) || "";
    var stray = function () {
      var all = flatten(scan(raw(st) || "", context(st))), out = [];
      for (var i = 0; i < all.length; i++) if (all[i].kind === "bracket") out.push(all[i].a);
      return out;
    };
    if (!stray().length) return false;
    adoptBrackets(st);
    var lone = stray();
    if (lone.length) {
      var text = raw(st), a = selA(st), b = selB(st);
      for (var k = lone.length - 1; k >= 0; k--) {
        var i = lone[k];
        var pos = function (p) { return p - (p > i ? 1 : 0); };
        text = text.slice(0, i) + text.slice(i + 1);
        a = pos(a); b = pos(b);
        st.bubbles = st.bubbles.map(function (bub) { return moved(bub, pos(bub.a), pos(bub.b)); });
      }
      setRaw(st, text);
      setSel(st, a, b);
    }
    collapseSingleChipBubbles(st);
    return raw(st) !== before;
  }

  /* What a multi-selection covers once it has to become ONE thing (a bubble, or a
     parameter — see selectionOf): groupSpan's earliest-to-latest, found inside the
     innermost container that holds all of it, then snapped out to that container's
     whole items. Shift-click only ever picks siblings, so for it the snap changes
     nothing; Cmd-click can pick chips at different depths ("a" and the "b" inside
     "cos(b + c)"), and there the raw span would cut through the call's own
     brackets — snapped, it takes the whole call instead. Null under two chips. */
  function multiSpan(st, text) {
    return snapSpan(text, scan(text, context(st)), st.multi);
  }
  function snapSpan(text, atoms, ranges) {       // pure — multiSpan's work, testable
    var span = groupSpan(ranges);
    if (!span) return null;
    var c = containerOf(containers(text, atoms), span);
    if (!c) return null;
    var a = span.a, b = span.b;
    for (var i = 0; i < c.items.length; i++) {
      var it = c.items[i];
      if (it.b > span.a && it.a < a) a = it.a;
      if (it.a < span.b && it.b > b) b = it.b;
    }
    return { a: a, b: b };
  }

  /* Wrap the current multi-selection in a bubble — Space, or Cmd+G (see onKeyDown).
     Refused (a no-op, the key still swallowed) under two chips. What gets wrapped
     is multiSpan's whole-item span, not just the chips picked. The new bubble is left
     selected as a whole. */
  function tryCreateBubble(st) {
    var text = raw(st);
    var span = multiSpan(st, text);
    if (!span) return false;
    var made = insertGroup(text, span.a, span.b, st.bubbles);
    st.multi = [];
    st.bubbles = made.bubbles;
    setRaw(st, made.text);
    setSel(st, span.a, span.b + 2);
    st.typingAt = -1;
    fire(st);
    render(st);
    return true;
  }

  /* ── Keys ──────────────────────────────────────────────────────────────────
     A field of items has a key model of its own, and it is short:

       SPACE      commits. Never a character — see onSpace. Held with a chip taken
                  hold of it is a MODIFIER instead, and the arrows move that chip.
       ← →        move the CARET between the items — to the far side of the chip next
                  to it, where the next thing typed goes. Inside a word being typed or
                  a chip being edited they are the ordinary arrows, until the caret
                  reaches the edge of it and there is nothing left to walk.
       ⌘← ⌘→      TAKE HOLD of the items instead, one at a time. Selecting is a
                  deliberate act (a click, or this); the bare arrow is for getting the
                  caret where you want to type. Inside a bubble, only that bubble's
                  items.
       selected   typing does nothing; F Space A D are commands (onSelectingKey).
       ⌫ ⌦        take a whole item, with the operator it orphans. On a held OPERATOR
                  they leave the caret in its place for the next one, "+" if none is
                  typed — see "An operator taken out".
       ⌘Z ⇧⌘Z     back a step and forward again — this file's, not the browser's, since
                  a bubble is text plus the note that says it is one (see Undo).
       Esc        the dialog's key, not this file's — see the note above onKeyDown. */
  function onKeyDown(st, e) {
    var el = st.el;
    var a = selA(st), b = selB(st);
    var held = !st.editing && a !== b && isAtomRange(st, a, b);   // a chip is HELD

    // The caret standing where an operator was deleted: see "An operator taken out".
    if (st.opSlot && opSlotKey(st, e)) return;

    // UNDO, ahead of everything: ⌘Z / ⌃Z back, ⇧ with either forward. Always swallowed,
    // whether or not there is anything to step to — the browser's own undo knows the
    // characters but not which brackets are bubbles, so letting it through would put
    // back a different expression from the one that was there (see Undo).
    if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z")) {
      e.preventDefault();
      if (e.shiftKey) redo(st); else undo(st);
      return;
    }
    // Select-all is a text gesture, and outside an open chip there is no text here to
    // apply it to — only objects. Swallowed rather than left to sweep the whole row
    // blue, which is the very thing a field of items is not.
    if ((e.metaKey || e.ctrlKey) && (e.key === "a" || e.key === "A")) {
      if (!st.editing) e.preventDefault();
      return;
    }
    // Cmd+G bubbles the current multi-selection (see shiftSelect) — always
    // swallowed, whether or not there is one to act on, the same as the operator
    // letters swallow a key that turned out to have nothing to become.
    if ((e.metaKey || e.ctrlKey) && (e.key === "g" || e.key === "G")) {
      e.preventDefault();
      tryCreateBubble(st);
      return;
    }
    var multi = !st.editing && st.multi.length >= 2;

    // The arrows, ahead of the modifier guard below, because ⌘ is one of their own
    // modifiers here: ⌘← / ⌘→ are the ones that take hold of chips.
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      if (e.shiftKey || e.altKey) return;
      var right = e.key === "ArrowRight";
      if (st.editing) {
        // Inside an open chip they are the field's — up to its edge, and no further.
        var ed = st.editing;
        if (a === b && (right ? a >= ed.b : a <= ed.a)) e.preventDefault();
        return;
      }
      // SPACE HELD with a chip taken hold of: the arrows MOVE it, the way dragging
      // it would — see onSelectingKey's note on the deferred space.
      if (st.space && !(e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (held && nudge(st, right)) st.space.used = true;
        return;
      }
      if (e.metaKey || e.ctrlKey) {
        if (multi) st.multi = [];
        onArrow(st, e, right, held);
        return;
      }
      if (multi) st.multi = [];
      caretArrow(st, e, right, held);
      return;
    }

    if (e.metaKey || e.ctrlKey || e.altKey) return;

    // Escape is NOT this file's key. It used to close an open chip or let go of a held
    // one and stop there — which meant Escape cancelled the dialog only on a SECOND
    // press, the first one having been spent on the chip. But the dialog's own Escape
    // is the one keystroke that must always mean "cancel", chip or no chip, so this
    // file no longer answers it at all and lets it fall through to the page.

    // SELECTING. With a chip (or several) taken hold of, the box is not taking text at
    // all — see "SELECTING IS NOT TYPING" in the header. A handful of letters are
    // commands; every other printable key is swallowed.
    if (held || multi) {
      if (e.key === "Backspace" || e.key === "Delete") {
        if (!held) return;
        e.preventDefault();
        if (!takeOperator(st, a, b)) cut(st, a, b);
        return;
      }
      if (e.key.length !== 1 && e.key !== "Spacebar") return;   // Enter, Tab… the page's
      e.preventDefault();
      // SPACE IS DEFERRED TO ITS KEYUP while something is held, because it is two keys
      // at once here: pressed and let go it lets the chip go (or bubbles several), and
      // HELD it is the modifier the arrows read to move the chip instead. Acting on
      // the keydown would let go of the very chip Space+← was about to move. Nothing
      // else waits — only this one key has a second meaning while it is down.
      if (e.key === " " || e.key === "Spacebar") {
        if (!st.space) st.space = { used: false };
        return;
      }
      // ONE PRESS, ONE COMMAND. These letters are commands, and a key held a moment too
      // long repeats: F on a bubble entered it and then, on the repeat, opened the first
      // chip inside it for editing — so entering a bubble looked like it put its
      // contents into a text field. A command is what the press meant, not how long it
      // was held.
      if (e.repeat) return;
      onSelectingKey(st, e.key.toLowerCase(), held, a, b);
      return;
    }

    if (e.key === " " || e.key === "Spacebar") {
      if (onSpace(st)) e.preventDefault();
      return;
    }

    if (a !== b) return;                                // an ordinary selection
    var c = a;

    // A BRACKET IS NEVER TEXT HERE — see Bubbles: typing one. "(" opens a bubble and
    // puts you in it, ")" steps out of it. Not in a field that keeps its own brackets
    // (opts.autoClose false), nor inside an open chip.
    if ((e.key === "(" || e.key === ")") && !st.editing && autoCloses(st.opts)) {
      if (e.key === ")") { e.preventDefault(); closeBubble(st); }
      else if (openBubble(st)) e.preventDefault();
      return;
    }

    if (e.key === "Backspace" && raw(st)[c - 1] === "(" && raw(st)[c] === ")") {
      // The pair this field put there. It was opened and closed in one keystroke, so
      // it closes in one too — and an empty bubble you were in hands you back to the
      // one around it, rather than dropping you out of every level at once.
      e.preventDefault();
      var sc0 = scopeOf(st);
      if (sc0 && sc0.a === c - 1 && sc0.b === c + 1) {
        var g0 = atomExactly(st, sc0.a, sc0.b);
        setScope(st, g0 ? parentGroup(st, g0) : null);
      }
      cut(st, c - 1, c + 1);
      return;
    }
    // A BRACKET IS NEVER DELETED ALONE. Backspace just inside a bubble's (or a call's)
    // "(", or Delete just inside its ")", would take one half of the pair and leave the
    // other as a plain bracket — painted as text, and every key after it answering a
    // pair that no longer exists. The bubble goes whole, from outside it, or not at all.
    if (autoCloses(st.opts) &&
        ((e.key === "Backspace" && raw(st)[c - 1] === "(") ||
         (e.key === "Delete" && raw(st)[c] === ")"))) {
      e.preventDefault();
      return;
    }
    if (e.key === "Backspace" || e.key === "Delete") {
      var hit = e.key === "Backspace" ? endingAt(st, c) : startingAt(st, c);
      if (hit) { e.preventDefault(); cut(st, hit.a, hit.b); return; }
      // An operator next to the caret, between two items: the same as deleting it held
      // — its place is kept for the next one. Handed to danglingRun, it went with the
      // gaps either side of it and glued the two items into one word.
      var op = operatorBeside(st, c, e.key === "Backspace");
      if (op && takeOperator(st, op.a, op.b)) { e.preventDefault(); return; }
      // No item boundary here — which only happens sitting in a gap this file wrote:
      // the run of whitespace and a dangling operator left behind when the item it
      // used to join something to was deleted (see removeRange — a TRAILING operator
      // is deliberately not orphaned, since "Width + " is a line still being typed).
      // Answered here rather than left to the browser is what keeps repeated deletes
      // at one press per chip instead of three: without this, the space goes on one
      // press and the operator on the next, with nothing visibly happening at either.
      var junk = danglingRun(raw(st), c, e.key === "Backspace");
      if (!junk) return;
      e.preventDefault();
      cut(st, junk.a, junk.b);
    }
  }

  /* The space bar coming back up. If nothing used it as a modifier while it was down
     (Space+← / Space+→, see nudge), it was the ordinary commit key after all and is
     answered now — one keystroke, one meaning, decided by how it ended. */
  function onKeyUp(st, e) {
    if (e && (e.key === " " || e.key === "Spacebar")) {
      var sp = st.space;
      st.space = null;
      if (sp && !sp.used) {
        var a = selA(st), b = selB(st);
        var held = !st.editing && a !== b && isAtomRange(st, a, b);
        if (held || st.multi.length >= 2) { onSelectingKey(st, " ", held, a, b); return; }
      }
    }
    render(st);
  }

  /* The run of whitespace, and the operator it may be hiding, that Backspace/Delete
     just landed in rather than on an item's edge. Only ever a TAIL or a HEAD: tidy()
     already keeps anything mid-line down to the single space between two items, and
     that space is the boundary onKeyDown answers before ever reaching here. A run can
     only build up past the last item or before the first, where nothing follows it to
     keep tidy() honest. isBinary is reused rather than duplicated: a sign belongs to
     what it signs and is never eaten here, only an operator with an operand on its
     far side is. */
  function danglingRun(text, pos, back) {
    var i = pos, seenSpace = false;
    if (back) {
      if (i <= 0) return null;
      while (i > 0 && /[ \t]/.test(text[i - 1])) { i--; seenSpace = true; }
      if (i > 0 && OPERATOR.test(text[i - 1]) && isBinary(text, i - 1, 0)) {
        i--;
        while (i > 0 && /[ \t]/.test(text[i - 1])) i--;
        return { a: i, b: pos };
      }
      return seenSpace ? { a: i, b: pos } : null;
    }
    if (i >= text.length) return null;
    while (i < text.length && /[ \t]/.test(text[i])) { i++; seenSpace = true; }
    if (i < text.length && OPERATOR.test(text[i]) && isBinary(text, i, 0)) {
      i++;
      while (i < text.length && /[ \t]/.test(text[i])) i++;
      return { a: pos, b: i };
    }
    return seenSpace ? { a: pos, b: i } : null;
  }

  /* ── Space ─────────────────────────────────────────────────────────────────
     With nothing selected (a held chip's Space is onSelectingKey's):

       a chip is OPEN (F)     → close it: commit what is in it, caret just past it;
       a word is being TYPED  → commit it (see commit), caret just past what it made,
                                so the next keystroke starts the next item;
       nothing                → nothing. The space is swallowed either way.

     Returns whether the key was consumed. The one time it is not is a GUARDED word —
     the plane box's datum — where the space really is a space, because it is what
     separates the face from the offset measured off it.                             */
  function onSpace(st) {
    var el = st.el, a = selA(st), b = selB(st);

    // Inside an open chip the space is what closes it again.
    if (st.editing) { closeEdit(st); return true; }
    if (a !== b) return true;                  // some other selection: no space, no more

    var text = raw(st);
    var w = wordBefore(text, a);
    if (w && st.opts.guard && st.opts.guard(text, w.a, w.b)) return false;

    var done = commit(st.opts, text, a);
    if (!done) return true;
    seal(st, done.text, done.caret);
    return true;
  }

  /* Write what a commit produced, caret just past it. The chip is NOT taken hold of:
     committing is finishing, and the next keystroke is the next item (slotNeeded
     writes the gap), not a command on the one just made. */
  function seal(st, text, caret) {
    var spaced = space(text, caret, at(st, caret, -1));
    st.editing = null;
    st.slot = null;                      // the word it was made for is an item now
    write(st, spaced.text, spaced.caret);
  }

  /* Open a held chip for editing — its own text field. ALL of its text is selected, so
     the common act (this is the wrong parameter, here is the right one) is one keystroke
     away, and the letters are still there to click into for the other act. A CALL opens
     at its argument: the call is not what you meant to rewrite, what is inside it is.
     Punctuation opens at nothing — there is no word in a "+". */
  function openItem(st, a, b) {
    var all = flatten(st.atoms), it = null;
    for (var i = 0; i < all.length; i++) if (all[i].a === a && all[i].b === b) it = all[i];
    if (!it || !isItem(it)) return;
    var r = it.kind === "fn" ? it.arg : { a: a, b: b };
    st.editing = { a: r.a, b: r.b };
    st.typingAt = -1;
    setSel(st, r.a, r.b);
    render(st);
  }

  /* Select the WHOLE field as an open edit — the same state openItem puts one chip
     into, covering everything instead. For a dialog that arrives prefilled with a
     plain value someone did not type (a measured dimension, say): the ordinary
     textbox expectation is that the first keystroke replaces it, and without this the
     value comes back HELD instead — one bare number is one atom — where a held chip's
     rule is exactly backwards (typing does nothing at all — see Selecting).
     Pressing Space with nothing else typed seals the untouched value back into a chip,
     the same as it would opening any other chip this way (see onSpace/closeEdit). */
  function editAll(el) {
    var st = stateOf(el);
    if (!st) return false;
    var len = raw(st).length;
    st.editing = { a: 0, b: len };
    st.typingAt = -1;
    setSel(st, 0, len);
    render(st);
    return true;
  }

  /* Park the caret at the end of the line, nothing held — what a dialog wants arriving
     at a real expression rather than a bare literal (see editAll): every item already
     there stays a chip, and the first keystroke starts the next item beside the last
     (onInput slots the gap). The same place the A key lets go to. */
  function caretAtEnd(el) {
    var st = stateOf(el);
    if (!st) return false;
    st.multi = [];
    setScope(st, null);
    st.typingAt = -1;
    setCaret(st, raw(st).length);
    return true;
  }

  /* Close an open chip: commit whatever is in it now, caret just past the result. The
     word is the run the caret is in, grown to its end, so it works whether the text was
     left alone or typed over. */
  function closeEdit(st) {
    var text = raw(st), pos = selB(st);
    while (pos < text.length && IDENT_CHAR.test(text[pos])) pos++;
    st.editing = null;
    var done = commit(st.opts, text, pos);
    if (done) seal(st, done.text, done.caret);
    else { st.typingAt = -1; setCaret(st, pos); }
  }

  /* ── Selecting ─────────────────────────────────────────────────────────────
     With one item held, or several multi-selected, the box is SELECTING: typing puts
     nothing in it, and four letters are commands instead —

       F      open it: a chip for editing, its text selected (openItem); a bubble is
              ENTERED instead, so its own items can be walked and taken one at a time
       Space  one item: let go, caret just past it, ready to type the next thing
              (onInput slots a gap so the letters never land ON the chip).
              Several: wrap them in a bubble (tryCreateBubble)
       A      let go of everything; caret to the end of the line
       D      step back out of the bubble you are in, holding it                     */
  function onSelectingKey(st, k, held, a, b) {
    // A held OPERATOR is changed by the keys that make one — f d s a, or the character
    // itself — and stays held, so it can be changed again. Ahead of the commands below:
    // there is nothing for F to open or A to let go into on an operator you are
    // pointing at to change.
    if (held && swapOperator(st, k, a, b)) return;
    if (k === "a") {
      st.multi = [];
      setScope(st, null);
      st.typingAt = -1;
      setCaret(st, raw(st).length);
      return;
    }
    if (k === "d") { exitScope(st); return; }
    if (k === " ") {
      if (st.multi.length >= 2) { tryCreateBubble(st); return; }
      st.typingAt = -1;
      setCaret(st, b);
      return;
    }
    if (k === "f" && held) {
      var it = atomExactly(st, a, b);
      if (it && it.kind === "group") enterScope(st, it);
      else openItem(st, a, b);
    }
  }

  /* ── Inside a bubble ───────────────────────────────────────────────────────
     A bubble is ONE thing until you go into it (F): clicks and arrows take the bubble
     whole, not whatever inside it the pointer happened to land on. Once inside, they
     work on its own items, everything outside it is dimmed, and D comes back out.

     The bubble you are in is marked on its own entry in st.bubbles (scope: true)
     rather than kept as a range of its own, so it shifts through every edit exactly
     the way the bubble itself does, and goes when the bubble goes — no second set of
     positions to keep honest. */
  function scopeOf(st) {
    for (var i = 0; i < st.bubbles.length; i++) if (st.bubbles[i].scope) return st.bubbles[i];
    return null;
  }
  function setScope(st, range) {
    // Leaving a bubble is when it is held to the one-chip rule again: while you are in
    // it (or in one inside it) it may hold one chip or none — it has only just been
    // opened with "(" — so collapseSingleChipBubbles spares it, and the next render
    // settles it once you are out (see render).
    var was = scopeOf(st);
    if (was && !(range && range.a === was.a && range.b === was.b)) st.settle = true;
    var out = [];
    for (var i = 0; i < st.bubbles.length; i++) {
      var bub = st.bubbles[i];
      var on = !!range && bub.a === range.a && bub.b === range.b;
      out.push(on === !!bub.scope ? bub : { a: bub.a, b: bub.b, scope: on || undefined });
    }
    st.bubbles = out;
  }
  function moved(bub, a, b) {
    return bub.scope ? { a: a, b: b, scope: true } : { a: a, b: b };
  }

  function atomExactly(st, a, b) {
    var all = flatten(st.atoms), best = null;
    for (var i = 0; i < all.length; i++) {
      if (all[i].a === a && all[i].b === b && (!best || all[i].kind === "group")) best = all[i];
    }
    return best;
  }

  /* The atoms from the top down to `target`, or [] if it is not in there. */
  function chainTo(atoms, target) {
    for (var i = 0; i < atoms.length; i++) {
      var at = atoms[i];
      if (at === target) return [at];
      if (at.items && at.a <= target.a && at.b >= target.b) {
        var deeper = chainTo(at.items, target);
        if (deeper.length) return [at].concat(deeper);
      }
    }
    return [];
  }

  function inside(scope, at) {
    return !scope || (at.a > scope.a && at.b < scope.b);
  }

  /* What a click or an arrow landing on `atom` takes hold of at the current level: the
     outermost bubble between the scope and it, or the atom itself when there is none.
     A call's argument is still reached directly — only bubbles are closed. Pure. */
  function levelItem(atoms, atom, scope) {
    var chain = chainTo(atoms, atom);
    var start = 0;
    if (scope) {
      for (var i = 0; i < chain.length; i++) {
        if (chain[i].kind === "group" && chain[i].a === scope.a && chain[i].b === scope.b) {
          start = i + 1; break;
        }
      }
    }
    for (var k = start; k < chain.length; k++) if (chain[k].kind === "group") return chain[k];
    return atom;
  }

  function enterScope(st, g) {
    setScope(st, { a: g.a, b: g.b });
    st.multi = [];
    // Going IN is not opening anything: the bubble's items are there to be clicked and
    // taken one at a time, and a chip is opened for typing only by F on that chip. Any
    // edit left open elsewhere in the line is closed by the same act.
    st.editing = null;
    st.typingAt = -1;
    for (var i = 0; i < g.items.length; i++) {
      if (isItem(g.items[i])) { select(st, g.items[i].a, g.items[i].b); return; }
    }
    setCaret(st, g.inner.a);
  }

  /* Out of the bubble, holding it — into whichever bubble encloses it, if any. */
  function exitScope(st) {
    var sc = scopeOf(st);
    if (!sc) return;
    var g = atomExactly(st, sc.a, sc.b);
    setScope(st, g ? parentGroup(st, g) : null);
    st.multi = [];
    select(st, sc.a, sc.b);
  }

  /* The bubble directly around group atom `g`, or null. */
  function parentGroup(st, g) {
    var chain = chainTo(st.atoms, g);
    for (var i = chain.length - 2; i >= 0; i--) if (chain[i].kind === "group") return chain[i];
    return null;
  }

  /* Out of the bubble you are in WITHOUT holding it — the caret just past it (or just
     before it, leaving leftward), into whichever bubble encloses it. What → at a
     bubble's last item and ")" do: you have finished filling it, and the next thing
     typed goes beside it. */
  function leaveScope(st, right) {
    var sc = scopeOf(st);
    if (!sc) return false;
    var g = atomExactly(st, sc.a, sc.b);
    setScope(st, g ? parentGroup(st, g) : null);
    st.multi = [];
    st.typingAt = -1;
    setCaret(st, right ? sc.b : sc.a);
    return true;
  }

  /* ── Bubbles: typing one ───────────────────────────────────────────────────
     A bracket in this box is never a character you see. "(" OPENS A BUBBLE at the caret
     — the pair written in, empty, and you put inside it (scope) — so everything typed
     and committed from there lands in it, however many chips that comes to. It is let
     off the one-chip rule while you are in it (collapseSingleChipBubbles), since it
     starts with none.

     You leave it by clicking outside it, by → past its last item (caretArrow), or by
     ")". After a word still being spelled, ")" commits the word first, the way Space
     would, then steps out.

     "(" straight after a function's name is still the call it always was: "cos(" is
     cos(), not cos beside a bubble. */
  function openBubble(st) {
    var text = raw(st), c = selA(st);
    var w = wordBefore(text, c);
    if (w && isFunction(text.slice(w.a, c))) return false;       // a call: onInput's
    if (w && st.opts.guard && st.opts.guard(text, w.a, w.b)) return false;
    var bubbles = st.bubbles;
    if (w) {
      var done = commit(st.opts, text, c);
      if (done) {
        bubbles = shiftBubbles(bubbles, text, done.text);
        text = done.text; c = done.caret;
      }
    }
    // Shifted by hand, not through insertGroup: an empty "()" written beside another
    // bracket — "((" — is a diff shiftBubbles cannot place, and it would misplace the
    // outer bubble's close. Here the insertion point is known exactly.
    var made = { text: text.slice(0, c) + "()" + text.slice(c) };
    var was = scopeOf(st);
    st.bubbles = bubbles.map(function (b) {
      return { a: b.a >= c ? b.a + 2 : b.a, b: b.b > c ? b.b + 2 : b.b };
    }).concat([{ a: c, b: c + 2, scope: true }]);
    if (was) st.settle = true;
    setRaw(st, made.text);
    st.multi = [];
    st.editing = null;
    st.slot = null;
    st.typingAt = -1;
    // The gaps and the "+" to whatever it now sits beside — written the same way a
    // committed chip's are (seal), measured against the text with the pair in it.
    var spaced = space(made.text, c + 1, at(st, c + 1, -1));
    write(st, spaced.text, spaced.caret);
    return true;
  }

  /* ")" — out of the innermost bubble (or call) the caret is in, caret just past it. */
  function closeBubble(st) {
    var text = raw(st), c = selA(st);
    var w = wordBefore(text, c);
    if (w && !(st.opts.guard && st.opts.guard(text, w.a, w.b))) {
      var done = commit(st.opts, text, c);
      if (done) { seal(st, done.text, done.caret); c = selA(st); }
    }
    var all = flatten(st.atoms), best = null, size = Infinity;
    for (var i = 0; i < all.length; i++) {
      var r = all[i].kind === "group" ? all[i].inner : all[i].kind === "fn" ? all[i].arg : null;
      if (r && c >= r.a && c <= r.b && r.b - r.a < size) { best = all[i]; size = r.b - r.a; }
    }
    if (!best) return;                                    // nothing to close: swallowed
    var sc = scopeOf(st);
    if (best.kind === "group" && sc && best.a <= sc.a && best.b >= sc.b) {
      setScope(st, parentGroup(st, best));
    }
    st.typingAt = -1;
    setCaret(st, best.b);
  }

  /* Does typing `ch` at `pos` land on the END of a finished item? Then it is the start
     of a new item beside it, never more letters on that one — a chip is edited with F
     and only with F. Asked of the atoms as they were BEFORE the keystroke. Pure. */
  function slotNeeded(atoms, pos, ch) {
    if (!IDENT_CHAR.test(ch || "")) return false;
    var all = flatten(atoms);
    for (var i = 0; i < all.length; i++) {
      if (all[i].b === pos && isItem(all[i])) return true;
    }
    return false;
  }

  /* …and on its START: the caret is before an item, never in it, so a letter typed
     there is a new item in front of it rather than more letters on it. Pure. */
  function slotBefore(atoms, pos, ch) {
    if (!IDENT_CHAR.test(ch || "")) return false;
    var all = flatten(atoms);
    for (var i = 0; i < all.length; i++) {
      if (all[i].a === pos && isItem(all[i])) return true;
    }
    return false;
  }

  /* ── Arrows ────────────────────────────────────────────────────────────────
     THE BARE ARROW MOVES THE CARET, hopping a whole chip at a time: a chip has no
     inside to walk through (its letters are reached with F, and only with F), so the
     only places the caret can be are the gaps between the items — and getting it into
     one of those is what you want the arrow for, since that is where the next thing
     you type goes.

     ⌘ WITH IT TAKES HOLD of the items instead, one at a time (onArrow). Selecting is
     a deliberate act here, the keyboard twin of clicking a chip, rather than something
     that happens on the way past.

     Inside a word still being typed — or a chip opened for editing — both are the
     ordinary arrows, right up to its edge, and the keystroke that would step off the
     end of the letters stops there. */
  function caretArrow(st, e, right, held) {
    var el = st.el, a = selA(st), b = selB(st);
    // Holding one: the arrow LETS GO, to the side it points at. The same place the
    // chip's own Space leaves the caret, reached with the key you were already using.
    if (a !== b) { e.preventDefault(); st.typingAt = -1; setCaret(st, right ? b : a); return; }
    var open = openAt(st, a);
    if (open && (right ? a < open.b : a > open.a)) return;    // letters left to walk
    e.preventDefault();
    if (open) return;                                         // …and no further
    var found = neighbour(st, a, right);
    // Past the last item of the bubble you are in (or before its first): out of it,
    // to that side — the way ")" leaves one.
    if (!found && leaveScope(st, right)) return;
    st.typingAt = -1;
    setCaret(st, found ? (right ? found.b : found.a)
                       : (right ? raw(st).length : 0));
  }

  /* Move the held chip one place along its own row — Space held, then ← or →. The
     same permutation a drag makes (planMove: the operands move, the operator skeleton
     stays), so the two routes can never disagree about what moving an item means.
     Returns whether anything moved; at either end of the row nothing does. */
  function nudge(st, right) {
    var text = raw(st), a = selA(st), b = selB(st);
    var range = { a: a, b: b };
    var cont = containers(text, st.atoms);
    var from = containerOf(cont, range);
    if (!from) return false;
    var i = indexOfItem(from, range);
    if (i < 0) return false;
    // planMove's dstIndex is a SLOT, so one place to the right is two slots along.
    var slot = right ? i + 2 : i - 1;
    if (slot < 0 || slot > from.items.length) return false;
    var plan = planMove(text, range, from, slot, cont);
    if (!plan) return false;
    setValue(st, plan.text);
    setSel(st, plan.range.a, plan.range.b);
    fire(st);
    render(st);
    return true;
  }

  function onArrow(st, e, right, held) {
    var el = st.el, a = selA(st), b = selB(st);
    if (held) {
      var next = neighbour(st, right ? b : a, right);
      e.preventDefault();
      if (next) select(st, next.a, next.b);
      else setCaret(st, right ? b : a);
      return;
    }
    if (a !== b) return;                               // an ordinary selection
    var open = openAt(st, a);
    if (open && (right ? a < open.b : a > open.a)) return;   // letters left to walk
    // At the edge of a word still being typed: it is where you are working, so the
    // arrow stops there rather than stepping out onto the chip beside it.
    if (open) { e.preventDefault(); return; }
    var found = neighbour(st, a, right);
    if (!found) return;
    e.preventDefault();
    select(st, found.a, found.b);
  }

  /* The nearest chip on one side of `pos`, the outermost where several end together —
     the call rather than the argument inside it.

     EVERY chip, punctuation included: an operator is painted as one and reads as one, so
     an arrow that stepped straight over it would be walking a different row from the one
     on the screen. */
  function neighbour(st, pos, right) {
    var all = flatten(st.atoms), best = null, sc = scopeOf(st);
    for (var i = 0; i < all.length; i++) {
      var it = all[i];
      if (!inside(sc, it)) continue;             // inside a bubble, only its own items
      if (right) {
        if (it.a < pos) continue;
        if (!best || it.a < best.a || (it.a === best.a && it.b > best.b)) best = it;
      } else {
        if (it.b > pos) continue;
        if (!best || it.b > best.b || (it.b === best.b && it.a < best.a)) best = it;
      }
    }
    return best && levelItem(st.atoms, best, sc);
  }

  /* The run of letters the caret is in that is NOT a finished item — a word being
     spelled, or a chip opened for editing. Null when the caret is on a chip or on
     nothing, which is when the arrows belong to the items rather than to the text. */
  function openAt(st, pos) {
    var t = raw(st), a = pos, b = pos;
    while (a > 0 && IDENT_CHAR.test(t[a - 1])) a--;
    while (b < t.length && IDENT_CHAR.test(t[b])) b++;
    if (a === b) return null;
    var all = flatten(st.atoms);
    for (var i = 0; i < all.length; i++) {
      if (isItem(all[i]) && all[i].a <= a && all[i].b >= b) return null;
    }
    return { a: a, b: b };
  }

  /* The item whose end is exactly here — what Backspace and ArrowLeft act on. A call
     is answered before its argument, so backspacing at the end of "cos(Taper)" takes
     the whole call rather than unpicking it from the inside. */
  function endingAt(st, pos) {
    var all = flatten(st.atoms);
    var best = null;
    for (var i = 0; i < all.length; i++) {
      if (!isItem(all[i])) continue;
      if (all[i].b === pos && (!best || all[i].a < best.a)) best = all[i];
    }
    return best;
  }
  function startingAt(st, pos) {
    var all = flatten(st.atoms);
    var best = null;
    for (var i = 0; i < all.length; i++) {
      if (!isItem(all[i])) continue;
      if (all[i].a === pos && (!best || all[i].b > best.b)) best = all[i];
    }
    return best;
  }

  /* Remove [a, b) and close the gap.

     An item taken out of the middle of an expression leaves an operator with nothing
     on one side of it — "A + B + C" without B is "A + + C", which is not something to
     hand to Fusion and not something anybody meant. So an operator ORPHANED by the
     removal goes with it. Orphaned is the whole test: an operator with another
     operator on its far side, or one left at the very start of the line. A TRAILING
     operator is not orphaned — "A + " is an expression halfway through being typed,
     and eating the + there would delete the thing you were about to use.

     Pure, so the rule is asserted in the tests rather than by deleting things in a
     window and looking at them. */
  function removeRange(text, a, b) {
    var before = text.slice(0, a), after = text.slice(b);
    if (/ $/.test(before) && /^ /.test(after)) after = after.slice(1);
    var m = /([+\-*\/^%,])[ \t]*$/.exec(before);
    if (m && /^[ \t]*[+\-*\/^%,]/.test(after)) {
      before = before.slice(0, m.index);
    } else if (!before.trim() && /^[ \t]*[+*\/^%,]/.test(after)) {
      // A leading operator, with the thing it applied to gone. Not +/- : those read as
      // a sign on whatever now comes first, which is a perfectly good line.
      after = after.replace(/^[ \t]*[+*\/^%,][ \t]*/, "");
    }
    return { text: before + after, caret: before.length };
  }

  function cut(st, a, b) {
    var r = removeRange(raw(st), a, b);
    write(st, r.text, r.caret);
  }

  /* ── An operator taken out ─────────────────────────────────────────────────
     Deleting the operator between two items is not asking for them to touch — two
     items side by side are ADDED anyway (see join) — it is asking for a different one.
     So the operator goes and the caret is left standing where it was, and what is typed
     there next is the new one: + - * / directly, or an operator letter and the space
     that commits it. Leave without typing one and it is "+".

     That "+" is in the text the whole time rather than written in on the way out: a
     placeholder, painted out (.chip-ghost) so the gap reads as empty, with the caret
     drawn in the middle of it. So there is no exit — Enter, a click, Tab, blur — that
     has to remember to fill the gap; every one of them finds "+" already there. Only a
     keystroke typed INTO the slot takes the placeholder out (opSlotKey).

     st.opSlot = { a, c }: a is where the placeholder is, c where the field's own caret
     stands — the end of the item before it, the ordinary place for a caret. render()
     drops the slot the moment either stops being true. */
  function takeOperator(st, a, b) {
    var text = raw(st), op = atomExactly(st, a, b);
    if (!op || op.kind !== "op" || text[a] === ",") return false;
    var c = a; while (c > 0 && /[ \t]/.test(text[c - 1])) c--;
    var r = b; while (r < text.length && /[ \t]/.test(text[r])) r++;
    if (!endingAt(st, c) || !startingAt(st, r)) return false;
    st.opSlot = { a: a, c: c };
    st.typingAt = -1;
    var t = text.slice(0, a) + "+" + text.slice(b);
    if (t !== text) setValue(st, t);
    setSel(st, c, c);
    if (t !== text) fire(st);
    render(st);
    return true;
  }

  /* The operator atom Backspace (back) or Delete would reach from `pos` across nothing
     but spaces, or null. */
  function operatorBeside(st, pos, back) {
    var text = raw(st), i = pos;
    if (back) { while (i > 0 && /[ \t]/.test(text[i - 1])) i--; i--; }
    else { while (i < text.length && /[ \t]/.test(text[i])) i++; }
    if (i < 0 || i >= text.length) return null;
    var it = atomExactly(st, i, i + 1);
    return it && it.kind === "op" ? it : null;
  }

  /* The held operator at [a, b) turned into the one key `k` stands for (an operator
     letter, or + - * / ^ % typed as itself). False if [a, b) is not an operator or `k`
     is not one; the comma between a call's arguments is not an operator to change. */
  function swapOperator(st, k, a, b) {
    var text = raw(st), it = atomExactly(st, a, b);
    if (!it || it.kind !== "op" || text[a] === ",") return false;
    var op = /^[+\-*\/^%]$/.test(k) ? k : operatorFor(k);
    if (!op || op.length !== 1) return false;       // the trig letters make calls, not this
    if (op !== text[a]) write(st, text.slice(0, a) + op + text.slice(b), a);
    select(st, a, a + 1);
    return true;
  }

  /* A key pressed with the caret in an operator slot. True when it was answered here;
     false lets onKeyDown carry on with it, the slot already given up. */
  function opSlotKey(st, e) {
    if (/^(Shift|Meta|Control|Alt|CapsLock|Fn)$/.test(e.key)) return false;
    var os = st.opSlot, text = raw(st), k = e.key;
    st.opSlot = null;
    if (e.metaKey || e.ctrlKey || e.altKey) { render(st); return false; }
    var next = os.a + 1; while (next < text.length && /[ \t]/.test(text[next])) next++;
    // The operator itself, or Space for the "+" already there: caret on past it.
    if (k === " " || k === "Spacebar" || (k.length === 1 && /[+\-*\/^%]/.test(k))) {
      e.preventDefault();
      var op = k.length === 1 && k !== " " ? k : "+";
      write(st, text.slice(0, os.a) + op + text.slice(os.a + 1), next);
      return true;
    }
    // Backspace goes on deleting leftwards — the item before, with the slot; Delete
    // the same rightwards.
    if (k === "Backspace" || k === "Delete") {
      var hit = k === "Backspace" ? endingAt(st, os.c) : startingAt(st, next);
      if (hit) {
        e.preventDefault();
        if (k === "Backspace") cut(st, hit.a, os.a + 1); else cut(st, os.a, hit.b);
        return true;
      }
    }
    // A letter or digit is a word starting in the slot — an operator letter, most
    // likely. The placeholder comes out and the key goes on to land the ordinary way,
    // beside the item before (onInput's slot); spelled back to nothing, the
    // placeholder comes back (see opFill in onInput).
    if (k.length === 1 && IDENT_CHAR.test(k)) {
      var t = text.slice(0, os.c) + " " + text.slice(next);
      setValue(st, t);
      setSel(st, os.c, os.c);
      st.prevValue = t;
      st.atoms = scan(t, context(st));
      st.opFill = true;
      return false;
    }
    render(st);
    return false;
  }

  /* ── Bubbles: keeping tracked ranges correct across an edit ──────────────────
     A bubble is tracked by POSITION (see bubbleAt), and chips.js changes the
     field's text from four different places (here, onInput's typing pass,
     onDragMove, and accept() on the page's behalf) with no one chokepoint among
     them. Rather than teach each of those what it just did, diff the old text
     against the new — the common prefix and suffix bound the one place they
     differ, regardless of which of the four produced the change — and shift or
     drop tracked ranges from that. Pure, so the arithmetic is asserted in the
     tests rather than by editing things in a window and looking at them. */
  function diffSplice(oldText, newText) {
    var n = Math.min(oldText.length, newText.length);
    var p = 0;
    while (p < n && oldText[p] === newText[p]) p++;
    var ae = oldText.length, be = newText.length;
    while (ae > p && be > p && oldText[ae - 1] === newText[be - 1]) { ae--; be--; }
    return { at: p, removedLen: ae - p, insertedLen: be - p };
  }

  /* Shift every tracked bubble by what `diffSplice(oldText, newText)` says changed.
     One survives untouched if the edit falls entirely outside it, shifts if the
     edit falls entirely before it, and otherwise survives ONLY if the edit lands
     strictly inside it without touching either paren character — anything that
     eats into or across a "(" or a ")" drops that bubble, which is the graceful
     side of this: scan() will simply paint whatever is left as a plain bracket,
     rather than this having to know it is no longer valid. */
  function shiftBubbles(bubbles, oldText, newText) {
    if (!bubbles.length) return bubbles;
    var d = diffSplice(oldText, newText);
    if (!d.removedLen && !d.insertedLen) return bubbles;
    var removedEnd = d.at + d.removedLen, delta = d.insertedLen - d.removedLen;
    var out = [];
    for (var i = 0; i < bubbles.length; i++) {
      var bub = bubbles[i];
      if (bub.a >= removedEnd) out.push(moved(bub, bub.a + delta, bub.b + delta));
      else if (bub.b <= d.at) out.push(bub);
      else if (d.at > bub.a && removedEnd < bub.b) out.push(moved(bub, bub.a, bub.b + delta));
      // else: the edit touched one of this bubble's own paren characters — dropped.
    }
    return out;
  }

  /* Assign the field's text and shift its bubbles by whatever just changed, in
     one call — for write() and onDragMove, whose own edit is a single ordinary
     splice (unlike insertGroup/removeGroup's own pair of them). accept(), on the
     page's behalf, keeps its own equivalent of this for the same reason; onInput
     needs its OWN variant instead of this, because by the time an `input` event
     reaches it the browser has already applied the keystroke to raw(st) —
     see its "before" note. */
  function setValue(st, text) {
    var old = raw(st);
    if (text !== old) st.bubbles = shiftBubbles(st.bubbles, old, text);
    setRaw(st, text);
  }

  /* A bubble reduced to one chip is not a bubble — see the removal rule in the
     phase-2 plan. Checked after every mutation that goes through write(): if a
     live "group" atom's contents come down to one item (or none — every chip in
     it was deleted individually), its two parens are spliced back out, leaving
     whatever is left as plain content. Looped rather than run once, since
     unwrapping an outer bubble can exactly reduce a parent bubble the same way. */
  function collapseSingleChipBubbles(st) {
    for (var guard = 0; guard < 20; guard++) {
      var all = flatten(scan(raw(st), context(st)));
      var hit = null, sc = scopeOf(st);
      for (var i = 0; i < all.length; i++) {
        if (all[i].kind !== "group") continue;
        // The bubble you are in, and every one around it, is still being filled.
        if (sc && all[i].a <= sc.a && all[i].b >= sc.b) continue;
        var n = 0;
        for (var k = 0; k < all[i].items.length; k++) if (isItem(all[i].items[k])) n++;
        if (n <= 1) { hit = all[i]; break; }
      }
      if (!hit) return;
      var a = selA(st), b = selB(st);
      var adj = function (pos) {
        var d = 0;
        if (pos > hit.b - 1) d++;
        if (pos > hit.a) d++;
        return pos - d;
      };
      a = adj(a); b = adj(b);
      var made = removeGroup(raw(st), hit, st.bubbles);
      st.bubbles = made.bubbles;
      setRaw(st, made.text);
      setSel(st, Math.max(0, a), Math.max(0, b));
    }
  }

  function write(st, text, caret) {
    var neat = tidy(text, caret == null ? text.length : caret);
    text = neat.text;
    if (caret != null) caret = neat.caret;
    setValue(st, text);
    if (caret != null) setSel(st, caret, caret);
    collapseSingleChipBubbles(st);
    st.typingAt = -1;
    fire(st);
    render(st);
  }

  function setCaret(st, pos) {
    setSel(st, pos, pos);
    render(st);
  }

  function select(st, a, b) {
    setSel(st, a, b);
    render(st);
  }

  /* Shift-click: extend the selection to a whole contiguous run of siblings, from
     wherever it already reaches to the chip just clicked — the same items an
     arrow-key walk would step through, found the same way dragging finds what it
     can permute (containers/splitItems). The anchor is the farthest end of an
     existing multi-selection if there is one (so a second Shift-click keeps
     growing the same run), otherwise the single held chip; with neither, or with
     the clicked chip in a different container from the anchor, there is no run
     to speak of and this falls back to an ordinary single select. */
  function shiftSelect(st, a, b) {
    var text = raw(st);
    var conts = containers(text, scan(text, context(st)));
    var target = containerOf(conts, { a: a, b: b });
    var toIdx = target ? indexOfItem(target, { a: a, b: b }) : -1;
    if (!target || toIdx < 0) { select(st, a, b); return; }

    var anchor = st.multi.length ? st.multi[st.multi.length - 1]
      : (selA(st) !== selB(st)
         ? { a: selA(st), b: selB(st) } : null);
    var fromIdx = anchor ? indexOfItem(target, anchor) : -1;
    if (fromIdx < 0) { select(st, a, b); return; }

    var lo = Math.min(fromIdx, toIdx), hi = Math.max(fromIdx, toIdx), run = [];
    for (var i = lo; i <= hi; i++) run.push({ a: target.items[i].a, b: target.items[i].b });
    st.multi = run;
    setSel(st, run[run.length - 1].b, run[run.length - 1].b);
    render(st);
  }

  /* Cmd-click: add one chip to the multi-selection, or take it back out, without
     touching the rest. A chip already HELD the ordinary way is the set's first
     member, so a click-then-Cmd-click builds a pair the way Shift-click does. Kept
     in click order rather than sorted, so the last one clicked is still the anchor
     a following Shift-click extends from (shiftSelect). Down to one member, it is
     simply that chip held again; down to none, nothing is. Members may be anywhere
     — making a bubble or a parameter out of them is what settles which container
     they have to share (selectionSpan). */
  function toggleSelect(st, a, b) {
    var multi = st.multi.slice();
    if (!multi.length) {
      var sa = selA(st), sb = selB(st);
      if (sa !== sb && isAtomRange(st, sa, sb) && !(sa === a && sb === b)) {
        multi.push({ a: sa, b: sb });
      }
    }
    var at = -1;
    for (var i = 0; i < multi.length; i++) if (multi[i].a === a && multi[i].b === b) at = i;
    if (at >= 0) multi.splice(at, 1); else multi.push({ a: a, b: b });

    if (multi.length <= 1) {
      st.multi = [];
      if (multi.length) select(st, multi[0].a, multi[0].b);
      else setCaret(st, b);
      return;
    }
    st.multi = multi;
    setSel(st, multi[multi.length - 1].b, multi[multi.length - 1].b);
    render(st);
  }

  /* Is [a, b) exactly an item? What tells a chip that was CLICKED from an ordinary
     drag-selection of some text that happens to contain one. */
  function isAtomRange(st, a, b) {
    var all = flatten(st.atoms);
    for (var i = 0; i < all.length; i++) if (all[i].a === a && all[i].b === b) return true;
    return false;
  }

  /* The page listens for `input` on these fields already — for the preview, the
     autocomplete and the bracket rules — so everything this does announces itself
     the same way typing does, and nothing has to be wired up twice.

     This file's OWN onInput hears it too, and diffs st.prevValue against the field to
     shift the bubbles by what changed. Every caller here has already moved them for
     its own edit (setValue, insertGroup, removeGroup), so the text as it stands IS the
     "before" — left stale, the same edit was applied twice: a bubble just made read
     as an edit across its own brackets, and was dropped the instant it appeared. */
  function fire(st) {
    st.prevValue = raw(st);
    try {
      st.el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    } catch (_) {
      try { st.el.dispatchEvent(new Event("input", { bubbles: true })); } catch (__) {}
    }
  }

  /* ── Undo ──────────────────────────────────────────────────────────────────
     ⌘Z / ⌃Z steps the box back, ⇧ with either steps it forward again.

     NOT THE BROWSER'S UNDO. A textarea's own history knows the characters and nothing
     else, so undoing a bubble put its brackets back as two plain brackets: which "("
     positions are bubbles lives in st.bubbles, beside the text rather than in it (see
     the "core mechanism" note), and an undo that does not restore both restores a
     different expression from the one you had. The native one is therefore swallowed
     and this stands in its place, taking a snapshot of BOTH.

     RECORDED FROM render(), not from each of the dozen places that write to the field.
     Every mutation here ends in a render, so the one place that already sees every
     edit is the one place that has to remember them: `last` is what the field held at
     the end of the previous render, and a render whose text differs from it pushes
     `last` — text, selection and bubbles as they were — onto the past.

     A RUN OF TYPING IS ONE STEP. Snapshots taken a keystroke apart would make undo a
     way of retyping the word backwards, so a one-character edit following another
     within COALESCE_MS extends the step already recorded instead of starting a new
     one. Anything bigger — a commit, a bubble, a drag, a delete — always starts its
     own.                                                                           */
  var COALESCE_MS = 450;
  var HIST_MAX = 200;

  function stamp() {
    return (typeof Date !== "undefined" && Date.now) ? Date.now() : 0;
  }
  function snapshot(st) {
    return { text: raw(st), a: selA(st), b: selB(st),
             bubbles: st.bubbles.slice() };
  }
  function recordHistory(st) {
    var h = st.hist;
    if (!h) return;
    var cur = snapshot(st);
    var last = h.last;
    h.last = cur;
    // A selection or a caret move is not an edit. It still updates `last`, so the step
    // recorded when an edit does come restores where the caret was before it.
    if (!last || last.text === cur.text) return;
    var now = stamp();
    var small = Math.abs(cur.text.length - last.text.length) === 1;
    if (h.small && small && now - h.stamp < COALESCE_MS && h.past.length) {
      h.stamp = now;                     // the same run of typing: one step, still open
    } else {
      h.past.push(last);
      if (h.past.length > HIST_MAX) h.past.shift();
      h.stamp = now;
      h.small = small;
    }
    // A new edit is a new branch: whatever had been undone is no longer ahead of it.
    h.future.length = 0;
  }

  /* Put one back. Everything a keystroke could have been half-way through — an open
     chip, a word being typed, a set being built, a bubble being walked — is let go:
     what is being restored is the field as it WAS, not a state to carry into it. */
  function applySnapshot(st, s) {
    st.editing = null;
    st.typingAt = -1;
    st.slot = null;
    st.space = null;
    st.multi = [];
    st.bubbles = s.bubbles.slice();
    setRaw(st, s.text);
    try { setSel(st, s.a, s.b); } catch (_) {}
    st.hist.last = snapshot(st);          // not an edit: recordHistory must not see one
    st.hist.stamp = 0;
    st.hist.small = false;
    fire(st);                             // the page re-previews, re-sizes, re-completes
    render(st);
  }

  function undo(st) {
    if (!st.hist || !st.hist.past.length) return false;
    var cur = snapshot(st);
    var prev = st.hist.past.pop();
    st.hist.future.push(cur);
    applySnapshot(st, prev);
    return true;
  }
  /* Forget everything there was to step back to — what a SESSION opening means. The
     text in the box belongs to the dimension or parameter being edited now, and undo
     is not a way back into the last one. */
  function resetHistory(el) {
    var st = stateOf(el);
    if (!st) return false;
    st.hist = { past: [], future: [], last: snapshot(st), stamp: 0, small: false };
    return true;
  }

  function redo(st) {
    if (!st.hist || !st.hist.future.length) return false;
    var cur = snapshot(st);
    var next = st.hist.future.pop();
    st.hist.past.push(cur);
    applySnapshot(st, next);
    return true;
  }

  /* ── Pointer: pressing ──────────────────────────────────────────────────────
     The layer never takes the pointer (it is pointer-events: none, and has to be —
     the field underneath needs every click for its caret). So a press over a chip is
     recognised by HIT-TESTING the painted chips against the pointer, and the press
     is then ours: it places the caret itself and, if the pointer moves, becomes a
     drag.                                                                         */
  function onMouseDown(st, e) {
    if (e.button !== 0 || !st.layer) return;
    var hit = chipUnder(st, e.clientX, e.clientY);

    if (hit) {
      e.preventDefault();
      st.el.focus();
      st.editing = null;

      // What the press takes hold of at the current level: a bubble you are not inside
      // is one thing, whatever in it the pointer landed on (see levelItem). A press
      // outside the bubble you ARE inside leaves it first.
      var sc = scopeOf(st);
      if (sc && hit.atom.a === sc.a && hit.atom.b === sc.b) { exitScope(st); return; }
      if (sc && !inside(sc, hit.atom)) {
        setScope(st, null);
        sc = null;
      }
      var atom = levelItem(st.atoms, hit.atom, sc);
      var a = atom.a, b = atom.b;

      // Modifiers first, punctuation included: an operator ⌘-clicked into a set is as
      // much a member of it as any operand (the span it becomes snaps to whole items
      // either way — see snapSpan).
      if (e.shiftKey) { shiftSelect(st, a, b); return; }
      if (e.metaKey || e.ctrlKey) { toggleSelect(st, a, b); return; }

      // Punctuation is taken hold of but never dragged: an operator is one character,
      // and moving the punctuation is exactly what a drag must not do (see planMove —
      // the skeleton stays put and the operands move through it).
      if (!isItem(atom)) { st.multi = []; select(st, a, b); return; }

      // A plain click starts fresh — whatever was building toward a bubble is let go,
      // the same way a plain click already lets go of one held chip.
      st.multi = [];

      // Nothing is selected yet. What this press MEANT is not known until it ends: a
      // click selects the item, a drag moves it, and only the first of those should
      // leave it highlighted.
      st.drag = {
        a: a, b: b, x: e.clientX, y: e.clientY, live: false,
        text: raw(st), sel: [selA(st), selB(st)],
        bubbles: st.bubbles.slice()
      };
      beginDragWatch(st);
      return;
    }

    // Anywhere else — the punctuation, the gaps, the empty room past the end. The caret
    // goes where it was clicked and NOTHING gets selected: this field does not hold a
    // paragraph to sweep blue, it holds a row of things, and the way to take hold of one
    // of them is to click it. So the press is answered here and the field never sees it,
    // which is what stops a drag across the box turning into a highlight.
    var pos = offsetFromPoint(st, e.clientX, e.clientY);
    if (pos === null) return;              // measured nothing: leave it to the field
    e.preventDefault();
    st.el.focus();
    st.typingAt = -1;
    st.multi = [];
    var scNow = scopeOf(st);
    if (scNow && !(pos > scNow.a && pos < scNow.b)) setScope(st, null);
    setCaret(st, pos);
  }

  /* A RIGHT-CLICK IS THIS BOX'S, not WebKit's. The native menu offered here is the one
     for a text field — Cut, Paste, Look Up, spelling — and none of it applies to a row
     of objects; what belongs on it is what you can do to a chip. So the native menu is
     refused outright and the page is asked to put its own up.

     The press TAKES HOLD of the chip it landed on first, the way a right-click on a
     list row does everywhere else — unless that chip is already part of what is
     selected, in which case the menu is for the selection and the selection stands. */
  function onContextMenu(st, e) {
    e.preventDefault();                      // never the native one, menu or no menu
    if (!st.layer) return;
    var hit = chipUnder(st, e.clientX, e.clientY);
    if (hit) {
      var sc = scopeOf(st);
      if (sc && !inside(sc, hit.atom)) { setScope(st, null); sc = null; }
      var atom = levelItem(st.atoms, hit.atom, sc);
      var already = inSelection(st, atom.a, atom.b);
      if (!already) {
        st.el.focus();
        st.editing = null;
        st.multi = [];
        select(st, atom.a, atom.b);
      }
    }
    if (!st.opts.menu) return;
    try {
      st.opts.menu({ x: e.clientX, y: e.clientY,
                     chip: hit ? chipAtCaret(st.el) : null,
                     selection: selectionOf(st.el) }, e);
    } catch (_) {}
  }

  /* Is [a, b) one of the chips currently held — the single one, or a member of the
     ⇧/⌘-clicked set? */
  function inSelection(st, a, b) {
    for (var i = 0; i < st.multi.length; i++) {
      if (st.multi[i].a === a && st.multi[i].b === b) return true;
    }
    return selA(st) === a && selB(st) === b && a !== b;
  }

  /* Which character a point is at, measured on the layer — the one place the text
     exists as real DOM. Walked rather than hit-tested: the field sits OVER the layer
     and would answer for its own shadow text, which is not text this can count, and
     an expression is short enough that laying a range over each character in turn is
     nothing. Nearest line first, then nearest side of the nearest character. */
  function offsetFromPoint(st, x, y) {
    var text = raw(st) || "";
    if (!text.length) return 0;
    var nodes = walker(st.layer);
    if (!nodes.length) return null;
    var best = null;
    for (var i = 0; i < text.length; i++) {
      var r = rectIn(nodes, i, i + 1);
      if (!r) continue;
      var dy = y < r.top ? r.top - y : (y > r.bottom ? y - r.bottom : 0);
      var cx = (r.left + r.right) / 2;
      var d = dy * 1000 + Math.abs(x - cx);
      if (best === null || d < best.d) best = { d: d, i: i, after: x > cx };
    }
    if (best === null) return null;
    return best.i + (best.after ? 1 : 0);
  }

  /* ── Pointer: the cursor shape ──────────────────────────────────────────────
     Over a chip it is a POINTER, because a chip is an object you click, not a run of
     characters you sweep through. It is an I-beam only where there is text to work on:
     a chip opened for editing, a word not yet committed, and the room to the right of
     the last chip where the next one goes.

     Which is exactly "not over a painted chip" — an open chip is painted as plain
     letters, and empty room is painted as nothing. A call is the one thing that is both:
     its NAME and brackets are a chip, and its argument is a field inside it. */
  function onMouseMove(st, e) {
    if (!st.layer) return;
    // Mid-drag there is no pointer at all: the chip is following the pointer, and a
    // cursor drawn on top of the thing it is carrying is a second answer to "where am
    // I" competing with the first. Left to the end of the drag to put back.
    if (st.drag && st.drag.live) { try { st.el.style.cursor = "none"; } catch (_) {} return; }
    var solid = st.layer.querySelectorAll(".chip:not(.chip-fn), .chip-fn-name");
    var over = false;
    for (var i = 0; i < solid.length && !over; i++) {
      var rects = solid[i].getClientRects();
      for (var k = 0; k < rects.length; k++) {
        var r = rects[k];
        if (e.clientX >= r.left && e.clientX <= r.right &&
            e.clientY >= r.top && e.clientY <= r.bottom) { over = true; break; }
      }
    }
    try { st.el.style.cursor = over ? "default" : "text"; } catch (_) {}
  }

  function chipUnder(st, x, y) {
    var els = st.layer.querySelectorAll(".chip");
    var found = null;
    for (var i = 0; i < els.length; i++) {
      var rects = els[i].getClientRects();
      for (var k = 0; k < rects.length; k++) {
        var r = rects[k];
        if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
          // Deepest wins: the argument chip inside a call is the one being pointed at.
          // querySelectorAll visits an ancestor before its descendants, so the check
          // has to ask whether what is ALREADY found contains this new candidate —
          // asking it the other way round is never true for a real ancestor/descendant
          // pair and left the outer chip stuck as the answer no matter where inside it
          // you clicked.
          if (!found || found.el.contains(els[i])) found = { el: els[i], rect: r };
        }
      }
    }
    if (!found) return null;
    found.atom = atomAt(st.atoms, +found.el.dataset.a, false) || { kind: "param" };
    return found;
  }

  function beginDragWatch(st) {
    if (typeof window === "undefined" || !window.addEventListener) return;
    var move = function (e) { onDragMove(st, e); };
    var up = function (e) {
      window.removeEventListener("mousemove", move, true);
      window.removeEventListener("mouseup", up, true);
      window.removeEventListener("keydown", key, true);
      var d = st.drag;
      var click = d && !d.live;
      endDrag(st, false);
      if (!click) return;
      // A click TAKES HOLD, and does nothing else however many times you do it. Editing
      // is deliberate — the space key — because a click that sometimes selected a thing
      // and sometimes opened it for typing is a click you have to think about.
      st.editing = null;
      select(st, d.a, d.b);
    };
    var key = function (e) {
      if (e.key === "Escape" && st.drag) {
        e.preventDefault(); e.stopPropagation();
        endDrag(st, true);
        window.removeEventListener("mousemove", move, true);
        window.removeEventListener("mouseup", up, true);
        window.removeEventListener("keydown", key, true);
      }
    };
    window.addEventListener("mousemove", move, true);
    window.addEventListener("mouseup", up, true);
    window.addEventListener("keydown", key, true);
  }

  function onDragMove(st, e) {
    var d = st.drag;
    if (!d) return;
    if (!d.live) {
      if (Math.abs(e.clientX - d.x) + Math.abs(e.clientY - d.y) < 4) return;
      d.live = true;
      try { st.layer.classList.add("dragging"); } catch (_) {}
      // The pointer goes while the chip is in hand — see onMouseMove. Set here as
      // well as there, since a drag that starts and ends without the pointer ever
      // re-entering the field would otherwise never have it taken off.
      try { st.el.style.cursor = "none"; } catch (_) {}
    }
    e.preventDefault();

    var target = dropTarget(st, e.clientX, e.clientY);
    if (!target) return;
    // The container list target came from, NOT a fresh one: planMove decides "same
    // level or a different one" by identity, and two lists built from the same text
    // hold equal objects that are never the same object. Rebuilding it here meant
    // every move looked like a move across levels and was refused.
    var plan = planMove(raw(st), { a: d.a, b: d.b },
                        target.container, target.index, target.containers);
    if (!plan) return;

    d.a = plan.range.a; d.b = plan.range.b;
    setValue(st, plan.text);
    // The caret follows the item, collapsed: a drag is a move, not a selection, and
    // what is moving is already said by the chip lifting out of the line.
    setSel(st, plan.range.b, plan.range.b);
    fire(st);
    render(st);
    markDragged(st);
  }

  /* Which slot the pointer is asking for: the operand nearest it, and which side of
     that operand's middle the pointer is on. An empty call's inside is a slot too —
     that is the drop that wraps a parameter in a cos(). */
  function dropTarget(st, x, y) {
    var text = raw(st);
    var cont = containers(text, st.atoms);
    var best = null;
    var report = function (b) { if (b) b.containers = cont; return b; };
    for (var ci = 0; ci < cont.length; ci++) {
      var c = cont[ci];
      if (!c.items.length) {
        var r0 = rangeRect(st, c.a, c.b);
        if (r0) {
          var d0 = dist(x, y, (r0.left + r0.right) / 2, (r0.top + r0.bottom) / 2);
          if (!best || d0 < best.d) best = { d: d0, container: c, index: 0 };
        }
        continue;
      }
      for (var i = 0; i < c.items.length; i++) {
        var r = rangeRect(st, c.items[i].a, c.items[i].b);
        if (!r) continue;
        var cx = (r.left + r.right) / 2, cy = (r.top + r.bottom) / 2;
        var d = dist(x, y, cx, cy);
        if (!best || d < best.d) best = { d: d, container: c, index: x > cx ? i + 1 : i };
      }
    }
    return report(best);
  }

  function dist(x, y, px, py) {
    var dx = x - px, dy = (y - py) * 3;      // a line apart is further than a word apart
    return Math.sqrt(dx * dx + dy * dy);
  }

  /* The screen rectangle of a text range, measured on the layer — the one place the
     characters exist as real DOM. */
  function rangeRect(st, a, b) {
    return rectIn(walker(st.layer), a, b);
  }

  function rectIn(walk, a, b) {
    var range;
    try { range = document.createRange(); } catch (_) { return null; }
    var startSet = false, pos = 0;
    for (var i = 0; i < walk.length; i++) {
      var node = walk[i], len = node.nodeValue.length;
      if (!startSet && pos + len >= a) { range.setStart(node, a - pos); startSet = true; }
      if (startSet && pos + len >= b) { range.setEnd(node, Math.max(0, b - pos)); break; }
      pos += len;
    }
    if (!startSet) return null;
    var r = range.getBoundingClientRect();
    return (r.width || r.height) ? r : null;
  }

  /* A labelled chip's reading, laid over its real name's letters: squeezed to fit if
     it is wider (it is only ever a little — see the thin spaces the page joins it
     with), centred if narrower. A name the field has WRAPPED across lines has no one
     box to fit into, so it shows as itself. */
  function fitLabels(st) {
    var els = st.layer.querySelectorAll(".chip.labelled");
    for (var i = 0; i < els.length; i++) {
      var raw = els[i].firstChild, lab = els[i].lastChild;
      var rr = raw.getClientRects();
      if (rr.length !== 1) { els[i].classList.remove("labelled"); lab.remove(); continue; }
      var rw = rr[0].width, lw = lab.getBoundingClientRect().width;
      if (!rw || !lw) continue;
      lab.style.transform = lw > rw ? "scaleX(" + (rw / lw) + ")"
                                    : "translateX(" + ((rw - lw) / 2) + "px)";
    }
  }

  function walker(root) {
    var out = [];
    (function rec(n) {
      for (var c = n.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 3) out.push(c);
        else if (!(c.classList && c.classList.contains("chip-label"))) rec(c);
      }
    })(root);
    return out;
  }

  function markDragged(st) {
    var d = st.drag;
    if (!d || !d.live || !st.layer) return;
    var els = st.layer.querySelectorAll(".chip");
    for (var i = 0; i < els.length; i++) {
      var on = +els[i].dataset.a === d.a && +els[i].dataset.b === d.b;
      // A bubble and the one chip left inside it can end a step at the same span; the
      // outer one is the thing being carried, so it wins and the inner is left alone.
      if (on && els[i].parentElement && els[i].parentElement.closest &&
          els[i].parentElement.closest('.chip[data-a="' + d.a + '"][data-b="' + d.b + '"]')) on = false;
      els[i].classList.toggle("held", on);
    }
  }

  function endDrag(st, revert) {
    var d = st.drag;
    st.drag = null;
    if (!d) return;
    try { st.layer.classList.remove("dragging"); } catch (_) {}
    try { st.el.style.cursor = ""; } catch (_) {}      // the pointer is back
    if (!d.live) return;
    if (revert) {
      st.bubbles = d.bubbles;
      setRaw(st, d.text);
      setSel(st, d.sel[0], d.sel[1]);
      fire(st);
    } else {
      // The drag may have left a bubble at one chip (dragged the second-to-last
      // one out) or none (dragged the last one out) — checked once, here, rather
      // than on every intermediate step of a live drag.
      collapseSingleChipBubbles(st);
    }
    render(st);
  }

  /* Every field re-read, for the same reason one is: the page has just written to them
     itself. It used to only repaint, which left the TYPING state from whatever was last
     typed — so a field prefilled from a new session could paint its last word as loose
     letters, because something typed a session ago said that word was being spelled. */
  function refreshAll() {
    for (var i = 0; i < fields.length; i++) reread(fields[i]);
  }

  /* The caret decides which words are items and which are letters being typed, so the
     paint has to follow it wherever it goes — including the places that fire no event
     of their own: a click, a drag-select, an arrow key handled somewhere else, a
     setSelectionRange from the page. selectionchange is the one signal that covers all
     of them. */
  if (typeof document !== "undefined" && document.addEventListener) {
    document.addEventListener("selectionchange", function () {
      var st = stateOf(document.activeElement);
      if (!st || st.drag) return;
      // The caret having moved off the end of the word being typed ends the typing —
      // that is a click or an arrow key, and what was left behind is finished. Typing
      // moves the caret too, but always to the same place: the end of that same word.
      // typingStart, not a walk over IDENT_CHAR: a decimal point is part of the word
      // (dotContinuesNumber), and stopping at it sealed "25." as a chip.
      if (st.typingAt >= 0) {
        var c = selA(st);
        if (c !== selB(st) || typingStart(raw(st), c) !== st.typingAt) {
          st.typingAt = -1;
        }
      }
      render(st);
    });
  }

  /* ── What the page asks about ──────────────────────────────────────────────── */

  /* The item the caret is on, for the tag keys: the chip it sits at the edge of, or
     inside. Returns {kind, name} or null. */
  function chipAtCaret(el) {
    var st = stateOf(el);
    if (!st) return null;
    var a = selA(st), b = selB(st);
    // A SELECTED item counts, and has to: clicking a chip selects it, so the thing you
    // are pointing at when you reach for a tag key is a selection, not a caret.
    if (a !== b) {
      var all = flatten(st.atoms);
      for (var i = 0; i < all.length; i++) {
        if (all[i].a === a && all[i].b === b) {
          return { kind: all[i].kind, name: all[i].name, a: a, b: b };
        }
      }
      return null;
    }
    var at = atomAt(st.atoms, a, true);
    return at ? { kind: at.kind, name: at.name, a: at.a, b: at.b } : null;
  }

  /* What F and ⌫ do to whatever is held, reachable by name — for a page putting the
     same two things on a menu (opts.menu). Deliberately the very functions the keys
     call, so the menu and the keyboard can never come to mean different things. Both
     answer false with nothing held, which is what tells the page not to offer them. */
  function openHeld(el) {
    var st = stateOf(el);
    if (!st || st.editing) return false;
    var a = selA(st), b = selB(st);
    if (a === b || !isAtomRange(st, a, b)) return false;
    var it = atomExactly(st, a, b);
    if (it && it.kind === "group") enterScope(st, it);
    else openItem(st, a, b);
    return true;
  }
  function deleteHeld(el) {
    var st = stateOf(el);
    if (!st || st.editing) return false;
    if (st.multi.length >= 2) {
      // Several: the whole-item span they cover, the same one a bubble or a parameter
      // made out of them would take (multiSpan) — never the chips alone, which would
      // leave the operators between them behind.
      var span = multiSpan(st, raw(st));
      if (!span) return false;
      st.multi = [];
      cut(st, span.a, span.b);
      return true;
    }
    var a2 = selA(st), b2 = selB(st);
    if (a2 === b2 || !isAtomRange(st, a2, b2)) return false;
    if (!takeOperator(st, a2, b2)) cut(st, a2, b2);
    return true;
  }

  /* Is [a, b) exactly one chip in this field? What the page asks before restoring a
     selection it saved — a held chip comes back held, anything else does not come back
     at all. */
  function isItemRangeOf(el, a, b) {
    var st = stateOf(el);
    return !!st && isAtomRange(st, a, b);
  }

  /* Insert a chosen name at [a, b), the one insertion this file does on the page's
     behalf — because a FUNCTION is chosen as a call: "cos" typed in a list means
     cos() with the caret between the brackets, ready for its argument. */
  function accept(el, a, b, name, kind) {
    var st = stateOf(el);
    var t = st ? raw(st) : el.value;
    var isCall = kind === "fn" || (isFunction(name) && kind !== "param");
    var text = isCall ? name + "()" : name;
    var caret = a + (isCall ? name.length + 1 : name.length);
    var whole = t.slice(0, a) + text + t.slice(b);
    // "2*" with Width taken from the list is "2 * Width": what the list puts in is
    // spaced from what is already there, exactly as typing it would have been.
    if (st) {
      // Closed, explicitly: a name taken from the list is finished by the taking, so it
      // is an item straight away — and being an item is what lets the "+" appear in
      // front of it when it lands next to one.
      var spaced = space(whole, caret, at(st, caret, -1));
      whole = spaced.text; caret = spaced.caret;
    }
    var neat = tidy(whole, caret);
    whole = neat.text; caret = neat.caret;
    // Not setValue: el may not be a chip-tracked field at all (this is called
    // generically), and st, when it exists, is fetched above rather than re-read.
    if (st) st.bubbles = shiftBubbles(st.bubbles, t, whole);
    if (st) { setRaw(st, whole); setSel(st, caret, caret); }
    else { el.value = whole; el.setSelectionRange(caret, caret); }
    if (st) {
      st.typingAt = -1;
      st.editing = null;
      // CHOOSING is committing. A name picked off the list is as finished as one ended
      // with the space, so it is sealed the same way: a chip, caret just past it, NOT
      // held — the next keystroke is the next item (see seal).
      render(st);
    }
    return { caret: caret, call: isCall };
  }

  /* What the page could turn into ONE thing — a new parameter, say: two or more
     multi-selected chips (the same whole-item span a bubble around them would take,
     see multiSpan), one held bubble, or one held number chip. Returns {a, b, expr} or null. `expr` is what
     that span MEANS on its own: a bubble's contents without its own brackets, since
     those are grouping, not part of the value. [a, b) is what to replace — a bubble
     brackets and all, since the thing that replaces it is already one item.

     `whole` says whether `expr` stands on its own (wholeExpr). The span is still
     reported when it does not — the page may want to say why — but nothing should be
     made out of one that is false. */
  function selectionOf(el) {
    var st = stateOf(el);
    if (!st || st.editing) return null;
    var text = raw(st);
    var found = null;
    if (st.multi.length >= 2) {
      var span = multiSpan(st, text);
      if (span) found = { a: span.a, b: span.b, expr: text.slice(span.a, span.b).trim() };
    } else {
      var a = selA(st), b = selB(st);
      if (a === b) return null;
      var all = flatten(st.atoms);
      for (var i = 0; i < all.length; i++) {
        if (all[i].a !== a || all[i].b !== b) continue;
        if (all[i].kind === "group") {
          found = { a: a, b: b, expr: text.slice(all[i].inner.a, all[i].inner.b).trim() };
          break;
        }
        // One held NUMBER is worth naming — "25 mm" has no name yet, unlike a
        // parameter chip (already one) or an operator (not a value at all).
        // …and so is one held CALL: cos(Taper) is a value just as a number is.
        // …and a parameter chip: any chip can seed a new parameter ("whole").
        if (all[i].kind === "num" || all[i].kind === "fn" || all[i].kind === "param") {
          found = { a: a, b: b, expr: text.slice(a, b).trim(), whole: true };
          break;
        }
      }
    }
    if (!found) return null;
    if (found.whole === undefined) found.whole = wholeExpr(found.expr);
    return found;
  }

  /* Put `insert` in place of [a, b) as one item, and hold it — the page's half of
     "make a parameter from this". Goes through write(), so bubbles around the span
     shift rather than being lost, and one left holding only the new item is undone
     the way any other one-chip bubble is. The page must already list `insert` as a
     name (opts.names), or it lands as a half-typed word rather than a chip. */
  function replaceSpan(el, a, b, insert) {
    var st = stateOf(el);
    var t = st ? raw(st) : el.value;
    var whole = t.slice(0, a) + insert + t.slice(b);
    if (!st) { el.value = whole; return null; }
    st.multi = [];
    st.editing = null;
    write(st, whole, a + insert.length);
    var made = endingAt(st, selA(st));
    if (made) select(st, made.a, made.b);
    return made;
  }

  /* The names this file adds to the dialog's list: everything Fusion can evaluate
     that is not a parameter. Offered as entries so they chip on the same rules and
     rank by the same score — a constant is as much a THING as a parameter is. */
  function vocabulary() {
    var out = [];
    for (var f in FUNCTIONS) {
      if (!Object.prototype.hasOwnProperty.call(FUNCTIONS, f)) continue;
      out.push({ name: f, kind: "fn", arg: FUNCTIONS[f].arg || "",
                 takes: FUNCTIONS[f].takes || 1 });
    }
    for (var c in CONSTANTS) {
      if (!Object.prototype.hasOwnProperty.call(CONSTANTS, c)) continue;
      out.push({ name: c, kind: "const", arg: "", takes: 0 });
    }
    return out;
  }

  return {
    attach: attach, refresh: refresh, refreshAll: refreshAll,
    attached: function (el) { return !!stateOf(el); },
    resetHistory: resetHistory,
    editAll: editAll, caretAtEnd: caretAtEnd,
    // The four that speak in positions and names, translated at the door for a field
    // with short names: the page asks and is answered in the REAL expression's terms.
    chipAtCaret: function (el) { return realChip(stateOf(el), chipAtCaret(el)); },
    accept: function (el, a, b, name, kind) {
      var st = stateOf(el);
      if (!st || !st.nat) return accept(el, a, b, name, kind);
      buildShort(st);
      var r = accept(el, fieldPos(st, a), fieldPos(st, b),
                     kind === "fn" || kind === "const" ? name : shortName(st, name), kind);
      r.caret = realPos(st, r.caret);
      return r;
    },
    isItemRange: function (el, a, b) {
      var st = stateOf(el);
      return isItemRangeOf(el, fieldPos(st, a), fieldPos(st, b));
    },
    openHeld: openHeld, deleteHeld: deleteHeld,
    // What the field SHOWS — short names and all — for a page measuring how wide it is.
    shown: function (el) { var st = stateOf(el); return st ? raw(st) : el.value; },
    selectionOf: function (el) { return realSel(stateOf(el), selectionOf(el)); },
    replaceSpan: function (el, a, b, insert) {
      var st = stateOf(el);
      if (!st || !st.nat) return replaceSpan(el, a, b, insert);
      buildShort(st);
      return realChip(st, replaceSpan(el, fieldPos(st, a), fieldPos(st, b),
                                      shortName(st, insert)));
    },
    vocabulary: vocabulary, isFunction: isFunction, isConstant: isConstant,
    FUNCTIONS: FUNCTIONS, CONSTANTS: CONSTANTS,
    // Pure, for the headless tests.
    scan: scan, splitItems: splitItems, containers: containers, planMove: planMove,
    removeRange: removeRange, space: space, commit: commit, tidy: tidy,
    wholeExpr: wholeExpr,
    flatten: flatten, atomAt: atomAt, matchParen: matchParen, danglingRun: danglingRun,
    groupSpan: groupSpan, snapSpan: snapSpan, insertGroup: insertGroup, removeGroup: removeGroup,
    diffSplice: diffSplice, shiftBubbles: shiftBubbles,
    levelItem: levelItem, slotNeeded: slotNeeded,
    // The stateful half, for the headless tests only: the harness has no events, so
    // these drive a field's key and input handlers directly.
    _state: stateOf,
    _keydown: function (el, e) { onKeyDown(stateOf(el), e); },
    _keyup: function (el, e) { onKeyUp(stateOf(el), e); },
    _input: function (el, e) { onInput(stateOf(el), e); },
    _toggle: function (el, a, b) { toggleSelect(stateOf(el), a, b); },
    _wholeChips: function (el, a, b) { return wholeChips(stateOf(el), a, b); }
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Chips;
