---
# REPRESENTATIVE PROFILE — not a real client. Northwind Digital is a plausible
# mid-sized Canadian IT consulting firm invented to keep this repo
# self-contained and its examples concrete. Every number, capability and
# constraint below is illustrative.
#
# Swap in your own and everything downstream retargets: the tender filter, the
# contracts extract, and the plan/audit scores are all derived from this file.
company_name: Northwind Digital Inc.
team_size: 25
founded: 2015
location: Toronto, ON

# Financial targets — DISABLED, and deliberately left commented rather than
# deleted so nobody tunes a filter that isn't running.
#
# The tender feed has no value field. The only source was a regex over the
# description, and measurement retired it: on the 2026-08-04 feed just 94 of 896
# descriptions contain any dollar figure, and the first one is usually not the
# price. The most common extraction was $10,000,000, off construction source
# lists reading "estimated value of $10 million and below" — a ceiling on a
# qualification vehicle. Median $10M, max $5B, min $0.
#
# Uncommenting these does nothing on its own. The range is only consulted when
# ingest runs with --extract-values, which reads the description with a model
# instead of a regex. See estimate_value in scripts/ingest.py.
#
# THE PROSE GOVERNS. Because this filter is off, the fit range that actually
# affects anything is the one stated under "What we're looking for" below, which
# Claude reads and applies as judgement. These two numbers mirror it so the file
# does not contradict itself — keep them in sync if you change the prose.
# value_min: 500000
# value_max: 3000000

# UNSPSC commodity families — the publisher's own classification, and the
# primary relevance filter. Prefix match, so '8111' catches every 8111xxxx code.
#
# Hand-checked, committed deliberately. Rediscover candidates offline with
#   python scripts/unspsc_discover.py --level L3 --segment 43 81 80
# which reads PSPC's reference file for the hierarchy and CommodityType. That
# file's GSIN side is never joined at runtime and shouldn't be: PSPC's caveat
# says linkages were assessed at higher levels and carried through
# indiscriminately, and it shows — telecom cable laying and highway paving both
# map to GSIN 5153 "Foundation work, including pile driving".
#
# Excluded on purpose after checking the L3 breakdown:
#   8010 management advisory  — 801015 is 19 notices of UN environment
#                               programmes and ATIP advisors
#   8114 manufacturing tech   — 811418 is facilities management
#   8110 prof. engineering    — civil, mechanical, aeronautical
#   4321/4322 computer + network hardware — we don't do hardware supply
unspsc_families:
  - '8111'   # Computer services — software/hardware engineering, sysadmin, MIS
  - '8116'   # IT Service Delivery — incl. 811620 cloud SaaS, 811623 BPaaS
  - '4323'   # Software — industry-specific, content mgmt, data management
  # One deliberate L4. 80101507 is "Information technology consultation
  # services", an island of real IT work inside 801015 "Business and corporate
  # management consultation", which is otherwise UN environment programmes and
  # ATIP advisors. Departments file genuine IT engagements here: the CIPO ITM3
  # modernization RFI and two DND cloud/informatics notices all carry it. Taking
  # the L3 would drag in 19 notices of noise; taking the L4 takes the work.
  - '80101507'

# Competencies — the keyword fallback, NOT the primary filter. These carry the
# 139 notices from source systems that file no UNSPSC at all (MX, PW, SSC), and
# they catch what the families miss: the DND cloud analytics notice is filed
# 80101507 under management advisory, and only "cloud" finds it.
#
# Terms are matched on whole words. Counts below are hits on the 2026-08-04 feed
# after the date and construction filters (435 notices) — recorded so dead
# vocabulary is visible rather than assumed. The zero-hit terms are kept
# because they cost nothing and would be real signals if they ever appeared;
# they are simply not how the Government of Canada writes tenders.
#
# INTAKE VOCABULARY, NOT CAPABILITY CLAIMS. A term here means "surface notices
# that say this so we can triage them" — never "we can deliver this". The two
# lists answer different questions and are allowed to disagree. `cybersecurity`
# is the standing example: it earns its place as an intake term, while the prose
# below caps the real capability at vulnerability assessment with no active SOC
# work. The prose governs what we bid; this list only governs what we get to
# look at. Don't prune a term because we can't prime that work, and don't read
# one as a capability — Claude is instructed to check the prose constraints
# before recommending anything.
competencies:
  - informatics             # 9  — the actual federal word for IT services
  - information technology  # 10
  - TBIPS                   # 10 — task-based informatics professional services
  - SBIPS                   # 1  — solution-based
  - software                # 17
  - cloud                   # 9
  - SaaS                    # 4
  - modernization           # 16
  - IT modernization        # 1
  - digital transformation  # 4
  - application maintenance # 1
  - AWS                     # 0
  - Azure                   # 0
  - DevOps                  # 0
  - cybersecurity           # 0
  - data engineering        # 0
