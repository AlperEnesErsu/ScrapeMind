"""OAuth sign-in — the login path that had no tests at all.

Two of the three providers here (`oauth_google.py`, `oauth_microsoft.py`) sat
at 0% coverage and `auth/routes.py` at 38%, which made them the least-tested
part of the most security-critical surface in the app. They are also the part
that `authlib` runs, and authlib is the dependency carrying the most
advisories — so this file exists before that upgrade, not after it.

The assertions that carry weight are the refusals. The interesting failure in
an OAuth flow is not "login broke", it is "login worked for the wrong person":
an identity provider that asserts an email it has not verified must not be
able to walk into an existing local account.

No network: the authlib client is replaced with a stub that returns whatever
the test says the provider claimed.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.models.oauth_account import OAuthAccount
from app.core.models.user import User


class _StubOAuthClient:
    """Stands in for `oauth.google` / `oauth.microsoft`.

    Only the three methods the routes call. `authorize_redirect` returns a
    marker string rather than a real redirect because the test never follows
    it — what matters is that the route reached this point.
    """

    def __init__(self, userinfo: dict):
        self._userinfo = userinfo

    def authorize_redirect(self, redirect_uri):  # noqa: ARG002
        return f"redirect-to-provider:{redirect_uri}"

    def authorize_access_token(self):
        return {"access_token": "stub-token"}

    def userinfo(self, token=None):  # noqa: ARG002
        return self._userinfo


@pytest.fixture
def provider(monkeypatch):
    """Install a stubbed provider and hand back a way to set what it claims."""
    from app.extensions import oauth

    def _claiming(**userinfo):
        monkeypatch.setattr(oauth, "google", _StubOAuthClient(userinfo), raising=False)

    return _claiming


@pytest.fixture
def local_user(db):
    """A user who signed up with a password, as most users here did."""
    from app.core.auth.strategies.local import LocalAuthStrategy

    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"local-{suffix}",
        email=f"local-{suffix}@example.test",
        full_name="Local User",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    user_id = user.id
    yield user

    # A completed OAuth login writes audit and session rows, and the login
    # route commits them, so the `db` fixture's rollback does not take them
    # back. `tests/core/test_users.py` bulk-deletes every User, which the audit
    # row's foreign key then blocks -- the same clean-up `test_splash.py` owes
    # for the same reason.
    from sqlalchemy import text

    db.session.rollback()
    for table in ("audit_logs", "user_sessions", "oauth_accounts", "user_settings"):
        db.session.execute(text(f"DELETE FROM {table} WHERE user_id = :uid"), {"uid": user_id})
    db.session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user_id})
    db.session.commit()


def _session(client) -> dict:
    with client.session_transaction() as session:
        return dict(session)


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------


def test_an_unverified_email_cannot_take_over_a_local_account(client, db, provider, local_user):
    """The one that matters.

    A provider asserting `email_verified: false` for an address that already
    belongs to a password account must not be handed that account. Anyone who
    can register that address at any identity provider would otherwise inherit
    the ScrapeMind account attached to it.
    """
    provider(sub="attacker-subject", email=local_user.email, name="Not Them", email_verified=False)

    client.get("/auth/oauth/google/callback", follow_redirects=True)

    assert "_user_id" not in _session(client), "must not sign anyone in"
    assert OAuthAccount.query.filter_by(provider_user_id="attacker-subject").first() is None


def test_a_missing_verified_claim_counts_as_unverified(client, db, provider, local_user):
    """Absence of the claim is not consent. A provider that says nothing about
    verification is treated as having said no."""
    provider(sub="quiet-subject", email=local_user.email, name="Not Them")

    client.get("/auth/oauth/google/callback", follow_redirects=True)

    assert "_user_id" not in _session(client)


def test_auto_registration_is_refused_when_the_setting_is_off(client, db, provider):
    """Default-off: an unknown email should not silently become an account."""
    provider(
        sub="new-subject",
        email=f"stranger-{uuid.uuid4().hex[:8]}@example.test",
        name="A Stranger",
        email_verified=True,
    )

    client.get("/auth/oauth/google/callback", follow_redirects=True)

    assert "_user_id" not in _session(client)


def test_an_unknown_provider_is_refused_without_crashing(client, db):
    """A hand-typed URL must not 500."""
    response = client.get("/auth/oauth/nosuchprovider/callback", follow_redirects=True)

    assert response.status_code == 200
    assert "_user_id" not in _session(client)


# --------------------------------------------------------------------------
# The paths that should work
# --------------------------------------------------------------------------


def test_a_verified_email_links_to_the_existing_account(client, db, provider, local_user):
    """The counterpart of the takeover test: with the claim present and true,
    linking is the intended behaviour."""
    provider(
        sub="verified-subject",
        email=local_user.email,
        name=local_user.full_name,
        email_verified=True,
    )

    client.get("/auth/oauth/google/callback", follow_redirects=True)

    assert _session(client).get("_user_id") == str(local_user.id)
    linked = OAuthAccount.query.filter_by(provider_user_id="verified-subject").first()
    assert linked is not None and linked.user_id == local_user.id


def test_an_already_linked_account_signs_straight_in(client, db, provider, local_user):
    """Second and subsequent logins take the short path — no email matching,
    no verification question, because the link already exists."""
    db.session.add(
        OAuthAccount(
            user_id=local_user.id,
            provider="google",
            provider_user_id="known-subject",
            email=local_user.email,
        )
    )
    db.session.commit()

    # Deliberately unverified: the claim is irrelevant once linked.
    provider(sub="known-subject", email=local_user.email, name="x", email_verified=False)

    client.get("/auth/oauth/google/callback", follow_redirects=True)

    assert _session(client).get("_user_id") == str(local_user.id)


def test_a_2fa_user_is_sent_to_the_challenge_not_signed_in(client, db, provider, local_user):
    """OAuth must not be a way around the second factor."""
    # `is_totp_enabled` is derived, not stored — set what it is derived from.
    from datetime import UTC, datetime

    local_user.totp_secret = "JBSWY3DPEHPK3PXP"
    local_user.totp_enabled_at = datetime.now(UTC)
    db.session.commit()
    assert local_user.is_totp_enabled

    db.session.add(
        OAuthAccount(
            user_id=local_user.id,
            provider="google",
            provider_user_id="totp-subject",
            email=local_user.email,
        )
    )
    db.session.commit()

    provider(sub="totp-subject", email=local_user.email, name="x", email_verified=True)

    from app.core.auth.routes import PENDING_2FA_KEY

    response = client.get("/auth/oauth/google/callback", follow_redirects=False)
    session = _session(client)

    assert "_user_id" not in session, "not signed in until the second factor"
    assert session.get(PENDING_2FA_KEY) == local_user.id
    assert "/auth/2fa" in response.headers.get("Location", "")


# --------------------------------------------------------------------------
# Redirect entry points
# --------------------------------------------------------------------------


def test_the_login_redirect_clears_any_stale_link_mode(client, db, provider):
    """`_oauth_mode` left over from an abandoned link attempt must not turn the
    next plain login into a link."""
    provider(sub="x", email="x@example.test", name="x")
    with client.session_transaction() as session:
        session["_oauth_mode"] = "link"
        session["_oauth_link_user_id"] = 999

    client.get("/auth/oauth/google")

    assert "_oauth_mode" not in _session(client)
