"""Download the English translation of the whole Yad from Sefaria into texts_en/<slug>.json.

Source: Eliyahu Touger's Moznaim translation (CC-BY-NC on Sefaria), including
its English explanatory footnotes -- these are the "English commentary" the
schedule page shows under each halakha. Same file naming and halacha numbering
as fetch_text.py / data/rambam.json, so the page can line the two up.

  python fetch_text_en.py          # fetch what's missing
  python fetch_text_en.py --force  # refetch everything

Each file: {"v": <version title>, "t": [[[text, [note, ...]] per halacha] per perek]}
where `text` carries {1}, {2}... markers pointing at `note` entries, and both
are plain text except for <i>..</i>, <b>..</b> and <br> (everything else stripped).
Halachot are None where Sefaria's English lacks that halakha.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "rambam.json")
OUT = os.path.join(HERE, "texts_en")
URL = "https://www.sefaria.org/api/v3/texts/"
VERSION = "Mishneh Torah, trans. by Eliyahu Touger. Jerusalem, Moznaim Pub. c1986-c2007"


def file_name(slug):
    return re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_") + ".json"


KEEP = {"i", "b"}


class _Split(HTMLParser):
    """Separate a halakha's prose from its footnotes.

    Sefaria marks them as <sup class="footnote-marker">N</sup><i class="footnote">body</i>;
    the body may itself contain <i>/<b>/<br>. Prose gets a {N} marker where the
    footnote hung, notes are collected in order.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.notes = [], []
        self.cur = None      # list being filled while inside a footnote body
        self.depth = 0       # <i> nesting inside that footnote
        self.in_sup = False

    def out(self):
        return self.cur if self.cur is not None else self.text

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if tag == "sup" and "footnote-marker" in cls:
            self.in_sup = True
        elif tag == "i" and "footnote" in cls and self.cur is None:
            self.cur, self.depth = [], 1
        elif self.cur is not None and tag == "i":
            self.depth += 1
            self.cur.append("<i>")
        elif tag == "br":
            self.out().append("<br>")
        elif tag in KEEP:
            self.out().append("<%s>" % tag)

    def handle_endtag(self, tag):
        if tag == "sup":
            self.in_sup = False
        elif self.cur is not None and tag == "i":
            self.depth -= 1
            if self.depth == 0:
                self.notes.append("".join(self.cur))
                self.text.append("{%d}" % len(self.notes))
                self.cur = None
            else:
                self.cur.append("</i>")
        elif tag in KEEP:
            self.out().append("</%s>" % tag)

    def handle_data(self, data):
        if not self.in_sup:
            self.out().append(data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def tidy(s):
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*<br>\s*", "<br>", s)
    s = re.sub(r"(<br>)+$|^(<br>)+", "", s)
    return s.strip()


def split_notes(raw):
    """-> (text with {n} markers, [notes]); both HTML-escaped, only <i>/<b>/<br> kept"""
    p = _Split()
    p.feed(raw)
    p.close()
    return tidy("".join(p.text)), [tidy(n) for n in p.notes]


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
            q = urllib.parse.quote(h["slug"], safe="") + "?version=" + urllib.parse.quote("english|" + VERSION, safe="")
            d = get(URL + q)
            vs = [v for v in d.get("versions") or [] if isinstance(v.get("text"), list) and v["text"]]
            if not vs:
                print(f"{h['slug']}: no English, skipped", flush=True)
                continue
            t = vs[0]["text"]
            if isinstance(t[0], str):
                t = [t]
            out, bad = [], 0
            for pi, want in enumerate(h["perek_halachot"]):
                per = t[pi] if pi < len(t) else []
                if len(per) != want:
                    bad += 1
                row = [split_notes(x) if isinstance(x, str) and x.strip() else None for x in per[:want]]
                row += [None] * (want - len(row))
                out.append([[a, b] if a else None for a, b in (r or ("", []) for r in row)])
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"v": vs[0].get("versionTitle", ""), "t": out}, f, ensure_ascii=False,
                          separators=(",", ":"))
            total += os.path.getsize(path)
            print(f"{h['slug']}: {sum(map(len, out))} perakim-halachot" + (f"  ({bad} perakim with count mismatch)" if bad else ""), flush=True)
            time.sleep(0.25)
    print(f"texts_en/: {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
