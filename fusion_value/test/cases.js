/* Assertions for the value window's autocomplete.
 *
 * Two sets of parameters are used, deliberately:
 *
 *   SYNTHETIC  names written in the CONVENTION — what is measured, then the type, then
 *              the axis or direction. Every behavioural assertion runs against these, so
 *              the tests describe the rule rather than whatever the desk model happens
 *              to be called this month. They do not rot when parameters are renamed.
 *   PARAMS     the real document, as a smoke test: a hundred names the engine has never
 *              seen must not crash it and must still produce a usable list.
 */

Naming.setVocabulary(VOCAB);
vocabReady = true;

const SYNTHETIC = [
  // One family, four situations.
  { name: "EdgeOffsetBackY",  expression: "720 mm" },
  { name: "EdgeOffsetFrontY", expression: "620 mm" },
  { name: "EdgeOffsetX",      expression: "500 mm" },
  { name: "EdgeOffsetZ",      expression: "720 mm" },
  // A direction BEFORE the type is a descriptor: these are the back gear and the front
  // gear, so they must stay two separate things and never merge into one family.
  { name: "DrawerBackGearOffsetZ",  expression: "10 mm" },
  { name: "DrawerFrontGearOffsetZ", expression: "12 mm" },
  // Josh's own example: Back describes which part of the boot ceiling, Front is the
  // situation.
  { name: "BootCeilingBackOffsetFront", expression: "30 mm" },
  // A family whose stem is ALSO a real parameter name — the stem must not be offered.
  { name: "PlateOffset",  expression: "5 mm" },
  { name: "PlateOffsetX", expression: "7 mm" },
  // No axis at all.
  { name: "Thickness", expression: "18 mm" },
  // Names that are ENTIRELY a variable word. They still carry their axis — treating
  // them as untagged made them match every situation.
  { name: "Depth",  expression: "600 mm" },
  { name: "Height", expression: "720 mm" },
  { name: "Width",  expression: "1400 mm" },
  // For the unit cases.
  { name: "TaperAngle",   expression: "3.2 deg", unit: "deg" },
  { name: "TaperInMetric", expression: "5 cm",    unit: "cm"  },
  { name: "TaperCount",   expression: "6",       unit: ""    },
  // For the frame cases. ShelfOffsetY is measured from the Y origin; ShelfOffsetBack is
  // measured IN FROM the back bound; Depth is the span that carries you between them.
  { name: "ShelfOffsetY",    expression: "300 mm", unit: "mm" },
  { name: "ShelfOffsetBack",  expression: "30 mm", unit: "mm" },
  { name: "ShelfOffsetFront", expression: "20 mm", unit: "mm" },
  // On a symmetric axis the span word names the bound, so this is measured in from the
  // side and must compare equal to a "right" or "left" datum.
  { name: "RunnerOffsetWidth", expression: "12 mm", unit: "mm" },
  { name: "RunnerOffsetX",     expression: "40 mm", unit: "mm" },
  // A name starting with "co", so the ranking of Fusion's own words against the
  // document's own has something to rank.
  { name: "CoverThickness", expression: "3 mm" },
  // For the word-anchoring cases: "we" appears inside Po|we|rboard but starts no word.
  { name: "SocketPowerboardWidth", expression: "300 mm" },
  { name: "PowerboardOffsetX",     expression: "120 mm" },
  // W and E each start a word here, but the name does not contain "we".
  { name: "WingEdgeConvexityZ",    expression: "4 mm" },
  // ...whereas this one does, at the start of the word Web.
  { name: "WingWebThickness",      expression: "6 mm" }
];

function suggest(typed, ax, en){
  // datum reset too, or a frame left behind by an earlier case leaks into this one.
  axis = ax; end = en; datum = ""; symmetric = false; invalidateEntries();
  __els["value"].value = typed;
  __els["value"].selectionStart = __els["value"].selectionEnd = typed.length;
  document.activeElement = valueEl;
  updateAC();
  return acItems.map(it => it.entry.label +
      (it.entry.stem ? "  ->  " + it.entry.target : ""));
}
function accept(typed, ax, en){
  suggest(typed, ax, en);
  // Deliberately does NOT force a selection: pressing Enter takes whatever updateAC
  // highlighted, and the point of several cases below is that it sometimes highlights
  // nothing on purpose.
  acceptAC();
  return __els["value"].value;
}
var fails = 0;
function expect(label, got, want){
  var ok = String(got) === String(want);
  if (!ok) fails++;
  print((ok ? "  PASS  " : "  FAIL  ") + label +
        (ok ? "" : "\n          got  " + got + "\n          want " + want));
}

params = SYNTHETIC;

print("=== one family, resolved by the situation ===");
expect('EdgeOffset on a Y dimension behind the origin', accept("EdgeOff", "Y", "high"), "EdgeOffsetBackY");
expect('EdgeOffset on a Y dimension in front of it',    accept("EdgeOff", "Y", "low"),  "EdgeOffsetFrontY");
expect('EdgeOffset on an X dimension',                  accept("EdgeOff", "X", ""),     "EdgeOffsetX");
expect('EdgeOffset on a Z dimension',                   accept("EdgeOff", "Z", ""),     "EdgeOffsetZ");

print("");
print("=== the stem is offered, and it says what it resolved to ===");
var rows = suggest("EdgeOff", "Y", "high");
expect("top row is the stem with its resolution", rows[0], "EdgeOffset  ->  EdgeOffsetBackY");

print("");
print("=== a direction BEFORE the type is a descriptor ===");
// The back gear and the front gear are different things. If Back/Front were read as
// situations these two would collapse into one family, and asking for the gear's Z
// offset would return whichever sorted first.
var gear = suggest("GearOffset", "Z", "");
expect("the two gears never merge into one family",
       gear.some(r => r.indexOf("->") >= 0), false);
expect("both gears are still offered", gear.length >= 2, true);

print("");
print("=== Josh's example: BootCeilingBackOffsetFront ===");
// Back survives into the stem, so the name is still reachable by the descriptor.
var bc = suggest("BootCeilingBack", "Y", "low");
expect("reachable by its descriptor", bc[0], "BootCeilingBackOffsetFront");

print("");
print("=== a stem that is also a real name is never offered as a stem ===");
// PlateOffset and PlateOffsetX form the family "PlateOffset", which is itself a
// parameter. Offering the stem would mean typing an existing name in full and having it
// silently resolve to a different parameter.
var plate = suggest("PlateOffset", "X", "");
expect("no stem row", plate.some(r => r.indexOf("->") >= 0), false);
expect("the real name is what you get", accept("PlateOffset", "X", ""), "PlateOffset");

print("");
print("=== a match must begin at a word start ===");
// "we" is inside Po|we|rboard, but starts no word there, so it must not be offered.
// Every word of a parameter name starts with a capital, so a fragment landing mid-word
// is a coincidence of letters rather than something the name is called.
params = SYNTHETIC; invalidateEntries();
expect('"we" does not match Powerboard',
       suggest("we", "", "").indexOf("SocketPowerboardWidth"), -1);
// The same word IS reachable by its own start, even though it is not the start of the name.
var po = suggest("po", "X", "");
expect('"po" reaches Powerboard mid-name', po.length >= 2, true);
// What you type must be in the name VERBATIM. Collecting the initials of separate
// words is not a match — WingEdgeConvexityZ does not contain "we" at all.
expect('"we" does not collect initials',
       suggest("we", "", "").indexOf("WingEdgeConvexityZ"), -1);
expect('"we" does match the word Web',
       suggest("we", "", "")[0], "WingWebThickness");
// Having started a word, a fragment may run past the end of it.
expect('"offsetb" may run into the next word',
       accept("offsetb", "Y", "high"), "EdgeOffsetBackY");
// An earlier match outranks a later one.
expect('an earlier word start ranks higher',
       suggest("off", "X", "")[0], "EdgeOffset  ->  EdgeOffsetX");

print("");
print("=== a capital typed starts a new word ===");
// "bootD" is "boot" then a later word starting "d" — the words between may be skipped.
params = SYNTHETIC.concat([
  { name: "BootDoorWidth",      expression: "40 mm" },
  { name: "BootLatchDoorDepth", expression: "8 mm" },
  { name: "BootLatchWidth",     expression: "9 mm" }
]); invalidateEntries();
var bd = suggest("bootD", "", "");
expect('"bootD" reaches BootDoorWidth', bd.indexOf("BootDoorWidth") >= 0, true);
expect('"bootD" reaches BootLatchDoorDepth', bd.indexOf("BootLatchDoorDepth") >= 0, true);
expect('"bootD" skips a name with no D word after Boot', bd.indexOf("BootLatchWidth"), -1);
expect('"bootd" in lowercase stays verbatim', suggest("bootd", "", "").indexOf("BootLatchDoorDepth"), -1);
expect('"latchD" works mid-name', suggest("latchD", "", "")[0], "BootLatchDoorDepth");
params = SYNTHETIC; invalidateEntries();

print("");
print("=== the wrong situation is FILTERED OUT, not just ranked down ===");
// Ranking down does nothing when the wrong-situation names are the ONLY matches: on an
// X dimension "de" matched five Y-axis Depth parameters and offered all five.
params = SYNTHETIC; invalidateEntries();
expect('"de" offers nothing on an X dimension',
       suggest("de", "X", "").join(","), "");
expect('"wi" does not reach an X-axis Width from a Y dimension',
       suggest("wi", "Y", "").indexOf("SocketPowerboardWidth"), -1);
expect('the wrong END is filtered too',
       suggest("edgeoffset", "Y", "high").indexOf("EdgeOffsetFrontY"), -1);
// Only an explicit disagreement filters. A parameter with no axis is general.
expect('an untagged parameter survives every situation',
       suggest("thick", "X", "high").indexOf("Thickness") >= 0, true);
// A name that is entirely a variable word still carries its axis.
expect('bare Depth is a Y parameter, so an X dimension does not offer it',
       suggest("de", "X", "").indexOf("Depth"), -1);
expect('bare Height is a Z parameter, so an X dimension does not offer it',
       suggest("he", "X", "").indexOf("Height"), -1);
expect('bare Width IS an X parameter, so an X dimension does offer it',
       suggest("wid", "X", "").indexOf("Width") >= 0, true);

print("");
print("=== ...but the whole variable word is an escape hatch ===");
// You must IDENTIFY the parameter: the whole variable word AND something outside it.
// Naming a variable on its own is starting a search, not asking for a parameter.
expect('"width" alone does not reach an X-axis Width from a Y dimension',
       suggest("width", "Y", "").indexOf("SocketPowerboardWidth"), -1);
expect('naming it identifiably does',
       suggest("powerboardwidth", "Y", "").indexOf("SocketPowerboardWidth") >= 0, true);
expect('spelling out the end reaches it',
       accept("EdgeOffsetFront", "Y", "high"), "EdgeOffsetFrontY");
print("");
print("=== ...and a trig expression takes every axis ===");
// atan(Height / Depth) on an X dimension: the other axes are the point of it.
expect('"he" inside atan( offers Z Height on an X dimension',
       suggest("atan(he", "X", "").indexOf("Height") >= 0, true);
expect('...and after the call closes, anywhere in the expression',
       suggest("Rise / tan(Pitch) + he", "X", "").indexOf("Height") >= 0, true);
expect('a Y parameter by its bare variable word, inside cos(',
       suggest("Width * cos(de", "X", "").indexOf("Depth") >= 0, true);
expect('"cost(" is not trig — the filter still applies',
       suggest("cost(he", "X", "").indexOf("Height"), -1);

// ...but naming a variable is starting a search, not asking for a parameter. The axis
// words are one character long, so covering one whole is no evidence of anything.
expect('"y" alone does not reach every Y parameter from an X dimension',
       suggest("y", "X", "").indexOf("EdgeOffsetBackY"), -1);
expect('naming it identifiably does',
       suggest("edgeoffsetback", "X", "").indexOf("EdgeOffsetBackY") >= 0, true);

print("");
print("=== the wrong UNIT is filtered out ===");
params = SYNTHETIC; invalidateEntries();
unit = "mm";
expect('an angle is not offered for a length',
       suggest("taper", "", "").indexOf("TaperAngle"), -1);
expect('centimetres ARE offered for a millimetre dimension',
       suggest("taper", "", "").indexOf("TaperInMetric") >= 0, true);
expect('a unitless count is offered for anything',
       suggest("taper", "", "").indexOf("TaperCount") >= 0, true);
unit = "deg";
expect('a length is not offered for an angle',
       suggest("taper", "", "").indexOf("TaperInMetric"), -1);
expect('the angle IS offered for an angle',
       suggest("taper", "", "").indexOf("TaperAngle") >= 0, true);
unit = "";
expect('a session with no unit filters nothing',
       suggest("taper", "", "").length, 3);

print("");
print("=== ...except inside trigonometry ===");
// sin/cos/tan take an ANGLE whatever the dimension is measured in, so tan(Taper) in a
// millimetre expression must still be offered. This was the unit rule's one wrong answer.
function suggestAt(prefix, u){
  unit = u; axis = ""; end = ""; invalidateEntries();
  __els["value"].value = prefix;
  __els["value"].selectionStart = __els["value"].selectionEnd = prefix.length;
  document.activeElement = valueEl;
  updateAC();
  return acItems.map(function(it){ return it.entry.label; });
}
expect('tan( on a length dimension offers the angle',
       suggestAt("tan(taper", "mm").indexOf("TaperAngle") >= 0, true);
expect('...and drops the lengths it would otherwise offer',
       suggestAt("tan(taper", "mm").indexOf("TaperInMetric"), -1);
expect('nested inside an expression still works',
       suggestAt("100 * tan(taper", "mm").indexOf("TaperAngle") >= 0, true);
expect('a non-trig call keeps the dimension\'s unit',
       suggestAt("sqrt(taper", "mm").indexOf("TaperAngle"), -1);
// asin/acos/atan take a ratio, which may well be one length over another.
expect('atan( has no opinion, so everything is offered',
       suggestAt("atan(taper", "mm").length, 3);
expect('"atan" is not read as "tan"',
       suggestAt("atan(taper", "mm").indexOf("TaperInMetric") >= 0, true);
unit = "";

print("");
print("=== frames of reference ===");
// A dimension measured FROM the origin wants values measured from the origin, not
// values measured in from a bound. Mixing them gives a number that is arithmetically
// right and wrong on the model.
params = SYNTHETIC; invalidateEntries();
function suggestFrom(prefix, ax, dat, u, sym){
  axis = ax; end = ""; datum = dat; unit = u || "mm"; symmetric = !!sym;
  invalidateEntries();
  __els["value"].value = prefix;
  __els["value"].selectionStart = __els["value"].selectionEnd = prefix.length;
  document.activeElement = valueEl;
  updateAC();
  // TARGETS, not labels: these three share the stem "ShelfOffset", so the row is
  // labelled with the stem and what matters is the parameter it resolves to.
  return acItems.map(function(it){ return it.entry.target; });
}
expect('from the origin, an origin-framed parameter is offered',
       suggestFrom("shelfoffset", "Y", "zero").indexOf("ShelfOffsetY") >= 0, true);
expect('...and a bound-framed one is not',
       suggestFrom("shelfoffset", "Y", "zero").indexOf("ShelfOffsetBack"), -1);

// THE FLIP. Naming the span carries you to the far side, and everything after it is
// measured from THERE.
expect('after naming the span, the bound-framed one appears',
       suggestFrom("Depth - shelfoffset", "Y", "zero").indexOf("ShelfOffsetBack") >= 0, true);
expect('...and the origin-framed one drops out',
       suggestFrom("Depth - shelfoffset", "Y", "zero").indexOf("ShelfOffsetY"), -1);

// The span itself belongs to neither frame — filtering it would make the flip
// unreachable from the origin frame in the first place.
expect('the span is always offered',
       suggestFrom("dep", "Y", "zero").indexOf("Depth") >= 0, true);

// A span for a DIFFERENT axis does not move you along this one.
expect('a span on another axis does not flip the frame',
       suggestFrom("Width - shelfoffset", "Y", "zero").indexOf("ShelfOffsetY") >= 0, true);

// No datum, no opinion — which is every dimension until bounds records exist.
expect('with no datum detected nothing is frame-filtered',
       suggestFrom("shelfoffset", "Y", "").length, 3);

// WHICH bound matters, not merely that it is one. Measuring from the front face wants
// values measured from the front; a back-framed one is as wrong there as an origin one.
expect('from the front bound, front-framed is offered',
       suggestFrom("shelfoffset", "Y", "front").indexOf("ShelfOffsetFront") >= 0, true);
expect('...and back-framed is not',
       suggestFrom("shelfoffset", "Y", "front").indexOf("ShelfOffsetBack"), -1);
expect('...and neither is origin-framed',
       suggestFrom("shelfoffset", "Y", "front").indexOf("ShelfOffsetY"), -1);
// A frame filter that would empty the box stands aside instead. Every parameter in a
// model may be origin-framed, and showing nothing teaches you the tool is broken rather
// than that the frames disagree.
expect('a frame with nothing to offer does not empty the list',
       suggestFrom("thick", "Y", "front").indexOf("Thickness") >= 0, true);
// Naming the span crosses to the opposite face.
// A symmetric axis names its bound with the span word, so "from the right" and "from
// the width" are the same frame.
expect('on a symmetric axis, a width-framed parameter matches a right datum',
       suggestFrom("runneroffset", "X", "right", "mm", true).indexOf("RunnerOffsetWidth") >= 0, true);
expect('...and the origin-framed one is filtered there',
       suggestFrom("runneroffset", "X", "right", "mm", true).indexOf("RunnerOffsetX"), -1);
expect('from the origin on that axis, the width-framed one is filtered',
       suggestFrom("runneroffset", "X", "zero", "mm", true).indexOf("RunnerOffsetWidth"), -1);
expect('...and the origin-framed one is offered',
       suggestFrom("runneroffset", "X", "zero", "mm", true).indexOf("RunnerOffsetX") >= 0, true);

expect('the span carries you from front to back',
       suggestFrom("Depth - shelfoffset", "Y", "front").indexOf("ShelfOffsetBack") >= 0, true);
expect('...and the front frame is left behind',
       suggestFrom("Depth - shelfoffset", "Y", "front").indexOf("ShelfOffsetFront"), -1);

print("");
print("=== bounds parameters keep their AXIS filter but not their END or FRAME one ===");
// Bounds.py tags six face parameters and three derived spans per component with a
// COMMENT rather than a name (see isBoundsParam / BOUNDS_COMMENT) — "Bounds of X" /
// "Span of X, derived from its faces". Both halves of this were broken against a live
// document: the END filter dropped the opposite face on a high-end dimension (typing
// "DrawerL" from an X/high situation offered nothing at all), and the FRAME filter
// dropped every origin-named face once the dimension itself was measured from a bound
// — which emptied the list down to spans on exactly the dimensions where a face is
// the answer you are reaching for.
const BOUNDS_PARAMS = [
  { name: "BracketRight", expression: "700 mm", comment: "Bounds of Bracket" },
  { name: "BracketLeft",  expression: "0 mm",   comment: "Bounds of Bracket" },
  { name: "BracketWidth", expression: "(BracketRight + BracketLeft) / 2",
    comment: "Span of Bracket, derived from its faces" },
  { name: "BracketBack",  expression: "500 mm", comment: "Bounds of Bracket" },
  { name: "BracketFront", expression: "0 mm",   comment: "Bounds of Bracket" }
];
params = BOUNDS_PARAMS; invalidateEntries();
expect("a bounds face is offered on the axis it names",
       suggest("BracketR", "X", "high").indexOf("BracketRight") >= 0, true);
expect("...and so is the OPPOSITE face on the same axis — only the axis is checked",
       suggest("BracketL", "X", "high").indexOf("BracketLeft") >= 0, true);
expect("a bounds face on the WRONG axis is still filtered",
       suggest("BracketB", "X", "high").indexOf("BracketBack") >= 0, false);
expect("measured from a bound rather than the origin, the same-axis face still shows",
       suggestFrom("BracketL", "X", "right").indexOf("BracketLeft") >= 0, true);
expect("...the far face too — a bounds param is exempt from the frame filter entirely",
       suggestFrom("BracketR", "X", "right").indexOf("BracketRight") >= 0, true);
params = SYNTHETIC; invalidateEntries();

print("");
print("=== a real name typed in full still wins ===");
expect("explicit beats inferred", accept("EdgeOffsetFront", "Y", "high"), "EdgeOffsetFrontY");

print("");
print("=== completion mid-expression ===");
axis = "Y"; end = "low"; invalidateEntries();
__els["value"].value = "100 - EdgeOff";
__els["value"].selectionStart = __els["value"].selectionEnd = 13;
document.activeElement = valueEl;
updateAC(); acceptAC();
expect("only the token under the caret is replaced",
       __els["value"].value, "100 - EdgeOffsetFrontY");

print("");
print("=== degrades safely with the engine unavailable ===");
vocabReady = false; invalidateEntries();
var bare = suggest("EdgeOff", "Y", "high");
expect("no stem rows", bare.some(r => r.indexOf("->") >= 0), false);
expect("real names still offered", bare.length > 0, true);
vocabReady = true; invalidateEntries();

