"""
Download the open-tender feed, revalidating against the publisher.

Not a clock-based cache. CanadaBuys serves this file from Azure Blob Storage,
which sends `ETag` and `Last-Modified`, so "has the feed moved" is a question
the publisher answers directly. See download_tenders for why that matters more
the more often the ingest runs.
"""
from __future__ import annotations

import gzip
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from ingest_common import REQUEST_HEADERS
from .paths import TENDER_URL


# ---------------------------------------------------------------------------
# Data download — revalidate rather than guess from a clock
# ---------------------------------------------------------------------------

def _validators_path(cache_path: Path) -> Path:
    """Where the publisher's cache validators for `cache_path` are recorded."""
    return cache_path.with_name(cache_path.name + ".http.json")


def _load_validators(cache_path: Path) -> dict:
    """ETag / Last-Modified recorded alongside the cached feed. {} when absent."""
    try:
        loaded = json.loads(_validators_path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _snapshot_dir(cache_path: Path) -> Path:
    """Where snapshots live, derived from the cache so `--cache` takes its own."""
    return cache_path.parent / "snapshots"


def _snapshot_feed(cache_path: Path) -> Optional[Path]:
    """
    Copy the feed we are about to overwrite into .cache/snapshots/, gzipped.

    The open-notice feed is a snapshot of what was open on the day it was read,
    and this file has been overwritten on every download since the repo existed
    — so which notices were in the feed on any past day is simply gone, and the
    filter audit has to say so in every replay it prints. This is the smallest
    thing that stops that being true of every FUTURE day.

    NAMED FROM THE OUTGOING FILE'S MTIME, not from today. A snapshot named
    2026-08-17 holds the feed as it was published on 2026-08-17; naming it by
    the download date that replaced it would put every snapshot one day after
    the data it describes. Same reading of mtime as `_feed_mtime_iso` — when the
    data last arrived, not when we last asked.

    DELIBERATELY DUMB, and this is the whole specification: no dedup, no
    pruning, no index, no retention policy, no manifest. A same-named file is
    overwritten. Every one of those would be a policy about which past days are
    worth keeping, and there is no basis for one yet — the point is to have the
    days at all. Compressed because the feed is ~6.7MB of CSV and gzips to
    roughly a tenth of that, which is the difference between a year of daily
    snapshots being unremarkable and being a problem.

    Returns the snapshot path, or None when there was nothing to copy. A first
    run has no cached feed and that is not an error.
    """
    if not cache_path.exists():
        return None
    stamp = datetime.fromtimestamp(cache_path.stat().st_mtime).strftime("%Y-%m-%d")
    target = _snapshot_dir(cache_path) / f"{cache_path.stem}-{stamp}.csv.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("rb") as source, gzip.open(target, "wb") as sink:
        shutil.copyfileobj(source, sink)
    print(f"  snapshotted the outgoing feed to {target.name} "
          f"({target.stat().st_size:,} bytes)")
    return target


def download_tenders(cache_path: Path, force: bool = False) -> pd.DataFrame:
    """
    Download the tender CSV, revalidating against the PUBLISHER rather than a clock.

    CanadaBuys serves this file from Azure Blob Storage, which sends `ETag` and
    `Last-Modified`. So "has the feed moved" is a question the publisher will
    answer directly, for the cost of one conditional request — and the answer is
    a fact about the data instead of an inference from how long ago we asked.
    That matters more the more often this runs: on a daily cadence a clock-based
    cache either re-downloads 6.7MB to learn nothing, or withholds a feed that
    did move, depending on which side of the TTL the run lands.

    A 304 deliberately leaves the cached file's mtime alone, which is what makes
    `_feed_mtime_iso` mean "when the data last arrived" rather than "when we last
    asked". The 12-hour TTL survives as the fallback for a response that carries
    no validators at all.

    A failed request RAISES rather than falling back to the cache. Serving stale
    bytes under a fresh build stamp is the one outcome the provenance states
    downstream cannot represent.

    The outgoing file is snapshotted before it is overwritten — see
    `_snapshot_feed`, which is what makes any past day's feed membership
    recoverable at all.
    """
    validators = {} if force else _load_validators(cache_path)
    headers = dict(REQUEST_HEADERS)

    if not force and cache_path.exists():
        if validators.get("etag"):
            headers["If-None-Match"] = validators["etag"]
        elif validators.get("last_modified"):
            headers["If-Modified-Since"] = validators["last_modified"]
        else:
            age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
            if age < timedelta(hours=12):
                print(f"Using cached CSV ({age.total_seconds() / 3600:.1f}h old; "
                      f"no validator recorded, so this is the 12h fallback)")
                return pd.read_csv(cache_path, low_memory=False)

    conditional = "If-None-Match" in headers or "If-Modified-Since" in headers
    print("Checking Canada Buys for a newer tender feed..." if conditional
          else "Downloading open tender notices from Canada Buys (may take a minute)...")
    response = requests.get(TENDER_URL, headers=headers, timeout=180)

    if response.status_code == 304:
        print("  304 Not Modified — the published feed is the one already cached.")
        return pd.read_csv(cache_path, low_memory=False)

    response.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # BEFORE the overwrite, and only here: this is the one line in the repo that
    # destroys feed state. The 304 branch above returns without writing, so it
    # needs no snapshot.
    _snapshot_feed(cache_path)
    cache_path.write_bytes(response.content)

    # Recorded only on a body we actually stored, so the validators can never
    # describe a file other than the one on disk.
    _validators_path(cache_path).write_text(
        json.dumps({"etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                    "fetched_at": datetime.now().isoformat(timespec="seconds")},
                   indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print(f"  downloaded {len(response.content):,} bytes")
    return pd.read_csv(cache_path, low_memory=False)

