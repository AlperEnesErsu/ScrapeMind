"""Email service smoke tests.

We do not assert on the SMTP transport itself — Flask-Mail's own tests
cover that. The goal here is to keep our two public helpers honest:

  * dev fallback returns the link instead of crashing when MAIL_SUPPRESS_SEND
  * a configured SMTP path actually invokes mail.send()
  * the rendered templates exist and do not blow up
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.core.email.service import send_email_verification, send_password_reset
from app.extensions import mail


def _fake_user():
    # Templates only need .full_name and .email — using a plain namespace
    # keeps these tests independent of the User model's column changes.
    return SimpleNamespace(full_name="Test User", email="user@example.test")


def test_password_reset_dev_mode_returns_url(app):
    app.config["MAIL_SUPPRESS_SEND"] = True
    sent, dev_url = send_password_reset(_fake_user(), "https://example.test/reset/abc")
    assert sent is False
    assert dev_url == "https://example.test/reset/abc"


def test_password_reset_calls_smtp_when_configured(app):
    app.config["MAIL_SUPPRESS_SEND"] = False
    with patch.object(mail, "send") as send:
        sent, dev_url = send_password_reset(_fake_user(), "https://example.test/reset/abc")
    assert sent is True
    assert dev_url is None
    assert send.call_count == 1
    msg = send.call_args.args[0]
    assert msg.recipients == ["user@example.test"]
    assert "reset" in (msg.body or "").lower() or "sifre" in (msg.body or "").lower()


def test_email_verification_dev_mode_returns_url(app):
    app.config["MAIL_SUPPRESS_SEND"] = True
    sent, dev_url = send_email_verification(
        _fake_user(), "academic@example.test", "https://example.test/verify/xyz"
    )
    assert sent is False
    assert dev_url == "https://example.test/verify/xyz"


def test_email_verification_calls_smtp_when_configured(app):
    app.config["MAIL_SUPPRESS_SEND"] = False
    with patch.object(mail, "send") as send:
        sent, _ = send_email_verification(
            _fake_user(), "academic@example.test", "https://example.test/verify/xyz"
        )
    assert sent is True
    msg = send.call_args.args[0]
    assert msg.recipients == ["academic@example.test"]
