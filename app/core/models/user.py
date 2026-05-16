from datetime import datetime, timezone

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

    roles = db.relationship("Role", secondary=user_roles, lazy="select")
    oauth_accounts = db.relationship("OAuthAccount", back_populates="user", lazy="select")
    settings = db.relationship("UserSettings", back_populates="user", uselist=False, lazy="select")

    def check_lock(self) -> None:
        if self.locked_until and self.locked_until <= datetime.now(timezone.utc):
            self.is_locked = False
            self.locked_until = None
            self.failed_login_count = 0
            db.session.commit()

    @property
    def effective_is_locked(self) -> bool:
        if self.locked_until and self.locked_until > datetime.now(timezone.utc):
            return True
        return self.is_locked and not self.locked_until

    def get_id(self) -> str:
        return str(self.id)
