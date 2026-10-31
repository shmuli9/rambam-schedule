"""Download Steinsaltz's Hebrew word notes for the whole Yad from Sefaria.

Used only as the *source* for translate_steinsaltz.py; raw Hebrew is written to
st_src/<slug>.json (gitignored -- Steinsaltz is all rights reserved) as
{"t": [[[gloss, ...] per halacha] per perek]} with HTML reduced to <b>phrase</b>.
"""
import json, os, re, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "st_src")


def get(url):
    for a in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "rambam-schedule/1.0"}), timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            if a == 3: raise
        except Exception:
            if a == 3: raise
        time.sleep(2 * (a + 1))


def clean(x):
    x = re.sub(r"<(?!/?b>)[^>]*>", "", str(x))
    return re.sub(r"\s+", " ", x).strip()


def main():
    data = json.load(open(os.path.join(HERE, "data", "rambam.json"), encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)
    tot = 0
    for s in data["sefarim"]:
        for h in s["hilchot"]:
            slug = h["slug"]
            path = os.path.join(OUT, re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_") + ".json")
            if os.path.exists(path):
                continue
            title = "Steinsaltz on " + slug.replace("_", " ").replace(" Their ", " their ")  # Sefaria spells it so
            if re.search(r"Mitzvot$|Transmission_of_the_Oral_Law$|Overview", slug):
                continue
            t = []
            for pi, n in enumerate(h["perek_halachot"], 1):
                d = get("https://www.sefaria.org/api/texts/" + urllib.parse.quote(f"{title} {pi}:1-{n}") + "?context=0&commentary=0")
                he = (d or {}).get("he") or []
                if he and isinstance(he[0], str):
                    he = [he]
                per = [[clean(g) for g in (a if isinstance(a, list) else [a]) if clean(g)] for a in he]
                per += [[]] * (n - len(per))
                t.append(per[:n])
                time.sleep(0.15)
            json.dump({"t": t}, open(path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            c = sum(len(g) for p in t for g in p); w = sum(len(x.split()) for p in t for g in p for x in g)
            tot += w
            print(f"{slug}: {c} glosses, {w} words", flush=True)
    print("new words:", tot)


if __name__ == "__main__":
    main()
