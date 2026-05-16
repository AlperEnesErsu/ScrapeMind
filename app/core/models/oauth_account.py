from app.extensions import db


class OAuthAccount(db.Model):
    __tablename__ = "oauth_accounts"

    id = db.Column(db.BigInteger, primary_key=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False)
    provider = db.Column(db.String(32), nullable=False)
    provider_user_id = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=True)
    raw_data = db.Column(db.JSON, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("provider", "provider_user_id", name="uq_oauth_provider_uid"),
    )

    user = db.relationship("User", back_populates="oauth_accounts")
