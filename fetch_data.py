"""Fetch Rambam Mishneh Torah structure + per-perek halacha counts from Sefaria.

Downloads (once, cached):
  1. Sefaria full table of contents -> extract Mishneh Torah tree (sefarim -> hilchot)
  2. Per-hilchot v3 texts API -> halacha counts + word counts per halacha

Writes data/rambam.json:
{
  "fetched_at": "...",
  "sefarim": [
    {"name_en": "...", "name_he": "...",
     "hilchot": [
        {"slug": "Mishneh_Torah,_De'ot", "title_en": "...", "title_he": "...",
         "perek_halachot": [7, 7, 3, 23, 13, 10, 8],
         "halacha_words": [[w1..w7], [..], ...]}
     ]}
  ]
}
"""
import json
import os
import sys
import io
import time
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
TOC_CACHE = os.path.join(DATA_DIR, "sefaria_toc.json")
OUT_FILE = os.path.join(DATA_DIR, "rambam.json")

TOC_URL = "https://www.sefaria.org/api/index/"
TEXTS_URL = "https://www.sefaria.org/api/v3/texts/"
UA = {"User-Agent": "rambam-schedule-builder/1.0 (personal study schedule tool)"}


def http_json(url, timeout=60, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def find_node(node, category_name):
    """Recursively find category node by name in TOC tree."""
    if isinstance(node, dict):
        if node.get("category") == category_name:
            return node
        for child in node.get("contents", []) or []:
            found = find_node(child, category_name)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = find_node(child, category_name)
            if found:
                return found
    return None


def ordered(children):
    """Sort TOC children by 'order' field (None last, keep stable)."""
    return sorted(children, key=lambda c: (c.get("order") is None, c.get("order") or 0))


def collect_indices(node, acc):
    """Flatten leaf index entries under a category node, preserving order."""
    if "title" in node and node.get("title"):
        acc.append(node)
        return
    for child in ordered(node.get("contents", []) or []):
        collect_indices(child, acc)


def count_words(s):
    """Rough Hebrew word count: whitespace-separated tokens."""
    if not isinstance(s, str):
        return 0
    return len([t for t in s.split() if t.strip("\u05be–-")])


def save_out(sefarim, n_h):
    out = {
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "sefaria.org",
        "sefarim": sefarim,
    }
    json.dump(out, open(OUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # 1) TOC (cached)
    if os.path.exists(TOC_CACHE):
        print(f"Using cached TOC: {TOC_CACHE}")
        toc = json.load(open(TOC_CACHE, encoding="utf-8"))
    else:
        print("Downloading Sefaria TOC (a few MB)...")
        toc = http_json(TOC_URL, timeout=120)
        json.dump(toc, open(TOC_CACHE, "w", encoding="utf-8"))

    mt = find_node(toc, "Mishneh Torah")
    if mt is None:
        sys.exit("ERROR: could not find 'Mishneh Torah' node in TOC")

    sefarim = []
    skipped_books = []
    for book in ordered(mt.get("contents", [])):
        if "title" in book and book.get("title"):
            sys.exit("ERROR: unexpected leaf directly under Mishneh Torah")
        book_name_en = book.get("category")
        book_name_he = book.get("heCategory")
        if not (book_name_en or "").startswith("Sefer") and book_name_en != "Introduction":
            skipped_books.append(book_name_en)
            continue
        hilchot_nodes = []
        collect_indices(book, hilchot_nodes)
        hilchot = []
        for h in hilchot_nodes:
            title = h["title"]            # e.g. "Mishneh Torah, Human Dispositions"
            he_title = h.get("heTitle")   # e.g. "משנה תורה, דעות"
            hilchot.append({
                "slug": title.replace(" ", "_"),
                "title_en": title,
                "title_he": he_title,
            })
        sefarim.append({
            "name_en": book_name_en,
            "name_he": book_name_he,
            "hilchot": hilchot,
        })

    n_h = sum(len(s["hilchot"]) for s in sefarim)
    print(f"Found {len(sefarim)} sefarim, {n_h} hilchot (skipped: {skipped_books})", flush=True)

    # 2) Halacha counts per perek (cached inside rambam.json if rerun)
    old = {}
    if os.path.exists(OUT_FILE):
        try:
            prev = json.load(open(OUT_FILE, encoding="utf-8"))
            for s in prev.get("sefarim", []):
                for h in s["hilchot"]:
                    old[h["slug"]] = h
        except Exception:
            pass

    done = 0
    for s in sefarim:
        for h in s["hilchot"]:
            cached = old.get(h["slug"])
            if isinstance(cached, dict) and cached.get("perek_halachot") and cached.get("halacha_words"):
                h["perek_halachot"] = cached["perek_halachot"]
                h["halacha_words"] = cached["halacha_words"]
                continue
            url = TEXTS_URL + urllib.parse.quote(h["slug"], safe="")
            d = http_json(url, timeout=90)
            versions = d.get("versions") or []
            text = None
            # prefer a primary Hebrew version with jagged [perek][halakha] text;
            # accept depth-1 texts (e.g. the Introduction) as a single perek
            for v in sorted(versions, key=lambda v: (not v.get("isPrimary"),
                                                     v.get("language") != "he")):
                t = v.get("text")
                if isinstance(t, list) and t:
                    if isinstance(t[0], list):
                        text = t
                        break
                    if isinstance(t[0], str) and text is None:
                        text = [t]
            if text is None:
                sys.exit(f"ERROR: no jagged text for {h['slug']}")
            h["perek_halachot"] = [len(p) for p in text]
            h["halacha_words"] = [[count_words(seg) for seg in p] for p in text]
            done += 1
            print(f"  [{done}] {h['slug']}: {len(text)} perakim, "
                  f"{sum(h['perek_halachot'])} halachot, "
                  f"{sum(sum(p) for p in h['halacha_words'])} words", flush=True)
            time.sleep(0.25)
            if done % 10 == 0:
                save_out(sefarim, n_h)

    out = save_out(sefarim, n_h)

    total_p = sum(len(h["perek_halachot"]) for s in sefarim for h in s["hilchot"])
    total_h = sum(sum(h["perek_halachot"]) for s in sefarim for h in s["hilchot"])
    print(f"Wrote {OUT_FILE}")
    print(f"Totals: {len(sefarim)} sefarim, {n_h} hilchot, {total_p} perakim, {total_h} halachot")


if __name__ == "__main__":
    main()
