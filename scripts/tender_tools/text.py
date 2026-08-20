"""Small formatting helpers, shared by everything that renders a tender."""
from __future__ import annotations

import re


def _slugify(text: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug[:max_len] or "untitled"


def _display_agency(meta: dict) -> str:
    """
    Which department to SHOW for a tender. End user is the department that
    needs the work and is what we care about; the contracting entity is the
    fallback for the ~half of rows where end user is blank. This is a display
    convenience only — the two fields are stored separately in ChromaDB and
    anything matching on department should read them separately.
    """
    return meta.get("end_user_entity") or meta.get("contracting_entity") or ""


def _yaml_list(raw: str) -> list[str]:
    """
    One inline frontmatter sequence as a list, for the no-dependency parse.

    cmd_list_watching reads frontmatter by hand rather than pulling in a YAML
    parser, so the `[a, b]` flow form is unpacked here. Quotes come off because
    department entries are written with them: a bare [[ircc]] is a nested
    sequence in YAML, not a string.
    """
    inner = raw.strip()
    if not inner.startswith("["):
        return [inner.strip('"')] if inner else []
    return [p.strip().strip('"') for p in inner[1:].rstrip("]").split(",") if p.strip()]

