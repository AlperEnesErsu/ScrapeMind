"""Read-only resource endpoints for API v1."""

from __future__ import annotations

from flask import g, request
from sqlalchemy import nulls_last

from app.api.v1 import api_v1_bp
from app.api.v1.decorators import jwt_required
from app.api.v1.errors import error_response
from app.api.v1.serializers import paper_to_dict, user_paper_to_dict, user_to_dict
from app.modules.scrape.models import Paper, UserPaper

_DEFAULT_PER_PAGE = 20
_MAX_PER_PAGE = 100


def _pagination_args() -> tuple[int, int]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", _DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        per_page = _DEFAULT_PER_PAGE
    per_page = min(max(1, per_page), _MAX_PER_PAGE)
    return page, per_page


def _paginated(query, per_page: int, page: int, serialize):
    pg = query.paginate(page=page, per_page=per_page, error_out=False)
    return {
        "data": [serialize(item) for item in pg.items],
        "pagination": {
            "page": pg.page,
            "per_page": per_page,
            "total": pg.total,
            "pages": pg.pages,
        },
    }


@api_v1_bp.route("/health")
def health():
    """Unauthenticated liveness probe."""
    return {"status": "ok"}


@api_v1_bp.route("/me")
@jwt_required
def me():
    return {"data": user_to_dict(g.api_user)}


@api_v1_bp.route("/papers")
@jwt_required
def list_papers():
    page, per_page = _pagination_args()
    query = (
        Paper.query.filter(Paper.deleted_at.is_(None))
        # Newest published first; papers with no publish date sink to the end.
        .order_by(nulls_last(Paper.published_at.desc()), Paper.id.desc())
    )
    return _paginated(query, per_page, page, paper_to_dict)


@api_v1_bp.route("/papers/<int:paper_id>")
@jwt_required
def get_paper(paper_id: int):
    paper = Paper.query.filter_by(id=paper_id, deleted_at=None).first()
    if paper is None:
        return error_response(404, "not_found", "Paper not found.")
    return {"data": paper_to_dict(paper)}


@api_v1_bp.route("/me/papers")
@jwt_required
def my_papers():
    """The papers surfaced to the authenticated user (excludes dismissed)."""
    page, per_page = _pagination_args()
    query = UserPaper.query.filter(
        UserPaper.user_id == g.api_user.id,
        UserPaper.deleted_at.is_(None),
        UserPaper.dismissed_at.is_(None),
    ).order_by(UserPaper.created_at.desc())
    return _paginated(query, per_page, page, user_paper_to_dict)


@api_v1_bp.route("/me/collections", methods=["GET"])
@jwt_required
def list_collections():
    from app.modules.scrape.models import Collection
    colls = Collection.query.filter_by(user_id=g.api_user.id, deleted_at=None).all()
    return {
        "data": [
            {
                "id": c.id,
                "name": c.name,
                "description": c.description,
                "is_public": c.is_public,
                "share_token": c.share_token,
                "paper_count": len(c.papers),
            }
            for c in colls
        ]
    }


@api_v1_bp.route("/me/collections", methods=["POST"])
@jwt_required
def create_api_collection():
    from app.modules.scrape.collection_service import create_collection
    payload = request.get_json(silent=True) or {}
    name = payload.get("name", "")
    description = payload.get("description", "")
    coll, err = create_collection(g.api_user, name, description)
    if err:
        return error_response(422, "validation_error", err)
    return {
        "data": {
            "id": coll.id,
            "name": coll.name,
            "description": coll.description,
            "is_public": coll.is_public,
            "share_token": coll.share_token,
        }
    }, 201


@api_v1_bp.route("/me/feeds", methods=["GET"])
@jwt_required
def list_api_feeds():
    from app.modules.scrape.service import list_user_feeds
    feeds = list_user_feeds(g.api_user)
    return {
        "data": [
            {
                "id": f.id,
                "url": f.url,
                "label": f.label,
                "active": f.active,
            }
            for f in feeds
        ]
    }


@api_v1_bp.route("/me/feeds", methods=["POST"])
@jwt_required
def add_api_feed():
    from app.modules.scrape.service import add_user_feed
    payload = request.get_json(silent=True) or {}
    url = payload.get("url", "")
    label = payload.get("label", "")
    feed, err = add_user_feed(g.api_user, url, label)
    if err:
        return error_response(422, "validation_error", err)
    return {
        "data": {
            "id": feed.id,
            "url": feed.url,
            "label": feed.label,
            "active": feed.active,
        }
    }, 201
