// Timeline icons — the glyph a timeline item draws and the kind colour it wears,
// shared by the vertical timeline (fusion_timeline) and the Browser's feature rows.
// Plain globals, like tl_names.js. Items we draw no glyph for keep Fusion's own icon,
// served by the timeline's server as /icon/<rel> (icons.json).

// The Browser's kind colour a feature wears: a sketch is a sketch, construction
// geometry is Origin, and any other *Feature makes or edits a body — except Move,
// which moves components as often as bodies and so stays white with them (like a
// new component, a joint, a canvas).
function kindCls(type){
  const t = String(type || "").replace(/^adsk::fusion::/, "");
  if (t === "Sketch") return "k-sketch";
  if (/^Construction(Plane|Axis|Point)$/.test(t)) return "k-origin";
  if (/Feature$/.test(t) && t !== "MoveFeature") return "k-body";
  return "";
}

// Our own glyphs for every COLOURED kind (sketch, construction, body features): 16px
// line art stroked in currentColor, so .tico's colour — the row's kind colour — draws
// them. F = a translucent fill, D = dashed; the rest is outline. A body feature not
// listed draws BODY. WHITE_GLYPHS are the white rows we draw too (a component coming
// in, a move); the other white rows (joints…) keep Fusion's icon.
const F = ' fill="currentColor" fill-opacity=".28"', D = ' stroke-dasharray="1.6 1.6"';
const BODY = '<path d="M8 1.8L13.8 5v6L8 14.2 2.2 11V5z"' + F + '/><path d="M2.2 5L8 8.2 13.8 5M8 8.2v6"/>';
const GLYPHS = {
  Sketch: '<rect x="1.5" y="5.5" width="8" height="8" rx=".5"/><path d="M6 10.5l.5-2.5 6-6 2 2-6 6z"' + F + '/>',
  ConstructionPlane: '<path d="M1.5 12L5 4h9.5L11 12z"' + F + '/>',
  ConstructionAxis: '<path d="M2.5 13.5l11-11"/><circle cx="8" cy="8" r="1.6"' + F + '/>',
  ConstructionPoint: '<circle cx="8" cy="8" r="2.2"' + F + '/><path d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3"/>',
  ExtrudeFeature: '<path d="M1.5 13.5L4 10.5h10.5L12 13.5z"' + F + '/><path d="M8 11.5V2.5M5.5 5L8 2.5 10.5 5"/>',
  // An extrude's operation (the add-in's `op`): join / new body draw the plain one.
  "ExtrudeFeature#cut": '<path d="M1.5 6.5H5V10h6V6.5h3.5v7.5h-13z"' + F + '/><path d="M8 1.5v6.5M5.5 5.5L8 8l2.5-2.5"/>',
  "ExtrudeFeature#intersect": '<rect x="1.5" y="5.5" width="9" height="9"/><rect x="5.5" y="1.5" width="9" height="9"' + D + '/><rect x="5.5" y="5.5" width="5" height="5"' + F.replace(".28", ".6") + ' stroke="none"/>',
  RevolveFeature: '<path d="M8 1.5v13"' + D + '/><path d="M3 11A5.5 2.5 0 1 0 8 6.5"/><path d="M6.4 5.2L8 6.5 6.4 7.8"/>',
  SweepFeature: '<ellipse cx="3" cy="12" rx="1.5" ry="2.5"' + F + '/><path d="M3 9.5C3 5 8 8 13.5 3M10.5 2.5h3v3"/>',
  LoftFeature: '<ellipse cx="8" cy="12.5" rx="5.5" ry="2"' + F + '/><rect x="5.5" y="2" width="5" height="3"/><path d="M2.5 12.5L5.5 5M13.5 12.5L10.5 5"/>',
  RibFeature: '<path d="M2.5 2.5v11h11"/><path d="M2.5 5.5l8 8h-8z"' + F + '/>',
  WebFeature: '<path d="M2 13.5h12M4 13.5V7M8 13.5V4M12 13.5V7M4 7l4-3 4 3"/>',
  HoleFeature: '<rect x="2" y="2" width="12" height="12" rx=".5"/><circle cx="8" cy="8" r="3"' + F + '/>',
  ThreadFeature: '<rect x="5" y="1.5" width="6" height="13" rx=".5"/><path d="M5 4.5l6 1.5M5 7.5l6 1.5M5 10.5l6 1.5"/>',
  FilletFeature: '<path d="M2.5 8v5.5H8"' + D + '/><path d="M2.5 2.5V8A5.5 5.5 0 0 0 8 13.5h5.5"/>',
  ChamferFeature: '<path d="M2.5 8v5.5H8"' + D + '/><path d="M2.5 2.5V8L8 13.5h5.5"/>',
  ShellFeature: '<path d="M2 2v12h12V2M4.5 2v9.5h7V2"/><path d="M2 14h12V2h-2.5v9.5h-7V2H2z"' + F + ' stroke="none"/>',
  DraftFeature: '<path d="M4 2.5v11M12 2.5v11"' + D + '/><path d="M4 2.5L2 13.5h12L12 2.5z"' + F + '/>',
  ThickenFeature: '<path d="M2 5c4-3 8 3 12 0M2 11c4-3 8 3 12 0"/><path d="M8 5.5v4.5"' + D + '/>',
  BoxFeature: BODY,
  CylinderFeature: '<ellipse cx="8" cy="4" rx="5" ry="2"' + F + '/><path d="M3 4v8a5 2 0 0 0 10 0V4"/>',
  SphereFeature: '<circle cx="8" cy="8" r="6"/><ellipse cx="8" cy="8" rx="6" ry="2"' + F + '/>',
  TorusFeature: '<ellipse cx="8" cy="8" rx="6.5" ry="4.5"' + F + '/><ellipse cx="8" cy="8" rx="2.5" ry="1.3"/>',
  PipeFeature: '<ellipse cx="3.5" cy="8" rx="1.5" ry="4"' + F + '/><path d="M3.5 4H13M3.5 12H13M13 4a1.5 4 0 0 1 0 8"/>',
  CoilFeature: '<path d="M4 2.5l8 1.5-8 1.5 8 1.5-8 1.5 8 1.5-8 1.5 8 1.5"/>',
  RectangularPatternFeature: '<rect x="2" y="2" width="5" height="5"' + F + '/><rect x="9" y="2" width="5" height="5"/><rect x="2" y="9" width="5" height="5"/><rect x="9" y="9" width="5" height="5"/>',
  CircularPatternFeature: '<circle cx="8" cy="2.8" r="1.6"' + F + '/><circle cx="13" cy="6.4" r="1.6"/><circle cx="11.1" cy="12.2" r="1.6"/><circle cx="4.9" cy="12.2" r="1.6"/><circle cx="3" cy="6.4" r="1.6"/><circle cx="8" cy="8" r=".6"/>',
  PathPatternFeature: '<path d="M1.5 13C5 13 5 4 14.5 3"' + D + '/><rect x="1.5" y="10" width="3.5" height="3.5"' + F + '/><rect x="5.5" y="5.5" width="3.5" height="3.5"/><rect x="10.5" y="1.5" width="3.5" height="3.5"/>',
  MirrorFeature: '<path d="M8 1.5v13"' + D + '/><path d="M6.5 3L2 12.5h4.5z"' + F + '/><path d="M9.5 3l4.5 9.5H9.5z"/>',
  CombineFeature: '<rect x="2" y="2" width="8" height="8"/><rect x="6" y="6" width="8" height="8"' + F + '/>',
  SplitBodyFeature: '<path d="M2 3h6l-2 10H2z"' + F + '/><path d="M10 3h4v10H8z"/>',
  SplitFaceFeature: '<rect x="2" y="2" width="12" height="12" rx=".5"/><path d="M2 10.5l12-5V14H2z"' + F + '/>',
  SilhouetteSplitFeature: '<circle cx="8" cy="8" r="5.5"/><path d="M1 8h14"/><path d="M2.5 8a5.5 5.5 0 0 0 11 0z"' + F + '/>',
  ScaleFeature: '<rect x="2" y="2" width="12" height="12"' + D + '/><rect x="2" y="9" width="5" height="5"' + F + '/><path d="M7.5 8.5L12 4M9 4h3v3"/>',
  RemoveFeature: '<rect x="2" y="2" width="12" height="12" rx=".5"/><path d="M5 5l6 6M11 5l-6 6"/>',
  DeleteFaceFeature: '<rect x="2" y="2" width="12" height="12" rx=".5"' + F + '/><path d="M5 5l6 6M11 5l-6 6"/>',
  ReplaceFaceFeature: '<rect x="2" y="7" width="12" height="7" rx=".5"' + F + '/><path d="M8 1.5V6M6 4l2 2 2-2"/>',
  OffsetFacesFeature: '<rect x="2" y="5" width="8" height="8"' + F + '/><path d="M10 9h4.5M12.5 7l2 2-2 2"/>',
  PatchFeature: '<path d="M3 5c2-4 8-4 10 0s-2 9-6 8-6-4-4-8z"' + F + '/>',
  StitchFeature: '<path d="M2 3h5.5v10H2zM8.5 3H14v10H8.5z"' + F + '/><path d="M6 5.5h4M6 8h4M6 10.5h4"/>',
  UnstitchFeature: '<path d="M1.5 3h5v10h-5z"' + F + '/><path d="M9.5 3h5v10h-5z"/>',
  TrimFeature: '<path d="M9 2l5 5"' + D + '/><path d="M9 2H2v12h12V7z"' + F + '/>',
  UntrimFeature: '<rect x="2" y="2" width="12" height="12"' + F + '/><circle cx="8" cy="8" r="2.5"' + D + '/>',
  OffsetFeature: '<path d="M2 11c4-3 8 3 12 0"/><path d="M2 5c4-3 8 3 12 0"' + D + '/>',
  ExtendFeature: '<path d="M2 4h7v8H2z"' + F + '/><path d="M9 4h5v8H9"' + D + '/>',
  RuledSurfaceFeature: '<path d="M2 12c3-3 7 1 12-2"/><path d="M4 11V5M7 10.2V4.2M10 11V5M13 10.4V4.4"' + D + '/>',
};

