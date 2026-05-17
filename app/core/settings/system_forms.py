from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import BooleanField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length


class SystemSettingsForm(FlaskForm):
    app_name = StringField(_l("Application Name"), validators=[DataRequired(), Length(max=128)])
    default_locale = SelectField(
        _l("Default Language"),
        choices=[("tr", "Türkçe"), ("en", "English")],
        validators=[DataRequired()],
    )
    oauth_auto_register = BooleanField(_l("Auto-register OAuth users"))
    registration_open = BooleanField(_l("Public registration enabled"))
    submit = SubmitField(_l("Save"))
