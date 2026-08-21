"""
Entry point for `python scripts/filter_audit <command>`.

Running a directory puts THAT DIRECTORY on sys.path[0], which would let a
module in here shadow a top-level import of the same name and would let a bare
`import predicates` load this package's module a SECOND time as a separate
object. Same hazard tender_tools/__main__.py documents; same fix.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))

from filter_audit.cli import main  # noqa: E402

main()
