"""
Shared test setup: put the vault out of reach before any test can write to it.

THE BUG THIS FIXES IS NOT A MISSING REBIND. It is that a test process could
reach the real vault at all. `promote` creates department nodes, `park` and
`archive` move tender files, and any one of those in a suite pointed at
`vault/` edits the user's working state — silently, and with no failure to
notice afterwards. A per-fixture rebind only holds while every future test
remembers to call the fixture, which is the same class of guarantee as a
comment asking nicely.

So the redirect happens at IMPORT TIME, here, before a test body runs. pytest
loads this file automatically for everything under tests/. The suite also runs
under plain `python tests/test_*.py` (see test_lifecycle's docstring), where
nothing is automatic, so those modules import this one explicitly as their
first project import — one line each, and the guard is unconditional after it.

EVERY REBIND GOES THROUGH `tender_tools.paths`, and that is not cosmetic. The
package splits across a dozen modules, and each one reads these as
`paths.VAULT` at call time rather than importing the name. A `from paths import
VAULT` anywhere in the package would take a private copy that the rebind below
cannot reach, and that module would go on writing into the real vault with
nothing here failing to say so. One owner, read by attribute — see the note at
the top of scripts/tender_tools/paths.py.

TWO SCOPES, and the difference matters. Import time moves the VAULT WRITE
TARGETS only. `PROJECT_ROOT` stays put, because the signal databases are
resolved from it at call time (`db = paths.PROJECT_ROOT / "data" / "plans.db"`
in contracts.py, signals.py and dossier.py) — moving it globally points every
dossier query at an empty directory and the read-only tests start failing for a
reason that has nothing to do with what they assert.

`redirect_vault()` moves `PROJECT_ROOT` as well, and is for tests that write:
promote reports its output as `relative_to(PROJECT_ROOT)`, which raises if the
target sits outside it. Those tests don't read the signal DBs.

`tender_tools.paths.PROFILE` is deliberately NOT redirected either. Tests read the
real profile; reads are safe and the profile is what they test against.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_VAULT = REPO_ROOT / "vault"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import tender_tools as tt  # noqa: E402

# Every module attribute a test could write a vault file through. Adding a new
# write target to tender_tools.paths without adding it here is what this list
# exists to make awkward — assert_vault_unreachable checks exactly these names.
#
# PROJECT_ROOT is deliberately absent: it is not a write target, it is how the
# signal DBs are found. See the module docstring.
_WRITE_TARGETS = ("VAULT", "WATCHING", "PARKED", "ARCHIVED", "AGENCIES")


def assert_vault_unreachable() -> None:
    """Raise unless every vault write target points outside the real vault."""
    for name in _WRITE_TARGETS:
        path = Path(getattr(tt.paths, name)).resolve()
        if path == REAL_VAULT or REAL_VAULT in path.parents:
            raise RuntimeError(
                f"tender_tools.paths.{name} points inside the real vault ({path}). "
                "A test run must never be able to write there."
            )


def _isolate_write_targets(root: Path) -> None:
    tt.paths.VAULT = root / "vault"
    tt.paths.WATCHING = tt.paths.VAULT / "tenders" / "watching"
    tt.paths.PARKED = tt.paths.VAULT / "tenders" / "parked"
    tt.paths.ARCHIVED = tt.paths.VAULT / "tenders" / "archived"
    tt.paths.AGENCIES = tt.paths.VAULT / "agencies"
    assert_vault_unreachable()


def redirect_vault(root: Path | str | None = None) -> Path:
    """
    Full isolation for a test that writes: vault targets AND PROJECT_ROOT.

    PROJECT_ROOT moves here and nowhere else, because promote reports its
    output as relative_to(PROJECT_ROOT) and would raise against a temp path
    otherwise.
    """
    root = Path(root or tempfile.mkdtemp(prefix="tender-vault-test-"))
    tt.paths.PROJECT_ROOT = root
    _isolate_write_targets(root)
    return root


# Import-time, not fixture-time. By the time any test module finishes importing
# this, the real vault is already unreachable — whether or not that module
# remembers to ask. Write targets only; PROJECT_ROOT is left alone so the
# read-only tests can still find data/*.db.
_isolate_write_targets(Path(tempfile.mkdtemp(prefix="tender-vault-guard-")))
