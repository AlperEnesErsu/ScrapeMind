from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import BooleanField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, Length, Optional


class UserEditForm(FlaskForm):
    full_name = StringField(_l("Full Name"), validators=[DataRequired(), Length(min=2, max=128)])
    email = StringField(_l("Email"), validators=[DataRequired(), Email(), Length(max=255)])
    is_active = BooleanField(_l("Active"))
    is_superuser = BooleanField(_l("Superuser"))
    avatar_url = StringField(_l("Avatar URL"), validators=[Optional(), Length(max=512)])
    submit = SubmitField(_l("Save"))
