"""Tests for the audit action humaniser."""

from __future__ import annotations

import pytest

from app.core.audit.labels import humanize_action


@pytest.mark.parametrize(
    "action,expected",
    [
        ("user.totp_failed", "User · Totp failed"),
        ("paper.favorite_toggled", "Paper · Favorite toggled"),
        ("system_settings.update", "System · Update"),
        ("user.login", "User · Login"),
        ("role.delete", "Role · Delete"),
        ("weird", "Weird"),  # no dot → generic
        ("", ""),
        (None, ""),
    ],
)
def test_humanize_action(action, expected):
    assert humanize_action(action) == expected
