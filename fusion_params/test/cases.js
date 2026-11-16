/* Assertions for the parameter editor's derived-group placement. */

ParamNaming.setVocabulary(PARAM_NAMING_VOCAB);
paramNamingReady = true;

var fails = 0;
function expect(label, got, want) {
  var ok = String(got) === String(want);
  if (!ok) fails++;
  print((ok ? "  PASS  " : "  FAIL  ") + label +
        (ok ? "" : "\n          got  " + got + "\n          want " + want));
}

// A fresh document — reconcileTree() only ever creates groups under a real tree.
switchDoc("testdoc");

function groupNamed(name, arr) {
  arr = arr || state.tree;
  for (const n of arr) {
    if (n.type === "group" && n.name === name) return n;
    if (n.type === "group") { const r = groupNamed(name, n.children); if (r) return r; }
  }
  return null;
}
function pathOf(node) {
  // The chain of group NAMES from the root down to (not including) `node` itself.
  function rec(arr, chain) {
    for (const n of arr) {
      if (n === node) return chain;
      if (n.type === "group") { const r = rec(n.children, chain.concat(n.name)); if (r) return r; }
    }
    return null;
  }
  return rec(state.tree, []);
}

print("=== derived groups: a structured name organizes itself ===");
params = [{ name: "Boot_Ceiling_Lip_Inset_Back", expression: "10 mm" }];
reconcileTree();
var ceiling = groupNamed("Ceiling");
expect("the innermost group is created", !!ceiling, true);
expect("nested under the component's own group", pathOf(ceiling).join(","), "Boot");
expect("the parameter itself is inside it", ceiling.children.some(c => c.type === "param" && c.name === "Boot_Ceiling_Lip_Inset_Back"), true);
expect("nothing landed at the root", state.tree.some(n => n.type === "param"), false);
params.push({ name: "Boot_Width_Span_X", expression: "400 mm" });
reconcileTree();
// One level past the owner is always a group — never a bare name at the component's
// own top — so "Width" gets its own group even alone.
expect("a single level past the owner is a group, not a name", !!groupNamed("Width"), true);
expect("...holding the parameter, not sitting loose in the component group",
       groupNamed("Width").children.some(c => c.type === "param" && c.name === "Boot_Width_Span_X"),
       true);
params.pop(); reconcileTree();

print("");
print("=== a second parameter under the same levels reuses the group ===");
params.push({ name: "Boot_Ceiling_Lip_Inset_Front", expression: "12 mm" });
reconcileTree();
var boot = groupNamed("Boot");
expect("still exactly one Boot group", state.tree.filter(n => n.type === "group" && n.name === "Boot").length, 1);
expect("still exactly one Ceiling group inside it", boot.children.filter(n => n.type === "group" && n.name === "Ceiling").length, 1);
expect("both parameters share it", groupNamed("Ceiling").children.filter(n => n.type === "param").length, 2);

print("");
print("=== a deeper level nests one group inside another ===");
params.push({ name: "Boot_Ceiling_Front_Rail_Span_X", expression: "400 mm" });
reconcileTree();
var front = groupNamed("Front");
expect("Front nests inside Ceiling, not beside it", pathOf(front).join(","), "Boot,Ceiling");

print("");
print("=== an old-convention (or otherwise unparsed) name is placed as it always was ===");
params.push({ name: "EdgeOffsetX", expression: "5 mm" });
reconcileTree();
expect("lands at the root, ungrouped", state.tree.some(n => n.type === "param" && n.name === "EdgeOffsetX"), true);

print("");
print("=== reconciling again doesn't move anything already placed ===");
var beforeBootChildCount = boot.children.length;
reconcileTree();
expect("no duplicate groups or params appear", boot.children.length, beforeBootChildCount);