print("");
print("=== smoke test against the real document ===");
params = PARAMS; invalidateEntries();
// The probe is TAKEN FROM the document, never written into this file. It used to be
// suggest("Edge", "Y", "high"), written when the desk model had an EdgeOffsetBackY /
// EdgeBackPositionY family. That family was later renamed away, and the case then
// passed only because "Edge" happened to land mid-name on ProngEdgeFillet — so the first
// time that name changed too, the smoke test failed with the engine untouched. Naming a
// real parameter here is exactly the rot the header says SYNTHETIC exists to avoid.
//
// So: the first real name that is longer than its probe and sits on an axis, typed to
// four characters in ITS OWN situation. That still runs real names through the situation
// filter (the point of passing an axis at all), and a name in its own situation must
// never be filtered out of its own list, so an empty list here is a real fault.
// unit is cleared because suggest() does not reset it and the frame cases above leave
// "mm" behind — which would quietly hide an angle-valued probe.
unit = "";
var probe = null;
for (var i = 0; i < PARAMS.length && !probe; i++){
  var pa = analysisOf(PARAMS[i].name);
  if (pa && pa.axis && PARAMS[i].name.length > 4){
    probe = { word: PARAMS[i].name.slice(0, 4), axis: pa.axis, end: pa.end || "" };
  }
}
// A document with no axis-tagged names at all (a fresh Untitled) has nothing to probe
// with; say so rather than report a failure the code did not cause.
if (probe){
  var real = suggest(probe.word, probe.axis, probe.end);
  expect("the real parameter list produces a list (\"" + probe.word + "\" on " +
         probe.axis + (probe.end ? " " + probe.end : "") + ")", real.length > 0, true);
} else {
  print("  SKIP  the real parameter list produces a list — no real name carries an axis");
}
var reachable = 0, expected = 0;
for (var i = 0; i < PARAMS.length; i++){
  var nm = PARAMS[i].name, pre = nm.slice(0, 4);
  // No situation, so nothing is filtered: this asks whether every name is FINDABLE,
  // which is a different question from whether it is offered in a given situation.
  var r = suggest(pre, "", "");              // must never throw
  // A name of four characters or fewer IS its own prefix, and a name typed in full is
  // correctly not suggested back to you — HIPS, MDF and Skin are the three here.
  if (pre === nm) continue;
  expected++;
  if (r.length) reachable++;
}
expect("every longer real name is reachable by its first four characters",
       reachable, expected);

print("");
print("=== the plane box ===");
/* One line: a datum word, then how far in from it. The datums here are what a component
 * with a symmetric X and an asymmetric Y and Z actually offers — the midline and the
 * side on X, both named faces on the other two. See Bounds.datum_words.
 */
const DATUMS = [
  { word:"X",      kind:"midline", axis:"X", end:"",     sign: 1, face:"",          datum:"zero",  symmetric:true  },
  { word:"Width",  kind:"face",    axis:"X", end:"",     sign: 1, face:"TestRight",  datum:"width", symmetric:true  },
  { word:"Back",   kind:"face",    axis:"Y", end:"high", sign: 1, face:"TestBack",   datum:"back",  symmetric:false },
  { word:"Front",  kind:"face",    axis:"Y", end:"low",  sign:-1, face:"TestFront",  datum:"front", symmetric:false },
  { word:"Top",    kind:"face",    axis:"Z", end:"high", sign: 1, face:"TestTop",    datum:"top",   symmetric:false },
  { word:"Bottom", kind:"face",    axis:"Z", end:"low",  sign:-1, face:"TestBottom", datum:"bottom",symmetric:false }
];

/* Type `typed` into the plane box with the caret at `caret` (default: the end) and let
 * the box do to it whatever it does on an input event. */
function planeBox(typed, caret){
  mode = "sketch"; datums = DATUMS; curDatum = null;
  params = SYNTHETIC; invalidateEntries();
  __els["value"].value = typed;
  const c = (caret == null) ? typed.length : caret;
  __els["value"].selectionStart = __els["value"].selectionEnd = c;
  document.activeElement = valueEl;
  syncSketchLine(); syncSketchSituation();
  return __els["value"].value;
}
function planeCaret(){ return __els["value"].selectionStart; }

// A datum on its own, and one term after it, are left exactly as typed.
expect("a bare datum",            planeBox("Width"),     "Width");
expect("one term after it",       planeBox("Width 100"), "Width 100");
// A leading sign belongs to the first term — "-5" is a number, not an operation.
expect("a negative number",       planeBox("Width -5"),  "Width -5");

// The moment a second term follows, the brackets appear — and the closing one lands
// AFTER the caret, so typing carries straight on into it.
expect("brackets appear mid-type", planeBox("Width 100 -"), "Width (100 -)");
expect("...with the caret before the closing bracket", planeCaret(), 12);
expect("two whole terms",         planeBox("Width 100 - Membrane"),
       "Width (100 - Membrane)");

// ⌫/⌦ on either bracket of that pair steps over it — it used to read the half left
// behind as typed and wrap it again, one more "(" per press.
function planeDelete(line, at, back){
  planeBox(line);
  __els["value"].value = line.slice(0, at) + line.slice(at + 1);
  __els["value"].selectionStart = __els["value"].selectionEnd = at;
  syncSketchLine({ inputType: back ? "deleteContentBackward" : "deleteContentForward" });
  return [__els["value"].value, planeCaret()];
}
expect("⌫ on the closing bracket steps inside it",
       planeDelete("Width (100 - Membrane)", 21, true), ["Width (100 - Membrane)", 21]);
expect("⌫ on the opening bracket steps before it",
       planeDelete("Width (100 - Membrane)", 6, true), ["Width (100 - Membrane)", 6]);
expect("⌦ on the closing bracket leaves it",
       planeDelete("Width (100 - Membrane)", 21, false), ["Width (100 - Membrane)", 21]);
expect("...and deleting a term is not undone",
       planeDelete("Width (100 - M)", 13, true)[0] !== "Width (100 - M)", true);

// And they go again when it drops back to one term.
expect("brackets disappear again", planeBox("Width (100)", 10), "Width 100");
expect("...caret kept on the same character", planeCaret(), 9);

// Typing on past the closing bracket wraps the lot, which is the arithmetic that was
// meant: the side, less ((100 - M) * 2).
expect("typing past the bracket re-wraps", planeBox("Width (100 - M) * 2"),
       "Width ((100 - M) * 2)");

// What the line composes to. This is what goes into the plane, and the low faces are
// negated as a whole because a low face sits at a negative coordinate.
planeBox("Width 100 - Membrane");
expect("composed, high face", sketchExpression(), "TestRight - (100 - Membrane)");
planeBox("Bottom 18");
expect("composed, low face",  sketchExpression(), "-(TestBottom - 18)");
planeBox("Bottom");
expect("composed, bare low face", sketchExpression(), "-TestBottom");
planeBox("X 250");
expect("composed, out from the midline", sketchExpression(), "250");
planeBox("X");
expect("composed, the midline itself", sketchExpression(), "0");

// The datum word sets the situation the REST of the line is typed in, so the offset
// completes against parameters measured from the same place.
planeBox("Back 20");
expect("the datum sets the axis",  axis, "Y");
expect("the datum sets the end",   end, "high");
expect("the datum sets the frame", datum, "back");
planeBox("X 250");
expect("a midline is measured from the origin", datum, "zero");

/* The suggestion list. An empty box offers the whole vocabulary — that is the answer to
 * "what can I say here" — and a prefix narrows it. */
function planeSuggest(typed, caret){
  planeBox(typed, caret);
  updateAC();
  return acItems.map(it => it.entry.label);
}
expect("an empty box offers every datum, axis by axis",
       planeSuggest("").join(" "), "X Width Back Front Top Bottom");
expect("a prefix narrows it", planeSuggest("B").join(" "), "Back Bottom");
expect("the datum list is case-insensitive", planeSuggest("wi").join(" "), "Width");
// Past the first word it is an ordinary expression again, completing against parameters.
var afterDatum = planeSuggest("Back 20 + Thick");
expect("past the datum it is the parameter list",
       afterDatum.indexOf("Back") < 0 && afterDatum.length > 0, true);

/* HOW A ROW IS MARKED. A list that has only just opened marks nothing — the mark says
 * "this is the one", and that is not true until you have said so with an arrow key.
 * acIndex is 0 all the same, so Enter still takes the top row; the default is a
 * convenience and has stopped looking like a choice. */
planeSuggest("");
expect("an open list marks nothing",       acEl.innerHTML.indexOf(" sel") >= 0, false);
expect("...though Enter still takes the top row", acIndex, 0);
acTouched = true; renderAC();               // what the first ↓ does — see the arrow keys
expect("stepping in marks the row",        acEl.innerHTML.indexOf('class="ac-row sel"') >= 0, true);
/* And the mark is a dot on the RIGHT, drawn on every row and shown on one, so the value
 * column beside it does not twitch as the mark moves down the list. */
expect("every row carries the mark",
       acEl.innerHTML.split('class="ac-mark"').length - 1, acItems.length);
/* A datum row says nothing but the word. The face parameter behind it (TestRight under
 * "Width") used to ride along in the value column; the word IS the vocabulary, and what
 * it composes to is spelled out in the readout under the line instead. */
expect("a datum row names no parameter",   acEl.innerHTML.indexOf("TestRight") >= 0, false);
acTouched = false;

// Accepting a datum takes a trailing space with it: what follows is how far in from it.
planeSuggest("Wi");
acceptAC();
expect("accepting a datum leaves a space", __els["value"].value, "Width ");
// Accepting sets .value directly, which fires NO input event — so the situation has to
// be re-read HERE or the box has a datum on screen and none in hand, and the commit
// refuses the very word the list just put there.
expect("...and the datum is in hand afterwards", curDatum && curDatum.word, "Width");
expect("...so it composes", sketchExpression(), "TestRight");

/* AND IT IS A CHIP. A datum is not a parameter, but it is exactly the kind of thing a
 * chip is for — a face you either mean or you don't — so chipsAttach hands the words
 * this component offers to chips.js as vocabulary alongside the parameters, and the
 * ordinary "a word closed is an item" rule does the rest. Asserted through the field's
 * own opts rather than a stand-in: the whole point is that the LIVE box says so. */
function planeVocab(){
  var st = Chips._state(valueEl);
  return (st && st.opts.names()) || [];
}
mode = "sketch"; datums = DATUMS;
expect("a datum word is vocabulary in the plane box",
       planeVocab().indexOf("Bottom") >= 0, true);
expect("...as is the midline's",  planeVocab().indexOf("X") >= 0, true);
expect("...alongside the parameters, not instead of them",
       planeVocab().indexOf(SYNTHETIC[0].name) >= 0, true);
// So it chips on the rule every other item chips on — the word, closed.
expect("a closed datum is an item",
       Chips.scan("Bottom 18", { caret: null, typingAt: -1,
                                 isParam: function (w){ return planeVocab().indexOf(w) >= 0; } })
         .map(function (a){ return a.kind + ":" + a.name; }).join(" "),
       "param:Bottom num:18");
mode = "dim";
expect("and nowhere else: a dimension box has no datums",
       planeVocab().indexOf("Bottom") >= 0, false);
mode = "sketch";

print("");
print('=== "same plane as" — the plane-box sketch picker ===');
mode = "sketch"; datums = DATUMS; curDatum = null;
sessionSketches = [
  { name: "Cutout", token: "tFlat", text: "Width 40", angle: null },
  { name: "Louver", token: "tAngled", text: "Back 20",
    angle: { text: "30 deg", flip: true, plus90: false, pivot: "Y" } }
];
renderPick();
expect("angled sketches are listed before flat ones",
       pickEl.innerHTML.indexOf("Louver") < pickEl.innerHTML.indexOf("Cutout"), true);
expect("a separator sits between the two groups",
       pickEl.innerHTML.indexOf("pick-sep") >= 0, true);
expect("an angled row's spec names the toggles that are on",
       pickEl.innerHTML.indexOf("30 deg · reversed · about Y") >= 0, true);
expect("a flat row carries no spec", pickEl.innerHTML.indexOf("Cutout</span><span class=\"ac-val\">Width 40<") >= 0, true);

applySketchRecipe(sessionSketches[1]);
expect("picking a recipe copies its line", __els["value"].value, "Back 20");
expect("...and its angle text", angleEl.value, "30 deg");
expect("...with the toggle it was stored with", angleFlip, true);
expect("...and the one that wasn't", anglePlus90, false);
expect("...and its pivot", anglePivot, "Y");
expect("the picker closes after picking", pickEl.innerHTML, "");

sessionSketches = [];
mode = "dim"; datums = []; curDatum = null;   // don't leak the mode into later cases

/* ─────────────────────────────────────────────────────────────────────────────
   Chips — expressions typed as items.

   The window itself cannot be looked at (screencapture returns solid black for these
   WKWebViews), and the drag is a pointer gesture. But everything that DECIDES anything
   is pure text work — which words are items, what an expression is made of, and where
   a dragged item lands — so it is asserted here, on the real chips.js the window
   serves.                                                                            */
print("");
print("=== chips: when a name becomes an item ===");

var CHIP_NAMES = { Width: true, Taper: true, Thickness: true };
function chipCtx(caret, typingAt, bubbles){
  return { caret: caret === undefined ? null : caret,
           typingAt: typingAt === undefined ? -1 : typingAt,
           isParam: function (w){ return CHIP_NAMES[w] === true; },
           bubbles: bubbles };
}
/* The items in `text`, as "kind:name" — with the caret written into the string as a
   pipe, which is the only readable way to say where it is. `typingAt` is where the
   word the last keystroke added to begins, or -1 for a caret that is merely parked
   somewhere; the two are what tell a word still being spelled from a finished one. */
function items(text, typingAt){
  var caret = text.indexOf("|");
  if (caret >= 0) text = text.slice(0, caret) + text.slice(caret + 1);
  var out = Chips.scan(text, chipCtx(caret < 0 ? null : caret, typingAt));
  return out.map(function (a){ return a.kind + ":" + a.name; }).join(" ");
}

expect("a word still being typed is letters",        items("Width|", 0), "");
expect("...even where it already reads as a name",   items("Widt|", 0), "");
expect("a space after it makes it an item",          items("Width |"), "param:Width");
expect("so does an operator",                        items("Width +|"), "param:Width op:+");
expect("so does a bracket",                          items("(Width)|"), "bracket:( param:Width bracket:)");
expect("accepting it makes it one immediately",      items("Width|"), "param:Width");
// Which is also what makes Backspace take the whole of it: a caret parked at the end
// of a finished word is aimed at an item, not at its last letter.
expect("the caret BEFORE it does not open it",       items("|Width "), "param:Width");
expect("clicking into it opens it again",            items("Wid|th "), "");
expect("a name that does not exist is never an item", items("Widthly |"), "");
expect("unfocused, everything is an item",           items("Width + Taper"),
       "param:Width op:+ param:Taper");

/* Punctuation is painted, never taken hold of. An operator is Fusion's, not yours — and
   a SIGN is not an operator at all: it belongs to the number it signs, which is what
   keeps "-5" from being painted apart into "- 5". */
expect("a sign is not an operator",   items("-5 + Width"), "num:-5 op:+ param:Width");
expect("...nor is one after another", items("Width * -5"), "param:Width op:* num:-5");
expect("a comma between arguments is",items("min(Width, Taper)"),
       "fn:min");

/* A number is an item on exactly the rule a name is: while it is being typed it is
   digits, and the space that ends it makes it one thing. The unit goes with it — "25 mm"
   is one value, said the way it is said. */
expect("a number being typed is digits",   items("25|", 0), "");
expect("a space after it makes it an item", items("25 |"), "num:25");
expect("the unit is part of the number",   items("25 mm |"), "num:25 mm");
expect("...and is still being typed too",  items("25 m|", 3), "");
expect("a name after a number is not a unit", items("25 Width|"), "num:25 param:Width");

print("");
print("=== chips: a function absorbs its argument ===");
expect("the call and what is in it are one item", items("cos(Taper)|"), "fn:cos");
expect("an unclosed call is still being typed",   items("cos(Tap|"), "bracket:(");
expect("a constant is Fusion's own item",         items("PI * 2|"), "const:PI op:* num:2");
expect("the argument is an item inside the call",
       Chips.scan("cos(Taper)", chipCtx(null)).map(function (a){
         return a.items.map(function (i){ return i.kind + ":" + i.name; }).join(""); }).join(""),
       "param:Taper");
expect("the caret in the NAME opens the call, not the argument",
       items("co|s(Taper)"), "param:Taper");

print("");
print("=== chips: items do not touch ===");
/* Typed without room between them, items are written back with it. Only around a
   binary operator, and never while deleting — see space(). */
function spaced(text){ return Chips.space(text, text.length, chipCtx(null)).text; }
expect("a name typed straight onto an operator", spaced("Width+Taper"), "Width + Taper");
expect("half of one is enough",                  spaced("Width+"), "Width +");
expect("a number counts the same",               spaced("2*Width"), "2 * Width");
expect("brackets stay tight against what they hold",
       spaced("(Width+2)*3"), "(Width + 2) * 3");
expect("a sign is left alone",                   spaced("-5 + Width"), "-5 + Width");
expect("room already made is left alone",        spaced("Width  +  Taper"), "Width  +  Taper");
/* The caret rides along, or typing would jump a character every time a space appeared. */
expect("the caret keeps its place",
       Chips.space("Width+", 6, chipCtx(null)).caret, 7);
/* …but a word typed just before an operator keeps the caret on its letters, not across
   the gap written after it — or the next letter would start a word of its own. */
expect("typing before an operator keeps the caret on the word",
       Chips.space("Depth X+ Right", 7, chipCtx(7, 6)).caret, 7);

print("");
print("=== chips: two items side by side are added ===");
/* Nothing between two things is not nothing: it is a sum. The "+" is written in, so
   what is on screen is what Fusion is given and the operator can be dragged and deleted
   like any other. */
expect("two names",              spaced("Width Taper"), "Width + Taper");
expect("a name and a number",    spaced("Width 25"), "Width + 25");
expect("a number and its unit is one thing", spaced("25 mm"), "25 mm");
expect("inside a call, too",     spaced("cos(Width Taper)"), "cos(Width + Taper)");
expect("an operator already there is left alone", spaced("Width * Taper"), "Width * Taper");
expect("a word that is nothing is not an item",   spaced("Width foo"), "Width foo");
/* Still being spelled, so there is nothing yet to add to. */
expect("...nor is one still being typed",
       Chips.space("Width Width", 11, chipCtx(11, 6)).text, "Width Width");

print("");
print("=== chips: the space commits the word ===");
/* The spacebar is not a character — it never puts a space in the field. It is handed
   the text and the END of the word, and what comes back is what the field then holds.
   The spacing pass runs straight after it, exactly as it does in the window. */
var CHIP_OPTS = { names: function (){ return ["Width", "Taper", "Thickness"]; } };
function typeSpace(text){
  var caret = text.length;
  var done = Chips.commit(CHIP_OPTS, text, caret);
  if (!done) return text;                       // nothing to commit, and no space either
  var sp = Chips.space(done.text, done.caret, chipCtx(done.caret, -1));
  return sp.text;
}
expect("f is +",  typeSpace("Width f"), "Width + ");
expect("d is -",  typeSpace("Width d"), "Width - ");
expect("s is *",  typeSpace("Width s"), "Width * ");
expect("a is /",  typeSpace("Width a"), "Width / ");
expect("w is a sin, opened and closed", typeSpace("Width w"), "Width + sin()");
expect("...with the caret inside it",
       Chips.commit(CHIP_OPTS, "Width w", 7).caret, 10);
expect("e is a cos",  typeSpace("Taper e"), "Taper + cos()");
expect("a name Fusion knows stays",     typeSpace("Width"), "Width");
expect("so does a constant",            typeSpace("PI"), "PI");
expect("so does a number",              typeSpace("25"), "25");
expect("so does a unit on its number",  typeSpace("25 mm"), "25 mm");
expect("a word that is nothing goes",   typeSpace("Width foo"), "Width ");
expect("...even on its own",            typeSpace("Widthly"), "");
/* capitalise.js makes "hips" "Hips"; a short name HIPS is still what was meant. */
expect("a name in the wrong case is that name", typeSpace("Width taper"), "Width + Taper");
expect("...but not when two names answer",
       Chips.commit({ names: function (){ return ["Width", "width"]; } }, "WIDTH", 5).text, "");
/* No word in front of it: the key does nothing, and above all does not type a space. */
expect("nothing in front of it is nothing", Chips.commit(CHIP_OPTS, "Width ", 6), null);
expect("...nor after an operator",          Chips.commit(CHIP_OPTS, "Width +", 7), null);
/* Until the parameters have arrived nothing is a name, and deleting on that would
   empty the box while the window was still starting up. */
expect("with no vocabulary yet, nothing goes",
       Chips.commit({}, "Widthly", 7).text, "Widthly");
/* A guarded word is not this file's business at all, and the space after it is a real
   one — it is what separates the plane box's datum from the offset measured off it. */
expect("a guarded word is left for the field",
       Chips.commit({ names: CHIP_OPTS.names, guard: function (t, a){ return a === 0; } },
                    "Front", 5), null);

print("");
print("=== chips: what an expression is made of ===");
function shape(text){
  var sp = Chips.splitItems(text, 0, text.length);
  return sp.items.map(function (it){ return text.slice(it.a, it.b); }).join(" | ");
}
expect("operands and operators",   shape("100 * cos(Taper) + Width"), "100 | cos(Taper) | Width");
expect("a number keeps its unit",  shape("25 mm + Width"), "25 mm | Width");
expect("a sign belongs to what it signs", shape("-5 + Width"), "-5 | Width");
expect("a bracketed group is one operand", shape("(Width + 2) * 3"), "(Width + 2) | 3");
expect("a half-typed line stops rather than guessing", shape("Width + "), "Width");

