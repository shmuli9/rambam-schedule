# Data sources & fetch status

Read this before fetching anything for the commentary-density proxy: the data
below is already in the repo, so there is rarely a reason to re-fetch.
Last verified: 2026-10-18.

## Sefaria  -- COMPLETE (the sole usable source)

| File | Contents | Status |
|---|---|---|
| `commentary.json` | Word counts for a basket of 39 Rambam meforshim x 84 hilchot, via Sefaria links + v3 texts API. `discovery` = which commentators point at each hilchot; `hilchot[*].meforshim[<author>]` = `{words, present, node}`. | **Complete.** Every discovered (hilchot, commentator) node was fetched (`present == attempted` for all 84). Fetched 2026-10-12. |
| `sefaria_toc.json`, `rambam.json` | Sefaria index / Rambam structure and per-halacha word counts. | Complete. |

Known gaps in the Sefaria data (they are gaps in Sefaria, not unfinished fetches):
- *The Order of Prayer* and 4 non-hilchot entries (Positive/Negative Mitzvot,
  Overview, Transmission of the Oral Law) have **no** commentary -> proxy falls
  back to the hand weight.
- Maggid Mishneh is on only 26 of 84 hilchot; Lechem Mishneh 55; Hasagot
  HaRa'avad 63; Kessef Mishneh 82.
- Re-run `python fetch_commentary.py` (cached, resumable) only to pick up newly
  added Sefaria texts; `--rediscover` re-runs discovery without re-fetching.

## Hebrew Wikisource  -- INVENTORY ONLY, text NOT fetched (deliberately)

`ws_pages.json` (produced by `ws_discover.py`, resumable; `--report` prints it)
lists every page that exists under each hub with its wikitext byte length, plus
`groups` = pages per hilchot name (Wikisource spelling, not Sefaria's).

| Hub | Pages | Hilchot covered | Adds anything over Sefaria? |
|---|---|---|---|
| כסף משנה | 108 | ~all | No (Sefaria has 82/84) |
| מגיד משנה | 127 | 20 | No -- all 20 are already in Sefaria's 26 |
| לחם משנה | 26 | 7 | Only Foundations of the Torah |
| משנה למלך | 40 | 8 | Only Foundations of the Torah, Human Dispositions |
| אור שמח / ראב"ד / הגהות מיימוניות / ר' חיים הלוי | 1-8 each | 1-2 | No (stubs, ~0-15 KB) |

Conclusion: not worth a text scraper -- 3 extra (commentator, hilchot) pairs, and
Wikisource counts are bytes of markup, not comparable to Sefaria word counts.
**If continuing:** the page titles in `ws_pages.json` are all you need to
fetch text (`action=query&prop=revisions&rvprop=content`, batch via POST, back
off on HTTP 429). Hilchot names differ between sites (מלוה ולוה / מלווה ולווה,
ביאת מקדש / ביאת המקדש, זכיה / זכייה) -- fuzzy-match, don't build titles.

## HebrewBooks  -- NOT FETCHED, blocked

`hebrewbooks.org` has the best catalogue (it has full Maggid Mishneh) but as of
2026-10-18 every plain HTTP request, `rambam.aspx` included, gets a Cloudflare
"Just a moment..." challenge (HTTP 403). Earlier it answered `&text=1` with
"Invalid Request" (JS overlay). Bypassing the challenge was deliberately not
attempted. **If continuing:** only via a manual export by the user (PDFs of
specific sefarim -> page count or OCR per hilchot), most valuable for Maggid
Mishneh's 58 missing hilchot.

## Not done / ideas
- A "consistent core basket" variant (Kessef Mishneh, Kiryat Sefer, Ohr Sameach,
  Mishneh LaMelech: 67-82 hilchot each) to compare with the 39-author basket.
