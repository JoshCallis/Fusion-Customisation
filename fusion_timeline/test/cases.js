/* Assertions for the timeline's tree model — the one piece of this window with no
 * live-Fusion dependency. FIXTURE is test/fixture.json (handed in by run.py). */

var fails = 0;
function expect(label, got, want) {
  var ok = String(got) === String(want);
  if (!ok) fails++;
  print((ok ? "  PASS  " : "  FAIL  ") + label +
        (ok ? "" : "\n          got  " + got + "\n          want " + want));
}
function clone(x) { return JSON.parse(JSON.stringify(x)); }
// The drawn order, one entry per line of the list: depth, then the name ([x] = group).
function seq(m) {
  return m.sequence.map(function (e) {
    return e.group ? e.group.depth + ":[" + e.group.label + "]"
                   : e.item.depth + ":" + e.item.label;
  }).join(" / ");
}
var R = FIXTURE.rows, C = FIXTURE.comps, A = FIXTURE.activeRef;
function mk(d) { return buildModel(R, C, A, d); }

print("=== component groups ===");
var m = mk(emptyData());
expect("sub-components sit in groups named after them, nested like the Browser",
  seq(m),
  "0:Base Sketch / 0:Body / 0:[Drawer] / 1:Drawer / 1:Front Sketch / 1:Front / " +
  "1:[Shelf] / 2:Shelf / 2:Shelf Board / 1:Pull Chamfer / 0:Top Fillet / 0:[Lid] / " +
  "1:Lid / 0:Offset Plane / 0:Handle Hole / 0:Mirror / 0:Mystery");
expect("a component-creation item leads its group",
  m.sequence[m.sequence.findIndex(function (e) { return e.group && e.group.id === "cg:DRW"; }) + 1].item.label,
  "Drawer");
expect("a row's gid is the group it is drawn in",
  m.items.find(function (i) { return i.id === "SHELF|Shelf Board"; }).gid, "cg:SHELF");
expect("a component group's parent is its parent component's group",
  m.groups.find(function (g) { return g.id === "cg:SHELF"; }).parent, "cg:DRW");

print("=== health roll-up ===");
expect("an error inside Shelf makes Shelf red", m.groups.find(function (g) { return g.id === "cg:SHELF"; }).health, "error");
expect("…and Drawer, which holds Shelf", m.groups.find(function (g) { return g.id === "cg:DRW"; }).health, "error");
expect("Lid has nothing wrong", m.groups.find(function (g) { return g.id === "cg:LID"; }).health, "");
expect("a warning row carries its class",
  m.items.find(function (i) { return i.id === "ROOT|Top Fillet"; }).cls, "h-warning k-body");
expect("a suppressed row carries its class",
  m.items.find(function (i) { return i.id === "ROOT|Handle Hole"; }).cls, "supp k-body");

print("=== kind colours ===");
expect("a sketch is sketch green", kindCls("adsk::fusion::Sketch"), "k-sketch");
expect("a plane is Origin pink", kindCls("adsk::fusion::ConstructionPlane"), "k-origin");
expect("a feature is body indigo", kindCls("adsk::fusion::ExtrudeFeature"), "k-body");
expect("a new component stays white", kindCls("adsk::fusion::Occurrence"), "");
expect("a move stays white", kindCls("adsk::fusion::MoveFeature"), "");
var cp = { name: "CutPaste Fuselage:1", type: "adsk::fusion::Occurrence" };
expect("a cut-paste draws Move's glyph", glyphType(cp), "adsk::fusion::MoveFeature");
expect("…and isn't bold like a new component", rowCls(cp), "");
expect("a creation keeps the component's", glyphType({ name: " Fuselage:1", type: "adsk::fusion::Occurrence" }),
  "adsk::fusion::Occurrence");

