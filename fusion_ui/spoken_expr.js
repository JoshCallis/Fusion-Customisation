/* spoken_expr.js — a dictated sentence turned into a Fusion expression.
 *
 * Wispr Flow pastes what was said ("half the depth plus ten", "Drawer width minus
 * 2 times wall thickness.") into the focused value field as one paste. This turns it
 * into an expression over the document's REAL parameter names:
 *
 *     "half the depth plus ten"              → Depth / 2 + 10
 *     "drawer width minus twice the steel thickness"
 *                                            → Drawer_..._Span_X - TopLevel_Materials_Steel_Thickness * 2
 *
 * Three steps:
 *   1. LEX — number words → digits ("one hundred and five", "two point five", "1,200"),
 *      operator words → + - * / ^ ( ), fractions ("half", "two thirds", "ten percent"),
 *      "twice"/"double", "squared"/"cubed", "square root of", units after a number
 *      ("ten millimetres" → 10 mm). Filler words (the, of, is, equals…) drop out.
 *   2. NAME — each run of the words left over is ONE parameter: scored against every
 *      candidate's READING (what the list/Browser shows — "Boot Ceiling - Dimension -
 *      Width") and its real name split into words, word by word, any order, with
 *      plurals, prefixes and one-letter slips forgiven. The session's own component (and
 *      the ones above it) win ties.
 *   3. EMIT — operands joined by their operators; a fraction or "twice" applies to the
 *      ONE operand after it ("half the width plus ten" is Width / 2 + 10). Two operands
 *      side by side are added, the chips' own rule ("Width Depth" is their sum).
 *
 * Pure: no DOM, no fetch. The page hands in candidates
 *     [{ name, reading, owner?, readingOnly? }]
 * and gets back
 *     { expr, refs: [real names], unmatched: [phrases], spoken: bool }
 * `spoken` false = the text was already an expression (only real names, digits,
 * symbols): leave the paste alone. Test: fusion_ui/spoken_expr_test.js.            */