const COMP = '<path d="M7 1.8L12.3 4.8v5.8L7 13.6 1.7 10.6V4.8z"/><path d="M1.7 4.8L7 7.8l5.3-3M7 7.8v5.8"/>';
const WHITE_GLYPHS = {
  Occurrence: COMP + '<path d="M12.5 10.5v4M10.5 12.5h4" stroke-width="1.4"/>',
  MoveFeature: '<path d="M8 1.5v13M1.5 8h13M6 3.5l2-2 2 2M6 12.5l2 2 2-2M3.5 6l-2 2 2 2M12.5 6l2 2-2 2"/>',
};

// The glyph a type draws here, or "" to take Fusion's icon. A glyph type may carry the
// feature's operation after a '#' ("…ExtrudeFeature#cut", `glyphKey`); an op with no
// glyph of its own draws the plain type's.
function glyphOf(type){
  const [base, op] = String(type || "").split("#");
  const t = base.replace(/^adsk::fusion::/, "");
  if (!kindCls(base)) return WHITE_GLYPHS[t] || "";
  return (op && GLYPHS[t + "#" + op]) || GLYPHS[t] || BODY;
}
// A row's glyph type: its objectType, plus the add-in's `op` when there is one.
function glyphKey(type, op){ return op ? type + "#" + op : (type || ""); }


// The glyph as an <svg> string ("" → take Fusion's icon), for pages that build HTML.
function glyphSVG(type){
  const g = glyphOf(type);
  return g ? '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"'
           + ' stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">' + g + '</svg>' : "";
}
