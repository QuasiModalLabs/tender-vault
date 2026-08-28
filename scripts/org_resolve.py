"""
Resolve federal organizations named in free text against the canonical registry.

WHY THIS IS NOT crosswalk.normalize_org. The crosswalk joins two clean NAME
FIELDS by normalized equality, and normalize_org exists to make that equality
work: it strips "Department of" and a trailing "Canada" so the legal and applied
names of one body compare equal. That is right for comparing a field to a field.

It is catastrophic for finding names inside PROSE. The same stripping reduces
canonical names to bare common nouns — health-canada to "health", eccc to
"environment", finance-canada to "finance", ised to "industry" — and an audit
sentence contains those words constantly. Scanning with normalize_org's output
matched "Standing Senate Committee on Energy, the Environment and Natural
Resources" as Environment and Climate Change Canada, 71 times. So this module
indexes SURFACE FORMS, folding only case, accents, ampersands and punctuation,
and never touching affixes.

WHAT IT REFUSES, and why each refusal is load-bearing:

  one-token phrases   No organization is indexed under a single word. "Health"
                      and "Industry" are not identifications.
  auditor-general     The OAG is the publisher of every record in oag.db, named
                      in 254 of 364. It is who WROTE the audit, never who was
                      audited.
  committee names     "Standing Committee on Fisheries and Oceans" is who a
                      report was tabled with. Attributing the audit to the
                      portfolio the committee is named after invents a finding
                      against a department that may not appear in the report at
                      all — the Canadian Heritage committee heard a connectivity
                      audit of ISED and the CRTC.
  the `not:` lists    org_aliases.yaml records, per entry, the names that must
                      never resolve to it. Nothing exercised them until now.

Longest match wins and consumes its span, so "Royal Canadian Mounted Police
External Review Committee" cannot also register as the force it reviews.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from difflib import get_close_matches
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from crosswalk import load_aliases, normalize_org, observed_variants  # noqa: E402

# The publisher of every OAG record. Present in 254 of 364 titles and never the
# audited body, so it is excluded from the scan index rather than filtered from
# results — a caller resolving "Office of the Auditor General of Canada" by name
# still gets the key, because that is a lookup, not an attribution.
PUBLISHER_KEY = "auditor-general"

# Parliamentary committees, stripped before scanning. The trailing alternation
# stops the span at the point the committee's name ends and the report's own
# subject begins, so "…Committee on Public Accounts on Report 5, Professional
# Services Contracts" loses only the committee.
_COMMITTEE = re.compile(
    r"\b(?:standing\s+)?(?:senate\s+)?(?:joint\s+)?committee\s+on\s+"
    r".*?(?=\s+(?:on|for|regarding|to discuss)\b|[,.;(–—]|$)",
    re.I,
)

# Minimum tokens for a phrase to be indexed. Two is the smallest value that
# excludes "Health"/"Industry"/"Finance" while keeping real short names like
# "Global Affairs Canada" (3) and "Parks Canada" (2).
MIN_PHRASE_TOKENS = 2


def fold(text: str) -> str:
    """
    Case, accents, ampersands and punctuation only. Affixes stay on.

    Returned space-padded so callers can test token-boundary containment with a
    plain `in` rather than building a regex per phrase.
    """
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return f" {re.sub(r'[ ]+', ' ', s).strip()} "


def strip_committees(text: str) -> str:
    """Remove parliamentary committee names ahead of a scan."""
    return _COMMITTEE.sub(" ", text or "")


class OrgResolver:
    """
    A phrase index over org_aliases.yaml, built once and reused.

    Construction is the expensive part (it reads and folds every name in the
    registry), so ingest builds one and scans 364 records through it.
    """

    def __init__(self, aliases: dict[str, dict] | None = None,
                 exclude_keys: frozenset[str] = frozenset({PUBLISHER_KEY})):
        self.aliases = aliases if aliases is not None else load_aliases()
        self.exclude_keys = exclude_keys

        # phrase -> the key(s) claiming it. A phrase claimed by two entries is a
        # registry defect; it is reported rather than silently resolved.
        self._phrases: dict[str, set[str]] = {}
        for key, entry in self.aliases.items():
            if key in exclude_keys:
                continue
            for name in self._names_of(entry):
                phrase = fold(name).strip()
                if len(phrase.split()) >= MIN_PHRASE_TOKENS:
                    self._phrases.setdefault(phrase, set()).add(key)

        # Per-entry refusals, folded to compare against matched phrases.
        self._refused: dict[str, set[str]] = {
            key: {fold(n).strip() for n in (entry.get("not") or [])}
            for key, entry in self.aliases.items()
        }

        # DECOYS. Every `not:` string is scanned for in its own right, even
        # though no entry may claim it as a name, because the hazard it guards
        # against is a LONGER string containing a SHORTER organization's name.
        # "Royal Canadian Mounted Police External Review Committee" opens with
        # the complete and correct name of a different body; checking the
        # refusal against the phrase that matched is useless, because the phrase
        # that matched is the short one. The decoy has to win the span first.
        self._decoys: dict[str, set[str]] = {}
        for key, refusals in self._refused.items():
            for phrase in refusals:
                if len(phrase.split()) >= MIN_PHRASE_TOKENS:
                    self._decoys.setdefault(phrase, set()).add(key)

        # Longest first so a superset name — or a decoy — consumes its own prefix.
        self._ordered = sorted(set(self._phrases) | set(self._decoys),
                               key=lambda p: -len(p.split()))

        # Exact-lookup index for resolve(), which answers "what is this string"
        # rather than "what is named in this prose". Includes one-token names and
        # the publisher, because neither is a hazard when the whole input is the
        # name being asked about.
        self._exact: dict[str, set[str]] = {}
        for key, entry in self.aliases.items():
            for name in self._names_of(entry):
                self._exact.setdefault(normalize_org(name), set()).add(key)

    @staticmethod
    def _names_of(entry: dict) -> list[str]:
        return [n for n in [entry.get("name")] + list(entry.get("observed_names") or []) if n]

    def ambiguous_phrases(self) -> dict[str, set[str]]:
        """Phrases more than one entry claims — a registry defect if non-empty."""
        return {p: k for p, k in self._phrases.items() if len(k) > 1}

    def scan(self, text: str, strip_committee_names: bool = True) -> dict[str, str]:
        """
        Every organization NAMED in `text`, as {canonical_key: matched phrase}.

        The matched phrase is returned because it is the evidence for the
        attribution — an edge that cannot say what string produced it is not
        reviewable.
        """
        if not text:
            return {}
        if strip_committee_names:
            text = strip_committees(text)
        haystack = fold(text)

        hits: dict[str, str] = {}
        for phrase in self._ordered:
            padded = f" {phrase} "
            if padded not in haystack:
                continue
            for key in self._phrases.get(phrase, ()):
                if phrase in self._refused.get(key, ()):
                    continue
                hits.setdefault(key, phrase)
            # Consume the span so a shorter name — or a decoy's victim — cannot
            # also claim it. A decoy that no entry claims consumes and awards
            # nothing, which is the intended outcome: the text named a body the
            # registry has no key for, and silence beats the wrong neighbour.
            haystack = haystack.replace(padded, " \x00 ")
        return hits

    def resolve(self, value: str) -> str | None:
        """
        Turn one identifier — a canonical key or an organization name — into a key.

        Registry-only and exact after normalization. Deliberately NOT a substring
        match: substring matching against display names is what the `not:` lists
        exist to prevent, and "Immigration and Refugee Board" would otherwise
        answer to IRCC, which is a different institution.

        This uses normalize_org, unlike scan(), and the difference is deliberate.
        The caller has handed over a COMPLETE identifier and the comparison is
        equality, so the trailing-"Canada" strip is a convenience — "Health"
        equals "Health Canada" and resolves to health-canada. Ten registry names
        reduce to one token that way and none of them collides. Inside prose the
        same word is one token in a sentence, which is why scan() indexes surface
        forms and refuses anything under two tokens.
        """
        if not value:
            return None
        candidate = value.strip()
        if candidate in self.aliases:
            return candidate
        keys = self._exact.get(normalize_org(candidate))
        if not keys:
            return None
        if len(keys) > 1:                       # registry defect, not a guess
            raise AmbiguousOrganization(candidate, sorted(keys))
        key = next(iter(keys))
        if normalize_org(candidate) in {normalize_org(n)
                                        for n in (self.aliases[key].get("not") or [])}:
            return None
        return key

    def suggest(self, value: str, n: int = 5) -> list[str]:
        """Closest keys and names, for the error message when resolve() fails."""
        pool = list(self.aliases) + [e.get("name") for e in self.aliases.values() if e.get("name")]
        return get_close_matches(value or "", pool, n=n, cutoff=0.5)

    def display_name(self, key: str) -> str:
        return (self.aliases.get(key) or {}).get("name") or key


class AmbiguousOrganization(Exception):
    """One string, two canonical keys — the registry must be fixed, not guessed."""

    def __init__(self, value: str, keys: list[str]):
        super().__init__(f"{value!r} resolves to more than one organization: {keys}")
        self.value, self.keys = value, keys


CROSSWALK_DB = Path(__file__).parent.parent / "data" / "crosswalk.db"


def department_scope(key: str) -> dict[str, list]:
    """
    What one canonical key means in each source database.

    The other two datasets do not store canonical keys — plans.db has the
    Infobase organization_id and contracts.db has the CKAN slug — so a
    department filter has to be translated per source. crosswalk.db already
    holds that mapping, and using it keeps every tool joining on an ID rather
    than comparing display names, which is the whole reason the crosswalk was
    built.

    Returns empty lists when the crosswalk has not been built or the key has no
    presence in a source. An organization that files plans but publishes no
    contracts is a real case, not an error.
    """
    scope = {"plan_ids": [], "contract_slugs": []}
    if not CROSSWALK_DB.exists():
        return scope
    import sqlite3
    con = sqlite3.connect(CROSSWALK_DB)
    try:
        for plan_id, slug in con.execute(
            "SELECT plan_organization_id, contract_owner_org FROM org_crosswalk "
            "WHERE canonical_key = ?", (key,)
        ):
            if plan_id is not None and plan_id not in scope["plan_ids"]:
                scope["plan_ids"].append(plan_id)
            if slug and slug not in scope["contract_slugs"]:
                scope["contract_slugs"].append(slug)
    except sqlite3.OperationalError:
        return scope
    finally:
        con.close()
    return scope


_DEFAULT: OrgResolver | None = None


def default_resolver() -> OrgResolver:
    """Process-wide resolver, so CLI tools don't rebuild the index per call."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = OrgResolver()
    return _DEFAULT


