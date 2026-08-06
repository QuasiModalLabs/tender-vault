# The profile

`vault/profiles/my-company.md` is the one file everything filters and scores
against. **The key-by-key specification lives in that file's own comments** —
open it and read them. Every key is annotated in place with what it does, why
the current value was chosen, and what was deliberately left out.

This document covers what the profile can't say about itself: the mechanics
behind the keys, how to tune them, and what changes downstream when you edit
one.

---

## What each edit costs you

The frontmatter is parsed by `parse_profile()` in `scripts/ingest.py`, which
every consumer shares. Which script reads a key determines whether editing it
needs a rebuild:

| Key | Read by | Effect | After editing |
|---|---|---|---|
| `unspsc_families`, `competencies`, `exclude`, `min_days_until_close` | `ingest.py` | Which notices enter the corpus at all | Re-run `ingest.py` (~2 min) |
| `value_min` / `value_max` | `ingest.py` | Nothing, unless run with `--extract-values` | Re-run `ingest.py --extract-values` |
| `plan_themes` | `plans_ingest.py` | How departmental-plan prose is ranked | Re-run `plans_ingest.py` (~1 min) |
| `oag_themes` | `oag_ingest.py` | How audits are ranked for IT relevance | Re-run `oag_ingest.py` (~1 min) |
| `contracts_categories`, `contracts_window_years` | `contracts_ingest.py` | Which contracts are persisted | Re-run `contracts_ingest.py` (~630MB, 5–10 min) |
| `expiry_min_value` | `scripts/tender_tools.py`, at query time | Value floor on `expiring-contracts` and the dossier expiry timeline | **Nothing — takes effect on the next query** |
| Prose below the frontmatter | Claude, at conversation start | Judgement: capabilities, constraints, what not to bid | **Nothing — takes effect immediately** |

Two of those are worth internalizing. `expiry_min_value` is the only
frontmatter key read at query time rather than ingest, so it's free to tune.
And the prose body is never parsed by any script — it's read by Claude per
`vault/CLAUDE.md`, which makes it the cheapest and most underused part of the
file. "We can't compete on anything requiring Secret clearance as a prime" does
more work than any filter key.

Re-running `contracts_ingest.py` is cheap **within 24 hours**: the ~630MB source
CSV is cached at `.cache/contracts.csv` and reused while it's under a day old,
so iterating on `contracts_categories` in one sitting costs minutes rather than
a fresh download. Past 24 hours — or if you delete the cache to reclaim the
space — the next run fetches it again. Tune that key in one session if you can.

---

## Two-pole theme scoring

`plan_themes` and `oag_themes` don't contain keywords. They contain **example
sentences**, and the embedding model generalizes from them to phrasings you
never listed. This is the part of the profile most likely to be edited wrongly,
because it doesn't behave like a filter.

### How the score is computed

Each theme's example sentences are embedded and **averaged into a single
vector** (`theme_vector()`, `scripts/plans_ingest.py:128`). A record's score is
the cosine similarity to the positive pole minus its similarity to the negative
pole:

```
score = similarity(text, POSITIVE_pole) − similarity(text, NEGATIVE_pole)
```

Subtracting the second pole is what makes this work. A single positive theme
would rank any prose that sounds vaguely governmental; the negative pole is
what pushes routine boilerplate down rather than merely leaving it unranked.

Three pairs are scored, across two ingests:

| Ingest | Text scored | Positive pole | Negative pole | Output |
|---|---|---|---|---|
| `plans_ingest.py` | `planning_explanation` (forward-looking) | `modernization_intent` | `routine_noise` | `intent_score` |
| `plans_ingest.py` | `variance_explanation` (retrospective) | `operational_pressure` | `accounting_noise` | `pressure_score` |
| `oag_ingest.py` | audit title + description | `it_audit` | `non_it_audit` | IT-relevance score |

The two plans pairs answer different questions and are not interchangeable:
`intent_score` is *what a department says it plans to do*, `pressure_score` is
*what strained last year*. The README's caveats about `planning_explanation`
apply to the first — many organizations file none at all, so a low
`intent_score` frequently means an absent field, not an absence of intent.

All scoring runs locally with `all-MiniLM-L6-v2` via sentence-transformers. No
API key, no model tokens, no network beyond the source data.

### Tuning

Both ingests accept `--show-extremes`, which prints the top and bottom scored
records:

