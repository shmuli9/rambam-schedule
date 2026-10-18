"""Analyse the commentary-density difficulty proxy.

Answers the questions that decide whether the proxy is worth blending in:
  * how much do the hand weights and the commentary proxy agree?  (if they were
    redundant there would be no point blending)
  * does *breadth* (how many commentators wrote on a hilchot) add anything over
    *density* (how much they wrote)?
  * which hilchot move the most, and does that look right?

Reads data/commentary.json and data/weights.csv.  No network access.

Usage:
    python analyze_proxy.py
    python analyze_proxy.py --top 20
"""
import json
import os
import sys
import io
import csv
import math
import argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def spearman(xs, ys):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    return pearson(rank(xs), rank(ys))


def proxy_scores(com, min_commentators=3):
    """Reproduce generate_schedule.compute_proxy_weights' normalisation."""
    H = com["hilchot"]
    mef_set = set()
    for r in H.values():
        mef_set.update((r.get("meforshim") or {}).keys())
    G = {}
    for m in mef_set:
        ratios = []
        for r in H.values():
            mr = (r.get("meforshim") or {}).get(m)
            rw = r.get("rambam_words", 0)
            if mr and mr.get("present") and mr.get("words", 0) > 0 and rw > 0:
                ratios.append(mr["words"] / rw)
        if ratios:
            G[m] = math.exp(sum(math.log(x) for x in ratios) / len(ratios))
    raw, nc, dens = {}, {}, {}
    for slug, r in H.items():
        rw = r.get("rambam_words", 0)
        contribs = []
        for m, mr in (r.get("meforshim") or {}).items():
            if mr.get("present") and mr.get("words", 0) > 0 and m in G and rw > 0:
                contribs.append((mr["words"] / rw) / G[m])
        raw[slug] = (sum(contribs) / len(contribs)) if contribs else 0.0
        nc[slug] = r.get("commentators", len(contribs))
        dens[slug] = r.get("density", 0.0)
    vals = [v for v in raw.values() if v > 0]
    G_raw = math.exp(sum(math.log(v) for v in vals) / len(vals)) if vals else 1.0
    proxy = {s: (raw[s] / G_raw if raw[s] > 0 else 0.0) for s in H}
    return proxy, nc, dens, G_raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-commentators", type=int, default=3)
    ap.add_argument("--commentary", default=os.path.join(DATA_DIR, "commentary.json"))
    ap.add_argument("--weights", default=os.path.join(DATA_DIR, "weights.csv"))
    args = ap.parse_args()

    com = json.load(open(args.commentary, encoding="utf-8"))
    proxy, nc, dens, G_raw = proxy_scores(com, args.min_commentators)

    hand, names = {}, {}
    with open(args.weights, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                hand[row["slug"]] = float(row["weight"])
            except (KeyError, ValueError):
                continue
            names[row["slug"]] = (row.get("sefer_en", ""), row.get("hilchot_en", ""))

    basket = com.get("basket") or []
    print(f"basket: {len(basket)} commentators over {len(com['hilchot'])} hilchot")
    print(f"proxy geometric mean = {G_raw:.3f} (normalised to 1.0)\n")

    usable = [s for s in hand if proxy.get(s, 0) > 0
              and nc.get(s, 0) >= args.min_commentators]
    print(f"hilchot with a usable proxy: {len(usable)}/{len(hand)} "
          f"(gate: >={args.min_commentators} commentators)")
    excluded = [s for s in hand if s not in set(usable)]
    if excluded:
        print("  falling back to hand weight:")
        for s in excluded:
            print(f"    {names[s][1][:44]:44s} commentators={nc.get(s, 0)}")

    h = [hand[s] for s in usable]
    p = [proxy[s] for s in usable]
    b = [float(nc[s]) for s in usable]
    print("\ncorrelations (on usable hilchot):")
    print(f"  hand vs density proxy : Pearson {pearson(h, p):+.3f}  "
          f"Spearman {spearman(h, p):+.3f}")
    print(f"  hand vs breadth       : Pearson {pearson(h, b):+.3f}  "
          f"Spearman {spearman(h, b):+.3f}")
    print(f"  density vs breadth    : Pearson {pearson(p, b):+.3f}  "
          f"Spearman {spearman(p, b):+.3f}")
    lh = [math.log(x) for x in h]
    lp = [math.log(x) for x in p if x > 0]
    if len(lh) == len(lp):
        print(f"  log hand vs log proxy : Pearson {pearson(lh, lp):+.3f}")

    ranked = sorted(usable, key=lambda s: -(proxy[s] / hand[s]))
    print(f"\nproxy rates these HARDER than the hand weights do (top {args.top}):")
    for s in ranked[:args.top]:
        se, he = names[s]
        print(f"  x{proxy[s] / hand[s]:5.2f}  proxy={proxy[s]:5.2f} hand={hand[s]:4.2f} "
              f"ncmt={nc[s]:3d}  {he[:46]}")
    print(f"\nproxy rates these EASIER than the hand weights do (top {args.top}):")
    for s in ranked[-args.top:][::-1]:
        se, he = names[s]
        print(f"  x{proxy[s] / hand[s]:5.2f}  proxy={proxy[s]:5.2f} hand={hand[s]:4.2f} "
              f"ncmt={nc[s]:3d}  {he[:46]}")

    print("\nhighest / lowest raw commentary density:")
    for s in sorted(usable, key=lambda s: -dens[s])[:8]:
        print(f"  {dens[s]:6.2f}  ncmt={nc[s]:3d}  {names[s][1][:46]}")
    print("  ...")
    for s in sorted(usable, key=lambda s: dens[s])[:8]:
        print(f"  {dens[s]:6.2f}  ncmt={nc[s]:3d}  {names[s][1][:46]}")


if __name__ == "__main__":
    main()
