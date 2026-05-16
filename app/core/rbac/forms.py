from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional


class RoleForm(FlaskForm):
    name = StringField(_l("Role Name"), validators=[DataRequired(), Length(min=2, max=64)])
    description = TextAreaField(_l("Description"), validators=[Optional(), Length(max=255)])
    submit = SubmitField(_l("Save"))