print("");
print("=== chips: dragging permutes the operands ===");
/* The operators stay exactly where they are and the operands move between them, which
   is what makes a drag predictable: "A + B * C" can never become "A B + * C". */
function drag(text, itemText, toIndex){
  var atoms = Chips.scan(text, chipCtx(null));
  var conts = Chips.containers(text, atoms);
  var at = text.indexOf(itemText);
  var plan = Chips.planMove(text, { a: at, b: at + itemText.length },
                            conts[0], toIndex, conts);
  return plan ? plan.text : "(refused)";
}
expect("to the front", drag("100 * cos(Taper) + Width", "Width", 0),
       "Width * 100 + cos(Taper)");
expect("to the middle", drag("100 * cos(Taper) + Width", "100", 2),
       "cos(Taper) * 100 + Width");
expect("onto itself is not a move", drag("100 + Width", "100", 0), "(refused)");

/* The one move that crosses a bracket: into an EMPTY call. Nothing has to be invented
   for it — an empty pair of brackets is a slot waiting for exactly one thing — and it
   is how a parameter already typed gets wrapped in a cos(). */
function dragInto(text, itemText){
  var atoms = Chips.scan(text, chipCtx(null));
  var conts = Chips.containers(text, atoms);
  var empty = null;
  for (var i = 0; i < conts.length; i++) if (!conts[i].items.length) empty = conts[i];
  var at = text.indexOf(itemText);
  var plan = Chips.planMove(text, { a: at, b: at + itemText.length }, empty, 0, conts);
  return plan ? plan.text : "(refused)";
}
expect("a parameter dropped into an empty call is wrapped by it",
       dragInto("Taper + cos()", "Taper"), "cos(Taper)");
expect("...taking the dangling operator with it",
       dragInto("100 * Taper + cos()", "Taper"), "100 * cos(Taper)");

print("");
print("=== chips: deleting an item takes its orphaned operator ===");
/* Backspace at the end of an item deletes the whole item — that is what makes it an
   item. What it must not leave behind is an operator with nothing on one side of it. */
function del(text, itemText){
  var at = text.indexOf(itemText);
  return Chips.removeRange(text, at, at + itemText.length).text;
}
expect("from the middle",     del("Width + Depth + Height", "Depth"), "Width + Height");
expect("from the front",      del("Depth * Width", "Depth"), "Width");
expect("a sign is left alone", del("Depth - Width", "Depth"), " - Width");
expect("a trailing operator is not orphaned", del("Width + Depth", "Depth"), "Width + ");
expect("nothing either side of it", del("Width", "Width"), "");

print("");
print("=== chips: a second delete takes the rest of the gap, not one raw char ===");
/* Deleting "Depth" above leaves "Width + " on purpose (see above) — a line still
 * being typed. The NEXT Backspace must take the whole dangling " + " in one press
 * rather than the browser's native one-character-at-a-time delete, which used to
 * mean three presses to remove two chips instead of two. */
function backspaceAt(text, pos){
  var r = Chips.danglingRun(text, pos, true);
  return r ? Chips.removeRange(text, r.a, r.b).text : "(nothing to do)";
}
function deleteAt(text, pos){
  var r = Chips.danglingRun(text, pos, false);
  return r ? Chips.removeRange(text, r.a, r.b).text : "(nothing to do)";
}
expect("a dangling trailing operator goes with its spaces",
       backspaceAt("Width + ", 8), "Width");
expect("a lone trailing space with nothing else goes on its own",
       backspaceAt("Width ", 6), "Width");
expect("an item boundary is not this file's to answer",
       Chips.danglingRun("Width + Depth", 5, true), null);
expect("a dangling leading operator goes with its spaces",
       deleteAt(" + Depth", 0), "Depth");

print("");
print("=== bubbles: a tracked paren pair becomes one \"group\" atom ===");
/* A bubble is a live {a,b} in ctx.bubbles, re-checked against the text on every
 * scan (see bubbleAt in chips.js) rather than trusted — so it behaves correctly
 * both while it is still valid and once an edit has invalidated it. */
function groupItems(text, bubbles){
  return Chips.scan(text, chipCtx(null, -1, bubbles))
    .map(function (a){ return a.kind + ":" + (a.name || a.items.length); }).join(" ");
}
expect("a matched pair at the tracked position groups",
       groupItems("(Width)", [{ a: 0, b: 7 }]), "group:1");
expect("...with its contents scanned inside it",
       Chips.scan("(Width)", chipCtx(null, -1, [{ a: 0, b: 7 }]))[0].items[0].kind, "param");
expect("an untracked pair is a plain bracket instead",
       groupItems("(Width)", []), "bracket:( param:Width bracket:)");
expect("a tracked position whose close paren has moved falls back to plain",
       groupItems("(Width", [{ a: 0, b: 7 }]), "bracket:( param:Width");
expect("a tracked pair around two chips nests its own container",
       Chips.containers("(Width + Taper)",
         Chips.scan("(Width + Taper)", chipCtx(null, -1, [{ a: 0, b: 15 }]))).length, 2);

print("");
print("=== bubbles: dragging one moves it as a block ===");
function dragGroup(text, bubbles, itemText, toIndex){
  var ctx = chipCtx(null, -1, bubbles);
  var atoms = Chips.scan(text, ctx);
  var conts = Chips.containers(text, atoms);
  var at = text.indexOf(itemText);
  var plan = Chips.planMove(text, { a: at, b: at + itemText.length }, conts[0], toIndex, conts);
  return plan ? plan.text : "(refused)";
}
expect("a bubble moves whole, like a call does",
       dragGroup("(Width + Taper) * 2", [{ a: 0, b: 15 }], "(Width + Taper)", 2),
       "2 * (Width + Taper)");
function dragIntoGroup(text, bubbles, itemText){
  var ctx = chipCtx(null, -1, bubbles);
  var atoms = Chips.scan(text, ctx);
  var conts = Chips.containers(text, atoms);
  var empty = null;
  for (var i = 0; i < conts.length; i++) if (!conts[i].items.length) empty = conts[i];
  var at = text.indexOf(itemText);
  var plan = Chips.planMove(text, { a: at, b: at + itemText.length }, empty, 0, conts);
  return plan ? plan.text : "(refused)";
}
expect("a chip drags INTO an empty bubble the way it drops into an empty call",
       dragIntoGroup("Taper + ()", [{ a: 8, b: 10 }], "Taper"), "(Taper)");

print("");
print("=== bubbles: keeping tracked ranges correct across an edit ===");
function splice(oldText, newText, bubbles){
  return Chips.shiftBubbles(bubbles, oldText, newText)
    .map(function (b){ return b.a + "-" + b.b; }).join(",");
}
expect("an edit entirely before a bubble shifts it",
       splice("Depth Width", "Depth  Width", [{ a: 6, b: 11 }]), "7-12");
expect("an edit entirely after a bubble leaves it alone",
       splice("(Width) Depth", "(Width) DepthX", [{ a: 0, b: 7 }]), "0-7");
expect("typing inside a bubble stretches its close paren, not its open one",
       splice("(Width)", "(WidthX)", [{ a: 0, b: 7 }]), "0-8");
expect("an edit across the open paren drops the bubble",
       splice("(Width)", "XWidth)", [{ a: 0, b: 7 }]), "");
expect("an edit across the close paren drops the bubble",
       splice("(Width)", "(WidthX", [{ a: 0, b: 7 }]), "");

print("");
print("=== bubbles: making and destroying one keeps a NESTED bubble correct ===");
/* The two-single-edit trick (insertGroup/removeGroup do the ")" then the "(", or
 * the reverse) exists for exactly this: one lumped edit spanning both characters
 * would misread a bubble sitting inside the span as having had its own parens
 * touched, and drop it instead of shifting it. */
expect("wrapping a span shifts a bubble already inside it",
       (function(){
         var made = Chips.insertGroup("A (B) C", 0, 7, [{ a: 2, b: 5 }]);
         return made.text + " | " + made.bubbles.map(function (b){ return b.a+"-"+b.b; }).join(",");
       })(), "(A (B) C) | 3-6,0-9");
expect("unwrapping a bubble shifts one nested inside it",
       (function(){
         var made = Chips.removeGroup("(A (B) C)", { a: 0, b: 9 }, [{ a: 0, b: 9 }, { a: 3, b: 6 }]);
         return made.text + " | " + made.bubbles.map(function (b){ return b.a+"-"+b.b; }).join(",");
       })(), "A (B) C | 2-5");

print("");
print("=== bubbles: a Cmd-click set snaps out to whole items of one container ===");
function snapped(text, picks, bubbles){
  var ranges = picks.map(function (p){ var at = text.indexOf(p); return { a: at, b: at + p.length }; });
  var s = Chips.snapSpan(text, Chips.scan(text, chipCtx(null, -1, bubbles || [])), ranges);
  return s ? text.slice(s.a, s.b) : "(none)";
}
expect("siblings snap to exactly themselves",
       snapped("Width + Taper * 2", ["Width", "Taper"]), "Width + Taper");
expect("skipping the middle one takes it along",
       snapped("Width + Taper + Thickness", ["Width", "Thickness"]), "Width + Taper + Thickness");
expect("a chip inside a call takes the whole call, never half its brackets",
       snapped("Width + cos(Taper + 2)", ["Width", "Taper"]), "Width + cos(Taper + 2)");
expect("a chip inside plain brackets takes the whole bracket",
       snapped("Width * (Taper + 2)", ["Width", "Taper"]), "Width * (Taper + 2)");
expect("two chips inside one bubble stay inside it",
       snapped("(Width + Taper + 2) * 3", ["Width", "Taper"], [{ a: 0, b: 19 }]), "Width + Taper");
expect("one chip is not a set",
       snapped("Width + Taper", ["Width"]), "(none)");

print("");
print("=== make a parameter: what is offered, and what goes back in its place ===");
params = SYNTHETIC.concat([{ name: "Taper", expression: "2 mm" }]); invalidateEntries();
document.activeElement = valueEl;
valueEl.value = "Width + Taper";
valueEl.selectionStart = 0; valueEl.selectionEnd = 5;
refreshChips();
expect("one held parameter chip is offered, as itself",
       (function(){ var s = Chips.selectionOf(valueEl); return s && (s.expr + " | " + s.whole); })(),
       "Width | true");
valueEl.selectionStart = 6; valueEl.selectionEnd = 7;
expect("one held operator is not offered", Chips.selectionOf(valueEl), null);
valueEl.value = "Width + 25 mm";
valueEl.selectionStart = 8; valueEl.selectionEnd = 13;
refreshChips();
expect("one held number is offered, as itself",
       (function(){ var s = Chips.selectionOf(valueEl); return s && (s.expr + " | " + s.whole); })(),
       "25 | true");     // the mm comes off on the refresh — see bareUnits below
valueEl.value = "Width * 2 + 3";
valueEl.selectionStart = valueEl.selectionEnd = 0;
refreshChips();
expect("replacing a span leaves the new name there, held",
       (function(){
         params = params.concat([{ name: "Twice", expression: "Width * 2" }]); invalidateEntries();
         Chips.replaceSpan(valueEl, 0, 9, "Twice");
         return valueEl.value + " | " + valueEl.value.slice(valueEl.selectionStart, valueEl.selectionEnd);
       })(), "Twice + 3 | Twice");

// Every expression field offers it, not only the value box of a dimension.
var wasOpen = isOpen, wasMode = mode;
isOpen = true; isDriven = false;
function heldIn(el, text, a, b){
  document.activeElement = el;
  el.value = text; el.selectionStart = a; el.selectionEnd = b;
  Chips.refresh(el);
  return canMake(el);
}
mode = "sketch"; datums = DATUMS;
expect("the plane box offers a held number in the offset", heldIn(valueEl, "Width 100", 6, 9), true);
expect("...never the datum word", heldIn(valueEl, "Width 100", 0, 5), false);
mode = "dim";
expect("one held call is offered, as itself",
       (heldIn(valueEl, "cos(Taper) * 2", 0, 10), (function(){
         var s = Chips.selectionOf(valueEl); return s && (s.expr + " | " + s.whole); })()),
       "cos(Taper) | true");
mode = "extrude";
var rowField = document.createElement("input");
chipsAttach(rowField);
expect("a panel's expression row offers it", heldIn(rowField, "25 + Width", 0, 2), true);
expect("...and not the value box, which is not on this panel",
       heldIn(valueEl, "25 + Width", 0, 2), false);
heldIn(rowField, "25 + Width", 0, 2);
openMake(rowField);
expect("...making from it remembers the field and the panel",
       [making && making.el === rowField, making && making.back], [true, panelShown]);
params = params.concat([{ name: "Gap", expression: "25" }]); invalidateEntries();
finishMake("Gap");
expect("...and the name goes back into THAT field", rowField.value, "Gap + Width");
isOpen = wasOpen; mode = wasMode;

print("");
print("=== chips: editAll and caretAtEnd — what a session arrives selected as ===");
/* Unlike everything above, these read ATTACHED per-field state (stateOf), not the pure
 * functions. dom_shim.js gives every element a parentNode so Chips.attach — called at
 * the page's own load, exactly as the real window calls it (see chipsAttach in index.js)
 * — actually succeeds under the harness rather than silently no-opping. */
params = SYNTHETIC; invalidateEntries();
document.activeElement = valueEl;

valueEl.value = "25 * 4";
valueEl.selectionStart = valueEl.selectionEnd = 0;
refreshChips();
Chips.editAll(valueEl);
expect("editAll selects the whole field, as TEXT to replace",
       valueEl.selectionStart + "-" + valueEl.selectionEnd, "0-6");

print("");
print("=== chips: mm is never shown on a number (bareUnits) ===");
function bareIn(u, m, text){
  unit = u; mode = m;
  valueEl.value = text;
  valueEl.selectionStart = valueEl.selectionEnd = text.length;
  refreshChips();
  return valueEl.value;
}
expect("a mm dimension's 25 mm is 25", bareIn("mm", "dim", "25 mm"), "25");
expect("...and inside an expression, spaced or not",
       bareIn("mm", "dim", "Width + 150 mm - 2mm"), "Width + 150 - 2");
expect("...the caret follows what it was after",
       valueEl.selectionStart + "-" + valueEl.selectionEnd, "15-15");
expect("a new parameter (no unit yet) loses it too", bareIn("", "param", "25 mm"), "25");
expect("another unit is left alone", bareIn("mm", "dim", "1 in"), "1 in");
expect("a cm parameter keeps its mm — there it changes the value",
       bareIn("cm", "editparam", "25 mm"), "25 mm");
expect("a unitless parameter keeps it too", bareIn("", "editparam", "25 mm"), "25 mm");
unit = "mm"; mode = "dim";

valueEl.value = "Width + 2";
valueEl.selectionStart = valueEl.selectionEnd = 0;
refreshChips();
Chips.caretAtEnd(valueEl);
expect("caretAtEnd parks the caret past the last item, nothing held",
       valueEl.selectionStart + "-" + valueEl.selectionEnd, "9-9");

valueEl.value = "";
valueEl.selectionStart = valueEl.selectionEnd = 0;
refreshChips();
Chips.caretAtEnd(valueEl);
expect("caretAtEnd on an empty field just parks the caret",
       valueEl.selectionStart + "-" + valueEl.selectionEnd, "0-0");

print("");
print("=== chips: Fusion's own words are offered too ===");
params = SYNTHETIC;
var co = suggest("co", "", "");
expect("cos is in the list", co.indexOf("cos") >= 0, true);
expect("...but below the document's own names", co[0], "CoverThickness");
expect("typing it in full puts it first", suggest("cos", "", "")[0], "cos");
expect("a function is accepted as a CALL", accept("cos", "", ""), "cos()");
expect("...with the caret waiting inside it", __els["value"].selectionStart, 4);

print("");
print("=== the Name field: picking an existing parameter retargets the session ===");
params = SYNTHETIC;
mode = "dim"; origName = ""; nameRequired = false; unit = "mm";
function nameSuggest(typed){
  attachAC(nameEl, fName);
  __els["pname"].value = typed;
  __els["pname"].selectionStart = __els["pname"].selectionEnd = typed.length;
  document.activeElement = nameEl;
  updateAC();
  return acItems.map(it => it.entry.label);
}
function nameAccept(typed){
  nameSuggest(typed);
  acceptAC();
}
expect("prefix-matched against real names", nameSuggest("Dep"), "Depth");
expect("nothing offered for an empty field", nameSuggest(""), "");
expect("picking one loads its expression into the value box",
       (nameAccept("Depth"), __els["value"].value), "600 mm");
expect("...and switches the session to editparam", mode, "editparam");
expect("...taking the name as its own", origName, "Depth");
expect("...and a name is no longer optional", nameRequired, true);
expect("the parameter being edited is not offered back to itself",
       (origName = "Depth", nameSuggest("Dep")), "");
origName = ""; mode = "dim"; nameRequired = false;
attachAC(valueEl, fValue);          // hand the suggestion list back to the value box

print("");
print("=== tags: what a name does not say ===");
/* Thickness carries no axis in its name, so it matches every situation and turns up on
   all three. Tagged, it belongs to one — and the same filters that read the convention
   read the tag, because analysisOf merges them into one answer. */
vocabSpec = VOCAB;
tagMap = {};
invalidateTags();
expect("untagged, it is offered on an X dimension",
       suggest("Thick", "X", "").indexOf("Thickness") >= 0, true);
tagMap = { Thickness: { axis: "Z" } };
invalidateTags();
expect("tagged Z, it is gone from the X list",
       suggest("Thick", "X", "").indexOf("Thickness") >= 0, false);
expect("...and still there on the Z one",
       suggest("Thick", "Z", "").indexOf("Thickness") >= 0, true);
/* An explicit tag beats what the name implies: the name is a guess about spelling, the
   tag is a person saying what this parameter is for. */
tagMap = { Depth: { axis: "X" } };
invalidateTags();
expect("an explicit tag overrides the name's own axis",
       suggest("Dep", "X", "").indexOf("Depth") >= 0, true);
tagMap = {};
invalidateTags();

print("");
print("=== the name stepper: building a new-convention name ===");
ParamNaming.setVocabulary(PARAM_NAMING_VOCAB);
paramNamingSpec = PARAM_NAMING_VOCAB;   // anchorInfo() reads this directly, like vocabSpec
paramNamingReady = true;

// A structured name's frame is ParamNaming.fits' to judge; the old engine read a
// Thickness as origin-framed and took it off every plane line measured from a face.
params = [{ name: "TopLevel_Materials_HIPS_Thickness", expression: "1 mm" },
          { name: "HipOffsetBack", expression: "5 mm" }];
browserShow = { TopLevel_Materials_HIPS_Thickness: { type: false, lv: ["Materials", "TopLevel"] } };
browserShowRev++;
expect("a structured Thickness is offered from a face",
       suggestFrom("Hi", "Y", "back").indexOf("TopLevel_Materials_HIPS_Thickness") >= 0, true);
browserShow = {}; browserShowRev++;

var STEP_PARAMS = [
  { name: "Boot_Width_Span_X", expression: "400 mm" },
  { name: "Boot_Ceiling_Inset_Back", expression: "10 mm" },
  // The two above end in their NAME word (Width, Ceiling), which is never a group;
  // these give each a group of the same word to drill into.
  { name: "Boot_Width_Rail_Span_Y", expression: "380 mm" },
  { name: "Boot_Ceiling_Lip_Inset_Back", expression: "12 mm" }
];
params = STEP_PARAMS;

// A brand-new name starts on Parent; A takes the default owner and opens its groups.
function openNew(s){ openSession(s); if (stepStage === "parent") pickStepIndex(0); }

// The groups typed as a New group straight under the owner — Space on the menu, then
// the text, levels comma-separated, and Enter with no suggestion highlighted
// (stepConfirmLevels). Starts from the owner, whatever path was browsed before.
function typeLevels(text){
  stepLevels = [stepLevels[0]];
  stepStartTyping();
  nameEl.value = text;
  return stepConfirmLevels();
}

