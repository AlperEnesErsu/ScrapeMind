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
from app.extensions import db  # noqa: E402

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
    # Added after the fact: the library search page carried two unlabelled
    # selects, two unlabelled date inputs and 81px of overflow at 280px,
    # entirely because it had never been in this list.
    "librarysearch": "/library/search?q=a",
    "workspace-alerts": "/settings/workspace?tab=alerts",
    "workspace-zotero": "/settings/workspace?tab=zotero",
    "workspace-ai": "/settings/workspace?tab=ai",
}

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "app" / "core" / "static"


def _ensure_a_paper(app, user_id: int) -> None:
    """Put a paper in the library if it is empty, and a seen one if none is.

    Without this the audit is weaker in CI than it is locally, and silently:
    a freshly seeded database has no papers, so no paper card renders, so the
    card's markup is never audited. That is how an unlabelled bulk-select
    checkbox survived -- it only appeared once a developer's own database
    happened to have something in it.

    Idempotent, and the row is obviously synthetic so nobody mistakes it for
    scraped data.
    """
    from app.modules.scrape.models import UserPaper

    with app.app_context():
        if UserPaper.query.filter_by(user_id=user_id).first() is None:
            _add_fixture_paper(user_id, "ui-audit-fixture")
        # A seen card is styled differently, and that style once dropped every
        # text colour on the card below AA -- invisible here because the one
        # fixture paper had never been opened. Render both states.
        seen = UserPaper.query.filter(
            UserPaper.user_id == user_id, UserPaper.seen_at.isnot(None)
        ).first()
        if seen is None:
            _add_fixture_paper(user_id, "ui-audit-fixture-seen", seen=True)


def _add_fixture_paper(user_id: int, external_id: str, *, seen: bool = False) -> None:
    from datetime import UTC, datetime

    from app.modules.scrape.models import Paper, UserPaper

    paper = Paper.query.filter_by(source="manual", external_id=external_id).first()
    if paper is None:
        paper = Paper(
            source="manual",
            external_id=external_id,
            # Long on purpose. A short title fits at 280px and proves
            # nothing; the reflow bug this fixture exists to catch only
            # appears once the card holds something the length of a real
            # paper. The DOI is here for the same reason -- it is one
            # unbreakable token, which is the other way this row overflows.
            title=(
                "UI audit fixture — not a real paper, deliberately given a title "
                "as long as a real one so the card is measured at the width it "
                "actually has to survive"
            ),
            abstract="Present so the audited pages actually render a paper card.",
            authors=["Audit Fixture"],
            doi=f"10.0000/{external_id}-with-a-deliberately-long-identifier",
        )
        db.session.add(paper)
        db.session.flush()
    db.session.add(
        UserPaper(
            user_id=user_id,
            paper_id=paper.id,
            seen_at=datetime.now(UTC) if seen else None,
        )
    )
    db.session.commit()


#: Title of the synthetic learning-map report, which is also how it is found
#: again on the next run (idempotent, like the fixture papers above).
_LEARNING_MAP_FIXTURE_TITLE = "UI audit fixture — learning map"


def _learning_map_payloads() -> list[dict]:
    """A small corpus whose citations draw a real map: five concepts, each
    citing along a chain, with names as long as OpenAlex's actually are --
    a short one fits at 280px and proves nothing."""
    chain = (
        ("Spectral graph theory and its applications in machine learning", None),
        ("Neural message passing on irregular graph-structured data", 0),
        ("Graph convolutional networks", 1),
        ("Graph attention mechanisms", 1),
        ("Molecular property prediction and drug discovery", 0),
    )
    payloads = []
    for c, (concept, cites) in enumerate(chain):
        for i in range(10):
            payloads.append(
                {
                    "external_id": f"WUI{c}-{i}",
                    "title": f"{concept} — fixture paper {i}",
                    "categories": [concept],
                    "referenced_works": [f"WUI{cites}-{i}"] if cites is not None else [],
                    "published_at": f"{2018 + i % 6}-06-01",
                    "cited_by_count": 100 - i,
                    "doi": f"10.0000/ui-audit-learning-map-{c}-{i}",
                    "url": f"https://example.com/ui-audit/{c}/{i}",
                }
            )
    return payloads


def _ensure_a_learning_map(app, user_id: int) -> int:
    """A finished learning-map report, so its ordered list is audited.

    The list is the map's accessible form (docs/PHASE9.md §6) -- the one
    thing on that page a screen reader relies on -- so it must be in the
    audit before the canvas lands on top of it. Narrated, with one key work
    in the library, so every element the list can draw is drawn.
    """
    from app.modules.scrape.learning_map import learning_map_graph
    from app.modules.scrape.models import Report

    with app.app_context():
        row = Report.query.filter_by(
            user_id=user_id, kind="learning_map", title=_LEARNING_MAP_FIXTURE_TITLE
        ).first()
        if row is None:
            graph = learning_map_graph(
                _learning_map_payloads(), library_dois=["10.0000/ui-audit-learning-map-0-0"]
            )
            narration = {
                "nodes": {
                    n["id"]: {
                        "label": n["id"].split(" ")[0] + " (fixture label)",
                        "explain": "Two sentences of fixture text stand in for the narration. "
                        "They are here so the card is measured with an explanation in it.",
                        "check_question": "Could you say what this concept adds to the one before?",
                    }
                    for n in graph["nodes"]
                }
            }
            row = Report(
                user_id=user_id,
                kind="learning_map",
                title=_LEARNING_MAP_FIXTURE_TITLE,
                params={"keywords": ["graph neural networks"], "years": 5},
                status="ok",
                stats={
                    "graph": graph,
                    "query": "graph neural networks",
                    "since_year": 2018,
                    "until_year": 2023,
                },
                sections={"narration": narration},
                item_count=len(_learning_map_payloads()),
            )
            db.session.add(row)
            db.session.commit()
        return row.id


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
    _ensure_a_paper(app, int(user_id))
    # Its id is only known once the row exists, so it joins the list here.
    pages = dict(PAGES)
    pages["report-learning-map"] = f"/papers/reports/{_ensure_a_learning_map(app, int(user_id))}"

    written = 0
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = True

        for name, url in pages.items():
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
