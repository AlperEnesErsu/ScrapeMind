"""Zotero export (Faz 7.2).

The tests that carry weight are the idempotency ones. This is the first write
this application makes to somebody else's data, and the failure that matters is
not "the export broke" -- it is "the export worked twice and now there are two
of everything in the user's library, and we cannot see it to clean up".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import requests

from app.modules.scrape import zotero
from app.modules.scrape.models import Paper


@pytest.fixture
def user(db):
    from app.core.models.user import User

    suffix = uuid.uuid4().hex[:8]
    u = User(
        username=f"zot-{suffix}",
        email=f"zot-{suffix}@example.test",
        full_name="Zotero tester",
        password_hash="x",
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def paper(db):
    def _make(**kwargs) -> Paper:
        defaults = {
            "source": "openalex",
            "external_id": f"W{uuid.uuid4().hex[:12]}",
            "title": "Pomegranate-shaped heat spreaders",
            "abstract": "We describe a spreader.",
            "authors": ["Ada Lovelace", "Grace Hopper"],
            "doi": "10.1234/abcd",
            "url": "https://doi.org/10.1234/abcd",
            "published_at": datetime(2026, 3, 1, tzinfo=UTC),
            "categories": ["Thermal", "Packaging"],
        }
        p = Paper(**{**defaults, **kwargs})
        db.session.add(p)
        db.session.commit()
        return p

    return _make


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def test_credentials_round_trip_without_storing_plaintext(db, user):
    zotero.set_credentials(user, "zk-secret-value", "123456")

    stored = (user.settings.settings or {})["zotero"]
    assert "zk-secret-value" not in str(stored), "the key must not be stored in the clear"

    assert zotero.get_credentials(user) == ("zk-secret-value", "123456")
    assert zotero.is_configured(user) is True


def test_clearing_the_key_removes_the_whole_block(db, user):
    """ "Configured" has to stay one unambiguous condition -- an empty string
    left behind would make `is_configured` true with nothing to send."""
    zotero.set_credentials(user, "zk-secret", "123456")
    zotero.set_credentials(user, "", "123456")

    assert "zotero" not in (user.settings.settings or {})
    assert zotero.is_configured(user) is False


def test_a_key_without_a_user_id_is_not_configured(db, user):
    """Zotero needs both; half a credential is not a credential."""
    zotero.set_credentials(user, "zk-secret", "")
    assert zotero.is_configured(user) is False


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------


def test_item_carries_the_marker_that_makes_export_idempotent(db, paper):
    item = zotero.paper_to_item(paper())
    assert f"{zotero.MARKER_PREFIX}" in item["extra"]
    assert item["itemType"] == "journalArticle"


def test_a_name_that_will_not_split_goes_in_whole(db, paper):
    """ "van der Berg" is not first name "van der" and last name "Berg".

    Zotero supports a single-field name for exactly this, and using it beats
    confidently mis-parsing somebody's name.
    """
    item = zotero.paper_to_item(paper(authors=["Ada Lovelace", "Prince", "van der Berg"]))
    creators = item["creators"]
    assert creators[0] == {
        "creatorType": "author",
        "firstName": "Ada",
        "lastName": "Lovelace",
    }
    assert creators[1] == {"creatorType": "author", "name": "Prince"}
    # Three words split into "van der" + "Berg", which is wrong but is what a
    # last-space split gives; recorded here so the behaviour is a decision
    # rather than a surprise.
    assert creators[2]["lastName"] == "Berg"


def test_a_collection_is_only_set_when_asked_for(db, paper):
    assert "collections" not in zotero.paper_to_item(paper())
    assert zotero.paper_to_item(paper(), collection_key="ABCD1234")["collections"] == ["ABCD1234"]


# --------------------------------------------------------------------------
# Idempotency -- the reason this module is careful
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, *, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_an_already_exported_paper_is_updated_not_duplicated(db, user, paper, monkeypatch):
    """The failure this module exists to prevent.

    A second export must reuse the Zotero item key rather than posting a new
    item -- otherwise running it twice leaves two copies in a library we cannot
    see into.
    """
    zotero.set_credentials(user, "zk", "9999")
    p = paper()

    monkeypatch.setattr(zotero, "find_existing", lambda *a, **kw: {p.id: "EXISTINGKEY"})

    posted = []

    def _post(url, headers=None, json=None, timeout=None):
        posted.append(json)
        return _Resp(payload={"successful": {"0": {}}})

    monkeypatch.setattr(zotero.requests, "post", _post)

    result = zotero.export_papers(user, [p])

    assert result.updated == 1
    assert result.created == 0
    assert posted[0][0]["key"] == "EXISTINGKEY", "must update through the existing key"


def test_a_new_paper_is_created_without_a_key(db, user, paper, monkeypatch):
    zotero.set_credentials(user, "zk", "9999")
    p = paper()
    monkeypatch.setattr(zotero, "find_existing", lambda *a, **kw: {})

    posted = []
    monkeypatch.setattr(
        zotero.requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: (
            posted.append(json) or _Resp(payload={"successful": {"0": {}}})
        ),
    )

    result = zotero.export_papers(user, [p])
    assert result.created == 1
    assert "key" not in posted[0][0]


def test_find_existing_reads_the_marker_back(db, monkeypatch):
    payload = [
        {"key": "AAA", "data": {"key": "AAA", "extra": f"{zotero.MARKER_PREFIX} 42\nSource: x"}},
        {"key": "BBB", "data": {"key": "BBB", "extra": "no marker here"}},
        {"key": "CCC", "data": {"key": "CCC", "extra": f"{zotero.MARKER_PREFIX} 99"}},
    ]
    monkeypatch.setattr(zotero.requests, "get", lambda *a, **kw: _Resp(payload=payload))
    assert zotero.find_existing("zk", "1", [42, 43]) == {42: "AAA"}


# --------------------------------------------------------------------------
# Partial failure
# --------------------------------------------------------------------------


def test_a_partly_written_batch_is_reported_not_swallowed(db, user, paper, monkeypatch):
    """Zotero answers 200 with a per-item breakdown, so "it returned OK" is
    not the same as "everything was written"."""
    zotero.set_credentials(user, "zk", "9999")
    papers = [paper() for _ in range(3)]
    monkeypatch.setattr(zotero, "find_existing", lambda *a, **kw: {})
    monkeypatch.setattr(
        zotero.requests,
        "post",
        lambda *a, **kw: _Resp(
            payload={"successful": {"0": {}}, "failed": {"1": "invalid field", "2": "too long"}}
        ),
    )

    result = zotero.export_papers(user, papers)

    assert result.created == 1
    assert result.failed == 2
    assert result.ok is False
    assert len(result.errors) == 2


def test_a_refused_key_is_an_error_not_a_silent_zero(db, user, paper, monkeypatch):
    zotero.set_credentials(user, "zk", "9999")
    monkeypatch.setattr(zotero.requests, "get", lambda *a, **kw: _Resp(status=403))
    with pytest.raises(zotero.ZoteroError, match="refused"):
        zotero.export_papers(user, [paper()])


def test_an_unreachable_zotero_is_an_error(db, user, paper, monkeypatch):
    zotero.set_credentials(user, "zk", "9999")

    def _boom(*a, **kw):
        raise requests.ConnectionError("dns")

    monkeypatch.setattr(zotero.requests, "get", _boom)
    with pytest.raises(zotero.ZoteroError, match="could not reach"):
        zotero.export_papers(user, [paper()])


def test_exporting_without_credentials_refuses(db, user, paper):
    with pytest.raises(zotero.ZoteroError, match="no Zotero credentials"):
        zotero.export_papers(user, [paper()])


def test_exporting_nothing_writes_nothing(db, user, monkeypatch):
    zotero.set_credentials(user, "zk", "9999")
    called = []
    monkeypatch.setattr(zotero.requests, "get", lambda *a, **kw: called.append(1))
    monkeypatch.setattr(zotero.requests, "post", lambda *a, **kw: called.append(1))

    result = zotero.export_papers(user, [])
    assert result.total == 0
    assert called == [], "an empty export must not touch the network"
