/* Cases for capitalise.js. Run it:
 *
 *     /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc \
 *       fusion_ui/capitalise_test.js
 *
 * It stands alone — a tiny document shim captures the delegated listener, and each case
 * hands it a fake element and a fake input event. Worth having because this rewrites
 * what Josh types, in three windows, and the half of it that matters is the half that
 * must NOT fire: a pasted parameter name, or one of the deliberately lowercase
 * driven-dimension aliases, would be corrupted by a rule that was too eager.
 */
var _handler = null;
var document = { addEventListener: function(t, fn){ if (t === "input") _handler = fn; } };
/* The REAL file, not a copy of it. This used to inline the source, and the copy drifted
   from the shipped one in exactly the way a copy does: the fake element below reported a
   textarea's type as "text", so "textarea too" passed here while every real textarea was
   being skipped. jsc has read(); use it. */
eval(read("/Users/josh/Code/Scripts/fusion_ui/capitalise.js"));

var fails = 0;
function el(value, caret, opts){
  opts = opts || {};
  var tag = opts.tag || "INPUT";
  // A real <textarea> reports type "textarea", and getting that wrong here is what let
  // the textarea case pass against a fake no browser would ever produce.
  return { tagName: tag, type: opts.type || (tag === "TEXTAREA" ? "textarea" : "text"),
           dataset: opts.dataset || {}, value: value,
           // The chip editor's class. It is what marks a field as holding an
           // EXPRESSION, which is the only place a word after a number is a unit.
           classList: { contains: function (c){ return !!opts.chipped && c === "chipped"; } },
           selectionStart: caret, selectionEnd: caret,
           setSelectionRange: function(a,b){ this.selectionStart=a; this.selectionEnd=b; } };
}
function type(e, inputType){
  _handler({ target: e, inputType: inputType === undefined ? "insertText" : inputType });
  return e.value;
}
function check(label, got, want){
  var ok = got === want; if (!ok) fails++;
  print((ok ? "  PASS  " : "  FAIL  ") + label + (ok ? "" : "  got " + JSON.stringify(got) + " want " + JSON.stringify(want)));
}

check("first letter of the field",        type(el("e", 1)),            "E");
check("letter after a space",             type(el("Edge o", 6)),       "Edge O");
check("mid-word is left alone",           type(el("Edge of", 7)),      "Edge of");
check("already capital is untouched",     type(el("Edge O", 6)),       "Edge O");
check("a digit is not a letter",          type(el("100", 3)),          "100");
check("caret not at the end still works", type(el("edge Offset", 1)),  "Edge Offset");
check("after a hyphen",                   type(el("100-t", 5)),        "100-T");
check("after a slash",                    type(el("Left/r", 6)),       "Left/R");
check("after an opening paren",           type(el("(w", 2)),           "(W");
check("after a paren mid-text",           type(el("Edge (o", 7)),      "Edge (O");
check("after a bracket",                  type(el("[w", 2)),           "[W");
check("after an opening double quote",    type(el('Say "h', 6)),       'Say "H');
check("after an opening single quote",    type(el("'h", 2)),           "'H");
check("after a curly opening quote",      type(el("\u201Ch", 2)),      "\u201CH");
check("quote after a paren opens too",    type(el('("h', 3)),          '("H');
check("an apostrophe is NOT a word start", type(el("Don't", 5)),       "Don't");
check("nor a closing quote",              type(el('"Hi"x', 5)),        '"Hi"x');
check("other operators still are not",    type(el("100+t", 5)),        "100+t");
check("textarea too",                     type(el("w", 1, {tag:"TEXTAREA"})), "W");

/* A unit is not a word Josh forgot to capitalise: "25 mm" is one value, and the chip
   editor reads it as one item. Only in an expression field — everywhere else a word
   after a number is still a word. */
check("a unit after a number is left alone",
      type(el("25 m", 4, { chipped: true })), "25 m");
check("...and after the decimal point too",
      type(el("2.5 m", 5, { chipped: true })), "2.5 m");
check("a name after an operator still capitalises",
      type(el("25 mm + w", 9, { chipped: true })), "25 mm + W");
check("outside an expression field the rule does not apply",
      type(el("25 m", 4)), "25 M");

print("");
print("data-joinwords: the space is spent asking for the capital, not kept");
check("a second word joins onto the first",
      type(el("my p", 4, { dataset: { joinwords: "1" } })), "myP");
check("...caret lands right after the new capital",
      (function(){ var e = el("my p", 4, { dataset: { joinwords: "1" } });
                   type(e); return e.selectionStart; })(), 3);
check("a third word keeps joining",
      type(el("MyParam n", 9, { dataset: { joinwords: "1" } })), "MyParamN");
check("the very FIRST letter still just capitalises — no space to spend",
      type(el("m", 1, { dataset: { joinwords: "1" } })), "M");
check("mid-word is still left alone",
      type(el("MyParam na", 10, { dataset: { joinwords: "1" } })), "MyParam na");
check("without the attribute the space is kept, as everywhere else",
      type(el("my p", 4)), "my P");
check("a comma starts a feature level, capitalised in place",
      type(el("Boot,c", 6, { dataset: { joinwords: "1" } })), "Boot,C");
check("...but not in a field without the attribute",
      type(el("a,b", 3)), "a,b");

print("");
print("things it must NOT touch:");
check("a paste is left alone",            type(el("edge", 4), "insertFromPaste"), "edge");
check("an undo is left alone",            type(el("edge", 4), "historyUndo"),     "edge");
check("opted out with data-nocaps",       type(el("e", 1, {dataset:{nocaps:"1"}})), "e");
check("a checkbox is ignored",            type(el("e", 1, {type:"checkbox"})),   "e");
var sel = el("edge", 4); sel.selectionEnd = 2;
_handler({ target: sel, inputType: "insertText" });
check("a live selection is left alone",   sel.value, "edge");
check("empty field, caret at 0",          type(el("", 0)),             "");

print("");
print(fails ? ("FAIL: " + fails) : "all capitalisation cases passed");
