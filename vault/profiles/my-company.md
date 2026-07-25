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