print("=== display names ===");
expect("a bracket is set off", displayName("Legs(Mirror)"), "Legs (Mirror)");
expect("…and text after it", displayName("Legs(Mirror)Top"), "Legs (Mirror) Top");
expect("CamelCase splits", displayName("SketchPlaneFront"), "Sketch Plane Front");
expect("capitals together stay whole", displayName("HIPS Shell"), "HIPS Shell");
expect("auto-number still goes first", displayName("Extrude9"), "Extrude");
expect("a cut-paste reads Reorder", displayName("CutPaste Fuselage:1"), "Reorder Fuselage");

print("=== ⌘G around rows ===");
var d = emptyData();
var got = groupAround(m, d, ["ROOT|Body", "ROOT|Offset Plane"], [], A);
expect("made a group", got.id, "g1");
expect("at the active level", d.groups.g1.parent, "c:ROOT");
expect("stored unnamed", d.groups.g1.name, "");
m = mk(d);
expect("the group sits where its earliest member is, rows stay in timeline order",
  seq(m).split(" / ").slice(0, 5).join(" / "),
  "0:Base Sketch / 0:[Extrusion & Plane] / 1:Body / 1:Offset Plane / 0:[Drawer]");
var got2 = groupAround(m, d, ["ROOT|Mirror"], [], A);
expect("an unnamed group is called after its one feature",
  mk(d).groups.find(function (g) { return g.id === "ug:" + got2.id; }).label, "Mirror");
expect("…and wears its kind's colour",
  mk(d).groups.find(function (g) { return g.id === "ug:" + got2.id; }).cls, "k-body");
expect("a mixed group stays white",
  mk(d).groups.find(function (g) { return g.id === "ug:g1"; }).cls, "");
explodeGroup(d, got2.id);

print("=== refusals ===");
var before = JSON.stringify(d);
var bad = groupAround(m, d, ["ROOT|Body", "DRW|Front"], [], A);
expect("rows from two components are refused", !!bad.error, true);
expect("…and nothing was written", JSON.stringify(d), before);
bad = groupAround(m, d, [], ["cg:DRW"], A);
expect("a component group can't be wrapped", !!bad.error, true);

print("=== nesting ===");
m = mk(d);
var inner = groupAround(m, d, ["ROOT|Offset Plane"], [], A);
expect("a group made from rows inside a group nests in it", d.groups[inner.id].parent, "g:g1");
m = mk(d);
expect("nested group draws one deeper",
  seq(m).split(" / ").slice(1, 5).join(" / "),
  "0:[Extrusion & Plane] / 1:Body / 1:[Plane] / 2:Offset Plane");
var wrap = groupAround(m, d, [], ["ug:g1"], A);
expect("⌘G on a group wraps it", d.groups.g1.parent, "g:" + wrap.id);
m = mk(d);
expect("…and the wrapper takes its place",
  seq(m).split(" / ").slice(1, 4).join(" / "),
  "0:[Extrusion & Plane] / 1:[Extrusion & Plane] / 2:Body");
var inside = addGroupInside(d, "cg:DRW", A);
expect("New Group Inside a component group belongs to that component", groupComp(d, inside), "DRW");
var drwGroup = groupAround(mk(d), d, ["DRW|Front", "DRW|Pull Chamfer"], [], A);
expect("rows of a sub-component group at that component's level", d.groups[drwGroup.id].parent, "c:DRW");
m = mk(d);
expect("the sub-component's group nests inside its component group",
  m.groups.find(function (g) { return g.id === "ug:" + drwGroup.id; }).parent, "cg:DRW");

print("=== auto names ===");
var X = "adsk::fusion::";
expect("one kind, several → plural", autoGroupName([X + "Sketch", X + "Sketch"]), "Sketches");
expect("one of a kind → singular", autoGroupName([X + "ConstructionPlane"]), "Plane");
expect("two kinds, commonest first",
  autoGroupName([X + "Sketch", X + "ExtrudeFeature", X + "ExtrudeFeature"]), "Extrusions & Sketch");
