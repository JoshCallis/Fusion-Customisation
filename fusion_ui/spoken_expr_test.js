/* Cases for spoken_expr.js, against a real document's parameter names
 * (spoken_expr_params.json — a /params snapshot, names only). Run it:
 *
 *     /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc \
 *       fusion_ui/param_naming.js fusion_ui/spoken_expr.js fusion_ui/spoken_expr_test.js
 *
 * The readings are built the way fusion_value's spokenNames() builds them for the
 * list (owner first; a Bounds parameter as owner + face), so the matcher is tested
 * against what the window really hands it. */
var ROOT = "/Users/josh/Code/Scripts/fusion_ui/";
ParamNaming.setVocabulary(JSON.parse(read(ROOT + "param_naming.json")));
var NAMES = JSON.parse(read(ROOT + "spoken_expr_params.json"));

function readings(names) {
  var parsed = names.map(function (n) { return ParamNaming.parse(n); });
  var idx = ParamNaming.nameIndex(parsed.filter(Boolean));
  return names.map(function (n, i) {
    var parts = parsed[i];
    if (!parts) return { name: n, reading: ParamNaming.words(n) || n };
    var owner = parts.levels[0];
    var bounds = parts.levels.indexOf("Bounds") === 1;
    var levels = bounds ? parts.levels.slice(0, 1) : ParamNaming.groupLevels(parts, idx);
    if (owner === "TopLevel") levels = levels.filter(function (w) { return w !== "TopLevel"; });
    else if (owner && !levels.some(function (w) { return w.toLowerCase().indexOf(owner.toLowerCase()) === 0; }))
      levels = [owner].concat(levels);
    return { name: n, owner: owner,
             reading: levels.map(ParamNaming.words).concat([ParamNaming.displayLeaf(parts, { index: idx })]).join(" - ") };
  });
}
var CANDS = readings(NAMES);

var fails = 0, passes = 0;
function check(said, want, opts) {
  var r = SpokenExpr.convert(said, CANDS, opts);
  if (r.expr !== want) { print("FAIL \"" + said + "\": got " + JSON.stringify(r.expr) + " want " + JSON.stringify(want) + (r.unmatched.length ? "  unmatched " + JSON.stringify(r.unmatched) : "")); fails++; }
  else passes++;
}
function num(said, want) {
  var v = SpokenExpr.readNumber(said);
  if (v !== want) { print("FAIL number \"" + said + "\": got " + v + " want " + want); fails++; }
  else passes++;
}

// ── number words ─────────────────────────────────────────────────────────────
num("ten", 10); num("twenty five", 25); num("twenty-five", 25); num("one hundred and five", 105);
num("a hundred", 100); num("three thousand two hundred", 3200); num("two point five", 2.5);
num("point two five", 0.25); num("1,200", 1200); num("one and a half", 1.5); num("zero", 0);
num("nineteen", 19); num("seventy", 70);

// ── numbers and operators only ───────────────────────────────────────────────
check("ten plus five", "10 + 5");
check("twenty minus three point five", "20 - 3.5");
check("two times three", "2 * 3");
check("one hundred divided by four", "100 / 4");
check("ten millimetres", "10 mm");
check("2.5 cm plus 4", "2.5 cm + 4");
check("forty five degrees", "45 deg");
check("minus ten", "-10");
check("Ten.", "10");
check("half of a hundred", "50");

// ── parameters (bare words find the real names) ──────────────────────────────
check("depth", "Depth");
check("depth plus ten", "Depth + 10");
check("Depth plus 10.", "Depth + 10");
check("half the depth", "Depth / 2");
check("half the depth plus ten", "Depth / 2 + 10");
check("twice the depth", "Depth * 2");
check("double depth minus five", "Depth * 2 - 5");
check("two thirds of the depth", "Depth * 2 / 3");
check("ten percent of depth", "Depth * 10 / 100");
check("depth squared", "Depth ^ 2");
check("square root of depth", "sqrt(Depth)");
check("open bracket depth plus ten close bracket times two", "(Depth + 10) * 2");
check("depth minus steel thickness", "Depth - TopLevel_Materials_Steel_Thickness");
check("veneer thickness times two", "TopLevel_Materials_Veneer_Thickness * 2");
check("the MDF thickness plus the glue tape thickness",
      "TopLevel_Materials_MDF_Thickness + TopLevel_Materials_GlueTape_Thickness");
check("drawer floor thickness", "Drawer_Floor_Thickness");
check("wing width minus wing front web thickness", "WingWidth - WingFrontWebThickness");
check("nose right", "Nose_Bounds_Right_Offset_X");
check("the nose's right plus five", "Nose_Bounds_Right_Offset_X + 5");
check("core width over four", "Core_Dimension_Span_X / 4");
check("powerboard height", "Powerboard_Dimension_Span_Z");
check("main webs thickness times three", "Main_Webs_Thickness * 3");
check("interior gap", "TopLevel_Gaps_InteriorGap_Span");
check("half the interior gap", "TopLevel_Gaps_InteriorGap_Span / 2");
check("taper angle", "TopLevel_Profile_Taper_Angle");
// Slips: a plural, a one-letter mishearing.
check("steel thicknesses", "TopLevel_Materials_Steel_Thickness");
check("aluminium thickness", "TopLevel_Materials_Aluminum_Thickness");
check("profile radius", "TopLevel_Profile_Radius");

// ── the session's component wins a tie ───────────────────────────────────────
check("width", "Drawer_Bounds_Span_X", { owners: ["Drawer"] });
check("width", "Nose_Bounds_Span_X", { owners: ["Nose"] });
check("top", "Top");                 // a whole name said is that name

// ── pasted expressions pass through ──────────────────────────────────────────
check("Depth + 10", "Depth + 10");
check("Drawer_Floor_Thickness*2", "Drawer_Floor_Thickness * 2");
check("max(Depth, 10)", "max(Depth, 10)");
var plain = SpokenExpr.convert("Depth*2 + 1", CANDS);
if (plain.spoken) { print("FAIL plain expression read as spoken"); fails++; } else passes++;
var said = SpokenExpr.convert("half the depth", CANDS);
if (!said.spoken) { print("FAIL dictation not read as spoken"); fails++; } else passes++;

// ── nothing matches: kept as one word, reported ──────────────────────────────
var r = SpokenExpr.convert("banana plus ten", CANDS);
if (r.unmatched.join() !== "banana" || r.expr !== "Banana + 10") { print("FAIL unmatched: " + JSON.stringify(r)); fails++; } else passes++;

print(fails ? fails + " FAILED, " + passes + " passed" : "OK " + passes + " cases");
if (fails) throw new Error("spoken_expr: " + fails + " failed");
