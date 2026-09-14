"""Compile translation catalogs. Thin CLI over `app.core.i18n.catalogs`.

    python scripts/compile_translations.py          # compile what is stale
    python scripts/compile_translations.py --force  # compile everything

Run by the Docker build and CI. The logic lives in the app so the app, the
tests and this script cannot drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.i18n.catalogs import compile_catalogs  # noqa: E402


def main() -> int:
    written = compile_catalogs(force="--force" in sys.argv)
    for po in written:
        print(f"compiled {po.relative_to(ROOT)}")
    if not written:
        print("translations up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
