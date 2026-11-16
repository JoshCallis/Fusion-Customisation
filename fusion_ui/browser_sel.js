/* ─────────────────────────────────────────────────────────────────────────────
   browser_sel.js — the Fusion Browser's selection-highlight lift.

   Control Room ▸ Light / Dark carries one slider per appearance (light / dark /
   xdark / xlight, fusion_ui/browser_sel.json), -50..50: how far to push the
   Browser's row-selection highlight (--sel, fusion_ui/theme.css) toward white or
   black. This script mirrors the current appearance's value onto
   --sel-lighten/--sel-darken (fusion_browser/index.css), each a non-negative
   percent so the color-mix there never needs a signed argument.

   Same poll-and-apply shape as appearance.js, and deliberately reads the SAME
   data-xdark/data-xlight attributes that script sets rather than re-deriving
   them, so the two never disagree about which appearance is current. Load this
   after appearance.js for that reason.
   ──────────────────────────────────────────────────────────────────────────── */
(function () {
  var root = document.documentElement;
  var dark = window.matchMedia("(prefers-color-scheme: dark)");
  var levels = { light: 0, dark: 0, xdark: 0, xlight: 0 };

  function mode() {
    if (dark.matches) return root.hasAttribute("data-xdark") ? "xdark" : "dark";
    return root.hasAttribute("data-xlight") ? "xlight" : "light";
  }

  function apply() {
    var lift = Number(levels[mode()]) || 0;
    root.style.setProperty("--sel-lighten", Math.max(0, lift) + "%");
    root.style.setProperty("--sel-darken", Math.max(0, -lift) + "%");
  }

  function parse(text) {
    try {
      var d = JSON.parse(text);
      return { light: +d.light || 0, dark: +d.dark || 0, xdark: +d.xdark || 0, xlight: +d.xlight || 0 };
    } catch (e) { return levels; }
  }

  try {
    var x = new XMLHttpRequest();
    x.open("GET", "/browser_sel.json?t=" + Date.now(), false);
    x.send();
    if (x.status === 200) { levels = parse(x.responseText); apply(); }
  } catch (e) { /* no server answer: stay at 0/0, today's --sel unchanged */ }

  var busy = false;
  function check() {
    if (busy) return;
    busy = true;
    fetch("/browser_sel.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (t) { if (t !== null) { levels = parse(t); apply(); } })
      .catch(function () {})
      .then(function () { busy = false; });
  }
  setInterval(check, 1000);
  window.addEventListener("focus", check);
  document.addEventListener("visibilitychange", check);
  dark.addEventListener("change", apply);
})();