// A brand-new 'param' session: the owner is the component, never typed.
openSession({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
expect("stepper engages for a brand-new parameter", stepperOn, true);
expect("starts on the parent step", [stepStage, STEP_TITLES[stepStage]], ["parent", "Parent"]);
expect("...offering the component, with Find on Space",
       [stepCandidates(), stepChoicesEl.innerHTML.indexOf('data-new="1"') >= 0], [["Boot"], true]);
pickStepIndex(0);
expect("A takes it: the component is the fixed first level", stepLevels.join(","), "Boot");
expect("...and the groups are next", [stepStage, STEP_TITLES[stepStage]], ["level", "Parameter groups"]);
expect("this component's own params suggest a branch",
       levelBranches().indexOf("Ceiling") >= 0, true);
expect("the owner dropdown defaults to the only component there is", stepOwnerName, "Boot");

// The feature step's suggestions: the owner's groups at the level being typed.
function levelSugg(text){
  nameEl.value = text;
  return stepLevelSuggestions().map(function (it){ return it.entry.target; });
}
expect("an empty field offers every group under the owner", levelSugg("").sort(), ["Ceiling", "Width"]);
expect("...filtered by what is typed", levelSugg("ce"), ["Ceiling"]);
params = STEP_PARAMS.concat([{ name: "Boot_Ceiling_Vent_Hole_Radius", expression: "4 mm" }]);
expect("after a comma it offers what is INSIDE the level before", levelSugg("Ceiling,"), ["Vent"]);
params = STEP_PARAMS;
acItems = [{ entry: { label: "Ceiling", target: "Ceiling", kind: "level" }, hits: [] }];
nameEl.value = "Cei";
stepTakeSuggestion(0);
expect("taking a suggestion puts it in with a comma and a space, staying on the step",
       [nameEl.value, stepStage], ["Ceiling, ", "level"]);
// The levels are read back trimmed, so that space is spacing and never part of a name.
expect("...and the level before it is still read back clean",
       stepLevelParts().done, ["Ceiling"]);
// A typed group that already exists, in any case or spacing, IS that group — never a
// second one beside it (existingLevels). Past the first new word the rest is as typed.
params = STEP_PARAMS.concat([{ name: "Boot_Ceiling_Vent_Hole_Radius", expression: "4 mm" }]);
typeLevels("ceiling");
expect("a typed group that exists goes into it, its own spelling kept", stepLevels, ["Boot", "Ceiling"]);
stepBack(); typeLevels("CEILING, vent, hole");
expect("...at every depth", stepLevels, ["Boot", "Ceiling", "Vent", "hole"]);
stepBack(); typeLevels("lid, ceiling");
expect("...but under a NEW group nothing is matched", stepLevels, ["Boot", "lid", "ceiling"]);
stepLevels = ["Boot"];
expect("after a lower-case existing group it offers what is inside it", levelSugg("ceiling,"), ["Vent"]);
params = STEP_PARAMS;
stepBack(); nameEl.value = ""; stepTyping = false;
expect("a level Fusion would reject is refused", typeLevels("Ceiling,2nd"), false);
expect("...and the step stays put", stepStage, "level");

// The owner can be any component up the chain.
openSession({ mode: "param", component: "Boot", componentPath: ["Car", "Body", "Boot"],
             symmetry: {}, nameRequired: true });
expect("the parent menu is the active component, then each level up",
       stepCandidates(), ["Boot", "Body", "Car"]);
pickStepIndex(1);
expect("S takes the one above", [stepOwnerName, stepLevels.join(","), stepStage], ["Body", "Body", "level"]);
stepBack();
expect("X from the owner's own groups is back to Parent", stepStage, "parent");
stepStartFinding();
nameEl.value = "ca";
expect("Space finds a component by name", stepCompSuggestions().map(function (it){ return it.entry.target; }), ["Car"]);
stepBack();
expect("...and backing out of it is the menu again", [stepFinding, stepStage], [false, "parent"]);
setOwner("Body");
typeLevels("Rear,Panel"); pickStepWord("Thickness");
expect("Thickness's axis is optional — offered, not skipped automatically", stepStage, "axis");
stepConfirmEmpty();
expect("...and a parent chosen instead owns the name, with no direction skipped",
       stepperName(), "Body_Rear_Panel_Thickness");
openSession({ mode: "editparam", name: "Car_Roof_Thickness",
             component: "Boot", componentPath: ["Car", "Body", "Boot"], symmetry: {}, nameRequired: true });
expect("an existing name's owner is its parent", stepOwnerName, "Car");
expect("...and heads the parent menu (the root: nothing above it)", ownerChain, ["Car"]);

// A component name with a space would otherwise seed level 0 with something
// NAME_RE rejects and stepBack() can't pop — see param_naming.js's identifier().
openNew({ mode: "param", component: "Front Panel", symmetry: {}, nameRequired: true });
expect("the fixed first level is squashed to a valid identifier",
       stepLevels.join(","), "FrontPanel");
typeLevels("Grip");
pickStepWord("Radius");
expect("...so the composed crumb is a name Fusion accepts",
       nameProblem(stepperName()), "");

// Back to the Boot fixture for the rest of this section.
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });

typeLevels("Gear");
expect("the typed segment becomes the levels", stepLevels.join(","), "Boot,Gear");
expect("...and moves straight on to the type", stepStage, "type");

pickStepWord("Inset");
expect("Inset has no axis of its own — anchor is next", stepStage, "anchor");
var anchors = stepCandidates();
expect("a non-symmetric axis offers both ends", anchors.indexOf("Front") >= 0, true);
pickStepWord("Back");
stepConfirmEmpty();   // Inset takes more than one anchor, so Enter/Space ends the step
expect("Inset+anchor is a complete name", stepStage, "done");
expect("composes back to the real Fusion name",
       stepperName(), "Boot_Gear_Inset_Back");

// Backspace peels one decision at a time, and the page it lands on loses its chip —
// whatever is picked there makes it again.
stepBack();
expect("backspace from 'done' goes back to anchor, the anchor gone",
       [stepStage, stepAnchors.length], ["anchor", 0]);
stepBack();
expect("backspace from anchor goes back to type, the type gone", [stepStage, stepType], ["type", null]);
stepBack();
expect("backspace from type goes back to the groups", stepStage, "level");
expect("...to the group menu, not the typing field", stepTyping, false);
expect("...the last group's chip gone", stepLevels.join(","), "Boot");
stepBack();
expect("back from the owner's groups is Parent, its chip gone",
       [stepStage, stepOwnerSet, stepCrumbEl.innerHTML], ["parent", false, ""]);

// The group menu: the owner's groups keyed, a key goes INTO one and the menu becomes
// its children; X backs out; Enter takes the group browsed to.
params = STEP_PARAMS.concat([{ name: "Boot_Ceiling_Vent_Hole_Radius", expression: "4 mm" }]);
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
expect("the menu opens on the owner's top-level groups", stepCandidates().slice().sort(), ["Ceiling", "Width"]);
expect("...keyed, with New group and Back (to Parent)",
       [stepChoicesEl.innerHTML.indexOf('data-new="1"') >= 0, stepChoicesEl.innerHTML.indexOf("data-back") >= 0],
       [true, true]);
stepConfirmBrowse();
expect("Enter with no group chosen stays put", stepStage, "level");
pickStepWord("Ceiling");
expect("a pick goes into the group", [stepLevels.join(","), stepStage], ["Boot,Ceiling", "level"]);
expect("...and the menu lists what is inside it", stepCandidates(), ["Vent"]);
expect("...with Back now offered", stepChoicesEl.innerHTML.indexOf("data-back") >= 0, true);
stepStartTyping();
expect("Space starts a new group, the menu stepping aside", [stepTyping, stepChoicesEl.innerHTML], [true, ""]);
expect("...its suggestions are what is under the path", levelSugg(""), ["Vent"]);
nameEl.value = "Lid, Hinge";
stepConfirmLevels();
expect("the new groups go under the path browsed to",
       [stepLevels.join(","), stepStage], ["Boot,Ceiling,Lid,Hinge", "type"]);
