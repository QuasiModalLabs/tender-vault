"""Corpus provenance — how old is what you are reading."""
from __future__ import annotations

import re
from pathlib import Path

from . import corpus, paths

# A briefing once dated the corpus from `chroma_db/` file times and got it
# wrong: ChromaDB rewrites its HNSW segment files whenever anything LOADS the
# collection, so those mtimes report when the corpus was last queried. Reading
# them describes your own read. The ingest therefore stamps the collection, and
# this is where that stamp is read back.
#
# TWO stamps, because they answer different questions. `feed_downloaded_at`
# says how old the DATA is; `corpus_built_at` says when it was last processed.
# A rebuild off an unchanged cache moves the second and not the first, and that
# is a filter or profile change rather than new notices.
#
# Plus `feed_sha256`, which answers a third question neither date can: WHICH
# feed, as opposed to when it arrived here. Dates are per-machine and a hash is
# not, so the hash is what makes two machines comparable at all. Both are kept
# — a hash cannot say how old anything is — and `_corpus_provenance` records
# which of the two it compared on rather than leaving the reader to guess.
#
# NO THRESHOLD, and deliberately no "stale" field. How old is too old is not
# stateable — the ingest cron runs daily but only rebuilds when the published
# feed moved, so a corpus several days old can be perfectly current — and a
# boolean verdict in a data field is the kind of judgement this project keeps
# out of its tools. Report the stamps beside the newest digest's and let the
# reader compare two observed facts.
#
# Note that the daily cadence made the threshold LESS stateable, not more. An
# age in days now measures the publisher's release schedule as much as this
# machine's, and the hash answers the question an age was being used to
# approximate anyway.