# NOT added: bare "application" (80 hits, mostly unrelated) and "application
# services", which matches "Pesticide application services for Joyceville and
# Collins Bay Institution". The 4323/8111 families cover real application work.

# Hard exclusions — tenders with these terms are dropped
exclude:
  - janitorial
  - landscaping
  - catering
  - food service

# Timeline. NOT A FILTER — this excludes nothing.
#
# Days-until-close below which a notice is tagged `imminent`. Everything still
# open enters the corpus regardless; this only labels the near ones so the
# briefing can lead with them and so you can decide whether a short fuse is
# disqualifying. Read at query time, so changing it takes effect immediately
# with no re-ingest.
#
# It was `min_days_until_close` and it did delete those notices. Three things
# were wrong with that, none of them visible from the corpus:
#
#   - The date-conflict detector ran AFTER the drop, so a notice closing in
#     eight days was never read for a prose deadline that contradicted its
#     field. Exactly the error that costs a bid.
#   - A tender promoted to watching/ fell out of the corpus in its final ten
#     days — when it matters most.
#   - The briefing's "act now" section asks for a 7-day window against this
#     10-day cutoff, so it could never fill. 7 < 10 is not a tuning problem.
#
# The old key is still read, with a printed migration notice. Renaming it here
# silences that.
imminent_within_days: 10

# Contracts intelligence: keep contracts whose award date OR delivery period
# falls within the last N years (captures recent awards + still-active work)
contracts_window_years: 3

# Opportunity-shaping: minimum contract value for the expiring-contracts scan.
# A future re-procurement below this isn't worth a proactive BD conversation.
# Tune per company — a boutique might set this low, a large firm high.
#
# NOT A FIT RANGE. This is a floor on someone else's EXPIRING contract — how big
# an incumbent's award has to be before its expiry is worth flagging. It says
# nothing about what size of work we would bid. That it currently equals the
# bottom of the prose fit range ($500K) is coincidence, not a link; changing one
# does not imply changing the other. Read by tender_tools.py at query time, so
# edits take effect immediately with no rebuild.
expiry_min_value: 500000

# Contracts describe work as procurement CATEGORY labels, not prose, so the
# contracts filter matches these (case-insensitive substring) rather than the
# competencies above. Terms below are tuned for an IT consulting firm.
# NOTE the data has spelling variants: "information technology" catches the two
# big consultant buckets, but NOT the abbreviated "info. technology and
# telecommunication consultants" variant — "technology and telecommunication"
# is added to catch that one too.
contracts_categories:
  - information technology
  - technology and telecommunication
  - computer equipment
  - application software
  - application development
  - informatics

# --- Reference catalog: contract category vocabulary (NOT active config) ---
# When adapting this profile for a different company, copy relevant terms from
# here into contracts_categories above. These are real category labels from the
# Proactive Disclosure dataset, by rough sector, with approximate volumes.
# Matching is case-insensitive substring, so a short fragment catches variants.
#
# IT / software / tech:
#   information technology and telecommunications consultants (~21k)
#   information technology consultants (~7k)
#   application software (including cots) and application development (~4k)
#   computer equipment - small-desktop / large-medium mainframe (~4k)
#   informatics
#
# Professional / business services:
#   management consulting (~15k), scientific consultants (~6k),
#   scientific services (~11k), temporary help services (~11k),
#   training consultants, research contracts, accounting and audit services,
#   communications professional services, translation services
#
# Engineering / construction:
#   engineering consultants - construction (~4k), other engineering works,
#   institutional buildings, marine installations
#
# Health / life sciences:
#   pharmaceutical and other medicinal products (~9k),
#   measuring, controlling, laboratory, medical and optical equipment (~11k),
#   physicians and surgeons, other health services, welfare services
#
# Goods / other:
#   road motor vehicles, office furniture, ships and boats, diesel fuel,
#   printed matter, printing services, courier services

