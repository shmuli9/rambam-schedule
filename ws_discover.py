"""Discover which Rambam meforshim exist on Hebrew Wikisource, and with what
structure/coverage.

HebrewBooks has the best catalogue but serves commentary through a JS overlay
(`rambam.aspx?...&text=1` returns "Invalid Request" to plain HTTP), so it is not
scrapable.  Wikisource publishes the same seforim as clean wikitext, and its
Rambam portal ("משנה תורה לרמב"ם") links eight of them:

    כסף משנה, מגיד משנה, לחם משנה, משנה למלך,
    אור שמח על רמב"ם, הגהות מיימוניות,
    השגות הראב"ד על רמב"ם, חידושי רבינו חיים הלוי

Two structural facts matter for scraping, and this script measures both:
  * page granularity differs per meforshim -- כסף משנה is one page per *hilchot*
    ("כסף משנה/הלכות אבל"), מגיד משנה is one page per *perek*
    ("מגיד משנה/הלכות שבת/פרק א");
  * hilchot names are spelled inconsistently between Wikisource and Sefaria
    (מלוה ולוה vs מלווה ולווה, ביאת מקדש vs ביאת המקדש, זכיה vs זכייה), so we
    cannot just build a title from the Sefaria name -- we enumerate the pages
    that actually exist and fuzzy-match them.

Writes data/ws_pages.json:

{ "<hub>": { "pages": {"<title>": {"len": n}}, "groups": {"<hilchot>": [titles]} } }

Usage:
    python ws_discover.py            # enumerate + measure (resumable, cached)
    python ws_discover.py --report   # just re-report from the cache
"""
import json
import os
import sys
import io
import time
import re
import collections
import urllib.parse
import urllib.request
import urllib.error
import argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
OUT = os.path.join(DATA_DIR, "ws_pages.json")
API = "https://he.wikisource.org/w/api.php"
UA = {"User-Agent": "rambam-schedule/1.0 (personal study schedule tool; contact: local)"}

HUBS = [
    "כסף משנה",
    "מגיד משנה",
    "לחם משנה",
    "משנה למלך",
    "אור שמח על רמב\"ם",
    "הגהות מיימוניות",
    "השגות הראב\"ד על רמב\"ם",
    "חידושי רבינו חיים הלוי",
]

# Rambam's own text on WS ("רמב"ם הלכות X") -- used to cross-check hilchot names.
RAMBAM_HE = "רמב\"ם"


def api(params, retries=6, base_sleep=3.0, post=None):
    """Call the MediaWiki API with polite exponential backoff (WS 429s easily).

    Long title lists exceed the URL length limit (HTTP 414), so those go by
    POST; `post=True/False` forces the method either way.
    """
    if post is None:
        post = len(urllib.parse.urlencode(params)) > 1500
    last = None
    for attempt in range(retries):
        try:
            if post:
                data = urllib.parse.urlencode(params).encode("utf-8")
                req = urllib.request.Request(API, data=data, headers=UA)
            else:
                req = urllib.request.Request(
                    API + "?" + urllib.parse.urlencode(params), headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(base_sleep * (2 ** attempt))
                continue
            if e.code in (400, 403, 404, 414):
                raise
            time.sleep(base_sleep * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(base_sleep * (attempt + 1))
    raise RuntimeError(f"API {params.get('action')} failed: {last}")


def allpages(prefix):
    out = []
    cont = None
    while True:
        p = {"action": "query", "list": "allpages", "apprefix": prefix,
             "aplimit": "500", "format": "json"}
        if cont:
            p.update(cont)
        d = api(p)
        out += [x["title"] for x in d["query"]["allpages"]]
        cont = d.get("continue")
        if not cont:
            return out
        time.sleep(0.8)


def page_lengths(titles):
    """{title: content length in bytes} via batched prop=info (25 titles/call)."""
    lens = {}
    for i in range(0, len(titles), 25):
        chunk = titles[i:i + 25]
        d = api({"action": "query", "prop": "info", "titles": "|".join(chunk),
                 "format": "json"}, post=True)
        for _pid, p in (d.get("query", {}).get("pages", {}) or {}).items():
            t = p.get("title")
            if t is None or p.get("missing") is not None:
                continue
            lens[t] = p.get("length", 0)
        time.sleep(0.8)
    return lens


def report(cache, ram_he_names=()):
    for hub, rec in cache.items():
        pages = rec["pages"]
        groups = rec["groups"]
        total = sum(v["len"] for v in pages.values())
        print(f"\n=== {hub}: {len(pages)} pages, {len(groups)} hilchot groups, "
              f"~{total//1024} KB wikitext")
        if len(groups) <= 8:
            for g, ts in sorted(groups.items()):
                print(f"    {g}: {len(ts)} page(s), "
                      f"{sum(pages[t]['len'] for t in ts)//1024} KB")
        else:
            sub = collections.Counter(len(ts) for ts in groups.values())
            print("    pages-per-group histogram:", dict(sorted(sub.items())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="report from cache only")
    ap.add_argument("--hubs", nargs="*", default=None)
    args = ap.parse_args()

    cache = {}
    if os.path.exists(OUT):
        try:
            cache = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            cache = {}

    if args.report:
        report(cache)
        return

    hubs = args.hubs or HUBS
    for hub in hubs:
        if hub in cache and cache[hub].get("pages"):
            print(f"[cache] {hub}: {len(cache[hub]['pages'])} pages")
            continue
        titles = allpages(hub + "/")
        titles += [t for t in allpages(hub) if t == hub or t.startswith(hub + " ")]
        titles = sorted(set(titles))
        lens = page_lengths(titles)
        groups = collections.defaultdict(list)
        for t in titles:
            parts = t.split("/")
            g = parts[1] if len(parts) >= 2 else "(hub)"
            groups[g].append(t)
        cache[hub] = {"pages": {t: {"len": lens.get(t, 0)} for t in titles},
                      "groups": {g: ts for g, ts in sorted(groups.items())}}
        json.dump(cache, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[fetch] {hub}: {len(titles)} pages -> saved")

    # The Rambam text itself, for hilchot-name cross-checking.
    if RAMBAM_HE not in cache:
        titles = allpages(RAMBAM_HE + " ")
        lens = page_lengths(titles)
        cache[RAMBAM_HE] = {"pages": {t: {"len": lens.get(t, 0)} for t in titles},
                            "groups": {t: [t] for t in titles}}
        json.dump(cache, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[fetch] {RAMBAM_HE}: {len(titles)} pages -> saved")

    report(cache)


if __name__ == "__main__":
    main()
