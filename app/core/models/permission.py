from app.core.base_model import BaseModel
from app.extensions import db


class Permission(BaseModel):
    __tablename__ = "permissions"

    code = db.Column(db.String(128), unique=True, nullable=False, index=True)
    label_key = db.Column(db.String(128), nullable=False)
    module_code = db.Column(db.String(64), db.ForeignKey("modules.code"), nullable=True)

    roles = db.relationship("Role", secondary="role_permissions", back_populates="permissions", lazy="select")
