from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange


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
