"""Plan (and write) the break points inside oversized single halachot.

Some Rambam halachot are so long that giving one of them a day wrecks the
effort distribution -- the worst offender is Tefillin 8:4, the list of every
parsha break in the Torah. The texts come from Sefaria and the break points
are derived from the text itself:

  * "headings" -- where the halacha carries its own <b> section headings
    (Tefillin 8:4: one part per Chumash, plus the shirot layout)
  * "list"     -- where it is an enumerated list, split into even parts at
    item boundaries (Sanhedrin 19:4: items 1-49, 50-84, 85-127, 128-168)

Usage: python plan_splits.py            # writes data/halacha_splits.json
       python plan_splits.py --scan     # just list the heaviest halachot

Add a case by copying one of the two calls at the bottom; part word counts are
checked against data/rambam.json and the script refuses to write nonsense.
"""
import csv
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
RAMBAM = os.path.join(DATA_DIR, "rambam.json")
WEIGHTS = os.path.join(DATA_DIR, "weights.csv")
OUT_FILE = os.path.join(DATA_DIR, "halacha_splits.json")
CACHE = os.path.join(DATA_DIR, "_sefaria_text_cache.json")
TEXTS_URL = "https://www.sefaria.org/api/v3/texts/"
UA = {"User-Agent": "rambam-schedule-builder/1.0 (personal study schedule tool)"}

# same tokenizer fetch_data.py used, so the parts add up to its counts
COUNT = lambda s: len([t for t in s.split() if t.strip("\u05be\u2013-")])


def http_json(url, timeout=90, retries=3):
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (a + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


_cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def load_perakim(slug):
    """[[halacha text, ...], ...] for one hilchot, primary Hebrew version."""
    url = TEXTS_URL + urllib.parse.quote(slug, safe="")
    if url not in _cache:
        print("  fetching", slug, flush=True)
        _cache[url] = http_json(url)
        json.dump(_cache, io.open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    for v in sorted(_cache[url].get("versions") or [],
                    key=lambda v: (not v.get("isPrimary"), v.get("language") != "he")):
        t = v.get("text")
        if isinstance(t, list) and t and isinstance(t[0], list):
            return t
    raise SystemExit("no jagged Hebrew text for " + slug)


def stored_words(slug, perek, halakha):
    data = json.load(io.open(RAMBAM, encoding="utf-8"))
    for s in data["sefarim"]:
        for h in s["hilchot"]:
            if h["slug"] == slug:
                return h["halacha_words"][perek - 1][halakha - 1]
    raise SystemExit("unknown slug " + slug)


def scan(top=12):
    weights = {}
    with io.open(WEIGHTS, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                weights[row["slug"]] = float(row["weight"])
            except (ValueError, KeyError):
                pass
    data = json.load(io.open(RAMBAM, encoding="utf-8"))
    rows = []
    for s in data["sefarim"]:
        for h in s["hilchot"]:
            w = weights.get(h["slug"], 1.0)
            for pi, per in enumerate(h["halacha_words"], 1):
                for hi, n in enumerate(per, 1):
                    rows.append((n * w, n, w, h["slug"], pi, hi, len(per)))
    rows.sort(reverse=True)
    print(f"{'effort':>7} {'words':>6} {'wt':>4}  slug:perek:halakha")
    for eff, n, w, slug, p, i, _k in rows[:top]:
        print(f"{eff:7.0f} {n:6} {w:4}  {slug.replace('Mishneh_Torah,_', '')}"
              f" {p}:{i}")


PLAN = {}


def record(slug, perek, halakha, strategy, source, parts):
    total = sum(p["words"] for p in parts)
    expect = stored_words(slug, perek, halakha)
    if total != expect:
        raise SystemExit(f"ERROR: {slug} {perek}:{halakha} parts add up to "
                         f"{total} words, expected {expect}")
    print(f"  {slug.replace('Mishneh_Torah,_', '')} {perek}:{halakha} -> "
          f"{len(parts)} parts ({sum(p['words'] for p in parts)} words, OK)")
    for p in parts:
        print(f"      {p['words']:5}w  {p['label']}")
    PLAN.setdefault(slug, {}).setdefault(str(perek), {})[str(halakha)] = {
        "strategy": strategy, "source": source, "parts": parts}


def from_headings(slug, perek, halakha, groups, labels):
    """Split at the text's own <b> headings; `groups` lists <br>-block indices."""
    blocks = load_perakim(slug)[perek - 1][halakha - 1].split("<br>")
    parts = []
    for g in groups:
        raw = "<br>".join(blocks[i] for i in g)
        parts.append({"label": labels[len(parts)], "words": COUNT(raw)})
    # cutting inside a fused "…:<br><b>…" token splits it in two, so every group
    # boundary adds one phantom word -- hand it back to keep the sum exact
    extra = len(parts) - 1
    if sum(p["words"] for p in parts) - stored_words(slug, perek, halakha) == extra:
        for p in parts[1:]:
            p["words"] -= 1
    record(slug, perek, halakha, "headings",
           "Sefaria <b> headings inside the halakha", parts)


def from_list(slug, perek, halakha, n_parts):
    """Split an enumerated list into even parts at item boundaries."""
    toks = load_perakim(slug)[perek - 1][halakha - 1].split()
    marks = [i for i, t in enumerate(toks)
             if re.fullmatch(r"[\u05d0-\u05ea\"'()]{1,8}\)", t)]
    total = COUNT(" ".join(toks))
    bounds = [0] + [min(marks, key=lambda i: abs(i - round(total * k / n_parts)))
                    for k in range(1, n_parts)] + [len(toks)]
    parts = []
    for a, z in zip(bounds, bounds[1:]):
        first = marks.index(a) + 1 if a in marks else 1
        prev = [i for i in marks if i < z]
        last = marks.index(prev[-1]) + 1 if prev else 1
        parts.append({"label": f"items {first}\u2013{last}" if last > first
                               else f"item {first}",
                      "words": COUNT(" ".join(toks[a:z]))})
    record(slug, perek, halakha, "list",
           f"enumerated list ({len(marks)} items) split into {n_parts} even "
           f"parts at item boundaries", parts)


TEFILLIN = "Mishneh_Torah,_Tefillin,_Mezuzah_and_the_Torah_Scroll"
SANHEDRIN = ("Mishneh_Torah,_The_Sanhedrin_and_the_Penalties_within_Their_"
             "Jurisdiction")


def main():
    args = sys.argv[1:]
    if "--scan" in args:
        scan()
        return
    print("== heaviest halachot (effort = words x weight) ==")
    scan(5)
    print("\n== deriving break points ==")
    from_headings(TEFILLIN, 8, 4,
                  [[0, 1], [2], [3], [4], [5], [6], [7, 8, 9]],
                  ["intro + Sefer Bereishit", "Sefer Shemot", "Sefer Vayikra",
                   "Sefer Bamidbar", "Sefer Devarim", "layout of Shirat Ha'azinu",
                   "Shirat HaYam, Shirat 'Az Yashir' layout + closing rules"])
    from_list(SANHEDRIN, 19, 4, 4)
    out = {"_comment": "Break points inside oversized single halachot, derived "
                       "from the Sefaria text by plan_splits.py. The words of "
                       "each halakha's parts must add up to its count in "
                       "data/rambam.json.",
           "hilchot": PLAN}
    json.dump(out, io.open(OUT_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\nwrote {OUT_FILE}")


if __name__ == "__main__":
    main()
