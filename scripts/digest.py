"""
Generate a digest of the tender corpus after ingest.

Writes to vault/digests/digest-YYYY-MM-DD.md. The digest is what gets committed
by GitHub Actions — it's the human-readable trail of what changed over time.

One digest per ingest that rebuilt the corpus. On the daily schedule that means
one per day the published feed actually moved — with one exception worth knowing
before reading the dates as data: the Monday run rebuilds unconditionally, so a
Monday digest is a heartbeat and does not by itself say the feed changed. Every
other date here does.

Keeps the file short on purpose (~2KB). Meant to be read in 30 seconds, not to
be exhaustive.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# Reuse the tools module's ChromaDB loading logic
sys.path.insert(0, str(Path(__file__).parent))
import tender_tools  # noqa: E402


PROJECT_ROOT = Path(__file__).parent.parent
DIGEST_DIR = PROJECT_ROOT / "vault" / "digests"
PARKED_DIR = PROJECT_ROOT / "vault" / "tenders" / "parked"

# Committed snapshot of last digest's tender IDs. Lives in vault/digests/
# (not .cache/) so the GitHub Actions runner can see the previous run's
# corpus — .cache/ doesn't survive between Actions runs.
CORPUS_SNAPSHOT = DIGEST_DIR / "corpus-latest.txt"


def _previous_digest(today: str) -> str | None:
    """
    The wikilink stem of the most recent digest strictly before `today`.

    BACKWARD ONLY, and that is the whole design. A forward link cannot be
    written when a digest is generated — its successor does not exist yet — so
    the only way to have one is to reopen and patch the previous file on every
    run. That would make a dated snapshot of a past day show up as modified in
    git forever, and turn one write per run into two.

    Obsidian supplies the forward direction for free: the backlinks pane on any
    digest lists the digest that links to it, which is its successor. The chain
    is navigable both ways; only one end of it is stored.

    Filenames are ISO-dated and prefixed, so a lexicographic sort is a
    chronological one. `<` rather than `<=` makes a same-day re-run idempotent:
    today's own file, if it already exists, is never its own predecessor.
    """
    stems = sorted(
        path.stem for path in DIGEST_DIR.glob("digest-*.md")
        if path.stem < f"digest-{today}"
    )
    return stems[-1] if stems else None


def _snapshot_ids() -> set[str] | None:
    """
    Tender IDs in the committed snapshot, or None when there is no snapshot.

    None and the empty set are different answers — "no previous corpus recorded"
    versus "a previous corpus that was empty" — and the diff below treats them
    differently, so they must not collapse into one another here.
    """
    if not CORPUS_SNAPSHOT.exists():
        return None
    return {
        line.strip()
        for line in CORPUS_SNAPSHOT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _summarize_parked() -> str:
    """
    Return a markdown summary of parked tenders. Splits into:
    - Still open (closing date in future) — most valuable to surface
    - Already closed (closing date past) — flagged for archival
    Returns empty string if there are no parked tenders.
    """
    if not PARKED_DIR.exists():
        return ""
    files = sorted(PARKED_DIR.glob("*.md"))
    if not files:
        return ""

    today = datetime.now()
    still_open: list[str] = []
    closed: list[str] = []

    for f in files:
        content = f.read_text(encoding="utf-8")
        # Lightweight frontmatter parse — same approach as cmd_list_parked
        fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        fields: dict[str, str] = {}
        if fm_match:
            for line in fm_match.group(1).split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    fields[k.strip()] = v.strip().strip('"')

        # Most recent revisit trigger
        revisit = ""
        for match in re.finditer(r"\*\*Revisit when:\*\*\s*(.+)", content):
            revisit = match.group(1).strip()

        tender_id = fields.get("tender_id", "?")
        title = fields.get("title", "Untitled")[:60]
        closing_str = fields.get("closing_date", "")

        is_open = False
        if closing_str:
            try:
                closing = datetime.strptime(closing_str, "%Y-%m-%d")
                is_open = closing >= today
            except ValueError:
                is_open = True  # Unparseable — assume open, surface it

        line = f"- `{tender_id}` — {title}"
        if revisit:
            line += f" — *revisit when: {revisit}*"
        (still_open if is_open else closed).append(line)

    parts = []
    if still_open:
        parts.append("\n".join(still_open))
    if closed:
        parts.append(
            "\n*The following parked tenders have closed — consider archiving:*\n"
            + "\n".join(closed)
        )
    return "\n\n".join(parts)


def generate_digest() -> str:
    """Build the digest markdown. Returns the file contents as a string."""
    tender_tools.load_collection()
    docs = tender_tools.corpus.doc_index

    if not docs:
        return "# Digest\n\nNo tenders in corpus.\n"

    # Basic counts
    total = len(docs)
    today = datetime.now().strftime("%Y-%m-%d")

    # Competency distribution
    comp_counter: Counter = Counter()
    for d in docs:
        matched = d["metadata"].get("matched_competencies", "")
        for comp in matched.split(","):
            comp = comp.strip()
            if comp:
                comp_counter[comp] += 1

    # Soonest-closing tenders (top 5 by days-until)
    parseable = []
    for d in docs:
        closing_str = d["metadata"].get("closing_date", "")
        if not closing_str:
            continue
        try:
            closing = datetime.strptime(closing_str, "%Y-%m-%d")
            days = (closing - datetime.now()).days
            if days >= 0:
                parseable.append((days, d))
        except ValueError:
            continue
    parseable.sort(key=lambda x: x[0])

    # Highest-value tenders with a parseable value (top 5)
    valued = [
        d for d in docs
        if isinstance(d["metadata"].get("estimated_value"), (int, float))
        and d["metadata"]["estimated_value"] > 0
    ]
    valued.sort(key=lambda d: d["metadata"]["estimated_value"], reverse=True)

    # Resolved once, up here, because two sections need it: the heading of the
    # diff names it, and the footer links it. Those must agree — a heading that
    # said "since X" while the footer pointed at Y would be a bug nobody sees.
    previous = _previous_digest(today)

    # New since the last digest — the diff against the committed snapshot.
    # First run (no snapshot) skips the section rather than listing everything.
    current_ids = {d["id"] for d in docs}
    old_ids = _snapshot_ids()
    new_ids: set[str] = set() if old_ids is None else current_ids - old_ids

    # Build the markdown.
    #
    # Frontmatter carries the corpus stamps, and this is the one thing in the
    # digest written for a machine rather than for the human reader:
    # chroma_db/ is gitignored and rebuilt on the Actions runner, so the digest
    # is the ONLY committed record of what corpus another machine last built.
    # tender_tools._corpus_provenance reads it back to answer "am I behind".
    #
    # It does not violate the "footer, not header" rule below — Obsidian
    # renders frontmatter as a properties block, so it costs none of the
    # preview lines that the corpus-size line is deliberately given.
    # NOT `getattr(..., None) or {}`: an UNLOADED collection and an UNSTAMPED
    # one would both collapse to {}, and those are different facts — the same
    # conflation the provenance states exist to prevent, one level up.
    # load_collection() above makes None unreachable, so assert it rather than
    # coercing it and hoping.
    collection = tender_tools.corpus._collection
    if collection is None:
        raise RuntimeError(
            "generate_digest: collection is not loaded, so corpus provenance "
            "cannot be read. load_collection() must run first — an unloaded "
            "collection and an unstamped corpus must never both read as empty."
        )
    provenance = dict(collection.metadata or {})

    # feed_sha256 rides along with the dates. It is the only one of the three a
    # second machine can compare against its own without both having run at the
    # same moment, which is what makes this frontmatter a cross-machine record
    # rather than a local one. Absent on digests written before it existed, and
    # the omit-when-missing filter below is what keeps that readable as "no
    # hash" instead of an empty string that compares unequal to everything.
    stamps = [(key, provenance[key])
              for key in ("corpus_built_at", "feed_downloaded_at", "feed_sha256")
              if provenance.get(key)]

    lines: list[str] = []
    if stamps:
        # Values are QUOTED, and that is load-bearing. An unquoted ISO
        # timestamp is a YAML timestamp scalar, so yaml.safe_load resolves
        # `corpus_built_at: 2026-08-09T14:27:11` to a datetime.datetime — while
        # the same stamp read out of Chroma metadata is a str. Every equality
        # check between them would then be False without raising, and the very
        # machine that produced this digest would report itself as behind.
        # Quoting keeps it a string for every reader, including Obsidian.
        #
        # The whole block is skipped when nothing is stamped, rather than
        # emitting a bare `---\n---`. That degenerate form happens to parse as
        # "no frontmatter" only because the closing delimiter never matches —
        # the right answer for the wrong reason, and not something to rely on.
        lines += ["---"]
        lines += [f'{key}: "{value}"' for key, value in stamps]
        lines += ["---", ""]

    lines += [
        f"# Digest — {today}",
        "",
        f"**Corpus size:** {total} tenders after filtering",
        "",
    ]

    if new_ids:
        id_to_doc = {d["id"]: d for d in docs}
        # Named for what it is: a set difference against one specific earlier
        # digest, not a time window. It said "this week" while being computed
        # against whatever snapshot was last committed, which was already three
        # times a week in practice and is daily now. Naming the file it diffed
        # against makes the interval something the reader can read off the page
        # instead of inferring from the schedule.
        against = f" since [[{previous}]]" if previous else ""
        lines += [f"## New{against} ({len(new_ids)})", ""]
        # Show up to 15, highest estimated value first so the interesting
        # ones surface; the rest are findable via search
        new_docs = sorted(
            (id_to_doc[i] for i in new_ids),
            key=lambda d: d["metadata"].get("estimated_value", 0) or 0,
            reverse=True,
        )
        for d in new_docs[:15]:
            title = d["metadata"].get("title", "Untitled")[:70]
            closing = d["metadata"].get("closing_date", "?")
            lines.append(f"- `{d['id']}` — {title} (closes {closing})")
        if len(new_ids) > 15:
            lines.append(f"- …and {len(new_ids) - 15} more")
        lines.append("")

    lines += [
        "## Competency distribution",
        "",
    ]
    for comp, count in comp_counter.most_common(10):
        lines.append(f"- **{comp}**: {count}")

    lines += ["", "## Closing soonest", ""]
    for days, d in parseable[:5]:
        title = d["metadata"].get("title", "Untitled")[:80]
        lines.append(f"- `{d['id']}` — {title} ({days}d)")

    # Instrument shape. More useful than value here, and unlike value it is
    # actually populated — it comes from the publisher's own notice type.
    kind_counter = Counter(
        d["metadata"].get("opportunity_kind", "unknown") for d in docs
    )
    lines += ["", "## By instrument shape", ""]
    for kind, count in kind_counter.most_common():
        lines.append(f"- **{kind}**: {count}")
    if kind_counter.get("qualification"):
        lines.append("")
        lines.append(
            f"> {kind_counter['qualification']} of these qualify a supplier onto a "
            f"vehicle rather than buying work. They are kept deliberately — "
            f"getting onto an arrangement is how the call-ups become reachable — "
            f"but they are not tenders to price.")

    # Only render a value section when a value actually exists. The ingest omits
    # estimated_value unless --extract-values ran, because the regex behind it
    # read ceilings and thresholds rather than prices. An always-empty section
    # under a confident heading reads as "no big tenders right now", which is a
    # different and false claim.
    if valued:
        lines += ["", "## Highest estimated value", ""]
        for d in valued[:5]:
            value = d["metadata"]["estimated_value"]
            title = d["metadata"].get("title", "Untitled")[:80]
            lines.append(f"- `{d['id']}` — {title} (${value:,.0f})")
    else:
        lines += [
            "", "## Estimated value", "",
            "Not available. The feed publishes no value field, and the "
            "description regex was retired for reading ceilings and trade-"
            "agreement thresholds as prices. Run `python scripts/ingest "
            "--extract-values` to populate it from the descriptions.",
        ]

    # Parked tenders — surface ones still active so the user is reminded
    # that their trigger conditions might still resolve in time
    parked_section = _summarize_parked()
    if parked_section:
        lines += ["", "## Parked tenders to keep an eye on", "", parked_section]

    # Footer, not header: this is navigation, and Obsidian previews a file by
    # its opening lines. The corpus-size line earns that space; a link does not.
    lines += ["", "---", ""]
    if previous:
        lines += [f"_Previous digest: [[{previous}]]_", ""]
    lines += [
        "_Generated automatically by `scripts/digest.py` after an ingest that "
        "found a changed feed._",
        "",
    ]
    return "\n".join(lines)


def main():
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    output_path = DIGEST_DIR / f"digest-{today}.md"
    content = generate_digest()
    ids = sorted(d["id"] for d in tender_tools.corpus.doc_index)

    # A SECOND RUN AGAINST AN UNCHANGED CORPUS MUST NOT REWRITE THE FIRST ONE'S
    # FILE. The diff is computed against the snapshot, and the snapshot is
    # rewritten at the end of every run — so run two of a day sees no new IDs,
    # renders the same filename with the "New since" section missing, and
    # destroys the listing run one produced. The output looked like a quiet day
    # rather than like the deletion it was.
    #
    # Both conditions are required. An existing file alone is not enough: a
    # genuine re-ingest that DID bring new notices should still refresh today's
    # digest, which is how a corrected mid-day run is meant to work.
    if output_path.exists() and _snapshot_ids() == set(ids):
        print(f"Digest for {today} already exists and the corpus is unchanged "
              f"since it was written.\n"
              f"  Kept: {output_path.relative_to(PROJECT_ROOT)}\n"
              f"  Rewriting it now would drop its 'New since' section, because "
              f"the snapshot it diffed against has already been advanced.")
        return

    output_path.write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote digest: {output_path.relative_to(PROJECT_ROOT)}")

    # Update the committed snapshot so the next digest can diff against it.
    # Must happen AFTER generate_digest() reads the old snapshot.
    CORPUS_SNAPSHOT.write_text(
        "\n".join(ids) + "\n", encoding="utf-8", newline="\n")
    print(f"Updated snapshot: {CORPUS_SNAPSHOT.relative_to(PROJECT_ROOT)} ({len(ids)} IDs)")


if __name__ == "__main__":
    main()
