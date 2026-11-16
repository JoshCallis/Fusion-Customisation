/* Cases for param_naming.js. Run it:
 *
 *     /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc \
 *       fusion_ui/param_naming.js fusion_ui/param_naming_test.js
 *
 * Two files, one jsc invocation: jsc shares one global scope across every file given
 * on the command line, so param_naming.js's top-level `const ParamNaming` is visible
 * here without an eval() that would otherwise scope it away (ES6 block-scoped
 * declarations inside eval() don't leak to the enclosing scope, unlike `var`).
 */
ParamNaming.setVocabulary(JSON.parse(read("/Users/josh/Code/Scripts/fusion_ui/param_naming.json")));

var fails = 0;
// Key order is not part of the answer, so the two sides are compared with their keys
// sorted — a parse result gained its plural fields (anchors/axes/ends) after these
// cases were written, and where they sit in the object is nobody's business.
function stable(v) {
  if (Array.isArray(v)) return "[" + v.map(stable).join(",") + "]";
  if (v && typeof v === "object") {
    return "{" + Object.keys(v).sort().map(function (k) {
      return JSON.stringify(k) + ":" + stable(v[k]);
    }).join(",") + "}";
  }
  return JSON.stringify(v);
}
function check(label, got, want) {
  var g = stable(got), w = stable(want);
  if (g !== w) { print("FAIL " + label + ": got " + g + " want " + w); fails++; }
}
// A one-direction expectation written with just anchor/axis/end: the plural fields
// are whatever those say, which is how every one-direction name parses.
function P(o) {
  if (!o.anchors) o.anchors = o.anchor ? [o.anchor] : [];
  if (!o.axes) o.axes = o.axis ? [o.axis] : [];
  if (!o.ends) o.ends = o.end ? [o.end] : [];
  return o;
}
function roundTrip(name) {
  var p = ParamNaming.parse(name);
  if (p === null) { print("FAIL roundtrip " + name + ": did not parse"); fails++; return; }
  check("compose(" + name + ")", ParamNaming.compose(p), name);
}

// identifier — a raw (component) name squashed to a valid Fusion identifier.
check("identifier strips space", ParamNaming.identifier("Front Panel"), "FrontPanel");
check("identifier leading digit", ParamNaming.identifier("3D Print"), "Comp3DPrint");
check("identifier empty", ParamNaming.identifier(""), "Comp");
check("identifier strips punctuation", ParamNaming.identifier("a-b.c"), "abc");
check("identifier already valid", ParamNaming.identifier("Boot"), "Boot");

// Full anchor form.
check("parse inset", ParamNaming.parse("BootCeiling_Front_Inset_Back"),
      P({ levels: ["BootCeiling", "Front"], type: "Inset", anchor: "Back", axis: "Y", end: "high" }));
roundTrip("BootCeiling_Front_Inset_Back");

// A level word equal to an anchor word stays a level when it isn't trailing.
check("level collides with anchor word", ParamNaming.parse("Drawer_Front_Span_X").levels,
      ["Drawer", "Front"]);

// Axis-letter tail.
check("parse span", ParamNaming.parse("Drawer_Width_Span_X"),
      P({ levels: ["Drawer", "Width"], type: "Span", anchor: null, axis: "X", end: null }));
roundTrip("Drawer_Width_Span_X");

// No anchor/axis at all (Radius, Multiplier, bare Count/Offset/Spacing).
check("parse radius", ParamNaming.parse("Handle_Radius"),
      P({ levels: ["Handle"], type: "Radius", anchor: null, axis: null, end: null }));
roundTrip("Handle_Radius");
check("parse thickness — no direction word at all", ParamNaming.parse("Boot_Wall_Thickness"),
      P({ levels: ["Boot", "Wall"], type: "Thickness", anchor: null, axis: null, end: null }));
roundTrip("Boot_Wall_Thickness");
check("thickness's axis is optional, not absent", ParamNaming.allowedAxes("Thickness"),
      ["X", "Y", "Z"]);
check("parse count no axis", ParamNaming.parse("Drawer_Count"),
      P({ levels: ["Drawer"], type: "Count", anchor: null, axis: null, end: null }));

// Optional axis given.
check("parse offset with axis", ParamNaming.parse("Shelf_Offset_Z"),
      P({ levels: ["Shelf"], type: "Offset", anchor: null, axis: "Z", end: null }));
roundTrip("Shelf_Offset_Z");

