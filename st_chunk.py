"""Split st_src/*.json into translation chunks (flat arrays of glosses) and re-assemble.
  python st_chunk.py split WORKDIR      -> WORKDIR/in/NNN.json  (+ index.json)
  python st_chunk.py merge WORKDIR      -> texts_st_en/<slug>.json from WORKDIR/out/NNN.json
"""
import glob, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 6000   # Hebrew words per chunk


def main():
    mode, work = sys.argv[1], sys.argv[2]
    if mode == "split":
        chunks, cur, curw, idx = [], [], 0, []
        for f in sorted(glob.glob(os.path.join(HERE, "st_src", "*.json"))):
            slug = os.path.basename(f)[:-5]
            t = json.load(open(f, encoding="utf-8"))["t"]
            for pi, p in enumerate(t):
                for hi, g in enumerate(p):
                    for gi, x in enumerate(g):
                        cur.append(x); idx.append([slug, pi, hi, gi]); curw += len(x.split())
                        if curw >= SIZE:
                            chunks.append((cur, idx)); cur, idx, curw = [], [], 0
        if cur: chunks.append((cur, idx))
        for n, (c, ix) in enumerate(chunks):
            json.dump(c, open(os.path.join(work, "in", f"{n:03d}.json"), "w", encoding="utf-8"), ensure_ascii=False)
            json.dump(ix, open(os.path.join(work, "in", f"{n:03d}.idx"), "w"))
        print(len(chunks), "chunks")
    else:
        out = {}
        miss = 0
        for f in sorted(glob.glob(os.path.join(work, "in", "*.idx"))):
            n = os.path.basename(f)[:-4]
            ix = json.load(open(f))
            src = json.load(open(os.path.join(work, "in", n + ".json"), encoding="utf-8"))
            op = os.path.join(work, "out", n + ".json")
            tr = json.load(open(op, encoding="utf-8")) if os.path.exists(op) else None
            if tr is None or len(tr) != len(src):
                miss += 1; print("not ready, leaving blank:", n); continue
            for (slug, pi, hi, gi), e in zip(ix, tr):
                out.setdefault(slug, {}).setdefault(pi, {}).setdefault(hi, {})[gi] = e
        # unfinished chunks leave their notes blank ("") -- the page then shows Hebrew only for those;
        # sources with no finished chunk at all are skipped
        print(miss, "chunks not ready")
        os.makedirs(os.path.join(HERE, "texts_st_en"), exist_ok=True)
        for f in glob.glob(os.path.join(work, "in", "*.idx")):    # slugs with every chunk missing still get a file
            for slug, *_ in json.load(open(f))[:1]:
                out.setdefault(slug, {})
        for slug, per in out.items():
            src = json.load(open(os.path.join(HERE, "st_src", slug + ".json"), encoding="utf-8"))["t"]
            t = [[[per.get(pi, {}).get(hi, {}).get(gi, "") for gi in range(len(g))] for hi, g in enumerate(p)] for pi, p in enumerate(src)]
            json.dump({"t": t}, open(os.path.join(HERE, "texts_st_en", slug + ".json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        print("wrote", len(out), "files")


main()
