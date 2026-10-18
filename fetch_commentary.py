"""Discover and fetch *every* classical Rambam meforshim on Sefaria.

For each halachic hilchot we compute, per commentator, the total Hebrew word
count of its commentary.  The aggregate "commentary density" (basket words /
Rambam words) is used by generate_schedule.py as an *objective* difficulty
proxy, blended with the hand-tuned weights in data/weights.csv.

Why discovery-driven?  The first version of this script worked from a
hand-written list of twelve meforshim and documented "Maggid Mishneh and Radbaz
are absent entirely" from Sefaria.  That was wrong -- they are absent only under
the naming convention the hand list guessed.  Asking Sefaria's links API which
commentaries actually point at each hilchot turns up **48 distinct
commentators**, including Maggid Mishneh (26 hilchot), Hasagot HaRa'avad (63),
Mishneh LaMelech (67) and the Radbaz (as "Commentary of Mahari Kurkus and
Radbaz", 3).  So instead of guessing titles we enumerate them, which is both
more complete and self-updating.

Two sources were evaluated for the meforshim Sefaria lacks:
  * HebrewBooks has the best *catalogue* but serves commentary through a JS
    overlay -- `rambam.aspx?...&text=1` answers plain HTTP with "Invalid
    Request" -- so it is not scrapable.
  * he.wikisource.org publishes clean wikitext (see ws_discover.py), but only
    partial transcriptions: 8 commentators, and for the two substantial ones
    Sefaria's text is strictly larger on every hilchot we compared (Maggid
    Mishneh, Shabbat: 79,980 words on Sefaria vs 13,983 on Wikisource).
Sefaria therefore wins outright and is the only source used here.

Selection: a commentator is kept if it is not a modern pedagogic work
(EXCLUDE) and is digitised on at least MIN_AUTHOR_HILCHOT hilchot; below that a
single-author quirk could move a hilchot's score on its own.

Data-quality guards (Sefaria is inconsistent here):
  * The Introduction (Hakdamah) texts are not halachot and have no meforshim;
    requesting them fuzzy-resolves to a *different* real hilchot and returns
    garbage, so we skip them and the hand weights handle them.
  * Some "commentaries" are books of drashot, not halacha-by-halacha notes; we
    require a Halakhah-based section structure.
  * We only accept a fetch whose returned `ref` exactly matches the requested
    node (otherwise Sefaria silently returned a whole-book / wrong node).

Writes data/commentary.json (cached + resumable):

{
  "fetched_at": "...",
  "basket": ["Kessef Mishneh", ...],          # commentators actually used
  "discovery": {"<slug>": {"<author>": "<exact node title>"}},
  "hilchot": {
     "<slug>": {
        "title_en": "...", "sefer": "...", "rambam_words": 7616,
        "meforshim": {"Kessef Mishneh": {"words": 29400, "present": true}},
        "basket_words": 123456, "present": 21, "attempted": 22,
        "coverage": 0.95, "density": 16.2, "commentators": 21
     }
  }
}

Usage:
    python fetch_commentary.py                 # full run (resumes from cache)
    python fetch_commentary.py --limit 8       # first 8 hilchot (smoke test)
    python fetch_commentary.py --report        # summarise the cache, no fetching
"""
import json
import os
import sys
import io
import time
import re
import urllib.parse
import urllib.request
import urllib.error
import argparse
import collections

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
RAMBAM = os.path.join(DATA_DIR, "rambam.json")
OUT_FILE = os.path.join(DATA_DIR, "commentary.json")
TEXTS_URL = "https://www.sefaria.org/api/v3/texts/"
LINKS_URL = "https://www.sefaria.org/api/links/"
UA = {"User-Agent": "rambam-schedule-builder/1.0 (personal study schedule tool)"}

# Modern pedagogic works: full or near-full coverage and written to explain
# rather than to resolve disputes, so their volume tracks exposition, not
# difficulty.  Everything else Sefaria links is treated as a classical meforshim.
EXCLUDE = {
    "Steinsaltz",
    "Yitzchak Yeranen",
    "Reshimot Shiurim",
}

# A commentator digitised on fewer hilchot than this is dropped: with so few
# observations its own normaliser is unreliable and it can dominate a hilchot.
MIN_AUTHOR_HILCHOT = 3


