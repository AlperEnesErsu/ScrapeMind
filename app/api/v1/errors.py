"""Consistent JSON error envelope for the v1 API.

Every error is `{"error": {"code": "...", "message": "..."}}` so clients can
switch on a stable machine code while showing `message` to humans.
"""

from __future__ import annotations

from typing import Any

from flask import jsonify


def error_response(status: int, code: str, message: str, **extra: Any):
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return jsonify(body), status
