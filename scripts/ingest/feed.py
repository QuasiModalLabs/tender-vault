"""Download the open-tender feed, cached aggressively."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from ingest_common import REQUEST_HEADERS
from .paths import TENDER_URL


def download_tenders(cache_path: Path, force: bool = False) -> pd.DataFrame:
    """Download tender CSV, cached to disk for 12 hours."""
    if not force and cache_path.exists():
        age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
        if age < timedelta(hours=12):
            print(f"Using cached CSV ({age.total_seconds() / 3600:.1f}h old)")
            return pd.read_csv(cache_path, low_memory=False)

    print("Downloading open tender notices from Canada Buys (may take a minute)...")
    response = requests.get(TENDER_URL, headers=REQUEST_HEADERS, timeout=180)
    response.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(response.content)
    return pd.read_csv(cache_path, low_memory=False)
