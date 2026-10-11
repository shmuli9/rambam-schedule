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


def load_entries(data_file=DATA_FILE, weights=None):
    """Flatten rambam.json into an ordered list of perek entries."""
    with open(data_file, encoding="utf-8") as f:
        data = json.load(f)
    weights = weights or {}
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
            for i, n in enumerate(counts, start=1):
                entries.append({
                    "sefer_en": sefer["name_en"],
                    "sefer_he": sefer["name_he"] or "",
                    "hil_en": hil_en,
                    "hil_he": hil_he,
                    "perek": i,
                    "n_halachot": n,
                    "halacha_words": words[i - 1],
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


def item_label(e, a, b):
    w = item_words(e, a, b)
    unit = e.get("unit", "halachot")
    whole = (a == 1 and b == e["n_halachot"])
    if whole:
        return (f"{e['hil_en']} {e['perek']} "
                f"(whole perek, {e['n_halachot']} {unit}, {w} words)")
    return (f"{e['hil_en']} {e['perek']} "
            f"({unit} {a}\u2013{b} of {e['n_halachot']}, {w} words)")


def item_label_short(e, a, b):
    """Compact label used in the PDF."""
    unit = e.get("unit", "halachot")
    whole = (a == 1 and b == e["n_halachot"])
    if whole:
        return (f"Perek {e['perek']} (whole, {e['n_halachot']} hal., "
                f"{item_words(e, a, b)} w)")
    return (f"Perek {e['perek']}: {unit} {a}\u2013{b} of "
            f"{e['n_halachot']} ({item_words(e, a, b)} w)")


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
                "halachot": sum(b - a + 1 for _, a, b in items),
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
        ("Effort formula", "words \u00d7 hilchot difficulty weight (data/weights.csv)"),
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
        whole = (a == 1 and b == e["n_halachot"])
        parts.append(f"{e['hil_en']} {e['perek']}" + ("" if whole else f" ({a}\u2013{b})"))
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
    #   PERAKIM  [[hilchot_idx, perek, "w1,w2,\u2026", weight]]
    #   DAYS     [[iso, hebrew_date, reason_idx]]   (reason 0 = learning day)
    sefarim, sef_idx = [], {}
    hilchot, hil_idx = [], {}
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
        perakim.append([hil_idx[hkey], e["perek"],
                        ",".join(str(w) for w in e["halacha_words"]),
                        e["weight"]])

    total_hal = sum(e["n_halachot"] for e in entries)
    total_words = sum(sum(e["halacha_words"]) for e in entries)
    total_effort = sum(sum(e["halacha_words"]) * e["weight"] for e in entries)

    # Calendar window: must reach the finish even at the slowest on-screen
    # pace. Estimate it, then verify against the real calendar and grow it
    # until the whole Yad fits.
    min_effort = float(params.get("min_effort") or MIN_EFFORT)
    horizon = max(400, int(total_effort / min_effort / 0.67) + 200)
    for _ in range(6):
        days = _calendar_window(params["start"], horizon, params["diaspora"])
        if _fits(days, entries, min_effort, params["max_perakim"],
                 params["min_split_words"]):
            break
        horizon = int(horizon * 1.3) + 200
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

    budget = float(params["budget"])
    effort_max = int(max(3000, round(budget * 2.5 / 100) * 100))
    start_pretty = f"{params['start']:%d %b %Y}"
    loc = "Diaspora (UK)" if params["diaspora"] else "Israel"

    data = (
        f"const SEFARIM = {dump(sefarim)};\n"
        f"const HILCHOT = {dump(hilchot)};\n"
        f"const PERAKIM = {dump(perakim)};\n"
        f"const REASONS = {dump(reasons)};\n"
        f"const DAYS = {dump(day_rows)};"
    )

    # r-string: backslash escapes belong to JavaScript, not Python
    doc = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rambam Yad — Learning Schedule</title>
<style>
  :root { --grey:#d9d9d9; --blue:#ddebf7; --head:#1f4e79; }
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", Arial, sans-serif; margin: 0; background:#f5f6f8; color:#222; }
  header { background: var(--head); color:#fff; padding: 14px 20px; }
  header h1 { margin:0; font-size: 20px; }
  header .sub { opacity:.85; font-size: 13px; margin-top: 4px; }
  .controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
              padding: 10px 20px; background:#fff; border-bottom:1px solid #ddd;
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
  .wrap { padding: 0 20px 30px; }
  table { border-collapse: collapse; width:100%; background:#fff; font-size: 13px; }
  th, td { border: 1px solid #cfd6dd; padding: 5px 8px; vertical-align: top; text-align: left; }
  thead th { background: var(--head); color:#fff; cursor:pointer; user-select:none;
             white-space:nowrap; position: sticky; top: 52px; }
  thead th .arrow { font-size: 10px; opacity:.8; }
  tbody tr:nth-child(even) td { background:#fafbfc; }
  tr.skip td { background: var(--grey) !important; color:#707070; font-style: italic; }
  tr.transition td { background: var(--blue) !important; }
  td.learning { min-width: 320px; }
  td.note { min-width: 160px; }
  .he { color:#444; font-size: 12px; }
  td[data-v] { font-variant-numeric: tabular-nums; }
  #stats.warn { color:#ffd97a; font-weight:600; opacity:1; }
  @media print {
    @page { size: A4 landscape; margin: 10mm; }
    body { background:#fff; }
    .controls, header .sub button { display: none !important; }
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
  <div class="sub">start @@START@@ · @@TOTAL_HAL@@ halachot / @@TOTAL_WORDS@@ words ·
    @@LOC@@ · <span id="stats"></span></div>
</header>
<div class="controls">
  <label class="effort" title="Effort = words × difficulty weight. Higher = more per day = fewer days.">
    <span>Effort/day</span>
    <input type="range" id="effort" min="@@MIN_EFFORT@@" max="@@EFFORT_MAX@@" step="10" value="@@DEFAULT@@">
    <input type="number" id="effortNum" min="50" max="20000" step="10" value="@@DEFAULT@@">
  </label>
  <input type="search" id="q" placeholder="Search (hilchot, perek, date, reason…)">
  <select id="sefer"><option value="">All sefarim</option>@@SEFER_OPTS@@</select>
  <select id="type">
    <option value="">All rows</option>
    <option value="learn">Learning days only</option>
    <option value="skip">Skipped days only</option>
  </select>
  <label><input type="checkbox" id="hideSkips"> Hide skipped days</label>
  <button id="reset">Reset</button>
  <button id="ics" class="primary" title="Download an .ics file of the current pace — import into Apple Calendar, Gmail, Outlook, etc.">📅 Export .ics</button>
  <span class="count" id="count"></span>
</div>
<div class="wrap">
<table id="sched">
  <thead><tr>
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
<script>
/* ---- embedded data -------------------------------------------------- */
@@DATA@@
const DEFAULT_EFFORT = @@DEFAULT@@;
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
  const statsEl = document.getElementById('stats');
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
  // Python-compatible rounding: round() ties to even, Math.round() ties up.
  function rint(x) {
    const f = Math.floor(x), frac = x - f;
    if (frac > 0.5) return f + 1;
    if (frac < 0.5) return f;
    return (f % 2 === 0) ? f : f + 1;
  }

  function itemLabel(p, w, a, b) {
    const h = HILCHOT[p[0]], unit = h[3], n = w.length;
    const words = sliceSum(w, a - 1, b);
    const whole = (a === 1 && b === n);
    return whole ? h[1] + ' ' + p[1] + ' (whole perek, ' + n + ' ' + unit + ', ' + words + ' words)'
                 : h[1] + ' ' + p[1] + ' (' + unit + ' ' + a + '\u2013' + b +
                   ' of ' + n + ', ' + words + ' words)';
  }

  /* ---- the pacer (port of build_rows / Cursor.next_day) -------------- */
  function buildSchedule(budget) {
    const slack = Math.round(budget * 0.08);
    const rows = [];
    const seen = new Set();
    let i = 0, off = 0, dayNo = 0;

    for (const day of DAYS) {
      const iso = day[0], heb = day[1], ri = day[2];
      if (ri !== 0) {                                  // yom tov / erev yom tov / tisha b'av
        rows.push({ type: 'skip', iso: iso, heb: heb, reason: REASONS[ri] });
        continue;
      }
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

      dayNo++;
      const his = items.map(it => PERAKIM[it.p][0]);
      const seferIdx = uniq(his.map(hi => HILCHOT[hi][0]));
      const newNames = seferIdx.filter(si => !seen.has(si)).map(si => SEFARIM[si][0]);
      seferIdx.forEach(si => seen.add(si));
      const weights = uniq(items.map(it => PERAKIM[it.p][3]));
      rows.push({
        type: 'learn', dayNo: dayNo, iso: iso, heb: heb, items: items,
        sefer: seferIdx.map(si => SEFARIM[si][0]).join(', '),
        hil: uniq(his.map(hi => HILCHOT[hi][1])).join(', '),
        hilHe: uniq(his.map(hi => HILCHOT[hi][2])).join(', '),
        weightStr: weights.map(fmtW).join(' / '),
        weightAvg: weights.reduce((x, y) => x + y, 0) / weights.length,
        newSefer: newNames.length ? '\u2192 New sefer: ' + newNames.join(', ') : '',
        halachot: items.reduce((s, it) => s + it.b - it.a + 1, 0),
        words: items.reduce((s, it) => s + sliceSum(PWORD[it.p], it.a - 1, it.b), 0),
        effort: effort
      });
    }
    return { rows: rows, complete: i >= PERAKIM.length, budget: budget };
  }

  /* ---- rendering ---------------------------------------------------- */
  function renderRow(r) {
    if (r.type === 'skip') {
      return '<tr class="skip" data-type="skip">' +
        '<td></td><td data-v="' + r.iso + '">' + r.iso + '</td><td>' + wdOf(r.iso) + '</td>' +
        '<td dir="rtl" lang="he">' + esc(r.heb) + '</td>' +
        '<td></td><td></td><td></td><td></td><td></td><td></td><td></td>' +
        '<td class="note">No learning \u2014 ' + esc(r.reason) + '</td></tr>';
    }
    return '<tr' + (r.newSefer ? ' class="transition"' : '') + ' data-type="learn">' +
      '<td data-v="' + r.dayNo + '">' + r.dayNo + '</td>' +
      '<td data-v="' + r.iso + '">' + r.iso + '</td>' +
      '<td>' + wdOf(r.iso) + '</td>' +
      '<td dir="rtl" lang="he">' + esc(r.heb) + '</td>' +
      '<td>' + esc(r.sefer) + '</td>' +
      '<td>' + esc(r.hil) + '<br><span class="he" dir="rtl" lang="he">' + esc(r.hilHe) + '</span></td>' +
      '<td data-v="' + r.weightAvg.toFixed(3) + '">' + esc(r.weightStr) + '</td>' +
      '<td class="learning">' +
        r.items.map(it => esc(itemLabel(PERAKIM[it.p], PWORD[it.p], it.a, it.b))).join('<br>') +
      '</td>' +
      '<td data-v="' + r.halachot + '">' + r.halachot + '</td>' +
      '<td data-v="' + r.words + '">' + r.words.toLocaleString() + '</td>' +
      '<td data-v="' + rint(r.effort) + '">' + rint(r.effort) + '</td>' +
      '<td class="note">' + (r.newSefer ? esc(r.newSefer) : '') + '</td></tr>';
  }

  function updateStats(res) {
    const learn = res.rows.filter(r => r.type === 'learn');
    const n = learn.length;
    let txt = n.toLocaleString() + ' learning days';
    if (n && res.complete) {
      const a = Date.parse(learn[0].iso + 'T00:00:00Z');
      const b = Date.parse(learn[n - 1].iso + 'T00:00:00Z');
      txt += ' \u00b7 finish ' + prettyDate(learn[n - 1].iso) +
             ' (' + ((b - a) / 31557600000).toFixed(1) + ' years)';
      statsEl.classList.remove('warn');
    } else {
      txt += ' \u00b7 still short of the finish when this file\u2019s calendar ends ' +
             prettyDate(DAYS[DAYS.length - 1][0]) + ' (needs effort \u2265 @@MIN_EFFORT@@/day)';
      statsEl.classList.add('warn');
    }
    statsEl.textContent = txt + ' \u00b7 ~' + res.budget + ' effort/day';
  }

  function rebuild() {
    const budget = parseFloat(effortNum.value) || DEFAULT_EFFORT;
    const res = buildSchedule(budget);
    current = res.rows;
    tbody.innerHTML = current.map(renderRow).join('');
    domRows = Array.from(tbody.rows);
    if (sortCol >= 0) sortRows(sortCol);
    apply();
    updateStats(res);
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
      if (ok && sefer && !(tr.cells[4].textContent.split(', ').includes(sefer))) ok = false;
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
    return current.filter(r => r.type === 'learn').map(r => {
      const parts = r.items.map(it => {
        const p = PERAKIM[it.p], w = PWORD[it.p];
        return HILCHOT[p[0]][1] + ' ' + p[1] +
               ((it.a === 1 && it.b === w.length) ? '' : ' (' + it.a + '\u2013' + it.b + ')');
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

  /* ---- wiring ------------------------------------------------------- */
  let pending = null;
  function scheduleRebuild() {
    updateBudgetLabel();
    clearTimeout(pending);
    pending = setTimeout(rebuild, 90);
  }
  function updateBudgetLabel() {
    const v = parseFloat(effortNum.value);
    if (v >= +effortRange.min && v <= +effortRange.max) effortRange.value = v;
  }

  effortRange.addEventListener('input', () => { effortNum.value = effortRange.value; scheduleRebuild(); });
  effortNum.addEventListener('input', scheduleRebuild);
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
    sortCol = -1; sortDir = 1;
    ths.forEach(th => { const a = th.querySelector('.arrow'); if (a) a.remove(); });
    rebuild();
  });
  document.getElementById('ics').addEventListener('click', exportIcs);

  rebuild();
})();
</script>
</body>
</html>"""

    sefer_opts = "".join(f'<option value="{s[0]}">{s[0]}</option>' for s in sefarim)
    doc = (doc
           .replace("@@DATA@@", data)
           .replace("@@START@@", start_pretty)
           .replace("@@LOC@@", loc)
           .replace("@@SEFER_OPTS@@", sefer_opts)
           .replace("@@TOTAL_HAL@@", f"{total_hal:,}")
           .replace("@@TOTAL_WORDS@@", f"{total_words:,}")
           .replace("@@MIN_EFFORT@@", str(int(min_effort)))   # also used in updateStats()
           .replace("@@EFFORT_MAX@@", str(effort_max))
           .replace("@@DEFAULT@@", f"{budget:g}")
           .replace("@@MIN_SPLIT@@", str(params["min_split_words"]))
           .replace("@@MAX_PERAKIM@@", str(params["max_perakim"])))

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)
    return horizon
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
    ap.add_argument("--min-effort", type=float, default=MIN_EFFORT,
                    help=f"Slowest pace offered by the HTML effort control "
                         f"(default {MIN_EFFORT}; lowers the pace bound and "
                         f"enlarges the calendar window it embeds)")
    ap.add_argument("--israel", action="store_true", help="Use Israel calendar (default: diaspora)")
    ap.add_argument("--outdir", default=os.path.join(HERE, "output"))
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-xlsx", action="store_true")
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("--no-ics", action="store_true")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start)
    diaspora = not args.israel

    weights = None if args.no_weights else load_weights(args.weights)
    entries = load_entries(weights=weights)
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
              "diaspora": diaspora, "min_effort": args.min_effort}

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
        horizon = write_html(entries, params, hp)
        print(f"Wrote {hp}  (effort slider >= {args.min_effort:g}/day, "
              f"calendar window {horizon} days)")
    if not args.no_ics:
        ip = os.path.join(args.outdir, stem + ".ics")
        write_ics(rows, ip)
        print(f"Wrote {ip}")


if __name__ == "__main__":
    main()
