"""Academic identity data model.

Phase 2 starts with multi-email support. The schema is generic so future
identifier types (ORCID, Scopus, WoS, ResearchGate, GitHub, ROR) can be
added by inserting one row into identifier_types — no migration needed.
"""

from app.core.base_model import BaseModel
from app.extensions import db


class IdentifierType(BaseModel):
    """Lookup table — one row per supported identifier kind."""

    __tablename__ = "identifier_types"

    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    validation_regex = db.Column(db.String(255), nullable=True)
    is_unique_per_user = db.Column(db.Boolean, nullable=False, default=True)
    verification_method = db.Column(
        db.String(32), nullable=True
    )  # email_link | oauth | manual | None


class UserIdentifier(BaseModel):
    """Concrete identifier value for a user (one row per email/ORCID/etc.)."""

    __tablename__ = "user_identifiers"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    identifier_type_id = db.Column(
        db.BigInteger, db.ForeignKey("identifier_types.id"), nullable=False
    )
    value = db.Column(db.String(255), nullable=False)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)
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
