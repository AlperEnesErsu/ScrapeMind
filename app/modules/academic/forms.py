from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length


class AddIdentifierForm(FlaskForm):
    type_code = SelectField(_l("Type"), choices=[], validators=[DataRequired()])
    value = StringField(_l("Value"), validators=[DataRequired(), Length(max=255)])
    submit = SubmitField(_l("Add"))


class AddKeywordForm(FlaskForm):
    value = StringField(_l("Keyword"), validators=[DataRequired(), Length(min=2, max=64)])
    submit = SubmitField(_l("Add"))
