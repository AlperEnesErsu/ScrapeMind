"""Render signed-in pages to standalone HTML, for the UI audit to inspect.

The accessibility and reflow audits need real pages, and most of this app's
pages are behind a login. Driving a browser through a login form to get at them
would make the audit depend on the login working, which is a different test.

So the pages are rendered through Flask's test client with the session set
directly, written to disk, and their `/static/` links rewritten to relative
paths so a browser can open them over `file://` with the real stylesheet
attached. No server, no credentials, no network.

    venv/Scripts/python.exe scripts/render_pages.py <output-dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.core.models.user import User  # noqa: E402

# Each page is here because it exercises something the others do not: a
# dashboard of metric tiles, a feed of source badges, a data table with row
# actions, a form-heavy settings tab, a filter form, and the heatmap.
PAGES = {
    "dashboard": "/",
    "discover": "/papers/",
    "library": "/library/",
    "tasks": "/admin/tasks/",
    "users": "/admin/users/",
    "settings": "/settings/profile",
    "audit": "/admin/audit/",
}

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "app" / "core" / "static"


def _admin_id(app) -> str:
    """The seeded admin, seeding first if this database has never been seeded."""
    from scripts.seed import run as run_seed

    with app.app_context():
        user = User.query.filter_by(username="admin").first()
        if user is None:
            run_seed(app)
            user = User.query.filter_by(username="admin").first()
        if user is None:
            raise SystemExit("no admin user after seeding -- cannot render signed-in pages")
        return str(user.id)


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "ui-audit-pages").resolve()
    out.mkdir(parents=True, exist_ok=True)

    # Computed rather than hardcoded: the output directory can sit anywhere,
    # and a wrong prefix means the browser loads the pages with no stylesheet
    # and audits something that never existed.
    import os

    static_prefix = os.path.relpath(STATIC_DIR, out).replace(os.sep, "/") + "/"

    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    user_id = _admin_id(app)

    written = 0
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = True

        for name, url in PAGES.items():
            response = client.get(url, follow_redirects=True)
            if response.status_code != 200:
                raise SystemExit(f"{url} answered {response.status_code}, not 200")
            html = response.get_data(as_text=True)
            html = html.replace('href="/static/', f'href="{static_prefix}')
            html = html.replace('src="/static/', f'src="{static_prefix}')
            (out / f"{name}.html").write_text(html, encoding="utf-8")
            written += 1
            print(f"  {name:<10} {url:<20} {len(html):>7} bytes")

    print(f"{written} page(s) written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
