/* ─────────────────────────────────────────────────────────────────────────────
   appearance.js — the extra appearances, Extra Dark and Extra Light.

   macOS knows Light and Dark, and theme.css follows those through
   prefers-color-scheme. The extras are macOS's own appearance PLUS a flag file,
   fusion_ui/extra.json ({"on": true}), which the Light → Dark → Extra Dark →
   Extra Light cycle (contrast_theme/toggle_appearance.sh) writes. This script
   mirrors that flag onto <html>: data-xdark when the system is Dark, data-xlight
   when it is Light — never both, so a rule names the one it means and no rule
   needs its own media query.

   Loaded in <head>, before the stylesheets paint: the first read is a SYNCHRONOUS
   request to this window's own local server, so a window never opens in the wrong
   palette and then flips. After that it re-reads about once a second (a hidden
   WKWebView throttles that to its own pace, which is fine — nobody is looking)
   and whenever the window is focused or shown, so a toggle reaches every open
   window without a reload. A system appearance change re-applies at once.

   SUPER DARK COMPENSATION. Under Super Dark macOS's red Colour Tint shows only
   the channel(s) the tint passes — measured with MediaAccessibility's own
   filter: pure red is the matrix diag(1, 0, 0), so the screen shows the source's
   RED channel and nothing else. A blue or green has little red, so it went near
   black. The page is drawn through an SVG filter that lifts the tint's strongest
   channel until the colour's brightness ON SCREEN matches its normal luminance
   (relative to white, which the tint dims the same way) — never lowers it, so
   reds and oranges stay as they are. Strength comes from /sdark_comp.json
   (Control Room ▸ Light / Dark ▸ Super Dark filter ▸ Compensation): 0 = off,
   100% = luminance-matched, above that brighter still.
   ──────────────────────────────────────────────────────────────────────────── */
(function () {
  var root = document.documentElement;
  var dark = window.matchMedia("(prefers-color-scheme: dark)");
  var on = false, sup = false;

  // data-sdark (Super Dark = Extra Dark + {"super": true}) rides ON TOP of data-xdark:
  // theme.css § Super Dark only swaps the blues, which macOS's red Colour Tint blacks out.
  function apply() {
    root.toggleAttribute("data-xdark", on && dark.matches);
    root.toggleAttribute("data-xlight", on && !dark.matches);
    root.toggleAttribute("data-sdark", on && sup && dark.matches);
    checkComp();
  }

  // ── Super Dark compensation ────────────────────────────────────────────────
  var COEF = [0.2126, 0.7152, 0.0722];   // linear-light luminance weights
  var comp = null, compKey = "", compBusy = false;

  // HSV(hue, 1, 1) — the colour Colour Tint passes (the probe: hue .5 = diag(0, 1, 1)).
  function tintRGB(hex) {
    var r = parseInt(hex.substr(1, 2), 16) / 255, g = parseInt(hex.substr(3, 2), 16) / 255,
        b = parseInt(hex.substr(5, 2), 16) / 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn, h = 0;
    if (d) h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h = ((h / 6) % 1 + 1) % 1;
    return [5, 3, 1].map(function (n) {
      var k = (n + h * 6) % 6;
      return 1 - Math.max(0, Math.min(k, 4 - k, 1));
    });
  }

  // The colour matrix (feColorMatrix "values", 4x5) for tint `c` at strength `s`.
  // D = what the tint keeps of each channel; w = each channel's share of on-screen
  // luminance; k = white's. Channel j (largest w) gets s * (k*Y - shown) / w[j] more,
  // where Y is the colour's normal luminance and shown its luminance under the tint.
  function compMatrix(c) {
    var t = tintRGB(/^#[0-9a-f]{6}$/i.test(c.color || "") ? c.color : "#ff0000");
    var i = Math.max(0, Math.min(1, (c.intensity == null ? 100 : c.intensity) / 100));
    var s = Math.max(0, (c.compensate == null ? 100 : c.compensate) / 100);
    var D = t.map(function (v) { return 1 + i * (v - 1); });
    var w = D.map(function (v, n) { return v * COEF[n]; });
    var k = w[0] + w[1] + w[2];
    var j = w.indexOf(Math.max(w[0], w[1], w[2]));
    var rows = [];
    for (var r = 0; r < 4; r++) {
      var row = [0, 0, 0, 0, 0];
      row[r] = 1;
      if (r === j) for (var n = 0; n < 3; n++) row[n] += s * (k * COEF[n] - w[n]) / w[j];
      rows.push(row.map(function (v) { return +v.toFixed(5); }).join(" "));
    }
    return rows.join("  ");
  }

  // feBlend "lighten" = per-channel max of the page and its lifted copy: nothing darkens.
  function setComp() {
    var want = root.hasAttribute("data-sdark") && comp && comp.compensate > 0;
    var key = want ? compMatrix(comp) : "";
    if (key === compKey) return;
    compKey = key;
    var svg = document.getElementById("sdark-comp-svg");
    if (!want) {
      root.style.removeProperty("filter");
      if (svg) svg.remove();
      return;
    }
    if (!svg) {
      svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.id = "sdark-comp-svg";
      svg.setAttribute("aria-hidden", "true");
      svg.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;pointer-events:none";
      svg.innerHTML = '<filter id="sdark-comp" color-interpolation-filters="linearRGB" '
        + 'x="0" y="0" width="100%" height="100%">'
        + '<feColorMatrix in="SourceGraphic" type="matrix" result="lift"/>'
        + '<feBlend in="SourceGraphic" in2="lift" mode="lighten"/></filter>';
      root.appendChild(svg);
    }
    svg.querySelector("feColorMatrix").setAttribute("values", key);
    root.style.setProperty("filter", "url(#sdark-comp)");
  }

  function checkComp() {
    if (!root.hasAttribute("data-sdark")) { setComp(); return; }
    if (compBusy) return;
    compBusy = true;
    fetch("/sdark_comp.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (j) comp = j; setComp(); })
      .catch(function () {})
      .then(function () { compBusy = false; });
  }

  function parse(text) {
    try { var f = JSON.parse(text); sup = !!f.super; return !!f.on; } catch (e) { sup = false; return false; }
  }

  try {
    var x = new XMLHttpRequest();
    x.open("GET", "/extra.json?t=" + Date.now(), false);
    x.send();
    if (x.status === 200) { on = parse(x.responseText); apply(); }
  } catch (e) { /* no server answer: stay on the system palette */ }

  var busy = false;
  function check() {
    if (busy) return;
    busy = true;
    fetch("/extra.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (t) { if (t !== null) { on = parse(t); apply(); } })
      .catch(function () {})
      .then(function () { busy = false; });
  }
  setInterval(check, 1000);
  window.addEventListener("focus", check);
  document.addEventListener("visibilitychange", check);
  dark.addEventListener("change", apply);
})();
