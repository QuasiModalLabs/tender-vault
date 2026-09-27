"""
Jev as a UNSPSC family IMPUTER. It names the commodity family a notice most
resembles from its title and description. It never judges fit.

Used three ways, each recorded before its code in
vault/reference/filter-refinements/:

  * ref-004 - EVALUATION. Imputed blind to their codes over the coded archive
    notices and scored against the keyword branch, under a decision rule
    committed before any run (evaluate.py). The rule returned PROCEED.
  * ref-006 - LIVE GATE on the uncoded branch. A notice that files no UNSPSC
    is admitted at relevance when Jev's summed mass across the profile
    families is at least 0.20. Keywords remain the fallback when the imputer
    cannot answer (gate.py, handed to filter_tenders by ingest/cli.py).
  * ref-007 - FLAGS on coded notices whose filed codes reject, at summed mass
    of 0.90 or more. Flag only: the notice is not admitted and the corpus is
    unchanged.

"No UNSPSC" is not "no codes": PW and SSC file a GSIN instead, which the
filter does not read, and MX files neither. See the 2026-09-26 correction in
ref-004.

------------------------------------------------------------------------------
WHAT JEV DOES HERE, AND WHAT IT DOES NOT
------------------------------------------------------------------------------
Jev reads a notice's English title and description and names the commodity
family it most resembles. It IMPUTES A CODE. It never judges fit: the question
it is asked names no company, no competency and no preference, and whether an
imputed family is one we buy is decided afterwards by the profile's
`unspsc_families`, exactly as for a publisher's code. The profile remains the
only place fit is decided.

------------------------------------------------------------------------------
BOUNDARIES - STRUCTURAL, NOT REMEMBERED
------------------------------------------------------------------------------
  * ONE PRODUCT MODULE IMPORTS ONE MODULE OF THIS PACKAGE (ref-006):
    scripts/ingest/cli.py imports `family_imputer.gate`, and hands the imputer
    to filter_tenders as an argument. Nothing else in the product may import
    from here - not tender_tools, not mcp_server, not the rest of ingest, not
    filter_audit.predicates (which receives an Imputation as data). And no
    Claude-read surface (tender_tools, mcp_server, .claude/skills) may name,
    compute or locate the probability. tests/test_family_imputer.py enforces
    both with AST scans. The corpus carries only relevance_basis,
    imputed_family and imputer_model; the probability stays in the gate cache.
    Until ref-006 the rule was "nothing imports this package"; that was the
    evaluation phase, and it is recorded in ref-004.

  * THE IMPUTER IS BLIND TO THE ANSWER. The client accepts only an
    ImputerState, which has two fields: title and description. The publisher's
    codes, the GSIN, the entity and the notice type are absent from the type,
    not merely unused. Codes are read in evaluate.py alone, after both the
    imputer and the keyword comparator have answered.

  * THE GSIN SIDE OF THE PSPC FILE NEVER LOADS. .cache/unspsc_reference.csv is
    the GSIN<->UNSPSC linkage file, and bridging the two code systems through
    it is a standing prohibition (see unspsc_discover.py). options.py reads the
    file through a column whitelist of three UNSPSC columns, so the GSIN columns
    are never held in memory at all.

  * THE QUESTION IS FROZEN. question.QUESTION_SHA256 is a recorded literal. A
    change to the instructions or criteria - including one caused by editing
    the profile's families - makes every run refuse, the same way
    backtest._check_frozen_manifest guards the classifier vocabulary.

  * THE MODEL IS PINNED, and a response naming any other version is refused
    before it can be cached.

  * WRITES GO TO data/family_imputer/ ONLY. Never to filter-reviews.jsonl, the
    golden set, the profile, ingest or predicates.py. Imputed fields are listed
    in filter_audit.blinding.WITHHELD_UNTIL_DISPOSED so that if they are ever
    joined onto a review item they are withheld like any other verdict.
"""
