"""The dashboard a non-admin user actually sees.

Admins are redirected to the admin overview, so every check here has to run as
an ordinary user — which is exactly why these defects survived: the page was
never the one being looked at.
"""

from __future__ import annotations

from app.modules.scrape.models import Paper, UserPaper


def _give_a_paper(db, user_id: int) -> None:
    paper = Paper(
        source="manual",
        external_id=f"student-fixture-{user_id}",
        title="A paper, so the briefing has something to be written from",
    )
    db.session.add(paper)
    db.session.flush()
    db.session.add(UserPaper(user_id=user_id, paper_id=paper.id))
    db.session.commit()


# --------------------------------------------------------------------------
# The interest counters
# --------------------------------------------------------------------------


def test_adding_an_interest_refreshes_every_counter(auth_client, db):
    """Three counters, one number.

    The interest count appears in the header pill, the stat strip and the
    manager card. The HTMX swap replaced only the card, so the other two went
    on saying 0 while the card said 1 — until the user happened to reload.
    """
    client, user_id = auth_client

    response = client.post(
        "/interests/add",
        data={"value": "kuantum hesaplama"},
        headers={"HX-Request": "true"},
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="interest-count-pill"' in html, "the header pill must ride along"
    assert 'id="interest-count-value"' in html, "the stat strip must ride along"
    assert html.count('hx-swap-oob="true"') == 2


def test_the_out_of_band_counters_carry_the_new_number(auth_client, db):
    """Present is not the same as correct."""
    client, user_id = auth_client

    client.post("/interests/add", data={"value": "biyoenformatik"}, headers={"HX-Request": "true"})
    html = client.post(
        "/interests/add",
        data={"value": "kuantum hesaplama"},
        headers={"HX-Request": "true"},
    ).get_data(as_text=True)

    marker = html.index('id="interest-count-value"')
    assert ">2<" in html[marker : marker + 120]


def test_a_normal_post_still_redirects(auth_client, db):
    """The out-of-band fragment is for HTMX only — without the header this is
    an ordinary form post and must not leak partial markup."""
    client, _ = auth_client

    response = client.post("/interests/add", data={"value": "sürdürülebilirlik"})

    assert response.status_code == 302


# --------------------------------------------------------------------------
# The briefing button
# --------------------------------------------------------------------------


def test_no_papers_means_no_briefing_button(auth_client, db):
    """`digest.run_for_user` correctly declines an empty run, but the button
    promised one anyway and the flash said it was on its way. The user waited
    for something that was never coming: the task was right, the button lied."""
    client, _ = auth_client

    html = client.get("/", follow_redirects=True).get_data(as_text=True)

    assert "/digest/run" not in html
    assert "run a scrape first" in html or "tarama çalıştır" in html


def test_with_papers_the_button_is_there(auth_client, db):
    client, user_id = auth_client
    _give_a_paper(db, user_id)

    html = client.get("/", follow_redirects=True).get_data(as_text=True)

    assert "/digest/run" in html
