"""With no model configured, the AI views point at the AI settings tab.

They used to tell every user to "set ANTHROPIC_API_KEY in your .env" -- an
operator-only variable for a provider that is not even the default
(OpenRouter is), while a user can bring their own key in the AI settings.
The chat heading also carried an untranslated "(RAG Chat)" suffix and its
blurb promised "Claude" whatever the provider.
"""

import pytest
from flask import render_template, url_for


@pytest.mark.parametrize(
    "template, context",
    [
        ("scrape/_ai_disabled.html", {}),
        ("scrape/_paper_chat.html", {"ai_enabled": False, "chat_messages": []}),
    ],
)
def test_disabled_ai_points_to_the_ai_settings_tab(app, template, context):
    with app.test_request_context("/?lang=en"):
        html = render_template(template, user_paper=None, paper=None, **context)
        settings = url_for("settings.workspace", tab="ai")
    assert "ANTHROPIC_API_KEY" not in html
    assert ".env" not in html
    assert f'href="{settings}"' in html
    assert "AI Settings" in html


def test_chat_heading_and_blurb_are_provider_neutral_and_translated(app):
    with app.test_request_context("/?lang=tr"):
        html = render_template(
            "scrape/_paper_chat.html",
            ai_enabled=False,
            chat_messages=[],
            user_paper=None,
            paper=None,
        )
    assert "RAG Chat" not in html
    assert "Claude" not in html
    assert "Makaleye Sor" in html
