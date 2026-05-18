"""Academic identity data model.

Two independent systems:

  users.email  — auth-only. The login email. Single, immutable from this
                 module's perspective. Editing it goes through the auth/
                 settings flow as before.

  user_identifiers — *historical* academic identities. Old institution
                 emails (especially helpful when a researcher's name or
                 affiliation has changed), ORCID, Scopus Author ID, WoS
                 Researcher ID. Multiple per user, never tied to login.

  user_keywords / keywords — research interest tags used to personalise
                 the scraping feed.
"""

from app.core.base_model import BaseModel
from app.extensions import db


class IdentifierType(BaseModel):
    """Lookup table — one row per supported identifier kind."""

    __tablename__ = "identifier_types"

    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    validation_regex = db.Column(db.String(255), nullable=True)
    verification_method = db.Column(
        db.String(32), nullable=True
    )  # email_link | oauth | manual | None


class UserIdentifier(BaseModel):
    """A historical academic identity for a user. NEVER the login email."""

    __tablename__ = "user_identifiers"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    identifier_type_id = db.Column(
        db.BigInteger, db.ForeignKey("identifier_types.id"), nullable=False
    )
    value = db.Column(db.String(255), nullable=False)
    is_verified = db.Column(db.Boolean, nullable=False, default=False)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=True)

    type = db.relationship("IdentifierType", lazy="joined")
    user = db.relationship("User", backref=db.backref("identifiers", lazy="select"))

    __table_args__ = (
        # An identifier value (e.g. an email or ORCID) belongs to at most one user globally.
        db.UniqueConstraint("identifier_type_id", "value", name="uq_id_type_value"),
        # Same user can't list the same identifier twice.
        db.UniqueConstraint("user_id", "identifier_type_id", "value", name="uq_user_id_type_value"),
    )


class Keyword(BaseModel):
    """Global research-interest dictionary. One row per normalised term."""

    __tablename__ = "keywords"

    value = db.Column(db.String(64), unique=True, nullable=False, index=True)


class UserKeyword(BaseModel):
    """Junction: which keywords does each user follow."""

    __tablename__ = "user_keywords"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    keyword_id = db.Column(db.BigInteger, db.ForeignKey("keywords.id"), nullable=False)

    keyword = db.relationship("Keyword", lazy="joined")
    user = db.relationship("User", backref=db.backref("keyword_links", lazy="select"))

    __table_args__ = (db.UniqueConstraint("user_id", "keyword_id", name="uq_user_keyword"),)
