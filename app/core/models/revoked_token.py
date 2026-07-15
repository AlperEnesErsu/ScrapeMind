"""Denylist for revoked API refresh tokens.

Only *refresh* tokens land here. Access tokens are deliberately not
denylisted — they live 15 minutes and are invalidated in bulk via
`User.token_version`, so the hot path (every /api/v1 request) stays free of
an extra query.

Rows are dead weight once `expires_at` passes — the token wouldn't validate
anyway — so `core.purge_revoked_tokens` sweeps them nightly.
"""

from app.extensions import db


class RevokedToken(db.Model):
    __tablename__ = "revoked_tokens"

    id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    # JWT ID claim of the revoked refresh token.
    jti = db.Column(db.String(36), nullable=False, unique=True, index=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)
    # Mirrors the token's own exp so the purge task knows when this row is moot.
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
