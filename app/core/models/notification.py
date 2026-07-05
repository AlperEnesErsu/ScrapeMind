from datetime import UTC, datetime

from app.extensions import db


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    user_id = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("users.id"),
        nullable=False,
    )
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC), nullable=False)

    user = db.relationship("User", backref=db.backref("notifications", lazy="dynamic"))


def add_notification(user_id, title, message) -> Notification:
    from app.extensions import db

    noti = Notification(user_id=user_id, title=title, message=message)
    db.session.add(noti)
    db.session.commit()
    return noti
