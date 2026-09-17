"""Run the CI `lint-and-test` job locally, in the environment CI uses.

    python scripts/ci_local.py                  # every step, in CI's order
    python scripts/ci_local.py --only mypy      # one or more named steps
    python scripts/ci_local.py --list           # the step names

Each run gets its own throwaway pgvector container on a private network,
removed afterwards. Every local test run used to share one `scrapemind_test`
database, and two sessions running pytest at once locked each other.

It also gives the answer CI will give (docs/PRELAUNCH.md O7/O11). A local venv
drifts from requirements.txt over time, and a drifted venv counts mypy errors
differently: with only beautifulsoup4 moved from its pin, the ratchet counted
78 where CI counted 75. A local count nobody can compare with CI is how a red
PR got merged. HANDOVER §4.12 lists the steps to run before pushing; this runs
them on Python 3.11 with the pinned requirements, as CI does.

What the container sees is what CI checks out: files git tracks plus new files
it does not ignore -- uncommitted edits included, but never `.env`, compiled
`.mo` catalogs, venvs or caches. The tree is copied in, not mounted, so
nothing the run writes lands in the working copy.

Needs Docker. The image (docker/ci.Dockerfile) rebuilds only when
requirements.txt changes.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "scrapemind-ci:py311"
DB_IMAGE = "pgvector/pgvector:pg17"

#: The job's environment, as ci.yml sets it; the database host is the
#: throwaway container's name on the private network.
ENV = {
    "FLASK_APP": "wsgi.py",
    "FLASK_ENV": "testing",
    "SECRET_KEY": "ci-secret",
    "TEST_DATABASE_URL": "postgresql://scrapemind:scrapemind@db:5432/scrapemind_test",
}

TRANSLATION_CONSISTENCY = """
import re, sys
def keys(path):
    return set(re.findall(r'^msgid "(.+)"', open(path, encoding="utf-8").read(), re.M))
tr = keys("translations/tr/LC_MESSAGES/messages.po")
en = keys("translations/en/LC_MESSAGES/messages.po")
diff = tr.symmetric_difference(en)
if diff:
    print("Translation mismatch:", diff); sys.exit(1)
print("Translations OK")
"""

#: (name, command) in ci.yml's order. Keep in step with the workflow: a step
#: CI runs and this skips is exactly the gap this script was written to close.
STEPS: list[tuple[str, list[str]]] = [
    ("translations", ["python", "scripts/compile_translations.py", "--force"]),
    ("ruff", ["ruff", "check", "app/", "tests/", "scripts/"]),
    ("black", ["black", "--check", "app/", "tests/", "scripts/"]),
    ("mypy", ["python", "scripts/mypy_ratchet.py"]),
    ("migrations", ["flask", "db", "upgrade"]),
    (
        "pytest",
        [
            "pytest",
            "-q",
            "--timeout=60",
            "--timeout-method=thread",
            "--cov=app",
            "--cov-fail-under=80",
        ],
    ),
    ("i18n-keys", ["python", "-c", TRANSLATION_CONSISTENCY]),
    ("i18n-audit", ["python", "scripts/i18n_audit.py"]),
]

#: Steps that talk to the database; a run without them skips starting one.
NEEDS_DB = {"migrations", "pytest"}


def _docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, **kwargs)


def _tree_tar() -> bytes:
    """Tracked files plus untracked-but-not-ignored ones, as a tar."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for raw in listed:
            if not raw:
                continue
            relative = raw.decode("utf-8")
            path = ROOT / relative
            if path.is_file():  # a tracked file deleted in the working copy is skipped
                tar.add(path, arcname=relative)
    return buffer.getvalue()


def _build_image() -> None:
    """The build context is requirements.txt alone, so the rest of the working
    copy is neither sent to the daemon nor able to invalidate the cache."""
    print(f"==> image {IMAGE} (cached unless requirements.txt changed)", flush=True)
    with tempfile.TemporaryDirectory() as context:
        shutil.copy(ROOT / "requirements.txt", context)
        _docker(
            "build",
            "--quiet",
            "-f",
            str(ROOT / "docker" / "ci.Dockerfile"),
            "-t",
            IMAGE,
            context,
            stdout=subprocess.DEVNULL,
        )


def _wait_for_db(name: str) -> None:
    for _ in range(60):
        ready = _docker(
            "exec",
            name,
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-U",
            "scrapemind",
            check=False,
            capture_output=True,
        )
        if ready.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit("the throwaway database did not become ready in 60s")


def main() -> int:
    names = [name for name, _ in STEPS]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="+", choices=names, metavar="STEP")
    parser.add_argument("--list", action="store_true", help="print the step names")
    args = parser.parse_args()

    if args.list:
        print("\n".join(names))
        return 0

    selected = [(n, c) for n, c in STEPS if not args.only or n in args.only]
    wants_db = any(n in NEEDS_DB for n, _ in selected)

    run_id = uuid.uuid4().hex[:8]
    network, db, runner = f"sm-ci-{run_id}", f"sm-ci-db-{run_id}", f"sm-ci-run-{run_id}"

    _build_image()
    try:
        _docker("network", "create", network, stdout=subprocess.DEVNULL)
        if wants_db:
            print("==> throwaway pgvector database", flush=True)
            _docker(
                "run",
                "-d",
                "--rm",
                "--name",
                db,
                "--network",
                network,
                "--network-alias",
                "db",
                "-e",
                "POSTGRES_USER=scrapemind",
                "-e",
                "POSTGRES_PASSWORD=scrapemind",
                "-e",
                "POSTGRES_DB=scrapemind_test",
                DB_IMAGE,
                stdout=subprocess.DEVNULL,
            )
            _wait_for_db(db)

        env_flags = [flag for key, value in ENV.items() for flag in ("-e", f"{key}={value}")]
        _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            runner,
            "--network",
            network,
            "-w",
            "/src",
            *env_flags,
            IMAGE,
            "sleep",
            "infinity",
            stdout=subprocess.DEVNULL,
        )
        _docker("exec", runner, "mkdir", "-p", "/src")
        _docker("cp", "-", f"{runner}:/src", input=_tree_tar(), stdout=subprocess.DEVNULL)

        for name, command in selected:
            print(f"\n==> {name}: {' '.join(command[:4])}", flush=True)
            started = time.monotonic()
            result = _docker("exec", runner, *command, check=False)
            took = time.monotonic() - started
            if result.returncode != 0:
                # Like CI: a failed step stops the job, so a later step's
                # failure is never mistaken for the first one.
                print(f"\nFAILED at {name} ({took:.0f}s)", flush=True)
                return result.returncode
            print(f"--> {name} ok ({took:.0f}s)", flush=True)
        print(f"\nAll {len(selected)} step(s) passed in Python 3.11, as CI runs them.")
        return 0
    finally:
        for container in (runner, db):
            _docker("rm", "-f", container, check=False, capture_output=True)
        _docker("network", "rm", network, check=False, capture_output=True)


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
