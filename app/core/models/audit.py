from app.extensions import db


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.BigInteger, primary_key=True)
    user_id = db.Column(db.BigInteger, db.ForeignKey("users.id"), nullable=True)
    action = db.Column(db.String(128), nullable=False)
    entity_type = db.Column(db.String(64), nullable=True)
    entity_id = db.Column(db.String(64), nullable=True)
    changes = db.Column(db.JSON, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.Text, nullable=True)
    locale = db.Column(db.String(8), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), server_default=db.func.now(), nullable=False, index=True
    )
