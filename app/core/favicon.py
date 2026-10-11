"""`/favicon.ico` at the site root.

Browsers, feed readers and crawlers ask for it whatever base.html's
`<link rel="icon">` tags say, and every one of those requests was a 404 in the
log. The file is derived from the mark by scripts/render_favicon.py like the
other icons, so serving it is all that is left: no login (the login page asks
for it too), no permission, no menu entry.
"""

from __future__ import annotations

from flask import Blueprint, current_app, send_from_directory

favicon_bp = Blueprint("favicon", __name__)

#: A week. The icon changes only when the mark does, and the file has no
#: version in its URL to bust a longer cache with.
MAX_AGE = 7 * 24 * 3600


@favicon_bp.route("/favicon.ico")
def favicon():
    return send_from_directory(
        f"{current_app.static_folder}/img",
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
        max_age=MAX_AGE,
    )
