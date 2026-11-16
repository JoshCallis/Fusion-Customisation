/* Cases for keys.js. Run it:
 *
 *     /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc \
 *       fusion_ui/keys.js fusion_ui/keys_test.js
 *
 * The chord spelling is pure; the panel is driven through a stub document and a
 * stub fetch, far enough to prove the dispatch rules the Browser and Params windows
 * rely on: custom bindings fire, `blocked` stops them, a general chord fires only
 * where the page asked for that, and `onGeneral` consumes what it claims.
 */
var fails = 0;
function check(label, got, want) {
  var g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { print("FAIL " + label + ": got " + g + " want " + w); fails++; }
  else print("  PASS  " + label);
}

// ── The chord spelling ───────────────────────────────────────────────────────
check("normSeq reorders Fusion's spelling", Keys.normSeq("shift+alt+c"), "alt+shift+c");
check("normSeq folds the long names", Keys.normSeq("Command+Option+Control+x"), "cmd+ctrl+alt+x");
check("normSeq of nothing", Keys.normSeq(null), "");
check("seqOf matches normSeq order",
      Keys.seqOf({ metaKey: true, shiftKey: true, altKey: true, key: "E" }), "cmd+alt+shift+e");
check("seqLabel glyphs", Keys.seqLabel("shift+cmd+e"), "⌘⇧E");
check("seqLabel named keys", [Keys.seqLabel("arrowdown"), Keys.seqLabel("alt+enter")], ["↓", "⌥↵"]);
check("normSeq: held space leads, bare space is the key",
      [Keys.normSeq("shift+space+t"), Keys.normSeq("space"), Keys.normSeq("shift+space")],
      ["space+shift+t", "space", "shift+space"]);
check("seqLabel held space", Keys.seqLabel("space+t"), "␣T");
check("lookup without space held", Keys.lookup({ t: "Trim", "space+t": "Toggle" }, { key: "t" }), "Trim");

// ── Space + C / R: section jumps ─────────────────────────────────────────────
// 0 Comp A (band 0)        5 Sketch 2 (2)
// 1   Sketch a (2)         6 Param x (3)
// 2   Sketch b (2)         7 Param y (3)
// 3 Comp B (0)             8 Origin (4)
// 4 Sketch 1 (2)
var JR = [[0,0],[1,2],[1,2],[0,0],[0,2],[0,2],[0,3],[0,3],[0,4]]
  .map(function (r) { return { depth: r[0], band: r[1] }; });
function walk(at, dir, times) {
  var out = [];
  for (var t = 0; t < times; t++) out.push(at = Keys.sectionJump(JR, at, dir));
  return out;
}
check("jump down: section end, then each next section's end, then wraps to the top",
      walk(4, 1, 5), [5, 7, 8, 0, 3]);
check("jump up: section top, then each previous section's top, then wraps to the bottom",
      walk(7, -1, 5), [6, 4, 0, 8, 6]);
check("jump up from the first row wraps to the last top-level row, not a row inside it",
      Keys.sectionJump([{ depth: 0 }, { depth: 1 }, { depth: 0 }, { depth: 1 }], 0, -1), 2);
check("jump down inside a group: its end, then up a level", walk(1, 1, 3), [2, 3, 5]);
check("jump up inside a group: its top, then the level above", walk(2, -1, 2), [1, 0]);
check("jump with no cursor lands on the first section's edges",
      [Keys.sectionJump(JR, -1, 1), Keys.sectionJump(JR, -1, -1)], [3, 0]);
check("jump with no bands is plain first / last sibling",
      [Keys.sectionJump([{ depth: 0 }, { depth: 0 }, { depth: 0 }], 1, 1),
       Keys.sectionJump([{ depth: 0 }, { depth: 0 }, { depth: 0 }], 1, -1)], [2, 0]);
check("jump on an empty list", Keys.sectionJump([], 0, 1), -1);

// ── The panel ────────────────────────────────────────────────────────────────
var document = { getElementById: function () { return null; } };
var localStorage = { getItem: function () { return null; }, setItem: function () {} };
var posted = [];
var SERVER = {
  "/general": { actions: [{ id: "rename", chord: "cmd+r" }, { id: "reveal", chord: "ctrl" },
                         { id: "ungroup", chord: "cmd+shift+g" }] },
  "/settings": { custom: { "go": ["cmd+g"] } },
};
var fetch = function (url, init) {
  if (init && init.method === "POST") { posted.push([url, JSON.parse(init.body)]); return Promise.resolve({}); }
  var path = url.replace(/^https?:\/\/[^/]+/, "");
  return Promise.resolve({ json: function () { return Promise.resolve(SERVER[path]); } });
};

function key(k, mods) {
  var e = { key: k, prevented: false, stopped: false,
            preventDefault: function () { this.prevented = true; },
            stopPropagation: function () { this.stopped = true; } };
  for (var m in (mods || {})) e[m] = mods[m];
  return e;
}

var ran = [], editing = false, reveal = null;
var ACTIONS = {
  "go":     { label: "Go",     scope: "shared", def: [], run: function () { ran.push("go"); } },
  "rename": { label: "Rename", scope: "shared", def: [], general: "rename", run: function () { ran.push("rename"); } },
  "ungroup": { label: "Ungroup", scope: "shared", def: [], general: "ungroup", dispatch: true,
               run: function () { ran.push("ungroup"); } },
};
var dispatching = Keys.panel({ api: "http://127.0.0.1:1", actions: ACTIONS, sections: [["S", "shared"]],
                               dispatchGeneral: true, blocked: function () { return editing; },
                               onGeneral: function (a) { if (a.id !== "reveal") return false; reveal = a.chord; return true; } });
var showing = Keys.panel({ actions: ACTIONS, sections: [["S", "shared"]] });

Promise.all([dispatching.load(), showing.load()]).then(function () {
  check("onGeneral consumed the reveal verb", reveal, "ctrl");

  var e = key("g", { metaKey: true });
  dispatching.onCustomKey(e);
  check("a custom binding runs its action", [ran, e.prevented, e.stopped], [["go"], true, true]);

  ran = []; editing = true;
  dispatching.onCustomKey(key("g", { metaKey: true }));
  check("blocked() stops a custom binding", ran, []);
  editing = false;

  ran = [];
  dispatching.onCustomKey(key("r", { metaKey: true }));
  showing.onCustomKey(key("r", { metaKey: true }));
  check("a general chord runs only where dispatchGeneral is set", ran, ["rename"]);

  ran = [];
  showing.onCustomKey(key("g", { metaKey: true, shiftKey: true }));
  check("an action with dispatch: true runs its general chord without dispatchGeneral", ran, ["ungroup"]);

  ran = [];
  dispatching.onCustomKey(key("x"));
  check("an unbound key does nothing", ran, []);
  check("not capturing when nothing asked to", dispatching.capturing(), false);
}).then(function () {
  print(fails ? "\n" + fails + " FAILED" : "\nall keys.js cases passed");
}, function (err) {
  print("FAIL exception: " + err + "\n" + (err && err.stack)); fails++;
});
