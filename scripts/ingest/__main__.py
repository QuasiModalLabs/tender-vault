"""
Entry point for `python scripts/ingest`.

Running a directory puts THAT DIRECTORY on sys.path[0], which is wrong twice
over. Any module in here would shadow a top-level import of the same name made
by any library we load — this is why the profile parser is company_profile.py
and not profile.py — and a bare `import paths` would load paths.py a SECOND
time, as a separate module object with its own DEFAULT_DB.

So the package directory comes off the path and the parent goes on, and the
import runs through the package. There is exactly one of everything, and
nothing in here is importable as a top-level name.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))

from ingest.cli import main  # noqa: E402

main()