def resolve_identifier(value: str) -> tuple[str | None, str, list[str]]:
    """
    Resolve a department identifier, falling back to the entity-field variants.

    Returns (key, how, keys_seen) where `how` is one of exact | variant |
    ambiguous | unresolved.

    WHY THE FALLBACK EXISTS. `resolve()` is exact after normalization, which is
    right when the caller hands over a complete name — but the strings this
    project actually carries are entity fields, and they do not look like that.
    `list-corpus` reports an agency as "Department of Employment and Social
    Development (ESDC)" and "Canada Revenue Agency - (Administered Activities)
    (CRA)". Neither resolves exactly, so every tool that says "check the name
    before querying" was answering "not a known organization" for departments
    the registry knows perfectly well.

    `crosswalk.observed_variants` already solves this at ingest — it strips the
    parenthetical acronym tail and splits slash-delimited multi-department
    values, and `ingest.entity_org_keys` reports the difference as 47 of 896
    rows resolving raw against 767 resolving through variants. This routes the
    same splitting through the same registry so a query filter and the ingest
    agree on what a string means.

    AMBIGUITY IS REFUSED, NOT GUESSED. One entity field can legitimately name
    several departments ("Department of Transport (TC) / Department of Fisheries
    and Oceans (DFO)"). That is a real multi-department value at ingest and a
    meaningless filter here, so it returns `ambiguous` with every key found
    rather than picking the first — the registry's rule throughout.
    """
    resolver = default_resolver()
    if not value or not str(value).strip():
        return None, "unresolved", []
    try:
        key = resolver.resolve(value)
    except AmbiguousOrganization:
        key = None
    if key:
        return key, "exact", [key]

    found: list[str] = []
    for variant in observed_variants(str(value)):
        try:
            candidate = resolver.resolve(variant)
        except AmbiguousOrganization:
            continue
        if candidate and candidate not in found:
            found.append(candidate)
    if len(found) == 1:
        return found[0], "variant", found
    if len(found) > 1:
        return None, "ambiguous", found
    return None, "unresolved", []


def resolve_department_arg(value: str | None, flag: str = "--department") -> str | None:
    """
    Resolve a user-supplied department identifier, or exit with a useful error.

    Shared by every tool that filters on a department, so `pspc`, `PSPC` and
    "Public Services and Procurement Canada" mean the same thing in all of them
    and none of them silently accepts a string that means nothing.
    """
    if value is None:
        return None
    resolver = default_resolver()
    key, how, found = resolve_identifier(value)
    if how == "ambiguous":
        sys.stderr.write(
            f"{flag}: {value!r} names more than one organization: "
            f"{', '.join(found)}.\n"
            "  A filter has to mean one department. Pass one of those keys.\n"
        )
        sys.exit(2)
    if key:
        return key
    hint = resolver.suggest(value)
    sys.stderr.write(
        f"{flag}: {value!r} is not a known organization.\n"
        + (f"  Closest matches: {', '.join(hint)}\n" if hint else "")
        + "  Pass a canonical key from vault/crosswalk/org_aliases.yaml, or an\n"
          "  organization's registered name. Run `tender_tools "
          "resolve-department <name>`\n  to check a string without running a query.\n"
    )
    sys.exit(2)
