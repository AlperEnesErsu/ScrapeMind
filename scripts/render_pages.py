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
from contextlib import contextmanager
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

#: Pages rendered as if a model were configured. Everything else renders with
#: no key at all, as CI and a fresh checkout do.
AI_PAGES = {
    "paper-detail-tr",
    "paper-detail-ai",
    "paper-detail-ask",
    "paper-detail-graph",
    "paper-detail-ai-empty",
    "paper-detail-ask-empty",
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


def _add_fixture_paper(user_id: int, external_id: str, *, seen: bool = False) -> int:
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
            # The TeX is there so KaTeX's output is audited too -- its
            # MathML, its colours and, at 280px, whether an inline formula
            # pushes the card sideways. Written the way arXiv writes it.
            abstract=(
                "Present so the audited pages actually render a paper card, with "
                r"inline TeX such as $E_{\mathrm{inc}}/E_{\mathrm{mat}}\in[10^{-2},10^{2}]$ "
                r"and a gain of $3.58\%$, as arXiv abstracts carry it."
            ),
            authors=["Audit Fixture"],
            doi=f"10.0000/{external_id}-with-a-deliberately-long-identifier",
        )
        db.session.add(paper)
        db.session.flush()
    link = UserPaper(
        user_id=user_id,
        paper_id=paper.id,
        seen_at=datetime.now(UTC) if seen else None,
    )
    db.session.add(link)
    db.session.commit()
    return link.id


def _ensure_a_detail_paper(app, user_id: int) -> int:
    """The user-paper id of a fixture paper for the detail page.

    Its own row rather than whichever paper the library happens to hold: the
    detail page was never audited, and when it was, its mode toggle pushed the
    page 182px sideways at 280px and its note filter carried Bootstrap's cyan
    at 1.9:1. Same long title, DOI and inline TeX as the card fixture, so the
    title, the KaTeX output and the identifier row are measured here too.
    """
    from app.modules.scrape.models import Paper, UserPaper

    with app.app_context():
        link = (
            UserPaper.query.join(Paper, Paper.id == UserPaper.paper_id)
            .filter(
                UserPaper.user_id == user_id,
                Paper.source == "manual",
                Paper.external_id == "ui-audit-fixture-detail",
            )
            .first()
        )
        if link is not None:
            return link.id
        return _add_fixture_paper(user_id, "ui-audit-fixture-detail", seen=True)


#: External id of a second detail paper that is never given a translation,
#: an analysis or a conversation -- the empty states render from it.
_BARE_DETAIL_FIXTURE = "ui-audit-fixture-detail-bare"


def _ensure_detail_modes(app, link_id: int) -> None:
    """Cached AI output for the detail fixture, so every mode renders content.

    The detail page has five modes and the audit saw only the first. The other
    four show nothing of their own until a model has answered once -- the
    Turkish and analysis modes fall back to a "generate" button, Ask to an
    empty conversation -- and nothing calls a model here, so the rows a model
    would have left behind are written directly. Same shapes the generation
    paths store; obviously synthetic text; idempotent like the rest.

    The model name is a real-length one on purpose: it sits in a `<code>` in
    the footer of both panels, one unbreakable token next to a timestamp.
    """
    from datetime import UTC, datetime, timedelta

    from app.modules.scrape.models import (
        PaperAnalysis,
        PaperChatMessage,
        PaperTranslation,
        UserPaper,
    )

    model = "qwen/qwen-2.5-72b-instruct:free"
    with app.app_context():
        link = db.session.get(UserPaper, link_id)
        paper_id = link.paper_id
        if PaperTranslation.query.filter_by(paper_id=paper_id, target_lang="tr").first() is None:
            db.session.add(
                PaperTranslation(
                    paper_id=paper_id,
                    target_lang="tr",
                    title_translated=(
                        "Arayüz denetimi fikstürü — gerçek bir makale değil, kart "
                        "dayanmak zorunda olduğu genişlikte ölçülsün diye gerçek bir "
                        "makale kadar uzun başlık verildi"
                    ),
                    # The translation keeps the TeX, as the translation prompt
                    # asks the model to; KaTeX renders it in this mode too.
                    abstract_translated=(
                        "Denetlenen sayfaların gerçekten bir makale göstermesi için "
                        r"burada; $E_{\mathrm{inc}}/E_{\mathrm{mat}}\in[10^{-2},10^{2}]$ "
                        r"gibi satır içi TeX ve $3.58\%$ kazanç, arXiv özetlerinde "
                        "olduğu biçimde yazıldı."
                    ),
                    model_version=model,
                )
            )
        if PaperAnalysis.query.filter_by(paper_id=paper_id, target_lang="tr").first() is None:
            db.session.add(
                PaperAnalysis(
                    paper_id=paper_id,
                    target_lang="tr",
                    tldr=(
                        "Fikstür özeti: analiz panelinin dolu hâli ölçülsün diye "
                        "yazılmış iki cümle. Hiçbir model çağrılmadı."
                    ),
                    method=[
                        "Flask test istemcisiyle oturum açılmış sayfa üretimi",
                        "Tarayıcıda axe-core ile WCAG 2.2 A/AA denetimi",
                    ],
                    findings=[
                        "Panel 280px genişlikte yan kaydırma olmadan sığmalı",
                        "Bulgu listesi uzun bir tanımlayıcı taşıyabilir: "
                        "10.0000/ui-audit-fixture-detail-with-a-deliberately-long-identifier",
                    ],
                    limitations=["Gerçek bir modelin yanıt uzunluğunu temsil etmez"],
                    personal_relevance=(
                        "İlgi alanlarınızla bağlantı burada anlatılır; fikstürde "
                        "yalnızca yer tutucudur."
                    ),
                    model_version=model,
                    raw_response={"fixture": True},
                )
            )
        if PaperChatMessage.query.filter_by(user_paper_id=link_id).first() is None:
            # created_at set by hand: both rows land in one transaction, where
            # the server default gives them the same timestamp -- and the page
            # orders by it, so the answer could render above the question.
            asked = datetime.now(UTC) - timedelta(minutes=5)
            db.session.add_all(
                [
                    PaperChatMessage(
                        user_paper_id=link_id,
                        role="user",
                        content="Bu çalışmanın temel yöntemi ne?",
                        created_at=asked,
                    ),
                    PaperChatMessage(
                        user_paper_id=link_id,
                        role="assistant",
                        # Long, with one unbreakable token: a bubble holding a
                        # DOI is how a conversation overflows a phone.
                        content=(
                            "Fikstür yanıtı: yöntem, oturum açılmış sayfaların test "
                            "istemcisiyle üretilip tarayıcıda denetlenmesi. Kaynak: "
                            "10.0000/ui-audit-fixture-detail-with-a-deliberately-long-identifier"
                        ),
                        created_at=asked + timedelta(seconds=30),
                    ),
                ]
            )
        db.session.commit()


