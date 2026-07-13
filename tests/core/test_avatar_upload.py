"""Avatar upload: re-encoding, validation, and the SSRF-skip for local paths."""

import io
import os

import pytest
from PIL import Image
from sqlalchemy import text
from werkzeug.datastructures import FileStorage

from app.core.auth.strategies.local import LocalAuthStrategy
from app.core.models.user import User
from app.core.settings.avatar_service import (
    delete_avatar_file,
    is_local_avatar,
    save_avatar,
)
from app.core.settings.service import update_personal_info


@pytest.fixture
def user(db):
    db.session.query(User).delete()
    db.session.commit()
    u = User(
        username="ava",
        email="ava@example.com",
        full_name="Ava",
        password_hash=LocalAuthStrategy.hash_password("oldpass12"),
    )
    db.session.add(u)
    db.session.commit()
    yield u
    delete_avatar_file(u)
    db.session.execute(text("DELETE FROM user_roles"))
    db.session.query(User).delete()
    db.session.commit()


def _png_upload(name="me.png", size=(400, 200), color=(10, 120, 240)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    buf.seek(0)
    return FileStorage(stream=buf, filename=name, content_type="image/png")


def test_save_avatar_reencodes_to_square_webp(app, db, user):
    with app.test_request_context():
        rel, err = save_avatar(user, _png_upload())
    assert err is None
    assert rel.startswith("/")
    assert f"user_{user.id}.webp" in rel
    assert "?v=" in rel  # cache-busted

    disk = os.path.join(app.static_folder, "uploads", "avatars", f"user_{user.id}.webp")
    assert os.path.exists(disk)
    with Image.open(disk) as saved:
        assert saved.format == "WEBP"
        assert saved.size == (256, 256)


def test_save_avatar_rejects_bad_extension(app, db, user):
    bad = FileStorage(stream=io.BytesIO(b"nope"), filename="evil.exe", content_type="image/png")
    with app.test_request_context():
        rel, err = save_avatar(user, bad)
    assert rel is None
    assert "PNG" in err


def test_save_avatar_rejects_disguised_non_image(app, db, user):
    # A payload renamed to .png must not survive Image.open + verify.
    disguised = FileStorage(
        stream=io.BytesIO(b"MZ\x90\x00 this is not an image"),
        filename="payload.png",
        content_type="image/png",
    )
    with app.test_request_context():
        rel, err = save_avatar(user, disguised)
    assert rel is None
    assert "valid image" in err


def test_update_personal_info_allows_local_avatar_path(db, user):
    # A same-site path we generated must bypass the external-URL SSRF check.
    update_personal_info(user, "Ava", "/static/uploads/avatars/user_1.webp?v=123")
    assert user.avatar_url == "/static/uploads/avatars/user_1.webp?v=123"


def test_update_personal_info_still_guards_external_url(db, user):
    with pytest.raises(ValueError):
        update_personal_info(user, "Ava", "http://169.254.169.254/latest/meta-data/")


def test_is_local_avatar_distinguishes_local_from_external():
    assert is_local_avatar("/static/uploads/avatars/user_1.webp?v=1") is True
    assert is_local_avatar("https://example.com/a.png") is False
    assert is_local_avatar(None) is False
    assert is_local_avatar("") is False
