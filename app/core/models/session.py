"""Kullanıcı oturumları — her login'de bir satır oluşturulur.

Amaç:
  - Aktif oturumları profil tab'ında göstermek
  - "Tüm oturumları kapat" ve "Bu oturumu kapat" işlevleri
  - Her oturumun IP + tarayıcı + son görülme bilgisini tutmak

Oturum doğrulaması:
  Flask session cookie'sine `_sk` (session key) eklenir.
  Her istekte bu key DB'de aranır; yoksa (revoke edilmişse) logout.
"""

import secrets

from app.extensions import db


class UserSession(db.Model):
    __tablename__ = "user_sessions"

    id = db.Column(db.BigInteger, primary_key=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=False, index=True)

    # Tarayıcı cookie'sine yazılan rastgele token (64 hex karakter)
    session_key = db.Column(db.String(64), unique=True, nullable=False, index=True)

    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True), server_default=db.func.now(), nullable=False
    )
    last_seen_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        onupdate=db.func.now(),
        nullable=False,
    )

    user = db.relationship("User", backref=db.backref("sessions", lazy="dynamic"))

    @staticmethod
    def generate_key() -> str:
        return secrets.token_hex(32)  # 64 hex karakter
