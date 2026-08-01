"""Email gönderim servisi.

MAIL_SERVER ayarlanmamışsa (dev ortamı) email gönderilmez —
caller'a `sent=False, dev_url=<link>` döner; route'lar bunu
flash ile kullanıcıya gösterir.

Kullanım:
    from app.core.email.service import send_password_reset

    ok, dev_url = send_password_reset(user, reset_url)
    if not ok and dev_url:
        flash(f"Dev: {dev_url}", "info")
"""

import structlog
from flask import current_app, render_template
from flask_mail import Message

from app.extensions import mail

logger = structlog.get_logger()


def _send(subject: str, recipients: list[str], html_body: str, text_body: str) -> bool:
    if current_app.config.get("MAIL_SUPPRESS_SEND"):
        return False
    try:
        msg = Message(
            subject=subject,
            recipients=recipients,
            html=html_body,
            body=text_body,
            sender=current_app.config["MAIL_DEFAULT_SENDER"],
        )
        mail.send(msg)
        logger.info("email_sent", recipients=recipients, subject=subject)
        return True
    except Exception:
        logger.exception("email_send_failed", recipients=recipients)
        return False


def send_email(recipient: str, subject: str, body: str, html_body: str | None = None) -> bool:
    """Send a plain-text (optionally HTML) email to a single recipient.

    The general-purpose sender for feature emails (e.g. the digest) that don't
    have a bespoke template. Returns True only when actually delivered — in dev
    (MAIL_SUPPRESS_SEND) it returns False without raising, so callers can fire
    it best-effort. Escapes the text into a minimal HTML body when none given.
    """
    if not recipient:
        return False
    if html_body is None:
        from markupsafe import escape

        html_body = f'<pre style="font-family:inherit;white-space:pre-wrap">{escape(body)}</pre>'
    return _send(subject=subject, recipients=[recipient], html_body=html_body, text_body=body)


def send_password_reset(user, reset_url: str) -> tuple[bool, str | None]:
    """Şifre sıfırlama emaili gönderir.

    Returns:
        (True, None)         — gönderildi
        (False, reset_url)   — dev modu, link flash'ta gösterilecek
        (False, None)        — SMTP hatası
    """
    html = render_template("email/password_reset.html", user=user, reset_url=reset_url)
    text = render_template("email/password_reset.txt", user=user, reset_url=reset_url)
    sent = _send(
        subject="Şifre Sıfırlama / Password Reset",
        recipients=[user.email],
        html_body=html,
        text_body=text,
    )
    if not sent and current_app.config.get("MAIL_SUPPRESS_SEND"):
        return False, reset_url  # dev: caller flash gösterir
    return sent, None


def send_email_verification(
    user, identifier_value: str, verify_url: str
) -> tuple[bool, str | None]:
    """Akademik email doğrulama linki gönderir."""
    html = render_template(
        "email/email_verify.html",
        user=user,
        email=identifier_value,
        verify_url=verify_url,
    )
    text = render_template(
        "email/email_verify.txt",
        user=user,
        email=identifier_value,
        verify_url=verify_url,
    )
    sent = _send(
        subject="Email Doğrulama / Email Verification",
        recipients=[identifier_value],
        html_body=html,
        text_body=text,
    )
    if not sent and current_app.config.get("MAIL_SUPPRESS_SEND"):
        return False, verify_url
    return sent, None
