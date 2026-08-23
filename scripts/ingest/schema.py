"""
The tender feed's column contract: what the ingest needs, and what it can live
without.
"""
from __future__ import annotations


# Column names with fallbacks, resolved at runtime against the real header so a
# rename fails loudly with the actual column list instead of silently producing
# nothing. This exists because it didn't: the agency field read a column name
# that had never been in this file, `df.get(col, "")` returned the default, and
# every tender carried an empty agency for weeks without a single error.
TENDER_COLUMNS = {
    "tender_id": ["referenceNumber-numeroReference"],
    "title": ["title-titre-eng"],
    "description": ["tenderDescription-descriptionAppelOffres-eng"],
    "closing_date": ["tenderClosingDate-appelOffresDateCloture"],
    "contracting_entity": ["contractingEntityName-nomEntitContractante-eng"],
    "end_user": ["endUserEntitiesName-nomEntitesUtilisateurFinal-eng"],
    # The publisher's own classification. Preferred over guessing their
    # vocabulary from prose — see classify_notice and unspsc_relevance below.
    "notice_type": ["noticeType-avisType-eng"],
    "procurement_category": ["procurementCategory-categorieApprovisionnement"],
    "unspsc": ["unspsc"],
}
# The six the corpus cannot be built without. The three classification columns
# are deliberately NOT required: they are absent for whole source systems today
# (see UNCODED_SOURCE_SYSTEMS) and a feed that stopped publishing them should
# degrade to text matching with a loud funnel line, not hard-exit the ingest.
TENDER_REQUIRED = [
    "tender_id", "title", "description", "closing_date",
    "contracting_entity", "end_user",
]

# Source systems that file NO unspsc and NO noticeType, as of the 2026-08-04
# feed: MX 100 notices, PW 21, SSC 18 — 139 of 896. Recorded here because the
# gap is not random. SSC is the largest federal IT buyer and PW is PSPC; if
# either starts publishing codes that is a material improvement in this
# pipeline's precision, and the ingest funnel prints the live split every run so
# a change shows up on the next ingest rather than months later.
UNCODED_SOURCE_SYSTEMS = ("MX", "PW", "SSC")
