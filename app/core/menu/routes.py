from flask import Blueprint, render_template
from flask_login import login_required

from app.core.auth.decorators import permission_required

menu_bp = Blueprint("menu", __name__)


@menu_bp.route("/")
@login_required
@permission_required("menu.view")
def menu_list():
    from app.core.models.menu import MenuItem

    items = MenuItem.query.order_by(MenuItem.order_index).all()
    return render_template("menu/list.html", items=items)
