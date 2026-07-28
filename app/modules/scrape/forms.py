from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, Optional


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