expect("the pattern kinds are one kind",
  autoGroupName([X + "RectangularPatternFeature", X + "CircularPatternFeature"]), "Patterns");
expect("three kinds → Group",
  autoGroupName([X + "Sketch", X + "ExtrudeFeature", X + "FilletFeature"]), "Group");
expect("an unlisted type is named from itself", autoGroupName([X + "DraftFeature", X + "DraftFeature"]), "Drafts");
expect("empty → Group", autoGroupName([]), "Group");
var an = { groups: { a: { name: "Group 3", parent: "c:ROOT" }, b: { name: "Mine", parent: "c:ROOT" } },
           member: { "ROOT|Body": "a", "ROOT|Top Fillet": "b" } };
m = mk(an);
expect("an old default name counts as unnamed",
  m.groups.find(function (g) { return g.id === "ug:a"; }).label, "Extrusion");
expect("a name the user gave stays",
  m.groups.find(function (g) { return g.id === "ug:b"; }).label, "Mine");

print("=== explode ===");
explodeGroup(d, wrap.id);
expect("exploding lifts a child group to the exploded group's parent", d.groups.g1.parent, "c:ROOT");
explodeGroup(d, inner.id);
expect("exploding lifts rows into the parent group", d.member["ROOT|Offset Plane"], "g1");
explodeGroup(d, "g1");
expect("at the top level the rows go loose", d.member["ROOT|Offset Plane"], undefined);
expect("Body is loose again", d.member["ROOT|Body"], undefined);
expect("the exploded group is gone", d.groups.g1, undefined);

print("=== wrong-side filing draws loose ===");
var w = emptyData();
w.groups.x = { name: "Root Stuff", parent: "c:ROOT" };
w.member["DRW|Front"] = "x";                     // a Drawer row filed in a root group
m = mk(w);
expect("the row stays in its own component's group",
  m.items.find(function (i) { return i.id === "DRW|Front"; }).gid, "cg:DRW");
expect("a group whose component is out of view isn't drawn",
  buildModel(R, C, A, { groups: { y: { name: "Gone", parent: "c:NOPE" } }, member: {} })
    .groups.some(function (g) { return g.id === "ug:y"; }), false);
expect("a looping chain isn't drawn",
  buildModel(R, C, A, { groups: { a: { name: "A", parent: "g:b" }, b: { name: "B", parent: "g:a" } }, member: {} })
    .groups.length, 3);

print("=== rename carry ===");
var rc = emptyData();
rc.groups.g1 = { name: "Group", parent: "c:ROOT" };
rc.member["ROOT|Body"] = "g1";
carryGroupKey(rc, "ROOT|Body", "ROOT|Main Body");
expect("a rename here moves the filing to the new key", rc.member["ROOT|Main Body"], "g1");
expect("…off the old one", rc.member["ROOT|Body"], undefined);
var renamed = clone(R);
renamed[1].name = "Shell Body"; renamed[1].key = "ROOT|Shell Body";      // tok t2 unchanged
rc.member = { "ROOT|Body": "g1" };
expect("a rename elsewhere is carried by entity token", carryByToken(rc, R, renamed), true);
expect("…to the new key", rc.member["ROOT|Shell Body"], "g1");
expect("an unchanged key carries nothing", carryByToken(rc, renamed, renamed), false);

print("=== collapse state ===");
var cs = emptyData();
cs.compCollapsed.DRW = true;
m = mk(cs);
expect("a collapsed component group says so", m.groups.find(function (g) { return g.id === "cg:DRW"; }).collapsed, true);

