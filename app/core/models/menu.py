from app.extensions import db


class MenuItem(db.Model):
    __tablename__ = "menu_items"

    id = db.Column(db.BigInteger, primary_key=True)
    parent_id = db.Column(db.BigInteger, db.ForeignKey("menu_items.id"), nullable=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    label_key = db.Column(db.String(128), nullable=False)
    icon = db.Column(db.String(64), nullable=True)
    url = db.Column(db.String(512), nullable=True)
    endpoint = db.Column(db.String(128), nullable=True)
    module_code = db.Column(db.String(64), db.ForeignKey("modules.code"), nullable=True)
    required_permission = db.Column(db.String(128), nullable=True)
    order_index = db.Column(db.Integer, nullable=False, default=0)
    is_visible = db.Column(db.Boolean, nullable=False, default=True)

    children = db.relationship("MenuItem", backref=db.backref("parent", remote_side=[id]), lazy="select")
    roles = db.relationship("Role", secondary="role_menus", back_populates="menu_items", lazy="select")
