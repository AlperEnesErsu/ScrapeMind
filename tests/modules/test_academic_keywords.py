import pytest
from sqlalchemy import text

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.modules.academic.models import Keyword
from app.modules.academic.service import add_user_keyword, list_user_keywords, remove_user_keyword


@pytest.fixture
def clean(db):
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
    db.session.execute(text("DELETE FROM user_identifiers"))
    db.session.execute(text("DELETE FROM identifier_types"))
    db.session.execute(text("DELETE FROM user_settings"))
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()
    u = User(
        username="alice",
        email="alice@example.com",
        full_name="Alice",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(u)
    db.session.commit()
    yield u
    db.session.execute(text("DELETE FROM user_keywords"))
    db.session.execute(text("DELETE FROM keywords"))
    db.session.query(User).delete()
    db.session.commit()


def test_add_keyword_normalises(db, clean):
    kw, err = add_user_keyword(clean, "  Transformer Architectures  ")
    assert err is None
    assert kw.value == "transformer architectures"


def test_add_keyword_too_short(db, clean):
    kw, err = add_user_keyword(clean, "a")
    assert kw is None
    assert "short" in err.lower()


def test_keyword_is_reused_globally(db, clean):
    """Two users following the same keyword share one keywords row."""
    other = User(
        username="bob",
        email="bob@example.com",
        full_name="Bob",
        password_hash=LocalAuthStrategy.hash_password("x12345678"),
    )
    db.session.add(other)
    db.session.commit()
    kw1, _ = add_user_keyword(clean, "graph neural networks")
    kw2, _ = add_user_keyword(other, "graph neural networks")
    assert kw1.id == kw2.id
    assert Keyword.query.count() == 1


def test_duplicate_keyword_for_same_user_rejected(db, clean):
    add_user_keyword(clean, "few-shot learning")
    kw, err = add_user_keyword(clean, "few-shot learning")
    assert kw is None
    assert "already follow" in err


def test_list_user_keywords(db, clean):
    add_user_keyword(clean, "rl")
    add_user_keyword(clean, "rlhf")
    add_user_keyword(clean, "diffusion")
    values = [k.value for k in list_user_keywords(clean)]
    assert values == sorted(values)
    assert "rl" in values


def test_remove_user_keyword(db, clean):
    kw, _ = add_user_keyword(clean, "to remove")
    ok, err = remove_user_keyword(clean, kw.id)
    assert ok is True
    assert kw not in list_user_keywords(clean)
