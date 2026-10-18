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

### Commentary-density difficulty proxy

The hand weights above are a subjective draft. As a cross-check we can use an
*objective* signal: **how much the classical commentaries wrote on each sugya.**
A sugya that drew voluminous debate (Kessef Mishneh, Maggid Mishneh, Hasagot
HaRa'avad, Ohr Sameach, Tzafnat Pa'neach, …) was, in general, a harder / more
contested one — so commentary *volume* is a reasonable proxy for difficulty.

No single Rambam commentary is digitised in full, so we average across a
**basket** of meforshim. Averaging fills the gaps where one commentary is sparse
and cancels out individual-author verbosity.

The basket is **discovered, not hand-written**. An earlier draft guessed twelve
commentator names and concluded that "Maggid Mishneh and Radbaz are absent
entirely" from Sefaria — that was wrong; they are only absent under the naming
convention the guess used. So instead we ask Sefaria's links API which
commentaries actually point at each hilchot, which turns up **49 distinct
commentators**. We keep the 39 that are classical (excluding modern pedagogic
works — Steinsaltz, Reshimot Shiurim, Yitzchak Yeranen — whose volume tracks
exposition rather than difficulty) and digitised on at least 3 hilchot. That
basket recovers Maggid Mishneh (26 hilchot), Hasagot HaRa'avad (63), Mishneh
LaMelech (67), Maaseh Rokeach (69) and the Radbaz (as "Commentary of Mahari
Kurkus and Radbaz", 3) — together **14.9M commentary words against the Rambam's
own 768k**, i.e. ~19 commentary words per word of Rambam.

Two other sources were evaluated and rejected:

- **HebrewBooks** has the best *catalogue* but is not scrapable: it serves
  commentary through a JS overlay, and plain HTTP now gets a Cloudflare
  challenge (HTTP 403).
- **he.wikisource.org** does publish clean wikitext (`ws_discover.py` measures
  it: 8 commentators, כסף משנה nearly complete, מגיד משנה only 19 hilchot), but
  the transcriptions are partial. Sefaria's text is strictly larger on every
  hilchot we compared — Maggid Mishneh on Shabbat is 79,980 words on Sefaria vs
  13,983 on Wikisource, on Gerushin 42,066 vs 3,569 — so Wikisource adds
  nothing and Sefaria is the sole source. What was fetched, what is complete
  and where to continue is recorded in `data/SOURCES.md`.

The proxy for a hilchot is its **commentary density** — total commentary words
divided by the Rambam's own word count (so long Rambam halachot don't look hard
just for being long). Each meforshim is first normalised to its own geometric
mean, so a verbose commentator can't dominate and a missing one merely drops
out; the per-hilchot averages are then normalised to a clean multiplier. That
per-author normalisation is what makes uneven coverage harmless: commentators
don't all write on the same hilchot, so each hilchot is scored only by those who
actually wrote on it, each measured against their own average. A hilchot nobody
commented on scores as *average*, not as easy.

**Core basket.** Because the 39-commentator basket scores each hilchot with a
different set of voices, there is also a *core* basket of the four commentators
who each cover 67-82 hilchot (Kessef Mishneh, Kiryat Sefer, Ohr Sameach, Mishneh
LaMelech), so almost every hilchot is judged by the same four. 80 hilchot have at
least 3 of them (Eruvin, Sheqel Dues and Leprosy defilement fall back to the hand
weight). It agrees only moderately with the full basket (Spearman 0.73) and, like
it, hardly at all with the hand weights. Use `--proxy --proxy-basket core`
(weights in `data/proxy_weights_core.csv`).

**In the HTML.** Both interactive pages have a *Difficulty* selector: hand
weights, hand × commentary (39), hand × core commentary (4), or *core commentary
only* (ignores the hand weights entirely; hilchot with too little commentary data
still use their hand weight, `--proxy-basket core-only`). Each row's Weight column
shows the weight applied to every line of that day, with the hand weight in grey
whenever it differs. Switching it
re-paces the remaining days (completed days are kept) so you can see how the
schedule moves. Total effort is held equal across the options, so the
effort/day scale stays comparable. The choice is remembered in localStorage.

Enable it for the static XLSX/PDF/ICS outputs with `--proxy`:

```bash
python fetch_commentary.py              # one-off; writes data/commentary.json
python analyze_proxy.py                 # inspect correlations & biggest movers
python generate_schedule.py --proxy     # blend hand weights + commentary proxy
```

The blend is geometric: `composite = hand^(1−α) · proxy^α` (α default 0.5,
`--proxy-blend`). A hilchot uses the proxy only if at least
`--proxy-min-commentators` (default 3) distinct commentators wrote on it — with
one or two voices the density estimate is noise — and at least `--coverage-min`
(default 0.25) of the attempted fetches returned usable text, which is a
data-quality check on the fetch rather than a difficulty signal. Hilchot failing
either gate keep their hand weight, so sparse digitisation never drags a sugya
toward "easy". In practice **83 of 84** hilchot qualify (median 13 commentators,
range 5–29); the only holdout is the Order of Prayer, which is liturgy and has
no meforshim at all. The Introduction's four entries are not halachot either,
so 5 of the 88 weighted rows fall back to hand weights.

By default the blend is **effort-neutral** — rescaled so the total weighted
words equal the hand-only total — meaning the proxy *only reshuffles relative
difficulty* and never changes the overall cycle length (which `--budget`
controls); pass `--no-proxy-effort-neutral` to let it set its own pace. Verified:
937,433 weighted words either way, with the cycle moving only 1253 → 1257
learning days (rounding).

