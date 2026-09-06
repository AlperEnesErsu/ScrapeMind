"""Author following: ORCID resolution, the follow service, ingestion.

The high-water mark (`UserAuthor.last_work_at`) is the load-bearing part: it
is what stops a prolific author's back catalogue being re-imported every
night.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests
from flask_babel import force_locale
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from app.core.models.user import User
from app.modules.scrape import service
from app.modules.scrape.models import UserAuthor
from app.modules.scrape.sources import openalex_source as oa
from app.modules.scrape.sources.payload import PaperPayload

_ORCID = "0000-0002-1825-0097"
_AUTHOR_ID = "A5023888391"


class _Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(oa, "openalex_slot", lambda: True)


@pytest.fixture
def a_user(db):
    user = User.query.filter_by(email="authoruser@example.test").first()
    if user is None:
        user = User(
            username="authoruser",
            email="authoruser@example.test",
            full_name="Author User",
            password_hash=generate_password_hash("password123"),
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
    uid = user.id

    yield user

    db.session.rollback()
    # author_group_members has no user_id of its own — go through its parent
    # group, and delete it before author_groups/user_authors (the two tables
    # it FKs to).
    db.session.execute(
        text(
            "DELETE FROM author_group_members WHERE group_id IN "
            "(SELECT id FROM author_groups WHERE user_id = :uid)"
        ),
        {"uid": uid},
    )
    db.session.execute(text("DELETE FROM author_groups WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM reports WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM user_authors WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM user_papers WHERE user_id = :uid"), {"uid": uid})
    db.session.execute(text("DELETE FROM audit_logs WHERE user_id = :uid"), {"uid": uid})
    db.session.query(User).filter_by(id=uid).delete()
    db.session.commit()


@pytest.fixture
def clean_papers(db):
    yield
    db.session.rollback()
    db.session.execute(text("DELETE FROM user_papers"))
    db.session.execute(text("DELETE FROM papers"))
    db.session.commit()


def _author_json(name="Jane Smith", *, institution=None, cited_by_count=None):
    data = {
        "id": f"https://openalex.org/{_AUTHOR_ID}",
        "display_name": name,
        "orcid": f"https://orcid.org/{_ORCID}",
        "works_count": 42,
    }
    if institution is not None:
        data["last_known_institutions"] = [{"display_name": institution}]
    if cited_by_count is not None:
        data["cited_by_count"] = cited_by_count
    return data


def _work_payload(ext="W1", published=None):
    return PaperPayload(
        source="openalex",
        external_id=ext,
        title=f"Work {ext}",
        abstract="abs",
        authors=["Jane Smith"],
        url="https://example.test/w",
        pdf_url=None,
        published_at=published or datetime(2025, 3, 1, tzinfo=UTC),
        categories=[],
    )


# ----------------------------------------------------------------------------
# ORCID normalisation
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        _ORCID,
        f"https://orcid.org/{_ORCID}",
        f"http://orcid.org/{_ORCID}",
        f"orcid.org/{_ORCID}",
        f"  {_ORCID}  ",
        "0000000218250097",  # no hyphens
    ],
)
def test_orcid_forms_all_normalise(raw):
    """Users paste the URL as often as the bare id, and sometimes without
    hyphens."""
    assert oa.normalize_orcid(raw) == _ORCID


def test_orcid_x_check_digit_is_valid():
    assert oa.normalize_orcid("0000-0002-1825-009X") == "0000-0002-1825-009X"


@pytest.mark.parametrize("bad", ["", None, "not-an-orcid", "1234", "0000-0002-1825"])
def test_malformed_orcids_are_rejected(bad):
    assert oa.normalize_orcid(bad) is None


# ----------------------------------------------------------------------------
# fetch_author
# ----------------------------------------------------------------------------


def test_fetch_author_by_orcid_uses_the_orcid_url(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp(_author_json())

    monkeypatch.setattr(oa._session, "get", fake_get)
    got = oa.fetch_author(f"https://orcid.org/{_ORCID}")

    assert f"orcid.org/{_ORCID}" in seen["url"]
    assert got == {
        "id": _AUTHOR_ID,
        "name": "Jane Smith",
        "orcid": _ORCID,
        "works_count": 42,
        "institution": None,
        "cited_by_count": None,
    }


def test_fetch_author_by_openalex_id(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        return _Resp(_author_json())

    monkeypatch.setattr(oa._session, "get", fake_get)
    assert oa.fetch_author(_AUTHOR_ID)["id"] == _AUTHOR_ID
    assert seen["url"].endswith(f"/{_AUTHOR_ID}")


def test_fetch_author_unknown_identifier_is_none(monkeypatch):
    """404 is OpenAlex's answer for an unknown ORCID — a typo, not a
    failure."""
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(status_code=404))
    assert oa.fetch_author(_ORCID) is None


def test_fetch_author_rejects_garbage_without_calling(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not have called OpenAlex")

    monkeypatch.setattr(oa._session, "get", boom)
    assert oa.fetch_author("just a name") is None
    assert oa.fetch_author("") is None


def test_fetch_author_server_error_propagates(monkeypatch):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(status_code=500))
    with pytest.raises(requests.HTTPError):
        oa.fetch_author(_ORCID)


# ----------------------------------------------------------------------------
# works_by_author
# ----------------------------------------------------------------------------


def test_works_by_author_filters_on_the_author(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs["params"])
        return _Resp({"results": []})

    monkeypatch.setattr(oa._session, "get", fake_get)
    oa.works_by_author(_AUTHOR_ID)

    assert f"author.id:{_AUTHOR_ID}" in seen["filter"]
    assert seen["sort"] == "publication_date:desc"


def test_works_by_author_passes_the_high_water_mark(monkeypatch):
    """The whole point: ask only for what is newer than what we have."""
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs["params"])
        return _Resp({"results": []})

    monkeypatch.setattr(oa._session, "get", fake_get)
    oa.works_by_author(_AUTHOR_ID, since=datetime(2025, 3, 1, tzinfo=UTC))

    assert "from_publication_date:2025-03-01" in seen["filter"]


def test_works_by_author_without_a_mark_asks_for_everything(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs["params"])
        return _Resp({"results": []})

    monkeypatch.setattr(oa._session, "get", fake_get)
    oa.works_by_author(_AUTHOR_ID)
    assert "from_publication_date" not in seen["filter"]


def test_works_by_author_needs_an_id(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not have called OpenAlex")

    monkeypatch.setattr(oa._session, "get", boom)
    assert oa.works_by_author("") == []


# ----------------------------------------------------------------------------
# fetch_by_doi (Scopus hydration)
# ----------------------------------------------------------------------------


def test_fetch_by_doi_returns_a_payload(monkeypatch):
    monkeypatch.setattr(
        oa._session,
        "get",
        lambda *a, **k: _Resp(
            {
                "id": "https://openalex.org/W99",
                "display_name": "Hydrated",
                "doi": "https://doi.org/10.1234/abc",
            }
        ),
    )
    payload = oa.fetch_by_doi("10.1234/abc")
    assert payload.title == "Hydrated"
    assert payload.doi == "10.1234/abc"


def test_fetch_by_doi_unknown_is_none(monkeypatch):
    """OpenAlex does not have everything; a miss is normal."""
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(status_code=404))
    assert oa.fetch_by_doi("10.1234/abc") is None


def test_fetch_by_doi_rejects_a_non_doi(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not have called OpenAlex")

    monkeypatch.setattr(oa._session, "get", boom)
    assert oa.fetch_by_doi("not a doi") is None


# ----------------------------------------------------------------------------
# follow / unfollow
# ----------------------------------------------------------------------------


def test_follow_stores_the_resolved_ids(app, db, a_user, monkeypatch):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))

    row, error = service.follow_author(a_user, f"https://orcid.org/{_ORCID}")
    assert error is None
    assert row.openalex_id == _AUTHOR_ID
    assert row.orcid == _ORCID
    assert row.author_name == "Jane Smith"
    assert row.active is True


def test_follow_rejects_blank_input(app, db, a_user):
    row, error = service.follow_author(a_user, "   ")
    assert row is None and error


def test_follow_reports_an_unknown_author(app, db, a_user, monkeypatch):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(status_code=404))
    row, error = service.follow_author(a_user, _ORCID)
    assert row is None
    # The message is a lazy translatable now, so it renders in whatever
    # locale is active when it is stringified — outside a request that is
    # BABEL_DEFAULT_LOCALE ("tr" here). Pin the locale instead of asserting
    # on whichever language the test config happens to default to.
    with force_locale("en"):
        assert "No author found" in str(error)


def test_follow_surfaces_a_lookup_failure_as_a_message(app, db, a_user, monkeypatch):
    """A network failure is user-facing here, not a 500 — resolution happens
    in front of the user precisely so they can see it."""

    def boom(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(oa._session, "get", boom)
    row, error = service.follow_author(a_user, _ORCID)
    assert row is None
    assert "OpenAlex" in error


def test_refollowing_reactivates_instead_of_duplicating(app, db, a_user, monkeypatch):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))

    row, _ = service.follow_author(a_user, _ORCID)
    service.toggle_user_author(a_user, row.id)
    assert row.active is False

    again, error = service.follow_author(a_user, _ORCID)
    assert error is None
    assert again.id == row.id
    assert again.active is True
    assert service.count_user_authors(a_user) == 1


def test_a_name_clash_is_disambiguated(app, db, a_user, monkeypatch):
    """The table's unique constraint predates openalex_id, and two different
    authors can share a display name."""
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    service.follow_author(a_user, _ORCID)

    other = dict(_author_json(), id="https://openalex.org/A999")
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(other))
    row, error = service.follow_author(a_user, "A999")

    assert error is None
    assert row.author_name != "Jane Smith"
    assert "A999" in row.author_name


def test_cap_is_enforced(app, db, a_user, monkeypatch):
    monkeypatch.setattr(service, "MAX_USER_AUTHORS", 1)
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    service.follow_author(a_user, _ORCID)

    monkeypatch.setattr(
        oa._session, "get", lambda *a, **k: _Resp(dict(_author_json("Other"), id="A999"))
    )
    row, error = service.follow_author(a_user, "A999")
    assert row is None
    assert str(error) == str(service.AUTHOR_CAP_MESSAGE)
    with force_locale("en"):
        assert "limit" in str(error).lower()


def test_unfollow_removes_the_row(app, db, a_user, monkeypatch):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    row, _ = service.follow_author(a_user, _ORCID)

    assert service.unfollow_author(a_user, row.id) is True
    assert service.count_user_authors(a_user) == 0


def test_unfollow_of_another_users_row_is_refused(app, db, a_user, monkeypatch):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    row, _ = service.follow_author(a_user, _ORCID)

    other = User(
        username="otherauthoruser",
        email="otherauthor@example.test",
        full_name="Other Author User",
        password_hash=generate_password_hash("x"),
        is_active=True,
    )
    db.session.add(other)
    db.session.commit()
    try:
        assert service.unfollow_author(other, row.id) is False
        assert service.count_user_authors(a_user) == 1
    finally:
        db.session.delete(other)
        db.session.commit()


# ----------------------------------------------------------------------------
# Ingestion
# ----------------------------------------------------------------------------


def test_ingest_without_follows_is_skipped(app, db, a_user):
    assert service.ingest_user_authors(a_user)["reason"] == "no_authors"


def test_ingest_links_works_and_advances_the_mark(app, db, a_user, monkeypatch, clean_papers):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    row, _ = service.follow_author(a_user, _ORCID)

    monkeypatch.setattr(
        service,
        "list_user_authors",
        lambda user: [row],
    )
    monkeypatch.setattr(
        oa,
        "works_by_author",
        lambda aid, **kw: [
            _work_payload("W1", datetime(2025, 3, 1, tzinfo=UTC)),
            _work_payload("W2", datetime(2025, 6, 1, tzinfo=UTC)),
        ],
    )

    result = service.ingest_user_authors(a_user)
    assert result["hits"] == 2
    assert result["linked"] == 2
    assert row.last_work_at == datetime(2025, 6, 1, tzinfo=UTC)


def test_second_run_asks_only_for_newer_works(app, db, a_user, monkeypatch, clean_papers):
    """Without the high-water mark a prolific author re-imports a career every
    night."""
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    row, _ = service.follow_author(a_user, _ORCID)
    row.last_work_at = datetime(2025, 6, 1, tzinfo=UTC)
    db.session.commit()

    seen = {}

    def fake_works(aid, *, since=None, max_results=25):
        seen["since"] = since
        return []

    monkeypatch.setattr(service, "list_user_authors", lambda user: [row])
    monkeypatch.setattr(oa, "works_by_author", fake_works)

    service.ingest_user_authors(a_user)
    assert seen["since"] == datetime(2025, 6, 1, tzinfo=UTC)


def test_a_failing_author_uses_the_sentinel(app, db, a_user, monkeypatch, clean_papers):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    row, _ = service.follow_author(a_user, _ORCID)

    def boom(*args, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(service, "list_user_authors", lambda user: [row])
    monkeypatch.setattr(oa, "works_by_author", boom)

    result = service.ingest_user_authors(a_user)
    assert result["sources"][row.author_name] == -1


def test_paused_and_unresolved_follows_are_skipped(app, db, a_user, monkeypatch, clean_papers):
    monkeypatch.setattr(oa._session, "get", lambda *a, **k: _Resp(_author_json()))
    paused, _ = service.follow_author(a_user, _ORCID)
    paused.active = False
    unresolved = UserAuthor(user_id=a_user.id, author_name="Unresolved", openalex_id=None)
    db.session.add(unresolved)
    db.session.commit()

    def boom(*args, **kwargs):
        raise AssertionError("should not have queried a skipped author")

    monkeypatch.setattr(oa, "works_by_author", boom)
    assert service.ingest_user_authors(a_user)["reason"] == "no_authors"
