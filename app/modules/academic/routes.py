"""Academic module routes.

verify-email buraya taşındı (core/auth/routes.py'den).
identifiers + interests profil tab rotaları buraya taşındı (core/settings/routes.py'den).

Kural: core, modülü import edemez — her iki yönlü bağımlılık da yasaktır.
Modül → core import: OK.  Core → modül import: YASAK.
"""

from flask import abort, flash, redirect, render_template, url_for
from flask_babel import _
from flask_login import current_user, login_required

from app.core.audit.middleware import log_action
from app.extensions import db, limiter
from app.modules.academic import academic_bp
from app.modules.academic.forms import AddIdentifierForm, AddKeywordForm
from app.modules.academic.models import IdentifierType, UserIdentifier
from app.modules.academic.service import (
    add_identifier,
    add_user_keywords_bulk,
    delete_identifier,
    list_user_identifiers,
    list_user_keywords,
    make_email_verify_token,
    remove_user_keyword,
    verify_email_token,
)

# ------------------------------------------------------------------ #
# Profil tab context builders (modül tarafı)
# ------------------------------------------------------------------ #


def _identifiers_ctx():
    form = AddIdentifierForm()
    types = (
        IdentifierType.query.filter(IdentifierType.deleted_at.is_(None))
        .order_by(IdentifierType.name)
        .all()
    )
    form.type_code.choices = [(t.code, t.name) for t in types] or [("email", "Email")]
    return {"form": form, "identifiers": list_user_identifiers(current_user)}


def _interests_ctx():
    return {"form": AddKeywordForm(), "keywords": list_user_keywords(current_user)}


def _register_tabs():
    """Tab registry'ye academic tablarını ekle — uygulama başlarken çağrılır."""
    from app.core.settings.tab_registry import register_profile_tab

    register_profile_tab("identifiers", "bi-person-vcard", "Academic Identifiers", _identifiers_ctx)
    register_profile_tab("interests", "bi-tags", "Research Interests", _interests_ctx)


# ------------------------------------------------------------------ #
# Email doğrulama (core/auth'dan taşındı)
# ------------------------------------------------------------------ #


@academic_bp.route("/verify-email/<token>")
@limiter.limit("10 per minute")
def verify_email(token: str):
    ident = verify_email_token(token)
    if ident is None:
        flash(_("Verification link is invalid or expired."), "danger")
    else:
        log_action(
            "user.email_verified",
            entity_type="user_identifier",
            entity_id=str(ident.id),
            changes={"email": ident.value},
        )
        flash(_("Email verified."), "success")
    if current_user.is_authenticated:
        return redirect(url_for("settings.profile", tab="identifiers"))
    return redirect(url_for("auth.login"))


# ------------------------------------------------------------------ #
# Profil tab POST rotaları (core/settings'ten taşındı)
# ------------------------------------------------------------------ #


def _render_tab(tab: str, **ctx):
    return render_template(f"settings/_tab_{tab}.html", active_tab=tab, **ctx)


@academic_bp.route("/profile/identifiers/add", methods=["POST"])
@login_required
def submit_identifier_add():
    form = AddIdentifierForm()
    types = IdentifierType.query.filter(IdentifierType.deleted_at.is_(None)).all()
    form.type_code.choices = [(t.code, t.name) for t in types]

    if form.validate_on_submit():
        ident, err = add_identifier(current_user, form.type_code.data, form.value.data)
        if err:
            return _render_tab(
                "identifiers", flash_msg=_(err), flash_kind="danger", **_identifiers_ctx()
            )
        log_action(
            "user.identifier_added",
            entity_type="user_identifier",
            entity_id=str(ident.id),
            changes={"type": form.type_code.data, "value": ident.value},
        )
        if form.type_code.data == "email":
            token = make_email_verify_token(ident)
            verify_url = url_for("academic.verify_email", token=token, _external=True)
            from flask import current_app

            from app.core.email.service import send_email_verification

            sent, dev_url = send_email_verification(current_user, ident.value, verify_url)
            if not sent and dev_url and current_app.debug:
                return _render_tab(
                    "identifiers",
                    flash_msg=_("Dev mode: verification link → %(url)s", url=dev_url),
                    flash_kind="info",
                    **_identifiers_ctx(),
                )
            return _render_tab(
                "identifiers",
                flash_msg=_("Identifier added — a verification link has been sent."),
                flash_kind="success",
                **_identifiers_ctx(),
            )
        return _render_tab(
            "identifiers",
            flash_msg=_("Identifier added."),
            flash_kind="success",
            **_identifiers_ctx(),
        )
    return _render_tab(
        "identifiers",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
        **_identifiers_ctx(),
    )