print("=== a user group's icon ===");
var ir = clone(R);
// Counted by TYPE (the glyphs follow the type), so every non-sketch is made an extrude.
ir.forEach(function (r) {
  r.icon = /Sketch/.test(r.name) ? "sk" : "ex";
  if (!/Sketch/.test(r.name)) r.type = "adsk::fusion::ExtrudeFeature";
});
var id_ = emptyData();
id_.groups.g1 = { name: "G", parent: "c:ROOT" };
id_.groups.g2 = { name: "Inner", parent: "g:g1" };
id_.member = { "ROOT|Base Sketch": "g1", "ROOT|Body": "g2", "ROOT|Offset Plane": "g2" };
var im = buildModel(ir, C, A, id_);
var gOf = function (id) { return im.groups.find(function (g) { return g.id === id; }); };
expect("the most frequent icon, nested features counted", gOf("ug:g1").icon, "ex");
expect("…and the type that draws it", gOf("ug:g1").iconType, "adsk::fusion::ExtrudeFeature");
expect("an empty group has none", (function () {
  id_.groups.g3 = { name: "Empty", parent: "c:ROOT" };
  return buildModel(ir, C, A, id_).groups.find(function (g) { return g.id === "ug:g3"; }).icon;
})(), "");
expect("a component group keeps the component icon", gOf("cg:DRW").icon, "");

print("=== dragging ===");
var dd = emptyData();
dd.groups.g1 = { name: "G", parent: "c:ROOT" };
dd.groups.g2 = { name: "Inner", parent: "g:g1" };
dd.groups.gd = { name: "In drawer", parent: "c:DRW" };
dd.member = { "ROOT|Body": "g1", "ROOT|Offset Plane": "g2", "DRW|Front": "gd" };
var dm = mk(dd);
var toks = function (rs) { return rs.map(function (r) { return r.tok; }).join(","); };
expect("a group drags every row under it, in timeline order",
  toks(dragRows(dm, { items: [], groups: ["ug:g1"] })), "t2,t10");
expect("a row and its own group count once",
  toks(dragRows(dm, { items: ["ROOT|Body"], groups: ["ug:g1"] })), "t2,t10");
expect("a component group is not draggable",
  dm.groups.find(function (g) { return g.id === "cg:DRW"; }).draggable, false);
expect("a user group is", dm.groups.find(function (g) { return g.id === "ug:g1"; }).draggable, true);
expect("before a row goes before its token, filed where it is",
  JSON.stringify(dropSpot(dm, { before: { item: "ROOT|Offset Plane" } })), '{"before":"t10","home":"ug:g2"}');
expect("before a group goes before its earliest row, filed beside it",
  JSON.stringify(dropSpot(dm, { before: { group: "ug:g1" } })), '{"before":"t2","home":""}');
expect("the floor is the end", JSON.stringify(dropSpot(dm, { end: true })), '{"before":"","home":""}');
expect("a heading's lower half files only", JSON.stringify(dropSpot(dm, { into: "ug:g1" })), '{"into":"ug:g1"}');
expect("a root row may file into a root group", canFileInto(dm, { items: ["ROOT|Mirror"], groups: [] }, "ug:g1", A), true);
expect("…not into the drawer's", canFileInto(dm, { items: ["ROOT|Mirror"], groups: [] }, "ug:gd", A), false);
expect("a group not inside itself", canFileInto(dm, { items: [], groups: ["ug:g1"] }, "ug:g2", A), false);
var fd = clone(dd);
fileAt(dm, fd, { items: ["ROOT|Mirror", "DRW|Pull Chamfer"], groups: [] }, "ug:g2", A);
expect("dropped among a group's rows files there", fd.member["ROOT|Mirror"], "g2");
expect("…but another component's row goes loose", fd.member["DRW|Pull Chamfer"], undefined);
fileAt(dm, fd, { items: ["ROOT|Offset Plane"], groups: ["ug:g2"] }, "", A);
expect("dropped at the top level unfiles", fd.member["ROOT|Offset Plane"], undefined);
expect("…and lifts a group out", fd.groups.g2.parent, "c:ROOT");

print("");
print(fails ? fails + " FAIL" : "ok");
