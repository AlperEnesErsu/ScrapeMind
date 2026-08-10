from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange

from app.core.settings.toggle_registry import all_system_toggles


class SystemSettingsForm(FlaskForm):
    app_name = StringField(_l("Application Name"), validators=[DataRequired(), Length(max=128)])
    default_locale = SelectField(
        _l("Default Language"),
        choices=[("tr", "Türkçe"), ("en", "English")],
        validators=[DataRequired()],
    )
    oauth_auto_register = BooleanField(_l("Auto-register OAuth users"))
    registration_open = BooleanField(_l("Public registration enabled"))
    # 0 is a valid value here — it means the feature is effectively disabled
    # for everyone, not "unset". A blank submission is required to fail too
    # (no "use config default" state): IntegerField's own int() coercion
    # already rejects an empty string with "Not a valid integer value"
    # before NumberRange even runs, so no separate DataRequired is needed.
    max_user_channels = IntegerField(
        _l("Max YouTube channels per user"),
        validators=[NumberRange(min=0, max=200)],
    )
    submit = SubmitField(_l("Save"))


def build_system_settings_form(*args, **kwargs) -> SystemSettingsForm:
    """`SystemSettingsForm` plus one BooleanField per module-registered toggle.

    Built per request rather than at import time: `register_system_toggle` runs
    when a module is imported, which happens after this module is first read,
    and a deployment that trims `app/modules/` should not carry fields for
    toggles nobody registered.

    The subclass is throwaway — WTForms binds fields per instance, so adding
    attributes to a fresh class each call cannot leak between requests.
    """

    class _Form(SystemSettingsForm):
        pass

    for toggle in all_system_toggles():
        setattr(_Form, toggle.key, BooleanField(_l(toggle.label)))
    return _Form(*args, **kwargs)
