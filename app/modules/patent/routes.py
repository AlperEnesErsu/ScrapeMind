"""Patent tracking pages.

Two routes for now: the window itself, and one patent read the way a patent is
actually read — claim 1 first, then the tree under it. Search arrives in 8.4.

Both must render on an empty corpus. Until the weekly load lands in 8.3 the
corpus *is* empty, and a page that only works once data exists cannot be
reviewed before then.
"""

from flask import Blueprint, abort, render_template
from flask_login import login_required

from app.modules.patent import service

patent_bp = Blueprint("patent", __name__, template_folder="templates")


@patent_bp.route("/")
@login_required
def index():
    """The window: what is in it, how wide it is, and what arrived last."""
    return render_template(
        "patent/index.html",
        stats=service.window_stats(),
        documents=service.recent_documents(),
        window_start=service.window_start(),
    )


@patent_bp.route("/<doc_number>")
@login_required
def detail(doc_number: str):
    document = service.get_document(doc_number)
    if document is None:
        abort(404)
    return render_template(
        "patent/detail.html",
        document=document,
        claim_tree=service.claim_tree(document),
    )
