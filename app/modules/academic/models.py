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
    """Global research-interest dictionary. One row per normalised term.

    The three translation columns exist because the sources we scrape (arXiv,
    Semantic Scholar, PubMed) are English corpora while users type their
    interests in Turkish — "kalp yetmezliği" matched nothing at all. They are
    filled lazily, once per term, by `scrape.service.ensure_keyword_translations`
    (lexicon fast-path, LLM fallback). This table is global and deduplicated,
    so a term is translated once for the whole deployment and every later user
    who follows it pays nothing.

      value_en      — canonical English form. Equal to `value` for terms that
                      are already English; NULL only until first translated.
      variants      — extra English synonyms ("cardiac failure"), used to widen
                      the OR-combined queries. NULL/[] when there are none.
      translated_at — set on every fill, including the no-op "already English"
                      one, so an untranslatable term isn't retried every scan.
    """

    __tablename__ = "keywords"

    value = db.Column(db.String(64), unique=True, nullable=False, index=True)
    value_en = db.Column(db.String(128), nullable=True)
    variants = db.Column(db.JSON, nullable=True)
    translated_at = db.Column(db.DateTime(timezone=True), nullable=True)

    @property
    def search_terms(self) -> list[str]:
        """Every English-ish term this keyword should be searched under, the
        canonical form first. Deduplicated case-insensitively; falls back to
        just `value` when nothing has been translated yet."""
        out: list[str] = []
        seen: set[str] = set()
        for term in [self.value_en, self.value, *(self.variants or [])]:
            cleaned = (term or "").strip()
            if cleaned and cleaned.lower() not in seen:
                seen.add(cleaned.lower())
                out.append(cleaned)
        return out


class UserKeyword(BaseModel):
    """Junction: which keywords does each user follow."""

    __tablename__ = "user_keywords"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    keyword_id = db.Column(db.BigInteger, db.ForeignKey("keywords.id"), nullable=False)

    keyword = db.relationship("Keyword", lazy="joined")
    user = db.relationship("User", backref=db.backref("keyword_links", lazy="select"))

    __table_args__ = (db.UniqueConstraint("user_id", "keyword_id", name="uq_user_keyword"),)
