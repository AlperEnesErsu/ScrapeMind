"""Run the end-to-end smoke suite: a real server, a real worker, a real browser.

    python scripts/e2e.py                       # throwaway Postgres + Redis in Docker
    python scripts/e2e.py -- --grep alert       # anything after -- goes to Playwright
    E2E_DATABASE_URL=... E2E_REDIS_URL=... python scripts/e2e.py   # CI: use its services

Why this exists. The unit suite is large and green, and still every one of
these shipped broken and was found by a person clicking:

- OAuth sign-in never worked: the providers were never registered.
- Language selection never worked: Babel was wired with a 2.x API.
- Saved-search alerts (Faz 7.1) never fired: they run in a worker with no
  request, and every test called them inside one.
- The health panel always said the worker was down: `celery.current_app` is
  thread-local, and the dev server answers on a different thread.

Each is a property of the running system -- processes, threads, cookies, CSRF,
the broker -- that a test client inside one process cannot have. So nothing
here imports the app into the test process. The server runs threaded, the
worker is a separate process on the real queues, and the specs drive Chromium.

Deliberately offline, and enforced rather than hoped for. Every credential the
local .env might hold is blanked, so a local run cannot spend money. Every
HTTP(S) request the server or worker makes goes to a proxy on a closed port,
except to localhost: the add-link path, for one, sends each URL to a third-party
reader before fetching it itself, and a smoke test must not depend on -- or
leak to -- a service it does not control. The one page fetched from "the web"
is served by e2e/stub_site.mjs on localhost.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "e2e" / ".artifacts"
PY = sys.executable

#: Keys in .env.example that carry a credential. The app loads the repo's .env
#: without overriding the environment, so setting these to "" here is what
#: stops a developer's real keys reaching the server or the worker.
SECRET_KEY_PATTERN = re.compile(r"(API_KEY|_TOKEN|_SECRET|CLIENT_ID|PASSWORD|_DSN)$")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def blanked_secrets() -> dict[str, str]:
    names = re.findall(
        r"^#?\s*([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text("utf-8"), re.M
    )
    return {name: "" for name in names if SECRET_KEY_PATTERN.search(name)}


class Services:
    """Postgres and Redis: the caller's, or throwaway containers."""

    def __init__(self) -> None:
        self.containers: list[str] = []
        self.database_url = os.environ.get("E2E_DATABASE_URL", "")
        self.redis_url = os.environ.get("E2E_REDIS_URL", "")

    def __enter__(self) -> Services:
        if self.database_url and self.redis_url:
            return self
        if shutil.which("docker") is None:
            raise SystemExit("set E2E_DATABASE_URL and E2E_REDIS_URL, or install Docker")
        tag = uuid.uuid4().hex[:8]
        db = self._run(
            f"scrapemind-e2e-db-{tag}",
            "pgvector/pgvector:pg17",
            "5432",
            ["-e", "POSTGRES_USER=e2e", "-e", "POSTGRES_PASSWORD=e2e", "-e", "POSTGRES_DB=e2e"],
        )
        redis = self._run(f"scrapemind-e2e-redis-{tag}", "redis:7-alpine", "6379", [])
        self.database_url = f"postgresql://e2e:e2e@127.0.0.1:{self._port(db, '5432')}/e2e"
        self.redis_url = f"redis://127.0.0.1:{self._port(redis, '6379')}"
        self._wait(lambda: _ok(["docker", "exec", db, "pg_isready", "-U", "e2e", "-d", "e2e"]))
        return self

    def __exit__(self, *exc) -> None:
        for name in self.containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    def _run(self, name: str, image: str, port: str, extra: list[str]) -> str:
        subprocess.run(
            ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1::{port}", *extra, image],
            check=True,
            capture_output=True,
        )
        self.containers.append(name)
        return name

    @staticmethod
    def _port(name: str, port: str) -> str:
        out = subprocess.run(
            ["docker", "port", name, port], check=True, capture_output=True, text=True
        ).stdout
        return out.strip().splitlines()[0].rsplit(":", 1)[1]

    @staticmethod
    def _wait(ready, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready():
                # pg_isready answers during the image's init restart; one more
                # beat avoids the first migration hitting that restart.
                time.sleep(2)
                return
            time.sleep(1)
        raise SystemExit("database did not become ready")


def _ok(cmd: list[str]) -> bool:
    return subprocess.run(cmd, capture_output=True).returncode == 0


def environment(services: Services, app_port: int, stub_port: int) -> dict[str, str]:
    env = dict(os.environ)
    env.update(blanked_secrets())
    env.update(
        {
            "FLASK_APP": "wsgi.py",
            # Development, not testing: TestingConfig turns CSRF off and runs
            # tasks eagerly in-process -- the two things this suite exists to
            # keep on. Production would require HTTPS for the session cookie.
            "FLASK_ENV": "development",
            "FLASK_DEBUG": "0",
            "SECRET_KEY": "e2e-secret",
            "DATABASE_URL": services.database_url,
            "REDIS_URL": f"{services.redis_url}/0",
            "CELERY_BROKER_URL": f"{services.redis_url}/0",
            "CELERY_RESULT_BACKEND": f"{services.redis_url}/0",
            "RATELIMIT_STORAGE_URI": f"{services.redis_url}/1",
            "MAIL_SUPPRESS_SEND": "true",
            # The stub site is on localhost; the SSRF guard would refuse it.
            "FEED_ALLOW_PRIVATE_HOSTS": "true",
            # No egress: port 9 (discard) is closed, so any outside request
            # fails fast instead of reaching an LLM, a reader or an API.
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
            # Lower-case too: on Linux a stray lower-case variant in the
            # caller's shell would otherwise win over the upper-case one.
            "http_proxy": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "no_proxy": "127.0.0.1,localhost",
            "LLM_PROVIDER": "openrouter",
            "EMBEDDING_PROVIDER": "",
            "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1",
            "E2E_BASE_URL": f"http://127.0.0.1:{app_port}",
            "E2E_STUB_PORT": str(stub_port),
            "E2E_PYTHON": PY,
        }
    )
    return env


def step(name: str, cmd: list[str], env: dict[str, str]) -> None:
    print(f"==> {name}", flush=True)
    if subprocess.run(cmd, cwd=ROOT, env=env).returncode != 0:
        raise SystemExit(f"e2e setup failed at: {name}")


def start(name: str, cmd: list[str], env: dict[str, str]) -> subprocess.Popen:
    log = open(ARTIFACTS / f"{name}.log", "w", encoding="utf-8")  # noqa: SIM115
    print(f"==> start {name} (log: e2e/.artifacts/{name}.log)", flush=True)
    return subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def wait_http(url: str, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"server exited ({proc.returncode}) before answering {url}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{url} did not answer within {timeout:.0f}s")


def stop(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def main(argv: list[str]) -> int:
    playwright_args = argv[argv.index("--") + 1 :] if "--" in argv else []
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("npx not found: install Node, then `npm ci`")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    app_port, stub_port = free_port(), free_port()
    procs: list[subprocess.Popen] = []

    with Services() as services:
        env = environment(services, app_port, stub_port)
        step("translations", [PY, "scripts/compile_translations.py"], env)
        step("migrations", [PY, "-m", "flask", "db", "upgrade"], env)
        step("prepare", [PY, "scripts/e2e_helper.py", "prepare"], env)
        try:
            # Threaded, as a real deployment answers: the health-panel bug
            # lived in a thread-local that a single-threaded server hides.
            web = start(
                "web",
                [
                    PY,
                    "-m",
                    "flask",
                    "run",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(app_port),
                    "--no-reload",
                    "--no-debugger",
                    "--with-threads",
                ],
                env,
            )
            procs.append(web)
            # The queues worker.bat and docker-compose use. -P solo works on
            # Windows too, and one task at a time is all a smoke run needs.
            procs.append(
                start(
                    "worker",
                    [
                        PY,
                        "-m",
                        "celery",
                        "-A",
                        "app.tasks",
                        "worker",
                        "-P",
                        "solo",
                        "-Q",
                        "celery,io,scrape,llm",
                        "--loglevel=info",
                    ],
                    env,
                )
            )
            wait_http(f"http://127.0.0.1:{app_port}/api/v1/health", web)
            step("worker", [PY, "scripts/e2e_helper.py", "wait-worker"], env)

            print("==> playwright", flush=True)
            result = subprocess.run(
                [
                    npx,
                    "playwright",
                    "test",
                    "--config",
                    "e2e/playwright.config.mjs",
                    *playwright_args,
                ],
                cwd=ROOT,
                env=env,
            )
            return result.returncode
        finally:
            stop(procs)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