Per-hilchot detail (hand weight, commentator count, raw commentary density,
proxy weight, blended composite, and whether the proxy was used) is written to
`data/proxy_weights.csv`.

#### How well does it work?

The proxy is essentially **uncorrelated** with the hand weights (Pearson
r ≈ −0.10, Spearman ≈ −0.10), i.e. it captures a genuinely different dimension
rather than restating the draft. Blending two independent signals is the point,
but it also means the proxy moves things a long way, so it's worth knowing where
it is strong and where it is weak.

We also measured a second candidate signal, **breadth** — how many distinct
commentators wrote on a hilchot. It turns out to be nearly orthogonal to density
(r ≈ +0.11) but weaker and topically confounded: commentators tend to cover
whole *sefarim*, so breadth mostly records who wrote where rather than how hard
the sugya is. It is reported in `proxy_weights.csv` and by `analyze_proxy.py` as
a diagnostic, but deliberately not blended in.

Where the proxy rates a sugya **harder** than the hand draft: Ownerless Property
and Gifts (×2.1), Slaves (×2.1), Festival Offering (×2.1), Woman Suspected of
Infidelity (×2.1), Circumcision (×2.0), Fringes (×1.9), Virgin Maiden (×1.8),
Kings and Wars (×1.8). These are heavily-litigated civil/ritual areas, and the
shift looks defensible.

The systematic **blind spot** is technical or mathematical material, where the
Rambam is already terse and exhaustive and the meforshim have proportionally
less to add: Sanctification of the New Moon (astronomy; proxy ×0.17 against a
hand weight of 1.5), Eruvin (geometry; ×0.25 vs 1.5), Vessels (×0.37 vs 1.5),
Sabbath (×0.43 vs 1.5), Tefillin/Mezuzah (×0.46 vs 1.25). Conversely the
densest commentary per Rambam word is concentrated in Temple service — Rest on
the Tenth of Tishrei (88.8), Things Forbidden on the Altar (85.1), The Chosen
Temple (73.2), Admission into the Sanctuary (63.1) — topics that are obscure
rather than technically hard. α = 0.5 halves both effects, and the hand weights
remain the place to encode "this is mathematically dense".

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
- `fetch_commentary.py` — discovers *every* classical Rambam commentary Sefaria
  links to each hilchot, downloads its Hebrew text and writes
  `data/commentary.json` (cached + resumable); the basis of the optional
  commentary-density difficulty proxy (see above).
- `analyze_proxy.py` — offline analysis of that proxy: correlations against the
  hand weights and against commentator breadth, plus the biggest movers.
- `ws_discover.py` — measures what Hebrew Wikisource holds for these meforshim
  (the evidence behind "Sefaria wins" above); writes `data/ws_pages.json`.
- `plan_splits.py` — derives `data/halacha_splits.json`: break points inside
  oversized single halachot.
- `generate_schedule.py` — builds the schedule; writes XLSX + PDF + HTML + ICS.
- `data/weights.csv` — per-hilchot difficulty weights (**user-editable**).
- `data/commentary.json` — per-hilchot commentary word counts for the whole
  discovered basket (output of `fetch_commentary.py`).
- `data/proxy_weights.csv` — per-hilchot hand / proxy / composite weights, written
  when `generate_schedule.py --proxy` is used.
- `data/halacha_splits.json` — break points inside the handful of oversized
  single halachot that would otherwise swallow a whole day (see below).
- `data/rambam.json` — cached Sefaria data.
- `manifest.webmanifest`, `sw.js`, `icons/`, `make_icons.py` — make the site an
  installable, offline-capable PWA (see below).
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

# blend the hand weights with the commentary-density proxy (see above)
python fetch_commentary.py                 # one-off: build data/commentary.json
python analyze_proxy.py                    # correlations + biggest movers (no network)
python generate_schedule.py --proxy       # weights = hand × commentary proxy
python generate_schedule.py --proxy --proxy-blend 0.35   # hand-weighted, proxy nudges
python generate_schedule.py --proxy --no-proxy-effort-neutral  # let proxy set the pace
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
| `--proxy` | off | Blend hand weights with the commentary-density proxy (data/commentary.json) |
| `--proxy-blend` | 0.5 | Blend exponent α: composite = hand^(1−α) · proxy^α |
| `--proxy-min-commentators` | 3 | Min distinct commentators on a hilchot to trust the proxy; below this, use hand weight |
| `--coverage-min` | 0.25 | Min fraction of attempted commentary fetches returning usable text (fetch data-quality gate) |
| `--commentary` | data/commentary.json | Commentary-density data from `fetch_commentary.py` |
| `--no-proxy-effort-neutral` | off | Let `--proxy` change overall pace (default keeps total effort = hand-only) |
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

## Install as an app (PWA)

Served over HTTPS (the GitHub Pages site) the pages can be installed from the
browser's *Install* / *Add to Home Screen* menu and then work fully offline. The
service worker (`sw.js`) precaches the landing page, both interactive schedules,
the manifest and icons, and serves everything stale-while-revalidate, so a
redeploy appears on the visit after the one that downloads it. Progress, effort
and difficulty choices live in localStorage as before. Opening a page from disk
(`file://`) simply skips the service worker. On phones (width up to 760 px) the table becomes one card per day, the page
itself scrolls, controls stack with 44 px touch targets and 16 px inputs (so iOS
doesn't zoom on focus), the search/filter/export controls fold behind one button,
and the layout respects the notch and home indicator (`viewport-fit=cover`). A
floating **Today** button jumps to today's learning, and the installed app opens
there automatically. Restyle the icon with
`python make_icons.py`; bump `CACHE` in `sw.js` only to purge old files.
