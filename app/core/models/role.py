from app.core.base_model import BaseModel
from app.extensions import db

role_permissions = db.Table(
    "role_permissions",
    db.Column("role_id", db.BigInteger, db.ForeignKey("roles.id"), primary_key=True),
    db.Column("permission_id", db.BigInteger, db.ForeignKey("permissions.id"), primary_key=True),
)

role_menus = db.Table(
    "role_menus",
    db.Column("role_id", db.BigInteger, db.ForeignKey("roles.id"), primary_key=True),
    db.Column("menu_item_id", db.BigInteger, db.ForeignKey("menu_items.id"), primary_key=True),
)


class Role(BaseModel):
    __tablename__ = "roles"

    name = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=True)

    permissions = db.relationship(
        "Permission", secondary=role_permissions, back_populates="roles", lazy="select"
    )
    menu_items = db.relationship(
        "MenuItem", secondary=role_menus, back_populates="roles", lazy="select"
    )
