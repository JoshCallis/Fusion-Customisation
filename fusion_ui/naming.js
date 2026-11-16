/* naming.js — the situational variable engine, window side.
 *
 * A faithful mirror of naming.py. The add-ins and servers use the Python; the companion
 * windows use this. Both read the SAME variables.json, and naming_selftest.py checks
 * the two agree on every real parameter name — so if you change one, change the other
 * and run the test.
 *
 *     EdgeOffsetBackY  EdgeOffsetFrontY  EdgeOffsetX  EdgeOffsetZ
 *         └──────────── family "EdgeOffset", four members ────────┘
 *
 * You type the stem, the situation picks the member, Fusion receives the real name.
 *
 * Usage:
 *     await Naming.loadVocabulary("http://127.0.0.1:8732/variables.json");
 *     const a = Naming.analyse("EdgeOffsetBackY");     // {stem, tags, axis, end, hits}
 *     const fams = Naming.families(params.map(p => p.name));
 *     const hit  = Naming.resolve(fams["EdgeOffset"], {axis: "Y", end: "high"});
 */

const Naming = (function () {
  "use strict";

  /* CamelCase word split. The first alternative takes a run of capitals NOT followed by
     a lowercase letter, which keeps acronyms whole: "HIPSThickness" -> HIPS + Thickness,
     so neither can yield a bare axis letter. "SomethingXY" yields "XY", matching nothing. */
  const WORDS_RE = /[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+/g;

  let vocab = null;

  /* ── Vocabulary ─────────────────────────────────────────────────────────── */
  function setVocabulary(spec) {
    const byWord = {};
    const dimensions = (spec && spec.dimensions) || [];
    for (const dim of dimensions) {
      for (const val of dim.values || []) {
        if (!val.word) continue;
        byWord[val.word.toLowerCase()] = {
          dimension: dim.id || "",
          symmetricOnly: !!dim.symmetricOnly,
          word: val.word,
          axis: val.axis || "",
          end: val.end || ""
        };
      }
    }
    const relativeTypes = new Set(
      ((spec && spec.relativeTypes) || []).map(w => String(w).toLowerCase()));
    vocab = { spec, dimensions, byWord, relativeTypes };
    return vocab;
  }

  async function loadVocabulary(url) {
    const r = await fetch(url, { cache: "no-store" });
    return setVocabulary(await r.json());
  }

  function ready() { return vocab !== null; }

  /* ── Splitting ──────────────────────────────────────────────────────────── */
  function words(name) {
    const out = [];
    let m;
    WORDS_RE.lastIndex = 0;
    while ((m = WORDS_RE.exec(name || "")) !== null) out.push([m[0], m.index]);
    return out;
  }

  /* ── Analysis ───────────────────────────────────────────────────────────── */
  /* Split `name` into a stem plus its situational tags.
   *
   * VARIABLES ARE A TRAILING RUN. The convention is: what is being measured, then the
   * type of measurement, then the axis or direction — so variables only ever appear at
   * the END of a name, and the parse is to scan backwards collecting vocabulary words
   * until one is not:
   *
   *     BootCeilingBackOffsetFront
   *     └──── stem ──────┘└ tags ┘   Front is collected; the scan stops at Offset;
   *                                  Back is never reached, so it stays a descriptor
   *
   * That one rule is what makes the convention unambiguous with no separator and no list
   * of measurement types — the type word is whatever stops the scan. The contract is a
   * single sentence: NOTHING BUT VARIABLES COMES AFTER THE MEASUREMENT TYPE.
   *
   * A direction before the type is a descriptor, which is what tells DrawerBackGearOffsetZ
   * (the back gear's Z offset) from EdgeOffsetBackY (the back edge, in Y).
   */
  function analyse(name) {
    if (!vocab) throw new Error("Naming: vocabulary not loaded");
    const ws = words(name);

    let cut = ws.length;
    while (cut > 0 && Object.prototype.hasOwnProperty.call(
             vocab.byWord, ws[cut - 1][0].toLowerCase())) cut--;

    const conflicts = [];
    const stemWords = ws.slice(0, cut).map(p => p[0]);
    const tags = {};
    let hits = [];

    for (const [word, at] of ws.slice(cut)) {
      const entry = vocab.byWord[word.toLowerCase()];
      const dim = entry.dimension;
      if (Object.prototype.hasOwnProperty.call(tags, dim)) {
        conflicts.push("two " + dim + " words (" + tags[dim] + " and " + word +
                       "); the second is left in the stem");
        stemWords.push(word);
        continue;
      }
      tags[dim] = entry.word;
      hits.push([dim, entry.word, at]);
    }

    const axisFrom = (dim) =>
      tags[dim] ? vocab.byWord[tags[dim].toLowerCase()].axis : "";

    // An axis or span word names the measured axis; a direction only names it when
    // nothing else does. The two disagreeing means the trailing run says two things at
    // once — malformed under the convention — so the direction is demoted and reported.
    const measured = axisFrom("axis") || axisFrom("span");
    const directionAxis = axisFrom("direction");

    if (measured && directionAxis && directionAxis !== measured) {
      const word = tags["direction"];
      conflicts.push(word + " implies " + directionAxis + " but the measured axis is " +
                     measured + "; read as a descriptor");
      delete tags["direction"];
      hits = hits.filter(h => h[0] !== "direction");
      stemWords.push(word);
    }

    const axis = measured || directionAxis;
    const end = tags["direction"]
      ? vocab.byWord[tags["direction"].toLowerCase()].end : "";

    // The whole name being variables — "Depth", "Width", "BackY" — leaves an empty stem,
    // which would collide every such name into one family. The TAGS are still true and
    // are exactly what situational filtering needs, so only the stem falls back to the
    // name. Getting this wrong made bare Depth and Height match every situation.
    const stem = stemWords.join("") || name;

    // THE FRAME: what this value is measured FROM.
    //
    // The measurement type decides. An "offset" goes FROM the direction named beside it,
    // so ShelfOffsetBack sits 30mm in from the BACK bound. Any other type merely locates
    // something in world space — FrontPosition is the front's position measured, like
    // everything else, from the origin. An offset carrying only an axis is likewise
    // origin-framed: ShelfOffsetY is 300mm from the Y origin.
    // On a SYMMETRIC axis there is no single direction to name — Left and Right are the
    // same distance away — so the span word is what names that bound: the bound in that
    // direction IS the width/depth/height. BootCeilingOffsetWidth is measured in from
    // the side, and a span frame means "the nearer bound" rather than one named end.
    let frame = "origin";
    if (stemWords.length &&
        vocab.relativeTypes.has(stemWords[stemWords.length - 1].toLowerCase())) {
      if (tags["direction"]) frame = tags["direction"];
      else if (tags["span"]) frame = tags["span"];
    }

    return {
      name,
      stem, frame,
      tags, axis, end, hits, conflicts,
      isTagged: Object.keys(tags).length > 0,
      situationKey: axis + "|" + end
    };
  }

  /* ── Direction lookups ──────────────────────────────────────────────────── */
  /* The direction word naming one end of an axis: ("Y", "high") -> "Back". */
  function directionAt(axis, end) {
    const A = String(axis || "").toUpperCase(), E = String(end || "").toLowerCase();
    for (const k of Object.keys(vocab.byWord)) {
      const e = vocab.byWord[k];
      if (e.dimension === "direction" && e.axis === A && e.end === E) return e.word;
    }
    return "";
  }

  /* The direction facing the other way down the same axis: "Back" -> "Front".
     What a SPAN crosses — naming Depth from the front bound carries you to the back
     one, which is why the frame filter reverses when a span appears. */
  function oppositeDirection(word) {
    const e = vocab.byWord[String(word || "").toLowerCase()];
    if (!e || e.dimension !== "direction") return "";
    return directionAt(e.axis, e.end === "high" ? "low" : "high");
  }

  /* ── Families ───────────────────────────────────────────────────────────── */
  function families(names) {
    const out = {};
    for (const name of names) {
      const a = analyse(name);
      (out[a.stem] = out[a.stem] || []).push(a);
    }
    return out;
  }

  /* Members of one family that share a situation key — that no situation can tell
     apart. {key: [analysis, ...]} for the clashing keys only. */
  function ambiguous(members) {
    const byKey = {};
    for (const a of members) (byKey[a.situationKey] = byKey[a.situationKey] || []).push(a);
    const out = {};
    for (const k of Object.keys(byKey)) if (byKey[k].length > 1) out[k] = byKey[k];
    return out;
  }

  /* ── Resolution ─────────────────────────────────────────────────────────── */
  /* The member of a family that fits the situation, or null.
   *
   *   1. axis and end both match          EdgeOffsetBackY for (Y, high)
   *   2. axis matches, member has no end  EdgeOffsetX     for (X, anything)
   *   3. the only member, when the situation says nothing at all
   *
   * A member tagged with a span word is eligible only when that axis is symmetric on
   * the component — on an asymmetric axis those words leave the vocabulary entirely,
   * and offering one would name a midline that is not a modelling feature.
   */
  function resolve(members, situation) {
    const { axis = "", end = "", symmetric = false } = situation || {};
    if (!members || !members.length) return null;

    const eligible = (a) => {
      for (const dim of Object.keys(a.tags)) {
        const entry = vocab.byWord[a.tags[dim].toLowerCase()];
        if (entry && entry.symmetricOnly && !symmetric) return false;
      }
      return true;
    };

    const pool = members.filter(eligible);
    if (!pool.length) return null;

    if (axis) {
      if (end) {
        const exact = pool.filter(a => a.axis === axis && a.end === end);
        if (exact.length) return exact[0];
      }
      const loose = pool.filter(a => a.axis === axis && !a.end);
      if (loose.length) return loose[0];
      const sameAxis = pool.filter(a => a.axis === axis);
      if (sameAxis.length) return sameAxis[0];
      return null;
    }

    const untagged = pool.filter(a => !a.axis);
    if (untagged.length) return untagged[0];
    return pool.length === 1 ? pool[0] : null;
  }

  /* Convenience: the real parameter name for `stem` in this situation, or "". */
  function resolveName(names, stem, situation) {
    const fam = families(names)[stem];
    if (!fam) return "";
    const hit = resolve(fam, situation);
    return hit ? hit.name : "";
  }

  return {
    setVocabulary, loadVocabulary, ready,
    words, analyse, families, ambiguous, resolve, resolveName,
    directionAt, oppositeDirection
  };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Naming;
