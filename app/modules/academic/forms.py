from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired, Email, Length


class AddEmailForm(FlaskForm):
    email = StringField(_l("Email"), validators=[DataRequired(), Email(), Length(max=255)])
    submit = SubmitField(_l("Add"))
