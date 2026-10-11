# Rambam Yad Learning Schedule

Generates a personalised Mishneh Torah learning schedule using a
**difficulty-weighted word budget**: each day consumes roughly the same
*effort* — words × difficulty weight of the hilchot — so easy sections go
faster (more perakim/day) and hard sections go slower (partial perakim).

Skips Shabbos, Erev Shabbos, Yom Tov, Erev Yom Tov and Tisha B'Av.

**Live copy:** <https://shmuli9.github.io/rambam-schedule/> — the interactive
schedules served straight from this repo by GitHub Pages.

## Your current schedule

| Setting | Value |
|---|---|
| Start | Monday, 12 October 2026 (א׳ מרחשוון תשפ״ז) |
| Calendar | Diaspora (UK) |
| Budget | ~788 effort/day (auto = raw-word pace of the old 15-halachot/day model), slack 8%, max 5 perakim/day, min split 40 words |
| Coverage | Introduction (Hakdamah) + 14 sefarim, 88 texts, 1,022 perakim/units, 15,896 segments, 784,312 words (Sefaria data) |
| Learning days | 1,253 |
| Completion | Sunday, 9 November 2031 |

The cycle opens with the Rambam's **Hakdamah** (12 learning days, 12–27 Oct 2026):
Transmission of the Oral Law (weight 0.75), the Positive/Negative Mitzvot
lists and the Overview of the Yad's structure (weight 0.5 — read-through;
their units are labelled "sections"/"mitzvot" rather than halachot).

### Difficulty weights

`data/weights.csv` holds a draft weight + rationale for each of the 88
texts — **edit it to taste and re-run** (opens directly in Excel):

| Weight | Meaning | Examples |
|---|---|---|
| 0.5 | read-through | Seder Tefillot (nusach) |
| 0.75 | easy | De'ot, Talmud Torah, Megillah, Melachim |
| 1.0 | standard | Teshuvah, Tefillah, Korbanot procedure |
| 1.25 | hard | Yesodei HaTorah, Shevuot, Sanhedrin, Sales |
| 1.5 | very hard | Shabbat, Eruvin, Kiddush HaChodesh, Ishut, Gerushin, Terumot, Tum'at Met, Tzara'at, Kelim, Nizkei Mamon, Malveh vLoveh |

Effect in practice (single-hilchot days): Shabbat ≈ 9 halachot/day vs
Melachim ≈ 16 halachot/day vs Seder Tefillot ≈ 15+ — same *time*, different
pace. The average draft weight ≈ 1.21, which stretches the cycle from ~4.0
to ~5.1 years; raise `--budget` or trim weights if that's too slow.

### Display conventions

- Every row shows its **Sefer**; the first day of a new sefer is highlighted
  light blue with "→ New sefer: …" in the notes.
- Yom Tov / Erev Yom Tov / Tisha B'Av rows appear **greyed out with a reason**.
- Routine Fridays and Shabbosim are omitted entirely.
- Learning happens on Chol HaMoed, Chanukah, Purim, minor fasts, Rosh Chodesh.
- In the diaspora, 20 Nisan (eve of the final Pesach days) is OFF as Erev Yom Tov.

## Files

- `fetch_data.py` — downloads Rambam structure, per-perek halacha counts and
  per-halacha word counts from Sefaria into `data/rambam.json` (cached).
- `plan_splits.py` — derives `data/halacha_splits.json`: break points inside
  oversized single halachot.
- `generate_schedule.py` — builds the schedule; writes XLSX + PDF + HTML + ICS.
- `data/weights.csv` — per-hilchot difficulty weights (**user-editable**).
- `data/halacha_splits.json` — break points inside the handful of oversized
  single halachot that would otherwise swallow a whole day (see below).
- `data/rambam.json` — cached Sefaria data.
- `index.html` — landing page (the GitHub Pages root) linking both interactive
  schedules and their XLSX/PDF/ICS downloads.
- `output/rambam_schedule.xlsx` — Schedule sheet (one row per day) + Summary.
- `output/rambam_schedule.pdf` — landscape PDF with Hebrew.
- `output/rambam_schedule.html` — self-contained interactive table. Its
  headline feature is the pair of controls at the top: a **Start** date picker
  and an **"Effort/day"** slider (or type either value). Changing either one
  re-paces the *entire* cycle in the browser — day numbers, dates, Hebrew
  dates, completion date, the Effort column and the .ics export all update
  instantly, so you can try any start + pace combination without re-running
  anything. Also: click any header to sort (numeric-aware), search box,
  sefer/type filters, "hide skipped days" toggle, **Weight column**, print
  stylesheet (A4 landscape via browser print) and an **"Export .ics" button**
  for the schedule currently on screen. No dependencies — opens in any browser.