def _digest_frontmatter(path: Path) -> dict[str, str]:
    """
    Frontmatter of one digest as a flat dict of STRINGS.

    Hand-parsed rather than run through PyYAML, and the type matters more than
    the parser. `corpus_built_at: 2026-08-09T14:27:11` unquoted is a YAML
    timestamp scalar: safe_load returns a datetime.datetime, while the stamp it
    gets compared against — read out of Chroma metadata — is a str. Every
    equality check between the two is then False without anything raising, and
    the machine that produced the digest reports itself as behind. digest.py
    quotes the values for exactly this reason.

    So the string type is asserted rather than assumed. If this is ever swapped
    for safe_load, or the quoting in digest.py is dropped, it fails loudly here
    instead of returning a confident wrong answer downstream.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"')
    bad = {k: type(v).__name__ for k, v in fields.items() if not isinstance(v, str)}
    if bad:
        raise TypeError(
            f"{path.name}: frontmatter values must be str, got {bad}. An ISO "
            f"timestamp parsed as a YAML date compares unequal to the string "
            f"stamp in the collection metadata, silently, in every direction."
        )
    return fields

def _newest_digest() -> Path | None:
    """
    The most recent digest file, or None if there are none.

    Same glob and ordering as digest._previous_digest, which documents the
    invariant this relies on: "Filenames are ISO-dated and prefixed, so a
    lexicographic sort is a chronological one." Kept identical on purpose — two
    lookups over the same directory that sort differently is a bug nobody sees.
    """
    digests = sorted(paths.DIGESTS.glob("digest-*.md"), key=lambda p: p.stem)
    return digests[-1] if digests else None


def _corpus_provenance() -> dict:
    """
    When this corpus was built, and how that compares to the newest digest.

    Four states per source, and they are NOT interchangeable:

      stamped           both fields present.
      unstamped         neither — built before provenance stamping. Rebuild.
      no_feed_at_build  built, but with no cached feed to date. The corpus is
                        current as a build and its DATA cannot be dated. That
                        is not the same fact as `unstamped`, which is why it
                        gets its own name: reading it as "predates stamping"
                        invites a rebuild that would fix nothing.
      not_found         (digest only) no digest file resolved at all. Distinct
                        from `unstamped` so a glob that silently matches
                        nothing cannot masquerade as working code.
    """
    meta = dict(getattr(corpus._collection, "metadata", None) or {})
    built_at = meta.get("corpus_built_at")
    feed_at = meta.get("feed_downloaded_at")

    if built_at is None:
        local_state = "unstamped"
        local_note = ("This corpus predates provenance stamping. "
                      "Re-run: python scripts/ingest.py")
    elif feed_at is None:
        local_state = "no_feed_at_build"
        local_note = ("Ingest ran with no cached feed, so the corpus is "
                      "stamped but the age of the data in it is unknown. "
                      "This is not the same as predating stamping — a rebuild "
                      "alone will not date it.")
    else:
        local_state, local_note = "stamped", None

    out = {
        "corpus_built_at": built_at,
        "feed_downloaded_at": feed_at,
        "state": local_state,
    }
    if local_note:
        out["note"] = local_note

    newest = _newest_digest()
    if newest is None:
        out["newest_digest"] = None
        out["newest_digest_state"] = "not_found"
        out["newest_digest_note"] = (
            f"No digest matched {paths.DIGESTS}/digest-*.md. Expected at least one; "
            f"if digests exist, the lookup is broken rather than empty."
        )
        return out

    fm = _digest_frontmatter(newest)
    d_built = fm.get("corpus_built_at") or None
    d_feed = fm.get("feed_downloaded_at") or None
    out["newest_digest"] = newest.stem
    out["newest_digest_corpus_built_at"] = d_built
    out["newest_digest_feed_downloaded_at"] = d_feed

    if d_built is None:
        out["newest_digest_state"] = "unstamped"
        out["newest_digest_note"] = (
            "This digest predates provenance stamping. "
            "Re-run: python scripts/digest.py")
        return out
    if d_feed is None:
        out["newest_digest_state"] = "no_feed_at_build"
    else:
        out["newest_digest_state"] = "stamped"

    # The comparison. It prefers the feed HASH over the feed timestamp, and the
    # difference is not cosmetic: `feed_downloaded_at` records when a machine
    # downloaded, so two machines that fetched the same bytes at different
    # moments disagree on it while holding identical data. At one ingest a week
    # that mismatch was rare enough to live with. At one a day it is the common
    # case, and it would tell a reader to re-ingest every morning to acquire a
    # feed they already have.
    #
    # `basis` names which comparison actually ran, because "the hashes differ"
    # and "there was no hash to compare" are different findings and the reader
    # cannot tell them apart from the reading alone. Same rule that keeps
    # `unstamped` and `no_feed_at_build` separate, one level down.
    #
    # Still no threshold and still no boolean anywhere — see the note above the
    # digest helpers, and test_provenance.test_no_verdict_field, which fails if
    # a verdict is ever added here.
    local_hash, digest_hash = meta.get("feed_sha256"), fm.get("feed_sha256") or None
    if local_hash and digest_hash:
        out["feed_sha256"] = local_hash
        out["newest_digest_feed_sha256"] = digest_hash
        out["basis"] = "feed_sha256"
        if local_hash == digest_hash:
            if built_at == d_built:
                out["reading"] = "this corpus produced the newest digest"
            else:
                out["reading"] = (
                    "same feed, different build — a membership difference is a "
                    "filter or profile effect, not new notices")
        else:
            # Hashes are not ordered, so which side is newer is a question the
            # hash cannot answer. The timestamps can, and are read ONLY here,
            # where the data is already known to differ.
            if feed_at is not None and d_feed is not None and feed_at < d_feed:
                out["reading"] = (
                    "behind on data — the newest digest was built from a "
                    "different feed, downloaded later than this one. "
                    "Run: python scripts/ingest.py")
            elif feed_at is not None and d_feed is not None:
                out["reading"] = (
                    "this machine has a feed the newest digest has not seen")
            else:
                out["reading"] = (
                    "different feeds, and no pair of download dates to order "
                    "them by. Re-ingest if this machine's corpus matters.")
    elif local_state == "stamped" and d_feed is not None:
        out["basis"] = "feed_downloaded_at"
        if feed_at == d_feed:
            if built_at == d_built:
                out["reading"] = "this corpus produced the newest digest"
            else:
                out["reading"] = (
                    "same feed, different build — a membership difference is a "
                    "filter or profile effect, not new notices")
        elif feed_at < d_feed:
            out["reading"] = (
                "behind on data — the newest digest was built from a feed this "
                "machine has not downloaded. Run: python scripts/ingest.py")
        else:
            out["reading"] = (
                "this machine has a feed the newest digest has not seen")
        # Said out loud rather than left to be inferred: a download date is a
        # weaker instrument than a hash, and one side of this comparison
        # predates hashing. Equal dates here are not proof of equal data.
        out["basis_note"] = (
            "Compared on download dates because "
            + ("the newest digest carries no feed_sha256"
               if local_hash else "this corpus carries no feed_sha256")
            + ". Dates are per-machine, so this cannot distinguish the same "
              "feed fetched twice from two different feeds.")
    return out
