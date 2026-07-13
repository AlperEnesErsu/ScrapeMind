"""API v1 blueprint.

Bearer-token (JWT) authenticated JSON API. Registered under /api/v1 and
CSRF-exempt (token auth doesn't rely on the session cookie) in app/__init__.py.
"""

from flask import Blueprint

api_v1_bp = Blueprint("api_v1", __name__)

# Import route modules for their side effect of registering handlers on the
# blueprint. Kept at the bottom to avoid a circular import (the modules import
# api_v1_bp from here).
from app.api.v1 import auth, routes  # noqa: E402,F401