- `output/rambam_schedule.ics` — static snapshot at the `--budget` pace:
  1,253 all-day events ("Rambam #N: …"), importable into Apple Calendar,
  Gmail, Outlook, etc. Skipped days are not included. (For another pace, use
  the HTML's Export .ics button.)

### Splitting an oversized halacha

A halacha is normally indivisible, which makes for the occasional monster day:
**Tefillin, Mezuzah and the Torah Scroll 8:4** is the Rambam's list of every
parsha break in the Torah — 2,903 words, i.e. 3,629 effort, 4.6× a normal day.
Sefaria's own text has section headings inside it, so `data/halacha_splits.json`
records those break points and the schedule treats each as its own unit:

| Part | Words | What it is |
|---|---|---|
| 1 | 369 | intro + Sefer Bereishit |
| 2 | 657 | Sefer Shemot |
| 3 | 382 | Sefer Vayikra |
| 4 | 507 | Sefer Bamidbar |
| 5 | 523 | Sefer Devarim |
| 6 | 183 | layout of Shirat Ha'azinu |
| 7 | 282 | Shirat HaYam, Shirat "Az Yashir" layout + closing rules |

The runner-up, **Sanhedrin 19:4** (944 words; the list of 168 lavim), has no
headings but is an enumerated list, so it is split into four even parts at item
boundaries (items 1–49, 50–84, 85–127, 128–168). Together this brings the
heaviest single unit from 3,629 effort down to 861 (Kiddush HaChodesh 12:2),
and no day is now more than ~10% over budget. Days show it as e.g.
`(halakha 4, part 2 of 7 (Sefer Shemot), 657 words)`.

`plan_splits.py` derives the file (it re-reads both texts from Sefaria and
rewrites it, verifying that every halacha's parts add up); `python plan_splits.py
--scan` lists the heaviest halachot still unsplit. Switch the feature off with
`--no-splits`.

## Usage

```bash
# regenerate with defaults
python generate_schedule.py

# tune the pace (effort = weighted words per day)
python generate_schedule.py --budget 900        # faster finish
python generate_schedule.py --budget 650        # slower
python generate_schedule.py --no-weights        # pure word-count pacing
python generate_schedule.py --min-split 60      # fewer small perek fragments

# the HTML lets you try every pace live (250+ effort/day out of the box);
# lower the floor if you want to explore slower cycles there too
python generate_schedule.py --min-effort 150
python generate_schedule.py --start 2026-10-12 --israel --outdir output --no-pdf --no-html --no-ics
```

### Parameters

| Flag | Default | Meaning |
|---|---|---|
| `--start` | 2026-10-12 | First day of the schedule |
| `--budget` | auto (≈771) | Effort per learning day = weighted words |
| `--slack` | 8% of budget | Overshoot allowed to finish a perek instead of splitting |
| `--min-split` | 40 words | Never *start* a new perek with fewer words than this; end the day short instead |
| `--max-perakim` | 5 | Cap on perakim combined into one day |
| `--weights` | data/weights.csv | Difficulty weights file |
| `--splits` | data/halacha_splits.json | Break points inside oversized halachot |
| `--no-splits` | off | Never break inside a halacha |
| `--no-weights` | off | Treat every hilchot as weight 1.0 |
| `--israel` | off | Israel calendar instead of diaspora |
| `--min-effort` | 250 | Slowest pace offered by the HTML effort control; also sizes the Hebrew-calendar window the HTML embeds (lower values ⇒ bigger HTML file, slower generation) |
| `--window-start` | today | Earliest date offered by the HTML start control |
| `--latest-start` | auto | Latest date offered by the HTML start control; the window must still fit the slowest pace from there, so pushing it out enlarges the HTML file |

## How the pacing works

All 15,091 halachot form one sequence (Yesodei HaTorah … Melachim). Each
halacha's effort = its word count × the weight of its hilchot. Each learning
day takes halachot until the budget is reached:

- Easy/short perakim are **combined** on one day.
- Long/hard perakim are **split at halacha boundaries chosen by word count**,
  so consecutive days get roughly equal amounts of text.
- A single halacha bigger than the budget (e.g. the full nusach in Seder
  Tefillot, the long writing-rules halacha in Tefillin 8) gets its own day —
  halachot are never split.
- `slack` lets a day overshoot slightly to finish a perek; `min-split`
  prevents ending a day with a 1–2 halacha fragment of a new perek.

## Dependencies

```bash
pip install hdate tzdata openpyxl reportlab python-bidi pypdf
```

PDF Hebrew rendering uses `C:\Windows\Fonts\arial.ttf` (falls back to DejaVu).

## Notes

- The Rambam's commentaries are not part of the cycle; the Hakdamah
  (Introduction) **is** included and comes first.
- Word counts are whitespace-token counts of Sefaria's Hebrew text — a proxy,
  not a precise measure.
- The HTML carries the perek/word/weight data and enough Hebrew calendar to
  re-pace itself, which is why it behaves exactly like the generated
  XLSX/PDF (same effort numbers, same rounding) at every pace.
- In the HTML you can move both ends: **Start** has a range of dates it can
  serve (the XLSX/PDF/ICS files stay at `--start`), and **Effort/day** works
  down to `--min-effort`. Pick a start date outside the range or an effort
  below the floor and the page tells you instead of quietly truncating.
- Refresh Sefaria data: delete `data/rambam.json` and re-run `python fetch_data.py`.
