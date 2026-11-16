/* ─────────────────────────────────────────────────────────────────────────────
   planes.js — the JavaScript twin of planes.py.

   The sketch-create box takes one line: a DATUM word, then optionally how far in
   from it. "Width 100 - Membrane" is the side face, less (100 - Membrane), and the
   whole reason this file exists twice is that BOTH sides need to say so:

     - the add-in composes the expression it actually writes into the plane
       (planes.py), and
     - the window shows the brackets as you type and previews the result
       (this file).

   Two implementations, one set of cases: planes_test.py runs its tables through
   both and fails if they ever disagree. Exactly the arrangement naming.py and
   naming.js are in, and for the same reason.

   Nothing here knows about the DOM. The live bracket insertion — where the caret
   goes when a bracket appears — is the dialog's business and lives in index.html;
   this file only answers whether brackets are wanted and what the expression is.
   ──────────────────────────────────────────────────────────────────────────── */
"use strict";

var Planes = (function(){

  // Which construction plane a sketch measured along each axis sits on: the one the
  // axis is normal to.
  const BASE_PLANE = { X: "YZ", Y: "XZ", Z: "XY" };
  // Angled planes — see planes.py for the reasoning. Default pivot first.
  const PIVOT_AXES = { X: ["Y", "Z"], Y: ["X", "Z"], Z: ["X", "Y"] };
  function pivotAxes(measured){ return PIVOT_AXES[(measured || "").toUpperCase()] || ["", ""]; }
  // The angle as Fusion will be given it: the typed text, reversed / quarter-turned
  // as the toggles say. The field itself never changes.
  function angleExpression(text, flip, plus90){
    const t = (text || "").trim();
    if (!t) return "";
    let inner = bracket(t);
    if (flip) inner = "-" + (inner.startsWith("(") ? inner : "(" + inner + ")");
    if (plus90) inner = inner + " + 90 deg";
    return inner;
  }

  const OPERATOR = /[+\-*/]/;

  /* Does what follows the datum contain an operation BETWEEN terms?
     A leading sign is part of the first term ("-5" is a number, not an operation),
     so the search starts past it. */
  function needsBrackets(rest){
    if (!rest) return false;
    const text = String(rest).trim();
    if (!text) return false;
    const start = /^[-+]/.test(text) ? 1 : 0;
    return OPERATOR.test(text.slice(start));
  }

  function balanced(text){
    let depth = 0;
    for (const ch of text){
      if (ch === "(") depth++;
      else if (ch === ")"){ depth--; if (depth < 0) return false; }
    }
    return depth === 0;
  }

  /* `rest`, parenthesised if it is more than one term. Already-wrapped stays as it
     is, so typing the brackets yourself is not punished with a second pair. */
  function bracket(rest){
    const text = String(rest == null ? "" : rest).trim();
    if (!text) return "";
    if (text.startsWith("(") && text.endsWith(")") && balanced(text.slice(1, -1)))
      return text;
    return needsBrackets(text) ? "(" + text + ")" : text;
  }

  /* ["Width", "100 - Membrane"] — the datum word and everything after it. */
  function splitInput(text){
    const stripped = String(text == null ? "" : text).trim();
    if (!stripped) return ["", ""];
    const m = stripped.match(/^(\S+)\s*([\s\S]*)$/);
    return [m[1], m[2].trim()];
  }

  /* The signed distance from the origin, as an expression Fusion can evaluate.

       datumKind  'face' | 'midline'
       faceName   the parameter naming that face — ignored for a midline, which is 0
       rest       what was typed after the datum, or ''
       sign       +1 for a high face (Right/Back/Top), -1 for a low one           */
  function offsetExpression(datumKind, faceName, rest, sign){
    const inner = bracket(rest);

    if (datumKind === "midline"){
      // Out from the midline along the positive axis. Nothing after it means the
      // midline itself, which is the origin.
      return inner || "0";
    }

    if (!faceName) return "";
    const body = inner ? (faceName + " - " + inner) : faceName;

    if (sign > 0) return body;
    // A low face sits at a negative coordinate, so the whole distance is negated —
    // brackets around it, or the negation would only reach the first term.
    return inner ? ("-(" + body + ")") : ("-" + faceName);
  }

  // `balanced` is exported for the dialog's live bracket insertion, which has to
  // recognise the pair it put there last keystroke before deciding whether to keep it.
  return { BASE_PLANE, PIVOT_AXES, pivotAxes, angleExpression,
           needsBrackets, bracket, balanced, splitInput, offsetExpression };
})();

// Reachable from the test harness, which runs this file under jsc rather than in a
// page. Harmless in the window, where `this` is already the global object.
if (typeof module !== "undefined" && module.exports) module.exports = Planes;
