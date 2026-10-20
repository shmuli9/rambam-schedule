"""Download the Hebrew text of the whole Yad from Sefaria into texts/<slug>.json.

The schedule page shows each day's actual text in a modal; these files let the
installed PWA do that offline (the service worker caches them). Same Hebrew
version selection as fetch_data.py, so halacha numbers line up with the word
counts in data/rambam.json (checked below).

  python fetch_text.py          # fetch what's missing
  python fetch_text.py --force  # refetch everything

Each file: {"v": <version title>, "t": [[halacha, ...] per perek]} with HTML,
footnotes and markup stripped.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "rambam.json")
OUT = os.path.join(HERE, "texts")
URL = "https://www.sefaria.org/api/v3/texts/"


def file_name(slug):
    return re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_") + ".json"


def plain(s):
    s = re.sub(r"<sup\b[^>]*>.*?</sup>", "", s, flags=re.S)
    s = re.sub(r"<i class=\"footnote\">.*?</i>", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def get(url):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "rambam-schedule/1.0"}), timeout=90) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            print("  retry:", e, flush=True)
            time.sleep(2 * (attempt + 1))


def pick(d):
    for v in sorted(d.get("versions") or [], key=lambda v: (not v.get("isPrimary"),
                                                            v.get("language") != "he")):
        t = v.get("text")
        if isinstance(t, list) and t:
            if isinstance(t[0], list):
                return v, t
            if isinstance(t[0], str):
                return v, [t]
    sys.exit("no jagged text")


def main():
    force = "--force" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    total = 0
    for s in data["sefarim"]:
        for h in s["hilchot"]:
            path = os.path.join(OUT, file_name(h["slug"]))
            if os.path.exists(path) and not force:
                total += os.path.getsize(path)
                continue
            v, t = pick(get(URL + urllib.parse.quote(h["slug"], safe="")))
            t = [[plain(x) for x in p] for p in t]
            if [len(p) for p in t] != h["perek_halachot"]:
                sys.exit(f"ERROR: {h['slug']} halacha counts differ from data/rambam.json")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"v": v.get("versionTitle", ""), "t": t}, f, ensure_ascii=False,
                          separators=(",", ":"))
            total += os.path.getsize(path)
            print(f"{h['slug']}: {sum(map(len, t))} halachot", flush=True)
            time.sleep(0.25)
    print(f"texts/: {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
