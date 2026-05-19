from flask import Blueprint

academic_bp = Blueprint("academic", __name__, template_folder="templates")

from app.modules.academic import routes  # noqa: E402, F401
from app.modules.academic.routes import _register_tabs

_register_tabs()
