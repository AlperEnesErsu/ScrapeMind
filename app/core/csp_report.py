"""Where browsers send Content-Security-Policy violations.

`script-src` goes out report-only first (PRELAUNCH Y4 step 3). A report-only
policy nobody reads is not a trial run, it is nothing -- so the reports land
here and become one structured log line each, searchable next to everything
else the app logs.

Written for hostile input, because anyone can POST to it:

* no CSRF (a browser sends these without a token) and no login (violations
  happen on the login page too), so it is rate limited per client instead;
* the body is capped before it is parsed;
* only a handful of known fields are logged, each truncated -- a report is
  never echoed into the log whole;
* the query string is cut off every URL. This app's URLs carry the reader's
  own search terms (`/library/search?q=...`); see Referrer-Policy in
  app/__init__.py for the same concern.

Always 204: the browser does nothing with the response, and a scanner learns
nothing from it.
"""

from __future__ import annotations

import json

import structlog
from flask import Blueprint, request

from app.extensions import limiter

logger = structlog.get_logger()

csp_report_bp = Blueprint("csp_report", __name__)

#: Real reports are a few hundred bytes.
MAX_BODY_BYTES = 16 * 1024

#: Fields worth keeping, under both spellings: the legacy `report-uri` format
#: (`{"csp-report": {"blocked-uri": ...}}`) and the Reporting API format
#: (`[{"type": "csp-violation", "body": {"blockedURL": ...}}]`).
_FIELDS = {
    "document-uri": "document",
    "documentURL": "document",
    "blocked-uri": "blocked",
    "blockedURL": "blocked",
    "violated-directive": "directive",
    "effective-directive": "directive",
    "effectiveDirective": "directive",
    "disposition": "disposition",
    "source-file": "source",
    "sourceFile": "source",
    "line-number": "line",
    "lineNumber": "line",
}


def _strip_query(value: str) -> str:
    return value.split("?", 1)[0].split("#", 1)[0]


def _summarise(report: dict) -> dict:
    out: dict = {}
    for key, name in _FIELDS.items():
        if name in out or key not in report:
            continue
        value = report[key]
        if isinstance(value, int) and not isinstance(value, bool):
            out[name] = value
        elif isinstance(value, str):
            if name in ("document", "blocked", "source"):
                value = _strip_query(value)
            out[name] = value[:300]
    return out


def _reports(payload) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("csp-report"), dict):
        return [payload["csp-report"]]
    if isinstance(payload, list):
        return [
            item["body"]
            for item in payload[:20]
            if isinstance(item, dict) and isinstance(item.get("body"), dict)
        ]
    return []


@csp_report_bp.route("/csp-report", methods=["POST"])
@limiter.limit("60 per minute")
def csp_report():
    if (request.content_length or 0) > MAX_BODY_BYTES:
        return "", 204
    raw = request.get_data(cache=False)[:MAX_BODY_BYTES]
    try:
        payload = json.loads(raw)
    except ValueError:
        return "", 204
    for report in _reports(payload):
        summary = _summarise(report)
        if summary:
            logger.warning("csp_violation", **summary)
    return "", 204
