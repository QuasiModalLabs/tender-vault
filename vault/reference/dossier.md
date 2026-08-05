# Reading a dossier

`dossier <department>` is what the four signal tools were built toward. One query, four sources, one canonical key.

## Two rules that govern the whole thing

**There is no score in it, and you should not compute one.** The four signals are incommensurable; any weighting would be invented, and a single number hides the reasoning that makes the dossier worth reading. Say what converges and why, in words — *"the AG flagged their IT modernization in 2023, their own plan names the same systems, and the incumbent's contract runs out in February"* — never "convergence: 8/10." This is the whole architecture of the tool.

**Tenders are not required, and their absence is the most valuable case.** A department with an audit finding, a stated plan, an expiring incumbent and no open tender is the pre-RFP position worth acting on: the work is coming and nobody has been asked yet. An empty tenders section never weakens the case. Say so out loud rather than treating it as a miss.

## The sections

Each carries a `state` saying which kind of empty it is. Read it.

### audits

- `direct_findings` name the department in the finding itself.
- `bundle_attached` are briefing packages that cite a report naming it — real scrutiny, weaker evidence. `parent_reports_in_bundle` says how many reports the package covered; one reached through a five-report agenda is much weaker than one named in the audit.

Never merge the two.

For links use `source_url`. Anything in `report_url_dead` is an `oag-bvg.gc.ca` deep link that no longer resolves — cite it, never hand it over as a working link.

### plans

- `intent_scored` — stated forward intent.
- `no_intent_prose` — the department files plans but no `planning_explanation` in any year, so nothing is intent-scored. That's 16 of 94 organizations, including DND, GAC, RCMP, CBSA and SSC. A `strain` block appears instead, scored from retrospective variance prose. **Strain is not intent.** Never rank them against each other.
- `no_prose_at_all` — neither field is populated.
- `files_no_plans` — the organization files none. A fact about the organization, not missing data.

A `boilerplate_note` marks one sentence filed against many programs. That is one signal, not many.

### contracts

Top vendors by value, and `expiry_timeline`. An incumbent contract ending in 6–24 months is the most actionable field in the dossier.

**Always check `identity.records_folded_in` before quoting a total.** Where a predecessor or absorbed organization is folded in, the registry's note says what the figure actually covers — an IRCC contract total includes Passport Canada, which is one program inside a much larger department.

### tenders

`entity_source` says how each notice reached this department. `end_user` means it named them as the customer; anything else means they are the contracting entity with the end user unstated — which for SSC and PSPC frequently means they are buying for somebody else.

A null `closing_date` with a `date_note` is a sentinel, not a deadline.

`opportunity_kind` is the instrument — see `notice-kinds.md`.

## One department identifier, across all four signal tools

`contracts-intel`, `expiring-contracts`, `program-signals` and `oag-signals` all take `--department`, and all take the same thing: a canonical key from `vault/crosswalk/org_aliases.yaml` (`pspc`, `ircc`, `dnd`) or an organization's registered name.

That is what makes convergence a real join. The same key works in all four, so "OAG flagged them, they plan to modernize it, and the incumbent contract expires next year" is one department rather than three lookups that might not be the same body.

Matching is exact after normalization. **Fragments are refused rather than guessed**, because substring matching is how one department's name lands on another's dossier — "Immigration and Refugee Board" is an independent tribunal and must never answer to IRCC. If a name doesn't resolve you get an error with the closest candidates, not an empty result. Use `resolve-department` to check.

## Reading OAG department attribution

`oag-signals` returns two different things and they are not equally strong:

- **`departments`** — named in the audit itself. Half of all audits name more than one; an audit of six departments is a finding against all six.
- **`inherited_departments`** — on committee briefing packages, which name no department of their own and take them from the report the hearing was about. Each carries `reports_in_hearing`. Pass `--direct-only` to drop them.

**An empty `departments` is usually not a gap.** Only ~110 of 364 records audit a federal department at all. The rest are briefing packages, the OAG's own quarterly financials and annual returns, Crown corporation special examinations, and territorial audits. Those carry `no_department_because` saying which. Don't report them as missing data, and don't quote a single blended coverage number — the honest figure is ~91% of federal audits attributed directly.

Two audits carry `vendor_focus` (GCStrategies, McKinsey) instead of a department. They're reachable via `--vendor` with no `--department`. An audit into a firm we bid against is competitive intelligence regardless of who it touches.