// Invalid: required anchor omitted.
check("inset without anchor is invalid", ParamNaming.parse("Drawer_Inset"), null);
// Span's axis is optional now — General, no axis, still a valid name.
check("span with no axis is General", ParamNaming.parse("Drawer_Span"),
      P({ levels: ["Drawer"], type: "Span", anchor: null, axis: null, end: null }));
roundTrip("Drawer_Span");
// Invalid: not enough tokens.
check("single token is invalid", ParamNaming.parse("Drawer"), null);
// Invalid: old-convention / unrecognised name.
check("unrecognised name", ParamNaming.parse("EdgeBackOffsetY"), null);

// display() — the delimited UI form.
check("display inset", ParamNaming.display(ParamNaming.parse("BootCeiling_Front_Inset_Back")),
      "BootCeiling,Front-Inset-Back");
check("display radius", ParamNaming.display(ParamNaming.parse("Handle_Radius")),
      "Handle-Radius");

// allowedAnchors — symmetric axis drops the low-end word only.
check("anchors, Y symmetric", ParamNaming.allowedAnchors("Inset", { Y: true }).sort(),
      ["Back", "Left", "Right", "Top", "Bottom"].sort());
check("anchors, none symmetric", ParamNaming.allowedAnchors("Inset", {}).sort(),
      ["Right", "Left", "Back", "Front", "Top", "Bottom"].sort());
check("span has no anchors", ParamNaming.allowedAnchors("Span", {}), []);

// allowedAxes.
check("span axes", ParamNaming.allowedAxes("Span"), ["X", "Y", "Z"]);
check("inset has no axis step", ParamNaming.allowedAxes("Inset"), []);
check("radius has no axis step", ParamNaming.allowedAxes("Radius"), []);

// typesForUnit — filters by unit, falls back to everything on an unknown unit.
check("types for deg", ParamNaming.typesForUnit("deg"), ["Angle"]);
check("types for unitless", ParamNaming.typesForUnit("").sort(),
      ["Count", "Multiplier"].sort());
check("types for unknown unit fall back to all",
      ParamNaming.typesForUnit("furlong").sort(), ParamNaming.typesForUnit(null).sort());

// ── More than one direction ──
// "X and Y both work": a multi type's name carries every direction it is for.
check("parse two axes", ParamNaming.parse("Drawer_Width_Span_X_Y"),
      { levels: ["Drawer", "Width"], type: "Span", anchor: null, axis: "X", end: null,
        anchors: [], axes: ["X", "Y"], ends: [] });
roundTrip("Drawer_Width_Span_X_Y");
check("parse two anchors", ParamNaming.parse("Boot_Ceiling_Inset_Front_Back"),
      { levels: ["Boot", "Ceiling"], type: "Inset", anchor: "Front", axis: "Y", end: "low",
        anchors: ["Front", "Back"], axes: ["Y"], ends: ["low", "high"] });
roundTrip("Boot_Ceiling_Inset_Front_Back");
check("two anchors on different axes", ParamNaming.parse("Boot_Ceiling_Inset_Left_Top").axes,
      ["X", "Z"]);
check("a level before a two-word tail is still a level",
      ParamNaming.parse("Drawer_Front_Span_X_Y").levels, ["Drawer", "Front"]);
check("Count takes one axis only", ParamNaming.parse("Rack_Count_X_Y"), null);
check("an anchor and an axis are never mixed", ParamNaming.parse("Drawer_Span_X_Back"), null);
check("Span takes many", ParamNaming.takesMany("Span"), true);
check("Count takes one", ParamNaming.takesMany("Count"), false);
check("display two axes", ParamNaming.display(ParamNaming.parse("Drawer_Width_Span_X_Y")),
      "Drawer,Width-Span-X,Y");
// compose() from the stepper's own shape — the plural fields, no singular ones.
check("compose from axes", ParamNaming.compose(
  { levels: ["Drawer", "Width"], type: "Span", anchors: [], axes: ["X", "Y"] }),
  "Drawer_Width_Span_X_Y");

