"""
Entry point for `python scripts/tender_tools <command>`.

Running a directory puts THAT DIRECTORY on sys.path[0], which is wrong twice
over. Any module in here would shadow a top-level import of the same name made
by any library we load — `profile.py` shadowing the stdlib `profile` is how
this was found, via a ValueError from sentence_transformers three imports
deep — and a bare `import corpus` would load corpus.py a SECOND time, as a
separate module object with its own doc_index and its own vault paths.

So the package directory comes off the path and the parent goes on, and the
import runs through the package. There is exactly one of everything, and
nothing in here is importable as a top-level name.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))

from tender_tools.cli import main  # noqa: E402

main()
