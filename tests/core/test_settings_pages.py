"""Account settings and product configuration live on separate pages (#58).

`/settings/profile` used to carry fourteen tabs, where a user's LLM provider
sat next to their password. Core's tabs are the account; module tabs are what
the product does for you and live on `/settings/workspace`.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.settings import tab_registry
from app.core.settings.tab_registry import (
    ACCOUNT_PAGE,
    CORE_TABS,
    WORKSPACE_PAGE,
    page_of,
    register_profile_tab,
    tabs_for,
)

ROOT = Path(__file__).resolve().parents[2]
CORE_CODES = [code for code, _icon, _label in CORE_TABS]


def _tab_codes(body: str) -> list[str]:
    return re.findall(r'data-tab="([a-z_]+)"', body)


def _redirect_target(response) -> tuple[str, dict]:
    url = urlparse(response.headers["Location"])
    return url.path, parse_qs(url.query)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_core_tabs_are_the_account_page():
    assert [code for code, *_ in tabs_for(ACCOUNT_PAGE)][: len(CORE_CODES)] == CORE_CODES
    assert all(page_of(code) == ACCOUNT_PAGE for code in CORE_CODES)


def test_module_tabs_default_to_the_workspace_page():
    workspace = [code for code, *_ in tabs_for(WORKSPACE_PAGE)]

    assert {"identifiers", "interests", "ai", "authors", "alerts", "zotero"} <= set(workspace)
    assert not set(workspace) & set(CORE_CODES)


def test_a_module_tab_can_opt_into_the_account_page(monkeypatch):
    monkeypatch.setattr(tab_registry, "_EXTRA_TABS", {})
    register_profile_tab("passkeys", "bi-key", "Passkeys", dict, page=ACCOUNT_PAGE)

    assert page_of("passkeys") == ACCOUNT_PAGE
    assert tabs_for(WORKSPACE_PAGE) == []


def test_an_unknown_page_is_refused():
    with pytest.raises(ValueError):
        register_profile_tab("x", "bi-x", "X", dict, page="elsewhere")


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def test_the_profile_page_shows_only_account_tabs(auth_client):
    client, _ = auth_client
    body = client.get("/settings/profile").get_data(as_text=True)

    assert _tab_codes(body) == CORE_CODES


def test_the_workspace_page_shows_only_module_tabs(auth_client):
    client, _ = auth_client
    response = client.get("/settings/workspace")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert _tab_codes(body) == [code for code, *_ in tabs_for(WORKSPACE_PAGE)]
    assert 'id="tab-content"' in body
    # Tab links push the workspace URL, not the profile one.
    assert "/settings/workspace?tab=identifiers" in body
    assert "/settings/profile?tab=" not in body


def test_the_workspace_page_is_named_by_the_module(app, auth_client):
    client, _ = auth_client
    with app.test_request_context():
        from flask_babel import force_locale, gettext

        with force_locale("en"):
            assert gettext("Research Settings") == "Research Settings"

    body = client.get("/settings/workspace?lang=en").get_data(as_text=True)
    assert "Research Settings" in body


@pytest.mark.parametrize("tab", ["ai", "authors", "alerts", "zotero", "identifiers", "interests"])
def test_an_old_profile_link_to_a_module_tab_is_redirected(auth_client, tab):
    """Every bookmark, email and link from before #58 pointed at the profile."""
    client, _ = auth_client
    response = client.get(f"/settings/profile?tab={tab}")

    assert response.status_code == 302
    assert _redirect_target(response) == ("/settings/workspace", {"tab": [tab]})


def test_an_account_tab_asked_of_the_workspace_page_is_redirected(auth_client):
    client, _ = auth_client
    response = client.get("/settings/workspace?tab=password")

    assert response.status_code == 302
    assert _redirect_target(response) == ("/settings/profile", {"tab": ["password"]})


def test_an_unknown_tab_falls_back_to_the_page_first_tab(auth_client):
    client, _ = auth_client
    body = client.get("/settings/workspace?tab=nope").get_data(as_text=True)

    first = tabs_for(WORKSPACE_PAGE)[0][0]
    assert re.search(rf'active"[^>]*data-tab="{first}"', body.replace("\n", " "))


def test_a_core_only_install_has_no_workspace_page(auth_client, monkeypatch):
    monkeypatch.setattr(tab_registry, "_EXTRA_TABS", {})
    client, _ = auth_client

    response = client.get("/settings/workspace")

    assert response.status_code == 302
    assert _redirect_target(response)[0] == "/settings/profile"


def test_the_workspace_page_requires_login(client):
    assert client.get("/settings/workspace").status_code in (302, 401)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_one_menu_entry_for_the_workspace_page(app):
    """#58 asked not to crowd the sidebar: one row, not one per tab."""
    from app.modules.academic.manifest import MODULE

    rows = [m for m in MODULE["menu"] if m["endpoint"] == "settings.workspace"]
    assert len(rows) == 1
    assert "settings.workspace" in app.view_functions
    assert rows[0]["order"] < 90, "sits just above Profilim (settings_profile, 90)"


def test_no_link_sends_a_workspace_tab_through_the_profile_page():
    """The redirect keeps old links alive; new code should not rely on it."""
    workspace_codes = "|".join(code for code, *_ in tabs_for(WORKSPACE_PAGE))
    pattern = re.compile(
        rf"url_for\(\s*['\"]settings\.profile['\"]\s*,\s*tab=['\"]({workspace_codes})['\"]"
    )
    offenders = []
    for path in [*(ROOT / "app").rglob("*.py"), *(ROOT / "app").rglob("*.html")]:
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    assert not offenders, "\n".join(offenders)
