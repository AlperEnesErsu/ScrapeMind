from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, Optional

from app.modules.scrape.report_service import REPORT_MAX_YEARS, REPORT_MIN_YEARS


class AiSettingsForm(FlaskForm):
    """Per-user OpenRouter key + optional model override — see
    app/modules/scrape/ai_service.py:set_user_llm_key. Both fields are
    optional: leaving the key blank keeps the currently-stored one (if any)
    and just updates the model override, or does nothing on an empty submit.
    """

    openrouter_api_key = PasswordField(
        _l("OpenRouter API Key"), validators=[Optional(), Length(max=256)]
    )
    model = StringField(_l("Model override (optional)"), validators=[Optional(), Length(max=128)])
    submit = SubmitField(_l("Save"))


class PriorArtForm(FlaskForm):
    """Free-text invention idea for the prior-art search (Faz 5.2).

    A textarea rather than a keyword box on purpose: the patent adapters take
    keywords, but the LLM novelty assessment reads the *description*, and
    asking for the idea once serves both — the route derives search terms from
    it. `Length(max=4000)` is a prompt-cost guard, not a domain limit.
    """

    idea = TextAreaField(
        _l("Describe your invention idea"),
        validators=[DataRequired(), Length(min=10, max=4000)],
    )
    assess = BooleanField(_l("Also assess novelty with AI"), default=True)
    submit = SubmitField(_l("Search prior art"))


class FollowAuthorForm(FlaskForm):
    """Follow an author by ORCID or OpenAlex id (Faz 5.4).

    Deliberately not a free-text name search: OpenAlex has thousands of
    "J. Smith" entries and picking the wrong one silently fills a user's feed
    with a stranger's papers. An identifier is unambiguous, and the ORCID a
    researcher already stored on the Identifiers tab is one click away.
    """

    identifier = StringField(
        _l("ORCID or OpenAlex author id"),
        validators=[DataRequired(), Length(max=64)],
    )
    submit = SubmitField(_l("Follow"))


class AuthorSearchForm(FlaskForm):
    """Free-text author name search against OpenAlex (Faz 6, ADR-0003).

    This is the counterpart `FollowAuthorForm` explicitly ruled out — that
    form's docstring still says "Deliberately not a free-text name search"
    and that reasoning still holds against *auto*-picking a match. What
    changed is the retrospective author-group use case: the user usually has
    only a name, not an ORCID, and a wrong pick there is immediately visible
    and harmless to the nightly feed (see docs/adr/0003-yazar-isim-aramasi.md).
    `search_authors` returns candidates only; the human always makes the
    final pick via `FollowAuthorForm`'s existing OpenAlex-id follow flow.
    """

    name = StringField(_l("Author name"), validators=[DataRequired(), Length(max=128)])
    submit = SubmitField(_l("Search"))


class AuthorGroupForm(FlaskForm):
    """Create a named author group (Faz 6) — see
    app/modules/scrape/service.py:create_author_group. Also reused, with only
    `.name` read, for the rename control on an existing group.
    """

    name = StringField(_l("Group name"), validators=[DataRequired(), Length(max=120)])
    description = StringField(
        _l("Description (optional)"), validators=[Optional(), Length(max=500)]
    )
    submit = SubmitField(_l("Create group"))


class UserFeedForm(FlaskForm):
    """Add a custom RSS/Atom feed — see
    app/modules/scrape/service.py:add_user_feed. `label` is optional; the
    service auto-fills it from the feed's own title when left blank.

    Deliberately no `wtforms.validators.URL` here: it insists on an explicit
    scheme, and the service supports pasting a bare "blog.example.test/feed.xml"
    (it prepends https://). All real validation lives in the service anyway —
    normalization, the SSRF guard, the per-user cap and an actual fetch — since
    those need config and DB access the form doesn't have.
    """

    url = StringField(_l("Feed URL"), validators=[DataRequired(), Length(max=512)])
    label = StringField(_l("Label (optional)"), validators=[Optional(), Length(max=128)])
    submit = SubmitField(_l("Add feed"))


class UserChannelForm(FlaskForm):
    """Subscribe to a YouTube channel — see
    app/modules/scrape/service.py:add_user_channel. `label` is optional; the
    service auto-fills it from the channel's own title when left blank.

    Deliberately no URL validator here either, for the same reason as
    UserFeedForm: the accepted input isn't always a URL (a bare "@handle" or
    a bare "UC..." id is valid too), and the real validation — resolution,
    the SSRF guard, the per-user cap and an actual verifying fetch — lives in
    the service, which is the only place with the config and DB access it
    needs.
    """

    url = StringField(_l("Channel URL or @handle"), validators=[DataRequired(), Length(max=512)])
    label = StringField(_l("Label (optional)"), validators=[Optional(), Length(max=200)])
    submit = SubmitField(_l("Add channel"))


class UserPageForm(FlaskForm):
    """Add a custom web page for non-RSS sites (Faz 5.2).

    `url` is the target page. `label` is optional (auto-filled from page title).
    `selector` is optional (custom CSS selector for container of repeating items).
    """

    url = StringField(_l("Web Page URL"), validators=[DataRequired(), Length(max=512)])
    label = StringField(_l("Label (optional)"), validators=[Optional(), Length(max=128)])
    selector = StringField(_l("CSS Selector (optional)"), validators=[Optional(), Length(max=256)])
    submit = SubmitField(_l("Add web page"))


class UserBlueskyForm(FlaskForm):
    """Follow a Bluesky account (Faz 5.3 — social feeds).

    `handle` accepts a Bluesky handle (e.g. `ylecun.bsky.social` or `@nature.com`),
    a profile URL (`https://bsky.app/profile/...`), or a DID.
    """

    handle = StringField(
        _l("Bluesky handle or profile URL"),
        validators=[DataRequired(), Length(max=256)],
    )
    submit = SubmitField(_l("Follow on Bluesky"))


class ReportForm(FlaskForm):
    """Request a new "topic" retrospective report (Faz 6) — see
    app/modules/scrape/report_service.py:create_report.

    Only the "topic" kind is requestable from this form. `create_report`
    also accepts an "author_group" kind, but there is no UI yet to manage
    `AuthorGroup`s themselves (group CRUD lives in `service.py` and is
    covered by tests/modules/test_author_groups.py, with no route in front
    of it) — so a group picker here would have nothing to list. The route
    always passes kind="topic".

    `keywords` is free-text, comma-separated, rather than a multi-select
    against the user's keyword dictionary: `create_report` only needs a
    flat list of strings, and a one-off report on a term the user hasn't
    added as a standing interest is a legitimate use. The route pre-fills
    this field from `academic.service.list_user_keywords` on a plain GET.

    `years` lists every whole year in
    `[REPORT_MIN_YEARS, REPORT_MAX_YEARS]` rather than a fixed "3 / 5 /
    custom" set — every choice already round-trips through the same
    `create_report` validation, so a free integer range is no less correct
    and needs no extra "custom" input to wire up.
    """

    keywords = StringField(
        _l("Keywords (comma-separated)"),
        validators=[DataRequired(), Length(max=512)],
    )
    years = SelectField(
        _l("Time range (years)"),
        choices=[(str(y), str(y)) for y in range(REPORT_MIN_YEARS, REPORT_MAX_YEARS + 1)],
        default="5" if REPORT_MIN_YEARS <= 5 <= REPORT_MAX_YEARS else str(REPORT_MIN_YEARS),
        validators=[DataRequired()],
    )
    submit = SubmitField(_l("Generate report"))
