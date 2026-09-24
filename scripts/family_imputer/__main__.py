"""
Entry point for `python scripts/family_imputer <command>`.

Same sys.path fix as filter_audit/__main__.py: running a directory puts THAT
directory on sys.path[0], which would let a module in here shadow a top-level
import of the same name.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))

from family_imputer.cli import main  # noqa: E402

main()