# Departmental-plan theming (two-pole semantic scoring). FOUR theme groups, used
# as TWO pairs against TWO different fields — see score_programs in
# scripts/plans_ingest.py:
#
#   planning_explanation  -> intent_score    (modernization_intent vs routine_noise)
#       PRIMARY. Forward-looking: what a department says it PLANS to do.
#       This is what program-signals ranks on and what --show-extremes prints.
#
#   variance_explanation  -> pressure_score  (operational_pressure vs accounting_noise)
#       SECONDARY context. Retrospective: what strained last year. A program
#       that both struggled and plans to fix it is the strongest signal, but
#       ranking is on intent.
#
# Each theme is defined by EXAMPLE SENTENCES, not keywords. The embedding model
# averages a group's examples into one vector and scores by meaning, toward the
# positive pole and away from its paired anti-pole:
#
#   intent_score   = sim(text, modernization_intent) - sim(text, routine_noise)
#   pressure_score = sim(text, operational_pressure) - sim(text, accounting_noise)
#
# Tune by editing/adding examples that capture how the signal actually reads;
# the model generalizes to phrasings you didn't list. Keep the two poles of a
# pair disjoint — an example that could sit in either drags both vectors
# together and flattens the spread. Run plans_ingest.py with --show-extremes to
# see whether your examples pull the right rows. Editing any group means
# re-running plans_ingest.py: it is a re-scoring, not a refresh.
plan_themes:
  modernization_intent:
    - "investing to modernize and replace an aging or legacy IT system"
    - "planned funding to migrate systems and infrastructure to the cloud"
    - "modernization initiative to transform digital service delivery"
    - "developing or implementing a new platform to support program operations"
    - "improvements and upgrades to the case management or processing system"
    - "digital transformation and automation of manual processes"
    - "the increase is attributed to a new application modernization initiative"
    - "higher spending due to investment in replacing the legacy IT system"
    - "the variance reflects funding for a system modernization project"
  routine_noise:
    - "ongoing delivery of the program at planned funding levels"
    - "routine statutory transfer payments to recipients"
    - "no significant change from the prior year plan"
    - "spending reflects normal operations and scheduled activities"
    - "variance relates to the timing of contribution or grant payments"
    - "difference is attributed to reprofiled funding and statutory adjustments"
  operational_pressure:
    - "unable to keep pace with a growing backlog of unprocessed cases"
    - "service delays and wait times worsened due to insufficient capacity"
    - "aging legacy systems repeatedly failed and could not meet demand"
    - "emergency funding was required to address a critical operational shortfall"
    - "the program could not deliver on its mandate without additional intervention"
    - "rising caseloads outstripped the resources available to process them"
  accounting_noise:
    - "variance due to timing of statutory payments and reprofiled funds"
    - "difference reflects legislative timing and accounting adjustments"
    - "actual spending differed from planned due to the timing of transfers"
    - "increase mainly due to general support for program delivery"
    - "resources reallocated to internal services for departmental initiatives"
    - "no significant variance between actual and planned spending"

# OAG audit IT-relevance theming (two-pole, like plan_themes). Scores each
# audit's title+description toward IT/systems/digital audits, away from
# financial/environmental/benefits audits. Tune the examples to reshape what
# counts as an IT-relevant audit for your firm.
oag_themes:
  it_audit:
    - "audit of the department's aging IT systems and technology modernization"
    - "failures in a case management or processing system causing backlogs"
    - "problems delivering a digital service or online application platform"
    - "weaknesses in cyber security of government networks and systems"
    - "delays and cost overruns in a major IT or software project"
    - "modernizing legacy technology infrastructure to meet service demand"
  non_it_audit:
    - "audit of financial statements and public accounts"
    - "environmental protection and climate change programs"
    - "administration of benefits, grants and transfer payments"
    - "physical infrastructure such as bridges, buildings and goods procurement"
---

# Company Profile

> **Northwind Digital Inc. is a representative example, not a real client.** It's
> an invented firm, used so this repo ships with a concrete profile rather than
> an empty template — the specificity below is what makes the filter and the
> scoring legible. Replace it with your own; nothing here describes a real
> company's capabilities or portfolio.

This is the single source of truth for "who we are" when Claude helps me find tenders. Claude reads this at the start of most conversations.

## About us

Mid-sized Canadian IT consulting firm, 25 people, founded 2015. Based in Toronto with remote staff across Ontario and Quebec. Primarily francophone-capable team (roughly 40% bilingual).

## Core capabilities

- **Cloud migration:** AWS (strongest), Azure, some GCP. 6 senior architects.
- **Application modernization:** Legacy .NET and Java to containerized microservices.
- **DevOps / Platform engineering:** Kubernetes, Terraform, CI/CD design.
- **Data engineering:** ETL pipelines, warehouse design (Snowflake, BigQuery).
- **Cybersecurity:** Limited — vulnerability assessment only, no active SOC work. Note that `cybersecurity` still appears in the frontmatter `competencies` list: that list is intake vocabulary for surfacing notices to triage, not a claim about what we can deliver. This bullet is what governs whether we bid.

## Current portfolio

- Three active provincial government contracts (Ontario Ministry of Health, Revenu Québec, Ontario LTB).
- Two large enterprise clients (a Canadian bank, an insurance co).
- No federal experience yet — this is the gap we're trying to close.

## What we're looking for

Federal tenders in the $500K–$3M range, in IT modernization or cloud migration. We can go smaller for a foothold contract. We can't realistically compete on anything requiring active Secret or higher clearance without a partner.

## What we're NOT looking for

- Pure staff augmentation / body-shop contracts
- Anything requiring Secret+ clearance as a prime
- Construction-adjacent work (even if it says "IT")
- Hardware supply contracts

## Constraints Claude should respect

- **Don't oversell us.** If a tender wants 10+ years of federal experience, it's not a fit. Say so.
- **Bilingual delivery is an asset but not always mandatory** — check the specific requirement.
- **Assume I've seen none of the tenders before** unless they're in `tenders/watching/` or `tenders/archived/`.