// ── Suggestion ──
// suggests() is the type step's question: which kinds of measurement belong in this
// situation. Only the datum rule — the axis is the step after.
var ORIGIN = { axis: "Y", end: "high", datum: "origin" };
var BOUNDS = { axis: "Y", end: "high", datum: "bounds" };
var UNKNOWN = { axis: "", end: "", datum: "" };
check("Offset is for a dimension off the origin", ParamNaming.suggests("Offset", ORIGIN), true);
check("...and not for one measured in from a bound", ParamNaming.suggests("Offset", BOUNDS), false);
check("Inset is the other way round", ParamNaming.suggests("Inset", BOUNDS), true);
check("Inset is not offered from the origin", ParamNaming.suggests("Inset", ORIGIN), false);
check("Span is offered either way", [ParamNaming.suggests("Span", ORIGIN),
                                     ParamNaming.suggests("Span", BOUNDS)], [true, true]);
check("an unknown datum disqualifies nothing",
      [ParamNaming.suggests("Offset", UNKNOWN), ParamNaming.suggests("Inset", UNKNOWN)],
      [true, true]);
check("suggestedTypes filters the unit's list",
      ParamNaming.suggestedTypes(BOUNDS, "mm").indexOf("Offset"), -1);
check("...and never returns nothing", ParamNaming.suggestedTypes(BOUNDS, "deg"), ["Angle"]);

// fits() is the value box's question: does this existing parameter belong here.
var fitsName = function (name, sit) { return ParamNaming.fits(ParamNaming.parse(name), sit); };
check("a General span (no axis) fits any dimension",
      fitsName("Drawer_Span", { axis: "X", end: "high", datum: "" }), true);
check("a Y span does NOT fit a plain X dimension",
      fitsName("Drawer_Width_Span_Y", { axis: "X", end: "high", datum: "", axes: ["X"] }), false);
check("a Y span DOES fit a 45°-diagonal dimension between X and Y",
      fitsName("Drawer_Width_Span_Y", { axis: "", end: "", datum: "", axes: ["X", "Y"] }), true);
check("an X offset does not fit a Y dimension",
      fitsName("Shelf_Offset_X", { axis: "Y", end: "high", datum: "origin" }), false);
check("a two-axis offset fits either of them",
      [fitsName("Shelf_Offset_X_Y", { axis: "Y", end: "", datum: "origin" }),
       fitsName("Shelf_Offset_X_Y", { axis: "Z", end: "", datum: "origin" })], [true, false]);
check("an anchored name fits the end it names",
      fitsName("Boot_Gear_Inset_Back", { axis: "Y", end: "high", datum: "bounds" }), true);
check("...and not the far end",
      fitsName("Boot_Gear_Inset_Back", { axis: "Y", end: "low", datum: "bounds" }), false);
check("two anchors fit both ends",
      [fitsName("Boot_Gear_Inset_Front_Back", { axis: "Y", end: "low", datum: "bounds" }),
       fitsName("Boot_Gear_Inset_Front_Back", { axis: "Y", end: "high", datum: "bounds" })],
      [true, true]);
check("a dimension with no axis rules nothing out",
      fitsName("Shelf_Offset_X", { axis: "", end: "", datum: "" }), true);
check("a name that doesn't parse is nobody's business", ParamNaming.fits(null, ORIGIN), true);

// branches — next level under a prefix, from already-parsed names.
check("branches", ParamNaming.branches(
  [{ levels: ["Drawer", "Gear"] }, { levels: ["Drawer", "Width"] }, { levels: ["Boot"] }],
  ["Drawer"]
).sort(), ["Gear", "Width"].sort());
check("branches, no match", ParamNaming.branches(
  [{ levels: ["Drawer", "Gear"] }], ["Boot"]
), []);

// ── nameWord / groupLevels / typeGroupAt / spanWordsFor ──
check("one entry after the owner is always a group, never a name",
      ParamNaming.nameWord(ParamNaming.parse("Drawer_Shelf_Radius")), null);
check("two entries after the owner: the last is the name",
      ParamNaming.nameWord(ParamNaming.parse("Drawer_Shelf_Curve_Radius")), "Curve");
check("a shared level chain is promoted to a group instead of a name", (function () {
  var a = ParamNaming.parse("Drawer_Shaft_Entry_Span_X");
  var b = ParamNaming.parse("Drawer_Shaft_Entry_Span_Y");
  var idx = ParamNaming.nameIndex([a, b]);
  return [ParamNaming.nameWord(a, idx), ParamNaming.nameWord(b, idx)];
})(), [null, null]);
check("a pinned name stays a name even when shared, and one entry can be pinned", (function () {
  var a = ParamNaming.parse("Drawer_Shaft__Entry_Span_X");
  var b = ParamNaming.parse("Drawer_Shaft__Entry_Span_Y");
  var idx = ParamNaming.nameIndex([a, b]);
  return [ParamNaming.nameWord(a, idx), ParamNaming.nameWord(b, idx),
          ParamNaming.nameWord(ParamNaming.parse("Drawer__Shelf_Radius"))];
})(), ["Entry", "Entry", "Shelf"]);
check("a pinned group stays a group even when alone",
      ParamNaming.nameWord(ParamNaming.parse("Drawer_Shelf_Curve__Radius")), null);