@academic_bp.route("/profile/identifiers/<int:ident_id>/delete", methods=["POST"])
@login_required
def submit_identifier_delete(ident_id: int):
    ok, err = delete_identifier(current_user, ident_id)
    if not ok:
        return _render_tab(
            "identifiers", flash_msg=_(err), flash_kind="danger", **_identifiers_ctx()
        )
    log_action("user.identifier_deleted", entity_type="user_identifier", entity_id=str(ident_id))
    return _render_tab(
        "identifiers",
        flash_msg=_("Identifier removed."),
        flash_kind="success",
        **_identifiers_ctx(),
    )


@academic_bp.route("/profile/identifiers/<int:ident_id>/resend", methods=["POST"])
@login_required
def submit_identifier_resend(ident_id: int):
    ident = db.session.get(UserIdentifier, ident_id)
    if ident is None or ident.user_id != current_user.id or ident.is_verified:
        abort(404)
    if ident.type.code != "email":
        abort(404)
    token = make_email_verify_token(ident)
    verify_url = url_for("academic.verify_email", token=token, _external=True)
    log_action(
        "user.identifier_verify_resent", entity_type="user_identifier", entity_id=str(ident_id)
    )
    from flask import current_app

    if current_app.debug:
        return _render_tab(
            "identifiers",
            flash_msg=_("Dev mode: verification link → %(url)s", url=verify_url),
            flash_kind="info",
            **_identifiers_ctx(),
        )
    return _render_tab(
        "identifiers",
        flash_msg=_("Verification link re-sent."),
        flash_kind="success",
        **_identifiers_ctx(),
    )


@academic_bp.route("/profile/interests/add", methods=["POST"])
@login_required
def submit_keyword_add():
    form = AddKeywordForm()
    if form.validate_on_submit():
        added, skipped = add_user_keywords_bulk(current_user, form.value.data)

        for kw in added:
            log_action(
                "user.keyword_added",
                entity_type="keyword",
                entity_id=str(kw.id),
                changes={"value": kw.value},
            )

        if not added and not skipped:
            return _render_tab(
                "interests",
                flash_msg=_("No valid keywords found."),
                flash_kind="warning",
                **_interests_ctx(),
            )

        if not skipped:
            msg = (
                _("%(n)d keywords added.", n=len(added)) if len(added) > 1 else _("Keyword added.")
            )
            return _render_tab("interests", flash_msg=msg, flash_kind="success", **_interests_ctx())

        msg = _("%(n)d added, %(s)d already following.", n=len(added), s=len(skipped))
        return _render_tab("interests", flash_msg=msg, flash_kind="info", **_interests_ctx())

    return _render_tab(
        "interests",
        flash_msg=_("Please correct the errors below."),
        flash_kind="danger",
        **_interests_ctx(),
    )


@academic_bp.route("/profile/interests/<int:keyword_id>/delete", methods=["POST"])
@login_required
def submit_keyword_delete(keyword_id: int):
    ok, err = remove_user_keyword(current_user, keyword_id)
    if not ok:
        return _render_tab("interests", flash_msg=_(err), flash_kind="danger", **_interests_ctx())
    log_action("user.keyword_removed", entity_type="keyword", entity_id=str(keyword_id))
    return _render_tab(
        "interests", flash_msg=_("Keyword removed."), flash_kind="success", **_interests_ctx()
    )
