from app.extensions import db


class UserSettings(db.Model):
    __tablename__ = "user_settings"

    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), primary_key=True)
    settings = db.Column(db.JSON, nullable=False, default=dict)
    updated_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

    user = db.relationship("User", back_populates="settings")


class SystemSettings(db.Model):
    __tablename__ = "system_settings"

    key = db.Column(db.String(128), primary_key=True)
    value = db.Column(db.JSON, nullable=False)
    updated_by = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
