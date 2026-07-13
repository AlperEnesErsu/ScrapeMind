"""Avatar upload handling — validate, normalise, store on local disk.

Uploaded images are re-encoded (never stored as-is) to strip metadata and
any hostile payload, square-cropped, downscaled to 256×256, and written as
WEBP under static/uploads/avatars/. The user's avatar_url then points at
that served path.

We always re-save through Pillow rather than trusting the client's file —
that's what makes "upload an image" safe: a renamed .exe won't survive
Image.open + re-encode.
"""

from __future__ import annotations

import os

import structlog
from flask import current_app, url_for
from PIL import Image, UnidentifiedImageError

logger = structlog.get_logger()

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
AVATAR_SIZE = 256
_SUBDIR = os.path.join("uploads", "avatars")


def _avatar_dir() -> str:
    """Absolute path to the avatar upload directory (created on demand)."""
    base = current_app.static_folder or ""  # app/core/static
    path = os.path.join(base, _SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def is_local_avatar(avatar_url: str | None) -> bool:
    """True if the URL points at an avatar we saved locally (vs. an external URL)."""
    if not avatar_url:
        return False
    return _SUBDIR.replace(os.sep, "/") in avatar_url


def save_avatar(user, file_storage) -> tuple[str | None, str | None]:
    """Validate + store an uploaded avatar for `user`.

    Returns (relative_url, error). On success the caller should set
    user.avatar_url to relative_url. On failure relative_url is None and
    error is a message id.
    """
    filename = (file_storage.filename or "").lower()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return None, "Only PNG, JPG or WEBP images are allowed."

    try:
        Image.open(file_storage.stream).verify()  # detect truncated/corrupt files
        # verify() leaves the image unusable — reopen for actual processing.
        file_storage.stream.seek(0)
        img: Image.Image = Image.open(file_storage.stream)
    except (UnidentifiedImageError, OSError):
        return None, "That file is not a valid image."

    img = img.convert("RGB")
    img = _square_crop(img)
    img = img.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)

    out_name = f"user_{user.id}.webp"
    out_path = os.path.join(_avatar_dir(), out_name)
    try:
        img.save(out_path, format="WEBP", quality=85)
    except OSError:
        logger.exception("avatar_save_failed", user_id=user.id)
        return None, "Could not save the image. Try again."

    # Cache-bust the served URL so the browser picks up the new file even
    # though the filename is stable per user.
    rel = url_for("static", filename=f"{_SUBDIR.replace(os.sep, '/')}/{out_name}")
    rel = f"{rel}?v={int(os.path.getmtime(out_path))}"
    logger.info("avatar_saved", user_id=user.id)
    return rel, None


def delete_avatar_file(user) -> None:
    """Remove the on-disk avatar for a user, if any. Silent on miss."""
    out_path = os.path.join(_avatar_dir(), f"user_{user.id}.webp")
    try:
        os.remove(out_path)
    except OSError:
        pass


def _square_crop(img: Image.Image) -> Image.Image:
    """Centre-crop to a square before downscaling so avatars aren't stretched."""
    w, h = img.size
    if w == h:
        return img
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))
