from flask import Blueprint, render_template
from flask_login import login_required

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/profile")
@login_required
def profile():
    return render_template("settings/profile.html")


@settings_bp.route("/system")
@login_required
def system():
    from app.core.auth.decorators import permission_required

    return render_template("settings/system.html")
