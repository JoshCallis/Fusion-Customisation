/* capitalise.js — every word starts with a capital, in every companion window.
 *
 * Josh never starts a word lowercase: parameter names are CamelCase, component names
 * are capitalised, and the shift key is wasted effort on the first letter of anything.
 * So the window does it. Type "edge offset y" and get "Edge Offset Y"; hold shift and
 * you get the same capital, because it was going to be one either way.
 *
 * A WORD START is the beginning of the field, or a letter following whitespace, an
 * opening bracket — ( [ { — a hyphen, a slash, or an OPENING quote. So "(edge", "top-
 * edge", "left/right" and '"inner' all capitalise their second word. A quote only opens
 * when it is itself at a word start: after a letter it is an apostrophe or a closing
 * quote, and "don't" must not become "don'T". Other operators (+ * , …) stay out.
 *
 * ONLY THE CHARACTER JUST TYPED is ever changed, which is what makes this safe to leave
 * running:
 *
 *   - Accepting an autocomplete suggestion sets .value directly, and assigning to
 *     .value fires no input event, so an inserted parameter name is never rewritten.
 *     That matters here: the driven-dimension aliases are deliberately case-flipped and
 *     DO start lowercase, and rewriting one would point the dimension at nothing.
 *   - Pasting is left alone for the same reason — it is not a keystroke, and silently
 *     recasing pasted text is a good way to corrupt something.
 *
 * A WORD AFTER A NUMBER is the exception, and only in an expression field: "25 mm" is
 * a value and its unit, and "25 Mm" is neither a unit Fusion knows nor anything Josh
 * typed on purpose. Expression fields are the ones the chip editor has taken over (see
 * chips.js, which reads the same pair as one item), so the class it puts on them is
 * exactly the test.
 *
 * Opt a field out with data-nocaps="1".
 *
 * Opt a field IN to data-joinwords="1" and the space that asks for the capital is spent
 * doing it rather than left in the name: "my param" becomes "MyParam", not "My Param".
 * A parameter's own name field wants exactly this — CamelCase has no spaces in it at
 * all — while a component's or a sketch's name is a sentence and keeps its spaces, so
 * this is the one place capital-by-space and word-by-space are the same keystroke.
 *

 * Attached by one delegated listener per window rather than per field, so inputs built
 * later — the New Component panel's rows, the Browser's inline editors — are covered
 * without having to remember to wire them up.
 */

(function () {
  "use strict";

  const OPENERS = /[\s(\[{\-\/]/;
  const QUOTES = /["'\u201C\u2018]/;

  // Does the character at i let the letter after it start a word? Off the front of the
  // field counts. A quote only if the quote itself sits at a word start (see header).
  function opensWord(text, i) {
    if (i < 0) return true;
    const ch = text.charAt(i);
    if (OPENERS.test(ch)) return true;
    return QUOTES.test(ch) && opensWord(text, i - 1);
  }

  function capitaliseAtCaret(el) {
    // A selection is a replacement in progress, not a finished keystroke.
    const c = el.selectionStart;
    if (c === null || c === 0 || el.selectionEnd !== c) return;

    const text = el.value;
    const ch = text.charAt(c - 1);
    if (!/[a-z]/.test(ch)) return;

    const before = c >= 2 ? text.charAt(c - 2) : "";
    // In a joinwords field (a parameter name) a comma starts a word too: it separates
    // the feature levels of a structured name ("Boot,Ceiling"), each its own CamelCase.
    const levelStart = before === "," && el.dataset && el.dataset.joinwords === "1";
    if (!opensWord(text, c - 2) && !levelStart) return;
    // The unit on a number, in a field that holds an expression. Left as it was typed.
    if (el.classList && el.classList.contains("chipped") &&
        /[0-9.][ \t]*$/.test(text.slice(0, c - 1))) return;

    // A field that asked for it (see data-joinwords above) treats the space that just
    // demanded a capital as spent by asking — gone, along with the letter it stood in
    // front of moving up to take its place. Only when there WAS one to spend: the very
    // first letter of the field still capitalises in place.
    // …EXCEPT the space after a comma, which is not a word-join at all: it is the
    // separator between two feature levels, written by the comma itself (see NAME_SEP
    // in fusion_value/index.js) and read back over by a trim. Spending it here would
    // unspace the name one letter after the field had spaced it.
    const afterComma = before === " " && c >= 3 && text.charAt(c - 3) === ",";
    if (before === " " && !afterComma && el.dataset && el.dataset.joinwords === "1") {
      el.value = text.slice(0, c - 2) + ch.toUpperCase() + text.slice(c);
      el.setSelectionRange(c - 1, c - 1);
      return;
    }

    el.value = text.slice(0, c - 1) + ch.toUpperCase() + text.slice(c);
    el.setSelectionRange(c, c);
  }

  document.addEventListener("input", function (e) {
    const el = e.target;
    if (!el || (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA")) return;
    // The type guard is about which KIND of input this is — a checkbox, a number, a
    // colour swatch — so it applies to <input> and nothing else. A <textarea> reports
    // its type as "textarea", which failed that test and quietly excluded the one field
    // in the family that holds the most typing: the value box.
    if (el.tagName === "INPUT" && el.type
        && el.type !== "text" && el.type !== "search") return;
    if (el.dataset && el.dataset.nocaps === "1") return;
    // insertText is a keystroke. A paste, a drop or an undo is not, and none of them
    // should be silently recased.
    if (e.inputType && e.inputType !== "insertText") return;
    capitaliseAtCaret(el);
  }, true);
})();
