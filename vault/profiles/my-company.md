---
company_name: Your Company Name
team_size: 25
founded: 2015
location: Toronto, ON

# Financial targets — used by ingest.py to filter tenders
value_min: 250000
value_max: 5000000

# Competencies — tenders matching these terms survive the ingest filter
competencies:
  - cloud
  - AWS
  - Azure
  - IT modernization
  - cybersecurity
  - DevOps
  - data engineering

# Hard exclusions — tenders with these terms are dropped
exclude:
  - janitorial
  - landscaping
  - catering
  - food service

# Timeline constraints
min_days_until_close: 10

# Contracts intelligence: keep contracts whose award date OR delivery period
# falls within the last N years (captures recent awards + still-active work)
contracts_window_years: 3

# Opportunity-shaping: minimum contract value for the expiring-contracts scan.
# A future re-procurement below this isn't worth a proactive BD conversation.
# Tune per company — a boutique might set this low, a large firm high.
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

# Departmental-plan variance theming (two-pole semantic scoring).
# Each theme is defined by EXAMPLE SENTENCES, not keywords: the embedding model
# scores each program's variance_explanation by meaning — toward the pressure
# pole, away from the accounting pole. pressure_score = sim(pressure) - sim(noise).
# Tune by editing/adding examples that capture how the signal actually reads;
# the model generalizes to phrasings you didn't list. Run plans_ingest.py with
# --show-extremes to see whether your examples pull the right rows.
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

This is the single source of truth for "who we are" when Claude helps me find tenders. Claude reads this at the start of most conversations.

## About us

Mid-sized Canadian IT consulting firm, 25 people, founded 2015. Based in Toronto with remote staff across Ontario and Quebec. Primarily francophone-capable team (roughly 40% bilingual).

## Core capabilities

- **Cloud migration:** AWS (strongest), Azure, some GCP. 6 senior architects.
- **Application modernization:** Legacy .NET and Java to containerized microservices.
- **DevOps / Platform engineering:** Kubernetes, Terraform, CI/CD design.
- **Data engineering:** ETL pipelines, warehouse design (Snowflake, BigQuery).
- **Cybersecurity:** Limited — vulnerability assessment only, no active SOC work.

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