```bash
python scripts/plans_ingest.py --show-extremes
python scripts/oag_ingest.py --show-extremes
```

Read the top of the list. If the highest-scoring records aren't the ones you'd
have picked by hand, the poles need work — not the threshold.

- **Top looks like noise?** Your negative pole is too narrow. Add examples that
  sound like the noise you're actually seeing.
- **Something obviously relevant scored low?** Add a positive example phrased
  the way *that record* is phrased, not the way you'd describe it.
- **Write sentences, not terms.** `"investing to modernize and replace an aging
  legacy IT system"` carries far more signal than `"modernization"`. The
  averaging step means a bag of single words produces a mushy centroid.
- **Keep the poles disjoint.** An example that could plausibly sit in either
  pole drags both vectors toward each other and flattens the spread.

If you define no themes at all, both scripts fall back to built-in defaults
(`scripts/plans_ingest.py:66`, `scripts/oag_ingest.py:647`) so a fresh clone
runs out of the box. The shipped profile overrides them.

**Re-running an ingest under edited themes is a re-scoring, not a refresh.** The
same source data produces different rankings for different firms. This is the
intended behaviour, and it's why these databases aren't committed.

---

## Choosing UNSPSC families

`unspsc_families` is the primary relevance filter — the publisher's own
commodity classification, which beats guessing a procurement officer's
vocabulary. It matches **by prefix**, so `'8111'` catches every `8111xxxx`
code.

The shipped list mixes prefix lengths on purpose. Three are four-digit L3
families; `'80101507'` is a single eight-digit L4 code, included because it's a
pocket of real IT work inside an L3 that is otherwise noise. Taking the whole
L3 would drag in nineteen unrelated notices. The profile's comments explain
each inclusion and each deliberate exclusion.

### Finding families for your own business

```bash
python scripts/unspsc_discover.py                     # families in the live feed
python scripts/unspsc_discover.py --segment 43 81 80  # drill into segments
python scripts/unspsc_discover.py --level L3          # roll up at L3
python scripts/unspsc_discover.py --grep cloud        # match on description
```

The script **downloads PSPC's reference file itself** on first run, to
`.cache/unspsc_reference.csv` — nothing to fetch by hand. Add `--force` to
re-download. The source is PSPC's GSIN/NIBS-to-UNSPSC linkage file, under the
Open Government Licence:

```
https://donnees-data.tpsgc-pwgsc.gc.ca/ba2/aev-bas/nibsunspsc-gsinunspsc.csv
```

This script is not part of the ingest path and nothing imports it. You run it,
read the table, and paste a hand-checked list into the profile. That separation
is deliberate: which families describe your competencies is a judgement about
your business, and it belongs in committed config where it shows up in a diff.

**Only the UNSPSC side of that file is read.** The GSIN columns are dropped on
load and never joined on. PSPC's own caveat is that the linkages were assessed
at higher levels and carried through indiscriminately — telecom cable laying
and highway paving both map to GSIN 5153, "Foundation work, including pile
driving". Rolling a tender up through GSIN would put paving in your cloud
results.

### The keyword fallback

Three source systems (MX, PW and SSC) file no UNSPSC codes at all, and one of
them is Shared Services Canada, the largest federal IT buyer. Those notices
fall back to `competencies`, matched on whole words — so `aws` matches Amazon
Web Services but not `flaws`.

Tune it against the live feed, not against intuition. The ingest funnel prints
the UNSPSC-versus-keyword split on every run, and the shipped profile records
per-term hit counts in its comments — including the terms that match **zero**
notices (`AWS`, `Azure`, `DevOps`, `cybersecurity`, `data engineering`). The
government writes *informatics*, *TBIPS*, *information technology*. Dead
vocabulary is kept in the list deliberately, annotated, so nobody re-adds it
assuming it was an oversight.

---

## Adapting the profile to a different company

1. Rewrite the prose body first. It's free, it needs no rebuild, and it does
   more than any single key.
2. Replace `competencies` and `exclude` with your vocabulary, then run
   `ingest.py` and read the funnel counts.
3. Run `unspsc_discover.py`, hand-check the table, paste in `unspsc_families`.
4. Copy terms from the profile's commented reference catalog into
   `contracts_categories` — it lists the contracts dataset's real category
   vocabulary by sector, so this is a copy-paste rather than a research task.
5. Edit the theme examples last, and verify each with `--show-extremes`.