const SpokenExpr = (function () {
  "use strict";

  const UNITS = {
    1: ["one"], 2: ["two"], 3: ["three"], 4: ["four"], 5: ["five"], 6: ["six"],
    7: ["seven"], 8: ["eight"], 9: ["nine"], 0: ["zero", "nought", "naught"]
  };
  const SMALL = {};
  for (const k in UNITS) for (const w of UNITS[k]) SMALL[w] = +k;
  const TEENS = { ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14,
                  fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19 };
  const TENS = { twenty: 20, thirty: 30, forty: 40, fourty: 40, fifty: 50, sixty: 60,
                 seventy: 70, eighty: 80, ninety: 90 };
  const FRACS = { half: 2, halves: 2, third: 3, thirds: 3, quarter: 4, quarters: 4,
                  fourth: 4, fourths: 4, fifth: 5, fifths: 5, sixth: 6, sixths: 6,
                  eighth: 8, eighths: 8, tenth: 10, tenths: 10 };
  // Multi-word operators first — the lexer takes the longest that matches.
  const OPS = [
    [["to", "the", "power", "of"], "^"], [["to", "the", "power"], "^"],
    [["multiplied", "by"], "*"], [["divided", "by"], "/"], [["divide", "by"], "/"],
    [["take", "away"], "-"], [["less"], "-"], [["minus"], "-"], [["subtract"], "-"],
    [["plus"], "+"], [["add"], "+"], [["added", "to"], "+"],
    [["times"], "*"], [["multiply", "by"], "*"], [["over"], "/"],
    [["power"], "^"],
    [["open", "bracket"], "("], [["open", "brackets"], "("], [["open", "paren"], "("],
    [["open", "parenthesis"], "("], [["left", "bracket"], "("], [["left", "paren"], "("],
    [["close", "bracket"], ")"], [["close", "brackets"], ")"], [["close", "paren"], ")"],
    [["close", "parenthesis"], ")"], [["right", "bracket"], ")"], [["right", "paren"], ")"],
    [["end", "bracket"], ")"]
  ];
  const UNIT_WORDS = {
    mm: "mm", millimetre: "mm", millimetres: "mm", millimeter: "mm", millimeters: "mm",
    mil: "mm", mils: "mm", mill: "mm", mills: "mm",
    cm: "cm", centimetre: "cm", centimetres: "cm", centimeter: "cm", centimeters: "cm",
    metre: "m", metres: "m", meter: "m", meters: "m",
    inch: "in", inches: "in", foot: "ft", feet: "ft",
    degree: "deg", degrees: "deg", deg: "deg", radian: "rad", radians: "rad"
  };
  const FILLER = new Set(["the", "of", "a", "an", "is", "equals", "equal", "to", "be",
    "it", "make", "set", "value", "um", "uh", "er", "erm", "please", "and", "by",
    "that", "this", "with", "its", "thing"]);
  const FUNCS = new Set(["sqrt", "abs", "sin", "cos", "tan", "asin", "acos", "atan",
    "floor", "ceil", "round", "max", "min", "log", "ln", "exp", "pow", "sign"]);
  const NUMBER_WORDS = new Set(Object.keys(SMALL).concat(Object.keys(TEENS),
    Object.keys(TENS), ["hundred", "thousand", "point"]));

  const squash = s => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

  function singular(w) {
    if (w.length > 4 && w.endsWith("ies")) return w.slice(0, -3) + "y";
    if (w.length > 4 && /(ss|x|ch|sh)es$/.test(w)) return w.slice(0, -2);
    if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss")) return w.slice(0, -1);
    return w;
  }

  /* ── Tokens ─────────────────────────────────────────────────────────────── */
  // Raw text → words, digit runs and symbols, lowercased. "10mm" splits; "1,200" joins;
  // "twenty-five" / "x-axis" lose the hyphen (a hyphen between letters is not a minus).
  function tokens(text) {
    let s = String(text || "")
      .replace(/[−–—]/g, "-").replace(/×/g, "*").replace(/÷/g, "/")
      .replace(/½/g, " 0.5 ").replace(/¼/g, " 0.25 ").replace(/¾/g, " 0.75 ")
      .replace(/[‘’']s\b/gi, "").replace(/[‘’']/g, "")
      .replace(/(\d),(\d{3})(?!\d)/g, "$1$2")
      .replace(/([A-Za-z])-(?=[A-Za-z])/g, "$1 ");
    const out = [];
    const re = /\d+(?:\.\d+)?|\.\d+|[A-Za-z_][A-Za-z0-9_]*|[-+*/^(),%]/g;
    let m;
    while ((m = re.exec(s))) out.push(m[0]);
    return out;
  }

  /* ── Numbers ────────────────────────────────────────────────────────────── */
  // A number at words[i] → { v, j } (j = first word after it) or null.
  function readNumber(w, i) {
    let j = i, total = 0, cur = 0, any = false, last = "";
    const isDigits = t => /^(\d+(\.\d+)?|\.\d+)$/.test(t || "");
    if (isDigits(w[j])) { cur = parseFloat(w[j]); j++; any = true; last = "digits"; }
    else {
      if (w[j] === "a" && (w[j + 1] === "hundred" || w[j + 1] === "thousand")) { cur = 1; j++; any = true; last = "unit"; }
      for (; j < w.length; j++) {
        const t = w[j];
        if (t in SMALL) {
          if (last === "unit" || last === "teen") break;
          cur += SMALL[t]; last = "unit";
        } else if (t in TEENS) {
          if (last === "unit" || last === "teen" || last === "tens") break;
          cur += TEENS[t]; last = "teen";
        } else if (t in TENS) {
          if (last && last !== "hundred" && last !== "thousand") break;
          cur += TENS[t]; last = "tens";
        } else if (t === "hundred") {
          if (!any) break;
          cur = (cur || 1) * 100; last = "hundred";
        } else if (t === "thousand") {
          if (!any) break;
          total += (cur || 1) * 1000; cur = 0; last = "thousand";
        } else if (t === "and" && (last === "hundred" || last === "thousand") &&
                   (w[j + 1] in SMALL || w[j + 1] in TEENS || w[j + 1] in TENS)) {
          continue;
        } else break;
        any = true;
      }
    }
    // "point five", "two point two five", "point 5"
    if (w[j] === "point") {
      let k = j + 1, frac = "";
      if (isDigits(w[k]) && !/\./.test(w[k])) { frac = w[k]; k++; }
      else while (k < w.length && (w[k] in SMALL)) { frac += SMALL[w[k]]; k++; }
      if (frac) { j = k; cur += parseFloat("0." + frac); any = true; }
    }
    if (!any) return null;
    let v = total + cur;
    // "one and a half", "two and a quarter"
    if (w[j] === "and" && w[j + 1] === "a" && (w[j + 2] === "half" || w[j + 2] === "quarter")) {
      v += w[j + 2] === "half" ? 0.5 : 0.25; j += 3;
    }
    return { v, j };
  }

  function fmt(v) {
    const r = Math.round(v * 1e6) / 1e6;
    return String(r);
  }

  /* ── Lexing ─────────────────────────────────────────────────────────────── */
  // Items: num {v, unit}, op {v}, lp, rp, comma, frac {n, d}, mul {v}, post {v},
  // fn {v}, word {v}. `exact` (squashed real name → name) lets a pasted real name or
  // function call pass through untouched.
  function lex(text, exact) {
    const w0 = tokens(text);
    const w = w0.map(t => t.toLowerCase());
    const items = [];
    let spokenHits = 0;
    for (let i = 0; i < w.length;) {
      const t = w[i];
      // A real name typed or pasted whole is that parameter, no questions asked — when it
      // could only have been written (WingWidth, Drawer_Floor_Thickness). A plain word
      // that happens to be a name ("Front", "Depth") is left to the phrase it sits in:
      // "nose right" is Nose's Right, not Nose + Right.
      if (/_|[a-z][A-Z]/.test(w0[i]) && exact.has(squash(w0[i]))) {
        items.push({ k: "name", v: exact.get(squash(w0[i])) }); i++; continue;
      }
      if ("+-*/^".includes(t)) { items.push({ k: "op", v: t }); i++; continue; }
      if (t === "(") { items.push({ k: "lp" }); i++; continue; }
      if (t === ")") { items.push({ k: "rp" }); i++; continue; }
      if (t === ",") { items.push({ k: "comma" }); i++; continue; }
      if (t === "%" || t === "percent" || (t === "per" && w[i + 1] === "cent")) {
        const prev = items[items.length - 1];
        if (prev && prev.k === "num") { items.pop(); items.push({ k: "frac", n: prev.v, d: 100 }); }
        i += t === "per" ? 2 : 1; spokenHits++; continue;
      }
      if (FUNCS.has(t) && w[i + 1] === "(") { items.push({ k: "fn", v: t, call: true }); i++; continue; }
      if ((t === "square" && w[i + 1] === "root") ) {
        items.push({ k: "fn", v: "sqrt" }); i += 2; spokenHits++;
        if (w[i] === "of") i++;
        continue;
      }
      if (t === "squared") { items.push({ k: "post", v: "^ 2" }); i++; spokenHits++; continue; }
      if (t === "cubed") { items.push({ k: "post", v: "^ 3" }); i++; spokenHits++; continue; }
      if (t === "twice" || t === "double") { items.push({ k: "mul", v: 2 }); i++; spokenHits++; continue; }
      if (t === "triple") { items.push({ k: "mul", v: 3 }); i++; spokenHits++; continue; }
      if (t === "negative") { items.push({ k: "op", v: "-" }); i++; spokenHits++; continue; }
      // Operator phrases, longest first.
      let op = null;
      for (const [ws, v] of OPS) {
        if (ws.every((x, k) => w[i + k] === x) && (!op || ws.length > op[0].length)) op = [ws, v];
      }
      if (op) {
        const v = op[1];
        items.push(v === "(" ? { k: "lp" } : v === ")" ? { k: "rp" } : { k: "op", v });
        i += op[0].length; spokenHits++; continue;
      }
      // Fractions: "half", "a third", "two thirds", "three quarters".
      if (t in FRACS) { items.push({ k: "frac", n: 1, d: FRACS[t] }); i++; spokenHits++; continue; }
      if (t === "a" && w[i + 1] in FRACS) { items.push({ k: "frac", n: 1, d: FRACS[w[i + 1]] }); i += 2; spokenHits++; continue; }
      const num = readNumber(w, i);
      if (num) {
        if (NUMBER_WORDS.has(t) || t === "a") spokenHits++;
        let j = num.j;
        if (w[j] in FRACS && !(num.v % 1)) {
          items.push({ k: "frac", n: num.v, d: FRACS[w[j]] }); i = j + 1; spokenHits++; continue;
        }
        const item = { k: "num", v: num.v };
        if (w[j] in UNIT_WORDS) { item.unit = UNIT_WORDS[w[j]]; j++; }
        else if (w[j] === "m" && j === w.length - 1) { item.unit = "m"; j++; }
        items.push(item); i = j; continue;
      }
      if (FILLER.has(t)) { i++; spokenHits++; continue; }
      items.push({ k: "word", v: t, raw: w0[i] }); i++;
    }
    return { items, spokenHits };
  }

  /* ── Matching a phrase to a parameter ───────────────────────────────────── */
  function lev(a, b, cap) {
    if (Math.abs(a.length - b.length) > cap) return cap + 1;
    let prev = Array.from({ length: b.length + 1 }, (_, k) => k);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i];
      let best = i;
      for (let j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
        if (cur[j] < best) best = cur[j];
      }
      if (best > cap) return cap + 1;
      prev = cur;
    }
    return prev[b.length];
  }
  function wordSim(q, c) {
    if (q === c) return 1;
    const qs = singular(q), cs = singular(c);
    if (qs === cs) return 0.95;
    if (q.length < 3 || c.length < 2) return 0;
    if (cs.startsWith(qs) && qs.length >= 3) return 0.6 + 0.3 * qs.length / cs.length;
    if (qs.startsWith(cs) && cs.length >= 3) return 0.5 + 0.3 * cs.length / qs.length;
    const cap = qs.length >= 7 ? 2 : qs.length >= 4 ? 1 : 0;
    if (cap && lev(qs, cs, cap) <= cap) return cap === 1 ? 0.7 : 0.55;
    return 0;
  }
  function splitWords(s) {
    return String(s || "")
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
      .toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  }
  function candWords(c) {
    const rd = splitWords(c.reading || "");
    const nm = c.readingOnly ? [] : splitWords(c.name);
    const all = Array.from(new Set(rd.concat(nm)));
    return { all, reading: rd.length ? rd : nm };
  }

  // Best candidate for the spoken words `qs`, or null.
  function bestFor(qs, cands, opts) {
    const owners = ((opts && opts.owners) || []).map(squash).filter(Boolean);
    let best = null, second = null;
    const qflat = qs.join("");
    for (const c of cands) {
      const cw = c._w || (c._w = candWords(c));
      let sum = 0, misses = 0, used = new Set(), order = 0, lastAt = -1;
      for (const q of qs) {
        let s = 0, at = -1;
        cw.all.forEach((x, k) => { const v = wordSim(q, x); if (v > s || (v === s && v > 0 && used.has(at) && !used.has(k))) { s = v; at = k; } });
        // Two spoken words for one written ("boot ceiling" vs "bootceiling") are
        // caught below by the flat comparison; here a miss is a miss.
        if (s < 0.5) { misses++; continue; }
        sum += s; used.add(at);
        if (at > lastAt) order++;
        lastAt = at;
      }
      let score;
      const flatR = cw.reading.join(""), flatN = squash(c.name);
      if (qflat === flatR || qflat === flatN) score = 3;
      else {
        // At most one stray word in three may be noise; never the only word.
        if (misses && (qs.length < 3 || misses * 3 > qs.length)) continue;
        score = sum / qs.length - misses * 0.25;
        // Specific beats vague: every word of the READING not said costs a little.
        const unsaid = cw.reading.filter(x => !qs.some(q => wordSim(q, x) >= 0.5)).length;
        score -= unsaid * 0.06;
        score += (order === qs.length - misses ? 0.03 : 0);
      }
      if (score < 0.5) continue;
      const ow = squash(c.owner || "");
      const oi = ow ? owners.indexOf(ow) : -1;
      if (oi >= 0) score += 0.12 - Math.min(oi, 3) * 0.03;
      const cand = { c, score };
      if (!best || score > best.score + 1e-9 ||
          (Math.abs(score - best.score) < 1e-9 && c.name.length < best.c.name.length)) {
        second = best; best = cand;
      } else if (!second || score > second.score) second = cand;
    }
    if (!best) return null;
    return { name: best.c.name, score: best.score,
             ambiguous: !!second && best.score - second.score < 0.02 };
  }

  /* ── Assemble ───────────────────────────────────────────────────────────── */
  function convert(text, cands, opts) {
    cands = cands || [];
    const exact = new Map();
    for (const c of cands) exact.set(squash(c.name), c.name);
    const { items, spokenHits } = lex(text, exact);

    // Words in a row are one parameter.
    const seq = [], unmatched = [], refs = [], ambiguous = [];
    let wordsLeft = 0;
    for (let i = 0; i < items.length;) {
      if (items[i].k !== "word") { seq.push(items[i]); i++; continue; }
      const run = [];
      while (i < items.length && items[i].k === "word") run.push(items[i++]);
      const hit = bestFor(run.map(x => x.v), cands, opts);
      // A run that spells a real name exactly ("Depth") is not, by itself, speech.
      if (!hit || run.length > 1 || squash(run[0].raw) !== squash(hit.name)) wordsLeft += run.length;
      if (hit) {
        seq.push({ k: "name", v: hit.name, plural: run[run.length - 1].v !== singular(run[run.length - 1].v) });
        if (hit.ambiguous) ambiguous.push(hit.name);
      } else {
        const raw = run.map(x => x.raw).join(" ");
        unmatched.push(raw);
        seq.push({ k: "raw", v: run.map(x => x.raw.charAt(0).toUpperCase() + x.raw.slice(1)).join("") });
      }
    }
    for (const x of seq) if (x.k === "name" && !refs.includes(x.v)) refs.push(x.v);

    let i = 0;
    const isStart = x => x && (x.k === "num" || x.k === "name" || x.k === "raw" || x.k === "lp" ||
                               x.k === "frac" || x.k === "mul" || x.k === "fn");
    function primary() {
      const x = seq[i];
      if (!x) return null;
      if (x.k === "num") { i++; return { t: fmt(x.v) + (x.unit ? " " + x.unit : ""), num: true }; }
      if (x.k === "name" || x.k === "raw") { i++; return { t: x.v, name: true, plural: x.plural }; }
      if (x.k === "lp") {
        i++;
        const inner = expr(true);
        if (seq[i] && seq[i].k === "rp") i++;
        return { t: "(" + inner + ")" };
      }
      if (x.k === "fn") {
        i++;
        if (x.call) {           // sqrt( … ) written out: arguments up to the bracket
          if (seq[i] && seq[i].k === "lp") i++;
          const args = [expr(true)];
          while (seq[i] && seq[i].k === "comma") { i++; args.push(expr(true)); }
          if (seq[i] && seq[i].k === "rp") i++;
          return { t: x.v + "(" + args.join(", ") + ")" };
        }
        const p = operand();
        return { t: x.v + "(" + (p ? p.t : "") + ")" };
      }
      if (x.k === "frac") {
        i++;
        const p = isStart(seq[i]) ? operand() : null;
        if (!p) return { t: fmt(x.n / x.d), num: true };
        if (p.num && !/[a-z]/.test(p.t)) return { t: fmt(parseFloat(p.t) * x.n / x.d), num: true };
        const t = wrap(p.t);
        return { t: x.n === 1 ? t + " / " + fmt(x.d) : t + " * " + fmt(x.n) + " / " + fmt(x.d) };
      }
      if (x.k === "mul") {
        i++;
        const p = isStart(seq[i]) ? operand() : null;
        if (!p) return { t: fmt(x.v), num: true };
        return { t: wrap(p.t) + " * " + fmt(x.v) };
      }
      return null;
    }
    // A compound operand inside a fraction keeps its own grouping.
    const wrap = t => /[-+]/.test(t.replace(/^\(.*\)$/, "").replace(/^-/, "")) && !/^\(.*\)$/.test(t) ? "(" + t + ")" : t;
    function operand() {
      const p = primary();
      if (!p) return null;
      while (seq[i] && seq[i].k === "post") { p.t = wrap(p.t) + " " + seq[i].v; p.num = false; i++; }
      return p;
    }
    function expr(inner) {
      const parts = [];
      let prev = null, wantOp = false;
      while (i < seq.length) {
        const x = seq[i];
        if (x.k === "rp" || x.k === "comma") { if (inner) break; i++; continue; }
        if (x.k === "op") {
          if (wantOp) { parts.push(x.v); wantOp = false; }
          else if (x.v === "-") parts.push("-");          // unary minus
          else if (parts.length && "+-*/^".includes(parts[parts.length - 1])) parts[parts.length - 1] = x.v;
          i++; continue;
        }
        if (x.k === "post") { i++; continue; }            // nothing to square
        if (!isStart(x)) { i++; continue; }
        const p = operand();
        if (!p) { i++; continue; }
        if (wantOp) parts.push(prev && prev.num && p.name && p.plural ? "*" : "+");
        if (parts.length && parts[parts.length - 1] === "-" && (parts.length === 1 || "+-*/^(".includes(parts[parts.length - 2]))) {
          parts[parts.length - 1] = "-" + p.t;
        } else parts.push(p.t);
        prev = p; wantOp = true;
      }
      while (parts.length && "+-*/^".includes(parts[parts.length - 1])) parts.pop();
      return parts.join(" ");
    }
    const out = expr(false);
    const spoken = spokenHits > 0 || wordsLeft > 0;
    return { expr: out, refs, unmatched, ambiguous, spoken };
  }

  return { convert, tokens, readNumber: (s) => { const w = tokens(s).map(t => t.toLowerCase()); const r = readNumber(w, 0); return r && r.j === w.length ? r.v : null; }, bestFor, singular };
})();

if (typeof module !== "undefined" && module.exports) module.exports = SpokenExpr;