def _ensure_a_bare_detail_paper(app, user_id: int) -> int:
    """A detail paper with no cached AI output, for the empty states."""
    from app.modules.scrape.models import Paper, UserPaper

    with app.app_context():
        link = (
            UserPaper.query.join(Paper, Paper.id == UserPaper.paper_id)
            .filter(
                UserPaper.user_id == user_id,
                Paper.source == "manual",
                Paper.external_id == _BARE_DETAIL_FIXTURE,
            )
            .first()
        )
        if link is not None:
            return link.id
        return _add_fixture_paper(user_id, _BARE_DETAIL_FIXTURE, seen=True)


def _fixture_citation_graph(paper) -> dict:
    """What OpenAlex would return for the detail fixture, without asking it.

    Stands in for `citation_service.get_raw_citation_graph` -- the network
    half -- so the page's own endpoint still does the rest, including marking
    which nodes are already in the library. Titles as long as real ones; one
    reference shares a DOI with a library fixture, so the "in your library"
    styling is part of what is drawn.
    """
    from app.modules.scrape.citation_service import build_local_center_node

    center = build_local_center_node(paper)

    def node(i: int, kind: str) -> dict:
        return {
            "id": f"W9{i:03d}",
            "title": f"Fixture {kind} {i}: a title as long as the ones OpenAlex really returns",
            "authors": ["Audit Fixture", "Second Author"],
            "year": 2015 + i,
            "venue": "Journal of Fixture Studies",
            "citation_count": 10 * i,
            "doi": None,
            "url": f"https://example.com/ui-audit/graph/{i}",
            "type": kind,
            "is_center": False,
            "in_library": False,
            "user_paper_id": None,
        }

    refs = [node(i, "reference") for i in range(1, 5)]
    refs[0]["doi"] = "10.0000/ui-audit-fixture-with-a-deliberately-long-identifier"
    cits = [node(i, "citation") for i in range(5, 8)]
    edges = [{"source": center["id"], "target": r["id"], "type": "cites"} for r in refs]
    edges += [{"source": c["id"], "target": center["id"], "type": "cites"} for c in cits]
    return {
        "center_id": center["id"],
        "nodes": [center, *refs, *cits],
        "edges": edges,
        "stats": {
            "references_count": len(refs),
            "citations_count": len(cits),
            "total_nodes": 1 + len(refs) + len(cits),
        },
        "source": "openalex",
    }


