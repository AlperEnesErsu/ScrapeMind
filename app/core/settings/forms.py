from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional

SUPPORTED_LOCALES = [("tr", "Türkçe"), ("en", "English")]
COMMON_TIMEZONES = [
    ("Europe/Istanbul", "Europe/Istanbul"),
    ("UTC", "UTC"),
    ("Europe/London", "Europe/London"),
    ("Europe/Berlin", "Europe/Berlin"),
    ("America/New_York", "America/New_York"),
    ("America/Los_Angeles", "America/Los_Angeles"),
    ("Asia/Tokyo", "Asia/Tokyo"),
]
THEMES = [("light", _l("Light")), ("dark", _l("Dark"))]


class PersonalInfoForm(FlaskForm):
    full_name = StringField(_l("Full Name"), validators=[DataRequired(), Length(min=2, max=128)])
    avatar_url = StringField(_l("Avatar URL"), validators=[Optional(), Length(max=512)])
    submit = SubmitField(_l("Save"))

    def validate_avatar_url(self, field):
        if not field.data:
            return
        from urllib.parse import urlparse
        import socket
        import ipaddress
        from wtforms.validators import ValidationError

        try:
            parsed = urlparse(field.data)
            if parsed.scheme not in ('http', 'https'):
                raise ValidationError(_l("URL must start with http:// or https://"))
            
            hostname = parsed.hostname
            if not hostname:
                raise ValidationError(_l("Invalid URL hostname."))
                
            try:
                ip = socket.gethostbyname(hostname)
            except Exception:
                raise ValidationError(_l("Unable to resolve hostname."))
                
            ip_obj = ipaddress.ip_address(ip)
            if ip_obj.is_loopback or ip_obj.is_private:
                raise ValidationError(_l("Private or local URLs are not allowed."))
        except ValidationError:
            raise
        except Exception:
            raise ValidationError(_l("Invalid URL format."))


class EmailChangeForm(FlaskForm):
    email = StringField(_l("Email"), validators=[DataRequired(), Email(), Length(max=255)])
    current_password = PasswordField(_l("Current Password"), validators=[DataRequired()])
    submit = SubmitField(_l("Save"))


class PasswordChangeForm(FlaskForm):
    from app.core.auth.password_policy import wtf_validator as _pw  # noqa: PLC0415

    current_password = PasswordField(_l("Current Password"), validators=[DataRequired()])
    new_password = PasswordField(_l("New Password"), validators=[DataRequired(), _pw])
    new_password2 = PasswordField(
        _l("Confirm New Password"),
        validators=[DataRequired(), EqualTo("new_password", message=_l("Passwords do not match."))],
    )
    submit = SubmitField(_l("Change Password"))


class PreferencesForm(FlaskForm):
    locale = SelectField(_l("Language"), choices=SUPPORTED_LOCALES, validators=[DataRequired()])
    timezone = SelectField(_l("Timezone"), choices=COMMON_TIMEZONES, validators=[DataRequired()])
    theme = SelectField(_l("Theme"), choices=THEMES, validators=[DataRequired()])
    submit = SubmitField(_l("Save"))