def http_json(url, timeout=120, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 403, 404):
                raise  # deterministic -> no point retrying
            last = e
            time.sleep(2.0 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def count_words(s):
    if not isinstance(s, str):
        return 0
    return len([t for t in s.split() if t.strip("\u05be\u2013-")])


def walk_words(o):
    if isinstance(o, str):
        return count_words(o)
    if isinstance(o, list):
        return sum(walk_words(x) for x in o)
    return 0


def norm(s):
    # collapse whitespace AND lowercase: Sefaria's meforshim-node titles
    # sometimes differ from the TOC title only by case (e.g. "within
    # Their Jurisdiction" vs "within their Jurisdiction").
    return " ".join((s or "").split()).lower()


def best_version_words(d):
    """Word count of the most complete Hebrew version of a text node."""
    best = 0
    for v in sorted(d.get("versions") or [],
                    key=lambda v: (not v.get("isPrimary"), v.get("language") != "he")):
        t = v.get("text")
        if isinstance(t, list) and t:
            jagged = t if isinstance(t[0], list) else [t]
            best = max(best, walk_words(jagged))
    return best


def extract_valid(d, requested_title):
    """Return the word count only if the node is genuinely scoped to the
    requested hilchot and is a halacha-structured commentary.  Else 0."""
    if d is None or "error" in d:
        return 0
    ref = d.get("ref") or d.get("title") or ""
    # Sefaria sometimes fuzzy-resolves a missing per-hilchot title to a
    # different (usually the first) real hilchot.  Reject anything that does
    # not exactly match what we asked for.
    if norm(ref) != norm(requested_title):
        return 0
    names = [(n or "").lower() for n in (d.get("sectionNames") or [])]
    if not any(("halakh" in n or "halach" in n) for n in names):
        return 0  # drashot / essay collections are not per-halacha -> unusable
    cats = d.get("categories") or []
    # Require BOTH tags: the Rambam's own text has "Mishneh Torah" but not
    # "Commentary", while a Talmud commentary (Korban Netanel on Eruvin) has
    # "Commentary" but not "Mishneh Torah".  Hilchot names collide with
    # tractate names (Hilchot Eruvin / Masechet Eruvin), so this is the guard
    # that keeps other works' commentaries out of the basket.
    if "Mishneh Torah" not in cats or "Commentary" not in cats:
        return 0
    return best_version_words(d)


def discover(slug_title, links_cache=None):
    """{author: exact_node_title} for one hilchot, from Sefaria's links API.

    The links API is case-insensitive and returns the *exact* node titles, which
    is what the v3 texts API needs; it also reveals commentators we would never
    have thought to ask for.  We only accept nodes whose title is a commentary
    *on this hilchot* (i.e. ends with the hilchot name), not cross-references.

    Returns None if the lookup failed (worth retrying) and {} if it succeeded
    but nobody commented on this hilchot -- the distinction matters, because the
    second case is a real result (e.g. the Order of Prayer is liturgy, not
    halachot, and has no meforshim at all).

    Two naming conventions exist and BOTH must be accepted:
      A) "<Name> on Mishneh Torah, <Hilchot>"   (most commentators)
      B) "<Name> on <Hilchot>"                  (e.g. Maaseh Rokeach)
    Matching only (A) silently drops (B) -- Maaseh Rokeach, digitised on 68
    hilchot, would vanish from the basket entirely.
    """
    title_en = slug_title
    bare = title_en
    if bare.startswith("Mishneh Torah, "):
        bare = bare[len("Mishneh Torah, "):]
    ref = "Mishneh Torah, " + bare + ".1"
    try:
        d = http_json(LINKS_URL + urllib.parse.quote(ref), timeout=150)
    except Exception:
        return None
    links = d if isinstance(d, list) else (d.get("links") or [])
    suffixes = (" " + title_en.lower(), " " + bare.lower())
    found = {}
    for l in links:
        if not isinstance(l, dict):
            continue
        it = l.get("index_title") or ""
        itl = it.lower()
        # Only accept nodes that are commentaries *on this hilchot* (the title
        # must follow the "on ..."); reject cross-references to other hilchot.
        if not any(itl.endswith(sfx) for sfx in suffixes):
            continue
        # The bare convention also matches commentaries on same-named Talmud
        # tractates ("Tosafot Yom Tov on Mishnah Eruvin", "Korban Netanel on
        # Eruvin") and the Rambam's own text.  Sefaria tags genuine Mishneh
        # Torah commentaries as category=Commentary/type=commentary; the odd one
        # out (Even Ha'azel, tagged "Quoting Commentary") still carries
        # "Mishneh Torah" in its title.  extract_valid re-checks via categories.
        if not ((l.get("category") == "Commentary" and l.get("type") == "commentary")
                or "mishneh torah" in itl):
            continue
        m = re.match(r"^(.+?) on ", it)
        if not m:
            continue
        author = m.group(1)
        if author in EXCLUDE or author.startswith("Mishneh Torah"):
            continue  # modern work, or the base text itself
        # keep the longest (most specific) node title per author
        if author not in found or len(it) > len(found[author]):
            found[author] = it
    return found


def load_hilchot():
    ram = json.load(open(RAMBAM, encoding="utf-8"))
    out = []
    for s in ram["sefarim"]:
        sefer = s["name_en"]
        if sefer == "Introduction":
            continue  # not halachot; hand weights handle them
        for h in s["hilchot"]:
            ram_words = sum(sum(p) for p in h["halacha_words"])
            out.append({
                "slug": h["slug"],
                "title_en": h["title_en"],
                "sefer": sefer,
                "n_perakim": len(h["perek_halachot"]),
                "rambam_words": ram_words,
            })
    return out


def recompute(rec, rambam_words):
    mef = rec.get("meforshim") or {}
    present_n = sum(1 for m in mef.values() if m.get("present"))
    attempted_n = len(mef)
    basket_words = sum(m.get("words", 0) for m in mef.values() if m.get("present"))
    rec["basket_words"] = basket_words
    rec["present"] = present_n
    rec["attempted"] = attempted_n
    rec["coverage"] = (present_n / attempted_n) if attempted_n else 0.0
    rec["density"] = (basket_words / rambam_words) if rambam_words else 0.0
    # breadth signal: how many distinct commentators weighed in
    rec["commentators"] = present_n
    return rec


def save(out, basket, disc):
    json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "basket": basket, "hilchot": out, "discovery": disc},
              open(OUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def report(cache):
    H = cache.get("hilchot") or {}
    if not H:
        print("cache is empty")
        return
    pres = collections.Counter()
    for rec in H.values():
        for m, v in (rec.get("meforshim") or {}).items():
            if v.get("present"):
                pres[m] += 1
    print(f"{len(H)} hilchot, {len(pres)} commentators in basket "
          f"(fetched {cache.get('fetched_at')})")
    tot_b = sum(r.get("basket_words", 0) for r in H.values())
    tot_r = sum(r.get("rambam_words", 0) for r in H.values())
    print(f"basket words={tot_b:,}  rambam words={tot_r:,}  "
          f"overall density={tot_b / tot_r:.2f}" if tot_r else "")
    print("\ncommentator coverage:")
    for m, c in pres.most_common():
        print(f"  {c:3d}/{len(H)}  {m}")
    print("\ndensity extremes:")
    rows = sorted(H.items(), key=lambda kv: -kv[1].get("density", 0))
    for s, r in rows[:8]:
        print(f"  {r.get('density', 0):6.2f}  {s}")
    print("  ...")
    for s, r in rows[-8:]:
        print(f"  {r.get('density', 0):6.2f}  {s}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only first N hilchot (smoke test)")
    ap.add_argument("--slugs", nargs="*", default=None, help="only these hilchot slugs")
    ap.add_argument("--min-author-hilchot", type=int, default=MIN_AUTHOR_HILCHOT,
                    help=f"drop commentators digitised on fewer hilchot "
                         f"(default {MIN_AUTHOR_HILCHOT})")
    ap.add_argument("--rediscover", action="store_true",
                    help="re-run links-API discovery even where cached")
    ap.add_argument("--force", action="store_true", help="re-fetch nodes already cached")
    ap.add_argument("--report", action="store_true", help="summarise cache and exit")
    args = ap.parse_args()

    if args.report:
        report(json.load(open(OUT_FILE, encoding="utf-8")) if os.path.exists(OUT_FILE) else {})
        return

    hilchot = load_hilchot()
    filtered = bool(args.limit or args.slugs)
    if args.limit:
        hilchot = hilchot[:args.limit]
    if args.slugs:
        want = set(args.slugs)
        hilchot = [h for h in hilchot if h["slug"] in want]
        print(f"Filtering to {len(hilchot)} requested slug(s)")
    visited = {h["slug"] for h in hilchot}

    # Always keep the whole cache as the base: a filtered run must not blow away
    # the hilchot it did not visit.
    cache = {}
    if os.path.exists(OUT_FILE):
        try:
            cache = json.load(open(OUT_FILE, encoding="utf-8"))
        except Exception:
            cache = {}
    out_hilchot = dict(cache.get("hilchot") or {})
    disc = dict(cache.get("discovery") or {})

    # --- Stage 1: discovery -------------------------------------------------
    print("Stage 1: discovering commentators per hilchot ...")
    n_new = 0
    for i, h in enumerate(hilchot):
        slug = h["slug"]
        if slug in disc and not args.rediscover:
            continue
        found = discover(h["title_en"])
        if found is None:
            print(f"  ! discovery failed for {slug} (will retry next run)")
            continue
        disc[slug] = found
        n_new += 1
        if n_new % 10 == 0:
            save(out_hilchot, cache.get("basket") or [], disc)  # checkpoint: ~84 requests
        if not found:
            print(f"  [{i + 1}/{len(hilchot)}] {slug}: no commentators "
                  f"(proxy will fall back to the hand weight)", flush=True)
        else:
            print(f"  [{i + 1}/{len(hilchot)}] {slug}: {len(found)} commentators", flush=True)
        time.sleep(0.3)
    if n_new:
        save(out_hilchot, cache.get("basket") or [], disc)
    print(f"  discovery: {n_new} hilchot refreshed, {len(disc)} cached")

    # --- Stage 2: pick the basket -------------------------------------------
    # Scope matters: on a filtered run we must size the basket from the hilchot
    # we actually know about, and never prune commentators belonging to hilchot
    # we did not visit.
    author_cov = collections.Counter()
    for slug in (disc.keys() if not filtered else visited & disc.keys()):
        author_cov.update(disc[slug].keys())
    basket = sorted(a for a, c in author_cov.items() if c >= args.min_author_hilchot)
    print(f"Stage 2: basket = {len(basket)} commentators "
          f"(>= {args.min_author_hilchot} hilchot each)")
    dropped = sorted(a for a in author_cov if a not in basket)
    if dropped:
        print(f"  dropped (too sparse): {', '.join(dropped)}")

    # Commentators already cached for visited hilchot stay in the basket on a
    # filtered run, so a smoke test cannot silently delete real data.
    if filtered:
        for slug in visited:
            for author in (out_hilchot.get(slug, {}).get("meforshim") or {}):
                if author not in basket:
                    basket.append(author)
        basket = sorted(basket)

    # --- Stage 3: fetch each (hilchot, commentator) node ---------------------
    print("Stage 3: fetching commentary text ...")
    todo = []
    for h in hilchot:
        slug = h["slug"]
        rec = out_hilchot.get(slug) or {}
        mef = dict(rec.get("meforshim") or {})
        for author in basket:
            node = (disc.get(slug) or {}).get(author)
            if not node:
                continue  # commentator did not write on this hilchot
            if mef.get(author, {}).get("present") and not args.force:
                continue
            todo.append((slug, h, author, node))
    print(f"  {len(todo)} node fetches to do")

    done = 0
    for slug, h, author, node in todo:
        rec = out_hilchot.setdefault(slug, {
            "title_en": h["title_en"], "sefer": h["sefer"],
            "rambam_words": h["rambam_words"], "meforshim": {},
        })
        rec["title_en"] = h["title_en"]
        rec["sefer"] = h["sefer"]
        rec["rambam_words"] = h["rambam_words"]
        mef = rec.setdefault("meforshim", {})
        words = 0
        try:
            d = http_json(TEXTS_URL + urllib.parse.quote(node))
            words = extract_valid(d, node)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {slug} / {author}: {e}", flush=True)
        mef[author] = {"words": words, "present": words > 0, "node": node}
        done += 1
        if done % 25 == 0 or done == len(todo):
            for s2, r2 in out_hilchot.items():
                rw = next((h2["rambam_words"] for h2 in hilchot if h2["slug"] == s2),
                          r2.get("rambam_words", 0))
                recompute(r2, rw)
            save(out_hilchot, basket, disc)
            print(f"  [{done}/{len(todo)}] saved ({slug} / {author} = {words}w)", flush=True)
        time.sleep(0.25)

    # On a filtered run we must not prune commentators belonging to hilchot we
    # did not visit, nor judge the basket from a partial sample; on a full run we
    # prune strictly to the discovered basket so stale/smoke-test entries go.
    if not filtered:
        for slug, rec in out_hilchot.items():
            mef = rec.get("meforshim") or {}
            for author in [a for a in mef if a not in basket]:
                del mef[author]
    for slug, rec in out_hilchot.items():
        rw = next((h["rambam_words"] for h in hilchot if h["slug"] == slug),
                  rec.get("rambam_words", 0))
        recompute(rec, rw)

    # Every hilchot gets a record, even one with no commentators, so the
    # fallback to hand weights is explicit rather than an absent key.
    for h in hilchot:
        if h["slug"] in out_hilchot:
            continue
        out_hilchot[h["slug"]] = recompute({
            "title_en": h["title_en"], "sefer": h["sefer"],
            "rambam_words": h["rambam_words"], "meforshim": {},
        }, h["rambam_words"])
        print(f"  + {h['slug']}: recorded with no commentators")

    save(out_hilchot, basket, disc)
    print(f"\nWrote {OUT_FILE} ({len(out_hilchot)} hilchot, {len(basket)} commentators)")
    report({"hilchot": out_hilchot, "fetched_at": "just now"})


if __name__ == "__main__":
    main()
