"""Run mypy and fail only if it got worse.

The CI step ran with `continue-on-error: true`, which meant nobody ever saw it:
95 errors, reported into a log no one opens, blocking nothing. Turning it into
a hard gate is not an option either -- 95 errors is not a morning's work, and
more than half of them are SQLAlchemy relationship attributes mypy cannot see
through on untyped models.

So the step ratchets instead. The count may fall freely; it may not rise. That
costs nothing today, protects against new untyped code, and turns lowering the
baseline into a small deliberate commit rather than a project.

    venv/Scripts/python.exe scripts/mypy_ratchet.py

The baseline lives in `mypy-baseline.txt` so lowering it shows up in a diff.

A count is only comparable with CI's if the installed packages are the ones
requirements.txt pins -- several ship their own type hints. A venv that had
drifted to beautifulsoup4 4.15 (pinned: 4.12.3) counted 3 more errors than CI,
and that was blamed on the Python version for weeks (PRELAUNCH O7/O11). So the
script names any drifted pin before it reports. For CI's exact answer, run
`python scripts/ci_local.py --only mypy`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / "mypy-baseline.txt"
TARGET = "app/"


def _baseline() -> int:
    if not BASELINE_FILE.exists():
        raise SystemExit(f"missing {BASELINE_FILE.name} -- cannot tell better from worse")
    for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return int(line)
    raise SystemExit(f"{BASELINE_FILE.name} has no number in it")


def _drifted_pins() -> list[str]:
    """`name==version` pins in requirements.txt that this interpreter does not match."""
    drifted = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        spec = line.split("#", 1)[0].strip()
        if "==" not in spec:
            continue
        name, pinned = (part.strip() for part in spec.split("==", 1))
        name = name.split("[", 1)[0]
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            drifted.append(f"{name}: pinned {pinned}, not installed")
            continue
        if installed != pinned:
            drifted.append(f"{name}: pinned {pinned}, installed {installed}")
    return drifted


def _run_mypy() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", TARGET, "--ignore-missing-imports"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    output = proc.stdout + proc.stderr
    match = re.search(r"Found (\d+) errors?", output)
    if match:
        return int(match.group(1)), output
    if "Success" in output:
        return 0, output
    raise SystemExit(f"could not read an error count out of mypy:\n{output[-2000:]}")


def main() -> int:
    baseline = _baseline()
    drifted = _drifted_pins()
    if drifted:
        print("WARNING: installed packages differ from requirements.txt, so this count")
        print("may not match CI's. `pip install -r requirements.txt`, or run")
        print("`python scripts/ci_local.py --only mypy` for CI's exact answer.")
        for entry in drifted:
            print(f"  {entry}")
        print()
    count, output = _run_mypy()

    if count > baseline:
        # Show only the errors, not the whole run -- the new ones are what
        # matter and they are hard to find in a hundred lines of context.
        for line in output.splitlines():
            if ": error:" in line:
                print(line)
        print()
        print(f"mypy: {count} errors, baseline is {baseline}. {count - baseline} new.")
        print("Fix them, or if they are genuinely unavoidable, raise the baseline")
        print("in mypy-baseline.txt in the same commit and say why.")
        return 1

    if count < baseline:
        print(f"mypy: {count} errors, down from {baseline}.")
        print(f"Lower the number in {BASELINE_FILE.name} to {count} to lock the gain in.")
        return 0

    print(f"mypy: {count} errors, unchanged from the baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
