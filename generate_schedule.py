"""Rambam Yad learning schedule generator (difficulty-weighted, word-based).

Pacing model: weighted word budget.
  Each halacha has an "effort" = (word count) x (difficulty weight of its
  hilchot). Each learning day consumes ~`budget` effort: short/easy perakim
  are combined, long/hard ones are split at halacha boundaries, with the
  split placed by word count so parts are as even as possible.

Difficulty weights: data/weights.csv (slug -> weight, editable).
  Missing hilchot default to 1.0.

Off days (no learning):
  - Friday (Erev Shabbat) and Shabbat            -> omitted from the sheet
  - Yom Tov, Erev Yom Tov, Tisha B'Av           -> shown greyed-out with reason

Outputs: XLSX (Schedule + Summary sheets), PDF (landscape table) and HTML
(interactive table with sorting/filtering).

Usage:
  python generate_schedule.py [--start 2026-10-12] [--budget AUTO]
                              [--slack AUTO] [--min-split 40] [--max-perakim 5]
                              [--weights data/weights.csv] [--no-weights]
                              [--israel] [--outdir output]
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys

from hdate import HDateInfo
from hdate.holidays import HolidayTypes

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "data", "rambam.json")
WEIGHTS_FILE = os.path.join(HERE, "data", "weights.csv")
SPLITS_FILE = os.path.join(HERE, "data", "halacha_splits.json")

# Baseline: the old "15 halachot/day" model completed the Yad in 995 learning
# days. Auto budget keeps the same raw (unweighted) word throughput.
BASELINE_DAYS = 995

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_weights(path):
    """slug -> (weight, rationale)."""
    weights = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    weights[row["slug"]] = (float(row["weight"]),
                                            row.get("rationale", ""))
                except (ValueError, KeyError):
                    continue
    return weights


# A deliberately small, evenly-spread basket: the four commentators who each
# cover 67-82 of the 84 hilchot, so (almost) every hilchot is scored by the same
# voices.  Compare with the full 39-commentator basket in the HTML.
CORE_BASKET = ["Kessef Mishneh", "Kiryat Sefer", "Ohr Sameach", "Mishneh LaMelech"]


def compute_proxy_weights(commentary_path, hand_weights, alpha=0.5, coverage_min=0.25,
                          min_commentators=3, effort_neutral=True, basket=None):
    """Blend the hand-tuned weights with a commentary-density difficulty proxy.

    The proxy for a hilchot is its *commentary density* -- total words written
    on it by a basket of Rambam meforshim, divided by the Rambam's own word
    count.  Each meforshim is first normalised to its own geometric mean so a
    verbose commentator (e.g. Tzafnat Pa'neach) cannot dominate and a missing
    one merely drops out; the per-hilchot averages are then normalised so the
    proxy has geometric mean 1.0 (a clean effort multiplier).

    Per-author normalisation is what makes the basket robust to *uneven
    coverage*: commentators do not all write on the same hilchot (Kessef Mishneh
    covers 82, the Radbaz 3), so a hilchot is scored only by those who actually
    wrote on it, each measured against their own average.  A hilchot nobody
    commented on scores as average rather than as easy.

    Two gates decide whether a hilchot may use the proxy at all:
      * min_commentators -- with only one or two voices the density estimate is
        noise, so fall back to the hand weight;
      * coverage_min -- fraction of attempted nodes that returned usable text
        (a data-quality check on the fetch, not on the hilchot).

    composite = hand^(1-alpha) * proxy^alpha.  Hilchot failing either gate (and
    those with no commentary data at all -- the Introduction, the Order of
    Prayer) keep their hand weight, so sparse digitisation never drags a sugya
    toward "easy".

    With effort_neutral=True (default) the blended weights are rescaled so the
    TOTAL weighted words equal the hand-only total -- the proxy then only
    reshuffles *relative* difficulty and never changes the overall cycle length
    (which --budget controls).  Set effort_neutral=False to let the blend set
    its own pace.

    basket -- optional list of commentator names to restrict the proxy to (e.g.
    CORE_BASKET); None uses every commentator in commentary.json.  The
    min_commentators gate then counts only commentators from this list.

    Returns (weights_dict, rows) where weights_dict is slug -> (weight,
    rationale) ready to hand to load_entries, and rows is a list of per-hilchot
    detail dicts for data/proxy_weights.csv.
    """
    import math
    try:
        with open(commentary_path, encoding="utf-8") as f:
            com = json.load(f)
    except FileNotFoundError:
        print(f"WARNING: commentary file {commentary_path} not found; "
              f"using hand weights only.")
        return dict(hand_weights), []
    H = com.get("hilchot", {})
    if not H:
        return dict(hand_weights), []
    only = set(basket) if basket else None
    basket_size = len(only) if only else len(com.get("basket") or [])

    # Per-meforshim geometric mean of (commentary words / Rambam words).
    mef_set = set()
    for r in H.values():
        mef_set.update(r.get("meforshim", {}).keys())
    if only:
        mef_set &= only
    G = {}
    for m in mef_set:
        ratios = []
        for r in H.values():
            mr = r.get("meforshim", {}).get(m)
            rw = r.get("rambam_words", 0)
            if mr and mr.get("present") and mr.get("words", 0) > 0 and rw > 0:
                ratios.append(mr["words"] / rw)
        if ratios:
            G[m] = math.exp(sum(math.log(x) for x in ratios) / len(ratios))

    # Unnormalised proxy per hilchot = mean of (ratio / G_m) over present m.
    raw, cov, dens, ncmt = {}, {}, {}, {}
    for slug, r in H.items():
        contribs = []
        rw = r.get("rambam_words", 0)
        for m, mr in r.get("meforshim", {}).items():
            if only and m not in only:
                continue
            if mr.get("present") and mr.get("words", 0) > 0 and m in G and rw > 0:
                contribs.append((mr["words"] / rw) / G[m])
        raw[slug] = (sum(contribs) / len(contribs)) if contribs else 0.0
        cov[slug] = r.get("coverage", 0.0)
        if only:
            dens[slug] = (sum(mr["words"] for m, mr in r.get("meforshim", {}).items()
                              if m in only and mr.get("present")) / rw) if rw else 0.0
            ncmt[slug] = len(contribs)
        else:
            dens[slug] = r.get("density", 0.0)
            ncmt[slug] = r.get("commentators", len(contribs))

    vals = [v for v in raw.values() if v > 0]
    G_raw = math.exp(sum(math.log(v) for v in vals) / len(vals)) if vals else 1.0
    proxy = {s: (raw[s] / G_raw if raw[s] > 0 else 0.0) for s in H}

    # Build the blended composite for every hand-weight hilchot.
    composite, rows = {}, []
    for slug, (hw, rat) in hand_weights.items():
        pv = proxy.get(slug, 0.0)
        c = cov.get(slug, 0.0)
        nc = ncmt.get(slug, 0)
        usable = (pv > 0) and (nc >= min_commentators) and (c >= coverage_min)
        comp = (hw ** (1 - alpha) * pv ** alpha) if usable else hw
        composite[slug] = comp
        rows.append({
            "slug": slug,
            "coverage": c,
            "commentators": nc,
            "basket_size": basket_size,
            "density": dens.get(slug, 0.0),
            "hand_weight": hw,
            "proxy_weight": pv if usable else 0.0,
            "composite_weight": comp,
            "source": "proxy" if usable else "hand",
        })
    rows_by_slug = {r["slug"]: r for r in rows}

    # Effort-neutral: rescale only the proxy-used hilchot so their total weighted
    # words match the hand-only total for those same hilchot.  Hand-only hilchot
    # (e.g. the Introduction, Order of Prayer) keep their hand weight exactly --
    # the proxy only reshuffles distribution, not overall pace.
    if effort_neutral:
        words = {}
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for s in data["sefarim"]:
            for h in s["hilchot"]:
                words[h["slug"]] = sum(sum(p) for p in h.get("halacha_words", []))
        used = [r["slug"] for r in rows if r["source"] == "proxy"]
        hand_total = sum(words[s] * hand_weights[s][0] for s in used)
        comp_total = sum(words[s] * composite[s] for s in used)
        if comp_total > 0:
            k = hand_total / comp_total
            for s in used:
                composite[s] *= k
                rows_by_slug[s]["composite_weight"] = composite[s]
    weights_out = {slug: (composite[slug], rat) for slug, (w, rat) in hand_weights.items()}
    return weights_out, rows


def write_proxy_csv(rows, out_path, names=None):
    """Write data/proxy_weights.csv: per-hilchot hand/proxy/composite weights."""
    import csv as _csv
    names = names or {}
    fields = ["slug", "sefer_en", "hilchot_en", "commentators", "basket_size",
              "coverage", "density", "hand_weight", "proxy_weight",
              "composite_weight", "source"]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            n = names.get(r["slug"], {})
            w.writerow({
                "slug": r["slug"],
                "sefer_en": n.get("sefer_en", ""),
                "hilchot_en": n.get("hilchot_en", ""),
                "commentators": r.get("commentators", 0),
                "basket_size": r.get("basket_size", 0),
                "coverage": round(r["coverage"], 3),
                "density": round(r["density"], 3),
                "hand_weight": round(r["hand_weight"], 3),
                "proxy_weight": round(r["proxy_weight"], 3),
                "composite_weight": round(r["composite_weight"], 3),
                "source": r["source"],
            })


def load_splits(path=SPLITS_FILE):
    """slug -> {perek -> {halacha -> [parts]}} from data/halacha_splits.json.

    Each part is {"label": str, "words": int}; they must add up to the
    halacha's word count in rambam.json (checked in load_entries).
    """
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        blob = json.load(f)
    out = {}
    for slug, perakim in blob.get("hilchot", {}).items():
        for perek, halachot in perakim.items():
            for halacha, spec in halachot.items():
                parts = spec.get("parts") if isinstance(spec, dict) else None
                if not parts:
                    continue
                out.setdefault(slug, {}).setdefault(int(perek), {})[int(halacha)] = spec
    return out


def load_entries(data_file=DATA_FILE, weights=None, splits=None):
    """Flatten rambam.json into an ordered list of perek entries.

    The atomic unit a day can stop at is normally one halacha, but an
    oversized halacha may be pre-split into parts (see load_splits); either
    way every entry exposes a parallel `units` list describing what each
    element of `halacha_words` actually is.
    """
    with open(data_file, encoding="utf-8") as f:
        data = json.load(f)
    weights = weights or {}
    splits = splits if splits is not None else load_splits()
    entries = []
    for sefer in data["sefarim"]:
        for hil in sefer["hilchot"]:
            hil_en = hil["title_en"].replace("Mishneh Torah, ", "")
            hil_he = (hil.get("title_he") or "").replace("משנה תורה, ", "").replace("משנה תורה,", "")
            unit = "halachot"
            if sefer["name_en"] == "Introduction":
                unit = "mitzvot" if "Mitzvot" in hil_en else "sections"
            counts = hil["perek_halachot"]
            words = hil.get("halacha_words")
            if not words:
                sys.exit(f"ERROR: no word counts for {hil['slug']}. "
                         f"Re-run: python fetch_data.py")
            wgt, rationale = weights.get(hil["slug"], (1.0, ""))
            hil_splits = splits.get(hil["slug"], {})
            for i, n in enumerate(counts, start=1):
                unit_words, units = [], []
                for h_no, h_words in enumerate(words[i - 1], start=1):
                    spec = hil_splits.get(i, {}).get(h_no) or {}
                    parts = spec.get("parts") if isinstance(spec, dict) else spec
                    if parts:
                        if sum(p["words"] for p in parts) != h_words:
                            sys.exit(f"ERROR: split of {hil['slug']} {i}:{h_no} "
                                     f"adds up to {sum(p['words'] for p in parts)} "
                                     f"words, expected {h_words}")
                        for p_no, p in enumerate(parts, start=1):
                            unit_words.append(p["words"])
                            units.append({"halacha": h_no, "part": p_no,
                                          "n_parts": len(parts), "label": p.get("label", "")})
                    else:
                        unit_words.append(h_words)
                        units.append({"halacha": h_no, "part": None})
                entries.append({
                    "slug": hil["slug"],
                    "sefer_en": sefer["name_en"],
                    "sefer_he": sefer["name_he"] or "",
                    "hil_en": hil_en,
                    "hil_he": hil_he,
                    "perek": i,
                    "n_halachot": n,
                    "halacha_words": unit_words,
                    "units": units,
                    "weight": wgt,
                    "unit": unit,
                })
    return entries


# ---------------------------------------------------------------------------
# Calendar classification
# ---------------------------------------------------------------------------

HOLIDAY_NAMES = {
    "erev_rosh_hashana": "Erev Rosh Hashana",
    "rosh_hashana_i": "Rosh Hashana (day 1)",
    "rosh_hashana_ii": "Rosh Hashana (day 2)",
    "erev_yom_kippur": "Erev Yom Kippur",
    "yom_kippur": "Yom Kippur",
    "erev_sukkot": "Erev Sukkot",
    "sukkot": "Sukkot (day 1)",
    "sukkot_ii": "Sukkot (day 2)",
    "hoshana_raba": "Erev Yom Tov (Hoshana Rabba)",
    "shmini_atzeret": "Shmini Atzeret",
    "simchat_torah": "Simchat Torah",
    "erev_pesach": "Erev Pesach",
    "pesach": "Pesach (day 1)",
    "pesach_ii": "Pesach (day 2)",
    "pesach_vii": "Pesach (day 7)",
    "pesach_viii": "Pesach (day 8)",
    "erev_shavuot": "Erev Shavuot",
    "shavuot": "Shavuot (day 1)",
    "shavuot_ii": "Shavuot (day 2)",
    "tisha_bav": "Tisha B'Av",
    # 20 Nisan in diaspora: chol hamoed but also erev of final Pesach days
    "hol_hamoed_pesach": "Erev Yom Tov (eve of final Pesach days)",
    "hol_hamoed_sukkot": "Erev Yom Tov (Hoshana Rabba)",
}


def classify_day(d, diaspora=True):
    """Return (kind, reason, info) where kind in {'learn','routine_off','special_off'}."""
    info = HDateInfo(d, diaspora=diaspora)
    for h in info.holidays:
        if h.type in (HolidayTypes.YOM_TOV, HolidayTypes.EREV_YOM_TOV):
            name = HOLIDAY_NAMES.get(h.name)
            if name is None:
                pretty = h.name.replace("_", " ").title()
                name = (f"Erev Yom Tov ({pretty})"
                        if h.type == HolidayTypes.EREV_YOM_TOV
                        else f"Yom Tov ({pretty})")
            return "special_off", name, info
        if h.name == "tisha_bav":
            return "special_off", HOLIDAY_NAMES["tisha_bav"], info
    if d.weekday() in (4, 5):  # Friday / Shabbat
        return "routine_off", None, info
    return "learn", None, info


# ---------------------------------------------------------------------------
# Budget chunker (weighted words)
# ---------------------------------------------------------------------------

class Cursor:
    """Walks the flat sequence of halachot across all perakim."""

    def __init__(self, entries):
        self.entries = entries
        self.i = 0            # entry index
        self.off = 0          # halacha offset within current entry (0-based)

    @property
    def done(self):
        return self.i >= len(self.entries)

    def next_day(self, budget, slack, max_perakim=5, min_split_words=40):
        """Consume ~budget weighted words; returns (items, effort).

        items: list of (entry, h_from, h_to) 1-based inclusive.
        Effort of a halacha = words x hilchot weight.
        """
        items = []
        effort = 0.0
        while not self.done and len(items) < max_perakim:
            e = self.entries[self.i]
            hw = e["halacha_words"]
            wgt = e["weight"]
            rest_effort = sum(hw[self.off:]) * wgt
            room = budget - effort

            if room > 0 and rest_effort <= room + slack:
                # finish this perek today (slack avoids tiny leftover splits)
                items.append((e, self.off + 1, len(hw)))
                effort += rest_effort
                self.i += 1
                self.off = 0
                continue
            if room <= 0:
                break

            # greedily take whole halachot while they fit in room
            k, eff = 0, 0.0
            while self.off + k < len(hw) and eff + hw[self.off + k] * wgt <= room:
                eff += hw[self.off + k] * wgt
                k += 1
            if k == 0:
                if items:
                    break
                k, eff = 1, hw[self.off] * wgt   # single halacha bigger than budget
            else:
                taken_words = sum(hw[self.off:self.off + k])
                if (taken_words < min_split_words and self.off == 0
                        and self.off + k < len(hw) and items):
                    break  # don't start a new perek with a tiny fragment
            items.append((e, self.off + 1, self.off + k))
            effort += eff
            self.off += k
            if self.off >= len(hw):
                self.i += 1
                self.off = 0
        return items, effort


def item_words(e, a, b):
    return sum(e["halacha_words"][a - 1:b])


def n_units(e):
    """Number of atomic units in this perek (may exceed the halacha count
    when a long halacha has been pre-split into parts)."""
    return len(e["halacha_words"])


def units_of(e):
    return e.get("units") or [{"halacha": i + 1, "part": None}
                              for i in range(n_units(e))]


def item_halachot(e, a, b):
    """Halachot touched: parts of a split halacha count once each group."""
    return len({u["halacha"] for u in units_of(e)[a - 1:b]})


def item_span(e, a, b):
    """Human-readable description of units a\u2013b (1-based inclusive)."""
    units = units_of(e)[a - 1:b]
    split = [u for u in units if u["part"]]
    if len({u["halacha"] for u in units}) == 1 and units[0]["part"]:
        u0, u1 = units[0], units[-1]
        if u0["part"] == u1["part"]:
            return (f"halakha {u0['halacha']}, part {u0['part']} of {u0['n_parts']} "
                    f"({u0['label']})")
        return (f"halakha {u0['halacha']}, parts {u0['part']}\u2013{u1['part']} "
                f"of {u0['n_parts']} ({u0['label']} \u2026)")
    word = e.get("unit", "halachot")
    lo, hi = units[0]["halacha"], units[-1]["halacha"]
    rng = str(lo) if lo == hi else f"{lo}\u2013{hi}"
    tail = ""
    if split and len(split) != len(units):
        tail = " (incl. " + ", ".join(f"{u['halacha']}.{u['part']}" for u in split) + ")"
    return f"{word} {rng} of {e['n_halachot']}" + tail


def item_label(e, a, b):
    w = item_words(e, a, b)
    unit = e.get("unit", "halachot")
    whole = (a == 1 and b == n_units(e))
    if whole:
        return (f"{e['hil_en']} {e['perek']} "
                f"(whole perek, {e['n_halachot']} {unit}, {w} words)")
    return f"{e['hil_en']} {e['perek']} ({item_span(e, a, b)}, {w} words)"


def item_label_short(e, a, b):
    """Compact label used in the PDF."""
    whole = (a == 1 and b == n_units(e))
    if whole:
        return (f"Perek {e['perek']} (whole, {e['n_halachot']} hal., "
                f"{item_words(e, a, b)} w)")
    return (f"Perek {e['perek']}: {item_span(e, a, b)} "
            f"({item_words(e, a, b)} w)")


# ---------------------------------------------------------------------------
# Row generation
# ---------------------------------------------------------------------------

def build_rows(entries, start, budget, slack, max_perakim=5,
               min_split_words=40, diaspora=True):
    cur = Cursor(entries)
    rows = []
    d = start
    day_no = 0
    limit = start + dt.timedelta(days=int(366 * 40))
    while not cur.done and d < limit:
        kind, reason, info = classify_day(d, diaspora=diaspora)
        hebrew_date = str(info.hdate)
        if kind == "learn":
            day_no += 1
            items, effort = cur.next_day(budget, slack, max_perakim, min_split_words)
            rows.append({
                "kind": "learn",
                "day_no": day_no,
                "date": d,
                "weekday": d.strftime("%a"),
                "hebrew_date": hebrew_date,
                "items": items,
                "halachot": sum(item_halachot(e, a, b) for e, a, b in items),
                "words": sum(item_words(e, a, b) for e, a, b in items),
                "effort": effort,
            })
        elif kind == "special_off":
            rows.append({
                "kind": "skip",
                "date": d,
                "weekday": d.strftime("%a"),
                "hebrew_date": hebrew_date,
                "reason": reason,
            })
        # routine_off (Fri/Sat) -> omitted entirely
        d += dt.timedelta(days=1)
    completion = rows[-1]["date"] if (rows and cur.done) else None
    return rows, completion


# ---------------------------------------------------------------------------
# XLSX output
# ---------------------------------------------------------------------------

def write_xlsx(rows, completion, params, out_path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Schedule"

    headers = ["Day #", "Date", "Weekday", "Hebrew Date", "Sefer",
               "Hilchot", "Hilchot (Hebrew)", "Learning",
               "Halachot", "Words", "Effort", "Status / Reason"]
    ws.append(headers)

    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    grey_fill = PatternFill("solid", fgColor="D9D9D9")
    grey_font = Font(color="808080", italic=True)
    sefer_fill = PatternFill("solid", fgColor="DDEBF7")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    center = Alignment(horizontal="center", vertical="top")

    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    r = 2
    prev_sedarim = set()
    for row in rows:
        if row["kind"] == "learn":
            sefarim_today = list(dict.fromkeys(e["sefer_en"] for e, _, _ in row["items"]))
            sefer_en = ", ".join(sefarim_today)
            hil_en = ", ".join(dict.fromkeys(e["hil_en"] for e, _, _ in row["items"]))
            hil_he = ", ".join(dict.fromkeys(e["hil_he"] for e, _, _ in row["items"]))
            learning = "\n".join(item_label(e, a, b) for e, a, b in row["items"])
            new_sedarim = [s for s in sefarim_today if s not in prev_sedarim]
            prev_sedarim.update(sefarim_today)
            new_sefer = bool(new_sedarim)
            status = f"\u2192 New sefer: {', '.join(new_sedarim)}" if new_sefer else ""
            vals = [row["day_no"], row["date"].strftime("%Y-%m-%d"), row["weekday"],
                    row["hebrew_date"], sefer_en, hil_en, hil_he, learning,
                    row["halachot"], row["words"], round(row["effort"]), status]
        else:
            vals = ["", row["date"].strftime("%Y-%m-%d"), row["weekday"],
                    row["hebrew_date"], "", "", "", "", "", "", "",
                    f"No learning \u2014 {row['reason']}"]
            new_sefer = False

        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = border
            if c in (1, 2, 3, 9, 10, 11):
                cell.alignment = center
            elif c == 8:
                cell.alignment = wrap
            else:
                cell.alignment = Alignment(vertical="top")
            if row["kind"] == "skip":
                cell.fill = grey_fill
                cell.font = grey_font
            elif new_sefer:
                cell.fill = sefer_fill
        r += 1

    widths = [6, 11, 9, 22, 16, 24, 20, 56, 9, 8, 8, 34]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:L{r-1}"

    # ---- Summary sheet ----
    sm = wb.create_sheet("Summary")
    sm.sheet_view.showGridLines = False
    sm["B2"] = "Rambam Yad \u2014 Learning Schedule Summary"
    sm["B2"].font = Font(bold=True, size=14)

    learn_rows = [x for x in rows if x["kind"] == "learn"]
    skip_rows = [x for x in rows if x["kind"] == "skip"]
    total_hal = sum(x["halachot"] for x in learn_rows)
    total_words = sum(x["words"] for x in learn_rows)
    total_effort = sum(x["effort"] for x in learn_rows)

    facts = [
        ("Start date", params["start"].strftime("%A, %d %B %Y")),
        ("Completion date", completion.strftime("%A, %d %B %Y") if completion else "beyond 40 years"),
        ("Pacing model", f"weighted words: ~{params['budget']:.0f} effort/day "
                         f"(slack {params['slack']:.0f}, max {params['max_perakim']} perakim/day, "
                         f"min split {params['min_split_words']} words)"),
        ("Effort formula",
         ("words \u00d7 hilchot weight, blended with commentary-density proxy "
          f"(α={params['proxy_blend']}, ≥{params['proxy_min_commentators']} commentators)"
          if params.get("proxy") else
          "words \u00d7 hilchot difficulty weight (data/weights.csv)")),
        ("Calendar", "Diaspora (UK)" if params["diaspora"] else "Israel"),
        ("Learning days", len(learn_rows)),
        ("Skipped days shown (Yom Tov etc.)", len(skip_rows)),
        ("Totals", f"{total_hal} halachot, {total_words} words, {total_effort:.0f} effort"),
        ("Off days", "Fri, Sat, Yom Tov, Erev Yom Tov, Tisha B'Av "
                     "(learn on Chol HaMoed, Chanukah, Purim, minor fasts, Rosh Chodesh)"),
    ]
    rr = 4
    for k, v in facts:
        sm.cell(row=rr, column=2, value=k).font = Font(bold=True)
        sm.cell(row=rr, column=3, value=v)
        rr += 1

    rr += 1
    sm.cell(row=rr, column=2, value="Progress by Sefer").font = Font(bold=True, size=12)
    rr += 1
    for c, h in enumerate(["Sefer", "Hilchot", "Perakim", "Halachot", "Words",
                           "First day", "Last day"], 2):
        cell = sm.cell(row=rr, column=c, value=h)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
    rr += 1

    agg = {}
    for row in learn_rows:
        for e, a, b in row["items"]:
            s = e["sefer_en"]
            rec = agg.setdefault(s, {"hil": set(), "per": set(), "hal": 0, "words": 0,
                                     "first": row["date"], "last": row["date"]})
            rec["hil"].add(e["hil_en"])
            rec["per"].add((e["hil_en"], e["perek"]))
            rec["hal"] += b - a + 1
            rec["words"] += item_words(e, a, b)
            rec["first"] = min(rec["first"], row["date"])
            rec["last"] = max(rec["last"], row["date"])
    for s, rec in agg.items():
        sm.cell(row=rr, column=2, value=s)
        sm.cell(row=rr, column=3, value=len(rec["hil"]))
        sm.cell(row=rr, column=4, value=len(rec["per"]))
        sm.cell(row=rr, column=5, value=rec["hal"])
        sm.cell(row=rr, column=6, value=rec["words"])
        sm.cell(row=rr, column=7, value=rec["first"].strftime("%Y-%m-%d"))
        sm.cell(row=rr, column=8, value=rec["last"].strftime("%Y-%m-%d"))
        rr += 1

    sm.column_dimensions["B"].width = 30
    sm.column_dimensions["C"].width = 40
    for col in "DEFGH":
        sm.column_dimensions[col].width = 14

    wb.save(out_path)


# ---------------------------------------------------------------------------
# PDF output
# ---------------------------------------------------------------------------

def write_pdf(rows, completion, params, out_path):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from bidi.algorithm import get_display

    font_paths = [
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        (os.path.join(os.path.dirname(__import__("reportlab").__file__), "fonts", "DejaVuSans.ttf"),
         os.path.join(os.path.dirname(__import__("reportlab").__file__), "fonts", "DejaVuSans-Bold.ttf")),
    ]
    reg = False
    for freg, fbold in font_paths:
        if os.path.exists(freg) and os.path.exists(fbold):
            pdfmetrics.registerFont(TTFont("Body", freg))
            pdfmetrics.registerFont(TTFont("Body-Bold", fbold))
            reg = True
            break
    if not reg:
        sys.exit("ERROR: no font with Hebrew glyphs found (Arial/DejaVu)")

    def rtl(s):
        return get_display(s) if s else s

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    st = ParagraphStyle("cell", fontName="Body", fontSize=7.2, leading=9)
    st_he = ParagraphStyle("cell_he", fontName="Body", fontSize=7.6, leading=9.5, alignment=2)
    st_head = ParagraphStyle("head", fontName="Body-Bold", fontSize=8, leading=10,
                             textColor=colors.white)

    doc = SimpleDocTemplate(out_path, pagesize=landscape(A4),
                            leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=14 * mm, bottomMargin=12 * mm,
                            title="Rambam Yad Learning Schedule",
                            author="rambam-schedule")

    def on_page(canv, doc_):
        canv.saveState()
        canv.setFont("Body-Bold", 10)
        canv.drawString(10 * mm, landscape(A4)[1] - 9 * mm,
                        "Rambam Yad \u2014 Learning Schedule")
        canv.setFont("Body", 8)
        loc = "Diaspora (UK)" if params["diaspora"] else "Israel"
        canv.drawString(70 * mm, landscape(A4)[1] - 9 * mm,
                        f"start {params['start']:%d %b %Y} \u00b7 "
                        f"~{params['budget']:.0f} effort/day \u00b7 {loc}")
        canv.drawRightString(landscape(A4)[0] - 10 * mm, 7 * mm, f"Page {canv.getPageNumber()}")
        canv.restoreState()

    headers = ["Day", "Date", "Hebrew Date", "Sefer", "Hilchot",
               "Learning", "Hal.", "Effort", "Notes"]
    data = [[Paragraph(esc(h), st_head) for h in headers]]
    styles = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BFBFBF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]

    r = 1
    prev_sedarim = set()
    for row in rows:
        if row["kind"] == "learn":
            sefarim_today = list(dict.fromkeys(e["sefer_en"] for e, _, _ in row["items"]))
            sefer = ", ".join(sefarim_today)
            hil = ", ".join(dict.fromkeys(e["hil_en"] for e, _, _ in row["items"]))
            hil_he = " | ".join(dict.fromkeys(rtl(e["hil_he"]) for e, _, _ in row["items"]))
            learning_lines = []
            for e, a, b in row["items"]:
                learning_lines.append(esc(item_label_short(e, a, b)))
            learning = Paragraph("<br/>".join(learning_lines), st)
            new_sedarim = [s for s in sefarim_today if s not in prev_sedarim]
            prev_sedarim.update(sefarim_today)
            new_sefer = bool(new_sedarim)
            note = f"\u2192 New sefer: {', '.join(new_sedarim)}" if new_sefer else ""
            vals = [str(row["day_no"]), row["date"].strftime("%d %b %Y"),
                    Paragraph(esc(rtl(row["hebrew_date"])), st_he),
                    Paragraph(esc(sefer), st),
                    Paragraph(esc(hil) + "<br/><font size=7>" + esc(hil_he) + "</font>", st),
                    learning, str(row["halachot"]), str(round(row["effort"])),
                    Paragraph(esc(note), st) if note else ""]
            data.append(vals)
            if new_sefer:
                styles.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#DDEBF7")))
        else:
            vals = ["", row["date"].strftime("%d %b %Y"),
                    Paragraph(esc(rtl(row["hebrew_date"])), st_he),
                    "", "", "", "", "", esc(f"No learning \u2014 {row['reason']}")]
            data.append(vals)
            styles.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#D9D9D9")))
            styles.append(("TEXTCOLOR", (0, r), (-1, r), colors.HexColor("#707070")))
        r += 1

    col_widths = [12 * mm, 19 * mm, 26 * mm, 24 * mm, 36 * mm, 84 * mm,
                  11 * mm, 13 * mm, 26 * mm]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(styles))

    story = [Spacer(1, 2 * mm), t]
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)


# ---------------------------------------------------------------------------
# Calendar events (shared by HTML export button and static .ics)
# ---------------------------------------------------------------------------

def event_for_row(row):
    """(summary, description) for a learning-day ICS event."""
    parts = []
    for e, a, b in row["items"]:
        whole = (a == 1 and b == n_units(e))
        units = units_of(e)[a - 1:b]
        if whole:
            suffix = ""
        elif all(u["part"] is None for u in units):
            lo, hi = units[0]["halacha"], units[-1]["halacha"]
            suffix = (f" ({lo}\u2013{hi})") if lo != hi else (f" ({lo})")
        else:
            u0, u1 = units[0], units[-1]
            pr = (f"{u0['part']}\u2013{u1['part']}" if u1["part"] != u0["part"]
                  else str(u0["part"]))
            suffix = f" ({u0['halacha']}.{pr})"
        parts.append(f"{e['hil_en']} {e['perek']}" + suffix)
    summary = f"Rambam #{row['day_no']}: " + "; ".join(parts)
    sefer = ", ".join(dict.fromkeys(e["sefer_en"] for e, _, _ in row["items"]))
    desc = "\n".join(
        [item_label(e, a, b) for e, a, b in row["items"]] +
        ["", row["hebrew_date"], sefer,
         f"{row['halachot']} halachot, {row['words']} words, effort {round(row['effort'])}"])
    return summary, desc


def write_ics(rows, out_path):
    """Static RFC 5545 .ics with one all-day event per learning day."""

    def esc(s):
        return (s.replace("\\", "\\\\").replace(";", "\\;")
                 .replace(",", "\\,").replace("\n", "\\n"))

    def fold(line):
        out, cur = [], line
        while len(cur.encode("utf-8")) > 74:
            n = 74
            while len(cur[:n].encode("utf-8")) > 74:
                n -= 1
            if 0xD800 <= ord(cur[n - 1]) <= 0xDBFF:
                n -= 1
            out.append(cur[:n])
            cur = " " + cur[n:]
        out.append(cur)
        return "\r\n".join(out)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//rambam-schedule//Rambam Yad//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:Rambam Yad", "X-WR-TIMEZONE:Europe/London"]
    for row in rows:
        if row["kind"] != "learn":
            continue
        summary, desc = event_for_row(row)
        d0 = row["date"]
        d1 = d0 + dt.timedelta(days=1)
        lines += ["BEGIN:VEVENT",
                  f"UID:rambam-{row['day_no']}-{d0:%Y%m%d}@rambam-schedule",
                  f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d0:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{d1:%Y%m%d}",
                  f"SUMMARY:{esc(summary)}",
                  f"DESCRIPTION:{esc(desc)}",
                  "TRANSP:TRANSPARENT",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(fold(l) for l in lines))


# ---------------------------------------------------------------------------
# HTML output (self-contained: re-paces itself in the browser for any effort
# budget; sortable + filterable table, .ics export -- no dependencies)
# ---------------------------------------------------------------------------

# Slowest effort/day offered by the HTML effort control; also sizes the calendar
# window embedded in the HTML (it must reach the finish at that pace).
MIN_EFFORT = 250


def _calendar_window(start, n_days, diaspora=True):
    """[(iso, hebrew_date, off_reason)] for every non-Shabbat day from start.

    Reason is "" on a learning day. This is the calendar half of the schedule,
    precomputed here (it needs hdate) so the HTML can re-pace offline.
    """
    out = []
    d = start
    for _ in range(n_days):
        kind, reason, info = classify_day(d, diaspora=diaspora)
        if kind != "routine_off":
            out.append((d.isoformat(), str(info.hdate), reason or ""))
        d += dt.timedelta(days=1)
    return out


def _first_index(days, start_iso):
    """Index of the first window day on/after `start_iso` (ISO dates sort as strings)."""
    for i, (iso, _heb, _reason) in enumerate(days):
        if iso >= start_iso:
            return i
    return max(0, len(days) - 1)


def _learning_days_needed(entries, budget, max_perakim, min_split_words):
    """Learning days the whole Yad needs at `budget` effort/day."""
    cur = Cursor(entries)
    n = 0
    while not cur.done:
        cur.next_day(budget, round(budget * 0.08), max_perakim, min_split_words)
        n += 1
    return n


def _latest_start_index(days, needed):
    """Latest window index that still leaves `needed` learning days after it."""
    remaining = 0
    best = 0
    for i in range(len(days) - 1, -1, -1):
        if not days[i][2]:          # a learning day
            remaining += 1
        if remaining >= needed:
            best = i
            break
    return best


def _fits(days, entries, budget, max_perakim, min_split_words):
    """True if `days` (from _calendar_window) are enough to finish at `budget`."""
    cur = Cursor(entries)
    for _iso, _heb, reason in days:
        if reason:
            continue
        if cur.done:
            return True
        cur.next_day(budget, round(budget * 0.08), max_perakim, min_split_words)
    return cur.done


def write_html(entries, params, out_path):
    """Self-contained HTML schedule: pick effort/day, table re-paces itself."""
    import json as _json

    # ---- compact payload ---------------------------------------------
    #   SEFARIM  [[en, he]]
    #   HILCHOT  [[sefer_idx, en, he, unit]]
    #   PERAKIM  [[hilchot_idx, perek, "w1,w2,\u2026", weight, n_halachot,
    #              units?]]
    #            units is 0 (every unit is a plain halacha) or an array
    #            parallel to the words: [halacha, part, n_parts, label]
    #            describing pre-split halachot (data/halacha_splits.json)
    #   DAYS     [[iso, hebrew_date, reason_idx]]   (reason 0 = learning day)
    sefarim, sef_idx = [], {}
    hilchot, hil_idx = [], {}
    hil_slug = []
    perakim = []
    for e in entries:
        skey = (e["sefer_en"], e["sefer_he"])
        if skey not in sef_idx:
            sef_idx[skey] = len(sefarim)
            sefarim.append([e["sefer_en"], e["sefer_he"]])
        hkey = (sef_idx[skey], e["hil_en"], e["hil_he"], e["unit"])
        if hkey not in hil_idx:
            hil_idx[hkey] = len(hilchot)
            hilchot.append(list(hkey))
            hil_slug.append(e["slug"])
        rec = [hil_idx[hkey], e["perek"],
               ",".join(str(w) for w in e["halacha_words"]),
               e["weight"]]
        rec.append(e["n_halachot"])
        units = units_of(e)
        if any(u["part"] for u in units):
            rec.append([[u["halacha"], u["part"] or 0, u.get("n_parts") or 0,
                         u.get("label") or ""] for u in units])
        else:
            rec.append(0)
        perakim.append(rec)

    total_hal = sum(e["n_halachot"] for e in entries)
    total_words = sum(sum(e["halacha_words"]) for e in entries)
    total_effort = sum(sum(e["halacha_words"]) * e["weight"] for e in entries)

    # Calendar window behind the HTML's start-date control: it runs from
    # `window_start` (today at the latest) and must still reach the finish at
    # the slowest on-screen pace starting from params["start"].
    min_effort = float(params.get("min_effort") or MIN_EFFORT)
    window_start = params.get("window_start") or min(dt.date.today(), params["start"])
    pre_days = max(0, (params["start"] - window_start).days)
    # the window has to stay big enough even if the cycle starts as late as
    # `latest_start` (later than that the page simply reports it won't finish)
    target_start = max(params.get("latest_start") or params["start"], params["start"])
    horizon = max(400, int(total_effort / min_effort / 0.67) + 200)
    for _ in range(6):
        days = _calendar_window(window_start, pre_days + horizon, params["diaspora"])
        if _fits(days[_first_index(days, target_start.isoformat()):],
                 entries, min_effort, params["max_perakim"],
                 params["min_split_words"]):
            break
        horizon = int(horizon * 1.3) + 200
    # latest date the start control may offer: still leaves room to finish
    # even at the slowest pace
    needed = _learning_days_needed(entries, min_effort, params["max_perakim"],
                                   params["min_split_words"])
    max_start = days[_latest_start_index(days, needed)][0]
    reasons, r_idx = [""], {"": 0}
    day_rows = []
    for iso, heb, reason in days:
        if reason not in r_idx:
            r_idx[reason] = len(reasons)
            reasons.append(reason)
        day_rows.append([iso, heb, r_idx[reason]])

    def dump(obj):
        s = _json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        return s.replace("</", "<\\/")   # safe inside <script>

    # Alternative difficulty weightings the page can switch between:
    #   WSETS [{id, label, w: [weight per HILCHOT index]}]
    wsets = [{"id": ws["id"], "label": ws["label"],
              "w": [round(ws["w"].get(sl, 1.0), 4) for sl in hil_slug]}
             for ws in params.get("weight_sets") or []]
    default_set = params.get("default_set") or (wsets[0]["id"] if wsets else "")

    budget = float(params["budget"])
    effort_max = int(max(3000, round(budget * 2.5 / 100) * 100))
    loc = "Diaspora (UK)" if params["diaspora"] else "Israel"

    data = (
        f"const SEFARIM = {dump(sefarim)};\n"
        f"const HILCHOT = {dump(hilchot)};\n"
        f"const HSLUG = {dump(hil_slug)};\n"
        f"const PERAKIM = {dump(perakim)};\n"
        f"const WSETS = {dump(wsets)};\n"
        f"const DEFAULT_SET = {dump(default_set)};\n"
        f"const REASONS = {dump(reasons)};\n"
        f"const DAYS = {dump(day_rows)};"
    )

    # r-string: backslash escapes belong to JavaScript, not Python
    doc = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, minimum-scale=1, viewport-fit=cover">
<title>Rambam Yad — Learning Schedule</title>
<link rel="manifest" href="../manifest.webmanifest">
<meta name="theme-color" content="#1f4e79">
<link rel="icon" type="image/png" sizes="192x192" href="../icons/icon-192.png">
<link rel="apple-touch-icon" href="../icons/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Rambam Yad">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  :root { --grey:#d9d9d9; --blue:#ddebf7; --head:#1f4e79; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; background:#f5f6f8; }
  button, input, select { font-family: inherit; }
  body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; margin: 0;
         background:#f5f6f8; color:#222; touch-action: manipulation; }
  /* notch / home-indicator / landscape-rounded-corner safe areas */
  :root { --sl: env(safe-area-inset-left, 0px); --sr: env(safe-area-inset-right, 0px);
          --st: env(safe-area-inset-top, 0px); --sb: env(safe-area-inset-bottom, 0px); }
  header { background: var(--head); color:#fff;
           padding: calc(14px + var(--st)) calc(20px + var(--sr)) 14px calc(20px + var(--sl)); }
  header h1 { margin:0; font-size: 20px; }
  header .sub { opacity:.85; font-size: 13px; margin-top: 4px; }
  .controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
              padding: 10px calc(20px + var(--sr)) 10px calc(20px + var(--sl));
              background:#fff; border-bottom:1px solid #ddd;
              position: sticky; top: 0; z-index: 5; }
  .controls input[type=search] { padding:6px 10px; border:1px solid #bbb; border-radius:4px;
              min-width: 240px; font-size:13px; }
  .controls select { padding:6px; border:1px solid #bbb; border-radius:4px; font-size:13px; }
  .controls label { font-size:13px; display:flex; align-items:center; gap:4px; }
  .controls .effort { border:1px solid #c9d3dd; background:#f7f9fb; border-radius:6px;
              padding:4px 8px; gap:8px; }
  .controls .effort > span { font-weight:600; }
  .controls input[type=range] { width: 190px; }
  .controls input[type=number] { width: 74px; padding:5px 6px; border:1px solid #bbb;
              border-radius:4px; font-size:13px; }
  .controls .count { margin-left:auto; font-size:13px; color:#555; }
  .controls button { padding:6px 10px; border:1px solid #bbb; border-radius:4px;
              background:#fff; cursor:pointer; font-size:13px; }
  .controls button.primary { background: var(--head); color:#fff; border-color: var(--head); }
  .controls button.primary:hover { background:#163a5c; }
  .wrap { padding: 0 calc(20px + var(--sr)) calc(30px + var(--sb)) calc(20px + var(--sl)); }
  .panel { background:#fff; border-bottom:1px solid #ddd;
           padding: 10px calc(20px + var(--sr)) 8px calc(20px + var(--sl)); }
  .panel-head { display:flex; flex-wrap:wrap; align-items:baseline; gap:10px;
                font-size:13px; margin-bottom:4px; }
  .panel-head strong { font-size:13px; }
  .panel-head .muted { color:#555; }
  td.wcell { white-space:nowrap; }
  td.wcell .hw { color:#888; font-size:11px; }
  .panel-head button { margin-left:auto; padding:4px 8px; border:1px solid #bbb;
                border-radius:4px; background:#fff; cursor:pointer; font-size:12px; }
  #distChart svg { display:block; width:100%; height:auto; }
  .panel.collapsed #distChart { display:none; }
  table { border-collapse: collapse; width:100%; background:#fff; font-size: 13px; }
  th, td { border: 1px solid #cfd6dd; padding: 5px 8px; vertical-align: top; text-align: left; }
  thead th { background: var(--head); color:#fff; cursor:pointer; user-select:none;
             white-space:nowrap; position: sticky; top: 52px; }
  thead th .arrow { font-size: 10px; opacity:.8; }
  tbody tr:nth-child(even) td { background:#fafbfc; }
  tr.skip td { background: var(--grey) !important; color:#707070; font-style: italic; }
  tr.transition td { background: var(--blue) !important; }
  td.learning { min-width: 320px; }
  td.chk { text-align:center; }
  td.chk input { cursor:pointer; width:16px; height:16px; }
  tr.done td { background:#e6f4ea !important; color:#4a5a4f; }
  [hidden] { display:none !important; }
  .controls .more { display: none; }
  .controls .progress { font-size:13px; color:#2e6b3f; font-weight:600; }
  td.note { min-width: 160px; }
  .he { color:#444; font-size: 12px; }
  td[data-v] { font-variant-numeric: tabular-nums; }
  #stats.warn { color:#ffd97a; font-weight:600; opacity:1; }
  /* narrow screens: let the table scroll sideways inside its own box instead
     of stretching the whole page (the sticky header does not apply there) */
  @media screen and (max-width: 1200px) {
    .wrap { overflow-x: auto; }
    thead th { position: static; }
  }
  /* phones and tablets (narrow or touch-only up to 1024px, so landscape phones too): each day becomes a card, the page itself scrolls (no nested
     scrollers), controls stack full-width with 44px touch targets, and inputs are
     16px so iOS does not zoom the page on focus */
  @media screen and (max-width: 760px), screen and (pointer: coarse) and (max-width: 1024px) {
    /* belt and braces for iOS: nothing may widen the page (a wider page makes
       Safari zoom out and gives a sideways scroll) */
    html, body { max-width: 100%; overflow-x: hidden; }
    .controls input[type=date] { -webkit-appearance: none; appearance: none; display: block;
      max-width: 100%; text-align: left; background: #fff; border: 1px solid #bbb; padding: 0 10px; }
    .controls input[type=date]::-webkit-date-and-time-value { text-align: left; min-width: 0; }
    .controls select { -webkit-appearance: none; appearance: none; max-width: 100%;
      background: #fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' fill='none' stroke='%23555' stroke-width='2'/%3E%3C/svg%3E") no-repeat right 12px center;
      padding: 0 32px 0 10px; text-overflow: ellipsis; border: 1px solid #bbb; }
    header h1 { font-size: 19px; }
    .controls { position: static; gap: 8px; padding-top: 12px; padding-bottom: 12px; }
    .controls label.effort { width: 100%; padding: 6px 10px; }
    .controls label.effort > span { min-width: 76px; }
    .controls label { font-size: 15px; }
    .controls input[type=search], .controls select, .controls input[type=date],
    .controls input[type=number], .controls button {
      font-size: 16px; min-height: 44px; border-radius: 8px; }
    .controls input[type=search] { width: 100%; min-width: 0; }
    .controls input[type=date] { flex: 1; min-width: 0; }
    .controls select { flex: 1 1 140px; min-width: 0; }
    .controls input[type=range] { flex: 1; width: auto; min-width: 0; height: 32px; }
    .controls input[type=number] { width: 84px; flex: none; }
    .controls input[type=checkbox] { width: 22px; height: 22px; }
    .controls button { padding: 0 16px; }
    .controls .count { width: 100%; margin-left: 0; }
    .controls .sec { display: none; }
    .controls.show-more .sec { display: flex; }
    .controls.show-more input.sec, .controls.show-more select.sec, .controls.show-more button.sec { display: block; }
    .controls .more { display: block; width: 100%; }
    .panel { padding-top: 8px; }
    .wrap { overflow: visible; padding: 10px calc(12px + var(--sr)) calc(96px + var(--sb)) calc(12px + var(--sl)); }
    table, tbody { display: block; background: transparent; }
    thead { display: none; }
    table tbody tr[data-type] {
      display: flex; flex-wrap: wrap; align-items: baseline; column-gap: 8px;
      background: #fff; border: 1px solid #dbe1e7; border-radius: 14px;
      padding: 12px 14px; margin: 0 0 10px; box-shadow: 0 1px 2px rgba(0,0,0,.05);
      content-visibility: auto; contain-intrinsic-size: auto 190px;
    }
    table tbody tr[data-type] td { display: block; border: 0; padding: 0; background: transparent !important; }
    table tbody tr[data-type] td:empty { display: none; }
    table tbody tr.skip { background: var(--grey); box-shadow: none; padding: 8px 14px; }
    table tbody tr.done { background: #e6f4ea; border-color: #bfdcc7; }
    table tbody tr.transition { background: var(--blue); border-color: #b7d0e6; }
    tr.skip td { font-style: italic; }
    td.chk { order: -1; margin-right: 6px; align-self: center; }
    td.chk input { width: 26px; height: 26px; }
    td:nth-child(2)::before { content: 'Day '; }
    td:nth-child(2) { font-weight: 700; font-size: 15px; }
    td:nth-child(3), td:nth-child(4) { font-size: 14px; color: #444; }
    td:nth-child(5) { margin-left: auto; font-size: 13px; color: #444; }
    td:nth-child(6) { flex-basis: 100%; margin-top: 8px; font-size: 12px; color: #666; }
    td:nth-child(7) { flex-basis: 100%; font-weight: 600; font-size: 16px; }
    td.learning { flex-basis: 100%; min-width: 0; margin-top: 6px; font-size: 15px; line-height: 1.45; }
    td:nth-child(n+8):nth-child(-n+12):not(.learning) {
      order: 1; flex: 1 1 0; margin-top: 10px; padding-top: 8px; border-top: 1px solid rgba(0,0,0,.08);
      font-size: 14px; }
    td:nth-child(8) { flex-grow: 2; }
    td:nth-child(8)::before { content: 'Weight'; }
    td:nth-child(10)::before { content: 'Halachot'; }
    td:nth-child(11)::before { content: 'Words'; }
    td:nth-child(12)::before { content: 'Effort'; }
    td:nth-child(8)::before, td:nth-child(10)::before, td:nth-child(11)::before,
    td:nth-child(12)::before { display: block; font-size: 11px; color: #777; text-transform: uppercase; letter-spacing: .03em; }
    td.note { order: 2; flex-basis: 100%; min-width: 0; margin-top: 6px; font-size: 13px; color: #1f4e79; }
    tr.skip td.note { margin-top: 0; color: #707070; }
    #todayBtn { position: fixed; right: calc(16px + var(--sr)); bottom: calc(16px + var(--sb)); z-index: 20;
      min-height: 48px; padding: 0 20px; border-radius: 24px; box-shadow: 0 4px 14px rgba(0,0,0,.28); }
  }
  a.rf { color: inherit; text-decoration: underline dotted; text-underline-offset: 3px; cursor: pointer; }
  a.rf:hover { color: var(--head); }
  #txm { position: fixed; inset: 0; z-index: 50; background: rgba(0,0,0,.5); display: flex;
         align-items: center; justify-content: center; padding: 16px; }
  #txm[hidden] { display: none; }
  .txbox { background:#fff; border-radius: 12px; width: min(760px, 100%); max-height: 100%;
           display: flex; flex-direction: column; box-shadow: 0 10px 40px rgba(0,0,0,.35); overflow: hidden; }
  .txhead { display:flex; justify-content: space-between; align-items:center; gap: 10px;
            padding: 12px 16px; background: var(--head); color:#fff; }
  .txhead button { background: transparent; color:#fff; border: 0; font-size: 22px; line-height: 1;
                   min-width: 40px; min-height: 40px; cursor: pointer; }
  #txB { overflow-y: auto; -webkit-overflow-scrolling: touch; overscroll-behavior: contain;
         padding: 4px 16px calc(16px + var(--sb)); }
  .txit h3 { margin: 16px 0 4px; font-size: 15px; }
  .txit .meta { font-size: 13px; color:#444; margin-bottom: 8px; line-height: 1.5; }
  .txit .meta a { color: var(--head); }
  .txit .snip { direction: rtl; unicode-bidi: isolate; color:#000; }
  .hl { direction: rtl; text-align: right; font-size: 20px; line-height: 1.75; padding: 4px 0;
        border-bottom: 1px solid #eee; font-family: "SBL Hebrew", "Frank Ruehl CLM", "David", "Times New Roman", serif; }
  .hl .hn { font-family: system-ui, sans-serif; font-size: 12px; color: #fff; background: #7a8794;
            border-radius: 9px; padding: 1px 7px; margin-left: 8px; direction: ltr; unicode-bidi: isolate; }
  .mk { font-size: 12px; font-weight: 700; letter-spacing: .04em; padding: 3px 8px; border-radius: 6px; margin: 8px 0 2px;
        direction: ltr; text-align: left; }
  .mk.st { background:#e6f4ea; color:#1e6b34; }
  .mk.en { background:#fdecea; color:#a52a1f; }
  .txerr { color:#a52a1f; font-size: 14px; padding: 8px 0; }
  .txbar { display:flex; gap:8px; padding: 8px 16px; border-bottom: 1px solid var(--line); background:#f5f6f8; flex-wrap: wrap; }
  .txbar button { font: inherit; font-size: 14px; min-height: 40px; padding: 0 14px; border-radius: 20px; cursor: pointer;
                  border: 1px solid var(--line); background:#fff; color:#22405c; }
  .txbar button[aria-pressed="true"] { background: var(--head); border-color: var(--head); color:#fff; }
  .hl { margin-top: 2px; }
  .hl.last { border-bottom: 0; }
  /* one halakha = Hebrew, English, notes, Steinsaltz stacked (phone); Hebrew | English side by side on wide screens */
  .hb { border-bottom: 1px solid #e3e6ea; padding: 6px 0 10px; }
  .hb .hl { border-bottom: 0; }
  .eng { direction: ltr; text-align: left; font-family: Georgia, "Times New Roman", serif; font-size: 17px; line-height: 1.6;
        color:#1d2b3a; padding: 6px 0 2px; }
  .eng .cred { display:block; font: 11px system-ui, sans-serif; color:#777; margin-top: 4px; }
  .fn { font: 700 11px system-ui, sans-serif; color:#fff; background: #3b7ab8; border: 0; border-radius: 9px;
        min-width: 22px; min-height: 22px; padding: 0 5px; margin: 0 2px; vertical-align: super; line-height: 1; cursor: pointer; }
  .fn[aria-expanded="true"] { background:#a35a00; }
  .fnote { direction: ltr; text-align: left; font: 14px/1.55 system-ui, sans-serif; background:#fff8e8; border-left: 3px solid #d9a441;
           color:#3a2d12; padding: 8px 10px; margin: 6px 0; border-radius: 0 6px 6px 0; }
  .fnote b { color:#a35a00; margin-right: 4px; }
  .fnote[hidden] { display: none; }
  .sz { direction: rtl; text-align: right; font-size: 16px; line-height: 1.6; background:#eef5fb; border-radius: 6px;
        padding: 6px 10px; margin: 6px 0; color:#10283f; }
  .sz .szl { direction: ltr; unicode-bidi: isolate; display:block; font: 11px system-ui, sans-serif; color:#46627d; margin-bottom: 2px; }
  .sz p { margin: 0 0 6px; }
  #txB.noHe .hl:not(.mkw), #txB.noEn .eng, #txB.noEn .fns { display: none; }
  #txB.noSt .sz, #txB.noSt .glnotes { display: none; }
  .gl { background: #dcebf9; border-bottom: 2px solid #3b7ab8; border-radius: 3px; cursor: pointer; padding: 0 1px; }
  .gl.open { background: #b9d6f2; }
  #txB.noSt .gl { background: none; border-bottom: 0; cursor: auto; }
  .glnote { font-size: 16px; margin: 4px 0 6px; }
  .glnote b { color: #1f4e79; }
  #txB.allNotes .glnote[hidden] { display: block; }
  #txB.allNotes .fnote[hidden] { display: block; }
  @media (max-width: 600px) {
    #txm { padding: 0; }
    .txbox { border-radius: 0; height: 100%; }
    #txB { padding-left: 12px; padding-right: 12px; }
    .txbar { padding: 8px 12px; }
  }
  @media (min-width: 900px) {
    .txbox { width: min(1040px, 100%); }
    #txB.sbs .hb { display: grid; grid-template-columns: 1fr 1fr; column-gap: 24px; }
    #txB.sbs .hb > .hl { grid-column: 2; grid-row: 1; }
    #txB.sbs .hb > .engg { grid-column: 1; grid-row: 1; }
    #txB.sbs .hb > .fns, #txB.sbs .hb > .sz { grid-column: 1 / -1; }
    #txB.sbs .hb > .glnotes { grid-column: 1 / -1; grid-row: 2; }
  }
  @media print {
    #txm { display: none !important; }
    #todayBtn { display: none !important; }
    @page { size: A4 landscape; margin: 10mm; }
    body { background:#fff; }
    .controls, .panel, header .sub button { display: none !important; }
    header { background:#fff; color:#000; padding: 0 0 4px; }
    header .sub { color:#444; }
    #stats.warn { color:#7a3b00; }
    .wrap { padding: 0; }
    table { font-size: 8.5px; }
    thead { display: table-header-group; }
    thead th { position: static; cursor: default; }
    thead th, tr.skip td, tr.transition td {
      -webkit-print-color-adjust: exact; print-color-adjust: exact;
    }
    tbody tr:nth-child(even) td { background:#fff; }
    tr { page-break-inside: avoid; }
    td.learning { min-width: 0; }
    .he { color:#333; }
  }
</style>
</head>
<body>
<header>
  <h1>Rambam Yad — Learning Schedule</h1>
  <div class="sub">@@TOTAL_HAL@@ halachot / @@TOTAL_WORDS@@ words ·
    @@LOC@@ · <span id="stats"></span></div>
</header>
<div class="controls">
  <label class="effort" title="First learning day. Shabbos, Yom Tov and fast days are skipped automatically.">
    <span>Start</span>
    <input type="date" id="startDate" min="@@MIN_START@@" max="@@MAX_START@@" value="@@START_ISO@@">
  </label>
  <label class="effort" title="Effort = words × difficulty weight. Higher = more per day = fewer days.">
    <span>Effort/day</span>
    <input type="range" id="effort" min="@@MIN_EFFORT@@" max="@@EFFORT_MAX@@" step="10" value="@@DEFAULT@@">
    <input type="number" inputmode="numeric" id="effortNum" min="50" max="20000" step="10" value="@@DEFAULT@@">
  </label>
  <label class="effort" id="wsetLabel" hidden title="Which difficulty weighting sets each day's load. Hand weights are subjective; the commentary options blend in how much the classical meforshim wrote on each hilchot. Total effort is kept equal, so the effort/day scale is comparable.">
    <span>Difficulty</span>
    <select id="wset"></select>
  </label>
  <input type="search" class="sec" id="q" placeholder="Search (hilchot, perek, date, reason…)">
  <select id="sefer" class="sec"><option value="">All sefarim</option>@@SEFER_OPTS@@</select>
  <select id="type" class="sec">
    <option value="">All rows</option>
    <option value="learn">Learning days only</option>
    <option value="skip">Skipped days only</option>
  </select>
  <label class="sec"><input type="checkbox" id="hideSkips"> Hide skipped days</label>
  <button id="moreBtn" class="more" aria-expanded="false">Search, filter &amp; export</button>
  <button id="todayBtn" class="primary" title="Scroll to today's learning">Today</button>
  <button id="reset" class="sec">Reset</button>
  <span class="progress" id="progress"></span>
  <button id="clearDone" title="Forget all completed days (stored only in this browser)" hidden>Clear progress</button>
  <button id="restoreBtn" class="sec" title="Restore completed days from a daily backup in your gist">Restore</button>
  <button id="syncBtn" title="Sync completed days across devices through a private GitHub gist">Sync</button>
  <button id="ics" class="primary sec" title="Download an .ics file of the current pace — import into Apple Calendar, Gmail, Outlook, etc.">📅 Export .ics</button>
  <span class="count sec" id="count"></span>
</div>
<div class="panel" id="distPanel">
  <div class="panel-head">
    <strong>Distribution of actual effort per day</strong>
    <span class="muted" id="distStats"></span>
    <button id="distToggle">Hide</button>
  </div>
  <div id="distChart"></div>
</div>
<div class="wrap">
<table id="sched">
  <thead><tr>
    <th data-type="num" title="Tick a day to mark it and every day before it as completed. Completed days keep their halachot when you change the effort.">Done</th>
    <th data-type="num">Day #</th>
    <th data-type="str">Date</th>
    <th data-type="str">Weekday</th>
    <th data-type="str">Hebrew Date</th>
    <th data-type="str">Sefer</th>
    <th data-type="str">Hilchot</th>
    <th data-type="num">Weight</th>
    <th data-type="str">Learning</th>
    <th data-type="num">Halachot</th>
    <th data-type="num">Words</th>
    <th data-type="num">Effort</th>
    <th data-type="str">Status / Reason</th>
  </tr></thead>
  <tbody></tbody>
</table>
</div>
<div id="txm" hidden role="dialog" aria-modal="true" aria-labelledby="txT">
  <div class="txbox">
    <div class="txhead"><b id="txT"></b><button id="txX" aria-label="Close">&#10005;</button></div>
    <div class="txbar" id="txbar">
      <button data-v="He" aria-pressed="true" title="Hebrew text">עברית</button>
      <button data-v="En" aria-pressed="true" title="English translation (Touger)">English</button>
      <button data-v="Notes" aria-pressed="false" title="Open all the English explanatory notes (or tap a number to open just one)">All notes</button>
      <button data-v="St" aria-pressed="false" title="Steinsaltz's modern Hebrew word-by-word explanation (needs a connection the first time)">Steinsaltz</button>
    </div>
    <div id="txB"></div>
  </div>
</div>
<script>
/* ---- embedded data -------------------------------------------------- */
@@DATA@@
const DEFAULT_EFFORT = @@DEFAULT@@;
const DEFAULT_START = '@@START_ISO@@';
const MIN_SPLIT = @@MIN_SPLIT@@;
const MAX_PERAKIM = @@MAX_PERAKIM@@;
const PWORD = PERAKIM.map(p => p[2].split(',').map(Number));

(function () {
  const table = document.getElementById('sched');
  const tbody = table.tBodies[0];
  const q = document.getElementById('q');
  const seferSel = document.getElementById('sefer');
  const typeSel = document.getElementById('type');
  const hideSkips = document.getElementById('hideSkips');
  const countEl = document.getElementById('count');
  const resetBtn = document.getElementById('reset');
  const effortRange = document.getElementById('effort');
  const effortNum = document.getElementById('effortNum');
  const startDate = document.getElementById('startDate');
  const wsetSel = document.getElementById('wset');
  const statsEl = document.getElementById('stats');
  const progressEl = document.getElementById('progress');
  const clearBtn = document.getElementById('clearDone');
  const chartEl = document.getElementById('distChart');
  const stats2El = document.getElementById('distStats');
  const distPanel = document.getElementById('distPanel');
  const distToggle = document.getElementById('distToggle');
  const ths = Array.from(table.tHead.rows[0].cells);
  const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  let current = [];      // rows of the current schedule
  let domRows = [];
  let sortCol = -1, sortDir = 1;

  /* ---- helpers ------------------------------------------------------ */
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s).replace(/[&<>"']/g, c => ESC[c]); }
  function uniq(a) { return Array.from(new Set(a)); }
  function fmtW(w) { return String(Math.round(w * 100) / 100); }
  function wdOf(iso) { return WEEKDAYS[new Date(iso + 'T00:00:00Z').getUTCDay()]; }
  function nextDay(iso) {
    return new Date(Date.parse(iso + 'T00:00:00Z') + 86400000).toISOString().slice(0, 10);
  }
  function prettyDate(iso) {
    return new Date(iso + 'T00:00:00Z').toLocaleDateString('en-GB', {
      weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC'
    });
  }
  function sliceSum(a, from, to) { let s = 0; for (let i = from; i < to; i++) s += a[i]; return s; }
  // first window day on/after `iso` (ISO date strings sort lexicographically)
  function startIndex(iso) {
    let lo = 0, hi = DAYS.length;
    while (lo < hi) { const mid = (lo + hi) >> 1;
      if (DAYS[mid][0] < iso) lo = mid + 1; else hi = mid; }
    return lo;
  }
  function pickedStart() { return startDate.value || DEFAULT_START; }
  // Python-compatible rounding: round() ties to even, Math.round() ties up.
  function rint(x) {
    const f = Math.floor(x), frac = x - f;
    if (frac > 0.5) return f + 1;
    if (frac < 0.5) return f;
    return (f % 2 === 0) ? f : f + 1;
  }

  // a perek's words are already split into atomic units (a halacha, or a
  // part of a pre-split long halacha); p[5] describes them when needed
  function unit(p, i) {
    return p[5] ? { halacha: p[5][i - 1][0], part: p[5][i - 1][1] || null,
                    n_parts: p[5][i - 1][2] || 0, label: p[5][i - 1][3] || '' }
                : { halacha: i, part: null, n_parts: 0, label: '' };
  }
  function halachaCount(p) { return p[4]; }

  function spanText(p, a, b) {
    const word = HILCHOT[p[0]][3];
    const u0 = unit(p, a), u1 = unit(p, b);
    const splitUnits = [];
    for (let i = a; i <= b; i++) if (unit(p, i).part) splitUnits.push(i);
    if (!splitUnits.length) {
      const rng = u0.halacha === u1.halacha ? String(u0.halacha)
                                            : u0.halacha + '\u2013' + u1.halacha;
      return word + ' ' + rng + ' of ' + halachaCount(p);
    }
    if (u0.halacha === u1.halacha && u0.part && u1.part) {
      return u0.part === u1.part
        ? 'halakha ' + u0.halacha + ', part ' + u0.part + ' of ' + u0.n_parts +
          ' (' + u0.label + ')'
        : 'halakha ' + u0.halacha + ', parts ' + u0.part + '\u2013' + u1.part + ' of ' +
          u0.n_parts + ' (' + u0.label + ' \u2026)';
    }
    const tail = ' (incl. ' + splitUnits.map(i => {
      const u = unit(p, i); return u.halacha + '.' + u.part;
    }).join(', ') + ')';
    return word + ' ' + u0.halacha + '\u2013' + u1.halacha + ' of ' + halachaCount(p) + tail;
  }

  function itemLabel(p, w, a, b) {
    const h = HILCHOT[p[0]], unit = h[3], n = w.length;
    const words = sliceSum(w, a - 1, b);
    const whole = (a === 1 && b === n);
    return whole ? h[1] + ' ' + p[1] + ' (whole perek, ' + halachaCount(p) + ' ' + unit +
                   ', ' + words + ' words)'
                 : h[1] + ' ' + p[1] + ' (' + spanText(p, a, b) + ', ' + words + ' words)';
  }

  /* ---- completed days (kept in this browser's localStorage) ---------- */
  const STORE_KEY = 'rambam-yad-progress-v1';
  // [{iso, items: [[perekIdx, firstUnit, lastUnit], ...]}, ...] in schedule order
  let frozen = loadFrozen();
  function loadFrozen() {
    try {
      const d = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
      if (d && d.n === PERAKIM.length && Array.isArray(d.days)) return d.days;
    } catch (e) { /* storage blocked or corrupt: start empty */ }
    return [];
  }
  function saveFrozen(local) {
    try {
      if (frozen.length) localStorage.setItem(STORE_KEY, JSON.stringify({ n: PERAKIM.length, days: frozen }));
      else localStorage.removeItem(STORE_KEY);
    } catch (e) { /* ignore */ }
    if (local !== false) { sync.t = Date.now(); saveSync(); syncSoon(); }
  }

  /* ---- cross-device sync via a private GitHub gist ------------------- */
  // The token (classic, "gist" scope only) and the last-change time live in their own
  // localStorage key; the progress key above is untouched. Newest change wins.
  const SYNC_KEY = 'rambam-yad-sync-v1', GIST_FILE = 'rambam-progress.json', BACKUPS = 14;
  let sync = (function () {
    try { return JSON.parse(localStorage.getItem(SYNC_KEY) || 'null') || {}; } catch (e) { return {}; }
  })();
  function saveSync() { try { localStorage.setItem(SYNC_KEY, JSON.stringify(sync)); } catch (e) { /* ignore */ } }
  function gh(path, method, body) {
    return fetch('https://api.github.com' + path, {
      method: method || 'GET',
      headers: { Authorization: 'Bearer ' + sync.token, Accept: 'application/vnd.github+json' },
      body: body ? JSON.stringify(body) : undefined
    }).then(r => { if (!r.ok) throw new Error('GitHub ' + r.status); return r.json(); });
  }
  function syncLabel(s) {
    const b = document.getElementById('syncBtn');
    if (b) b.textContent = s;
  }
  let syncTimer = 0, syncing = false;
  function syncSoon() { if (sync.token) { clearTimeout(syncTimer); syncTimer = setTimeout(syncNow, 1000); } }
  async function syncNow() {
    if (!sync.token || syncing) return;
    syncing = true; syncLabel('Syncing…');
    try {
      const mine = () => JSON.stringify({ n: PERAKIM.length, t: sync.t || 0, days: frozen });
      if (!sync.gist) {                                     // find ours, else create it
        const list = await gh('/gists?per_page=100');
        const g = list.find(x => x.files && x.files[GIST_FILE]);
        if (g) sync.gist = g.id;
      }
      let remote = null, rfiles = {};
      if (sync.gist) {
        const g = await gh('/gists/' + sync.gist);
        rfiles = g.files;
        remote = JSON.parse(rfiles[GIST_FILE].content);
        if (remote.n !== PERAKIM.length || !Array.isArray(remote.days)) remote = null;
      }
      const rt = remote ? remote.t || 0 : -1, lt = sync.t || 0;
      // never synced here but has progress on both sides: keep whichever is further along
      const useRemote = remote && (lt ? rt > lt : remote.days.length > frozen.length);
      if (useRemote) {
        frozen = remote.days; sync.t = rt;
        saveFrozen(false); saveSync(); rebuild();
      }
      const push = !remote || (!useRemote && (lt ? lt > rt : frozen.length > remote.days.length));
      // one snapshot per day (last BACKUPS kept) so a mistaken clear/untick can be restored
      const bn = 'backup-' + todayIso() + '.json', files = {};
      const snap = frozen.length && !rfiles[bn];
      if (push || snap) {
        if (push && !lt) sync.t = Date.now();
        if (push) files[GIST_FILE] = { content: mine() };
        if (snap) {
          files[bn] = { content: mine() };
          Object.keys(rfiles).filter(k => /^backup-/.test(k)).sort()
            .slice(0, Math.max(0, Object.keys(rfiles).filter(k => /^backup-/.test(k)).length + 1 - BACKUPS))
            .forEach(k => { files[k] = null; });
        }
        if (sync.gist) await gh('/gists/' + sync.gist, 'PATCH', { files: files });
        else sync.gist = (await gh('/gists', 'POST', { description: 'Rambam schedule progress', public: false, files: files })).id;
        saveSync();
      }
      const d = new Date();
      syncLabel('✓ Synced ' + d.getHours() + ':' + String(d.getMinutes()).padStart(2, '0'));
    } catch (e) {
      syncLabel('Sync failed');
      if (/40[13]/.test(e.message)) { sync.token = ''; saveSync(); }
    } finally { syncing = false; }
  }
  async function syncRestore() {
    if (!sync.token || !sync.gist) { alert('Turn on Sync first.'); return; }
    try {
      const g = await gh('/gists/' + sync.gist);
      const names = Object.keys(g.files).filter(k => /^backup-/.test(k)).sort().reverse();
      if (!names.length) { alert('No backups yet (one is saved per day the app is opened).'); return; }
      const info = names.map(k => { const d = JSON.parse(g.files[k].content).days; return k.slice(7, 17) + ' (' + d.length + ' days done)'; });
      const pick = prompt('Restore progress from which day?\n' + info.join('\n') + '\n\nType the date (YYYY-MM-DD):', names[0].slice(7, 17));
      const f = pick && g.files['backup-' + pick.trim() + '.json'];
      if (!f) return;
      frozen = JSON.parse(f.content).days;
      saveFrozen(); rebuild();
    } catch (e) { syncLabel('Restore failed'); }
  }
  function syncSetup() {
    const t = prompt(sync.token
      ? 'Sync is on. Paste a new token to replace it, or clear the box to turn sync off.'
      : 'Paste a GitHub classic token with only the "gist" scope. Progress is stored in a private gist.', '');
    if (t === null) return;
    if (!t.trim()) { if (sync.token) { sync = { t: sync.t }; saveSync(); syncLabel('Sync'); } return; }
    sync.token = t.trim(); sync.gist = ''; saveSync(); syncNow();
  }

  function makeLearnRow(items, effort, day, dayNo, seen, done) {
    const his = items.map(it => PERAKIM[it.p][0]);
    const seferIdx = uniq(his.map(hi => HILCHOT[hi][0]));
    const newNames = seferIdx.filter(si => !seen.has(si)).map(si => SEFARIM[si][0]);
    seferIdx.forEach(si => seen.add(si));
    const weights = uniq(items.map(it => PERAKIM[it.p][3]));
    return {
      type: 'learn', done: done, dayNo: dayNo, iso: day[0], heb: day[1], items: items,
      sefer: seferIdx.map(si => SEFARIM[si][0]).join(', '),
      hil: uniq(his.map(hi => HILCHOT[hi][1])).join(', '),
      hilHe: uniq(his.map(hi => HILCHOT[hi][2])).join(', '),
      weightStr: weights.map(fmtW).join(' / '),
      weightAvg: weights.reduce((x, y) => x + y, 0) / weights.length,
      newSefer: newNames.length ? '→ New sefer: ' + newNames.join(', ') : '',
      halachot: items.reduce((s, it) => {
        const hs = new Set();
        for (let i = it.a; i <= it.b; i++) hs.add(unit(PERAKIM[it.p], i).halacha);
        return s + hs.size;
      }, 0),
      words: items.reduce((s, it) => s + sliceSum(PWORD[it.p], it.a - 1, it.b), 0),
      effort: effort
    };
  }

  /* ---- the pacer (port of build_rows / Cursor.next_day) -------------- */
  // Completed (frozen) days are replayed as-is; only the days after them are
  // re-paced at `budget`, starting no earlier than the picked start date.
  function buildSchedule(budget, from) {
    const slack = Math.round(budget * 0.08);
    const rows = [];
    const seen = new Set();
    const pStart = pickedStart();
    const lastFz = frozen.length ? frozen[frozen.length - 1].iso : '';
    let i = 0, off = 0, dayNo = 0, fk = 0;

    for (let di = from; di < DAYS.length; di++) {
      const day = DAYS[di];
      const iso = day[0], ri = day[2];
      if (fk < frozen.length && frozen[fk].iso === iso) {   // a completed day: keep it
        const items = frozen[fk++].items.map(x => ({ p: x[0], a: x[1], b: x[2] }));
        const effort = items.reduce((s, it) => s + sliceSum(PWORD[it.p], it.a - 1, it.b) * PERAKIM[it.p][3], 0);
        const last = items[items.length - 1];
        i = last.p; off = last.b;
        if (off >= PWORD[i].length) { i++; off = 0; }
        rows.push(makeLearnRow(items, effort, day, ++dayNo, seen, true));
        continue;
      }
      if (iso <= lastFz || ri !== 0) {
        if (ri !== 0 && (iso <= lastFz || iso >= pStart))   // yom tov / erev yom tov / tisha b'av
          rows.push({ type: 'skip', iso: iso, heb: day[1], reason: REASONS[ri] });
        continue;
      }
      if (iso < pStart) continue;
      if (i >= PERAKIM.length) break;

      /* consume ~budget weighted words */
      const items = [];
      let effort = 0;
      while (i < PERAKIM.length && items.length < MAX_PERAKIM) {
        const p = PERAKIM[i], w = PWORD[i], wgt = p[3];
        const room = budget - effort;
        const rest = sliceSum(w, off, w.length) * wgt;
        if (room > 0 && rest <= room + slack) {         // finish the perek today
          items.push({ p: i, a: off + 1, b: w.length });
          effort += rest; i++; off = 0; continue;
        }
        if (room <= 0) break;
        let k = 0, eff = 0;
        while (off + k < w.length && eff + w[off + k] * wgt <= room) { eff += w[off + k] * wgt; k++; }
        if (k === 0) {
          if (items.length) break;
          k = 1; eff = w[off] * wgt;                   // one halacha alone exceeds the budget
        } else {
          const taken = sliceSum(w, off, off + k);
          if (taken < MIN_SPLIT && off === 0 && off + k < w.length && items.length) break;
        }
        items.push({ p: i, a: off + 1, b: off + k });
        effort += eff; off += k;
        if (off >= w.length) { i++; off = 0; }
      }
      if (!items.length) break;
      rows.push(makeLearnRow(items, effort, day, ++dayNo, seen, false));
    }
    return { rows: rows, complete: i >= PERAKIM.length, budget: budget, start: pStart };
  }

  /* ---- effort distribution (histogram + normal fit) ----------------- */
  function renderDist(learnRows, budget) {
    const vals = learnRows.map(r => r.effort);
    const n = vals.length;
    if (n < 2) { chartEl.innerHTML = ''; stats2El.textContent = ''; return; }

    const mean = vals.reduce((a, b) => a + b, 0) / n;
    const sorted = vals.slice().sort((a, b) => a - b);
    const med = (n % 2) ? sorted[(n - 1) / 2]
                        : (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
    const q = frac => sorted[Math.round(frac * (n - 1))];
    // A handful of days are one enormous halacha (the nusach in Seder Tefillot,
    // Tefillin 8, …) and a few are the tail end of a split perek. Frame the
    // chart on the 2nd–98th percentile so the bulk is legible; anything
    // outside collects in the olive end bins.
    let lo = Math.max(1, Math.min(q(0.02), budget * 0.8, med * 0.9));
    let hi = Math.max(q(0.98), budget * 1.15, med * 1.1);
    if (hi - lo < 1) hi = lo + 1;
    const outside = vals.filter(v => v < lo || v > hi).length;
    const bins = 32, bw = (hi - lo) / bins;
    const counts = new Array(bins).fill(0);
    for (const v of vals) {
      let k = Math.floor((v - lo) / bw);
      if (k < 0) k = 0;
      if (k >= bins) k = bins - 1;
      counts[k]++;
    }
    // Gaussian-kernel smoothing (bandwidth = ~one bin): the curve follows
    // whatever shape the bulk has rather than assuming normality
    const hStep = bw * 1.1;
    const K = u => Math.exp(-(u * u) / 2) / Math.sqrt(2 * Math.PI);
    const src = vals.filter(v => v >= lo - 3 * hStep && v <= hi + 3 * hStep);
    const fit = [];
    for (let i = 0; i <= 140; i++) {
      const x = lo + (hi - lo) * i / 140;
      let s = 0;
      for (const v of src) s += K((x - v) / hStep);
      fit.push([x, s * (bw / hStep)]);
    }
    const yMax = Math.max(Math.max.apply(null, counts),
                         Math.max.apply(null, fit.map(p => p[1]))) * 1.15 || 1;

    const W = 920, H = 210, L = 46, R = 14, T = 12, B = 26;
    const pw = W - L - R, ph = H - T - B;
    const xp = v => L + (v - lo) / (hi - lo) * pw;
    const yp = c => T + ph - Math.min(c / yMax, 1.05) * ph;
    const f = x => (Math.round(x * 10) / 10);

    let axis = '';
    for (let i = 0; i <= 6; i++) {
      const v = lo + (hi - lo) * i / 6;
      axis += '<line x1="' + f(xp(v)) + '" y1="' + (H - B) + '" x2="' + f(xp(v)) + '" y2="' + (H - B + 4) +
              '" stroke="#999"/>' +
              '<text x="' + f(xp(v)) + '" y="' + (H - B + 15) + '" font-size="10" fill="#666" ' +
              'text-anchor="middle">' + Math.round(v) + '</text>';
    }
    for (let i = 0; i <= 2; i++) {
      const c = yMax * i / 2, y = f(yp(c));
      axis += '<line x1="' + L + '" y1="' + y + '" x2="' + (W - R) + '" y2="' + y + '" stroke="#e3e7ea"/>' +
              '<text x="' + (L - 6) + '" y="' + (y + 3) + '" font-size="10" fill="#666" ' +
              'text-anchor="end">' + Math.round(c) + '</text>';
    }
    const baseline = '<line x1="' + L + '" y1="' + (H - B) + '" x2="' + (W - R) + '" y2="' + (H - B) +
                     '" stroke="#999"/>';

    const last = bins - 1;
    const bars = counts.map((c, i) => {
      if (!c) return '';
      const x0 = xp(lo + i * bw), x1 = xp(lo + (i + 1) * bw);
      const y = yp(c), h = Math.max(1, (H - B) - y);
      const edge = (i === 0 || i === last);
      const label = i === 0
        ? 'effort \u2264 ' + Math.round(lo + bw) + ': ' + c.toLocaleString() + ' days'
        : i === last
          ? 'effort \u2265 ' + Math.round(lo + i * bw) + ': ' + c.toLocaleString() + ' days'
          : 'effort ' + Math.round(lo + i * bw) + '\u2013' + Math.round(lo + (i + 1) * bw) +
            ': ' + c.toLocaleString() + ' days';
      return '<rect data-c="' + c + '" x="' + f(x0) + '" y="' + f(y) + '" width="' +
             f(Math.max(0.6, x1 - x0 - 1)) +
             '" height="' + f(h) + '" fill="' + (edge ? '#c8b06a' : '#7fa8d0') +
             '" fill-opacity="0.85">' + '<title>' + label + '</title></rect>';
    }).join('');

    const curve = '<path d="M' + fit.map(p => f(xp(p[0])) + ' ' + f(yp(p[1]))).join(' L') +
                  '" fill="none" stroke="#b0402f" stroke-width="1.6"/>';
    const marks =
      '<line x1="' + f(xp(mean)) + '" y1="' + T + '" x2="' + f(xp(mean)) + '" y2="' + (H - B) +
      '" stroke="#1f4e79" stroke-width="1.2"/>' +
      '<text x="' + f(xp(mean) + 4) + '" y="' + (T + 10) + '" font-size="10" fill="#1f4e79">mean ' +
      Math.round(mean) + '</text>' +
      '<line x1="' + f(xp(budget)) + '" y1="' + T + '" x2="' + f(xp(budget)) + '" y2="' + (H - B) +
      '" stroke="#b0402f" stroke-width="1.2" stroke-dasharray="4 3"/>' +
      '<text x="' + f(xp(budget) + 4) + '" y="' + (T + 22) + '" font-size="10" fill="#b0402f">budget ' +
      Math.round(budget) + '</text>' +
      '<text x="' + L + '" y="' + (T + 10) + '" font-size="10" fill="#777">days per bin</text>' +
      (outside ? '<text x="' + (W - R) + '" y="' + (T + 10) + '" font-size="10" fill="#8a742c" ' +
                  'text-anchor="end">olive bins collect ' + outside.toLocaleString() +
                  ' days outside ' + Math.round(lo) + '\u2013' + Math.round(hi) +
                  ' (true range ' + Math.round(sorted[0]) + '\u2013' +
                  Math.round(sorted[n - 1]).toLocaleString() + ')</text>' : '');

    chartEl.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
      'aria-label="Histogram of per-day effort with a fitted normal curve">' +
      baseline + axis + bars + curve + marks + '</svg>';

    const within = vals.filter(v => Math.abs(v - budget) <= 0.1 * budget).length / n;
    const over = vals.filter(v => v > budget * 1.08).length;
    const veryShort = vals.filter(v => v < budget * 0.5).length;
    stats2El.textContent =
      n.toLocaleString() + ' days \u00b7 mean ' + Math.round(mean) +
      ' (' + Math.round(mean / budget * 100) + '% of budget) \u00b7 median ' + Math.round(med) +
      ' \u00b7 IQR ' + Math.round(q(0.25)) + '\u2013' + Math.round(q(0.75)) +
      ' \u00b7 ' + Math.round(within * 100) + '% within \u00b110% \u00b7 ' +
      over.toLocaleString() + ' overshoot days' +
      (veryShort ? ' \u00b7 ' + veryShort.toLocaleString() + ' very short days' : '') +
      ' \u00b7 range ' + Math.round(sorted[0]) + '\u2013' + Math.round(sorted[n - 1]).toLocaleString();
  }

  /* ---- rendering ---------------------------------------------------- */
  function renderRow(r) {
    if (r.type === 'skip') {
      return '<tr class="skip" data-type="skip">' +
        '<td></td><td></td><td data-v="' + r.iso + '">' + r.iso + '</td><td>' + wdOf(r.iso) + '</td>' +
        '<td dir="rtl" lang="he">' + esc(r.heb) + '</td>' +
        '<td></td><td></td><td></td><td></td><td></td><td></td><td></td>' +
        '<td class="note">No learning \u2014 ' + esc(r.reason) + '</td></tr>';
    }
    const cls = (r.done ? 'done' : '') + (r.newSefer ? ' transition' : '');
    return '<tr' + (cls ? ' class="' + cls.trim() + '"' : '') + ' data-type="learn">' +
      '<td class="chk" data-v="' + (r.done ? 1 : 0) + '"><input type="checkbox" data-day="' + r.dayNo + '"' +
        (r.done ? ' checked' : '') + ' aria-label="Mark day ' + r.dayNo + ' completed"></td>' +
      '<td data-v="' + r.dayNo + '">' + r.dayNo + '</td>' +
      '<td data-v="' + r.iso + '">' + r.iso + '</td>' +
      '<td>' + wdOf(r.iso) + '</td>' +
      '<td dir="rtl" lang="he">' + esc(r.heb) + '</td>' +
      '<td>' + esc(r.sefer) + '</td>' +
      '<td>' + esc(r.hil) + '<br><span class="he" dir="rtl" lang="he">' + esc(r.hilHe) + '</span></td>' +
      '<td class="wcell" data-v="' + r.weightAvg.toFixed(3) + '">' +
        r.items.map(it => itemWeight(it.p)).join('<br>') + '</td>' +
      '<td class="learning">' +
        r.items.map(it => '<a class="rf" href="' + esc(sefariaUrl(it)) + '" data-day="' + r.dayNo +
          '" title="Show the Hebrew text for this day">' +
          esc(itemLabel(PERAKIM[it.p], PWORD[it.p], it.a, it.b)) + '</a>').join('<br>') +
      '</td>' +
      '<td data-v="' + r.halachot + '">' + r.halachot + '</td>' +
      '<td data-v="' + r.words + '">' + r.words.toLocaleString() + '</td>' +
      '<td data-v="' + rint(r.effort) + '">' + rint(r.effort) + '</td>' +
      '<td class="note">' + (r.newSefer ? esc(r.newSefer) : '') + '</td></tr>';
  }

  function updateStats(res) {
    const learn = res.rows.filter(r => r.type === 'learn');
    const n = learn.length;
    const nDone = learn.filter(r => r.done).length;
    let txt = n.toLocaleString() + ' learning days' + (nDone ? ' (' + nDone + ' completed)' : '');
    if (n && res.complete) {
      const a = Date.parse(learn[0].iso + 'T00:00:00Z');
      const b = Date.parse(learn[n - 1].iso + 'T00:00:00Z');
      txt += ' \u00b7 start ' + prettyDate(learn[0].iso) +
             ' \u00b7 finish ' + prettyDate(learn[n - 1].iso) +
             ' (' + ((b - a) / 31557600000).toFixed(1) + ' years)';
      statsEl.classList.remove('warn');
    } else {
      txt += ' \u00b7 still short of the finish when this file\u2019s calendar ends ' +
             prettyDate(DAYS[DAYS.length - 1][0]) + ' (choose an earlier start or '
             + 'effort \u2265 @@MIN_EFFORT@@/day)';
      statsEl.classList.add('warn');
    }
    statsEl.textContent = txt + ' \u00b7 ~' + res.budget + ' effort/day';
  }

  function rebuild() {
    const budget = parseFloat(effortNum.value) || DEFAULT_EFFORT;
    const from = Math.min(startIndex(frozen.length ? frozen[0].iso : pickedStart()), DAYS.length - 1);
    const res = buildSchedule(budget, from);
    current = res.rows;
    tbody.innerHTML = current.map(renderRow).join('');
    domRows = Array.from(tbody.rows);
    if (sortCol >= 0) sortRows(sortCol);
    apply();
    updateStats(res);
    renderDist(res.rows.filter(r => r.type === 'learn' && !r.done), budget);
    const nDone = frozen.length;
    progressEl.textContent = nDone ? '\u2713 ' + nDone + ' day' + (nDone === 1 ? '' : 's') + ' completed' : '';
    clearBtn.hidden = !nDone;
  }

  // tick day N: it and every day before it become completed (their halachot are
  // kept); untick day N: it and every later day go back to being re-paced
  function setDoneThrough(dayNo, done) {
    const keep = current.filter(r => r.type === 'learn' && (done ? r.dayNo <= dayNo : r.dayNo < dayNo));
    frozen = keep.map(r => ({ iso: r.iso, items: r.items.map(it => [it.p, it.a, it.b]) }));
    saveFrozen();
    rebuild();
  }

  /* ---- filtering + sorting ------------------------------------------ */
  function apply() {
    const query = q.value.trim().toLowerCase();
    const sefer = seferSel.value;
    const type = typeSel.value;
    let shown = 0;
    for (const tr of domRows) {
      let ok = true;
      if (hideSkips.checked && tr.dataset.type === 'skip') ok = false;
      if (ok && type && tr.dataset.type !== type) ok = false;
      if (ok && sefer && !(tr.cells[5].textContent.split(', ').includes(sefer))) ok = false;
      if (ok && query && !(tr.textContent.toLowerCase().includes(query))) ok = false;
      tr.style.display = ok ? '' : 'none';
      if (ok) shown++;
    }
    countEl.textContent = shown + ' / ' + domRows.length + ' rows';
  }

  function sortRows(col) {
    const type = ths[col].dataset.type;
    domRows.sort((a, b) => {
      const ca = a.cells[col], cb = b.cells[col];
      let va = ca.dataset.v !== undefined ? ca.dataset.v : ca.textContent.trim();
      let vb = cb.dataset.v !== undefined ? cb.dataset.v : cb.textContent.trim();
      if (type === 'num') { va = parseFloat(va) || 0; vb = parseFloat(vb) || 0;
                            return (va - vb) * sortDir; }
      return va.localeCompare(vb) * sortDir;
    });
    const frag = document.createDocumentFragment();
    domRows.forEach(r => frag.appendChild(r));
    tbody.appendChild(frag);
    ths.forEach((th, i) => {
      const old = th.querySelector('.arrow');
      if (old) old.remove();
      if (i === col) {
        const s = document.createElement('span');
        s.className = 'arrow';
        s.textContent = sortDir === 1 ? ' \u25b2' : ' \u25bc';
        th.appendChild(s);
      }
    });
  }

  /* ---- ICS export of the current pace -------------------------------- */
  function icsEscape(s) {
    return s.replace(/\\/g, '\\\\').replace(/;/g, '\\;').replace(/,/g, '\\,').replace(/\n/g, '\\n');
  }
  function fold(line) {
    const enc = new TextEncoder();
    const byteLen = s => enc.encode(s).length;
    const out = []; let cur = line;
    while (byteLen(cur) > 74) {
      let n = 74;
      while (byteLen(cur.slice(0, n)) > 74) n--;
      const code = cur.charCodeAt(n - 1);   // don't split surrogate pairs
      if (code >= 0xD800 && code <= 0xDBFF) n--;
      out.push(cur.slice(0, n));
      cur = ' ' + cur.slice(n);
    }
    out.push(cur);
    return out.join('\r\n');
  }
  function currentEvents() {
    return current.filter(r => r.type === 'learn' && !r.done).map(r => {
      const parts = r.items.map(it => {
        const p = PERAKIM[it.p], w = PWORD[it.p];
        if (it.a === 1 && it.b === w.length) return HILCHOT[p[0]][1] + ' ' + p[1];
        const u0 = unit(p, it.a), u1 = unit(p, it.b);
        const hasParts = !p[5] ? false : (u0.part || u1.part);
        if (!hasParts) {
          return HILCHOT[p[0]][1] + ' ' + p[1] +
                 (u0.halacha === u1.halacha ? ' (' + u0.halacha + ')'
                                            : ' (' + u0.halacha + '\u2013' + u1.halacha + ')');
        }
        const pr = (u1.part && u1.part !== u0.part) ? u0.part + '\u2013' + u1.part : String(u0.part);
        return HILCHOT[p[0]][1] + ' ' + p[1] + ' (' + u0.halacha + '.' + pr + ')';
      });
      const summary = 'Rambam #' + r.dayNo + ': ' + parts.join('; ');
      const desc = [
        r.items.map(it => itemLabel(PERAKIM[it.p], PWORD[it.p], it.a, it.b)).join('\n'),
        '', r.heb, r.sefer,
        r.halachot + ' halachot, ' + r.words + ' words, effort ' + rint(r.effort)
      ].join('\n');
      const d0 = r.iso.replace(/-/g, '');
      return [d0, nextDay(r.iso).replace(/-/g, ''), r.dayNo, summary, desc];
    });
  }
  function exportIcs() {
    const stamp = new Date().toISOString().replace(/[-:]/g, '').split('.')[0] + 'Z';
    const lines = ['BEGIN:VCALENDAR', 'VERSION:2.0',
                   'PRODID:-//rambam-schedule//Rambam Yad//EN',
                   'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
                   'X-WR-CALNAME:Rambam Yad', 'X-WR-TIMEZONE:Europe/London'];
    for (const [d0, d1, n, summary, desc] of currentEvents()) {
      lines.push('BEGIN:VEVENT',
                 'UID:rambam-' + n + '-' + d0 + '@rambam-schedule',
                 'DTSTAMP:' + stamp,
                 'DTSTART;VALUE=DATE:' + d0,
                 'DTEND;VALUE=DATE:' + d1,
                 'SUMMARY:' + icsEscape(summary),
                 'DESCRIPTION:' + icsEscape(desc),
                 'TRANSP:TRANSPARENT',
                 'END:VEVENT');
    }
    lines.push('END:VCALENDAR');
    const blob = new Blob([lines.map(fold).join('\r\n')],
                          { type: 'text/calendar;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'rambam_yad_schedule_' + Math.round(parseFloat(effortNum.value) || DEFAULT_EFFORT) +
                 '_effort.ics';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  /* ---- difficulty weighting (hand vs commentary-density proxies) ----- */
  let wsetId = DEFAULT_SET;
  function applyWeightSet(id) {
    const ws = WSETS.find(x => x.id === id);
    if (!ws) return false;
    wsetId = id;
    PERAKIM.forEach(p => { p[3] = ws.w[p[0]]; });
    wsetSel.value = id;
    return true;
  }
  if (WSETS.length > 1) {
    wsetSel.innerHTML = WSETS.map(w => '<option value="' + esc(w.id) + '">' + esc(w.label) + '</option>').join('');
    document.getElementById('wsetLabel').hidden = false;
    applyWeightSet(DEFAULT_SET);
  }

  /* weight of one perek under the chosen weighting; when that differs from the
     hand weight, the hand weight is shown alongside for comparison */
  function itemWeight(p) {
    const w = PERAKIM[p][3];
    const hand = WSETS.find(x => x.id === 'hand');
    const h = hand ? hand.w[PERAKIM[p][0]] : w;
    return fmtW(w) + (Math.abs(h - w) >= 0.005 ? ' <span class="hw">(hand ' + fmtW(h) + ')</span>' : '');
  }

  /* ---- the actual text: modal (Hebrew from Sefaria) -------------------- */
  const FLAT = /Mitzvot$|Transmission_of_the_Oral_Law$/;   // one-"perek" texts: ref has no perek
  function sefRef(it) {
    const p = PERAKIM[it.p], slug = HSLUG[p[0]];
    const lo = unit(p, it.a).halacha, hi = unit(p, it.b).halacha;
    const span = lo === hi ? String(lo) : lo + '-' + hi;
    const flat = FLAT.test(slug);
    return { ref: slug + '.' + (flat ? '' : p[1] + '.') + span, lo: lo, hi: hi, p: p,
             name: HILCHOT[p[0]][1] + ' ' + (flat ? '' : p[1] + ':') + (lo === hi ? lo : lo + '\u2013' + hi),
             pre: flat ? '' : p[1] + ':' };
  }
  function sefariaUrl(it) { return 'https://www.sefaria.org/' + encodeURI(sefRef(it).ref) + '?lang=he'; }

  /* Local copy first: texts/<slug>.json sits next to index.html and is cached by
     the service worker as it is read (only what you open is stored), so it works
     offline and survives restarts. Sefaria's API
     is only the fallback (e.g. a file:// copy of this page). */
  const txFiles = new Map();
  function textFile(slug) {
    if (!txFiles.has(slug)) {
      try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist(); } catch (e) { /* ignore */ }
      const pr = fetch('../texts/' + slug.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '') + '.json')
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
      pr.catch(() => txFiles.delete(slug));
      txFiles.set(slug, pr);
    }
    return txFiles.get(slug);
  }
  function loadItem(f) {
    const slug = HSLUG[f.p[0]];
    return textFile(slug).then(d => {
      const per = d.t[FLAT.test(slug) ? 0 : f.p[1] - 1];
      const hs = per && per.slice(f.lo - 1, f.hi);
      if (!hs || hs.length !== f.hi - f.lo + 1) throw new Error('bad local text');
      return hs;
    }).catch(() => fetchText(f.ref));
  }

  /* English (Touger, CC-BY-NC) with its footnotes: texts_en/<slug>.json, same layout
     as the Hebrew -> [[text, [notes]] | null per halakha]. Optional: failure just hides it. */
  const enFiles = new Map();
  function enFile(slug) {
    if (!enFiles.has(slug)) {
      const pr = fetch('../texts_en/' + slug.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '') + '.json')
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
      pr.catch(() => enFiles.delete(slug));
      enFiles.set(slug, pr);
    }
    return enFiles.get(slug);
  }
  function loadEn(f) {
    const slug = HSLUG[f.p[0]];
    return enFile(slug).then(d => {
      const per = d.t[FLAT.test(slug) ? 0 : f.p[1] - 1];
      return per ? per.slice(f.lo - 1, f.hi) : [];
    });
  }
  /* Steinsaltz's short Hebrew glosses are fetched live from Sefaria (not bundled: all
     rights reserved) and kept by the service worker for offline re-reading. */
  const stCache = new Map();
  function loadSt(f) {
    const slug = HSLUG[f.p[0]];
    if (FLAT.test(slug)) return Promise.resolve([]);
    const ref = 'Steinsaltz on ' + slug.replace(/_/g, ' ') + ' ' + f.p[1] + ':' + f.lo + (f.hi > f.lo ? '-' + f.hi : '');
    if (!stCache.has(ref)) {
      const pr = fetch('https://www.sefaria.org/api/texts/' + encodeURIComponent(ref) + '?context=0&commentary=0')
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(d => {
          let h = d.he; if (!Array.isArray(h)) return [];
          if (typeof h[0] === 'string') h = [h];
          return h.map(a => (Array.isArray(a) ? a : [a]).map(x => String(x)
            .replace(/<(?!\/?b>)[^>]*>/g, '').replace(/\s+/g, ' ').trim()).filter(Boolean));
        });
      pr.catch(() => stCache.delete(ref));
      stCache.set(ref, pr);
    }
    return stCache.get(ref);
  }
  function stHtml(list) {
    return list.length ? '<div class="sz" dir="rtl" lang="he"><span class="szl">Steinsaltz · word notes</span>' +
      list.map(x => '<p>' + x + '</p>').join('') + '</div>' : '';
  }

  const txCache = new Map();
  function fetchText(ref) {
    if (!txCache.has(ref)) {
      const pr = fetch('https://www.sefaria.org/api/v3/texts/' + encodeURIComponent(ref) +
                       '?version=hebrew&return_format=text_only')
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(d => {
          const v = d.versions && d.versions[0];
          if (!v) throw new Error('no Hebrew text');
          let t = v.text; if (!Array.isArray(t)) t = [t];
          return t.flat(3).map(plain);
        });
      pr.catch(() => txCache.delete(ref));
      txCache.set(ref, pr);
    }
    return txCache.get(ref);
  }
  function plain(h) {
    const d = new DOMParser().parseFromString(String(h), 'text/html');
    d.querySelectorAll('sup, i.footnote').forEach(n => n.remove());
    return d.body.textContent.replace(/\s+/g, ' ').trim();
  }
  function words(t, n, tail) {
    const w = t.split(' ');
    if (w.length <= n) return t;
    return tail ? '\u2026 ' + w.slice(-n).join(' ') : w.slice(0, n).join(' ') + ' \u2026';
  }

  const txm = document.getElementById('txm'), txB = document.getElementById('txB');
  function closeText() { txm.hidden = true; document.body.style.overflow = ''; }
  document.getElementById('txX').addEventListener('click', closeText);
  txm.addEventListener('click', e => { if (e.target === txm) closeText(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !txm.hidden) closeText(); });

  let shownItems = [], shownHs = [];
  function showText(dayNo, k) {
    const r = current.find(x => x.type === 'learn' && x.dayNo === dayNo);
    if (!r) return;
    shownItems = r.items; shownHs = [];
    document.getElementById('txT').innerHTML = 'Day ' + r.dayNo + ' · ' + r.iso + ' · <bdi>' + esc(r.heb) + '</bdi>';
    txB.innerHTML = r.items.map((it, i) => {
      const f = sefRef(it), p = f.p;
      const u0 = unit(p, it.a), u1 = unit(p, it.b);
      const note = (u0.part || u1.part)
        ? '<br>Note: this day covers only part of a long halakha (' +
          esc(spanText(p, it.a, it.b)) + '); the whole halakha is shown below.' : '';
      return '<section class="txit" id="tx' + i + '"><h3>' + (i + 1) + '. ' + esc(f.name) + '</h3>' +
        '<div class="meta" id="txm' + i + '">Starts at ' + esc(f.pre) + f.lo + ' \u00b7 ends at ' + esc(f.pre) + f.hi +
        ' \u00b7 <a href="' + esc(sefariaUrl(it)) + '" target="_blank" rel="noopener">open in Sefaria \u2197</a>' + note +
        '</div><div id="txt' + i + '">Loading\u2026</div></section>';
    }).join('');
    txm.hidden = false;
    document.body.style.overflow = 'hidden';
    txB.scrollTop = 0;
    r.items.forEach((it, i) => {
      const f = sefRef(it);
      /* Hebrew is required; English is a bonus that must not block it */
      Promise.all([loadItem(f), loadEn(f).catch(() => null)]).then(([hs, en]) => {
        const box = document.getElementById('txt' + i), meta = document.getElementById('txm' + i);
        if (!box) return;
        box.innerHTML = hs.map((t, j) => {
          const e = en && en[j];
          return (j === 0 ? '<div class="mk st">▶ START</div>' : '') +
            '<div class="hb" data-j="' + j + '">' +
            '<div class="hl" dir="rtl" lang="he"><span class="hn">' + esc(f.pre) + (f.lo + j) + '</span>' + esc(t) + '</div>' +
            (e ? '<div class="eng" lang="en">' + enHtml(e[0]) + '</div>' + notesHtml(e[1]) :
                 en ? '' : '<div class="eng"><i>English not available (this text has none, or you are offline and have not opened it before).</i></div>') +
            '<div class="stw"></div></div>' +
            (j === hs.length - 1 ? '<div class="mk en">■ END</div>' : '');
        }).join('');
        meta.insertAdjacentHTML('beforeend',
          '<br>Begins: <span class="snip">' + esc(words(hs[0], 7, false)) + '</span>' +
          '<br>Ends: <span class="snip">' + esc(words(hs[hs.length - 1], 7, true)) + '</span>');
        if (k === i) document.getElementById('tx' + i).scrollIntoView();
        shownHs[i] = hs;
        if (!txB.classList.contains('noSt')) addSt(i);
      }).catch(err => {
        const box = document.getElementById('txt' + i);
        if (box) box.innerHTML = '<div class="txerr">Could not load the text (' + esc(err.message) +
          ') — you need to be online. Use the Sefaria link above.</div>';
      });
    });
  }

  /* English is already escaped by fetch_text_en.py (only <i>/<b>/<br> and {n} markers remain) */
  function enHtml(t) {
    return t.replace(/\{(\d+)\}/g, '<button class="fn" data-n="$1" aria-expanded="false" aria-label="Note $1">$1</button>');
  }
  function notesHtml(notes) {
    return notes.length ? '<div class="fns">' + notes.map((n, i) =>
      '<div class="fnote" data-n="' + (i + 1) + '" hidden><b>' + (i + 1) + '</b>' + n + '</div>').join('') + '</div>' : '';
  }
  /* Hebrew letters only, nikud/cantillation/punctuation dropped, with a map back to the original offsets */
  function normHe(t) {
    let n = '', map = [];
    for (let k = 0; k < t.length; k++) {
      const c = t[k];
      if (c >= 'א' && c <= 'ת') { n += c; map.push(k); }
      else if (/[\s־]/.test(c) && n && n[n.length - 1] !== ' ') { n += ' '; map.push(k); }
    }
    return { n: n, map: map };
  }
  function glossParts(g) {                 // '<b>phrase</b>. explanation' -> [phrase, explanation]
    const m = /^\s*<b>([\s\S]*?)<\/b>\s*[.:]?\s*([\s\S]*)$/.exec(g);
    return m ? [m[1].replace(/<[^>]*>/g, ''), m[2]] : null;
  }
  function inlineGloss(hb, text, label, list) {
    const hl = hb.querySelector('.hl'), N = normHe(text), found = [], rest = [];
    let from = 0;
    list.forEach(g => {
      const gp = glossParts(g);
      if (!gp) { rest.push(g); return; }
      const w = normHe(gp[0].split(/וכו['׳]|וגו['׳]|\.\.\.|…/)[0]).n.trim().split(' ').filter(Boolean);
      let at = -1, len = 0;
      for (let c = w.length; c >= Math.min(2, w.length) && at < 0; c--) {
        const q = w.slice(0, c).join(' ');
        if (q.length < 3) break;
        at = N.n.indexOf(q, from);
        if (at < 0) at = N.n.indexOf(q);
        if (at >= 0 && found.some(x => at < x.e && at + q.length > x.s)) at = -1;   // overlaps an earlier one
        if (at >= 0) len = q.length;
      }
      if (at < 0) { rest.push(g); return; }
      found.push({ s: at, e: at + len, g: gp });
      from = at + len;
    });
    if (!found.length) return rest;
    found.sort((a, b) => a.s - b.s);
    let out = '', pos = 0;
    found.forEach((x, k) => {
      const a = N.map[x.s], b = N.map[x.e - 1] + 1;
      out += esc(text.slice(pos, a)) + '<span class="gl" data-k="' + k + '" tabindex="0" role="button">' + esc(text.slice(a, b)) + '</span>';
      pos = b;
    });
    out += esc(text.slice(pos));
    hl.innerHTML = '<span class="hn">' + esc(label) + '</span>' + out;
    hb.querySelectorAll('.glnote').forEach(n => n.remove());
    hb.querySelector('.hl').insertAdjacentHTML('afterend', '<div class="glnotes">' + found.map((x, k) =>
      '<div class="glnote sz" data-k="' + k + '" dir="rtl" lang="he" hidden><b>' + x.g[0].replace(/<[^>]*>/g, '') + '</b>. ' + x.g[1] + '</div>').join('') + '</div>');
    return rest;
  }
  function addSt(i) {
    const sec = document.getElementById('tx' + i), f = sefRef(shownItems[i]);
    loadSt(f).then(per => {
      if (!sec.isConnected) return;
      sec.querySelectorAll('.hb').forEach((hb, j) => {
        /* glosses whose phrase is found in the halakha are marked in the Hebrew itself;
           the rest stay in a list underneath */
        const rest = inlineGloss(hb, shownHs[i][j], f.pre + (f.lo + j), per[j] || []);
        hb.querySelector('.stw').innerHTML = stHtml(rest);
      });
    }).catch(() => {
      const w = sec.querySelector('.stw');
      if (w && !w.innerHTML) w.innerHTML = '<div class="sz"><span class="szl">Steinsaltz notes need a connection the first time you open them.</span></div>';
    });
  }

  /* reading prefs: which layers are shown; remembered across visits */
  const VIEW_KEY = 'rambam-yad-view-v1';
  const view = { He: true, En: true, Notes: false, St: false };
  try { Object.assign(view, JSON.parse(localStorage.getItem(VIEW_KEY) || '{}')); } catch (e) { /* ignore */ }
  function applyView() {
    txB.classList.toggle('noHe', !view.He);
    txB.classList.toggle('noEn', !view.En);
    txB.classList.toggle('allNotes', view.Notes);
    txB.classList.toggle('noSt', !view.St);
    txB.classList.toggle('sbs', view.He && view.En);
    document.querySelectorAll('#txbar button').forEach(b => b.setAttribute('aria-pressed', view[b.dataset.v] ? 'true' : 'false'));
  }
  document.getElementById('txbar').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    const v = b.dataset.v; view[v] = !view[v];
    if (!view.He && !view.En) view[v === 'He' ? 'En' : 'He'] = true;   // never show nothing
    try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch (err) { /* ignore */ }
    applyView();
    if (v === 'St' && view.St) shownItems.forEach((it, i) => {
      if (shownHs[i] && !txB.querySelector('#tx' + i + ' .stw:not(:empty)') && !txB.querySelector('#tx' + i + ' .gl')) addSt(i);
    });
  });
  /* tap a highlighted Hebrew phrase to open/close its Steinsaltz note */
  function glToggle(g) {
    const n = g.closest('.hb').querySelector('.glnote[data-k="' + g.dataset.k + '"]');
    if (!n) return;
    n.hidden = !n.hidden;
    g.classList.toggle('open', !n.hidden);
  }
  txB.addEventListener('keydown', e => {
    const g = e.target.closest && e.target.closest('.gl');
    if (g && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); glToggle(g); }
  });
  /* tap a note number to open/close just that note */
  txB.addEventListener('click', e => {
    const g = e.target.closest('.gl'); if (g) { glToggle(g); return; }
    const b = e.target.closest('.fn'); if (!b) return;
    const note = b.closest('.hb').querySelector('.fnote[data-n="' + b.dataset.n + '"]');
    if (!note) return;
    note.hidden = !note.hidden;
    b.setAttribute('aria-expanded', note.hidden ? 'false' : 'true');
  });
  applyView();

  /* ---- wiring ------------------------------------------------------- */
  /* last effort / start the user chose, restored on reload */
  const PREFS_KEY = 'rambam-yad-prefs-v1';
  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({ effort: effortNum.value, start: startDate.value, wset: wsetId }));
    } catch (e) { /* ignore */ }
  }
  function loadPrefs() {
    try {
      const d = JSON.parse(localStorage.getItem(PREFS_KEY) || 'null');
      if (!d) return;
      const e = parseFloat(d.effort);
      if (e >= 50 && e <= 20000) effortNum.value = e;
      if (/^\d{4}-\d{2}-\d{2}$/.test(d.start || '')) startDate.value = d.start;
      if (d.wset && WSETS.length > 1) applyWeightSet(d.wset);
      updateBudgetLabel();
    } catch (e) { /* ignore */ }
  }
  let pending = null;
  function scheduleRebuild(delay) {
    updateBudgetLabel();
    savePrefs();
    clearTimeout(pending);
    pending = setTimeout(rebuild, typeof delay === 'number' ? delay : 90);
  }
  function updateBudgetLabel() {
    const v = parseFloat(effortNum.value);
    if (v >= +effortRange.min && v <= +effortRange.max) effortRange.value = v;
  }

  effortRange.addEventListener('input', () => { effortNum.value = effortRange.value; scheduleRebuild(); });
  /* typing: wait until the user pauses (a rebuild takes long enough to make
     each keystroke freeze the page), and ignore half-typed values such as the
     "1" of "1000"; leaving the box or pressing Enter applies it at once */
  effortNum.addEventListener('input', () => {
    if (!(parseFloat(effortNum.value) >= 50)) { clearTimeout(pending); return; }
    scheduleRebuild(700);
  });
  effortNum.addEventListener('change', () => scheduleRebuild(0));
  effortNum.addEventListener('keydown', e => { if (e.key === 'Enter') scheduleRebuild(0); });
  wsetSel.addEventListener('change', () => { applyWeightSet(wsetSel.value); scheduleRebuild(); });
  startDate.addEventListener('input', () => scheduleRebuild());
  startDate.addEventListener('change', () => scheduleRebuild());
  ths.forEach((th, i) => th.addEventListener('click', () => {
    if (sortCol === i) sortDir = -sortDir; else { sortCol = i; sortDir = 1; }
    sortRows(i);
  }));
  q.addEventListener('input', apply);
  seferSel.addEventListener('change', apply);
  typeSel.addEventListener('change', apply);
  hideSkips.addEventListener('change', apply);
  resetBtn.addEventListener('click', () => {
    q.value = ''; seferSel.value = ''; typeSel.value = ''; hideSkips.checked = false;
    effortNum.value = DEFAULT_EFFORT; effortRange.value = DEFAULT_EFFORT;
    startDate.value = DEFAULT_START;
    if (WSETS.length > 1) applyWeightSet(DEFAULT_SET);
    savePrefs();
    sortCol = -1; sortDir = 1;
    ths.forEach(th => { const a = th.querySelector('.arrow'); if (a) a.remove(); });
    rebuild();
  });
  document.getElementById('ics').addEventListener('click', exportIcs);
  tbody.addEventListener('click', e => {
    const a = e.target.closest && e.target.closest('a.rf');
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    e.preventDefault();
    const links = Array.from(a.closest('td').querySelectorAll('a.rf'));
    showText(+a.dataset.day, links.indexOf(a));
  });
  tbody.addEventListener('change', e => {
    const cb = e.target;
    if (cb.matches && cb.matches('input[data-day]')) setDoneThrough(+cb.dataset.day, cb.checked);
  });
  clearBtn.addEventListener('click', () => {
    if (!confirm('Forget all ' + frozen.length + ' completed days? The schedule will be re-paced from the start.')) return;
    frozen = [];
    saveFrozen();
    rebuild();
  });
  document.getElementById('syncBtn').addEventListener('click', syncSetup);
  document.getElementById('restoreBtn').addEventListener('click', syncRestore);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) syncNow(); });
  distToggle.addEventListener('click', () => {
    const collapsed = distPanel.classList.toggle('collapsed');
    distToggle.textContent = collapsed ? 'Show' : 'Hide';
  });

  /* ---- jump to today ------------------------------------------------- */
  function todayIso() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }
  function scrollToToday(smooth) {
    const t = todayIso();
    let best = null, last = null;
    for (const tr of domRows) {
      if (tr.dataset.type !== 'learn' || tr.style.display === 'none') continue;
      last = tr;
      if (!best && tr.cells[2].dataset.v >= t && !tr.classList.contains('done')) best = tr;
    }
    const target = best || last;
    if (target) target.scrollIntoView({ block: 'center', behavior: smooth ? 'smooth' : 'auto' });
  }
  document.getElementById('todayBtn').addEventListener('click', () => scrollToToday(true));

  /* open the page with #debug to see viewport/overflow diagnostics */
  if (location.hash === '#debug') {
    const box = document.createElement('pre');
    box.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:99;margin:0;padding:6px;font:11px monospace;' +
      'background:rgba(0,0,0,.85);color:#9f9;white-space:pre-wrap;max-height:45vh;overflow:auto';
    const report = () => {
      const W = document.documentElement.clientWidth, bad = [];
      document.querySelectorAll('body *').forEach(el => {
        const b = el.getBoundingClientRect();
        if (b.width && b.right > W + 0.5 && getComputedStyle(el).display !== 'none')
          bad.push(el.tagName + '#' + el.id + '.' + (el.className.baseVal === undefined ? el.className : '') + ' right=' + Math.round(b.right));
      });
      box.textContent = 'ui=@@UI_VERSION@@ inner=' + innerWidth + ' client=' + W +
        ' scrollW=' + document.documentElement.scrollWidth + ' scale=' + (visualViewport ? visualViewport.scale : '?') +
        ' dpr=' + devicePixelRatio + ' coarse=' + matchMedia('(pointer: coarse)').matches +
        '\noverflowing: ' + (bad.length ? '\n' + bad.slice(0, 10).join('\n') : 'none');
    };
    document.body.appendChild(box); report(); setInterval(report, 1500);
  }

  document.getElementById('moreBtn').addEventListener('click', e => {
    const open = e.currentTarget.closest('.controls').classList.toggle('show-more');
    e.currentTarget.setAttribute('aria-expanded', open);
  });
  /* phones: start with the histogram folded away so the schedule is what you see */
  if (window.matchMedia && matchMedia('(max-width: 760px), (pointer: coarse) and (max-width: 1024px)').matches) {
    distPanel.classList.add('collapsed'); distToggle.textContent = 'Show';
  }

  loadPrefs();
  rebuild();
  syncNow();
  /* launched from the home screen: open on today's learning like a native app */
  if ((window.matchMedia && matchMedia('(display-mode: standalone)').matches) || navigator.standalone) {
    requestAnimationFrame(() => scrollToToday(false));
  }
})();
</script>
<script>
/* PWA: offline + installable (only when served over http(s); the service worker
   and manifest live one level up, next to index.html) */
if ('serviceWorker' in navigator && /^https?:$/.test(location.protocol)) {
  var hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.addEventListener('controllerchange', function () {
    if (hadController) location.reload();   // a new version just took over
  });
  navigator.serviceWorker.register('../sw.js').catch(function () {});
}
</script>
</body>
</html>"""

    min_start = days[0][0]
    sefer_opts = "".join(f'<option value="{s[0]}">{s[0]}</option>' for s in sefarim)
    doc = (doc
           .replace("@@DATA@@", data)
           .replace("@@START_ISO@@", params["start"].isoformat())
           .replace("@@MIN_START@@", min_start)
           .replace("@@MAX_START@@", max_start)
           .replace("@@LOC@@", loc)
           .replace("@@SEFER_OPTS@@", sefer_opts)
           .replace("@@TOTAL_HAL@@", f"{total_hal:,}")
           .replace("@@TOTAL_WORDS@@", f"{total_words:,}")
           .replace("@@MIN_EFFORT@@", str(int(min_effort)))   # also used in updateStats()
           .replace("@@EFFORT_MAX@@", str(effort_max))
           .replace("@@DEFAULT@@", f"{budget:g}")
           .replace("@@UI_VERSION@@", "phone-5")
           .replace("@@MIN_SPLIT@@", str(params["min_split_words"]))
           .replace("@@MAX_PERAKIM@@", str(params["max_perakim"])))

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)
    return horizon, min_start, max_start
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Generate a Rambam Yad learning schedule")
    ap.add_argument("--start", default="2026-10-12", help="Start date YYYY-MM-DD")
    ap.add_argument("--budget", type=float, default=None,
                    help="Target effort per learning day (weighted words). "
                         "Default: auto = raw words / 995 (matches old 15-halachot/day pace)")
    ap.add_argument("--slack", type=float, default=None,
                    help="May overshoot budget by this much to finish a perek. Default: 8%% of budget")
    ap.add_argument("--min-split", type=int, default=40,
                    help="Don't START a new perek with fewer words than this; end the day short instead")
    ap.add_argument("--max-perakim", type=int, default=5,
                    help="Cap on how many perakim may be combined on one day")
    ap.add_argument("--weights", default=WEIGHTS_FILE,
                    help="CSV with slug,weight columns (default: data/weights.csv)")
    ap.add_argument("--no-weights", action="store_true",
                    help="Ignore weights file (all hilchot weight 1.0)")
    ap.add_argument("--proxy", action="store_true",
                    help="Blend hand weights with a commentary-density difficulty proxy "
                         "(data/commentary.json; run fetch_commentary.py first)")
    ap.add_argument("--proxy-blend", type=float, default=0.5,
                    help="Blend exponent: composite = hand^(1-a) * proxy^a (default 0.5)")
    ap.add_argument("--proxy-basket", choices=["all", "core", "core-only"], default="all",
                    help="Commentators the --proxy uses: 'all' = the 39-commentator "
                         "basket, 'core' = the four with near-complete coverage "
                         "(Kessef Mishneh, Kiryat Sefer, Ohr Sameach, Mishneh LaMelech); "
                         "'core-only' = core with the hand weights ignored "
                         "(hilchot lacking data still use their hand weight)")
    ap.add_argument("--coverage-min", type=float, default=0.25,
                    help="Min fraction of attempted commentary nodes that must return "
                         "usable text -- a fetch data-quality gate, not a difficulty "
                         "signal (default 0.25)")
    ap.add_argument("--proxy-min-commentators", type=int, default=3,
                    help="Min distinct commentators on a hilchot to trust the proxy; "
                         "with fewer, the density estimate is noise, so use the hand "
                         "weight (default 3)")
    ap.add_argument("--no-proxy-effort-neutral", action="store_true",
                    help="Let --proxy change overall pace; default keeps total "
                         "weighted words = hand-only (proxy only reshuffles)")
    ap.add_argument("--commentary", default=os.path.join(HERE, "data", "commentary.json"),
                    help="Commentary-density data from fetch_commentary.py "
                         "(default: data/commentary.json)")
    ap.add_argument("--splits", default=SPLITS_FILE,
                    help="JSON break points for oversized single halachot "
                         "(default: data/halacha_splits.json)")
    ap.add_argument("--no-splits", action="store_true",
                    help="Ignore the splits file: never break inside a halacha")
    ap.add_argument("--min-effort", type=float, default=MIN_EFFORT,
                    help=f"Slowest pace offered by the HTML effort control "
                         f"(default {MIN_EFFORT}; lowers the pace bound and "
                         f"enlarges the calendar window it embeds)")
    ap.add_argument("--window-start", default=None,
                    help="Earliest date offered by the HTML start-date control "
                         "YYYY-MM-DD (default: today)")
    ap.add_argument("--latest-start", default=None,
                    help="Latest date offered by the HTML start-date control "
                         "YYYY-MM-DD (default: as late as the slowest pace "
                         "allows; bigger windows make a bigger HTML file)")
    ap.add_argument("--israel", action="store_true", help="Use Israel calendar (default: diaspora)")
    ap.add_argument("--outdir", default=os.path.join(HERE, "output"))
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-xlsx", action="store_true")
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("--no-ics", action="store_true")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start)
    window_start = (dt.date.fromisoformat(args.window_start) if args.window_start
                    else min(dt.date.today(), start))
    latest_start = (dt.date.fromisoformat(args.latest_start) if args.latest_start
                    else start)
    diaspora = not args.israel

    weights = None if args.no_weights else load_weights(args.weights)
    splits = None if args.no_splits else load_splits(args.splits)
    proxy_rows = None
    hand_weights = weights
    weight_names = {}
    if weights is not None and os.path.exists(args.weights):
        with open(args.weights, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                weight_names[row["slug"]] = {
                    "sefer_en": row.get("sefer_en", ""),
                    "hilchot_en": row.get("hilchot_en", ""),
                }
    data_dir = os.path.dirname(os.path.abspath(args.weights))

    def proxy_set(basket, alpha=None):
        return compute_proxy_weights(
            args.commentary, hand_weights,
            args.proxy_blend if alpha is None else alpha, args.coverage_min,
            min_commentators=args.proxy_min_commentators,
            effort_neutral=not args.no_proxy_effort_neutral, basket=basket)

    # Every weighting the HTML can switch between (hand is always first).
    weight_sets, default_set = [], "hand"
    if hand_weights is not None:
        weight_sets.append({"id": "hand", "label": "Hand weights",
                            "w": {k: v[0] for k, v in hand_weights.items()}})
        if os.path.exists(args.commentary):
            for sid, label, basket, alpha, csv_name in (
                    ("all", "Hand × commentary (39 commentators)", None, None,
                     "proxy_weights.csv"),
                    ("core", "Hand × core commentary (4 commentators)", CORE_BASKET, None,
                     "proxy_weights_core.csv"),
                    ("core-only", "Core commentary only (ignores hand weights)",
                     CORE_BASKET, 1.0, "proxy_weights_core_only.csv")):
                w_set, rows = proxy_set(basket, alpha)
                if not rows:
                    continue
                write_proxy_csv(rows, os.path.join(data_dir, csv_name), weight_names)
                weight_sets.append({"id": sid, "label": label,
                                    "w": {k: v[0] for k, v in w_set.items()},
                                    "n_proxy": sum(1 for r in rows if r["source"] == "proxy")})
                if args.proxy and args.proxy_basket == sid:
                    weights, proxy_rows = w_set, rows
                    default_set = sid
                    n_proxy = weight_sets[-1]["n_proxy"]
                    ncm = sorted(r['commentators'] for r in rows if r['source'] == 'proxy')
                    print(f"Proxy blended (basket={sid}, alpha={args.proxy_blend if alpha is None else alpha}, "
                          f"commentators>={args.proxy_min_commentators}, "
                          f"coverage>={args.coverage_min}); wrote "
                          f"{os.path.join(data_dir, csv_name)} "
                          f"({n_proxy} hilchot use proxy, {len(rows) - n_proxy} hand-only"
                          + (f"; commentators median {ncm[len(ncm) // 2]}, "
                             f"range {ncm[0]}-{ncm[-1]}" if ncm else "") + ")")
    elif args.proxy:
        print("WARNING: --proxy given but --no-weights set; ignoring proxy.")
    if splits:
        specs = [spec for part_splits in splits.values()
                 for h in part_splits.values() for spec in h.values()]
        print(f"Halacha splits: {len(specs)} oversized halachot pre-split into "
              f"{sum(len(s['parts']) for s in specs)} parts"
              f" ({', '.join(sorted(set(s['strategy'] for s in specs)))})")
    entries = load_entries(weights=weights, splits=splits)
    total_hal = sum(e["n_halachot"] for e in entries)
    total_words = sum(sum(e["halacha_words"]) for e in entries)
    total_effort = sum(sum(e["halacha_words"]) * e["weight"] for e in entries)
    print(f"Loaded {len(entries)} perakim, {total_hal} halachot, "
          f"{total_words} words, {total_effort:.0f} weighted words")
    if weights:
        print(f"Weights applied for {sum(1 for e in entries if e['weight'] != 1.0)} "
              f"perakim across {len(set(e['hil_en'] for e in entries if e['weight'] != 1.0))} hilchot")

    budget = args.budget if args.budget else round(total_words / BASELINE_DAYS)
    slack = args.slack if args.slack else round(budget * 0.08)
    src = "explicit" if args.budget else f"raw-word pace of {BASELINE_DAYS} days"
    print(f"Budget: {budget} effort/day ({src}), slack {slack}")

    params = {"start": start, "budget": budget, "slack": slack,
              "max_perakim": args.max_perakim, "min_split_words": args.min_split,
              "diaspora": diaspora, "min_effort": args.min_effort,
              "window_start": window_start, "latest_start": latest_start,
              "weight_sets": weight_sets, "default_set": default_set,
              "proxy": args.proxy and weights is not None,
              "proxy_blend": args.proxy_blend,
              "proxy_min_commentators": args.proxy_min_commentators,
              "coverage_min": args.coverage_min}

    rows, completion = build_rows(entries, start, budget, slack,
                                  args.max_perakim, args.min_split, diaspora)
    learn = [x for x in rows if x["kind"] == "learn"]
    skips = [x for x in rows if x["kind"] == "skip"]
    print(f"Learning days: {len(learn)} | special skipped days shown: {len(skips)}")
    if completion:
        print(f"Completion: {completion:%A, %d %B %Y}")
    else:
        print("WARNING: schedule did not complete within 40 years")

    os.makedirs(args.outdir, exist_ok=True)
    stem = "rambam_schedule" + (f"_b{args.budget:g}" if args.budget else "")
    if not args.no_xlsx:
        xp = os.path.join(args.outdir, stem + ".xlsx")
        write_xlsx(rows, completion, params, xp)
        print(f"Wrote {xp}")
    if not args.no_pdf:
        pp = os.path.join(args.outdir, stem + ".pdf")
        write_pdf(rows, completion, params, pp)
        print(f"Wrote {pp}")
    if not args.no_html:
        hp = os.path.join(args.outdir, stem + ".html")
        horizon, min_start, max_start = write_html(entries, params, hp)
        print(f"Wrote {hp}  (effort \u2265 {args.min_effort:g}/day, "
              f"start {min_start}\u2013{max_start}, "
              f"calendar window {horizon} days)")
    if not args.no_ics:
        ip = os.path.join(args.outdir, stem + ".ics")
        write_ics(rows, ip)
        print(f"Wrote {ip}")


if __name__ == "__main__":
    main()