print("");
print("=== an explicit pendingInsert selection still wins over derived placement ===");
switchDoc("testdoc2");
params = [{ name: "Handle_Grip_Radius", expression: "3 mm" }];
var existing = { id: newId(), type: "group", name: "Elsewhere", collapsed: false, children: [] };
state.tree.push(existing);
pendingInsert = { name: "Handle_Grip_Radius", select: true, insideId: existing.id };
reconcileTree();
expect("the explicit destination is honoured", existing.children.some(c => c.name === "Handle_Grip_Radius"), true);
expect("no derived Handle group was created instead", groupNamed("Handle"), null);

print("");
print("=== nested edit stack: navigating into a referenced parameter ===");
params = [
  { name: "Boot_Width", expression: "400 mm" },
  { name: "Boot_Height", expression: "720 mm" }
];
state.selected = new Set();

// enterExpression does a real DOM lookup this harness can't provide (no virtual
// DOM here) — recorded instead, so the STACK LOGIC around it is what's under test.
var enterExpressionCalls = [];
var origEnterExpression = enterExpression;
enterExpression = function (name) {
  enterExpressionCalls.push({ name: name, navigatingNested: navigatingNested });
  // Just enough of the real thing for beginEdit()'s selectSingle to have somewhere
  // sane to land, so "current" in the breadcrumb reads correctly.
  state.selected = new Set(["param:" + name]);
};

expect("wordAt finds the identifier under the caret",
       wordAt("Boot_Width * 2", 3), "Boot_Width");
expect("wordAt at a boundary still finds it", wordAt("Boot_Width", 0), "Boot_Width");
expect("wordAt on punctuation finds nothing", wordAt("1 * 2", 2), null);

var fakeInput = { value: "Boot_Width * 2", selectionStart: 3, dataset: { param: "Boot_Height" } };
editStack = [];
tryEnterNested(fakeInput);
expect("a real, DIFFERENT parameter pushes the CURRENT row onto the stack",
       editStack.join(","), "Boot_Height");
expect("enterExpression was called for the REFERENCED parameter",
       enterExpressionCalls[enterExpressionCalls.length - 1].name, "Boot_Width");
expect("...with navigatingNested true, so enterEdit won't reset the trail",
       enterExpressionCalls[enterExpressionCalls.length - 1].navigatingNested, true);
expect("navigatingNested is false again once the call returns", navigatingNested, false);

var notAWord = { value: "42", selectionStart: 1, dataset: { param: "Boot_Height" } };
enterExpressionCalls = [];
tryEnterNested(notAWord);
expect("no word under the caret: nothing happens", enterExpressionCalls.length, 0);

var ownName = { value: "Boot_Height + 1", selectionStart: 3, dataset: { param: "Boot_Height" } };
enterExpressionCalls = [];
editStack = [];
tryEnterNested(ownName);
expect("a parameter's own name in its own expression doesn't nest into itself",
       enterExpressionCalls.length, 0);

var unknownName = { value: "SomeOtherThing * 2", selectionStart: 3, dataset: { param: "Boot_Height" } };
enterExpressionCalls = [];
tryEnterNested(unknownName);
expect("a word that ISN'T a real parameter doesn't nest either",
       enterExpressionCalls.length, 0);

print("");
print("=== stepping back and jumping the breadcrumb ===");
editStack = ["Boot_Height", "Boot_Width"];
enterExpressionCalls = [];
var stepped = stepEditBack();
expect("stepEditBack reports it moved", stepped, true);
expect("...popping the most recently pushed name", editStack.join(","), "Boot_Height");
expect("...and reopening it", enterExpressionCalls[0].name, "Boot_Width");

expect("stepEditBack on an empty trail reports nothing to do",
       (function () { editStack = []; return stepEditBack(); })(), false);

editStack = ["A", "B", "C"];
enterExpressionCalls = [];
jumpEditBreadcrumb(0);
expect("jumping to depth 0 truncates the trail to just what's before it",
       editStack.join(","), "");
expect("...and reopens THAT one directly, not one step at a time",
       enterExpressionCalls.length, 1);
expect("...the right one", enterExpressionCalls[0].name, "A");

enterExpression = origEnterExpression;
editStack = [];
params = [];

print("");
print(fails ? "FAIL: " + fails + " assertion(s)" : "all assertions passed");
