// Timeline names — what a timeline item (displayName) or an unnamed group of them
// (autoGroupName) is CALLED on screen, shared by the vertical
// timeline (fusion_timeline) and the Browser's feature rows (a body's features, drawn
// under it once it is opened with F). Plain global, like capitalise.js.

// What a row is CALLED here, not what it is named: Fusion's auto-numbering goes, as in
// the Browser (_display_name in Browser.py) — "Leg (1)", "Fuselage:1", "Body 2" — plus
// the feature counter glued to a default name ("Extrude9", "R-Pattern1"). Only after a
// lowercase letter, so "M3" or "HIPS2" keep theirs. Then it is spaced like text:
// CamelCase split into words ("SketchPlane" → "Sketch Plane", only lower→Upper, so
// "HIPS" stays whole) and a bracket set off with a space ("Legs(Mirror)" → "Legs
// (Mirror)", the Browser's spaceParens), and a cut-paste's "CutPaste" reads
// "Reorder". The real name is never changed.
function displayName(name){
  const n = String(name || "").trim();
  const s = n.replace(/\s*\(\d+\)\s*$/, "").replace(/:\d+$/, "").replace(/\s+\d+$/, "")
             .replace(/^([A-Za-z][A-Za-z-]*[a-z])\d+$/, "$1").trim();
  return (s || n).replace(/^CutPaste\b/, "Reorder")    // a cut-paste reads as what it does
                 .replace(/([a-z])([A-Z])/g, "$1 $2")
                 .replace(/([^\s(])\(/g, "$1 (").replace(/\)([^\s):,.;])/g, ") $1");
}

// A group nobody has named is called after what is in it. KIND_NAMES is [singular,
// plural] per objectType; a type missing here is named from its own type ("DraftFeature"
// → "Draft"/"Drafts"). A name the page used to give ("Group", "Group 3") counts as none.
const KIND_NAMES = {
  Sketch: ["Sketch", "Sketches"], Canvas: ["Canvas", "Canvases"],
  Occurrence: ["Component", "Components"],
  ExtrudeFeature: ["Extrusion", "Extrusions"],
  RectangularPatternFeature: ["Pattern", "Patterns"], CircularPatternFeature: ["Pattern", "Patterns"],
  PathPatternFeature: ["Pattern", "Patterns"],
  SplitBodyFeature: ["Split", "Splits"], SplitFaceFeature: ["Split", "Splits"],
  SilhouetteSplitFeature: ["Split", "Splits"],
  BoxFeature: ["Primitive", "Primitives"], CylinderFeature: ["Primitive", "Primitives"],
  SphereFeature: ["Primitive", "Primitives"], TorusFeature: ["Primitive", "Primitives"],
  RemoveFeature: ["Delete", "Deletes"], DeleteFaceFeature: ["Delete", "Deletes"],
  OffsetFacesFeature: ["Press Pull", "Press Pulls"],
  ConstructionPlane: ["Plane", "Planes"], ConstructionAxis: ["Axis", "Axes"],
  ConstructionPoint: ["Point", "Points"],
  Joint: ["Joint", "Joints"], AsBuiltJoint: ["Joint", "Joints"],
};
const AUTO_NAME = /^(Group( \d+)?)?$/;
function kindName(type){
  const t = String(type || "").replace(/^.*::/, "");
  if (KIND_NAMES[t]) return KIND_NAMES[t];
  const one = t.replace(/Feature$/, "").replace(/([a-z])([A-Z])/g, "$1 $2");
  return one ? [one, /s$/.test(one) ? one : one + "s"] : null;
}
// "Sketches", "Sketches & Extrusions" (by count, then timeline order); "Group" for
// three kinds or more, or an empty group. `types` = its rows' objectTypes, in order.
function autoGroupName(types){
  const count = new Map();
  for (const t of types){
    const k = kindName(t);
    if (!k) continue;
    const c = count.get(k[1]) || { k, n: 0 };
    c.n++; count.set(k[1], c);
  }
  const kinds = [...count.values()].sort((a, b) => b.n - a.n);
  const say = c => c.k[c.n > 1 ? 1 : 0];
  if (kinds.length === 1) return say(kinds[0]);
  if (kinds.length === 2) return say(kinds[0]) + " & " + say(kinds[1]);
  return "Group";
}
