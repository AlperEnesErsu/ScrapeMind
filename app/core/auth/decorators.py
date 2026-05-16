from functools import wraps

from flask import abort
from flask_login import current_user

from app.core.rbac.service import user_has_permission


def permission_required(permission_code: str):
    """Restrict a view to users who hold the given permission code.

    is_superuser bypass is applied HERE and ONLY here.
    Do not add bypass checks anywhere else; a module developer forgetting to
    add the check in a new place would silently drop the superuser privilege.
    """

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(403)
            # Central superuser bypass — single source of truth.
            if current_user.is_superuser:
                return f(*args, **kwargs)
            if not user_has_permission(current_user, permission_code):
                abort(403)
            return f(*args, **kwargs)

        return wrapped

    return decorator


def login_required_json(f):
    """Like @login_required but returns 401 JSON for API/HTMX callers."""

    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            from flask import jsonify

            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)

    return wrapped
