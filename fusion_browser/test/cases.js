/* Assertions for suspend mode and ⌘C — the two pieces of this window with no live-
 * Fusion dependency. Everything else here (activation, selection, drag/drop, folder
 * filing) needs a real add-in behind it and is verified by hand in Fusion itself.
 */

var fails = 0;
function expect(label, got, want) {
  var ok = String(got) === String(want);
  if (!ok) fails++;
  print((ok ? "  PASS  " : "  FAIL  ") + label +
        (ok ? "" : "\n          got  " + got + "\n          want " + want));
}

// A recording spy in place of the network — dom_shim's own fetch always rejects,
// which is exactly "nothing was sent", so it has to be replaced to tell "nothing
// sent because suspended" apart from "would have sent, but the shim eats it anyway".
var sent = [];
fetch = function(url, opts) {
  sent.push({ url: url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  return Promise.resolve({ json: function () { return Promise.resolve({ ok: true }); } });
};

print("=== post() drops viewport/selection/sketch actions while suspended ===");
suspended = false;
sent = [];
post({ action: "activate", token: "x" });
expect("not suspended: reaches fetch", sent.length, 1);

suspended = true;
sent = [];
post({ action: "activate", token: "x" });
expect("suspended: activate is dropped", sent.length, 0);
post({ action: "selectbody", token: "x" });
post({ action: "selectsketch", token: "x" });
post({ action: "entersketch", token: "x" });
post({ action: "deselect" });
post({ action: "setvis", token: "x" });
post({ action: "togglevis", token: "x" });
expect("suspended: none of the viewport family got through", sent.length, 0);
post({ action: "rename", token: "x", name: "Y" });
expect("suspended: a content edit (rename) still goes through — not a navigation action",
       sent.length, 1);
suspended = false;

print("");
print("=== suspended: navigation PEEKS — never activates, never enters ===");
// A ─┬─ B        (B is the real active component)
//    └─ D ── E
state.tree = [{ token: "A", type: "component", name: "A", children: [
  { token: "B", type: "component", name: "B", children: [] },
  { token: "D", type: "component", name: "D", children: [
    { token: "E", type: "component", name: "E", children: [] }] }] }];
state.activeToken = "B"; prevActiveToken = "B";
state.peekToken = null; pendingPeek = null;
suspended = true;
var cmds = function () { return sent.filter(function (s) { return /\/cmd$/.test(s.url); }); };
var actions = function () { return cmds().map(function (s) { return s.body.action; }).join(","); };
var firstTok = function () { var c = cmds()[0]; return c && c.body.token; };

sent = [];
activateComponentTok("E");
expect("activateComponentTok: activeToken untouched", state.activeToken, "B");
expect("...posts a peek, not an activate", actions(), "peek");
expect("...with the row's token", firstTok(), "E");
expect("...and the tree roots on the peek", viewTok(), "E");

sent = [];
goUpOneLevel(true);
expect("goUpOneLevel: peeks the parent of the PEEK (D), not of the active (A)",
       firstTok(), "D");
expect("...only a peek was sent (no activate, no selection)", actions(), "peek");
expect("...activeToken still untouched", state.activeToken, "B");

sent = [];
enterSketch({ token: "sketch:1", occToken: "E" });
expect("enterSketch: peeks the sketch's component", firstTok(), "E");
expect("...never enters it", actions(), "peek");
expect("...editingSketch untouched", state.editingSketch, false);

sent = [];
activateRootUI();
expect("activateRootUI: peeks the root", firstTok(), "__root__");
expect("...the view is the root", viewTok(), "");
expect("...no deselect / activate", actions(), "peek");

print("");
print("=== suspended: a row CLICK on a component peeks too ===");
sent = [];
handleClick({ tok: "A" });
expect("click on A (holds the active B): peeks A", actions(), "peek");
expect("...with A's token", firstTok(), "A");
expect("...activeToken untouched (no underline moves)", state.activeToken, "B");
expect("...the view is A", viewTok(), "A");

sent = [];
collapsed.delete("A");
handleClick({ tok: "A" });
expect("click on the view row (A): folds it, posts nothing", sent.length, 0);
expect("...it is folded", collapsed.has("A"), true);
collapsed.delete("A");

sent = [];
handleClick({ tok: "B" });
expect("click on the ACTIVE row while peeked elsewhere: peeks it", actions(), "peek");
expect("...which ends the peek on this side", state.peekToken, null);
expect("...and nothing waits for a confirmation", pendingPeek, null);
expect("...the view is the active component again", viewTok(), "B");

print("");
print("=== suspended: the rest still no-op entirely ===");
sent = [];
toggleVisCursor();
expect("toggleVisCursor: nothing was sent", sent.length, 0);

selected = new Set(["comp:1"]);
deselectAll();
expect("deselectAll: the selection survives", selected.size, 1);
expect("...and nothing was sent", sent.length, 0);
selected.clear();

print("");
print("=== the toggle and its visual class ===");
suspended = false;
document.body.classList.toggle = function (cls, on) { document.body._suspendedClass = !!on; };
toggleSuspend();
expect("toggleSuspend flips the flag on", suspended, true);
expect("...and sets the class", document.body._suspendedClass, true);
toggleSuspend();
expect("toggleSuspend flips it back off", suspended, false);
expect("...and clears the class", document.body._suspendedClass, false);

print("");
print("=== unsuspending jumps back: the peek is cleared ===");
suspended = true;
state.activeToken = "B"; state.peekToken = "E"; pendingPeek = { token: "E", ts: Date.now() };
expect("suspended with a peek: the view is the peek", viewTok(), "E");
sent = [];
setSuspended(false);
expect("off: the peek is gone", state.peekToken, null);
expect("...nothing held for it", pendingPeek, null);
expect("...the view is the active component again", viewTok(), "B");
expect("...and the add-in is told", cmds().length && cmds()[0].body.action + ":" + cmds()[0].body.on,
       "suspend:false");
state.tree = []; state.activeToken = ""; prevActiveToken = "";

print("");
print("=== feature rows: a component's Geometry group ===");
// Here, before the sections below stub nodeByTok and refreshFolders: this needs the
// real tree.
rawTree = [{ token: "occF", type: "component", name: "Box", compId: "cF", visible: true,
  children: [{ token: "bg:cF", type: "geometry", name: "Bodies", compId: "cF", visible: true, children: [
    { token: "bodyA", type: "body", name: "Panel", key: "kA", occToken: "occF", visible: true, children: [] },
    { token: "bodyB", type: "body", name: "Lid",   key: "kB", occToken: "occF", visible: true, children: [] }
  ]}, { token: "fx:cF", type: "features", name: "Operations", compId: "cF", occToken: "occF",
        visible: true, children: [] }]}];
state.activeToken = "occF"; prevActiveToken = "occF";
collapsed = new Set(); openGeos = new Set(); suspended = false;
featsByGeo = { "fx:cF": [
  { tok: "f1", name: "Extrude1", type: "adsk::fusion::ExtrudeFeature", phase: 4 },
  { tok: "f2", name: "Fillet2",  type: "adsk::fusion::FilletFeature",  phase: 4 },
  { tok: "f3", name: "Combine3", type: "adsk::fusion::CombineFeature", phase: 5 }
]};
refreshFolders();
function shown() {
  return flatten().filter(function (r) { return !r.placeholder; })
                  .map(function (r) { return r.node.name; }).join(",");
}
function sentTo(action) {
  return sent.filter(function (s) { return s.body && s.body.action === action; });
}
var SHUT = "Operations,Bodies,Panel,Lid", OPEN = "Operations,Extrude1,Fillet2,Combine3,Bodies,Panel,Lid";
expect("Operations starts shut, above Bodies", shown(), SHUT);
sent = [];
handleClick({ tok: "bodyA" });
expect("a click selects the body in the viewport", sentTo("selectbody").length, 1);
expect("...and opens nothing", shown(), SHUT);
handleClick({ tok: "fx:cF" });
expect("a click on Geometry opens it: the features, in timeline order", shown(), OPEN);
expect("...and doesn't select the group", selected.has("fx:cF"), false);
handleClick({ tok: "fx:cF" });
expect("a second click shuts it", shown(), SHUT);

sent = [];
cursorTok = "bodyA"; selected = new Set(["bodyA"]);
expandOrInto();
expect("F on a body opens its component's Geometry group", shown(), OPEN);
var w = sentTo("watch")[0];
expect("...and the Timeline add-in is told which component to scan",
       w ? w.url.indexOf("8736") >= 0 && JSON.stringify(w.body.comps) : "",
       JSON.stringify([{ key: "fx:cF", comp: "cF" }]));
expect("a feature row reads as the timeline names it", displayName("Extrude1"), "Extrude");

var f1 = FEAT_PREFIX + "fx:cF|f1", f2 = FEAT_PREFIX + "fx:cF|f2";
sent = [];
handleClick({ tok: f1 });
var sf = sentTo("selectfeature")[0];
expect("clicking a feature selects IT in the viewport, in its component's context",
       sf ? sf.body.tok + " " + sf.body.occTok : "", "f1 occF");

sent = [];
activateCursor();
var ed = sentTo("edit")[0];
expect("Enter on the picked feature edits it, through the Timeline add-in",
       ed ? ed.url.indexOf("8736") >= 0 && ed.body.tok + " " + ed.body.occTok : "", "f1 occF");
expect("...and doesn't just select it again", sentTo("selectfeature").length, 0);

sent = [];
expandOrInto();
var es = sentTo("editsource")[0];
expect("F on the picked feature edits the sketch it was made from",
       es ? es.url.indexOf("8736") >= 0 && es.body.tok + " " + es.body.occTok : "", "f1 occF");
expect("...not the feature itself", sentTo("edit").length, 0);

// A feature picked where its row isn't drawn (Trace, the dock's timeline) leaves
// nothing picked here; F still edits its sketch through the Timeline add-in.
sent = [];
cursorTok = null; selected = new Set();
state.selQuiet = true; state.selFeats = ["f9"]; state.selTokens = [];
expandOrInto();
var ev = sentTo("editsource")[0];
expect("F with a feature picked only in the viewport edits its sketch",
       ev ? ev.body.tok : "", "f9");
state.selQuiet = false; state.selFeats = [];

sent = [];
cursorTok = "fx:cF"; selected = new Set(["fx:cF"]);
var origST = setTimeout, fired = null;
setTimeout = function (fn) { fired = fn; return 1; };
moveCursor(1);
setTimeout = origST;
if (fired) fired();
var cs = sentTo("selectfeature")[0];
expect("C onto a feature selects it in the viewport, through the Timeline add-in",
       cs ? cs.body.tok + " " + cs.body.occTok : "", "f1 occF");
expect("...and doesn't clear the viewport instead", sentTo("selectmany").length, 0);
expect("...and the row stays picked here", [...selected].join(), f1);

sent = [];
cursorTok = f1; selected = new Set([f1]);
moveSelectionInList(1);
expect("⇧↓ on a feature never reorders the real timeline (drag only)",
       sentTo("featreorder").length, 0);

// A drop draws the rows where they were dropped, spinning, until Fusion answers.
sent = [];
applyDrop({ tok: f1, toks: [f1] }, { mode: "after", tok: f2, feat: true, featTok: "f2" });
var fr = sentTo("featreorder")[0];
expect("dropping a feature asks the add-in to move it, with an id for the verdict",
       fr ? fr.body.toks.join() + " after " + fr.body.after + " " + !!fr.body.id : "",
       "f1 after f2 true");
expect("...and draws it there at once", shown(), "Operations,Fillet2,Extrude1,Combine3,Bodies,Panel,Lid");
expect("...marked as moving", nodeByTok(f1).moving, true);
expect("...and no second feature drop while it's pending", featDrop({ clientX: 0, clientY: 0 }), null);
endFeatMove(featMove, "Fusion won't allow that order (a dependency would break).");
expect("a refusal puts it back", shown(), OPEN);
expect("...with the reason on the row", nodeByTok(f1).moveErr,
       "Fusion won't allow that order (a dependency would break).");
expect("...no longer moving", nodeByTok(f1).moving, false);
featMoveErr = null; refreshFolders();

sent = [];
deleteTokens([f2], true);
var dl = sentTo("delete")[0];
expect("deleting a feature row deletes the feature, through the Timeline add-in",
       dl ? dl.url.indexOf("8736") >= 0 && dl.body.toks.join() : "", "f2");

sent = [];
cursorTok = f1; selected = new Set([f1]);
collapseOrOut();
expect("D on a feature shuts its Geometry group", shown(), SHUT);
expect("...and picks the group", [...selected].join(), "fx:cF");

cursorTok = "bodyA"; selected = new Set(["bodyA"]);
expandOrInto();
revealActiveSpine();
expect("arriving at a level shuts every open Geometry group", openGeos.size, 0);
collapsed.delete("bg:cF");                     // (the tidy shuts Bodies too)
expect("...so reopening Bodies shows it shut", shown(), SHUT);

showBodiesGroup = false; showGeometryGroup = false;
expect("the view toggles drop both groups", shown(), "");
showBodiesGroup = true; showGeometryGroup = true;

state.editingSketch = true;
expect("editing a sketch hides Bodies and Geometry too", shown(), "");
state.editingSketch = false;
expect("...and leaving the sketch brings them back", shown(), SHUT);

print("");
print("=== a viewport pick opens nothing inside a shut top-level group ===");
// A folder inside Bodies, drawn straight into the tree (refreshFolders rebuilds it after).
nodeByTok("bg:cF").children.push({ token: "fld:t1", type: "folder", name: "Sub",
  visible: true, children: [{ token: "bodyC", type: "body", name: "Rail", key: "kC",
                               occToken: "occF", visible: true, children: [] }] });
collapsed = new Set(["bg:cF", "fld:t1"]); openGeos = new Set();
state.selTokens = ["bodyC"]; selected = new Set(["bodyC"]);
revealSelection(["bodyC"]);
expect("a body picked with Bodies shut leaves Bodies shut",
       collapsed.has("bg:cF") + " " + collapsed.has("fld:t1"), "true true");
handleClick({ tok: "bg:cF" });
expect("opening Bodies by hand opens on down to the pick",
       collapsed.has("bg:cF") + " " + collapsed.has("fld:t1"), "false false");
collapsed.add("fld:t1");
revealSelection(["bodyC"]);
expect("with Bodies open, a pick opens the folder it is in", collapsed.has("fld:t1"), false);

state.selTokens = ["bodyA"]; selected = new Set(["bodyA"]);
state.selPick = { kind: "part", feat: "f1" };
openPickedFeature();
expect("a face pick with Geometry shut leaves it shut", openGeos.size, 0);
expect("...and the body keeps the pick", [...selected].join(), "bodyA");
handleClick({ tok: "fx:cF" });
expect("opening Geometry by hand lands on the pick's feature", [...selected].join(), f1);
expect("...with the group open", openGeos.has("fx:cF"), true);
selected = new Set(["bodyA"]);
openPickedFeature();
expect("with Geometry open, a face pick hands straight to its feature", [...selected].join(), f1);
state.selPick = null; state.selTokens = []; selected = new Set(); cursorTok = null;
openGeos = new Set(); collapsed = new Set(); pickBody = null; pickEdit = null;
refreshFolders();

print("");
print("=== new bodies land in their source's group (BrowserFileNewBodiesCmd) ===");
(function () {
  var oldTree = rawTree, oldReady = foldersReady, oldDoc = foldersDoc;
  function body(n) {
    return { token: "t" + n, type: "body", name: n, key: "cF|bd|" + n, occToken: "occF",
             visible: true, children: [] };
  }
  function load(names) {
    rawTree = [{ token: "occF", type: "component", name: "Box", compId: "cF", visible: true,
      children: [{ token: "bg:cF", type: "geometry", name: "Bodies", compId: "cF",
                   occToken: "occF", visible: true, children: names.map(body) }]}];
  }
  function folderNamed(name, parent) {
    return Object.keys(folderDefs).filter(function (k) {
      return folderDefs[k].name === name && folderDefs[k].parent === parent; })[0] || "";
  }
  var olds = ["A", "B", "C", "D", "E", "F"], news = ["A2", "B2", "C2", "D2", "E2", "F2"];
  foldersReady = true; foldersDoc = "doc";
  folderDefs = { g1: { id: "g1", name: "Legs", comp: "cF", parent: GEO_PARENT, hidden: false, kind: "" } };
  leafFolder = {}; siblingOrder = {};
  olds.forEach(function (n) { leafFolder["cF|bd|" + n] = "g1"; });
  leafFolder["cF|bd|Z"] = "g1";
  load(olds.concat(["Z", "Z2", "Loose", "Loose2"]));
  refreshFolders();

  // A small one: every body into its source's group, loose stays loose.
  ACTION_IMPL.BrowserFileNewBodiesCmd(null, { bodies: [
    { key: "cF|bd|Z2", from: "cF|bd|Z" }, { key: "cF|bd|Loose2", from: "cF|bd|Loose" }],
    mirrors: [] });
  expect("a copy files into its source's group", leafFolder["cF|bd|Z2"], "g1");
  expect("a copy of a loose body stays loose", leafFolder["cF|bd|Loose2"], undefined);

  // A mirror of six: Right / Left subgroups inside Legs.
  load(olds.concat(news, ["Z", "Z2"]));
  refreshFolders();
  ACTION_IMPL.BrowserFileNewBodiesCmd(null, {
    bodies: news.map(function (n, i) { return { key: "cF|bd|" + n, from: "cF|bd|" + olds[i] }; }),
    mirrors: [{ made: news.map(function (n) { return "cF|bd|" + n; }),
                srcSide: "Right", newSide: "Left" }] });
  var right = folderNamed("Right", "g1"), left = folderNamed("Left", "g1");
  expect("a big mirror makes Right and Left inside the originals' group", !!right && !!left, true);
  expect("...the originals move into Right",
         olds.every(function (n) { return leafFolder["cF|bd|" + n] === right; }), true);
  expect("...the copies into Left",
         news.every(function (n) { return leafFolder["cF|bd|" + n] === left; }), true);
  expect("...a body that wasn't mirrored stays put", leafFolder["cF|bd|Z"], "g1");
  expect("...Right sits before Left",
         siblingOrder["fld:g1"].indexOf("fld:" + right) < siblingOrder["fld:g1"].indexOf("fld:" + left), true);

  // Mirroring the Right group again: the copies go beside it, not inside it.
  var again = ["A3", "B3", "C3", "D3", "E3", "F3"];
  load(olds.concat(news, again, ["Z", "Z2"]));
  refreshFolders();
  ACTION_IMPL.BrowserFileNewBodiesCmd(null, {
    bodies: again.map(function (n, i) { return { key: "cF|bd|" + n, from: "cF|bd|" + olds[i] }; }),
    mirrors: [{ made: again.map(function (n) { return "cF|bd|" + n; }),
                srcSide: "Right", newSide: "Left" }] });
  expect("originals already in Right: copies join the Left beside it",
         again.every(function (n) { return leafFolder["cF|bd|" + n] === left; }), true);
  expect("...and no Right inside Right", folderNamed("Right", right), "");

  rawTree = oldTree; foldersReady = oldReady; foldersDoc = oldDoc;
  folderDefs = {}; leafFolder = {}; siblingOrder = {};
  refreshFolders();
})();

print("");
print("=== Cycle Visibility (groupOff) dims a row — additive, not instead of a strike ===");
// Distinct from the view toggles above: those are THIS window's own display filter
// and drop rows outright. groupOff is the real Visibility Snapshot cycle_visibility
// hotkey — Bodies/Sketches temporarily off for the active component — and the row
// stays in the tree either way. groupDim is unconditional (every switched-off row
// dims, hidden by hand or not); strikeExemptFromGroup only keeps a merely-switched-
// off row from ALSO striking when its own bulb is still on.
function rowFor(tok) {
  return flatten().filter(function (r) { return !r.placeholder; })
                  .find(function (r) { return r.node.token === tok; });
}
var bodyNode = rowFor("bodyA").node, bgNode = rowFor("bg:cF").node;
bodyNode.groupOff = true; bodyNode.visible = false;
bgNode.groupOff = true;
var rBody = rowFor("bodyA"), rBg = rowFor("bg:cF");
expect("a visible-bulb body whose group toggle is off dims, not strikes",
       [rBody.offView, rBody.effHidden, rBody.groupDim].join(), [false, false, true].join());
expect("so does the Bodies group row itself (its bulb IS the folder bulb)",
       [rBg.offView, rBg.effHidden, rBg.groupDim].join(), [false, false, true].join());
bodyNode.userHidden = true;
var rBody2 = rowFor("bodyA");
expect("...a body ALSO hidden by hand keeps its strike AND still dims",
       [rBody2.offView, rBody2.effHidden, rBody2.groupDim].join(), [true, true, true].join());
bodyNode.groupOff = false; bodyNode.visible = true; bodyNode.userHidden = false;
bgNode.groupOff = false;
expect("back to normal once the group and the body are both on again",
       [rowFor("bodyA").offView, rowFor("bodyA").effHidden, rowFor("bodyA").groupDim].join(),
       [false, false, false].join());
expect("groupOffIn: a folder holding a switched-off sketch reads as switched off too",
       groupOffIn({ type: "folder", children: [
         { type: "sketch", groupOff: true },
         { type: "sketch", groupOff: false }
       ]}), true);
expect("...recurses into nested folders", groupOffIn({ type: "folder", children: [
  { type: "folder", children: [{ type: "body", groupOff: true }] }
]}), true);
expect("...nothing switched off inside: false", groupOffIn({ type: "folder", children: [
  { type: "sketch", groupOff: false }, { type: "folder", children: [] }
]}), false);

print("");
print("=== jumpToCursor: the escape hatch runs once, then suspend mode resumes ===");
suspended = true;
sent = [];
cursorTok = "comp:1";
var origActivate = activateComponentTok;
var activateCalledWith = null;
activateComponentTok = function (tok) { activateCalledWith = tok; };  // stub the real work
// activateCursor() dispatches by node type; fake a component node for the cursor.
nodeByTok = function (tok) { return tok === "comp:1" ? { type: "component" } : null; };
jumpToCursor();
expect("the real action ran once despite being suspended", activateCalledWith, "comp:1");
expect("suspend mode is back on afterward", suspended, true);
activateComponentTok = origActivate;

print("");
print("=== ⌘C copies the selected parameter's name, not its value ===");
suspended = false;
sent = [];
selected = new Set(["param:Boot_Width_Span_X"]);
cursorTok = null;
paramRows = [{ name: "Boot_Width_Span_X", expression: "400 mm" }];
copySelectionName();
expect("posts to /clipboard", sent.length && sent[0].url.indexOf("/clipboard") >= 0, true);
expect("carries the parameter's NAME", sent[0].body.text, "Boot_Width_Span_X");
selected.clear();

sent = [];
copySelectionName();
expect("nothing selected, nothing to copy: no request at all", sent.length, 0);

print("");
print("=== name groups: the tree is read off the parameter names ===");
/* The one part of this tree that needs no Fusion at all — a parameter's groups and its
 * label are a reading of its name, so the whole layer can be driven from a fake
 * component index and a list of names. What is checked here is the READING and the
 * REWRITES (move / rename / explode / collision), not the drawing. */
ParamNaming.setVocabulary(PARAM_NAMING_VOCAB);
paramNamingReady = true;
partsCache = new Map();

// The refusal dialog is DOM and the shim auto-vivifies every element, so the real one
// would always read as open. Recorded instead: what is asserted below is that the
// gesture was refused and SAID WHY, which is the part that isn't markup.
var blocked = null;
showBlocked = function (what, why) { blocked = { what: what, why: why }; };
hideBlocked = function () { blocked = null; };
blockedOpen = function () { return !!blocked; };

// Two components and a root, standing in for the tree. rebuildIdentIndex reads
// compByCid and state.rootName; compRefOf reads compByTok, which is how an occurrence
// token (what a drop target is) gets back to a component id.
state.rootName = "Cabinet";
compByCid = { c1: { name: "Drawer", compId: "c1", token: "occ1" },
              c2: { name: "Boot Ceiling", compId: "c2", token: "occ2" } };
compByTok = { occ1: compByCid.c1, occ2: compByCid.c2 };
// The real refreshFolders rebuilds these indexes from the live tree; there is no tree
// here, so it would wipe the stand-ins above. Replaced with just the part this layer
// is about — same two rebuilds, off the stand-ins.
refreshFolders = function () {
  partsCache = new Map(); rebuildIdentIndex(); rebuildNameIndex();
};
paramRows = [
  { name: "Drawer_Front_Curve_Inset_Back", expression: "10 mm" },
  { name: "Drawer_Front_Span_X",           expression: "20 mm" },
  { name: "Drawer_Width_Span_X_Y",         expression: "30 mm" },
  { name: "Drawer_Width_Span_Z",           expression: "35 mm" },   // Width - Span's twin
  { name: "BootCeiling_Handle_Radius",     expression: "4 mm"  },
  { name: "Cabinet_Shell_Thickness",       expression: "18 mm" },
  { name: "Ghost_Panel_Offset_Z",          expression: "2 mm"  },   // owner: no such component
  { name: "SomeOldName",                   expression: "1 mm"  }    // not this convention
];
rebuildIdentIndex();
rebuildNameIndex();

expect("a component name with a space is matched by its identifier",
       compByIdent["BootCeiling"], "c2");
expect("the root component is matched too", compByIdent["Cabinet"], "__root__");

// Where each one files. The last level is the parameter's NAME, not a group — UNLESS
// it is the only entry after the owner: one entry alone is always a group (Cabinet_
// Shell_Thickness, Ghost_Panel_Offset_Z), never a name. Two or more, the last is the
// name (Drawer_Front_Curve_Inset_Back: Front the group, Curve the name).
expect("owner level picks the component, the last level is the name",
       JSON.stringify(homeOfParts(partsOf("Drawer_Front_Curve_Inset_Back"), nameIdx)),
       JSON.stringify({ ref: "c1", bucket: "c1", words: ["Front"], name: "Curve" }));
expect("a root-owned name with only one entry is a group, not a name",
       JSON.stringify(homeOfParts(partsOf("Cabinet_Shell_Thickness"), nameIdx)),
       JSON.stringify({ ref: "__root__", bucket: "__root__", words: ["Shell"], name: null }));
expect("an owner with no component keeps its whole chain, in the orphan bucket",
       JSON.stringify(homeOfParts(partsOf("Ghost_Panel_Offset_Z"), nameIdx)),
       JSON.stringify({ ref: "?", bucket: "?", words: ["Ghost", "Panel"], name: null }));

// The rows one container draws, top level first.
function shape(ref, path) {
  return nameGroupNodes(ref, path || "").map(function (n) {
    return n.type === "param" ? n.name : n.name + "/";
  }).join(" ");
}
expect("Drawer's one-entry names are groups too, so nothing draws at its top level",
       shape("c1"), "Front/ Width/");
expect("...the Front group holds its own name (Curve) and its group-mate with none",
       shape("c1", "Front"), "Curve - Inset Width");
expect("...Width's own rows read as Span's axis words, no name of their own",
       shape("c1", "Width"), "Width, Depth Height");
expect("a CamelCase owner, a one-entry name is a group holding its row",
       shape("c2"), "Handle/");
expect("...its row has no name of its own either", shape("c2", "Handle"), "Radius");
expect("a root-owned one-entry name is a group at the top level too",
       shape("__root__"), "Shell/");
expect("...holding its Thickness row, unparsed rows left to the hand filing",
       shape("__root__", "Shell"), "Thickness");

// An unparsed name still files by hand; a parsed one never does (or it would draw twice).
paramPlace = { SomeOldName: "c1" };
expect("only the unparsed row comes back from the hand filing",
       paramNodesFor("c1").map(function (n) { return n.name; }).join(","), "SomeOldName");
paramPlace = {};

// Rolled back in the timeline, a component missing from the tree may not exist YET:
// what is tied to it is hidden, not spilled onto the top level.
function topRows() {
  return graftFolders("__root__", []).map(function (n) { return n.name; }).join(",");
}
paramPlace = { SomeOldName: "cGone" };
expect("at the timeline's end, a parameter filed in a gone component falls to the top",
       paramNodesFor("").map(function (n) { return n.name; }).join(","), "SomeOldName");
expect("...and the orphan chain draws at the top level", topRows().indexOf("Ghost") >= 0, true);
state.rolledBack = true;
rebuildNameIndex();
expect("rolled back, the parameter filed in the missing component is hidden",
       paramNodesFor("").length, 0);
expect("...and so is the orphan chain", topRows().indexOf("Ghost") >= 0, false);
state.rolledBack = false;
rebuildNameIndex();
paramPlace = {};

print("");
print("=== moving a parameter rewrites its name, and the tree follows ===");
sent = [];
expect("into another component: the owner level changes",
       moveParamsByName(["Drawer_Front_Span_X"], "occ2"), true);
// Dropped on the component itself, so it lands at that component's top level and the
// groups it used to be in are gone from the name along with them. "Front" was only
// ever the group here (one entry after the owner), never a name, so nothing of it
// survives the move.
expect("...and that is what was sent to Fusion — no name of its own to keep",
       JSON.stringify(sent[0].body), JSON.stringify({ name: "Drawer_Front_Span_X",
                                                      newName: "BootCeiling_Span_X" }));

sent = [];
expect("into a name group: the whole chain changes",
       moveParamsByName(["BootCeiling_Handle_Radius"], nameTok("c1", ["Front"])), true);
expect("...to the group's own levels — Handle was a group too, not a name, so it drops",
       sent[0].body.newName, "Drawer_Front_Radius");

sent = [];
expect("to the top level: the root component becomes the owner",
       moveParamsByName(["Drawer_Width_Span_X_Y"], "__root__"), true);
expect("...named for the root, and at its top level like any other component drop",
       sent[0].body.newName, "Cabinet_Span_X_Y");

print("");
print("=== renaming and exploding a group rewrite every name under it ===");
// Rebuilt from scratch: the moves above rewrote paramRows in place.
function groupRows() {
  return [
    { name: "Drawer_Front_Curve_Inset_Back", expression: "10 mm" },
    { name: "Drawer_Front_Lid_Span_X",       expression: "20 mm" },
    // One entry after the owner is always a group now — Front is Drawer's group here
    // too, same as the other two, holding a nameless "Span X" row of its own.
    { name: "Drawer_Front_Span_X",           expression: "25 mm" }
  ];
}
paramRows = groupRows();
partsCache = new Map(); rebuildIdentIndex(); rebuildNameIndex();

sent = [];
expect("rename takes the typed words and joins them back",
       renameNameGroup(nameTok("c1", ["Front"]), "front face"), true);
expect("...in every name beneath it, at that one level — never a name word",
       sent.map(function (s) { return s.body.newName; }).sort().join(" "),
       "Drawer_FrontFace_Curve_Inset_Back Drawer_FrontFace_Lid_Span_X Drawer_FrontFace_Span_X");

paramRows = groupRows();
partsCache = new Map(); rebuildNameIndex();
sent = [];
expect("explode takes the level out altogether",
       explodeNameGroup(nameTok("c1", ["Front"])), true);
expect("...so its parameters and subgroups lift into what held it",
       sent.map(function (s) { return s.body.newName; }).sort().join(" "),
       "Drawer_Curve_Inset_Back Drawer_Lid_Span_X Drawer_Span_X");

print("");
print("=== a move that would collide is refused whole, with the dialog up ===");
paramRows = [
  { name: "Drawer_Front_Lid_Span_X", expression: "20 mm" },
  { name: "Drawer_Back_Lid_Span_X",  expression: "30 mm" },
  // A DIFFERENT group ("Frame", not "Lid") — sharing "Lid" with Back_Lid_Span_X would
  // promote it to a group instead (two parameters, one chain), which is its own rule
  // (see NAME_INDEX_CASES) and not what this section is testing.
  { name: "Drawer_Back_Frame_Inset_Back", expression: "5 mm" }
];
partsCache = new Map(); rebuildNameIndex();
sent = [];
blocked = null;
expect("moving onto a name that already exists is refused",
       moveParamsByName(["Drawer_Front_Lid_Span_X"], nameTok("c1", ["Back"])), false);
expect("...and NOTHING was sent", sent.length, 0);
expect("...and it says which name is in the way",
       blocked && blocked.what === "Move"
               && blocked.why.indexOf("Drawer_Back_Lid_Span_X") >= 0, true);

sent = []; blocked = null;
expect("two parameters landing on one name is refused before either goes",
       moveParamsByName(["Drawer_Front_Lid_Span_X", "Drawer_Back_Lid_Span_X"],
                        nameTok("c1", ["Side"])), false);
expect("...nothing sent for the one that would have been fine either",
       sent.length, 0);
expect("...and it names both of them",
       blocked && blocked.why.indexOf("Drawer_Front_Lid_Span_X") >= 0
               && blocked.why.indexOf("Drawer_Back_Lid_Span_X") >= 0, true);

sent = [];
expect("a move with no collision still goes",
       moveParamsByName(["Drawer_Front_Lid_Span_X"], nameTok("c1", ["Side"])), true);
expect("...one rename", sent.length, 1);

print("");
print("=== renaming a row edits its LEAF, never its groups ===");
// Front is a GROUP here (one entry after the owner) — renaming the leaf to "Curve -
// Inset" gives the parameter a name INSIDE that group; Front itself is untouched.
paramRows = [{ name: "Drawer_Front_Inset_Back", expression: "10 mm" }];
partsCache = new Map(); rebuildNameIndex();
sent = [];
expect("typed without its hidden direction, the row keeps it, its group untouched",
       renameParamLeaf("param:Drawer_Front_Inset_Back", "Curve - Inset"),
       "Drawer_Front_Curve_Inset_Back");
// Front + Curve is a group and a name (two entries) — stripping the name from the
// leaf text takes only that word out; Front stays.
paramRows = [{ name: "Drawer_Front_Curve_Inset_Back", expression: "10 mm" }];
partsCache = new Map(); rebuildNameIndex();
sent = [];
expect("a name word taken out of the text comes out of the name, the group stays",
       renameParamLeaf("param:Drawer_Front_Curve_Inset_Back", "Inset"), "Drawer_Front_Inset_Back");

paramRows = [{ name: "Drawer_Front_Curve_Inset_Back", expression: "10 mm" }];
partsCache = new Map(); rebuildNameIndex();
sent = [];
expect("the leaf is put back under the groups it already had",
       renameParamLeaf("param:Drawer_Front_Curve_Inset_Back", "Curve - Outset - Z"),
       "Drawer_Front_Curve_Outset_Z");
expect("...which is what Fusion is told", sent[0].body.newName,
       "Drawer_Front_Curve_Outset_Z");

sent = []; blocked = null;
expect("a leaf the convention can't read is refused",
       renameParamLeaf("param:Drawer_Front_Curve_Outset_Z", "Wibble - Back"), "");
expect("...nothing sent", sent.length, 0);
expect("...and the refusal quotes what was typed",
       blocked && blocked.why.indexOf("Wibble - Back") >= 0, true);

print("");
print("=== parameter groups: folders are split once, and the two kinds never mix ===");
// nodeByTok was stubbed for jumpToCursor above; the real one walks state.tree.
nodeByTok = function (tok) {
  var found = null;
  (function w(ns) { for (var i = 0; i < ns.length; i++) { var n = ns[i];
    if (n.token === tok) { found = n; return; } if (n.children) w(n.children); } })(state.tree);
  return found;
};
foldersReady = true; foldersSplit = false;
folderDefs = {
  fA: { id: "fA", name: "Profile", comp: "c1", parent: "",    hidden: true },   // both
  fB: { id: "fB", name: "Sub",     comp: "c1", parent: "fA",  hidden: false },  // params, in a mixed one
  fC: { id: "fC", name: "Dims",    comp: "c1", parent: "",    hidden: false },  // params only
  fD: { id: "fD", name: "Sketchy", comp: "c1", parent: "",    hidden: false },  // sketches only
  fE: { id: "fE", name: "Empty",   comp: "c1", parent: "",    ignoreGeo: true },// kept for params
  fG: { id: "fG", name: "Bolts",   comp: "c1", parent: "geo", hidden: false }   // bodies + a stray param
};
paramRows = [{ name: "P1" }, { name: "P2" }, { name: "P3" }, { name: "P4" }];
paramPlace = { P1: "fld:fA", P2: "fld:fB", P3: "fld:fC", P4: "fld:fG" };
leafFolder = { "c1|sk|S1": "fA", "c1|sk|S2": "fD", "c1|bd|B1": "fG" };
migrateParamFolders();
var twinOf = function (id) {
  return Object.keys(folderDefs).filter(function (k) {
    var f = folderDefs[k]; return k !== id && f.name === folderDefs[id].name; })[0];
};
var tA = twinOf("fA"), tG = twinOf("fG");
expect("a mixed folder stays a sketch folder", folderDefs.fA.kind || "", "");
expect("...and gets a parameter-group twin of the same name", folderDefs[tA].kind, "params");
expect("...which takes its parameters", paramPlace.P1, "fld:" + tA);
expect("a params-only folder inside it moves to the twin", folderDefs.fB.parent, tA);
expect("...as a parameter group", folderDefs.fB.kind, "params");
expect("a params-only folder becomes a parameter group", folderDefs.fC.kind, "params");
expect("a sketches-only folder is untouched", folderDefs.fD.kind || "", "");
expect("an empty folder ignored only for geometry was being kept for parameters",
       folderDefs.fE.kind, "params");
expect("a Geometry-side folder never becomes a parameter group", folderDefs.fG.kind || "", "");
expect("...its stray parameter goes to a twin at the component level",
       folderDefs[tG].parent + "|" + paramPlace.P4, "|fld:" + tG);
expect("the split is recorded", foldersSplit, true);

// The tree those make: parameter groups first, no parameter in a sketch folder.
keyByTok = { sk1: "c1|sk|S1", sk2: "c1|sk|S2", bd1: "c1|bd|B1" };
var kids = [
  { token: "sk1", type: "sketch", name: "S1", occToken: "occ1" },
  { token: "sk2", type: "sketch", name: "S2", occToken: "occ1" },
  { token: "bg:c1", type: "geometry", name: "Geometry", occToken: "occ1", compId: "c1",
    children: [{ token: "bd1", type: "body", name: "B1", occToken: "occ1" }] }
];
state.tree = [{ token: "occ1", type: "component", name: "Drawer", compId: "c1",
                children: graftFolders("c1", kids) }];
var top = orderedChildren("occ1", state.tree[0].children).map(function (n) {
  return (isParamSide(n) ? "P:" : "") + n.name; });
// (Front is a name group left over from the cases above — parameter side too.)
expect("the parameter side is pinned below everything else, sketch folders above",
       top.join(" | "),
       "Geometry | Profile | Sketchy | P:Front | P:Dims | P:Empty | P:Profile | P:Bolts");

// D closes an open parameter group (or name group) before it leaves the component —
// one level per press, deepest first.
state.activeToken = "occ1";
selected.clear(); cursorTok = null;
collapsed = new Set();
var wentUp = false, origUp = goUpOneLevel;
goUpOneLevel = function () { wentUp = true; };
function openGroupDepths() {
  return flatten().filter(function (r) {
    return !r.placeholder && isGroupTok(r.node.token) && !collapsed.has(r.node.token); })
    .map(function (r) { return r.depth; });
}
var before = openGroupDepths(), deepest = Math.max.apply(null, before),
    shallowest = Math.min.apply(null, before);
collapseOrOut();
var after = openGroupDepths();
expect("D with groups open closes them first", wentUp, false);
expect("...the deepest level only", after.indexOf(deepest) < 0
       && (deepest === shallowest || after.indexOf(shallowest) >= 0), true);
var presses = 1;
while (openGroupDepths().length && presses < 20) { collapseOrOut(); presses++; }
expect("...one press per level", presses, new Set(before).size);
expect("...parameter groups included", collapsed.has("fld:fC"), true);
expect("...and name groups", collapsed.has(nodeByTok("occ1").children.filter(function (n) {
  return n.type === "namegroup"; })[0].token), true);
expect("...without leaving", wentUp, false);
collapseOrOut();
expect("the next D leaves the component", wentUp, true);
goUpOneLevel = origUp;
expect("the sketch folder holds its sketch and no parameter",
       nodeByTok("fld:fA").children.map(function (n) { return n.type; }).join(","), "sketch");

expect("a parameter can't be dropped into a sketch folder",
       moveKind("param:P3", "fld:fD"), "null");
expect("...nor into the Geometry group", moveKind("param:P3", "bg:c1"), "null");
expect("...but can into a parameter group", moveKind("param:P3", "fld:" + tA), "virtual");
expect("a sketch can't be dropped into a parameter group", moveKind("sk2", "fld:fC"), "null");
expect("a parameter group can't go into a sketch folder", moveKind("fld:fC", "fld:fD"), "null");
expect("a sketch folder can't go into a parameter group", moveKind("fld:fD", "fld:fC"), "null");
expect("parameter groups nest", moveKind("fld:fC", "fld:" + tA), "virtual");

print("");
print("=== ⌘X / ⌘V take whole groups ===");
state.tree = [{ token: "occ1", type: "component", name: "Drawer", compId: "c1",
                children: graftFolders("c1", kids) }];
selected = new Set(["fld:fC", "param:P3"]); cursorTok = null;
expect("a parameter group is cuttable, and its own parameter rides along with it",
       cuttableToks().join(","), "fld:fC");
selected = new Set(["fld:fD"]);
expect("a sketch folder is not", cuttableToks().length, 0);
folderDefs.fX = { id: "fX", name: "Loose", comp: "c1", parent: "", kind: "params" };
state.tree = [{ token: "occ1", type: "component", name: "Drawer", compId: "c1",
                children: graftFolders("c1", kids) }];
selected = new Set(["fld:fX"]);
expect("an EMPTY parameter group is cuttable too", cuttableToks().join(","), "fld:fX");

selected = new Set(["fld:fC"]); cursorTok = null;
cutSelection();
selected = new Set(); cursorTok = "fld:fC";
pasteCut();
expect("⌘V on the group that was cut does nothing to it", folderDefs.fC.parent, "");

selected = new Set(["fld:fC"]); cutSelection();
selected = new Set(); cursorTok = "fld:" + tA;
pasteCut();
expect("⌘V on another parameter group files the cut group inside it",
       folderDefs.fC.parent, tA);
expect("...its parameter still in it", paramPlace.P3, "fld:fC");

state.tree = [{ token: "occ1", type: "component", name: "Drawer", compId: "c1",
                children: graftFolders("c1", kids) }];
selected = new Set(["fld:fX", "fld:fE", "fld:fD", "bg:c1"]); cutSelection();
expect("several groups cut together — the sketch folder and Geometry left out",
       cutToks.slice().sort().join(","), "fld:fE,fld:fX");
selected = new Set(); cursorTok = "fld:fC";
pasteCut();
expect("...and all pasted together", folderDefs.fX.parent + "," + folderDefs.fE.parent,
       "fC,fC");

print("");
print("=== right-click Paste: open group → inside, shut group or a row → alongside ===");
state.tree = [{ token: "occ1", type: "component", name: "Drawer", compId: "c1",
                children: graftFolders("c1", kids) }];
refreshFolders();
var gC = nodeByTok("fld:fC"), outer = parentTokOf("fld:fC");
collapsed.delete("fld:fC");
expect("an open group takes the paste inside", ctxPasteTarget(gC), "fld:fC");
collapsed.add("fld:fC");
expect("a shut group takes it alongside, in its parent", ctxPasteTarget(gC), outer);
expect("an empty group takes it inside, open or shut",
       ctxPasteTarget({ token: "fld:fZ", type: "folder", children: [] }), "fld:fZ");
expect("a parameter takes it alongside", ctxPasteTarget(nodeByTok("param:P3")),
       parentTokOf("param:P3"));
collapsed.delete("fld:fC");

print("");
print("=== ⌘G: groups around one kind, inside an open group, refused across kinds ===");
var renamed = null;
beginRename = function (tok) { renamed = tok; };
scrollTokIntoView = function () {};
foldersDoc = foldersDoc || "doc";
paramRows = [
  { name: "Drawer_Front_Curve_Inset_Back", expression: "10 mm" },
  { name: "Drawer_Front_Lid_Span_X",       expression: "20 mm" },
  { name: "Drawer_Handle_Radius",          expression: "4 mm"  }
];
partsCache = new Map(); rebuildNameIndex();
function regraft() {
  state.tree = [{ token: "occ1", type: "component", name: "Drawer", compId: "c1",
                  children: graftFolders("c1", kids) }];
}
regraft();
state.activeToken = "occ1"; collapsed = new Set(); cursorTok = null;

sent = []; blocked = null;
selected = new Set(["param:Drawer_Front_Curve_Inset_Back", "param:Drawer_Front_Lid_Span_X"]);
addFolder();
function renames() {
  return sent.filter(function (s) { return s.body && s.body.newName; })
             .map(function (s) { return s.body.newName; }).sort().join(" ");
}
expect("named parameters are grouped WITHOUT renaming them", renames(), "");
var gtok = renamed;
expect("...into a new parameter group, opened in rename",
       isFolderTok(gtok) && isParamFolder(gtok), true);
expect("...both filed in it", paramPlace["Drawer_Front_Curve_Inset_Back"] === gtok
       && paramPlace["Drawer_Front_Lid_Span_X"] === gtok, true);
partsCache = new Map(); rebuildNameIndex(); regraft();
expect("...and their name group draws inside it", parentTokOf(nameTok(gtok, ["Front"])), gtok);
expect("...with the rows under it", parentTokOf("param:Drawer_Front_Lid_Span_X"),
       nameTok(gtok, ["Front"]));

// Dragged back out onto the component: out of the group, still not renamed.
sent = [];
moveParamsByName(["Drawer_Front_Lid_Span_X"], "occ1");
expect("a drop on the component takes it out of the group",
       paramPlace["Drawer_Front_Lid_Span_X"] === undefined
       && paramPlace[renames()] === undefined, true);
paramRows = [
  { name: "Drawer_Front_Curve_Inset_Back", expression: "10 mm" },
  { name: "Drawer_Front_Lid_Span_X",       expression: "20 mm" },
  { name: "Drawer_Handle_Radius",          expression: "4 mm"  }];
paramPlace = {};
partsCache = new Map(); rebuildNameIndex();
regraft(); sent = []; renamed = null;
selected = new Set([nameTok("c1", ["Front"])]);
collapsed = new Set([nameTok("c1", ["Front"])]);
addFolder();
expect("a SHUT name group gets a group around it, nothing renamed", renames(), "");
partsCache = new Map(); rebuildNameIndex(); regraft();
expect("...the name group now inside it", parentTokOf(nameTok(renamed, ["Front"])), renamed);
paramPlace = {};
partsCache = new Map(); rebuildNameIndex();

regraft(); sent = []; blocked = null;
collapsed = new Set();
selected = new Set(["sk2", "param:Drawer_Handle_Radius"]);
addFolder();
expect("a sketch and a parameter together are refused", !!blocked, true);
expect("...nothing renamed", sent.length, 0);

regraft(); blocked = null;
var before = Object.keys(folderDefs).length;
selected = new Set(["fld:fD"]);           // an open sketch folder
addFolder();
var made = Object.keys(folderDefs).map(function (k) { return folderDefs[k]; })
  .filter(function (f) { return f.parent === "fD"; });
expect("an OPEN group gets a new group inside it", made.length, 1);
expect("...of its own kind", made[0].kind || "", "");

regraft();
selected = new Set(["fld:fD"]); collapsed = new Set(["fld:fD"]);
addFolder();
expect("a SHUT group gets a group around it", folderDefs.fD.parent !== "", true);
expect("...a sketch folder", (folderDefs[folderDefs.fD.parent] || {}).kind || "", "");

regraft();
selected = new Set(); collapsed = new Set();
before = Object.keys(folderDefs).length;
addFolder();
expect("nothing selected: a group at the top of the component you are in",
       Object.keys(folderDefs).length - before, 1);
state.tree[0].children.push({ token: "occ9", type: "component", name: "Knob", compId: "c9" });
addFolder();
var newest = folderDefs[Object.keys(folderDefs).pop()];
expect("...a parameter group when the component has components in it (an empty "
       + "sketch folder would never be drawn there)", newest.kind, "params");

print("=== delete with a substitute ===");
// Width ← Half (Width/2) ← Quarter (Half/2); d12 is a sketch dimension on Width.
paramRows = [
  { name: "Width",   expression: "100 mm",    unit: "mm",  depNames: ["Half", "d12"] },
  { name: "Half",    expression: "Width / 2", unit: "mm",  depNames: ["Quarter"] },
  { name: "Quarter", expression: "Half / 2",  unit: "mm",  depNames: [] },
  { name: "Depth",   expression: "50 mm",     unit: "mm",  depNames: [] },
  { name: "DepthAngle", expression: "10 deg", unit: "deg", depNames: [] },
  { name: "Loose",   expression: "5 mm",      unit: "mm",  depNames: [] },
];
document.body.insertAdjacentHTML = function () {};
var asked = null;
var realOpen = openSubstitute;
openSubstitute = function (names, blocked) { asked = { names: names, blocked: blocked }; };
sent = [];
deleteParams(["param:Loose"]);
expect("nothing depends on it: deleted straight away, no question",
       asked === null && sent.length === 1 && sent[0].body.name === "Loose", true);
expect("...with no replacement", sent[0].body.replacement, undefined);
paramRows.push({ name: "Loose", expression: "5 mm", unit: "mm", depNames: [] });
deleteParams(["param:Width"]);
expect("a dependant outside the set: asks for a substitute", asked && asked.blocked.join(), "Width");
asked = null;
deleteParams(["param:Quarter", "param:Half"]);
expect("dependant inside the set: no question", asked, null);
openSubstitute = realOpen;

paramRows = [
  { name: "Width",   expression: "100 mm",    unit: "mm",  depNames: ["Half", "d12"] },
  { name: "Half",    expression: "Width / 2", unit: "mm",  depNames: ["Quarter"] },
  { name: "Quarter", expression: "Half / 2",  unit: "mm",  depNames: [] },
  { name: "Depth",   expression: "50 mm",     unit: "mm",  depNames: [] },
  { name: "DepthAngle", expression: "10 deg", unit: "deg", depNames: [] },
];
expect("dependants first", dependantsFirst(["Width", "Half", "Quarter"]).join(), "Quarter,Half,Width");
expect("everything under Width is off the list",
       [...substDependants("Width")].sort().join(), "Half,Quarter,d12");
openSubstitute(["Width"], ["Width"]);
var inp = document.getElementById("subst-in");
inp.value = "de"; updateSubstAC();
expect("suggests by prefix, same unit first", subst.items.map(function (i) { return i.p.name; }).join(),
       "Depth,DepthAngle");
expect("...best row ready for Enter", subst.idx, 0);
inp.value = "angle"; updateSubstAC();
expect("a match at a later word", subst.items.map(function (i) { return i.p.name; }).join(), "DepthAngle");
inp.value = "h"; updateSubstAC();
expect("its own dependants are never offered",
       subst.items.some(function (i) { return i.p.name === "Half"; }), false);
inp.value = "Half"; submitSubstStep();
expect("...and are refused if typed", !!subst && /depends on Width/.test(document.getElementById("subst-err").textContent), true);
inp.value = "Nope"; submitSubstStep();
expect("an unknown name is refused", /no parameter called/.test(document.getElementById("subst-err").textContent), true);
inp.value = "de"; updateSubstAC();
substKey({ key: "Enter", preventDefault: function () {}, stopPropagation: function () {} });
expect("Enter takes the highlighted suggestion", inp.value, "Depth");
sent = [];
substKey({ key: "Enter", preventDefault: function () {}, stopPropagation: function () {} });
expect("a second Enter deletes, with the substitute", sent.length && JSON.stringify(sent[0].body),
       JSON.stringify({ name: "Width", replacement: "Depth" }));
expect("...and the dialog is gone", subst, null);
expect("...and the row went at once", !!paramByName("Width"), false);

print("");
print("=== the Bounds parameter group takes dropped parameters ===");
folderDefs.fBnd = { id: "fBnd", name: "Bounds", comp: "c1", parent: "", kind: "params" };
paramRows = [
  { name: "Drawer_Bounds_Span_X",    expression: "10 mm" },
  { name: "Drawer_Front_Lid_Span_X", expression: "20 mm" },
  { name: "DrawerTop",               expression: "30 mm" },
  { name: "LooseOne",                expression: "40 mm" }
];
paramPlace = { DrawerTop: "fld:fBnd", LooseOne: "c1" };
partsCache = new Map(); rebuildNameIndex(); regraft();
expect("a structured <Owner>_Bounds_… draws inside the Bounds group, not beside it",
       nodeByTok("fld:fBnd").children.map(function (n) { return n.name; }).sort().join(","),
       "DrawerTop,Width");
expect("...and there is no second Bounds row",
       nodeByTok("occ1").children.filter(function (n) { return n.name === "Bounds"; }).length, 1);
expect("a structured parameter can be dropped on Bounds",
       moveKind("param:Drawer_Front_Lid_Span_X", "fld:fBnd"), "name");
sent = [];
moveParamsByName(["Drawer_Front_Lid_Span_X"], "fld:fBnd");
expect("...which renames it into the Bounds level, its name word kept",
       sent.length && sent[0].body.newName, "Drawer_Bounds_Lid_Span_X");
expect("an unstructured one files into Bounds by hand",
       moveKind("param:LooseOne", "fld:fBnd"), "virtual");
expect("a name group can go into Bounds too",
       moveKind(nameTok("c1", ["Front"]), "fld:fBnd"), "name");
collapsed = new Set();
expect("an open Bounds group never catches a new parameter",
       openGroupRows().some(function (r) { return r.tok === "fld:fBnd"; }), false);
// What Add Bounds / New Component's filing does: every new bounds parameter, parsed
// or not, is filed INTO the Bounds group. A parsed one must still draw at its Bounds
// level — rooted in the folder it would draw a second Bounds group inside the first.
paramRows = [
  { name: "Drawer_Bounds_Span_X",         expression: "10 mm" },
  { name: "Drawer_Bounds_Right_Offset_X", expression: "5 mm" }
];
paramPlace = { Drawer_Bounds_Span_X: "fld:fBnd", Drawer_Bounds_Right_Offset_X: "fld:fBnd" };
partsCache = new Map(); rebuildNameIndex(); regraft();
expect("a bounds name filed into the Bounds group draws in it once, not in a nested Bounds",
       nodeByTok("fld:fBnd").children.map(function (n) { return n.type; }).join(","),
       "param,param");
// …and reads as its one word: Offset and the axis off the row, owner + Bounds off typing.
paramRows[0].comment = "Span of Drawer, derived from its faces [c1]";
paramRows[1].comment = "Bounds of Drawer [c1]";
paramShow = { Drawer_Bounds_Right_Offset_X: { type: true } };
foldersReady = true; foldersBoundsShown = false;
ensureBoundsShow();
expect("the first pass gives every bounds parameter the one-word reading, over old entries",
       JSON.stringify(paramShow.Drawer_Bounds_Right_Offset_X),
       JSON.stringify({ type: false, lv: ["Drawer", "Bounds"], dir: false }));
expect("...a span keeps its axis (it IS its Width)",
       JSON.stringify(paramShow.Drawer_Bounds_Span_X),
       JSON.stringify({ type: false, lv: ["Drawer", "Bounds"] }));
partsCache = new Map(); rebuildNameIndex(); regraft();
expect("...so the rows say just Right and Width",
       nodeByTok("fld:fBnd").children.map(function (n) { return n.name; }).sort().join(","),
       "Right,Width");
paramShow.Drawer_Bounds_Right_Offset_X = {};
ensureBoundsShow();
expect("after that, an entry set by hand (even empty) is left alone",
       JSON.stringify(paramShow.Drawer_Bounds_Right_Offset_X), "{}");
foldersReady = false; paramShow = {};

print("");
print("=== components that share a name: the pin says which one ===");
// Three components all called "Steel", under Roof / Boot Ceiling (deeper) / Drawer
// Ceiling. The owner level "Steel" alone can't tell them apart.
compByCid = {
  sR: { name: "Steel", compId: "sR", token: "oR", key: "oc|r:1/sR:1" },
  sB: { name: "Steel", compId: "sB", token: "oB", key: "oc|b:1/x:1/sB:1" },
  sD: { name: "Steel", compId: "sD", token: "oD", key: "oc|d:1/sD:1" },
  b:  { name: "Boot Ceiling", compId: "b", token: "ob", key: "oc|b:1" }
};
compByTok = { oR: compByCid.sR, oB: compByCid.sB, oD: compByCid.sD, ob: compByCid.b };
folderDefs = {}; leafFolder = {};
paramRows = [{ name: "SheetThk", expression: "1 mm" },
             { name: "Steel_Tab_Radius", expression: "5 mm" }];
paramPlace = { SheetThk: "sB" };
partsCache = new Map(); rebuildIdentIndex(); rebuildNameIndex();
expect("an unpinned shared name goes to the first one drawn",
       homeOfParts(partsOf("Steel_Tab_Radius"), nameIdx, "Steel_Tab_Radius").ref, "sR");
// The value dialog renames SheetThk (filed in Boot Ceiling's Steel) to Steel_Lip_Radius.
carryFilingOnRename("SheetThk", "Steel_Lip_Radius");
paramRows[0].name = "Steel_Lip_Radius";
partsCache = new Map(); rebuildNameIndex();
expect("a rename spelling out the component it's in keeps it there",
       homeOfParts(partsOf("Steel_Lip_Radius"), nameIdx, "Steel_Lip_Radius").ref, "sB");
sent = [];
nodeByTok = function (tok) { return compByTok[tok] ? { type: "component", token: tok } : null; };
expect("dragging it onto another Steel is allowed",
       moveKind("param:Steel_Lip_Radius", "oD"), "name");
moveParamsByName(["Steel_Lip_Radius"], "oD");
partsCache = new Map(); rebuildNameIndex();
// Dropped on the component itself, so the Lip group comes off — the name changes,
// but only to Steel_Radius, which on its own could still mean any of the three.
expect("...and lands in THAT Steel, not the first one drawn",
       paramRows[0].name + "|" + homeOfParts(partsOf(paramRows[0].name), nameIdx, paramRows[0].name).ref,
       "Steel_Radius|sD");
sent = [];
moveParamsByName(["Steel_Radius"], "oB");
partsCache = new Map(); rebuildNameIndex();
expect("a move whose name doesn't change is a re-pin: no rename sent, new Steel",
       sent.filter(function (r) { return r.body && r.body.newName; }).length + "|"
       + homeOfParts(partsOf("Steel_Radius"), nameIdx, "Steel_Radius").ref, "0|sB");
expect("a name group dragged between Steels moves too",
       (moveNameGroup(nameTok("sR", ["Tab"]), "oB"),
        partsCache = new Map(), rebuildNameIndex(),
        homeOfParts(partsOf("Steel_Tab_Radius"), nameIdx, "Steel_Tab_Radius").ref), "sB");
expect("renamed to Steel_… from Boot Ceiling itself: its own Steel, not Roof's",
       pinFor("Steel_New_Radius", "b"), "sB");

print("");
print("=== spaceParens: a bracketed tag is spaced off the text either side ===");
expect("Legs(Mirror) -> Legs (Mirror)", spaceParens("Legs(Mirror)"), "Legs (Mirror)");
expect("already spaced is left alone", spaceParens("Legs (Mirror)"), "Legs (Mirror)");
expect("text after the bracket gets a space", spaceParens("Legs(Mirror)Top"), "Legs (Mirror) Top");
expect("leading bracket", spaceParens("(Mirror)Legs"), "(Mirror) Legs");
expect("inside stays tight", spaceParens("A( b )C"), "A ( b ) C");
expect("occurrence suffix stays glued", spaceParens("Legs(Mirror):1"), "Legs (Mirror):1");
expect("no brackets, untouched", spaceParens("Legs"), "Legs");

print("");
print("=== groupFeatures: an open body's features in the timeline's groups ===");
(function(){
  var E = "adsk::fusion::ExtrudeFeature", F = "adsk::fusion::FilletFeature";
  var f = function(name, type, phase, ref){
    return { type: "feature", token: "ft:b|" + name, name: name, ftype: type, phase: phase,
             key: (ref || "C") + "|" + name, compRef: ref || "C", health: "", children: [] };
  };
  var feats = [f("Ex1", E, 4), f("Ex2", E, 4), f("Fil", F, 5), f("Ex3", E, 6), f("Odd", E, 4, "D")];
  feats.forEach(function(x, i){ x.pos = i; });
  var data = normData({ groups: { g1: { name: "", parent: "c:C" }, g2: { name: "Mine", parent: "g:g1" } },
                        member: { "C|Ex1": "g1", "C|Ex3": "g1", "C|Fil": "g2", "D|Odd": "g1" } });
  var top = groupFeatures({ token: "b" }, feats, data);
  var names = function(ns){ return ns.map(function(n){ return n.type === "fgroup" ? "[" + n.name + "]" : n.name; }).join(","); };
  expect("group sits at its earliest member; wrong-component row stays loose",
         names(top), "[Extrusions & Fillet],Ex2,Odd");
  expect("nested group inside, in timeline order", names(top[0].children), "Ex1,[Mine],Ex3");
  expect("one-kind group wears its colour", top[0].children[1].kindCls, "k-body");
  expect("mixed group's icon is its most frequent type", top[0].iconType, E);
  expect("phase edges between siblings of different phases",
         top[0].children.map(function(n){ return !!n.phaseEdge; }).join(","), "false,true,true");
  expect("no edge between same-phase siblings", top.map(function(n){ return !!n.phaseEdge; }).join(","),
         "false,false,false");
})();

print("\n=== featNodesFor: the timeline marker's dot ===");
(function(){
  var old = featsByGeo;
  var dots = function(pasts){
    featsByGeo = { fx: pasts.map(function(p, i){ return { tok: "t" + i, name: "F" + i, past: p }; }) };
    return featNodesFor({ token: "fx" }).map(function(n){ return n.marker ? "o" : "-"; }).join("");
  };
  expect("rolled back mid-list: the last feature before the marker", dots([false, false, true, true]), "-o--");
  expect("at the end: no dot", dots([false, false, false]), "---");
  expect("all rolled back: no dot", dots([true, true]), "--");
  featsByGeo = old;
})();

print("\n=== boundsSketchesInAxisOrder: Bounds X, Y, Z whatever the build order ===");
(function(){
  var sk = function(name){ return { type: "sketch", name: name }; };
  var list = [sk("Bounds Z"), sk("Profile"), sk("Bounds Y"), sk("Bounds X"), sk("Top")];
  expect("re-dealt into their own slots by axis",
         boundsSketchesInAxisOrder(list).map(function(n){ return n.name; }).join(","),
         "Bounds X,Profile,Bounds Y,Bounds Z,Top");
})();

print("\n=== pickBodyLit: a face pick keeps its body lit, never selected ===");
(function(){
  var oldSel = selected, oldState = state, oldPB = pickBody;
  state = { selTokens: ["body1"] };
  pickBody = { tok: "body1", row: "ft:fx|feat" };
  selected = new Set(["ft:fx|feat"]);
  expect("body lit while its feature row holds the pick", pickBodyLit("body1"), true);
  expect("body is not in the selection", selected.has("body1"), false);
  selected = new Set(["other"]);
  expect("unlit once something else is picked here", pickBodyLit("body1"), false);
  selected = new Set(["ft:fx|feat"]); state = { selTokens: [] };
  expect("unlit once the viewport drops it", pickBodyLit("body1"), false);
  selected = oldSel; state = oldState; pickBody = oldPB;
})();

print("\n=== same-named bodies: each keeps its own filing ===");
(function(){
  var oldLF = leafFolder, oldSF = saveFolders, oldSO = saveOrder, oldSC = saveCollapsed;
  saveFolders = saveOrder = saveCollapsed = function(){};
  var body = function(tok, key){ return { type: "body", token: tok, key: key, children: [] }; };
  var tree = function(){ return [{ type: "component", token: "oc", key: "oc|c:1", compId: "c",
                                   children: [].slice.call(arguments) }]; };
  keyByTok = {};
  leafFolder = { "c|bd|Rib (1)": "fA" };
  rebuildKeyIndex(tree(body("t1", "c|bd|Rib (1)"), body("t2", "c|bd|Rib (1)#2")));
  expect("the first is filed", folderIdOfLeaf("t1"), "fA");
  expect("its same-named twin is not", folderIdOfLeaf("t2"), undefined);

  leafFolder["c|bd|Rib (1)#2"] = "fB";
  rebuildKeyIndex(tree(body("t1", "c|bd|Rib X"), body("t2", "c|bd|Rib (1)")));
  expect("renaming the first: it keeps its folder", folderIdOfLeaf("t1"), "fA");
  expect("…and the twin, shifted onto the plain key, keeps its own", folderIdOfLeaf("t2"), "fB");

  leafFolder = { "c|bd|Rib (1)": "fA" };
  rebuildKeyIndex(tree(body("t3", "c|bd|Rib (1)"), body("t4", "c|bd|Rib (1)#2")));
  rebuildKeyIndex(tree(body("t4", "c|bd|Rib (1)")));
  expect("deleting the filed first: the unfiled twin doesn't inherit it",
         folderIdOfLeaf("t4"), undefined);
  leafFolder = oldLF; saveFolders = oldSF; saveOrder = oldSO; saveCollapsed = oldSC;
})();

print("\n=== filing a body stamps its component's ids; twins carry onto them ===");
(function(){
  var oldLF = leafFolder, oldSF = saveFolders, oldSO = saveOrder, oldSC = saveCollapsed;
  var oldPost = post, oldST = setTimeout, posts = [], fired = null;
  saveFolders = saveOrder = saveCollapsed = function(){};
  post = function(b){ posts.push(b); };
  setTimeout = function(fn){ fired = fn; return 1; };
  var body = function(tok, key){ return { type: "body", token: tok, key: key, children: [] }; };
  var tree = function(){ return [{ type: "component", token: "oc", key: "oc|c:1", compId: "c",
                                   children: [].slice.call(arguments) }]; };
  keyByTok = {};
  leafFolder = {};
  rebuildKeyIndex(tree(body("t1", "c|bd|Rib (1)"), body("t2", "c|bd|Rib (1)#2"),
                       body("t3", "c|bd|@abc")));
  setContainer("t1", "fld:fA"); setContainer("t2", "fld:fB");
  expect("filed under the name keys first", leafFolder["c|bd|Rib (1)#2"], "fB");
  if (fired) fired();
  expect("one stamp request for the component", JSON.stringify(posts),
         JSON.stringify([{ action: "stampbodies", comps: ["c"] }]));
  posts = []; fired = null;
  setContainer("t3", "fld:fA");
  expect("an already-stamped body asks for nothing", fired, null);
  rebuildKeyIndex(tree(body("t2", "c|bd|@u2"), body("t1", "c|bd|@u1"),
                       body("t3", "c|bd|@abc")));
  expect("the first twin keeps its group under its id", folderIdOfLeaf("t1"), "fA");
  expect("…and the second its own", folderIdOfLeaf("t2"), "fB");
  expect("no name key is left behind", "c|bd|Rib (1)" in leafFolder, false);
  expect("the group policy re-runs on the new keys", posts.some(function(p){
    return p.action === "regroup"; }), true);
  posts = [];
  rebuildKeyIndex(tree(body("t1", "c|bd|@u1"), body("t2", "c|bd|@u2"),
                       body("t3", "c|bd|@abc")));
  expect("twins changing order changes nothing", folderIdOfLeaf("t2"), "fB");
  expect("…and posts nothing", posts.length, 0);
  leafFolder = oldLF; saveFolders = oldSF; saveOrder = oldSO; saveCollapsed = oldSC;
  post = oldPost; setTimeout = oldST;
})();

print("\n=== every sketch is kept in its component's Sketches (fileSketches) ===");
(function(){
  var oldTree = rawTree, oldReady = foldersReady, oldDoc = foldersDoc, oldState = state;
  var oldSF = saveFolders, oldPost = post;
  saveFolders = post = function(){};
  var sketch = function(cid, name){ return { type: "sketch", token: "s" + cid + name,
                                            key: cid + "|sk|" + name, children: [] }; };
  var comp = function(cid, kids){ return { type: "component", token: "oc" + cid, compId: cid,
                                           key: "oc|" + cid, children: kids }; };
  foldersReady = true; foldersDoc = "doc"; state = { doc_id: "doc" };
  folderDefs = { fP: { id: "fP", name: "Profile", comp: "c1", parent: "", hidden: false, kind: "" },
                 fE: { id: "fE", name: "Empty", comp: "c1", parent: "", hidden: false, kind: "" },
                 fB: { id: "fB", name: "Legs", comp: "c1", parent: GEO_PARENT, hidden: false, kind: "" },
                 fQ: { id: "fQ", name: "Bounds", comp: "c1", parent: "", hidden: false, kind: "params" } };
  leafFolder = { "c1|sk|Mine": "fP", "c1|sk|OnBodySide": "fB" };
  rawTree = [comp("c1", [sketch("c1", "Mine"), sketch("c1", "OnBodySide"),
                         sketch("c1", "Bounds X"), sketch("c1", "Sketch7")])];
  rebuildKeyIndex(rawTree);
  keepSketchesFiled();
  var sk = Object.keys(folderDefs).filter(function(k){
    return folderDefs[k].name === "Sketches" && !folderDefs[k].parent; });
  expect("one Sketches folder at the top", sk.length, 1);
  sk = sk[0];
  expect("a new (loose) sketch goes into it on the next tree — no push needed",
         leafFolder["c1|sk|Sketch7"], sk);
  expect("a sketch filed on the Bodies side is refiled into Sketches",
         leafFolder["c1|sk|OnBodySide"], sk);
  var bnd = folderDefs[leafFolder["c1|sk|Bounds X"]];
  expect("a bounds sketch's Bounds folder is BORN inside Sketches", bnd && bnd.parent, sk);
  expect("...not in the Bounds parameter group", bnd && bnd.kind, undefined);
  expect("a hand folder of its own keeps its sketch", leafFolder["c1|sk|Mine"], "fP");
  expect("...and moves inside Sketches", folderDefs.fP.parent, sk);
  expect("an empty top-level folder waits", folderDefs.fE.parent, "");
  expect("a Bodies-side folder stays on the Bodies side", folderDefs.fB.parent, GEO_PARENT);
  expect("the Bounds parameter group stays at the top", folderDefs.fQ.parent, "");

  // A top-level sketch Bounds folder from before (made outside Sketches) is moved in.
  folderDefs.fO = { id: "fO", name: "Bounds", comp: "c1", parent: "", hidden: true, kind: "" };
  delete folderDefs[bnd.id];
  leafFolder["c1|sk|Bounds X"] = "fO";
  keepSketchesFiled();
  expect("an old top-level Bounds folder moves inside Sketches", folderDefs.fO.parent, sk);
  expect("...keeping its sketch", leafFolder["c1|sk|Bounds X"], "fO");

  var snap = JSON.stringify([folderDefs, leafFolder]);
  keepSketchesFiled();
  expect("a second pass changes nothing", JSON.stringify([folderDefs, leafFolder]), snap);

  // Another document's tree arriving before its overlay has loaded: hands off.
  state = { doc_id: "other" };
  rawTree = [comp("c1", [sketch("c1", "Stranger")])];
  rebuildKeyIndex(rawTree);
  keepSketchesFiled();
  expect("a tree from another document files nothing", leafFolder["c1|sk|Stranger"], undefined);

  rawTree = oldTree; foldersReady = oldReady; foldersDoc = oldDoc; state = oldState;
  saveFolders = oldSF; post = oldPost;
  folderDefs = {}; leafFolder = {};
})();

print("\n=== File Bounds Into Groups also files every other sketch into Sketches ===");
(function(){
  var oldTree = rawTree, oldReady = foldersReady, oldDoc = foldersDoc, oldRows = paramRows;
  var oldSF = saveFolders, oldRF = refreshFolders, oldPost = post, oldState = state;
  saveFolders = refreshFolders = post = function(){};
  state = { doc_id: "doc" };
  var sketch = function(cid, name){ return { type: "sketch", token: "s" + cid + name,
                                            key: cid + "|sk|" + name, children: [] }; };
  rawTree = [
    sketch("r", "Master Plan"), sketch("r", "Loose Root"),
    { type: "component", token: "oc1", compId: "c1", children: [
        sketch("c1", "Bounds Drawer"), sketch("c1", "Profile"), sketch("c1", "Kept") ]},
    { type: "component", token: "oc2", compId: "c2", children: [ sketch("c2", "Rail") ]},
    { type: "component", token: "oc3", compId: "c3", children: [] }
  ];
  foldersReady = true; foldersDoc = "doc"; paramRows = [];
  folderDefs = { fK: { id: "fK", name: "Mine", comp: "c1", parent: "", hidden: false, kind: "" },
                 fX: { id: "fX", name: "Stray", comp: "c2", parent: "", hidden: false, kind: "" } };
  rebuildKeyIndex(rawTree);
  leafFolder = { "c1|sk|Kept": "fK", "c1|sk|Profile": "fX", "c2|sk|Rail": "gone" };
  fileAllBounds();
  var home = function(k){ var f = folderDefs[leafFolder[k]]; return f ? f.comp + "/" + f.name : ""; };
  expect("a bounds sketch still goes to Bounds", home("c1|sk|Bounds Drawer"), "c1/Bounds");
  expect("the root's master still goes to Masters", home("r|sk|Master Plan"), ROOT_TOKEN + "/Masters");
  expect("a loose root sketch goes to the root's Sketches", home("r|sk|Loose Root"), ROOT_TOKEN + "/Sketches");
  expect("one filed into another component's folder is refiled", home("c1|sk|Profile"), "c1/Sketches");
  expect("one naming a missing folder is refiled (Sketches made)", home("c2|sk|Rail"), "c2/Sketches");
  expect("one in its own component's folder stays", leafFolder["c1|sk|Kept"], "fK");
  expect("...its folder moved inside Sketches", folderDefs[folderDefs.fK.parent].name, "Sketches");
  expect("a component with no sketches gets no Sketches folder",
         Object.keys(folderDefs).some(function(k){ return folderDefs[k].comp === "c3"; }), false);
  var n = Object.keys(folderDefs).length;
  fileAllBounds();
  var bnd = folderDefs[leafFolder["c1|sk|Bounds Drawer"]];
  expect("the Bounds folder sits inside Sketches", folderDefs[bnd.parent] && folderDefs[bnd.parent].name, "Sketches");
  expect("a second sweep makes nothing new", Object.keys(folderDefs).length, n);
  rawTree = oldTree; foldersReady = oldReady; foldersDoc = oldDoc; paramRows = oldRows;
  saveFolders = oldSF; refreshFolders = oldRF; post = oldPost; state = oldState;
  folderDefs = {}; leafFolder = {};
})();

// ── Exploding a group a Combine follows asks first ───────────────────────────
(function(){
  var oldDF = deleteFolders, oldSC = showConfirm;
  var gone = [], asked = null;
  deleteFolders = function(toks){ gone = gone.concat(toks); };
  showConfirm = function(title, html, ok, go){ asked = { title: title, html: html, go: go }; };
  folderDefs = { fA: { id: "fA", name: "Ribs", comp: "c1", parent: "" },
                 fB: { id: "fB", name: "Loose", comp: "c1", parent: "" } };
  noteGroupUses({ fA: ["Combine3", "Combine4"] });
  deleteTokens(["fld:fB"]);
  expect("a group nothing follows explodes at once", gone.join() + "|" + !!asked, "fld:fB|false");
  gone = [];
  deleteTokens(["fld:fA"]);
  expect("one a Combine follows asks instead", gone.length + "|" + (asked && asked.title), "0|Explode Group");
  expect("...naming the combines", /Combine3/.test(asked.html) && /Combine4/.test(asked.html), true);
  asked.go();
  expect("...and explodes once told to", gone.join(), "fld:fA");
  expect("the uses come from /state", groupCombines(["fld:fA", "fld:fB"]).join(), "Combine3,Combine4");
  noteGroupUses(undefined);
  expect("...and go with it", groupCombines(["fld:fA"]).length, 0);
  deleteFolders = oldDF; showConfirm = oldSC; folderDefs = {};
})();

print("=== an auto group with nothing showing disappears; a hand-made one gets parens ===");
(function () {
  var oldSB = showBoundsFolders, oldHS = hideSketches;
  showBoundsFolders = false; hideSketches = false;
  folderDefs = {
    sk: { id: "sk", name: "Sketches", comp: "c1", parent: "" },
    bd: { id: "bd", name: "Bounds", comp: "c1", parent: "sk", hidden: true },
    mine: { id: "mine", name: "Mine", comp: "c1", parent: "" },
  };
  var bounds = { token: "fld:bd", type: "folder", name: "Bounds", children: [
    { token: "s1", type: "sketch", name: "Bounds Top", visible: false } ] };
  var sketches = { token: "fld:sk", type: "folder", name: "Sketches", children: [bounds] };
  var mine = { token: "fld:mine", type: "folder", name: "Mine", children: [bounds] };
  expect("Sketches holding only a hidden Bounds isn't drawn", rowDrawn(sketches), false);
  expect("a hand-made folder in the same spot still draws", rowDrawn(mine), true);
  expect("...as empty (parens)", folderShowsRows(mine), false);
  showBoundsFolders = true;
  expect("Bounds shown → Sketches draws again", rowDrawn(sketches) && folderShowsRows(sketches), true);
  showBoundsFolders = oldSB; hideSketches = oldHS; folderDefs = {};
})();

print("");
print(fails ? "FAIL: " + fails + " assertion(s)" : "all assertions passed");
