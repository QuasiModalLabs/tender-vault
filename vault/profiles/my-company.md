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
