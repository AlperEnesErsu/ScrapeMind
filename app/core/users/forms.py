from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, Length, Optional

from app.core.auth.password_policy import wtf_validator as _pw


class UserEditForm(FlaskForm):
    full_name = StringField(_l("Full Name"), validators=[DataRequired(), Length(min=2, max=128)])
    email = StringField(_l("Email"), validators=[DataRequired(), Email(), Length(max=255)])
    is_active = BooleanField(_l("Active"))
    is_superuser = BooleanField(_l("Superuser"))
    avatar_url = StringField(_l("Avatar URL"), validators=[Optional(), Length(max=512)])
    submit = SubmitField(_l("Save"))

    def validate_avatar_url(self, field):
        from app.core.settings.forms import PersonalInfoForm

        PersonalInfoForm().validate_avatar_url(field)


class UserCreateForm(FlaskForm):
    username = StringField(_l("Username"), validators=[DataRequired(), Length(min=3, max=64)])
    full_name = StringField(_l("Full Name"), validators=[DataRequired(), Length(min=2, max=128)])
    email = StringField(_l("Email"), validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField(_l("Password"), validators=[DataRequired(), _pw])
    is_active = BooleanField(_l("Active"), default=True)
    is_superuser = BooleanField(_l("Superuser"))
    avatar_url = StringField(_l("Avatar URL"), validators=[Optional(), Length(max=512)])
    submit = SubmitField(_l("Create User"))

    def validate_avatar_url(self, field):
        from app.core.settings.forms import PersonalInfoForm

        PersonalInfoForm().validate_avatar_url(field)
