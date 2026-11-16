// Timeline groups — the window-only user groups (groups.json, served by the timeline's
// server on 8736), shared by the vertical timeline and the Browser's feature rows: both
// read and write the same data, so a group made in either shows in both. Pure edits
// on `data` = {groups: {id: {name, parent, collapsed}}, member: {rowKey: id},
// compCollapsed}; `parent` is "c:<compRef>" or "g:<id>". Row key = "<compRef>|<name>".
// Plain globals, like tl_names.js.

function emptyData(){ return { groups: {}, member: {}, compCollapsed: {} }; }

function normData(d){
  const obj = x => (x && typeof x === "object" && !Array.isArray(x)) ? x : {};
  d = obj(d);
  d.groups = obj(d.groups); d.member = obj(d.member); d.compCollapsed = obj(d.compCollapsed);
  return d;
}

// The component a user group belongs to: the "c:<ref>" its parent chain ends at, or
// null for a broken chain (a missing group, or a loop).
function groupComp(data, gid){
  const seen = new Set();
  while (gid != null && data.groups[gid]){
    if (seen.has(gid)) return null;
    seen.add(gid);
    const p = String(data.groups[gid].parent || "");
    if (p.startsWith("c:")) return p.slice(2);
    if (!p.startsWith("g:")) return null;
    gid = p.slice(2);
  }
  return null;
}

function newGroupId(data){
  let n = 1;
  while (data.groups["g" + n]) n++;
  return "g" + n;
}

// Explode: the group goes, its rows and groups lift into its parent.
function explodeGroup(data, gid){
  const g = data.groups[gid];
  if (!g) return false;
  const p = String(g.parent || "");
  for (const k of Object.keys(data.member)){
    if (data.member[k] !== gid) continue;
    if (p.startsWith("g:")) data.member[k] = p.slice(2); else delete data.member[k];
  }
  for (const id of Object.keys(data.groups))
    if (data.groups[id].parent === "g:" + gid) data.groups[id].parent = p;
  delete data.groups[gid];
  return true;
}

// A row's key is compRef|name, so a rename moves its filing to the new key.
function carryGroupKey(data, from, to){
  if (!from || !to || from === to || data.member[from] === undefined) return false;
  if (data.member[to] === undefined) data.member[to] = data.member[from];
  delete data.member[from];
  return true;
}
