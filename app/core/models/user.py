from datetime import UTC, datetime

from flask_login import UserMixin

from app.core.base_model import BaseModel
from app.extensions import db

user_roles = db.Table(
    "user_roles",
    db.Column("user_id", db.BigInteger, db.ForeignKey("users.id"), primary_key=True),
    db.Column("role_id", db.BigInteger, db.ForeignKey("roles.id"), primary_key=True),
)


class User(UserMixin, BaseModel):
    __tablename__ = "users"

    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    full_name = db.Column(db.String(128), nullable=False)
    avatar_url = db.Column(db.String(512), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_superuser = db.Column(db.Boolean, nullable=False, default=False)
    is_locked = db.Column(db.Boolean, nullable=False, default=False)
    locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    locale = db.Column(db.String(8), nullable=False, default="tr")
    timezone = db.Column(db.String(64), nullable=False, default="Europe/Istanbul")

    # 2FA (TOTP). totp_secret is base32; recovery_codes is a JSON list of
    # argon2-hashed one-time codes. enabled_at is the source of truth for
    # "is 2FA active?" — set only when the user confirms a code during setup.
    totp_secret = db.Column(db.String(64), nullable=True)
    totp_enabled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    totp_recovery_codes = db.Column(db.JSON, nullable=True)

    roles = db.relationship("Role", secondary=user_roles, lazy="select")
    oauth_accounts = db.relationship("OAuthAccount", back_populates="user", lazy="select")
    settings = db.relationship("UserSettings", back_populates="user", uselist=False, lazy="select")

    def check_lock(self) -> None:
        if self.locked_until and self.locked_until <= datetime.now(UTC):
            self.is_locked = False
            self.locked_until = None
            self.failed_login_count = 0
            db.session.commit()

    @property
    def effective_is_locked(self) -> bool:
        if self.locked_until and self.locked_until > datetime.now(UTC):
            return True
        return self.is_locked and not self.locked_until

    def get_id(self) -> str:
        return str(self.id)

    @property
    def is_totp_enabled(self) -> bool:
        return self.totp_enabled_at is not None and bool(self.totp_secret)
