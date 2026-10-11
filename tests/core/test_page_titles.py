"""Every page title carries the product name exactly once.

Templates used to add " — ScrapeMind" by hand; about twenty did not, so
"Profil" and "Patent Takibi" sat in the tab bar and history next to
"Keşfet — ScrapeMind". base.html now adds it.
"""

import re

import pytest


def _title(response) -> str:
    match = re.search(r"<title>(.*?)</title>", response.get_data(as_text=True), re.S)
    assert match, "no <title>"
    return match.group(1).strip()


@pytest.mark.parametrize(
    "path",
    ["/auth/login", "/auth/forgot", "/auth/register", "/definitely-not-a-page"],
)
def test_public_pages_name_the_product_once(client, path):
    title = _title(client.get(path))
    assert title.count("ScrapeMind") == 1, title
    assert title.endswith("ScrapeMind"), title


@pytest.mark.parametrize("path", ["/settings/profile", "/papers/", "/library/"])
def test_signed_in_pages_name_the_product_once(auth_client, path):
    client, _uid = auth_client
    title = _title(client.get(path, follow_redirects=True))
    assert title.count("ScrapeMind") == 1, title
    assert title != "ScrapeMind", "the page's own name is missing"