renderCrumb();
expect("...the last one typed is the NAME chip, the rest groups",
       (stepCrumbEl.innerHTML.match(/seg-level[^"]*"[^>]*>[^<]*/g) || []).map(function (c){
         return (c.indexOf("seg-name") >= 0 ? "name:" : "group:") + c.split(">").pop(); }),
       ["group:Ceiling", "group:Lid", "name:Hinge"]);
stepBack();
expect("X from the type drops the last group", stepLevels.join(","), "Boot,Ceiling,Lid");
stepBack();
expect("X backs out one group at a time", stepLevels.join(","), "Boot,Ceiling");
stepStartTyping(); stepBack();
expect("back out of typing is the menu again, path untouched",
       [stepTyping, stepLevels.join(",")], [false, "Boot,Ceiling"]);
stepConfirmBrowse();
expect("Enter on the menu takes the group browsed to", [stepLevels.join(","), stepStage], ["Boot,Ceiling", "type"]);
stepEditSegment("parent");
setOwner("Body");
expect("a new parent (its chip clicked) keeps the groups chosen, and opens the groups step",
       [stepLevels.join(","), stepStage], ["Body,Ceiling", "level"]);
stepConfirmBrowse();
expect("...Enter there resumes at what the name lacks", stepStage, "type");
pickStepWord("Thickness");
expect("...and the name is the new parent's", stepperName(), "Body_Ceiling_Thickness");

// The crumb's chips: click one, change that segment, keep the rest.
openSession({ mode: "editparam", name: "Boot_Ceiling_Vent_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
expect("an existing name opens whole", [stepStage, stepperName()], ["done", "Boot_Ceiling_Vent_Inset_Back"]);
stepEditSegment("level", 1);
expect("a group chip opens the groups beside it, the rest set aside",
       [stepStage, stepLevels.join(","), stepEdit.tail.join(",")], ["level", "Boot", "Ceiling,Vent"]);
pickStepWord("Ceiling");
expect("...the same group again puts it all back", [stepStage, stepperName()], ["done", "Boot_Ceiling_Vent_Inset_Back"]);
stepEditSegment("level", 1);
pickStepWord("Width");
expect("...a different one drops the groups after it", [stepStage, stepLevels.join(",")], ["level", "Boot,Width"]);
stepConfirmBrowse();
expect("...and keeps the type and anchor", [stepStage, stepperName()], ["done", "Boot_Width_Inset_Back"]);
stepEditSegment("level", 1);
stepBack();
expect("X while changing a chip leaves the name as it was", [stepStage, stepperName()], ["done", "Boot_Width_Inset_Back"]);
stepEditSegment("level", 2);
stepStartTyping(); nameEl.value = "Grille"; stepConfirmLevels();
expect("a New group in a chip's place is a new branch too", stepperName(), "Boot_Width_Grille_Inset_Back");
stepEditSegment("dir");
expect("the anchor chip opens the step titled Anchor", [stepStage, STEP_TITLES[stepStage]], ["anchor", "Anchor"]);
stepBack();
stepEditSegment("type");
pickStepWord("Outset");
expect("a type that takes a required axis instead asks for one, titled Axis",
       [stepStage, STEP_TITLES[stepStage], stepAnchors.length], ["axis", "Axis", 0]);
stepEditSegment("parent");
expect("the parent chip opens Parent", stepStage, "parent");
stepBack();
expect("...and X backs out of all of it", stepperName(), "Boot_Width_Grille_Inset_Back");
// Insert new: on a clicked parent or group chip, new groups go in right after it.
openSession({ mode: "editparam", name: "Boot_Ceiling_Vent_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
expect("nothing clicked: no Insert new", stepCanInsert(), false);
stepEditSegment("level", 1);
expect("a group chip offers Insert new", [stepCanInsert(), stepChoicesEl.innerHTML.includes("Insert new")], [true, true]);
stepStartInsert();
expect("...typing, the chip kept and the rest set aside",
       [stepTyping, stepLevels.join(","), stepEdit.tail.join(",")], [true, "Boot,Ceiling", "Vent"]);
nameEl.value = "Lid, Hinge"; stepConfirmLevels();
expect("...Enter puts them in after it, the rest kept", [stepStage, stepperName()],
       ["done", "Boot_Ceiling_Lid_Hinge_Vent_Inset_Back"]);
stepEditSegment("parent");
stepStartInsert();
expect("the parent chip inserts before the first group", [stepStage, stepLevels.join(",")], ["level", "Boot"]);
stepStopTyping();
expect("...abandoned, the parent step as it was",
       [stepStage, stepInsert, stepLevels.join(",")], ["parent", null, "Boot,Ceiling,Lid,Hinge,Vent"]);
stepStartInsert(); nameEl.value = "Top"; stepConfirmLevels();
expect("...taken, it heads the groups", stepperName(), "Boot_Top_Ceiling_Lid_Hinge_Vent_Inset_Back");
stepEditSegment("type");
expect("the type chip has no Insert new", stepCanInsert(), false);
stepBack();
stepEditSegment("level", 2);
stepStartInsert();
stepBack();
expect("X mid-insert leaves the name as it was", stepperName(), "Boot_Top_Ceiling_Lid_Hinge_Vent_Inset_Back");

// The Delete chord on a clicked chip takes that piece out — when a name is left.
openSession({ mode: "editparam", name: "Boot_Ceiling_Vent_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
expect("nothing clicked: Delete isn't the stepper's", stepDeleteChip(), false);
stepEditSegment("level", 1);
expect("a group chip deletes, the next one held", [stepDeleteChip(), stepStage,
       stepLevels.concat(stepEdit.tail).join(","), stepEdit.tail.join(",")], [true, "level", "Boot,Vent", "Vent"]);
expect("...but not the only one left: refused, the chip still open",
       [stepDeleteChip(), stepStage, stepEdit.tail.join(",")], [true, "level", "Vent"]);
stepBack();
expect("...and X leaves it as it was", stepperName(), "Boot_Vent_Inset_Back");
stepEditSegment("parent");
expect("the parent refuses", [stepDeleteChip(), stepStage], [true, "parent"]);
stepBack();
stepEditSegment("type");
expect("the type refuses", [stepDeleteChip(), stepStage], [true, "type"]);
stepBack();
stepEditSegment("dir", 0);
expect("a required anchor, the only one, refuses",
       [stepDeleteChip(), stepAnchors.join(",")], [true, "Back"]);
stepBack();
expect("...and the name is untouched", stepperName(), "Boot_Vent_Inset_Back");
// ⌘E on a clicked name / type / direction chip: said on the Browser's row, or not.
params = [{ name: "Boot_Ceiling_Vent_Inset_Back", expression: "1 mm" }];
openSession({ mode: "editparam", name: "Boot_Ceiling_Vent_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
expect("by default: name and type said, the direction not (no twin)",
       stepShown(), { name: true, type: true, dir: false });
expect("nothing clicked: ⌘E isn't the stepper's", stepToggleHide(), false);
stepEditSegment("dir", 0);
expect("a direction chip shown", [stepToggleHide(), stepShown().dir, stepStage], [true, true, "done"]);
stepEditSegment("type");
stepToggleHide();
stepEditSegment("level", 2);
stepToggleHide();
expect("the type and the name word hidden, the name itself untouched",
       [stepShow, stepperName()], [{ dir: true, type: false, name: false }, "Boot_Ceiling_Vent_Inset_Back"]);
stepEditSegment("level", 2);
stepToggleHide();
expect("⌘E again: back to the default, no override kept", stepShow, { dir: true, type: false });
stepEditSegment("level", 1);
expect("a group chip is hidden from typing, not from the row", [stepToggleHide(), stepShow, stepShown()],
       [true, { dir: true, type: false, lv: ["Ceiling"] }, { name: true, type: false, dir: true }]);
stepEditSegment("parent");
stepToggleHide();
expect("the parent chip too", stepShow.lv, ["Ceiling", "Boot"]);
stepEditSegment("level", 1);
stepToggleHide();
stepEditSegment("parent");
stepToggleHide();
expect("⌘E again on both: no lv kept", stepShow, { dir: true, type: false });
// Several chips at once: ⌘-click adds, Shift-click takes a range; the menu of choices
// goes, and ⌘E / Delete act on every one.
stepShow = {};
stepEditSegment("type");
stepMultiClick("level:2", false);
expect("⌘-click beside a held chip: both selected, the menu of choices gone",
       [stepMulti, stepEdit, /data-w=/.test(stepChoicesEl.innerHTML), /data-mhide/.test(stepChoicesEl.innerHTML)],
       [["type:", "level:2"], null, false, true]);
expect("...⌘E hides them both", [stepMultiHide(), stepShow], [true, { type: false, name: false }]);
expect("...and again shows them both", [stepMultiHide(), stepShow], [true, {}]);
stepMultiClick("level:2", false);
expect("⌘-click one out: the other is simply held", [stepMulti, stepStage], [[], "type"]);
stepBack();
stepEditSegment("parent");
stepMultiClick("dir:0", true);
expect("Shift-click: every chip between", stepMulti, ["parent:", "level:1", "level:2", "type:", "dir:0"]);
expect("...a mix: any said → hide all (groups from typing, the rest from the row)",
       [stepMultiHide(), stepShow], [true, { lv: ["Boot", "Ceiling"], name: false, type: false }]);
expect("...Delete with the parent in it refuses, nothing gone",
       [stepMultiDelete(), stepperName()], [true, "Boot_Ceiling_Vent_Inset_Back"]);
stepMultiKey({ key: "x" }, () => {});
expect("X drops the selection", [stepMulti, stepStage], [[], "done"]);
stepShow = {};
params = [{ name: "Boot_Ceiling_Hatch_Vent_Inset_Back", expression: "1 mm" }];
openSession({ mode: "editparam", name: "Boot_Ceiling_Hatch_Vent_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
stepMultiClick("level:1", false);
stepMultiClick("level:2", false);
expect("Delete on two groups: both go", [stepMultiDelete(), stepperName(), stepMulti],
       [true, "Boot_Vent_Inset_Back", []]);
params = [{ name: "Boot_Ceiling_Vent_Inset_Back", expression: "1 mm" }];
openSession({ mode: "editparam", name: "Boot_Ceiling_Vent_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
stepShow = { dir: true, type: false };
// The value box: hidden words leave the reading, and the real name no longer matches.
browserShow = { "Boot_Ceiling_Vent_Inset_Back": { lv: ["Boot", "Ceiling"] } }; browserShowRev++;
invalidateEntries();
{
  const e = entries().find(x => x.target === "Boot_Ceiling_Vent_Inset_Back");
  expect("hidden from typing: the list drops the group, keeps the component", e.shown,
         "Boot - Vent - Backset");
  expect("...typed by its own name", !!score(e, "vent"), true);
  expect("...and by its component's", !!score(e, "bootvent"), true);
  expect("...never by the hidden group's", score(e, "ceiling"), null);
  expect("...the chip says just its own", chipLabel("Boot_Ceiling_Vent_Inset_Back"), "Vent - Backset");
}
browserShow = {}; browserShowRev++; invalidateEntries();
// The component heads every reading in the list — once, and never "Top Level".
{
  const saved = params;
  params = params.concat([{ name: "TopLevel_Lid_Hinge_Span_X", expression: "1 mm" },
                          { name: "Boot_BootCeiling_Hinge_Span_X", expression: "1 mm" }]);
  browserShow = { "TopLevel_Lid_Hinge_Span_X": { lv: ["TopLevel"] } }; browserShowRev++;
  invalidateEntries();
  const shown = n => entries().find(x => x.target === n).shown;
  expect("top level: no owner word, hidden or not", [shown("TopLevel_Lid_Hinge_Span_X"),
         chipLabel("TopLevel_Lid_Hinge_Span_X")], ["Lid - Hinge - Width", "Lid - Hinge - Width"]);
  expect("a group already starting with the component: not said twice",
         shown("Boot_BootCeiling_Hinge_Span_X"), "Boot - Boot Ceiling - Hinge - Width");
  params = saved; browserShow = {}; browserShowRev++; invalidateEntries();
}
// A bounds parameter, its component and Bounds hidden: in the list AND on its chip it is
// "<component> - <face>", typed that way, one per component; a top-level one just "<face>".
{
  const saved = params;
  const bnd = (o, face, ax) => ({ name: o + "_Bounds_" + face + "_Offset_" + ax,
    expression: "1 mm", unit: "mm", comment: "Bounds of " + o + " [x]" });
  params = params.concat([bnd("Nose", "Right", "X"), bnd("BootDoor", "Right", "X"),
                          bnd("Nose", "Back", "Y"), bnd("TopLevel", "Left", "X")]);
  browserShow = {};
  for (const p of params) if (/_Bounds_/.test(p.name))
    browserShow[p.name] = { type: false, dir: false, lv: [p.name.split("_")[0], "Bounds"] };
  browserShowRev++; invalidateEntries();
  const rows = () => acItems.map(it => it.entry.shown);
  suggest("right", "X", "");
  expect("bounds: every component's face offered, by component", rows(),
         ["Nose - Right", "Boot Door - Right"]);
  suggest("bootdoorri", "X", "");
  expect("...typed as component + face", rows(), ["Boot Door - Right"]);
  suggest("noseback", "X", "");
  expect("...still filtered by axis", rows(), []);
  expect("...the chip says the component too", chipLabel("Nose_Bounds_Right_Offset_X"), "Nose - Right");
  expect("...but not the top level's", [chipLabel("TopLevel_Bounds_Left_Offset_X"),
         (suggest("left", "X", ""), rows())], ["Left", ["Left"]]);
  params = saved; browserShow = {}; browserShowRev++; invalidateEntries();
}
stepEditSegment("dir", 0);
stepCancelEdit();
params = params.concat([{ name: "Boot_Ceiling_Vent_Inset_Front", expression: "1 mm" }]);
openSession({ mode: "editparam", name: "Boot_Ceiling_Vent_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
expect("a twin at the same chain and type: the direction said by itself",
       stepShown().dir, true);
stepEditSegment("dir", 0);
stepToggleHide();
expect("...and ⌘E hides it anyway", [stepShow, stepShown().dir], [{ dir: false }, false]);
openSession({ mode: "editparam", name: "Boot_Ceiling_Vent_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
expect("a fresh session starts from nothing (the Browser's copy comes by fetch)", stepShow, {});
// ⌘R on a clicked group chip renames the group in every parameter under it.
params = [{ name: "Boot_Ceiling_Vent_Inset_Back", expression: "1 mm" },
          { name: "Boot_Ceiling_Radius", expression: "2 mm" },
          { name: "Boot_Floor_Radius", expression: "3 mm" },
          { name: "Lid_Ceiling_Radius", expression: "4 mm" }];
expect("a group rename takes every parameter under it, and only those",
       groupRenamePlan(["Boot"], "Ceiling", "Roof"),
       [{ from: "Boot_Ceiling_Vent_Inset_Back", to: "Boot_Roof_Vent_Inset_Back" },
        { from: "Boot_Ceiling_Radius", to: "Boot_Roof_Radius" }]);
expect("...and refuses one that would collide",
       typeof groupRenamePlan(["Boot"], "Ceiling", "Floor"), "string");
openSession({ mode: "editparam", name: "Boot_Ceiling_Vent_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
stepEditSegment("level", 1);
stepStartRename();
expect("the chord opens the group's word for renaming", [nameEl.value, stepRenaming.depth], ["Ceiling", 1]);
nameEl.value = "Roof";
stepApplyRename();
expect("...and the name being edited follows it",
       [stepStage, stepperName(), origName, pendingRetarget],
       ["done", "Boot_Roof_Vent_Inset_Back", "Boot_Roof_Vent_Inset_Back", "Boot_Roof_Vent_Inset_Back"]);
pendingRetarget = "";
params = STEP_PARAMS;
openSession({ mode: "editparam", name: "Boot_Width_Grille_Inset_Back", component: "Boot",
             symmetry: {}, nameRequired: true });
expect("the crumb draws one chip per segment",
       (stepCrumbEl.innerHTML.match(/class="stepchip/g) || []).length, 5);
params = STEP_PARAMS;

// A type with only one candidate for the session's unit skips straight past it, and a
// type that takes neither anchor nor axis (Radius) settles straight to 'done'.
openNew({ mode: "dim", component: "Lid", symmetry: {}, unit: "deg", nameRequired: true });
typeLevels("Pivot");
expect("a 'deg' session's only matching type auto-selects", stepType, "Angle");
expect("Angle takes neither anchor nor axis — settles straight to done", stepStage, "done");
expect("composes with no anchor/axis tail", stepperName(), "Lid_Pivot_Angle");

openNew({ mode: "param", component: "Handle", symmetry: {}, nameRequired: true });
typeLevels("Grip");
pickStepWord("Radius");
expect("Radius has no anchor or axis — settles straight to done", stepStage, "done");
expect("composes with no tail", stepperName(), "Handle_Grip_Radius");
stepBack();
expect("backspace from a no-tail type's 'done' skips straight to type", stepStage, "type");
expect("...leaving the level it was typed under alone", stepLevels.join(","), "Handle,Grip");

// An optional axis that was explicitly skipped is still reachable by backspace — it
// was a real (if empty) decision, not nothing asked at all.
openNew({ mode: "param", component: "Shelf", symmetry: {}, nameRequired: true });
typeLevels("Rail");
pickStepWord("Offset");
expect("Offset's axis is optional — offered, not skipped automatically", stepStage, "axis");
stepConfirmEmpty();
expect("Space/Enter with nothing typed skips it", stepStage, "done");
expect("...with no axis in the composed name", stepperName(), "Shelf_Rail_Offset");
stepBack();
expect("backspace from an explicitly-skipped optional axis returns to it", stepStage, "axis");

// An exempt word (fusion_ui/param_exempt.json) typed alone adds "None" to the type
// step; taking it names the parameter just that word, in the list's own spelling.
paramExemptWords = ["Top", "Width"];
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Gear");
expect("a non-exempt feature is not offered None", stepCandidates().indexOf("None"), -1);
stepBack();
typeLevels("width");
expect("an exempt feature is offered None, last", stepCandidates().slice(-1)[0], "None");
expect("...after every ordinary type", stepCandidates().indexOf("Span") >= 0, true);
pickStepWord("None");
expect("None finishes the name", stepStage, "done");
expect("...as just the word, list spelling, no owner", stepperName(), "Width");
expect("...and it is a valid, free name", nameProblem(stepperName()), "");
stepBack();
expect("backspace from None goes back to the type step", [stepStage, stepperName()], ["type", ""]);
pickStepWord("Thickness");
stepConfirmEmpty();   // Thickness's axis is optional now — Enter/Space skips it
expect("an exempt word still composes normally with a type (into the existing Width group)",
       stepperName(), "Boot_Width_Thickness");
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Top,Lid");
expect("an exempt word as one of several levels is not offered None",
       stepCandidates().indexOf("None"), -1);
openNew({ mode: "dim", component: "Lid", symmetry: {}, unit: "deg", nameRequired: true });
typeLevels("Top");
expect("a one-type unit no longer auto-picks when None is on offer", stepStage, "type");
paramExemptWords = [];

// A 'dim' session's own axis/end is offered first (and so takes the first key).
openNew({ mode: "dim", component: "Boot", symmetry: {}, axis: "Y", end: "high" });
typeLevels("Latch");
pickStepWord("Inset");
expect("the dimension's own anchor (Back = Y/high) is offered first",
       stepCandidates()[0], "Back");
stepConfirmEmpty();
expect("Enter/Space with nothing typed takes that default", stepAnchors.join(","), "Back");
expect("composes with the defaulted anchor", stepperName(), "Boot_Latch_Inset_Back");

// A symmetric axis drops the low-end anchor word.
openNew({ mode: "param", component: "Boot", symmetry: { Y: true }, nameRequired: true });
typeLevels("Shelf");
pickStepWord("Inset");
expect("a symmetric Y axis drops Front", stepCandidates().indexOf("Front"), -1);
expect("...but keeps Back", stepCandidates().indexOf("Back") >= 0, true);

// editparam: a name that already parses opens prefilled; one that doesn't falls back
// to the plain field, unchanged from before the stepper existed.
origName = "Boot_Ceiling_Inset_Back";
openSession({ mode: "editparam", name: "Boot_Ceiling_Inset_Back",
             component: "Boot", symmetry: {}, nameRequired: true });
expect("an existing structured name opens the stepper prefilled", stepperOn, true);
expect("...at the done stage", stepStage, "done");
expect("...with its real levels", stepLevels.join(","), "Boot,Ceiling");
expect("...and its anchor", stepAnchors.join(","), "Back");

openSession({ mode: "editparam", name: "EdgeOffsetX",
             component: "Boot", symmetry: {}, nameRequired: true });
expect("an old-convention name opens the stepper too", stepperOn, true);
expect("...as one chip holding the whole name", stepLegacy, "EdgeOffsetX");
expect("...which stands as the name", stepperName(), "EdgeOffsetX");
stepLegacyRename(true);
expect("clicking the chip starts the naming at Parent", stepStage, "parent");
expect("...with the old name gone from the result", stepperName(), "");
stepBack();
expect("X on Parent puts the old name back", stepperName(), "EdgeOffsetX");
stepBack();
expect("deleting the chip starts the naming at Parent", stepStage, "parent");
stepBack();
expect("...with no way back to the old name", stepLegacy, "");
pickStepIndex(0);
expect("...and naming goes on to the groups", stepStage, "level");

// Filed in the Browser: renaming it starts with its component and groups as chips.
openSession({ mode: "editparam", name: "EdgeOffsetX",
             component: "Boot", componentPath: ["Top Level", "Boot"],
             filed: { owner: "Top Level", groups: ["Boot Ceiling", "Lid"] },
             symmetry: {}, nameRequired: true });
stepLegacyRename(true);
expect("a filed old name's rename opens on the group menu", stepStage, "level");
expect("...its owner and groups already chips", stepLevels.join(","), "TopLevel,BootCeiling,Lid");
expect("...the owner chip showing", stepOwnerSet, true);
stepBack();
expect("X takes the last group chip off", stepLevels.join(","), "TopLevel,BootCeiling");

// ── A type that takes more than one direction ──
// "X and Y both work": the step stays open, each pick toggles, Enter/Space finishes,
// and every chosen direction ends up in the name.
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Frame");
pickStepWord("Span");
expect("Span's axis step is open", stepStage, "axis");
pickStepWord("X");
expect("a multi step stays open after the first pick", stepStage, "axis");
expect("...holding what was picked", stepAxes.join(","), "X");
expect("...and saying so in the crumb", stepCrumbText(), "Boot, Frame - Span - X");
pickStepWord("Y");
expect("a second direction goes in beside the first", stepAxes.join(","), "X,Y");
pickStepWord("Y");
expect("...and the same key takes it back out", stepAxes.join(","), "X");
pickStepWord("Y");
stepConfirmEmpty();
expect("Enter/Space finishes the step", stepStage, "done");
expect("both directions are in the name", stepperName(), "Boot_Frame_Span_X_Y");
expect("...and it parses back to the two of them",
       ParamNaming.parse(stepperName()).axes.join(","), "X,Y");
stepBack();
expect("backspace reopens the step", stepStage, "axis");
expect("...its chips gone, to be picked again", stepAxes.join(","), "");
pickStepWord("X"); pickStepWord("Y");
stepBack();
expect("backspace mid-step takes the last direction out", stepAxes.join(","), "X");
stepBack();
expect("...and again", stepAxes.join(","), "");
stepBack();
expect("an empty multi step backs out to the type", stepStage, "type");

// The same for anchors, where the two can be on different axes.
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Panel");
pickStepWord("Inset");
pickStepWord("Left");
pickStepWord("Top");
stepConfirmEmpty();
expect("two anchors compose in the order they were picked",
       stepperName(), "Boot_Panel_Inset_Left_Top");
expect("...and the name is on both their axes",
       ParamNaming.parse(stepperName()).axes.join(","), "X,Z");

// A one-direction type is untouched: the first pick still finishes the name.
openNew({ mode: "param", component: "Rack", symmetry: {}, nameRequired: true });
typeLevels("Slot");
pickStepWord("Count");
pickStepWord("X");
expect("Count takes one axis, so picking one finishes it", stepStage, "done");
expect("...composing with just that one", stepperName(), "Rack_Slot_Count_X");

// ── "Make as group" ──
// The type step's own toggle: G, or a click on its own row, flips it — off by default,
// and it never advances the stage by itself. On, the TYPE becomes a group level of its
// own, spliced in before the trailing name (stepFinalLevels), and every existing
// sibling under the same base path and type moves into it too, on commit.
params = [{ name: "Boot_Shaft_Entry_Span_Y", expression: "30 mm" }];
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Shaft,Entry");
expect("off by default", stepTypeGroup, false);
stepToggleGroup();
expect("G/a click flips it, and stays on the type step", [stepTypeGroup, stepStage], [true, "type"]);
stepToggleGroup();
expect("flips back off the same way", stepTypeGroup, false);
stepToggleGroup();
pickStepWord("Span");
expect("picking the type after toggling carries it forward", stepTypeGroup, true);
pickStepWord("X");
stepConfirmEmpty();   // Span's axis is a multi step: Enter with something chosen ends it
expect("the type is spliced in before the name, not appended after it",
       stepperName(), "Boot_Shaft_Span_Entry_Span_X");
expect("the existing sibling (same base path and type) is still ungrouped until commit",
       params[0].name, "Boot_Shaft_Entry_Span_Y");
valueEl.value = "5";   // commit() refuses an empty expression before it ever reaches this
commit();
expect("...moves into the group on commit, same posts a group rename makes",
       params[0].name, "Boot_Shaft_Span_Entry_Span_Y");
openSession({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });   // resets `committing`

// A one-entry path (owner + Width alone, always a group already) simply nests the type
// group under it — appended, since there's no trailing name to protect.
params = [];
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Width");
stepToggleGroup();
pickStepWord("Thickness");
stepConfirmEmpty();   // Thickness's axis is optional — Enter with nothing skips it
expect("nests under the bare group rather than losing it",
       stepperName(), "Boot_Width_Thickness_Thickness");
openSession({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });

// An 'editparam' session on an already type-grouped name opens with the toggle already
// on, the type level pulled back out of the levels it stands among.
openSession({ mode: "editparam", name: "Boot_Shaft_Span_Entry_Span_X",
             component: "Boot", symmetry: {}, nameRequired: true });
expect("the toggle opens on for an already-grouped name", stepTypeGroup, true);
expect("...the type level pulled back out of stepLevels", stepLevels.join(","), "Boot,Shaft,Entry");
expect("...and composes back to the exact same name", stepperName(), "Boot_Shaft_Span_Entry_Span_X");

// ── "Make name" / "Make group" on the last group chip ──
// G on the clicked LAST level flips it between the parameter's own name and a group,
// pinned with a double underscore (ParamNaming PIN); commit takes every other parameter
// at that place the same way (lastPinPlan).
// A name, made a group: the one already naming itself Curve goes into it.
params = [{ name: "Boot_Shelf__Curve_Radius", expression: "3 mm" }];
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Shelf,Curve");
pickStepWord("Thickness");
stepConfirmEmpty();
expect("a place already pinned as a name is inherited", [stepLastIs(), stepperName()],
       ["name", "Boot_Shelf__Curve_Thickness"]);
stepEditSegment("level", 1);
expect("G is offered only on the last group chip", stepCanFlipLast(), false);
stepCancelEdit();
stepEditSegment("level", 2);
expect("...which it is on", stepCanFlipLast(), true);
stepFlipLast();
expect("flipped: a group, the name whole again", [stepStage, stepperName()],
       ["done", "Boot_Shelf_Curve__Thickness"]);
valueEl.value = "5";
commit();
expect("...and the other one at that place goes into the group", params[0].name,
       "Boot_Shelf_Curve__Radius");
openSession({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });

// A group, made a name: the one directly in it takes it as its name, and a row in it
// with a name of its own takes both, joined. The group is gone.
params = [{ name: "Boot_Shelf_Curve_Radius", expression: "3 mm" },
          { name: "Boot_Shelf_Curve_Lip_Thickness", expression: "1 mm" }];
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Shelf,Curve");
pickStepWord("Span"); pickStepWord("X"); stepConfirmEmpty();
expect("shared, the last level is a group by default", [stepLastIs(), crumbNameIndex()], ["group", -1]);
stepEditSegment("level", 2);
stepFlipLast();
expect("flipped: the name, pinned", stepperName(), "Boot_Shelf__Curve_Span_X");
valueEl.value = "5";
commit();
expect("...the others follow", params.map(p => p.name).join(" "),
       "Boot_Shelf__Curve_Radius Boot_Shelf__CurveLip_Thickness");
openSession({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });

// A group with a group inside it can't become a name: refused whole.
params = [{ name: "Boot_Shelf_Curve_Radius", expression: "3 mm" },
          { name: "Boot_Shelf_Curve_Deep_Lip_Thickness", expression: "1 mm" }];
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Shelf,Curve");
pickStepWord("Span"); pickStepWord("X"); stepConfirmEmpty();
stepEditSegment("level", 2);
stepFlipLast();
valueEl.value = "5";
commit();
expect("refused, nothing renamed", [committing, params[0].name], [false, "Boot_Shelf_Curve_Radius"]);
openSession({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });

// An edited pinned name opens with its pin, and composes back to itself.
params = [{ name: "Boot_Shelf_Curve__Radius", expression: "3 mm" }];
openSession({ mode: "editparam", name: "Boot_Shelf_Curve__Radius",
             component: "Boot", symmetry: {}, nameRequired: true });
expect("an edited pin round-trips", [stepLastIs(), stepperName()], ["group", "Boot_Shelf_Curve__Radius"]);
params = STEP_PARAMS;

// ── The step title's error, on a required step Enter couldn't finish ──
openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
typeLevels("Frame");
stepConfirmEmpty();   // nothing picked yet — the type step has no default and no skip
expect("Enter on the type step, nothing picked, says so on the title line",
       stepErrEl.textContent, "Pick a measurement type.");
pickStepWord("Outset");   // required axis, more than one candidate — waits for a pick
stepConfirmEmpty();
expect("Enter on a required axis with nothing chosen names THAT step instead",
       stepErrEl.textContent, "Pick an axis.");
clearStepTitleError();
expect("cleared — the next key does this for real, in the keydown handler",
       stepErrEl.textContent, "");

// ── Which types are offered, and which parameters are suggested back ──
// Both go through param_naming.json's `suggest` block (ParamNaming.suggests / fits).
// Offset is for a dimension measured from the origin, Inset for one measured in from
// a bound, and a dimension that doesn't know its datum is offered both.
openNew({ mode: "dim", component: "Boot", symmetry: {}, unit: "mm", axis: "Y", end: "high" });
datum = "zero";
typeLevels("Rail");
expect("from the origin, Offset is on the type step", stepCandidates().indexOf("Offset") >= 0, true);
expect("...and Inset is not", stepCandidates().indexOf("Inset"), -1);
datum = "back";
expect("in from a bound, the two swap over",
       [stepCandidates().indexOf("Inset") >= 0, stepCandidates().indexOf("Offset")], [true, -1]);
datum = "";
expect("a dimension with no known datum is offered both",
       [stepCandidates().indexOf("Inset") >= 0, stepCandidates().indexOf("Offset") >= 0],
       [true, true]);
expect("Span is offered whatever the datum", stepCandidates().indexOf("Span") >= 0, true);
stepWide = true;
datum = "zero";
expect("Option held offers every type regardless", stepCandidates().indexOf("Inset") >= 0, true);
stepWide = false;
datum = "";

// The situational suggestion filter (paramNamingSituationBad, called from
// conflictsWithSituation): Span filters by axis like any other directional type now
// (its OWN axis is optional, but a name that gives one is held to it); Inset only in
// its own anchor's situation; a type with no axis at all (Radius) never conflicts.
axis = "Y"; end = "";
expect("Span on an axis it doesn't share is filtered out",
       paramNamingSituationBad(ParamNaming.parse("Boot_Width_Span_X")), true);
axis = "X"; end = "";
expect("...and not on the one it names",
       paramNamingSituationBad(ParamNaming.parse("Boot_Width_Span_X")), false);
axis = "Y"; end = "high";
expect("Inset is offered when the anchor's own axis+end match",
       paramNamingSituationBad(ParamNaming.parse("Boot_Ceiling_Inset_Back")), false);
axis = "Y"; end = "low";
expect("...and filtered out on the wrong end of the same axis",
       paramNamingSituationBad(ParamNaming.parse("Boot_Ceiling_Inset_Back")), true);
axis = "X"; end = "";
expect("Radius has no situation to conflict with",
       paramNamingSituationBad(ParamNaming.parse("Boot_Handle_Radius")), false);
// A name on two axes is suggested on either of them.
axis = "Z"; end = "";
expect("a two-axis span is filtered when neither axis is shared",
       paramNamingSituationBad(ParamNaming.parse("Boot_Frame_Span_X_Y")), true);
axis = "Y"; end = "";
expect("...but offered when one of them is",
       paramNamingSituationBad(ParamNaming.parse("Boot_Frame_Span_X_Y")), false);
axis = "Z"; end = "";
expect("an Offset on X and Y is offered on Y",
       paramNamingSituationBad(ParamNaming.parse("Boot_Rail_Offset_X_Y")), true);
axis = "Y";
expect("...and not on Z",
       paramNamingSituationBad(ParamNaming.parse("Boot_Rail_Offset_X_Y")), false);
axis = "Y"; end = "low";
expect("two anchors cover both ends",
       paramNamingSituationBad(ParamNaming.parse("Boot_Panel_Inset_Front_Back")), false);
axis = "X"; end = "";

// The wiring end to end: a session opened with the add-in's own axes/ends (a diagonal
// dimension, no single nearest axis) reaches namingSituation() through openSession.
openSession({ mode: "dim", component: "Boot", symmetry: {}, unit: "mm",
             axis: "", end: "", axes: ["X", "Y"], ends: { X: "high", Y: "low" } });
expect("a diagonal dimension's own axes/ends reach namingSituation()",
       paramNamingSituationBad(ParamNaming.parse("Boot_Width_Span_X")), false);
expect("...and an axis outside its 45° pool is still filtered",
       paramNamingSituationBad(ParamNaming.parse("Boot_Width_Span_Z")), true);
openSession({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });

print("");
print("=== pasting a real parameter's name inserts it as a chip, not text ===");
params = STEP_PARAMS;
valueEl.value = "";
valueEl.selectionStart = valueEl.selectionEnd = 0;
expect("a real name is accepted and chipped", pasteAsChip("Boot_Width_Span_X"), true);
expect("the field now holds it", valueEl.value, "Boot_Width_Span_X");
expect("...as a SEALED item (chipAtCaret finds one, one caret step back)",
       !!Chips.chipAtCaret(valueEl, valueEl.value.length - 1), true);
valueEl.value = "";
valueEl.selectionStart = valueEl.selectionEnd = 0;
expect("text that isn't a real name pastes as nothing special",
       pasteAsChip("NotAnExistingParameter"), false);
expect("whitespace around a real name still matches",
       pasteAsChip("  Boot_Width_Span_X  \n"), true);

origName = ""; mode = "dim"; nameRequired = false; unit = ""; axis = ""; end = "";
stepperOn = false;
params = SYNTHETIC;
invalidateEntries();

print("");
print("=== the Extrude panel's keydown chord canonicalises the same way a bound one is stored ===");
function fakeKeydown(code, mods){
  mods = mods || {};
  return { code: code, ctrlKey: !!mods.ctrl, altKey: !!mods.alt,
           shiftKey: !!mods.shift, metaKey: !!mods.cmd };
}
expect("a bare letter", xChordFrom(fakeKeydown("KeyE")), "e");
expect("mods spelled in fusion.py::join order regardless of which fired first",
       xChordFrom(fakeKeydown("KeyE", { cmd: true, ctrl: true, shift: true, alt: true })),
       "ctrl+alt+shift+cmd+e");
expect("two mods still land in that order, not event order",
       xChordFrom(fakeKeydown("KeyE", { cmd: true, ctrl: true })), "ctrl+cmd+e");
expect("a digit", xChordFrom(fakeKeydown("Digit1")), "1");
expect("a punctuation key from X_CODE", xChordFrom(fakeKeydown("Period")), ".");
expect("a bare modifier has no chord", xChordFrom(fakeKeydown("ShiftLeft")), null);

print("");
print("=== the command-search palette's ranker ===");
var CMDS = [
  { id: "SmartDimensionCmd",       name: "Smart Dimension",             group: "SKETCH",  kind: "command" },
  { id: "ToggleSketchVisibility",  name: "Toggle Sketch Visibility",    group: "SKETCH",  kind: "action" },
  { id: "FusionSheetMetalFlange",  name: "Flange",                      group: "SHEET METAL", kind: "command" },
  { id: "SmartExtrudeCmd",         name: "Extrude",                     group: "My add-ins", kind: "addin" },
  { id: "SomeMissingCmd",          name: "SomeMissingCmd",              group: "Not loaded", kind: "missing" },
  { id: "CommandSearchCmd",        name: "Command Search",              group: "My add-ins", kind: "action" },
];
function ids(list){ return list.map(function(c){ return c.id; }); }

expect("a substring anywhere in the name matches",
       ids(searchRank("dim", CMDS, [])).indexOf("SmartDimensionCmd") !== -1, true);
expect("a mid-word substring still matches (not just a prefix or a word start)",
       ids(searchRank("lang", CMDS, [])).indexOf("FusionSheetMetalFlange") !== -1, true);
expect("every word of a multi-word query must hit",
       ids(searchRank("sketch vis", CMDS, [])), ["ToggleSketchVisibility"]);
expect("a start-of-name hit outranks a mid-word hit",
       searchRank("s", CMDS, [])[0].id, "SmartDimensionCmd");
expect("kind:missing is never offered",
       ids(searchRank("missing", CMDS, [])), []);
expect("the palette's own command never offers itself",
       ids(searchRank("command", CMDS, [])).indexOf("CommandSearchCmd"), -1);
expect("an empty query lists nothing, even with recents",
       ids(searchRank("", CMDS, ["SmartExtrudeCmd", "SmartDimensionCmd"])), []);
expect("an empty query and no recents lists nothing",
       ids(searchRank("", CMDS, [])), []);
expect("recency breaks a tie between two equally well-placed hits",
       searchRank("flange", [
         { id: "FlangeA", name: "Flange A", group: "X", kind: "command" },
         { id: "FlangeB", name: "Flange B", group: "X", kind: "command" },
       ], ["FlangeB"])[0].id, "FlangeB");

print("");
print("=== the ranker's environment filter and ours-first dedup ===");
var ENV_CMDS = [
  { id: "SketchLineCmd",      name: "Line",     group: "SKETCH",      kind: "command" },
  { id: "SolidExtrudeCmd",    name: "Extrude",  group: "SOLID",       kind: "command" },
  { id: "SheetExtrudeCmd",    name: "Extrude",  group: "SHEET METAL", kind: "command" },
  { id: "SurfaceExtrudeCmd",  name: "Extrude",  group: "SURFACE",     kind: "command" },
  { id: "NativeFlangeCmd",    name: "Flange",   group: "SHEET METAL", kind: "command" },
  { id: "OurFlangeAction",    name: "Flange",   group: "My add-ins",  kind: "action" },
];
expect("a SKETCH command is offered in a sketch",
       ids(searchRank("line", ENV_CMDS, [], { env: "sketch" })), ["SketchLineCmd"]);
expect("...and dropped outside one",
       ids(searchRank("line", ENV_CMDS, [], { env: "viewport" })), []);
expect("a SOLID/SURFACE/SHEET METAL command is dropped in a sketch",
       ids(searchRank("extrude", ENV_CMDS, [], { env: "sketch" })), []);
expect("...and offered in the viewport",
       ids(searchRank("extrude", ENV_CMDS, [], { env: "viewport" })).length, 3);
expect("an unknown env ('') fails open for both groups",
       ids(searchRank("line", ENV_CMDS, [], { env: "" })).indexOf("SketchLineCmd") !== -1
       && ids(searchRank("extrude", ENV_CMDS, [], { env: "" })).length === 3, true);
expect("SOLID, SHEET METAL, SURFACE all stay for the same name (a genuinely different "
       + "command per environment, not a duplicate) and sort in that order",
       ids(searchRank("extrude", ENV_CMDS, [], { env: "viewport" })),
       ["SolidExtrudeCmd", "SheetExtrudeCmd", "SurfaceExtrudeCmd"]);
expect("an ordinary same-name collision (native + ours, one environment) keeps only ours",
       ids(searchRank("flange", ENV_CMDS, [], { env: "viewport" })), ["OurFlangeAction"]);

print("");
print("=== the ranker's Show In filter ===");
var SHOW_CMDS = [
  { id: "Everywhere",  name: "Everywhere Flange" },
  { id: "Hidden",      name: "Hidden Flange" },
  { id: "SketchOnly",  name: "Sketch Only Flange" },
];
var SHOW_STORE = { Hidden: [], SketchOnly: ["sketch"] };
expect("no entry for the id is shown, in any env",
       ids(searchRank("flange", SHOW_CMDS, [], { env: "viewport", show: SHOW_STORE }))
         .indexOf("Everywhere") !== -1, true);
expect("an empty list hides it even with no env known",
       ids(searchRank("flange", SHOW_CMDS, [], { env: "", show: SHOW_STORE }))
         .indexOf("Hidden") !== -1, false);
expect("listed for one env, shown there",
       ids(searchRank("flange", SHOW_CMDS, [], { env: "sketch", show: SHOW_STORE }))
         .indexOf("SketchOnly") !== -1, true);
expect("listed for one env, hidden in another",
       ids(searchRank("flange", SHOW_CMDS, [], { env: "viewport", show: SHOW_STORE }))
         .indexOf("SketchOnly") !== -1, false);
expect("an unknown env ('') fails open even for a scoped entry",
       ids(searchRank("flange", SHOW_CMDS, [], { env: "", show: SHOW_STORE }))
         .indexOf("SketchOnly") !== -1, true);
expect("showHidden brings back everything the store hides",
       ids(searchRank("flange", SHOW_CMDS, [],
                      { env: "viewport", show: SHOW_STORE, showHidden: true })).length, 3);
expect("a search-only rename is matched under the new name, not the old",
       [ids(searchRank("banana", SHOW_CMDS, [], { names: { Everywhere: "Banana" } })),
        ids(searchRank("everywhere", SHOW_CMDS, [], { names: { Everywhere: "Banana" } }))],
       [["Everywhere"], []]);

print("");
print("=== the ranker's pin sort ===");
var PIN_CMDS = [
  { id: "FaceCmd",     name: "Face",           group: "SOLID", kind: "command" },
  { id: "FilletCmd",   name: "Fillet",         group: "SOLID", kind: "command" },
  { id: "FlangeCmd",   name: "Flange",         group: "SHEET METAL", kind: "command" },
];
expect("a pinned mid-alphabet match still sorts ahead of an unpinned better-ranked one",
       ids(searchRank("f", PIN_CMDS, [], { pins: { FilletCmd: true } }))[0], "FilletCmd");
expect("...and the rest keep their ordinary relative order behind it",
       ids(searchRank("f", PIN_CMDS, [], { pins: { FilletCmd: true } })),
       ["FilletCmd", "FaceCmd", "FlangeCmd"]);
expect("several pinned items still order among themselves the ordinary way",
       ids(searchRank("f", PIN_CMDS, [], { pins: { FilletCmd: true, FlangeCmd: true } })),
       ["FilletCmd", "FlangeCmd", "FaceCmd"]);
expect("no pins at all falls back to the plain ranking",
       ids(searchRank("f", PIN_CMDS, [], {})), ["FaceCmd", "FilletCmd", "FlangeCmd"]);

print("");
print("=== the ranker drops what Fusion reports disabled ===");
// The 2026-09-30 "circle doesn't start": Drawing/Electronics circles out-ranked the
// sketch's own, and execute() on a disabled command does nothing.
var DEAD_CMDS = [
  { id: "FusionDrawingCircleDiameterCommand", name: "CircleDiameter", group: "Other commands", kind: "other" },
  { id: "Electron::Circle",   name: "Circle",                 group: "FOOTPRINT", kind: "command" },
  { id: "DrawCircle",         name: "Circle",                 group: "Other commands", kind: "other" },
  { id: "CircleCenterRadius", name: "Center Diameter Circle", group: "SKETCH", kind: "command" },
];
var DEAD = ["FusionDrawingCircleDiameterCommand", "Electron::Circle"];
expect("disabled commands are never offered, even when they'd rank first",
       ids(searchRank("circle", DEAD_CMDS, [], { disabled: DEAD })),
       ["DrawCircle", "CircleCenterRadius"]);
expect("a disabled same-named copy can't win dedup over the live one",
       ids(searchRank("circle", DEAD_CMDS, [], { disabled: DEAD })).indexOf("DrawCircle") >= 0, true);
expect("no disabled list (unknown) drops nothing",
       ids(searchRank("circle", DEAD_CMDS, [], {})).length, 3);
expect("in a sketch, a SKETCH-group match outranks a better-placed stray one",
       ids(searchRank("circle", DEAD_CMDS, [], { env: "sketch", disabled: DEAD }))[0],
       "CircleCenterRadius");
expect("...but with no sketch known the ordinary ranking stands",
       ids(searchRank("circle", DEAD_CMDS, [], { disabled: DEAD }))[0],
       "DrawCircle");

print("");
print("=== chips: selecting is not typing ===");
function chipField(text){
  var el = document.createElement("textarea");
  el.tagName = "TEXTAREA";
  Chips.attach(el, CHIP_OPTS);
  document.activeElement = el;
  el.value = text;
  Chips.refresh(el);
  return el;
}
function key(el, k, mods){
  var m = mods || {};
  var ev = { key: k, metaKey: !!m.meta, ctrlKey: !!m.ctrl, altKey: !!m.alt,
             shiftKey: !!m.shift, repeat: !!m.repeat,
             prevented: false, preventDefault: function (){ this.prevented = true; } };
  Chips._keydown(el, ev);
  return ev;
}
function keyUp(el, k){ Chips._keyup(el, { key: k }); }
// SPACE IS TWO KEYS in a field of chips: let go and it commits, held and it is the
// modifier the arrows read (see chips.js, "Keys"). So a test that means the commit has
// to release it, the way a finger does.
function tapSpace(el){ key(el, " "); keyUp(el, " "); }
function hold(el, a, b){ el.setSelectionRange(a, b); Chips.refresh(el); }
function sel(el){ return [el.selectionStart, el.selectionEnd]; }

print("");
print("=== bubbles: a value arriving with brackets ===");
// An extrude edit shown flipped arrives as "-(Width)": one thing in brackets is not a
// bubble, so the pair goes on arrival. And re-reading the same text (every refresh) must
// not track the same pair again — each copy used to paint a bubble of its own.
var ab = chipField("-(Width)");
expect("brackets around one chip are dropped on arrival", ab.value, "-Width");
ab = chipField("((Width + Taper))");
expect("a bubble around only a bubble is dropped", ab.value, "(Width + Taper)");
expect("...and the one left is tracked once", Chips._state(ab).bubbles.length, 1);
ab = chipField("(Width + Taper) * 2");
Chips.refresh(ab); Chips.refresh(ab);
expect("a refresh does not track the same bubble again",
       Chips._state(ab).bubbles.length, 1);
ab = chipField("((Width + Taper) + (Width + 1))");
expect("a bubble holding two bubbles stays", ab.value, "((Width + Taper) + (Width + 1))");
var wrapped = Chips.insertGroup("(A + B) + C", 0, 11, [{ a: 0, b: 7 }]);
expect("wrapping a span that starts with a bubble moves that bubble along",
       wrapped.text + " " + JSON.stringify(wrapped.bubbles),
       '((A + B) + C) [{"a":1,"b":8},{"a":0,"b":13}]');
var unwrapped = Chips.removeGroup("((A + B))", { a: 0, b: 9 }, [{ a: 1, b: 8 }, { a: 0, b: 9 }]);
expect("unwrapping the outer of two keeps the inner",
       unwrapped.text + " " + JSON.stringify(unwrapped.bubbles), '(A + B) [{"a":0,"b":7}]');

var cf = chipField("Width + Taper");
hold(cf, 0, 5);
expect("a letter with a chip held is swallowed", key(cf, "x").prevented, true);
expect("...and puts nothing in the box", cf.value, "Width + Taper");
tapSpace(cf);
expect("Space on one held chip lets go, caret just past it", sel(cf), [5, 5]);
cf.value = "Widthx + Taper"; cf.setSelectionRange(6, 6);
Chips._input(cf, { inputType: "insertText", data: "x" });
expect("a letter typed at a chip's end starts a new item, never more of the chip",
       cf.value.indexOf("Widthx"), -1);
expect("...in a gap of its own (the + comes with the space that commits it)", cf.value, "Width x + Taper");

cf = chipField("Width + Taper");
hold(cf, 0, 5);
key(cf, "f");
expect("F on a held chip opens it for editing", !!Chips._state(cf).editing, true);
expect("...with its text selected", sel(cf), [0, 5]);

cf = chipField("Width + Taper");
hold(cf, 0, 5);
key(cf, "a");
expect("A lets go, caret at the end of the line", sel(cf), [13, 13]);

cf = chipField("Width + Taper");
Chips._toggle(cf, 0, 5); Chips._toggle(cf, 6, 7);
expect("⌘-clicking an operator adds it to the set", Chips._state(cf).multi.length, 2);
Chips._toggle(cf, 8, 13);
expect("...and the set keeps growing past it", Chips._state(cf).multi.length, 3);
expect("\"[\" does nothing", [key(cf, "[").prevented, cf.value], [true, "Width + Taper"]);
tapSpace(cf);
expect("Space on several makes a bubble", cf.value, "(Width + Taper)");
expect("...a tracked one", Chips._state(cf).bubbles.length, 1);
expect("...held", sel(cf), [0, 15]);

key(cf, "f");
expect("F on a held bubble goes into it, holding its first item", sel(cf), [1, 6]);
expect("...which paints as a HELD chip, not letters selected across the bubble",
       Chips._wholeChips(cf, 1, 6), true);
expect("...while part of a chip's letters is still a text selection",
       Chips._wholeChips(cf, 2, 4), false);
expect("...and is inside it", !!Chips._state(cf).bubbles[0].scope, true);
key(cf, "ArrowRight", { meta: true }); key(cf, "ArrowRight", { meta: true });
expect("⌘-arrows walk the bubble's own items", sel(cf), [9, 14]);
key(cf, "ArrowRight", { meta: true });
expect("...and stop at its edge rather than leaving it", !!Chips._state(cf).bubbles[0].scope, true);
hold(cf, 1, 6);
key(cf, "d");
expect("D comes back out, holding the bubble", sel(cf), [0, 15]);
expect("...and is no longer inside it", !!Chips._state(cf).bubbles[0].scope, false);

// GOING IN IS NOT OPENING ANYTHING. The bubble's items are there to be clicked and
// taken one at a time; a chip is opened for typing by F on that chip alone. An F held
// a moment too long repeats, and the repeat used to land on the item entering had just
// selected — so entering a bubble put its first chip into a text field.
cf = chipField("(Width + Taper) + Thickness");
Chips._state(cf).bubbles = [{ a: 0, b: 15 }];
hold(cf, 0, 15);
key(cf, "f");
expect("F into a bubble opens nothing for editing", !!Chips._state(cf).editing, false);
key(cf, "f", { repeat: true });
expect("...and a repeat of the same press opens nothing either",
       !!Chips._state(cf).editing, false);
expect("...it is still the first item that is held", sel(cf), [1, 6]);

print("");
print("=== chips: undo ===");
/* The browser's own undo knows the characters and not which brackets are bubbles, so
   this file keeps its own history — text, selection and bubbles together. */
cf = chipField("Width + Taper");
Chips._toggle(cf, 0, 5); Chips._toggle(cf, 6, 7); Chips._toggle(cf, 8, 13);
tapSpace(cf);
expect("a bubble to undo", [cf.value, Chips._state(cf).bubbles.length], ["(Width + Taper)", 1]);
key(cf, "z", { meta: true });
expect("⌘Z puts the text back", cf.value, "Width + Taper");
expect("...and the bubble with it", Chips._state(cf).bubbles.length, 0);
key(cf, "z", { meta: true, shift: true });
expect("⇧⌘Z does it again", cf.value, "(Width + Taper)");
expect("...bubble and all", Chips._state(cf).bubbles.length, 1);
expect("⌘Z is always swallowed, so the browser's own never also runs",
       key(cf, "z", { meta: true }).prevented, true);
// A run of typing is ONE step: undo is a way back to what you had, not a way of
// retyping the word backwards.
cf = chipField("Width + ");
cf.setSelectionRange(8, 8); Chips.refresh(cf);
typeChar(cf, "T"); typeChar(cf, "a"); typeChar(cf, "p");
expect("three letters typed", cf.value, "Width + Tap");
key(cf, "z", { meta: true });
expect("...come back off as one step", cf.value, "Width + ");

var LT = "(Width + Taper) + Thickness", LB = [{ a: 0, b: 15 }];
var latoms = Chips.scan(LT, chipCtx(null, -1, LB));
var lwidth = Chips.atomAt(latoms, 1, false);
expect("a bubble you are not in is taken whole", Chips.levelItem(latoms, lwidth, null).kind, "group");
expect("...and one you are in gives up its own items",
       Chips.levelItem(latoms, lwidth, { a: 0, b: 15 }).name, "Width");
expect("typing onto a finished chip's end needs a slot",
       Chips.slotNeeded(Chips.scan("Width", chipCtx(null)), 5, "x"), true);
expect("...an operator does not", Chips.slotNeeded(Chips.scan("Width", chipCtx(null)), 5, "+"), false);

// Typing a number straight after a chip: the + goes in, and the number stays OPEN so the
// next digit lands on it rather than starting yet another item.
function typeChar(el, ch){
  var c = el.selectionStart, v = el.value;
  el.value = v.slice(0, c) + ch + v.slice(el.selectionEnd);
  el.setSelectionRange(c + 1, c + 1);
  Chips._input(el, { inputType: "insertText", data: ch });
}
// The browser's own backspace, as it reaches onInput: the character is already gone by
// the time the `input` event fires.
function delChar(el){
  var c = el.selectionStart, v = el.value;
  el.value = v.slice(0, c - 1) + v.slice(c);
  el.setSelectionRange(c - 1, c - 1);
  Chips._input(el, { inputType: "deleteContentBackward" });
}
cf = chipField("25"); cf.setSelectionRange(2, 2); Chips.refresh(cf);
typeChar(cf, "3");
typeChar(cf, "4");
expect("a digit typed after a chip gets its + and stays open for more digits",
       [cf.value, sel(cf)], ["25 + 34", [7, 7]]);

// A decimal point is part of the number being typed — and stays so when the browser's
// selectionchange follows the keystroke, as it always does. That listener used to walk
// the word back over letters and digits only, stop at the ".", and seal "25" as a chip.
function typeCharLive(el, ch){
  typeChar(el, ch);
  document.activeElement = el;
  (document.__listeners.selectionchange || []).forEach(function (fn){ fn(); });
  document.activeElement = null;
}
cf = chipField(""); cf.setSelectionRange(0, 0); Chips.refresh(cf);
typeCharLive(cf, "2"); typeCharLive(cf, "5"); typeCharLive(cf, ".");
expect("a decimal point keeps the number open", Chips._state(cf).typingAt, 0);
typeCharLive(cf, "5");
expect("...and the digits after it land on the same number",
       [cf.value, Chips._state(cf).typingAt], ["25.5", 0]);

cf = chipField("2 + Wi"); cf.setSelectionRange(6, 6); Chips.refresh(cf);
Chips.accept(cf, 4, 6, "Width", "param");
expect("a suggestion taken is a chip with the caret past it, not held",
       [cf.value, sel(cf)], ["2 + Width", [9, 9]]);

print("");
print("=== chips: the arrows move the caret, ⌘ takes hold ===");
cf = chipField("Width + Taper"); cf.setSelectionRange(0, 0); Chips.refresh(cf);
key(cf, "ArrowRight");
expect("→ hops the whole chip, caret past it", sel(cf), [5, 5]);
key(cf, "ArrowRight");
expect("...then the operator", sel(cf), [7, 7]);
key(cf, "ArrowLeft");
expect("← comes back the same way", sel(cf), [6, 6]);
key(cf, "ArrowRight", { meta: true });
expect("⌘→ takes hold of the next one instead", sel(cf), [6, 7]);
key(cf, "ArrowRight");
expect("...and a bare arrow from a held chip lets it go", sel(cf), [7, 7]);

print("");
print("=== chips: Space held moves the chip ===");
cf = chipField("Width + Taper");
hold(cf, 0, 5);
key(cf, " ");                       // down, not released: the modifier
key(cf, "ArrowRight");
expect("Space+→ moves the held chip along the row", cf.value, "Taper + Width");
expect("...and it is still held", sel(cf), [8, 13]);
keyUp(cf, " ");
expect("...and the space it was held with does not also let it go", sel(cf), [8, 13]);
key(cf, " "); key(cf, "ArrowRight"); keyUp(cf, " ");
expect("at the end of the row there is nowhere to move to", cf.value, "Taper + Width");

print("");
print("=== chips: a gap goes back with the word it was made for ===");
cf = chipField("Width + Taper"); cf.setSelectionRange(5, 5); Chips.refresh(cf);
typeChar(cf, "x");
expect("a letter typed at a chip's end opens a slot", cf.value, "Width x + Taper");
delChar(cf);
expect("...and deleting it back to nothing takes the slot with it", cf.value, "Width + Taper");

cf = chipField("Width"); cf.setSelectionRange(5, 5); Chips.refresh(cf);
typeChar(cf, "3");
expect("a digit gets the + as well", cf.value, "Width + 3");
delChar(cf);
expect("...and that + goes back too, rather than dangling off the end", cf.value, "Width");

print("");
print("=== chips: what a parameter can be made of ===");
expect("two operands and an operator is an expression",
       Chips.wholeExpr("Width + Taper"), true);
expect("a chip and the operator beside it is not", Chips.wholeExpr("Width +"), false);
expect("nor is an operator and a chip",            Chips.wholeExpr("+ Width"), false);
expect("nor is one chip on its own",               Chips.wholeExpr("Width"), false);
expect("a call counts as one operand",             Chips.wholeExpr("cos(Taper) * 2"), true);

print("");
print("=== chips: a bare number takes the field's unit ===");
var MM_OPTS = { names: CHIP_OPTS.names, unit: function (){ return "mm"; } };
expect("25 committed in a millimetre box is 25 mm",
       Chips.commit(MM_OPTS, "25", 2).text, "25 mm");
expect("...with the caret past the unit", Chips.commit(MM_OPTS, "25", 2).caret, 5);
expect("a number that already says its unit is left alone",
       Chips.commit(MM_OPTS, "25mm", 4).text, "25mm");
expect("...and so is one with the unit written apart",
       Chips.commit(MM_OPTS, "25 mm", 5).text, "25 mm");
expect("a field with no unit of its own adds nothing",
       Chips.commit(CHIP_OPTS, "25", 2).text, "25");

print("");
print("=== chips: a typed bracket is a bubble, never text ===");
// Type one character the way the browser does it: keydown first (which may answer it),
// then — only if nothing prevented it — the character lands and `input` fires.
function typeCh(el, ch){
  var ev = key(el, ch);
  if (ev.prevented) return;
  var c = el.selectionStart;
  el.value = el.value.slice(0, c) + ch + el.value.slice(el.selectionEnd);
  el.setSelectionRange(c + 1, c + 1);
  Chips._input(el, { inputType: "insertText", data: ch });
}
function typeStr(el, s){ for (var i = 0; i < s.length; i++) typeCh(el, s[i]); }
function scopeRange(el){
  var b = Chips._state(el).bubbles.filter(function (x){ return x.scope; })[0];
  return b ? [b.a, b.b] : null;
}
var bf = chipField("");
typeCh(bf, "(");
expect("\"(\" writes an empty bubble", bf.value, "()");
expect("...tracked", Chips._state(bf).bubbles.length, 1);
expect("...with the caret in it", sel(bf), [1, 1]);
expect("...and you inside it", scopeRange(bf), [0, 2]);
typeStr(bf, "Width"); tapSpace(bf);
expect("a committed chip stays in the bubble, one chip or not", Chips._state(bf).bubbles.length, 1);
typeStr(bf, "f"); tapSpace(bf); typeStr(bf, "Taper"); tapSpace(bf);
expect("...and so does everything committed after it", bf.value, "(Width + Taper)");
expect("...still inside", scopeRange(bf), [0, 15]);
typeCh(bf, ")");
expect("\")\" puts nothing in the box", bf.value, "(Width + Taper)");
expect("...and steps out, caret just past the bubble", sel(bf), [15, 15]);
expect("...no longer inside it", scopeRange(bf), null);
typeStr(bf, "s"); tapSpace(bf); typeStr(bf, "2"); tapSpace(bf);
expect("what is typed next goes beside it", bf.value, "(Width + Taper) * 2");

bf = chipField("");
typeCh(bf, "("); typeStr(bf, "Width"); tapSpace(bf);
typeStr(bf, "f"); tapSpace(bf); typeStr(bf, "Taper");
typeCh(bf, ")");
expect("\")\" after a word still being typed commits it, then steps out",
       [bf.value, sel(bf)], ["(Width + Taper)", [15, 15]]);

bf = chipField("");
typeCh(bf, "("); typeStr(bf, "Width"); tapSpace(bf);
typeStr(bf, "f"); tapSpace(bf); typeStr(bf, "Taper"); tapSpace(bf);
key(bf, "ArrowRight");
expect("→ past the bubble's last item steps out of it", [sel(bf), scopeRange(bf)], [[15, 15], null]);

bf = chipField("");
typeCh(bf, "("); typeStr(bf, "Width"); tapSpace(bf);
key(bf, "ArrowRight");
expect("leaving a bubble holding one chip unwraps it", bf.value, "Width");

bf = chipField("Width");
bf.setSelectionRange(5, 5); Chips.refresh(bf);
typeCh(bf, "(");
expect("\"(\" beside a chip opens a bubble added to it", bf.value, "Width + ()");
expect("...caret inside", sel(bf), [9, 9]);

bf = chipField("");
typeCh(bf, "("); typeCh(bf, "(");
expect("\"((\" nests, inside the inner one", [bf.value, scopeRange(bf)], ["(())", [1, 3]]);
typeStr(bf, "Width"); tapSpace(bf); typeStr(bf, "f"); tapSpace(bf); typeStr(bf, "Taper"); tapSpace(bf);
typeCh(bf, ")");
expect("\")\" steps out ONE level, into the outer bubble", scopeRange(bf), [0, 17]);

bf = chipField("");
typeCh(bf, "("); typeCh(bf, "(");
key(bf, "Backspace");
expect("...an empty inner one, leaving you in the outer", [bf.value, scopeRange(bf)], ["()", [0, 2]]);

bf = chipField("cos(Taper)");
bf.setSelectionRange(9, 9); Chips.refresh(bf);
typeCh(bf, ")");
expect("\")\" inside a call steps over its bracket rather than doubling it",
       [bf.value, sel(bf)], ["cos(Taper)", [10, 10]]);
bf = chipField("");
typeStr(bf, "cos"); typeCh(bf, "(");
expect("\"(\" after a function's name is still the call", bf.value, "cos()");

bf = chipField("(Width + Taper) * 2");
expect("a value arriving with brackets has them taken on as a bubble",
       Chips.scan(bf.value, { caret: null, typingAt: -1, isParam: function (){ return true; },
                              bubbles: Chips._state(bf).bubbles })[0].kind, "group");

// A bracket is never deleted alone: Backspace just inside a bubble's "(" took that half
// of the pair and left the other on screen as a plain ")" — and every key after it
// answered a pair that no longer existed (found by fuzzing: "(" "(" Backspace).
bf = chipField("");
typeCh(bf, "("); typeCh(bf, "(");
bf.setSelectionRange(1, 1);
expect("Backspace just inside a bubble's \"(\" takes nothing",
       [key(bf, "Backspace").prevented, bf.value], [true, "(())"]);
bf = chipField("(Width + Taper) * 2"); bf.setSelectionRange(14, 14);
expect("...nor Delete just inside its \")\"",
       [key(bf, "Delete").prevented, bf.value], [true, "(Width + Taper) * 2"]);

// And whatever slips past that is mended on the next render, not left for the next key:
// a pair whose tracking was lost is a bubble again, a lone bracket goes.
function rerender(el){ Chips._keyup(el, { key: "x" }); }
bf = chipField("(Width + Taper) * 2");
Chips._state(bf).bubbles = []; rerender(bf);
expect("a lost bubble is taken back on the next render",
       Chips.scan(bf.value, { caret: null, typingAt: -1, isParam: function (){ return true; },
                              bubbles: Chips._state(bf).bubbles })[0].kind, "group");
bf = chipField("Width + Taper"); bf.value = "Width) + (Taper"; rerender(bf);
expect("a lone bracket goes on the next render", bf.value, "Width + Taper");

// The plane box's own pair round a two-term offset is a BUBBLE too — it used to stay
// two plain brackets, and ⌫ on one only had the line put it back. Typed the way the
// page sees it: the page's input listener (syncSketchLine) runs before chips.js's own.
planeBox("Width 100"); Chips.refresh(valueEl);
valueEl.setSelectionRange(9, 9); Chips.refresh(valueEl);
this.InputEvent = function (type){ this.type = type; };
valueEl.dispatchEvent = function (ev){ syncSketchLine(ev); syncSketchSituation(); Chips._input(valueEl, ev); };
function planeType(ch){
  var ev = key(valueEl, ch);
  if (ev.prevented) return;
  var c = valueEl.selectionStart;
  valueEl.value = valueEl.value.slice(0, c) + ch + valueEl.value.slice(valueEl.selectionEnd);
  valueEl.setSelectionRange(c + 1, c + 1);
  var ie = { inputType: "insertText", data: ch };
  syncSketchLine(ie); syncSketchSituation(); Chips._input(valueEl, ie);
}
function planeKinds(){
  return Chips.scan(valueEl.value, { caret: null, typingAt: -1, isParam: function (){ return true; },
                                     bubbles: Chips._state(valueEl).bubbles })
    .map(function (a){ return a.kind; }).join(" ");
}
planeType("f"); tapSpace(valueEl); planeType("2"); tapSpace(valueEl);
expect("the plane line's offset pair is a bubble, not two brackets",
       [valueEl.value, planeKinds()], ["Width (100 + 2)", "param group"]);
expect("...with the caret in it", Chips._state(valueEl).bubbles.some(function (b){ return b.scope; }), true);
valueEl.setSelectionRange(7, 7);
expect("...and ⌫ just inside it takes nothing",
       [key(valueEl, "Backspace").prevented, valueEl.value], [true, "Width (100 + 2)"]);
delete this.InputEvent; delete valueEl.dispatchEvent;

print("");
print("=== chips: deleting an operator leaves the caret in its place ===");
var of = chipField("Width * Taper");
hold(of, 6, 7); key(of, "Backspace");
expect("the operator's place holds a \"+\" until something else goes there", of.value, "Width + Taper");
expect("...with the caret standing in it", [sel(of), !!Chips._state(of).opSlot], [[5, 5], true]);
typeCh(of, "-");
expect("an operator typed there takes the place", [of.value, !!Chips._state(of).opSlot], ["Width - Taper", false]);

of = chipField("Width * Taper");
hold(of, 6, 7); key(of, "Backspace");
typeStr(of, "d");
expect("an operator letter is spelled in the gap", of.value, "Width d Taper");
tapSpace(of);
expect("...and its space makes it the operator", of.value, "Width - Taper");

of = chipField("Width * Taper");
hold(of, 6, 7); key(of, "Backspace");
key(of, "ArrowRight");
expect("leaving without typing one leaves the \"+\"", [of.value, !!Chips._state(of).opSlot], ["Width + Taper", false]);

of = chipField("Width * Taper");
hold(of, 6, 7); key(of, "Backspace");
typeStr(of, "d");
of.value = "Width  Taper"; of.setSelectionRange(6, 6);
Chips._input(of, { inputType: "deleteContentBackward" });
expect("a letter typed there and backspaced away gives the slot back",
       [of.value, sel(of), !!Chips._state(of).opSlot], ["Width + Taper", [5, 5], true]);

of = chipField("Width * Taper");
hold(of, 6, 7); key(of, "d");
expect("an operator letter on a held operator changes it", of.value, "Width - Taper");
expect("...and it stays held", sel(of), [6, 7]);
key(of, "a");
expect("...A too, rather than letting go", [of.value, sel(of)], ["Width / Taper", [6, 7]]);
key(of, "f"); key(of, "s");
expect("...F and S as well", [of.value, sel(of)], ["Width * Taper", [6, 7]]);
key(of, "+");
expect("...or the operator typed as itself", of.value, "Width + Taper");
hold(of, 0, 5); key(of, "d");
expect("a held parameter is not changed by one", of.value, "Width + Taper");

of = chipField("Width * Taper");
of.setSelectionRange(8, 8); Chips.refresh(of);
key(of, "Backspace");
expect("Backspace with the caret just past an operator leaves its slot, never gluing the two",
       [of.value, sel(of), !!Chips._state(of).opSlot], ["Width + Taper", [5, 5], true]);
of = chipField("Width * Taper");
of.setSelectionRange(7, 7); Chips.refresh(of);
key(of, "Backspace");
expect("...tight against it too", [of.value, !!Chips._state(of).opSlot], ["Width + Taper", true]);
of = chipField("Width * Taper");
of.setSelectionRange(5, 5); Chips.refresh(of);
key(of, "Delete");
expect("...and Delete with the operator ahead", [of.value, !!Chips._state(of).opSlot], ["Width + Taper", true]);

of = chipField("Thickness + Width * Taper");
hold(of, 18, 19); key(of, "Backspace");
key(of, "Backspace");
expect("Backspace again takes the item before, with the slot", of.value, "Thickness + Taper");

print("");
print("=== the value box never carries a unit it wasn't typed with ===");
openSession({ mode: "dim", expression: "400 mm", unit: "mm", component: "Boot", symmetry: {} });
expect("a dimension's 400 mm opens as 400", valueEl.value, "400");
openSession({ mode: "dim", expression: "Width / 2", unit: "mm", component: "Boot", symmetry: {} });
expect("...an expression is left as it is", valueEl.value, "Width / 2");
openSession({ mode: "dim", expression: "1 in", unit: "mm", component: "Boot", symmetry: {} });
expect("...and so is a number in some other unit", valueEl.value, "1 in");

print("");
print("=== the list reads names the way the Browser does ===");
// Shown as "Boot Ceiling - Dimension - Width", matched on that reading too — the typed
// word against the reading with its spaces and dashes squeezed out — and accepted as
// the real name.
params = [{ name: "BootCeiling_Dimension_Span_X", expression: "40 mm" },
          { name: "BootCeiling_Dimension_Radius", expression: "3 mm" },
          { name: "DrawerButtonPlateHeight", expression: "12 mm" }];
function spokenRows(typed){
  axis = ""; end = ""; unit = ""; invalidateEntries();
  __els["value"].value = typed;
  __els["value"].selectionStart = __els["value"].selectionEnd = typed.length;
  document.activeElement = valueEl;
  updateAC();
  return acItems.map(function(it){ return it.shownHits ? it.entry.shown : it.entry.label; });
}
expect("a structured name is shown as it is read",
       spokenRows("bootceiling").sort(), ["Boot Ceiling - Dimension - Radius", "Boot Ceiling - Dimension - Width"]);
expect("the reading's own words match (Width, not Span_X)",
       spokenRows("dimensionwid"), ["Boot Ceiling - Dimension - Width"]);
expect("...word by word at the capitals typed",
       spokenRows("ceilingDimW"), ["Boot Ceiling - Dimension - Width"]);
expect("a plain CamelCase name is spaced out",
       spokenRows("buttonpl"), ["Drawer Button Plate Height"]);
expect("the real name still matches",
       spokenRows("Dimension_Sp"), ["Boot Ceiling - Dimension - Width"]);
spokenRows("dimensionwid"); acceptAC();
expect("accepting writes the real name", valueEl.value.trim(), "BootCeiling_Dimension_Span_X");

print("");
print("=== the feature panel: rows arrive as data ===");
(function(){
  var sentBodies = [], realPost = post;
  post = function(body){ sentBodies.push(body); };
  var ROWS = [
    { type: "seg", key: "dir1", label: "Direction", slot: "dir1",
      options: [["x", "X"], ["y", "Y"], ["picked", "Pick"]] },
    { type: "text", key: "count1", placeholder: "Quantity", unit: "" },
    { type: "seg", key: "dir2", label: "Second", options: [["none", "None"], ["x", "X"]] },
    { type: "text", key: "count2", placeholder: "Quantity, second", when: { dir2: ["x"] } },
    { type: "sel", key: "selection", label: "Selected", button: "Clear" },
    { type: "sel", key: "target", label: "Target", button: "Pick" },
    { type: "seg", key: "compute", label: "Compute", adv: true,
      options: [["optimized", "Optimized"]] },
  ];
  var shownKeys = function(){
    return oRowEls.filter(function(e){ return e.row.style.display !== "none"; })
                  .map(function(e){ return e.def.key; }).join(",");
  };
  openFeatureSession({ mode: "feature", unit: "mm", feature: { kind: "pattern", rows: ROWS,
    spec: { dir1: "x", count1: "2", dir2: "none", count2: "3", compute: "optimized",
            selectionText: "2 bodies", keys: ["ctrl+q"], armed: "target", picked: {} } } });
  expect("a row per definition", oRowEls.length, 7);
  expect("a text row is a field, filled from the spec", oFields.count1.value, "2");
  expect("a `when` row hides until its condition holds", shownKeys(),
         "dir1,count1,dir2,selection,target,compute");
  expect("keys come with the spec", oKeys.has("ctrl+q"), true);

  sentBodies = [];
  oChoose(ROWS[2], "x");
  expect("a choice posts the whole spec, tagged with its kind",
         JSON.stringify([sentBodies[0].action, sentBodies[0].feature, sentBodies[0].dir2,
                         sentBodies[0].count1]),
         JSON.stringify(["setfeature", "pattern", "x", "2"]));
  expect("...and shows the rows that wait on it", shownKeys(),
         "dir1,count1,dir2,count2,selection,target,compute");

  oSpec.count1 = "5";                     // typed, and not yet answered
  applyFeatureUpdate({ kind: "pattern", spec: { count1: "2", selectionText: "3 bodies",
                                                dir2: "x" } });
  expect("a push never overwrites a field", oSpec.count1, "5");
  expect("...but takes a read-out", oSpec.selectionText, "3 bodies");
  applyFeatureUpdate({ kind: "combine", spec: { selectionText: "9 tools" } });
  expect("a push for another feature is ignored", oSpec.selectionText, "3 bodies");

  sentBodies = [];
  oChoose(ROWS[0], "picked");
  expect("Pick on a slot's row posts it", sentBodies[0].dir1, "picked");
  oSpec.picked = { dir1: "Edge" };
  sentBodies = [];
  oChoose(ROWS[0], "picked");
  expect("Pick again, with something picked, lets go of it (repick)",
         sentBodies[0].repick, "dir1");

  // The shim keeps no children, so the row is built again here with its cells caught.
  var made = [], realCreate = document.createElement;
  document.createElement = function(){ var e = realCreate(); made.push(e); return e; };
  var seg = oSegRow(ROWS[0], document.getElementById("__segtest"));
  document.createElement = realCreate;
  var shownCells = function(){
    seg.render();
    return made.filter(function(c){ return c.className === "xopt" && c.style.display !== "none"; })
               .map(function(c){ return c.textContent; }).join(",");
  };
  oSpec.dir1 = "y"; oSpec.dir1Only = ["y"];
  expect("a seg row shows only the choices the add-in offers — Pick always",
         shownCells(), "Y,Pick");
  oSpec.dir1Only = null;
  expect("...and all of them when it offers no list", shownCells(), "X,Y,Pick");

  sentBodies = [];
  commitFeature();
  expect("Enter commits, tagged with its kind",
         JSON.stringify(sentBodies[0]), JSON.stringify({ action: "commitfeature", feature: "pattern" }));
  committing = false;
  post = realPost;
})();

print("");
print("=== ASDF: an unlikely option is dimmed when the likely one is all but certain ===");
(function(){
  openNew({ mode: "dim", component: "Boot", symmetry: {}, unit: "mm", axis: "Y", end: "high" });
  stepType = "Inset"; stepStage = "anchor"; stepAnchors = []; stepProposed = false;
  renderSteps();
  expect("the dimension's own end leads the anchor step", stepCandidates()[0], "Back");
  expect("...and the others are dimmed",
         (stepChoicesEl.innerHTML.match(/class="stepopt dim"/g) || []).length > 0, true);
  expect("...but the likely one is not",
         /class="stepopt" data-w="Back"/.test(stepChoicesEl.innerHTML), true);
  openNew({ mode: "dim", component: "Boot", symmetry: {}, unit: "mm" });
  stepType = "Inset"; stepStage = "anchor"; stepAnchors = []; stepProposed = false;
  renderSteps();
  expect("no situation: nothing dimmed", stepChoicesEl.innerHTML.indexOf("stepopt dim"), -1);
})();

print("");
print("=== an Extrude panel does not inherit the last dimension's situation ===");
(function(){
  var realPost = post; post = function(){};
  openSession({ mode: "dim", unit: "mm", axis: "X", end: "high", datum: "right",
                axes: ["X"], ends: { X: "high" }, symmetric: true });
  openExtrudeSession({ mode: "extrude", unit: "mm", extrude: { operation: "auto", rev: 0 } });
  expect("none sent: the stale dimension's axis/datum are gone",
         [axis, datum, JSON.stringify(situAxes), symmetric], ["", "", "[]", false]);
  openExtrudeSession({ mode: "extrude", unit: "mm", extrude: { operation: "auto", rev: 0,
    situation: { axis: "Z", axes: ["Z"], datum: "zero", datumFaces: ["Top"] } } });
  expect("one sent: the profile's plane is the situation",
         [axis, datum, datumFaces.join(), situAxes.join()], ["Z", "zero", "top", "Z"]);
  post = realPost;
})();

print("");
print("=== a choice holds until Fusion has HEARD it (rev), not for a timer ===");
(function(){
  var sentBodies = [], realPost = post;
  post = function(body){ sentBodies.push(body); };
  openExtrudeSession({ mode: "extrude", unit: "mm",
    extrude: { operation: "auto", operationResolved: "join", direction: "one", rev: 0,
               cycleKeys: { "ctrl+o": "operation", "ctrl+d": "direction" } } });
  var realNow = Date.now, now = realNow();
  Date.now = function(){ return now; };
  xClaim("operation", "cut");
  xSpec.operation = "cut";
  xPush();
  var rev = sentBodies[sentBodies.length - 1].rev;
  expect("the post carries the claim's rev", rev, panelRev);
  now += 5000;                           // a long preview: the old timer is long gone
  applyExtrudeUpdate({ operation: "auto", rev: rev - 1 });
  expect("a push that left before the click landed does not flick the row back",
         xSpec.operation, "cut");
  applyExtrudeUpdate({ operation: "intersect", rev: rev });
  expect("one that has heard it wins, even with another word (Fusion's rules)",
         xSpec.operation, "intersect");
  xClaim("direction", "two"); xSpec.direction = "two";
  now += 2000;
  applyExtrudeUpdate({ direction: "one" });
  expect("a push with no rev (an add-in not reloaded) falls back to the timer",
         xSpec.direction, "one");
  now += 60000;
  Date.now = realNow;

  // A cycle key steps the row here, from what it is doing.
  xSpec.operation = "auto"; xSpec.operationResolved = "join";
  sentBodies = [];
  xCycle("operation");
  expect("Cycle Operation on auto steps from the operation it is doing",
         xSpec.operation, "newbody");
  expect("...and posts it as a choice", sentBodies[0].operation, "newbody");
  xCycle("operation");
  expect("...and wraps", xSpec.operation, "cut");
  xSpec.direction = "two";
  xCycle("direction");
  expect("Cycle Direction wraps too", xSpec.direction, "one");
  expect("the chords arrive with the spec", xCycleKeys["ctrl+o"], "operation");

  // The feature panel: the add-in's offered order, Pick only with a pick, and a second
  // row of the same choices swaps rather than coincides.
  var DIRS = [["x", "X"], ["y", "Y"], ["z", "Z"], ["picked", "Pick"]];
  openFeatureSession({ mode: "feature", unit: "mm", feature: { kind: "pattern", rows: [
      { type: "seg", key: "dir1", label: "Direction", slot: "dir1", options: DIRS },
      { type: "seg", key: "two", label: "Directions", options: [["one", "One"], ["two", "Two"]] },
      { type: "seg", key: "dir2", label: "Second", slot: "dir2", options: DIRS,
        when: { two: ["two"] } }],
    spec: { dir1: "z", dir2: "x", two: "two", dir1Only: ["z", "x"], picked: {},
            cycles: { direction: "dir1" } } } });
  sentBodies = [];
  oCycle("direction");
  expect("the feature panel steps in the offered order", oSpec.dir1, "x");
  expect("...the second direction swaps out of its way", oSpec.dir2, "z");
  expect("...and both go in the one post",
         JSON.stringify([sentBodies[0].dir1, sentBodies[0].dir2]), JSON.stringify(["x", "z"]));
  oCycle("direction");
  expect("no pick: Pick is not in the ring", oSpec.dir1, "z");
  expect("a kind with no row is handed to Fusion instead", oCycle("operation"), false);
  post = realPost;
})();

print("");
print("=== dialogTitle: the header's title text ===");
(function(){
  expect("a dimension", dialogTitle("dim"), "Dimension");
  expect("a new parameter", dialogTitle("param"), "Parameter");
  expect("editing one", dialogTitle("editparam"), "Edit Parameter");
  expect("the plane box", dialogTitle("sketch"), "Sketch Plane");
  expect("New Component", dialogTitle("component"), "Component");
  expect("Add Bounds", dialogTitle("addbounds"), "Bounds");
  expect("Edit Bounds reads the same as Add Bounds", dialogTitle("editbounds"), "Bounds");
  expect("Extrude", dialogTitle("extrude"), "Extrude");
  expect("Mirror", dialogTitle("mirror"), "Mirror");
  expect("Make a parameter", dialogTitle("make"), "Make Parameter");
  expect("the feature panel takes its title from the add-in, not a fixed string",
         dialogTitle("feature", { feature: { title: "Array" } }), "Array");
  expect("...and says nothing if the add-in didn't send one",
         dialogTitle("feature", { feature: {} }), "");
  expect("the search palette is a palette, not a dialog — no title",
         dialogTitle("search"), "");
})();

print("");
print("=== F takes a suggestion only once an arrow has marked it ===");
(function(){
  var realAccept = acceptAC, took = 0, realMode = mode;
  acceptAC = function(){ took++; return true; };
  mode = "dim";
  function press(k){
    var ev = { key: k, target: valueEl, prevented: false,
               preventDefault(){ this.prevented = true; }, stopPropagation(){} };
    (document.__listeners.keydown || []).forEach(function (fn){ fn(ev); });
    return ev.prevented;
  }
  valueEl.focus();
  acItems = [{ entry: { label: "Width", target: "Width", kind: "param" }, hits: [] }];
  acIndex = 0; acTouched = false;
  expect("an unmarked list: F is typed", [press("f"), took], [false, 0]);
  acTouched = true;
  expect("marked by an arrow: F takes it, as Enter would", [press("f"), took], [true, 1]);
  acItems = []; acIndex = -1; acTouched = false;
  acceptAC = realAccept; mode = realMode;
})();

print("");
print("=== ⇧Esc (Space+Esc) closes the whole dialog, past an open suggestion list ===");
(function(){
  var sentBodies = [], realPost = post, realMode = mode;
  post = function(body){ sentBodies.push(body); };
  function esc(shift){
    var ev = { key: "Escape", shiftKey: !!shift, target: valueEl,
               preventDefault(){}, stopPropagation(){} };
    (document.__listeners.keydown || []).forEach(function (fn){ fn(ev); });
  }
  mode = "dim";
  // The shim's classList knows no classes: say the chip menu and the picker are hidden.
  var hidden = { add(){}, remove(){}, toggle(){}, contains(c){ return c === "hide"; } };
  var realCL = [chipMenuEl.classList, pickEl.classList];
  chipMenuEl.classList = hidden; pickEl.classList = hidden;
  function openWithList(){
    isOpen = true; committing = false; sentBodies = [];
    acItems = [{ entry: { label: "Width", target: "Width", kind: "param" }, hits: [] }];
  }
  openWithList(); esc(false);
  expect("plain Esc with a list up only puts the list away",
         [acItems.length, isOpen, sentBodies.length], [0, true, 0]);
  openWithList(); esc(true);
  expect("⇧Esc with a list up cancels the session at once",
         [acItems.length, isOpen, sentBodies.map(function (b){ return b.action; })],
         [0, false, ["cancel"]]);
  // Where Space DOES reach the page (a panel with no field), the master chord itself.
  openWithList(); spaceDown = true; esc(false); spaceDown = false;
  expect("Space+Esc seen by the page is the master chord, and closes it all",
         [acItems.length, isOpen], [0, false]);
  closeDialogChord = "cmd+escape";
  openWithList(); spaceDown = true; esc(false); spaceDown = false;
  expect("...a different master chord and Space+Esc is only Escape again",
         [acItems.length, isOpen], [0, true]);
  closeDialogChord = "space+escape";
  mode = realMode; isOpen = false;
  chipMenuEl.classList = realCL[0]; pickEl.classList = realCL[1];
  post = realPost;
})();

print("=== a just-placed dimension opens with its value selected, and stays selected ===");
(function(){
  var realMode = mode, realActive = document.activeElement;
  var fake = { isConnected: valueEl.isConnected, getClientRects: valueEl.getClientRects,
               tagName: valueEl.tagName };
  valueEl.isConnected = true; valueEl.tagName = "TEXTAREA";
  valueEl.getClientRects = function(){ return [1]; };
  function sel(){ return [valueEl.selectionStart, valueEl.selectionEnd, valueEl.value.length]; }
  openSession({ mode: "dim", expression: "40 mm", unit: "mm", created: true });
  isOpen = true; document.activeElement = valueEl;
  expect("the placed value is shown, not blanked", valueEl.value, "40");
  expect("...selected whole", sel(), [0, 2, 2]);
  // The host's reclaim on activation: __refocus used to turn a whole-field selection
  // into a caret at the end.
  valueEl.setSelectionRange(2, 2);
  window.__refocus();
  expect("__refocus brings the selection back, not a caret", sel(), [0, 2, 2]);
  guardCaret(valueEl);
  valueEl.setSelectionRange(2, 2);
  enforceCaret();
  expect("the guard puts back a collapse to the end too", sel(), [0, 2, 2]);
  openSession({ mode: "dim", expression: "37.628541 mm", unit: "mm" });
  expect("an existing dimension with a pointer-length value is selected too",
         [valueEl.value, sel()], ["37.628541", [0, 9, 9]]);
  openSession({ mode: "dim", expression: "40 mm", unit: "mm" });
  expect("an existing dimension with a chosen value opens with the caret at the end",
         [openSelectAll, sel()], [false, [2, 2, 2]]);
  valueEl.isConnected = fake.isConnected; valueEl.tagName = fake.tagName;
  valueEl.getClientRects = fake.getClientRects;
  caretGuard = null; openSelectAll = false;
  mode = realMode; isOpen = false; document.activeElement = realActive;
})();

print("");
print("=== short names: a chip is as wide as its reading, and the page never sees one ===");
(function(){
  // A field of its own, so nothing the page's valueEl holds is disturbed. The readings
  // are the page's kind: " - " between levels, ", " between axis words.
  var LABELS = { Drawer_Shelf__Width: "Shelf - Width", Drawer_Span_X_Y: "Width, Depth",
                 Twin_A: "Same", Twin_B: "Same", Plain: "Plain" };
  var names = Object.keys(LABELS).concat(["Other"]);
  var el = document.createElement("textarea");
  Chips.attach(el, { names: function(){ return names; },
                     label: function(n){ return LABELS[n] || null; } });
  var st = Chips._state(el);
  var SH = "Shelf\u202f\u2011\u202fWidth", WD = "Width\u201a\u202fDepth";
  el.value = "Drawer_Shelf__Width + 2 * Drawer_Span_X_Y";
  expect("the field holds the readings", st.nat.get(), SH + " + 2 * " + WD);
  expect("...the page reads the real expression", el.value,
         "Drawer_Shelf__Width + 2 * Drawer_Span_X_Y");
  var atoms = Chips.flatten(Chips.scan(st.nat.get(), { caret: null, typingAt: -1,
    isParam: function(w){ return st.opts.names().indexOf(w) >= 0; } }));
  expect("a short name is ONE chip, a parameter",
         atoms.filter(function(a){ return a.kind === "param"; }).map(function(a){ return a.name; }),
         [SH, WD]);
  // Positions: the field's caret after the first chip is the real one after its name.
  st.nat.sel(SH.length, SH.length);
  expect("the caret reads back in real positions",
         [el.selectionStart, el.selectionEnd], [19, 19]);
  el.setSelectionRange(22, 22);                       // after "Drawer_Shelf__Width + "
  expect("...and is set in them", st.nat.ss(), SH.length + 3);
  // A chip held: the page is told its real name and span.
  document.activeElement = el;
  st.nat.sel(0, SH.length);
  Chips.refresh(el);
  var c = Chips.chipAtCaret(el);
  expect("chipAtCaret answers with the real name", [c.name, c.a, c.b],
         ["Drawer_Shelf__Width", 0, 19]);
  // Accepting a suggestion: the page hands the real name at real positions.
  el.value = "2 + ";
  var r = Chips.accept(el, 4, 4, "Drawer_Shelf__Width", "param");
  expect("an accepted name goes in short", st.nat.get(), "2 + " + SH);
  expect("...the caret comes back real", r.caret, 23);
  // A reading two parameters share makes no short name — they keep their real ones.
  el.value = "Twin_A + Plain";
  expect("a shared reading stays the real name, as does one that IS the name",
         st.nat.get(), "Twin_A + Plain");
  el.value = "Twin_A + Drawer_Shelf__Width";
  expect("...and reads back unchanged", el.value, "Twin_A + Drawer_Shelf__Width");
  // A real name that turns up in the field any other way is made short at the render.
  st.nat.set("Drawer_Span_X_Y");
  Chips.refresh(el);
  expect("a real name typed out in full becomes its reading", st.nat.get(), WD);
  // The part of a reading being edited is not a name of its own ("Shelf" of "Shelf - …").
  LABELS.Shelf = "Shelf Thing"; names.push("Shelf");
  el.value = "Shelf";
  expect("a name with a short name goes in short", st.nat.get(), "Shelf\u202fThing");
  st.nat.set("Shelf\u202f\u2011\u202fWid");
  expect("...but not the same word inside a reading being edited", el.value,
         "Shelf\u202f\u2011\u202fWid");
  document.activeElement = null;
})();

print("");
print("=== number parameters: a typed number that is a parameter reads as the number ===");
(function(){
  var realParams = params, realUnit = unit;
  params = [{ name: "Num_aaaaaaaa", expression: "10 mm", unit: "mm" },
            { name: "Num_bbbbbbbb", expression: "10 mm", unit: "mm" },
            { name: "Num_cccccccc", expression: "2.5 cm", unit: "cm" },
            { name: "Shelf", expression: "18 mm", unit: "mm" }];
  unit = "mm";
  expect("a number parameter reads as its value, without the field's unit",
         chipLabel("Num_aaaaaaaa"), "10");
  expect("...and keeps a unit that is not the field's", chipLabel("Num_cccccccc"), "2.5 cm");
  expect("it is never suggested",
         entries().filter(function(e){ return isNumberParam(e.target); }).length, 0);
  var el = document.createElement("textarea");
  Chips.attach(el, { names: function(){ return params.map(function(p){ return p.name; }); },
                     label: chipLabel });
  var st = Chips._state(el);
  el.value = "Shelf + Num_aaaaaaaa + Num_bbbbbbbb";
  var shown = st.nat.get();
  expect("the field shows the numbers (and nothing else visible)",
         shown.replace(/\u2060/g, ""), "Shelf + 10 + 10");
  expect("...the page reads the real expression", el.value,
         "Shelf + Num_aaaaaaaa + Num_bbbbbbbb");
  var atoms = Chips.flatten(Chips.scan(shown, { caret: null, typingAt: -1,
    isParam: function(w){ return Chips._state(el).short.all.indexOf(w) >= 0; } }));
  expect("each is ONE param chip",
         atoms.filter(function(a){ return a.kind === "param"; }).length, 3);
  // Rebuilding keeps each one's short name: the field never comes to mean another.
  params = params.concat([{ name: "Num_00000000", expression: "10 mm", unit: "mm" }]);
  Chips.refresh(el);
  expect("a new twin reading changes nothing already there", el.value,
         "Shelf + Num_aaaaaaaa + Num_bbbbbbbb");
  // Opened and retyped, a number chip is a plain number to the page.
  var a = shown.indexOf("\u206010");
  st.nat.set(shown.slice(0, a) + "\u206012" + shown.slice(a + 3));
  expect("a retyped number chip reads back as the number", el.value,
         "Shelf + 12 + Num_bbbbbbbb");
  params = realParams; unit = realUnit;
})();
// The value box itself, on the live document's parameters: a session opened on an
// expression shows each parameter as its reading, and the page reads the expression back.
(function(){
  var realParams = params, realMode = mode;
  params = PARAMS;
  var p = PARAMS.filter(function(q){
    var l = chipLabel(q.name);
    return l && l !== q.name && /^[A-Za-z][A-Za-z0-9_ ,\-]*[A-Za-z0-9_]$/.test(l);
  })[0];
  if (!p){ print("  (skipped: no parameter with a reading in the live document)"); return; }
  var expr = p.name + " * 2";
  openSession({ mode: "editparam", expression: expr, unit: "", name: "Scratch_Test" });
  expect("the value box shows the reading, not " + p.name,
         Chips.shown(valueEl).indexOf(p.name) < 0 &&
         Chips.shown(valueEl).indexOf(chipLabel(p.name).split(" ")[0]) === 0, true);
  expect("...and the page reads the real expression", valueEl.value, expr);
  params = realParams; mode = realMode;
})();

print("");
print("=== make a parameter: named with the stepper ===");
(function(){
  var realParams = params, realMode = mode;
  params = SYNTHETIC; invalidateEntries();
  openNew({ mode: "param", component: "Boot", symmetry: {}, nameRequired: true });
  typeLevels("Gear");                               // the session's own name, half made
  var before = [stepLevels.join(","), stepStage];
  heldIn(valueEl, "Width * 2 + 3", 12, 13);
  openMake(valueEl);
  expect("the make panel names from Parent, in the stepper",
         [stepperOn, stepStage, stepLevels.join(","), fSteps.parentNode === mPanel],
         [true, "parent", "Boot", true]);
  stepCommit();
  expect("...an unfinished name makes nothing", [makePending, !!mErr.textContent], ["", true]);
  closeMake();
  expect("...and closing it gives the session its own naming back",
         [stepLevels.join(","), stepStage, making], before.concat([null]));

  mode = "extrude";
  var f = document.createElement("input");
  chipsAttach(f);
  heldIn(f, "25 + Width", 0, 2);
  openMake(f);
  expect("a field whose session names no component asks the add-in first",
         [makeInfoWait, makeSaved], [true, null]);
  makeInfoWait = false;
  makeStepperStart({ component: "Lid", componentPath: ["TopLevel", "Lid"], components: [] });
  expect("...and names from the component it answers with",
         [stepperOn, stepStage, stepLevels[0]], [true, "parent", "Lid"]);
  closeMake();

  // The plane box hides #pname; the make panel's stepper needs it back to take keys.
  openSketchSession({ datums: [], text: "Width 100" });
  var wasHidden = nameEl.style.display;
  heldIn(valueEl, "Width 100", 6, 9);
  openMake(valueEl);
  makeInfoWait = false;
  makeStepperStart({ component: "Lid", componentPath: ["TopLevel", "Lid"], components: [] });
  expect("the plane box's make can take keys: #pname is shown",
         [wasHidden, nameEl.style.display], ["none", ""]);
  closeMake();
  expect("...and hidden again once it closes", nameEl.style.display, "none");
  params = realParams; mode = realMode; invalidateEntries();
})();

print("");
print("=== section sketch: proposal, doppelganger, flexible ===");
(function (){
  var realParams = params;
  params = STEP_PARAMS;
  // The add-in proposes Inset from the Back: only the feature word is asked.
  openNew({ mode: "dim", component: "Boot", symmetry: {}, axis: "Y", end: "high",
            expression: "40 mm", proposal: { type: "Inset", anchors: ["Back"], axes: [] } });
  typeLevels("Shelf");
  expect("a proposal takes the type and anchor steps", [stepStage, stepperName()],
         ["done", "Boot_Shelf_Inset_Back"]);
  // Span gets its axis; Thickness none.
  openNew({ mode: "dim", component: "Boot", symmetry: {}, axis: "X", expression: "200 mm",
            proposal: { type: "Span", anchors: [], axes: ["X"] } });
  typeLevels("Gap");
  expect("a Span proposal carries its axis", stepperName(), "Boot_Gap_Span_X");
  openNew({ mode: "dim", component: "Boot", symmetry: {}, axis: "Z", expression: "18 mm",
            proposal: { type: "Thickness", anchors: [], axes: [] } });
  typeLevels("Wall");
  expect("a Thickness proposal has no axis", [stepStage, stepperName()], ["done", "Boot_Wall_Thickness"]);
  // Back to the type step asks as usual — the proposal is used once.
  stepBack(); stepBack();
  expect("going back asks", stepStage === "type" || stepStage === "level", true);
  // A second Thickness with the same name, on another axis, is not a collision.
  params = STEP_PARAMS.concat([{ name: "Boot_Wall_Thickness", expression: "18 mm" }]);
  openNew({ mode: "dim", component: "Boot", symmetry: {}, axis: "X", expression: "18 mm",
            proposal: { type: "Thickness", anchors: [], axes: [] } });
  expect("a doppelganger is allowed through", nameProblem("Boot_Wall_Thickness"), "");
  expect("...and flagged for the add-in", doppelName, "Boot_Wall_Thickness");
  expect("...but an axis-carrying twin is still a clash",
         nameProblem("Boot_Ceiling_Lip_Inset_Back") !== "", true);
  // Flexible: on by default, limits ride in the commit.
  expect("flexible is on by default", flexState(), { flexible: true, min: "", max: "" });
  flexMinEl.value = "8 mm";
  expect("a limit is read back", flexState().min, "8 mm");
  valueEl.value = "";
  expect("flexible + a limit + no value is a gap", flexGap(), true);
  setFlex({ flexible: false, min: "", max: "" });
  expect("rigid is read back", flexState().flexible, false);
  params = realParams;
})();

print("");
print(fails ? "FAIL: " + fails + " assertion(s)" : "all assertions passed");