check("compose writes the pin back", [
  ParamNaming.compose(ParamNaming.parse("Drawer_Shelf__Curve_Radius")),
  ParamNaming.compose(ParamNaming.parse("Drawer_Shelf_Curve__Radius")),
  ParamNaming.compose(ParamNaming.parse("Drawer_Shelf_Curve_Radius"))],
  ["Drawer_Shelf__Curve_Radius", "Drawer_Shelf_Curve__Radius", "Drawer_Shelf_Curve_Radius"]);
check("Make-as-group: the type word as a level, found by typeGroupAt", (function () {
  var p = ParamNaming.parse("Drawer_Shaft_Span_Entry_Span_X");
  return [ParamNaming.typeGroupAt(p), ParamNaming.nameWord(p)];
})(), [2, "Entry"]);
check("Span's own axis reads as Width/Depth/Height",
      ParamNaming.spanWordsFor(ParamNaming.parse("Drawer_Span_X")), ["Width"]);
check("Span with several axes reads as several words",
      ParamNaming.spanWordsFor(ParamNaming.parse("Drawer_Span_Y_Z")), ["Depth", "Height"]);
check("a General span's words are []",
      ParamNaming.spanWordsFor(ParamNaming.parse("Drawer_Span")), []);
check("a non-Span type has no span words",
      ParamNaming.spanWordsFor(ParamNaming.parse("Handle_Radius")), null);
check("displayLeaf shows Span's axis, not the word Span",
      ParamNaming.displayLeaf(ParamNaming.parse("Drawer_Shaft_Entry_Span_X")), "Entry - Width");
check("displayLeaf on a type-grouped Span still shows the axis, not the word Span",
      ParamNaming.displayLeaf(ParamNaming.parse("Drawer_Shaft_Span_Entry_Span_X")), "Entry - Width");

// Per-part show/hide (the stepper's ⌘E): opts.show overrides the default reading.
const curve = ParamNaming.parse("Drawer_Front_Curve_Inset_Back");
check("leafShown by default: name and type said, the direction not (no twin)",
      ParamNaming.leafShown(curve, { direction: false }), { name: true, type: true, dir: false });
check("hiding the type leaves the name alone",
      ParamNaming.displayLeaf(curve, { direction: false, show: { type: false } }), "Curve");
check("showing a direction no twin needs",
      ParamNaming.displayLeaf(curve, { direction: false, show: { dir: true } }), "Curve - Backset");
check("hiding a direction a twin would show",
      ParamNaming.displayLeaf(curve, { show: { dir: false } }), "Curve - Inset");
check("a part the name lacks stays null whatever show says",
      ParamNaming.leafShown(ParamNaming.parse("Drawer_Shelf_Inset_Back"), { show: { name: true } }).name, null);
check("hiding everything still says the type",
      ParamNaming.displayLeaf(curve, { show: { name: false, type: false, dir: false } }), "Inset");
check("a Span's axis words are its direction part",
      ParamNaming.displayLeaf(ParamNaming.parse("Drawer_Shaft_Entry_Span_X"), { show: { dir: false } }), "Entry");

check("Inset + one anchor is said as one fused word",
      ParamNaming.displayLeaf(ParamNaming.parse("Drawer_Shelf_Inset_Back")), "Backset");
check("...with a name word in front",
      ParamNaming.displayLeaf(ParamNaming.parse("Drawer_Shelf_Curve_Inset_Back")), "Curve - Backset");
check("a fused word parses back to Inset + anchor",
      ParamNaming.parseLeaf("Backset"), {name: null, type: "Inset", anchors: ["Back"], axes: []});
check("...after a name word",
      ParamNaming.parseLeaf("Curve - Topset"), {name: "Curve", type: "Inset", anchors: ["Top"], axes: []});
check("the old spelling still parses",
      ParamNaming.parseLeaf("Inset - Back"), {name: null, type: "Inset", anchors: ["Back"], axes: []});

print(fails === 0 ? "all cases passed" : fails + " FAILURES");
