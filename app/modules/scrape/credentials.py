"""Fernet-encrypted per-user secrets, shared by whatever needs one.

Extracted from `ai_service` when Zotero became the second consumer. The key
derivation is unchanged and must stay unchanged: existing LLM keys in
`UserSettings.settings["llm"]["api_key_enc"]` were encrypted with it, and a
different derivation would make every one of them undecryptable with no error
loud enough to notice -- `decrypt_secret` degrades to None by design.

`ai_service` re-exports the two LLM-named helpers so its callers and its tests
keep working; new code should use the neutral names here.
"""

from __future__ import annotations

import base64
import hashlib

import structlog
from flask import current_app

logger = structlog.get_logger(__name__)


def _fernet_key() -> bytes:
    """32-byte urlsafe-base64 Fernet key.

    Uses `LLM_ENC_KEY` when configured, otherwise derives a stable one from
    `SECRET_KEY` so dev and test never need a separate secret.

    The config name still says LLM because rotating it would orphan every key
    encrypted before the rename. It is the *secret* key for this application's
    stored credentials, whatever they are for.
    """
    raw = (current_app.config.get("LLM_ENC_KEY") or "").strip()
    if raw:
        return raw.encode("utf-8")

    secret = (current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    digest = hashlib.sha256(secret).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(_fernet_key())


def encrypt_secret(plain: str) -> str:
    """Encrypt a plaintext credential for storage.

    Never call this on anything you intend to log, and never log what you pass
    to it.
    """
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str | None:
    """Decrypt a stored credential, or None.

    Returns None -- and logs without the plaintext -- on any failure, so a
    corrupt row or a rotated key degrades to "no credential" rather than
    crashing a request. That is deliberate, and it is also why the derivation
    above must not change casually: the failure mode is silent.
    """
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        logger.warning("secret_decrypt_failed")
        return None
