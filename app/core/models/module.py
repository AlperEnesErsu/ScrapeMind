from app.extensions import db


class Module(db.Model):
    __tablename__ = "modules"

    code = db.Column(db.String(64), primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    version = db.Column(db.String(32), nullable=False, default="0.0.1")
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    installed_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    settings_schema = db.Column(db.JSON, nullable=True)