def _inline_citation_graph(client, html: str, user_paper_id: int) -> str:
    """Hand the graph mode its data the way the app would, minus the network.

    The page fetches its graph from `data-graph-url` after load. Under
    `file://` that relative URL resolves to nothing, so the audited page would
    only ever show the "no citation data" overlay -- a state users see only
    when OpenAlex and Semantic Scholar both come back empty. Instead the
    page's own endpoint is asked here, through the test client, and its JSON
    answer goes into the attribute as a `data:` URL, which `fetch()` reads
    like any other. Only the network half of the endpoint is replaced (see
    `_fixture_citation_graph`).
    """
    import base64

    from flask import url_for

    from app.modules.scrape import citation_service

    with client.application.test_request_context():
        served = url_for("scrape.citation_graph", user_paper_id=user_paper_id)
    real = citation_service.get_raw_citation_graph
    citation_service.get_raw_citation_graph = _fixture_citation_graph
    try:
        response = client.get(served)
    finally:
        citation_service.get_raw_citation_graph = real
    if response.status_code != 200:
        raise SystemExit(f"{served} answered {response.status_code}, not 200")
    attribute = f'data-graph-url="{served}"'
    if attribute not in html:
        # Auditing the loading spinner instead would pass and prove nothing.
        raise SystemExit("graph mode no longer carries data-graph-url -- update render_pages.py")
    payload = base64.b64encode(response.get_data()).decode("ascii")
    return html.replace(attribute, f'data-graph-url="data:application/json;base64,{payload}"')


@contextmanager
def _llm_configured(app, configured: bool):
    """Render with a placeholder OpenRouter key, or with none at all.

    Set explicitly both ways: a developer's own `.env` may hold a real key or
    name another provider, and the disabled pages must not inherit either.
    """
    keys = ("LLM_PROVIDER", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")
    saved = {k: app.config.get(k) for k in keys}
    app.config["LLM_PROVIDER"] = "openrouter"
    app.config["OPENROUTER_API_KEY"] = "render-only-placeholder" if configured else ""
    app.config["ANTHROPIC_API_KEY"] = ""
    try:
        yield
    finally:
        app.config.update(saved)


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
    detail_id = _ensure_a_detail_paper(app, int(user_id))
    _ensure_detail_modes(app, detail_id)
    bare_id = _ensure_a_bare_detail_paper(app, int(user_id))
    pages["paper-detail"] = f"/papers/{detail_id}"
    # Every mode of the detail page, each with the content a user sees once a
    # model has answered (#138 audited only the first).
    pages["paper-detail-tr"] = f"/papers/{detail_id}?mode=tr"
    pages["paper-detail-ai"] = f"/papers/{detail_id}?mode=ai"
    pages["paper-detail-ask"] = f"/papers/{detail_id}?mode=chat"
    pages["paper-detail-graph"] = f"/papers/{detail_id}?mode=graph"
    # And the states before that: nothing generated yet, and no model at all.
    pages["paper-detail-ai-empty"] = f"/papers/{bare_id}?mode=ai"
    pages["paper-detail-ask-empty"] = f"/papers/{bare_id}?mode=chat"
    pages["paper-detail-ai-disabled"] = f"/papers/{detail_id}?mode=ai"
    pages["paper-detail-ask-disabled"] = f"/papers/{detail_id}?mode=chat"
    # Module blueprints serve their own static files (`/papers/static/...`).
    # Left as absolute paths they 404 under file://, so a page's own script
    # never runs and the audit sees only its no-JavaScript state -- for the
    # learning map, a hidden step card that was never measured.
    blueprint_statics: list[tuple[str, str]] = []
    with app.test_request_context():
        from flask import url_for

        for name, blueprint in app.blueprints.items():
            if blueprint.has_static_folder and blueprint.static_folder:
                served = url_for(f"{name}.static", filename="")
                local = os.path.relpath(blueprint.static_folder, out).replace(os.sep, "/") + "/"
                blueprint_statics.append((served, local))

    written = 0
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = True

        for name, url in pages.items():
            # Whether a model is configured decides which state the AI modes
            # draw. Nothing here calls one -- a GET only reads the cache -- so
            # a placeholder key is enough to stand for "configured".
            with _llm_configured(app, name in AI_PAGES):
                response = client.get(url, follow_redirects=True)
            if response.status_code != 200:
                raise SystemExit(f"{url} answered {response.status_code}, not 200")
            html = response.get_data(as_text=True)
            if name == "paper-detail-graph":
                html = _inline_citation_graph(client, html, detail_id)
            html = html.replace('href="/static/', f'href="{static_prefix}')
            html = html.replace('src="/static/', f'src="{static_prefix}')
            for served, local in blueprint_statics:
                html = html.replace(f'href="{served}', f'href="{local}')
                html = html.replace(f'src="{served}', f'src="{local}')
            (out / f"{name}.html").write_text(html, encoding="utf-8")
            written += 1
            print(f"  {name:<26} {url:<28} {len(html):>7} bytes")

    print(f"{written} page(s) written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
